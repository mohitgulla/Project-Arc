"""E10.1 (D44): experiment registry, pre-registration lock, one running per area, arm_id.

Every test injects ``now``; nothing reads the wall clock.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sqlite3
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from arc.backtest import ranking as backtest_ranking
from arc.config import ArcSettings
from arc.control.effective import effective_settings, experiments_config
from arc.control.service import ControlService
from arc.experiments.cli import add_experiment_parser, run_experiment
from arc.experiments.config import ExperimentDefaults, load_experiments_config
from arc.experiments.models import (
    ExperimentSpec,
    ExperimentStatus,
    RunningDetail,
    StopDetail,
    StopReason,
    arm_id,
    canonical_json,
    spec_hash,
)
from arc.experiments.overlay import arm_config_data, fill_defaults, load_spec, validate_arms
from arc.experiments.store import (
    AaRequiredError,
    ExperimentStore,
    OwnerApprovalError,
    SpecLockedError,
    TransitionError,
    check_owner_approval,
)
from arc.journal.reasons import ReasonCode, Stage
from arc.journal.store import JournalStore
from arc.store.db import connect
from arc.store.migrate import migrate
from arc.utils import yamlpatch
from arc.utils.calendar import ET

REPO = Path(__file__).resolve().parent.parent
LIVE = REPO / "config" / "experiments" / "live"
NOW = dt.datetime(2026, 10, 5, 10, 0, tzinfo=ET)
OWNER = "local"
ARM_TABLES = ("run_manifests", "proposals", "decisions", "outcomes", "pnl_snapshots", "executions")


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = connect(":memory:")
    migrate(c)
    return c


def _clock() -> object:
    t = [NOW]

    def now() -> dt.datetime:
        t[0] += dt.timedelta(minutes=1)
        return t[0]

    return now


@pytest.fixture
def store(conn: sqlite3.Connection) -> ExperimentStore:
    return ExperimentStore(conn, now=_clock())  # type: ignore[arg-type]


def _spec(
    eid: str = "XP-1", *, area: str = "other", kind: str = "aa", **kw: object
) -> ExperimentSpec:
    data: dict[str, object] = {
        "id": eid,
        "title": f"{eid} title",
        "hypothesis": "noise floor",
        "area": area,
        "kind": kind,
        "proposed_by": "owner",
    }
    if kind == "ab":
        data |= {
            "arms": {"treatment": {"overlay": {"exits": {"default": {"take_profit_pct": 0.4}}}}},
            "backtest_ref": "data/backtests/e75a/compare.json",
            "non_inferiority_margin": 0.1,
        }
    data |= kw
    return fill_defaults(ExperimentSpec.model_validate(data), ExperimentDefaults())


def _running() -> RunningDetail:
    return RunningDetail(t0=NOW, t0_equity=100_000.0, control_sha="53e5351abc")


# ---------------------------------------------------------------------------
# Contracts
# ---------------------------------------------------------------------------


def test_spec_hash_is_canonical_and_order_independent() -> None:
    a = _spec()
    b = ExperimentSpec.model_validate(json.loads(canonical_json(a)))
    assert spec_hash(a) == spec_hash(b)
    assert canonical_json(a) == json.dumps(
        json.loads(canonical_json(a)), sort_keys=True, separators=(",", ":")
    )
    assert spec_hash(a) != spec_hash(a.model_copy(update={"title": "other"}))


@pytest.mark.parametrize(
    ("patch", "match"),
    [
        ({"id": "X1"}, "XP-<n>"),
        ({"proposed_by": "sentinel"}, "proposed_by"),
        ({"arms": {"control": {"overlay": {"exits": {"a": 1}}}}}, "control arm"),
        ({"arms": {"treatment": {"overlay": {"exits": {"a": 1}}}}}, "identical arms"),
        ({"arms": {"treatment": {"overlay": {"gate": {"a": 1}}}}}, "not one of"),
        ({"arms": {"treatment": {"overlay": {"exits": {}}}}}, "empty"),
        ({"min_sessions": 30, "max_sessions": 20}, "min_sessions"),
        ({"surprise": 1}, "Extra inputs"),
    ],
)
def test_spec_validation_refuses(patch: dict[str, object], match: str) -> None:
    data = {
        "id": "XP-1",
        "title": "t",
        "hypothesis": "h",
        "area": "other",
        "kind": "aa",
        "proposed_by": "A-3",
    } | patch
    with pytest.raises(ValidationError, match=match):
        ExperimentSpec.model_validate(data)


@pytest.mark.parametrize("missing", ["arms", "backtest_ref", "non_inferiority_margin"])
def test_ab_spec_needs_overlay_backtest_and_margin(missing: str) -> None:
    data = {
        "id": "XP-2",
        "title": "t",
        "hypothesis": "h",
        "area": "exits",
        "kind": "ab",
        "proposed_by": "owner",
        "arms": {"treatment": {"overlay": {"exits": {"default": {"take_profit_pct": 0.4}}}}},
        "backtest_ref": "ref",
        "non_inferiority_margin": 0.1,
    }
    data.pop(missing)
    with pytest.raises(ValidationError):
        ExperimentSpec.model_validate(data)


def test_fill_defaults_from_config_and_aa_length() -> None:
    d = load_experiments_config().defaults
    assert (d.alpha, d.power, d.min_sessions, d.max_sessions) == (0.05, 0.8, 20, 60)
    assert not hasattr(d, "guardrails")  # owner 2026-10-03: no guardrail stops
    aa = _spec()
    assert (aa.min_sessions, aa.max_sessions) == (10, 10)  # A/A runs aa_sessions
    ab = _spec("XP-2", area="exits", kind="ab")
    assert (ab.min_sessions, ab.max_sessions, ab.alpha) == (20, 60, 0.05)
    pinned = _spec("XP-3", alpha=0.01, min_sessions=15)
    assert pinned.alpha == 0.01 and pinned.min_sessions == 15
    assert (
        aa.complete
        and not ExperimentSpec.model_validate(aa.model_dump() | {"alpha": None}).complete
    )


def test_arm_id_null_means_control() -> None:
    assert arm_id("XP-1", "control") is None
    assert arm_id("XP-1", "treatment") == "XP-1:treatment"


# ---------------------------------------------------------------------------
# Overlay: the SAME deep-merge as `arc backtest rank --experiment`
# ---------------------------------------------------------------------------


def test_overlay_reuses_the_backtest_deep_merge() -> None:
    from arc.experiments import overlay

    assert overlay.deep_merge is yamlpatch.deep_merge
    assert backtest_ranking.deep_merge is yamlpatch.deep_merge
    assert not hasattr(backtest_ranking, "_deep_merge")  # no fork left behind


def test_backtest_overlay_file_drives_a_forward_arm_identically() -> None:
    """A backtest overlay file, used as a forward treatment overlay, merges the same way."""
    path = REPO / "config/experiments/e75a_b_short_dte.yaml"
    body = yamlpatch.overlay_body(yaml.safe_load(path.read_text()))
    spec = ExperimentSpec.model_validate(
        {
            "id": "XP-9",
            "title": "short DTE",
            "hypothesis": "h",
            "area": "entries",
            "kind": "ab",
            "proposed_by": "A-4",
            "backtest_ref": "e75a_b",
            "non_inferiority_margin": 0.1,
            "arms": {"treatment": {"overlay": {"ranking": body}}},
        }
    )
    forward = arm_config_data(spec, "treatment", "ranking")
    backtest = backtest_ranking.load_ranking_file(None, [path])
    assert backtest_ranking.RankingFile.model_validate(forward) == backtest
    assert forward["backtest"]["dte_windows"]["cash_debit"] == [21, 35]
    assert arm_config_data(spec, "control", "ranking") == yaml.safe_load(
        (REPO / "config/ranking.yaml").read_text()
    )


def test_deep_merge_semantics_and_purity() -> None:
    base = {"a": {"b": 1, "c": [1, 2]}, "d": 1}
    over = {"a": {"c": [3]}, "e": {"f": 1}}
    out = yamlpatch.deep_merge(base, over)
    assert out == {"a": {"b": 1, "c": [3]}, "d": 1, "e": {"f": 1}}
    out["a"]["b"] = 99
    assert base["a"]["b"] == 1  # inputs untouched
    assert yamlpatch.overlay_body({"experiment": "x", "k": 1}) == {"k": 1}
    assert yamlpatch.overlay_body(None) == {}


def test_bad_overlay_fails_at_load(tmp_path: Path) -> None:
    spec = _spec("XP-2", area="exits", kind="ab").model_copy(
        update={
            "arms": ExperimentSpec.model_validate(
                _spec("XP-2", area="exits", kind="ab").model_dump()
                | {"arms": {"treatment": {"overlay": {"exits": {"not_a_knob": 1}}}}}
            ).arms
        }
    )
    with pytest.raises(ValueError, match=r"treatment overlay for exits\.yaml"):
        validate_arms(spec)
    p = tmp_path / "bad.yaml"
    p.write_text(yaml.safe_dump(spec.model_dump(mode="json")))
    with pytest.raises(ValueError, match="does not validate"):
        load_spec(p)


@pytest.mark.parametrize("path", sorted(LIVE.glob("*.yaml")), ids=lambda p: p.name)
def test_every_live_spec_loads(path: Path) -> None:
    spec = load_spec(path)
    assert path.stem.lower().startswith(spec.id.lower().replace("-", ""))


# ---------------------------------------------------------------------------
# Store: hash lock, one per area, A/A first
# ---------------------------------------------------------------------------


def test_create_revises_draft_and_register_locks(store: ExperimentStore) -> None:
    s = store.create(_spec(), actor=OWNER, owner_approval="P-1")
    assert (s.status, s.revision, s.registered_hash) == (ExperimentStatus.DRAFT, 1, None)
    assert (
        store.create(_spec(), actor=OWNER, owner_approval="P-1").revision == 1
    )  # identical: no new revision
    s = store.create(_spec(title="better title"), actor=OWNER, owner_approval="P-1")
    assert s.revision == 2 and s.spec.title == "better title"
    r = store.register("XP-1", actor=OWNER, owner_approval="P-1")
    assert r.status is ExperimentStatus.REGISTERED
    assert r.registered_hash == spec_hash(r.spec) == r.spec_hash
    with pytest.raises(SpecLockedError, match="new id"):
        store.create(_spec(title="sneaky edit"), actor=OWNER, owner_approval="P-1")
    with pytest.raises(TransitionError):
        store.register("XP-1", actor=OWNER, owner_approval="P-1")
    assert store.verify("XP-1")["ok"]


def test_lock_is_enforced_by_the_db_too(conn: sqlite3.Connection, store: ExperimentStore) -> None:
    store.create(_spec(), actor=OWNER, owner_approval="P-1")
    store.register("XP-1", actor=OWNER, owner_approval="P-1")
    with pytest.raises(sqlite3.IntegrityError, match="locked after registration"):
        conn.execute(
            """INSERT INTO experiments (experiment_id, revision, spec_version, area, kind,
               spec, spec_hash, actor, created_at) VALUES ('XP-1', 9, 1, 'other', 'aa', '{}',
               'x', 'evil', '2026-10-05T14:00:00.000000Z')"""
        )
    for table in ("experiments", "experiment_events"):
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(f"UPDATE {table} SET actor = 'x'")  # noqa: S608
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(f"DELETE FROM {table}")  # noqa: S608


def test_verify_detects_a_tampered_spec(conn: sqlite3.Connection, store: ExperimentStore) -> None:
    store.create(_spec(), actor=OWNER, owner_approval="P-1")
    store.register("XP-1", actor=OWNER, owner_approval="P-1")
    conn.execute("DROP TRIGGER experiments_no_update")  # simulate out-of-band tampering
    row = conn.execute("SELECT spec FROM experiments").fetchone()
    tampered = json.loads(row[0]) | {"alpha": 0.2}
    conn.execute("UPDATE experiments SET spec = ?", (json.dumps(tampered),))
    v = store.verify("XP-1")
    assert not v["ok"] and v["recomputed_hash"] != v["registered_hash"]
    with pytest.raises(SpecLockedError, match="mismatch"):
        store.start("XP-1", _running(), actor=OWNER)


def test_one_registered_or_running_per_area_others_queue(store: ExperimentStore) -> None:
    for eid in ("XP-1", "XP-2", "XP-3"):
        store.create(_spec(eid, area="other", kind="ab"), actor=OWNER, owner_approval="P-1")
    store.create(_spec("XP-4", area="ranking", kind="ab"), actor=OWNER, owner_approval="P-1")
    assert (
        store.register("XP-1", actor=OWNER, owner_approval="P-1").status
        is ExperimentStatus.REGISTERED
    )
    assert (
        store.register("XP-2", actor=OWNER, owner_approval="P-1").status is ExperimentStatus.QUEUED
    )
    assert (
        store.register("XP-3", actor=OWNER, owner_approval="P-1").status is ExperimentStatus.QUEUED
    )
    assert (
        store.register("XP-4", actor=OWNER, owner_approval="P-1").status
        is ExperimentStatus.REGISTERED
    )  # other area
    # a queued spec is locked too
    with pytest.raises(SpecLockedError):
        store.create(_spec("XP-2", kind="ab", title="edit"), actor=OWNER, owner_approval="P-1")
    store.start("XP-1", _running(), actor=OWNER, aa_override=True)
    assert [s.experiment_id for s in store.active_in_area("other")] == ["XP-1"]
    with pytest.raises(TransitionError):
        store.start("XP-2", _running(), actor=OWNER, aa_override=True)  # queued cannot start
    store.stop("XP-1", StopReason.FUTILITY, actor=OWNER, detail=StopDetail(sigma=0.4))
    # the oldest queued experiment takes the area; the next stays queued
    assert store.require("XP-2").status is ExperimentStatus.REGISTERED
    assert store.require("XP-3").status is ExperimentStatus.QUEUED
    assert len(store.active_in_area("other")) == 1


def test_ab_needs_aa_sigma_unless_owner_overrides(
    conn: sqlite3.Connection, store: ExperimentStore
) -> None:
    store.create(_spec("XP-2", area="exits", kind="ab"), actor=OWNER, owner_approval="P-1")
    store.register("XP-2", actor=OWNER, owner_approval="P-1")
    with pytest.raises(AaRequiredError, match="A/A"):
        store.start("XP-2", _running(), actor=OWNER)
    with pytest.raises(AaRequiredError, match="owner"):
        store.start("XP-2", _running(), actor="arc.experiments", aa_override=True)
    s = store.start("XP-2", _running(), actor=OWNER, aa_override=True)
    assert s.status is ExperimentStatus.RUNNING and s.running and s.running.aa_override
    codes = [d.reason_code for d in JournalStore(conn).decisions() if d.subject == "XP-2"]
    assert ReasonCode.EXPERIMENT_AA_OVERRIDE in codes


def test_ab_starts_after_an_aa_recorded_sigma(store: ExperimentStore) -> None:
    store.create(_spec("XP-1"), actor=OWNER, owner_approval="P-1")
    store.register("XP-1", actor=OWNER, owner_approval="P-1")
    store.start("XP-1", _running(), actor=OWNER)
    store.stop("XP-1", StopReason.FUTILITY, actor=OWNER)  # no sigma: does not count
    assert store.aa_sigma() is None
    store.create(_spec("XP-3"), actor=OWNER, owner_approval="P-1")
    store.register("XP-3", actor=OWNER, owner_approval="P-1")
    store.start("XP-3", _running(), actor=OWNER)
    store.stop("XP-3", StopReason.FUTILITY, actor=OWNER, detail=StopDetail(sigma=0.35))
    assert store.aa_sigma() == 0.35
    store.create(_spec("XP-2", area="exits", kind="ab"), actor=OWNER, owner_approval="P-1")
    store.register("XP-2", actor=OWNER, owner_approval="P-1")
    s = store.start("XP-2", _running(), actor="arc.experiments")
    assert s.status is ExperimentStatus.RUNNING and s.running and not s.running.aa_override
    assert s.running.t0 == NOW and s.running.t0_equity == 100_000.0


def test_lifecycle_transitions_and_journal(
    conn: sqlite3.Connection, store: ExperimentStore
) -> None:
    store.create(_spec(), actor=OWNER, owner_approval="P-1")
    with pytest.raises(TransitionError):
        store.stop("XP-1", StopReason.OWNER, actor=OWNER)  # draft cannot stop
    store.register("XP-1", actor=OWNER, owner_approval="P-1")
    with pytest.raises(TransitionError):
        store.decide("XP-1", promote=True, actor=OWNER)
    store.start("XP-1", _running(), actor=OWNER)
    s = store.stop("XP-1", StopReason.HARM, actor=OWNER, detail=StopDetail(note="worst day"))
    assert s.status is ExperimentStatus.STOPPED and s.reason is StopReason.HARM
    s = store.decide("XP-1", promote=False, actor=OWNER)
    assert s.status is ExperimentStatus.REJECTED and s.reason is StopReason.HARM
    with pytest.raises(TransitionError):
        store.stop("XP-1", StopReason.OWNER, actor=OWNER)
    decs = [d for d in JournalStore(conn).decisions() if d.stage is Stage.EXPERIMENT]
    assert [d.reason_code for d in decs] == [
        ReasonCode.EXPERIMENT_DRAFTED,
        ReasonCode.EXPERIMENT_REGISTERED,
        ReasonCode.EXPERIMENT_STARTED,
        ReasonCode.EXPERIMENT_STOPPED,
        ReasonCode.EXPERIMENT_REJECTED,
    ]
    assert all(d.payload["experiment_id"] == "XP-1" for d in decs)
    assert [e.status.value for e in s.events] == [
        "draft",
        "registered",
        "running",
        "stopped",
        "rejected",
    ]


def test_register_refuses_incomplete_spec(store: ExperimentStore) -> None:
    raw = ExperimentSpec.model_validate(_spec().model_dump() | {"alpha": None})
    store.create(raw, actor=OWNER, owner_approval="P-1")
    with pytest.raises(ValueError, match="unset defaults"):
        store.register("XP-1", actor=OWNER, owner_approval="P-1")


def test_unknown_experiment(store: ExperimentStore) -> None:
    assert store.get("XP-404") is None
    with pytest.raises(ValueError, match="unknown experiment"):
        store.register("XP-404", actor=OWNER, owner_approval="P-1")


# ---------------------------------------------------------------------------
# arm_id columns
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("table", ARM_TABLES)
def test_arm_id_column_present_nullable_default_control(
    conn: sqlite3.Connection, table: str
) -> None:
    cols = {r[1]: r for r in conn.execute(f"PRAGMA table_info({table})")}  # noqa: S608
    assert "arm_id" in cols
    _cid, _name, typ, notnull, default, _pk = cols["arm_id"]
    assert typ == "TEXT" and notnull == 0 and default is None  # NULL = control


def test_existing_writers_leave_arm_id_null(conn: sqlite3.Connection) -> None:
    """Rows written by today's (pre-E10.2) code paths are control rows."""
    JournalStore(conn).record(
        persona="system",
        stage="experiment",
        subject="XP-1",
        choice="noted",
        reason_code="experiment:drafted",
        at=NOW,
    )
    assert conn.execute("SELECT arm_id FROM decisions").fetchone()[0] is None


