"""E25.10 (D122): owner amendment of a registered/running XP's ``analysis`` block.

Every test injects ``now``; nothing reads the wall clock.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
import pytest

from arc.experiments.cli import add_experiment_parser, run_experiment
from arc.experiments.config import ExperimentsConfig
from arc.experiments.evaluate import ExperimentReport, build_report
from arc.experiments.models import (
    Analysis,
    ExperimentStatus,
    StopDetail,
    StopReason,
    canonical_json,
    spec_hash,
)
from arc.experiments.store import (
    ExperimentError,
    ExperimentStore,
    OwnerApprovalError,
    SpecLockedError,
    TransitionError,
)
from arc.journal.reasons import ReasonCode
from arc.store.db import connect
from arc.store.migrate import migrate
from arc.utils.calendar import previous_session
from tests import experiment_fixtures as fx
from tests.test_experiment_hac import FakeSpy, _after, _closes

if TYPE_CHECKING:
    import datetime as dt

REPO = Path(__file__).resolve().parent.parent
LIVE = REPO / "config" / "experiments" / "live"
ADJ = Analysis(covariates=["spy_ret"], hac_lag=5)
CFG = ExperimentsConfig()


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = connect(":memory:")
    migrate(c)
    return c


def _amend(store: ExperimentStore, eid: str = "XP-2", **kw: object):  # type: ignore[no-untyped-def]
    args: dict[str, object] = {
        "actor": "local",
        "owner_approval": "slack:1792193805.441269",
        "sessions_at_amend": 3,
        "reason": "#project-arc 1792193805.441269",
    }
    args |= kw
    return store.amend_analysis(eid, ADJ, **args)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# store: the audited path
# ---------------------------------------------------------------------------


def test_amend_appends_a_revision_and_an_event_and_keeps_registration(
    conn: sqlite3.Connection,
) -> None:
    store = fx.start(conn, fx.spec("XP-2"))
    before = store.require("XP-2")
    assert before.spec.analysis is None and before.amendments == []
    st = _amend(store)
    assert st.status is ExperimentStatus.RUNNING  # status unchanged
    assert st.revision == before.revision + 1 and st.spec.analysis == ADJ
    assert st.registered_hash == before.registered_hash  # nothing rewritten
    assert st.spec_hash == spec_hash(st.spec) != before.spec_hash
    assert st.running == before.running  # the start detail still comes from the start
    rows = conn.execute(
        "SELECT revision, spec_hash, amends_revision FROM experiments ORDER BY revision"
    ).fetchall()
    assert [tuple(r) for r in rows] == [
        (1, before.spec_hash, None),
        (2, st.spec_hash, 1),
    ]
    ev = conn.execute(
        "SELECT status, spec_hash, detail FROM experiment_events ORDER BY id DESC LIMIT 1"
    ).fetchone()
    detail = json.loads(ev["detail"])
    assert ev["status"] == "running" and ev["spec_hash"] == st.spec_hash
    assert detail == {
        "amended": "analysis",
        "from": None,
        "to": {"covariates": ["spy_ret"], "hac_lag": 5},
        "from_hash": before.spec_hash,
        "to_hash": st.spec_hash,
        "revision": 2,
        "amends_revision": 1,
        "sessions_at_amend": 3,
        "owner_approval": "slack:1792193805.441269",
        "reason": "#project-arc 1792193805.441269",
    }
    (a,) = st.amendments
    assert a.from_ is None and a.to == ADJ and a.sessions_at_amend == 3
    assert st.analysis_amended is not None and st.analysis_amended.to == ADJ
    j = conn.execute(
        "SELECT reason_code, choice FROM decisions WHERE subject = 'XP-2' ORDER BY rowid DESC"
    ).fetchone()
    assert j["reason_code"] == ReasonCode.EXPERIMENT_AMENDED.value
    v = store.verify("XP-2")
    assert v["ok"] and v["lock_hash"] == st.spec_hash and v["problems"] == []
    assert len(v["chain"]) == 2


def test_registered_not_yet_running_may_be_amended(conn: sqlite3.Connection) -> None:
    store = ExperimentStore(conn, now=lambda: fx.T0)
    sp = fx.spec("XP-2")
    store.create(sp, actor="local", owner_approval="XP-2")
    store.register("XP-2", actor="local", owner_approval="XP-2")
    st = _amend(store)
    assert st.status is ExperimentStatus.REGISTERED and st.spec.analysis == ADJ
    assert store.verify("XP-2")["ok"]


def test_two_amendments_chain_and_back_to_none(conn: sqlite3.Connection) -> None:
    store = fx.start(conn, fx.spec("XP-2"))
    _amend(store)
    st = store.amend_analysis(
        "XP-2", None, actor="local", owner_approval="owner:revert", sessions_at_amend=4
    )
    assert st.spec.analysis is None and st.revision == 3 and len(st.amendments) == 2
    v = store.verify("XP-2")
    assert v["ok"], v
    assert len(v["chain"]) == 3
    assert spec_hash(st.spec) == st.registered_hash  # back to the registered spec


def test_non_owner_actor_refused(conn: sqlite3.Connection) -> None:
    store = fx.start(conn, fx.spec("XP-2"))
    with pytest.raises(OwnerApprovalError, match="only the owner"):
        _amend(store, actor="arc-analyst")
    with pytest.raises(OwnerApprovalError, match="only the owner"):
        _amend(store, actor="U_NOBODY", is_owner=lambda a: a == "U_OWNER")
    assert _amend(store, actor="U_OWNER", is_owner=lambda a: a == "U_OWNER").revision == 2


def test_missing_owner_approval_refused(conn: sqlite3.Connection) -> None:
    store = fx.start(conn, fx.spec("XP-2"))
    with pytest.raises(OwnerApprovalError):
        _amend(store, owner_approval="")


def test_draft_refused(conn: sqlite3.Connection) -> None:
    store = ExperimentStore(conn, now=lambda: fx.T0)
    store.create(fx.spec("XP-2"), actor="local", owner_approval="XP-2")
    with pytest.raises(TransitionError, match="draft"):
        _amend(store)


def test_concluded_refused(conn: sqlite3.Connection) -> None:
    store = fx.start(conn, fx.spec("XP-2"))
    store.stop("XP-2", StopReason.OWNER, actor="local", detail=StopDetail())
    with pytest.raises(TransitionError, match="stopped"):
        _amend(store)


def test_unchanged_analysis_refused(conn: sqlite3.Connection) -> None:
    store = fx.start(conn, fx.spec("XP-2", analysis=ADJ.model_dump()))
    with pytest.raises(ExperimentError, match="already"):
        _amend(store)
    with pytest.raises(ExperimentError, match="sessions_at_amend"):
        _amend(store, sessions_at_amend=-1)


def test_trigger_refuses_a_non_analysis_diff_and_plain_revisions(
    conn: sqlite3.Connection,
) -> None:
    """The migration is the backstop: a hand-written insert that changes anything but
    ``analysis`` (or is not flagged as an amendment) is refused by the DB itself."""
    store = fx.start(conn, fx.spec("XP-2"))
    st = store.require("XP-2")
    base = st.spec.model_dump(mode="json")

    def insert(doc: dict, *, amends: int | None, revision: int = 2) -> None:
        from arc.experiments.models import ExperimentSpec

        sp = ExperimentSpec.model_validate(doc)
        conn.execute(
            """INSERT INTO experiments (experiment_id, revision, spec_version, area, kind,
                 spec, spec_hash, actor, created_at, amends_revision)
               VALUES ('XP-2', ?, ?, ?, ?, ?, ?, 'local', '2026-10-05T09:00:00-04:00', ?)""",
            (revision, sp.spec_version, sp.area.value, sp.kind.value, canonical_json(sp),
             spec_hash(sp), amends),
        )  # fmt: skip

    adj = ADJ.model_dump()
    with pytest.raises(sqlite3.IntegrityError, match="only the analysis"):
        insert(base | {"analysis": adj, "alpha": 0.1}, amends=1)
    with pytest.raises(sqlite3.IntegrityError, match="only the analysis"):
        insert(base | {"analysis": adj}, amends=1, revision=3)  # skips a revision
    with pytest.raises(sqlite3.IntegrityError, match="locked after registration"):
        insert(base | {"analysis": adj}, amends=None)
    insert(base | {"analysis": adj}, amends=1)  # the narrow path itself is allowed
    with pytest.raises(sqlite3.IntegrityError, match="only the analysis"):
        insert(base | {"analysis": None}, amends=1, revision=3)  # not the latest
    # the table stays append-only
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE experiments SET spec_hash = 'x' WHERE revision = 2")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM experiments WHERE revision = 2")


def test_trigger_refuses_an_amendment_of_a_draft(conn: sqlite3.Connection) -> None:
    store = ExperimentStore(conn, now=lambda: fx.T0)
    sp = fx.spec("XP-2")
    store.create(sp, actor="local", owner_approval="XP-2")
    amended = sp.model_copy(update={"analysis": ADJ})
    with pytest.raises(sqlite3.IntegrityError, match="only the analysis"):
        conn.execute(
            """INSERT INTO experiments (experiment_id, revision, spec_version, area, kind,
                 spec, spec_hash, actor, created_at, amends_revision)
               VALUES ('XP-2', 2, ?, ?, ?, ?, ?, 'local', 'x', 1)""",
            (sp.spec_version, sp.area.value, sp.kind.value, canonical_json(amended),
             spec_hash(amended)),
        )  # fmt: skip


def test_verify_fails_on_a_tampered_amendment_and_start_is_refused(
    conn: sqlite3.Connection,
) -> None:
    """Tampering bypasses the append-only triggers by dropping them (as an attacker with
    the file would); verify must still catch it from the recomputed chain."""
    store = fx.start(conn, fx.spec("XP-2"))
    st = _amend(store)
    conn.execute("DROP TRIGGER experiments_no_update")
    tampered = st.spec.model_copy(update={"analysis": Analysis(covariates=[], hac_lag=7)})
    conn.execute("UPDATE experiments SET spec = ? WHERE revision = 2", (canonical_json(tampered),))
    v = store.verify("XP-2")
    assert not v["ok"] and v["problems"], v
    with pytest.raises(SpecLockedError):
        store._check_lock(store.require("XP-2"))  # noqa: SLF001 - the run-time guard


def test_verify_fails_on_a_forged_amendment_event(conn: sqlite3.Connection) -> None:
    store = fx.start(conn, fx.spec("XP-2"))
    _amend(store)
    conn.execute("DROP TRIGGER IF EXISTS experiment_events_no_update")
    row = conn.execute(
        "SELECT id, detail FROM experiment_events ORDER BY id DESC LIMIT 1"
    ).fetchone()
    d = json.loads(row["detail"]) | {"from_hash": "0" * 64}
    conn.execute("UPDATE experiment_events SET detail = ? WHERE id = ?", (json.dumps(d), row["id"]))
    v = store.verify("XP-2")
    assert not v["ok"] and any("does not follow" in p for p in v["problems"]), v


def test_unamended_verify_payload_unchanged(conn: sqlite3.Connection) -> None:
    store = fx.start(conn, fx.spec("XP-2"))
    v = store.verify("XP-2")
    assert v["ok"] and set(v) == {
        "experiment_id", "status", "ok", "revision", "registered_hash", "stored_hash",
        "recomputed_hash",
    }  # fmt: skip


# ---------------------------------------------------------------------------
# evaluator: unadjusted before, pre-registered adjusted after; report carries it
# ---------------------------------------------------------------------------


def _curves(conn: sqlite3.Connection) -> tuple[ExperimentStore, list[dt.date], list[float]]:
    rng = np.random.default_rng(17)
    n = 25
    x = list(rng.normal(0, 0.01, n))
    ctrl = list(rng.normal(20, 300, n))
    treat = [
        c + 400 + 0.5 * xi * fx.T0_EQUITY + e
        for c, xi, e in zip(ctrl, x, rng.normal(0, 100, n), strict=True)
    ]
    store = fx.start(conn, fx.spec("XP-2"))
    days = fx.equity_curves(conn, "XP-2", ctrl, treat)
    return store, days, x


def test_evaluator_unadjusted_before_amend_adjusted_after(conn: sqlite3.Connection) -> None:
    store, days, x = _curves(conn)
    spy = FakeSpy(_closes(days, x))
    pre = build_report(conn, store.require("XP-2"), CFG, now=_after(days), market=spy)
    assert pre.primary.adjustment is not None and not pre.primary.adjustment.pre_registered
    assert pre.analysis is None and pre.analysis_amended is None
    assert "analysis_amended" not in json.loads(pre.canonical_json())
    _amend(store, sessions_at_amend=pre.sessions)
    post = build_report(conn, store.require("XP-2"), CFG, now=_after(days), market=spy)
    lr = post.primary.adjustment
    assert lr is not None and lr.pre_registered and post.analysis == ADJ
    assert post.primary.mean == lr.estimate and post.primary.primary_unadjusted is not None
    am = post.analysis_amended
    assert am is not None and am.from_ is None and am.to == ADJ
    assert am.sessions_at_amend == pre.sessions
    payload = json.loads(post.canonical_json())
    assert payload["analysis_amended"] == {
        "at": payload["analysis_amended"]["at"],
        "sessions_at_amend": pre.sessions,
        "from": None,
        "to": {"covariates": ["spy_ret"], "hac_lag": 5},
    }
    # the payload reads back (reports stay reproducible)
    back = ExperimentReport.model_validate_json(post.canonical_json())
    assert back.analysis_amended == am


def test_amended_aa_stop_serves_the_adjusted_sigma(conn: sqlite3.Connection) -> None:
    """Later A/B sizing (``aa_sigma(analysis)``) reads XP-1's adjusted sigma after the
    amendment: the stop detail carries ``sigma_adj`` once the A/A is adjusted."""
    rng = np.random.default_rng(4)
    ctrl = list(rng.normal(0, 300, 10))
    treat = [c + e for c, e in zip(ctrl, rng.normal(0, 150, 10), strict=True)]
    store = fx.start(conn, fx.spec("XP-1", kind="aa"))
    days = fx.equity_curves(conn, "XP-1", ctrl, treat)
    fx.pnl_row(conn, previous_session(days[0]), fx.T0_EQUITY, None)
    _amend(store, "XP-1")
    from arc.experiments.evaluate import evaluate

    xs = list(rng.normal(0, 0.01, 10))
    rep = evaluate(store, "XP-1", CFG, now=_after(days), market=FakeSpy(_closes(days, xs)))
    assert rep.analysis_amended is not None and rep.primary.adjustment is not None
    assert rep.primary.adjustment.pre_registered
    assert store.aa_sigma(ADJ) == pytest.approx(rep.calibration.sigma_adj)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _cli(*argv: str) -> argparse.Namespace:
    p = argparse.ArgumentParser()
    add_experiment_parser(p.add_subparsers(dest="command"))
    return p.parse_args(["experiment", *argv])


def test_cli_amend_show_verify(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = str(tmp_path / "x.db")
    spec = str(LIVE / "xp1_aa_k3.yaml")
    assert (
        run_experiment(_cli("create", "--owner-approval", "XP-1", "--spec", spec, "--db", db)) == 0
    )
    # draft: refused (exit 2)
    amend = ("amend-analysis", "XP-1", "--covariates", "spy_ret", "--hac-lag", "5")
    reason = ("--reason", "#project-arc 1792193805.441269", "--db", db)
    assert run_experiment(_cli(*amend, "--actor", "local", *reason)) == 2
    assert "draft" in capsys.readouterr().err
    assert run_experiment(_cli("register", "--owner-approval", "XP-1", "XP-1", "--db", db)) == 0
    capsys.readouterr()
    # non-owner: refused
    assert run_experiment(_cli(*amend, "--actor", "U_NOBODY", *reason)) == 2
    assert "not the owner" in capsys.readouterr().err
    # bad analysis: refused
    bad = ("amend-analysis", "XP-1", "--covariates", "nope", "--hac-lag", "5")
    assert run_experiment(_cli(*bad, "--actor", "local", *reason)) == 2
    assert "invalid analysis" in capsys.readouterr().err
    assert run_experiment(_cli(*amend, "--actor", "local", *reason)) == 0
    out = capsys.readouterr().out
    assert "analysis amended" in out and "at session 0" in out
    assert "none (D44 iid unadjusted) -> {covariates: [spy_ret], hac_lag: 5}" in out
    assert "approval slack:1792193805.441269" in out
    assert run_experiment(_cli("show", "XP-1", "--db", db)) == 0
    assert "analysis amended" in capsys.readouterr().out
    assert run_experiment(_cli("verify", "XP-1", "--db", db)) == 0
    out = capsys.readouterr().out
    assert ": OK" in out and "(amended, D122)" in out and "r1 -> r2" in out
    assert run_experiment(_cli("show", "XP-1", "--db", db, "--json")) == 0
    out = capsys.readouterr().out
    shown = json.loads(out[out.index("{\n") :])
    assert shown["spec"]["analysis"] == {"covariates": ["spy_ret"], "hac_lag": 5}
    # a second identical amend: refused, nothing written
    assert run_experiment(_cli(*amend, "--actor", "local", *reason)) == 2
    assert "already" in capsys.readouterr().err


def test_cli_approval_from_reason() -> None:
    from arc.experiments.cli import _amend_approval

    assert _amend_approval("#project-arc 1792193805.441269", None) == "slack:1792193805.441269"
    assert _amend_approval("owner call", None) == "owner:owner call"
    assert _amend_approval("x", "slack:1.2") == "slack:1.2"