# ---------------------------------------------------------------------------
# Control panel: every new knob is registered and reaches the effective config
# ---------------------------------------------------------------------------


def test_experiment_defaults_are_tunable_and_reach_effective_config(
    conn: sqlite3.Connection,
) -> None:
    from arc.control.registry import REGISTRY, Target

    keys = {k for k, t in REGISTRY.items() if t.target is Target.EXPERIMENTS}
    assert keys >= {
        "experiments.alpha",
        "experiments.power",
        "experiments.min_sessions",
        "experiments.max_sessions",
        "experiments.aa_sessions",
    }
    assert not any("guardrails" in k for k in keys)
    svc = ControlService(conn, base=ArcSettings(), now=lambda: NOW, is_halted=lambda: False)
    # stricter alpha is safer: applies immediately
    assert svc.set("experiments.alpha", "0.01", actor=OWNER, source="cli").outcome == "applied"
    # fewer sessions is riskier: needs a confirm
    r = svc.set("experiments.min_sessions", "15", actor=OWNER, source="cli")
    assert r.pending is not None
    svc.confirm(r.pending.code, actor=OWNER, source="cli")
    assert svc.set("experiments.alpha", "0.5", actor=OWNER, source="cli").outcome == "refused"
    cfg = experiments_config(effective_settings(conn)).defaults
    assert cfg.alpha == 0.01 and cfg.min_sessions == 15
    assert experiments_config(svc.settings()).defaults.alpha == 0.01


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _cli(*argv: str) -> argparse.Namespace:
    p = argparse.ArgumentParser()
    add_experiment_parser(p.add_subparsers(dest="command"))
    return p.parse_args(["experiment", *argv])


def test_cli_create_register_show_verify_stop(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = str(tmp_path / "x.db")
    spec = str(LIVE / "xp1_aa_baseline.yaml")
    assert (
        run_experiment(_cli("create", "--owner-approval", "P-1", "--spec", spec, "--db", db)) == 0
    )
    assert "XP-1  draft" in capsys.readouterr().out
    assert run_experiment(_cli("register", "--owner-approval", "P-1", "XP-1", "--db", db)) == 0
    assert "XP-1  registered" in capsys.readouterr().out
    assert run_experiment(_cli("show", "XP-1", "--db", db, "--json")) == 0
    out = capsys.readouterr().out
    shown = json.loads(out[out.index("{\n") :])  # structlog prints to stdout under pytest
    assert shown["status"] == "registered" and shown["registered_hash"] == shown["spec_hash"]
    assert shown["spec"]["min_sessions"] == 10 and shown["spec"]["alpha"] == 0.05
    assert run_experiment(_cli("show", "XP-1", "--db", db)) == 0
    assert "registered sha256" in capsys.readouterr().out
    assert run_experiment(_cli("verify", "XP-1", "--db", db)) == 0
    assert ": OK" in capsys.readouterr().out
    assert run_experiment(_cli("list", "--db", db)) == 0
    assert "XP-1" in capsys.readouterr().out
    # re-create after registration is refused (exit 2)
    assert (
        run_experiment(_cli("create", "--owner-approval", "P-1", "--spec", spec, "--db", db)) == 2
    )
    assert "locked" in capsys.readouterr().err
    assert run_experiment(_cli("stop", "XP-1", "--reason", "owner", "--actor", "U_NOBODY",
                               "--db", db)) == 2  # fmt: skip
    assert run_experiment(_cli("stop", "XP-1", "--reason", "owner", "--actor", "local",
                               "--db", db)) == 0  # fmt: skip
    assert "stopped (owner)" in capsys.readouterr().out
    assert run_experiment(_cli("list", "--status", "running", "--db", db)) == 0
    assert "no experiments" in capsys.readouterr().out


def test_cli_rejects_bad_spec_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("id: XP-1\ntitle: t\n")
    assert (
        run_experiment(
            _cli(
                "create",
                "--owner-approval",
                "P-1",
                "--spec",
                str(bad),
                "--db",
                str(tmp_path / "x.db"),
            )
        )
        == 2
    )
    assert "invalid spec" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# D86 (E21.2): no experiment rows without an owner-approval reference
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("ref", ["P-1", "P-12", "slack:1791668051.305679", "owner:A/A rerun"])
def test_owner_approval_refs_accepted(ref: str) -> None:
    assert check_owner_approval(f" {ref} ") == ref


@pytest.mark.parametrize(
    "ref", [None, "", "  ", "P-0", "P-01", "p-1", "XP-1", "slack:", "slack:abc", "owner:", "yes"]
)
def test_owner_approval_refs_refused(ref: str | None) -> None:
    with pytest.raises(OwnerApprovalError, match="arc experiment adopt"):
        check_owner_approval(ref)


def test_store_create_and_register_need_owner_approval(
    conn: sqlite3.Connection, store: ExperimentStore
) -> None:
    with pytest.raises(OwnerApprovalError, match="owner approval required"):
        store.create(_spec(), actor=OWNER, owner_approval="")
    assert store.get("XP-1") is None  # nothing written
    store.create(_spec(), actor=OWNER, owner_approval="slack:1791668051.305679")
    with pytest.raises(OwnerApprovalError, match="invalid owner approval 'later'"):
        store.register("XP-1", actor=OWNER, owner_approval="later")
    assert store.require("XP-1").status is ExperimentStatus.DRAFT
    st = store.register("XP-1", actor=OWNER, owner_approval="P-3")
    details = [(e.status.value, e.detail.get("owner_approval")) for e in st.events]
    assert details == [("draft", "slack:1791668051.305679"), ("registered", "P-3")]


def test_cli_create_and_register_refuse_without_owner_approval(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = str(tmp_path / "x.db")
    spec = str(LIVE / "xp1_aa_baseline.yaml")
    assert run_experiment(_cli("create", "--spec", spec, "--db", db)) == 2
    err = capsys.readouterr().err
    assert "arc experiment create: owner approval required" in err
    assert "arc experiment adopt P-<n>" in err and "PLAN D86" in err
    assert run_experiment(_cli("list", "--db", db)) == 0
    assert "no experiments" in capsys.readouterr().out
    # refused before the spec is read: a bad path still names the approval
    assert run_experiment(_cli("create", "--spec", "nope.yaml", "--db", db)) == 2
    assert "owner approval required" in capsys.readouterr().err
    assert (
        run_experiment(_cli("create", "--owner-approval", "P-7", "--spec", spec, "--db", db)) == 0
    )
    capsys.readouterr()
    assert run_experiment(_cli("register", "XP-1", "--db", db)) == 2
    assert "arc experiment register: owner approval required" in capsys.readouterr().err
    assert run_experiment(_cli("register", "--owner-approval", "P-7", "XP-1", "--db", db)) == 0
    capsys.readouterr()
    assert run_experiment(_cli("show", "XP-1", "--db", db)) == 0
    out = capsys.readouterr().out
    assert "draft by local (approval P-7)" in out and "registered by local (approval P-7)" in out


@pytest.mark.parametrize("bad", ["X-1", "XP-0", "xp-1", "XP-01", "XP-", "XP1"])
def test_experiment_ids_are_xp_n(bad: str) -> None:
    """Owner, 2026-10-03: experiment ids are ``XP-<n>`` (renamed from ``X-<n>``)."""
    data = _spec("XP-1").model_dump(mode="json") | {"id": bad}
    with pytest.raises(ValidationError, match="XP-<n>"):
        ExperimentSpec.model_validate(data)
    assert arm_id("XP-22", "treatment") == "XP-22:treatment"
