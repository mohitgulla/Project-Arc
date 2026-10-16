"""E21.4 (D86): approve -> register (owner starts by hand, D112), and a verdict file per outcome.

``arc experiment adopt`` (ids across three namespaces, validation, idempotency, queueing,
the spec file and PR text), the owner's manual start command (D112), the verdict
files per outcome, and that both PR bodies pass the strategy-lane check (E21.1).
"""

from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
import yaml

from arc.experiments import adopt as ad
from arc.experiments.cli import add_experiment_parser, run_experiment
from arc.experiments.config import ExperimentsConfig, load_experiments_config
from arc.experiments.models import (
    ExperimentSpec,
    ExperimentStatus,
    RunningDetail,
    StopDetail,
    StopReason,
    spec_hash,
)
from arc.experiments.publish import EXPERIMENTS_DIR, VERDICTS_DIR, PublishResult
from arc.experiments.store import ExperimentStore, OwnerApprovalError, proposal_ref_of
from arc.experiments.verdicts import (
    publish_pending_verdicts,
    verdict_doc,
    verdict_file_text,
    verdict_path,
    verdict_pr_body,
)
from arc.routines.runs import RoutineStateRepo
from arc.store.db import connect
from arc.store.migrate import migrate
from arc.utils.calendar import ET

if TYPE_CHECKING:
    import sqlite3

REPO = Path(__file__).resolve().parent.parent
NOW = dt.datetime(2026, 10, 12, 20, 0, tzinfo=ET)  # Monday evening
APPROVAL = "slack:1791668051.305679"


def _load(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(name, mod)
    spec.loader.exec_module(mod)
    return mod


lane = _load("strategy_lane_check", REPO / "scripts" / "strategy_lane_check.py")
LANE_CFG = lane.load_lane_config(REPO / "config" / "strategy_lane.yaml")


# -- fixtures -----------------------------------------------------------------------------


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = connect(":memory:")
    migrate(c)
    return c


def _cfg() -> ExperimentsConfig:
    return load_experiments_config()


def _store(conn: sqlite3.Connection, cfg: ExperimentsConfig | None = None) -> ExperimentStore:
    clock = [NOW]

    def now() -> dt.datetime:
        clock[0] += dt.timedelta(seconds=1)
        return clock[0]

    return ExperimentStore.for_runner(conn, (cfg or _cfg()).runner, now=now)


def _treatments(k: int = 3) -> dict[str, Any]:
    return {
        f"t{i}": {
            "overlay": {
                "exits": {"default": {"take_profit_pct_of_max_gain": round(0.3 + i / 20, 2)}}
            }
        }
        for i in range(1, k + 1)
    }


def _proposal_spec(k: int = 3, area: str = "exits", **kw: Any) -> dict[str, Any]:
    return {
        "id": "XP-<next>",
        "kind": "ab",
        "spec_version": 2,
        "title": "Take-profit level x3",
        "hypothesis": "an earlier take profit lifts net P&L per session",
        "area": area,
        "arms": {"treatments": _treatments(k)},
        "backtest_ref": "BT-3",
        "non_inferiority_margin": 0.1,
        **kw,
    }


def _proposal(pid: str = "XP-1", **kw: Any) -> ad.Proposal:
    return ad.load_proposal(_proposal_spec(**kw), proposal_id=pid)


class FakeMain:
    """``origin/main``: ``{repo path: text}``."""

    def __init__(self, files: dict[str, str] | None = None) -> None:
        self.data = dict(files or {})

    def files(self, directory: str) -> dict[str, str]:
        d = directory.rstrip("/") + "/"
        return {p: t for p, t in self.data.items() if p.startswith(d) and "/" not in p[len(d) :]}


class FakePublisher:
    def __init__(self, fail: bool = False) -> None:
        self.calls: list[dict[str, Any]] = []
        self.fail = fail

    def publish(
        self, *, branch: str, files: dict[str, str], title: str, body: str
    ) -> PublishResult:
        from arc.experiments.publish import PublishError

        if self.fail:
            msg = "gh: network down"
            raise PublishError(msg)
        self.calls.append({"branch": branch, "files": dict(files), "title": title, "body": body})
        return PublishResult(branch, f"https://github.com/o/r/pull/{len(self.calls)}", "opened")


def _empty_repo(tmp_path: Path) -> Path:
    (tmp_path / EXPERIMENTS_DIR / "verdicts").mkdir(parents=True, exist_ok=True)
    return tmp_path


def _adopt(
    conn: sqlite3.Connection,
    tmp_path: Path,
    proposal: ad.Proposal | None = None,
    *,
    main: FakeMain | None = None,
    publisher: FakePublisher | None = None,
    cfg: ExperimentsConfig | None = None,
    **kw: Any,
) -> ad.AdoptResult:
    return ad.adopt(
        conn,
        proposal or _proposal(),
        cfg or _cfg(),
        owner_approval=APPROVAL,
        now=NOW,
        main=main if main is not None else FakeMain(),
        publisher=publisher,
        repo=_empty_repo(tmp_path),
        **kw,
    )


def _aa(store: ExperimentStore, eid: str = "XP-1", sigma: float | None = 0.35) -> None:
    sp = ExperimentSpec.model_validate(
        {
            "id": eid, "title": "A/A", "hypothesis": "noise", "area": "other", "kind": "aa",
            "proposed_by": "owner",
        }
    )  # fmt: skip
    from arc.experiments.overlay import fill_defaults

    sp = fill_defaults(sp, _cfg().defaults)
    store.create(sp, actor="local", owner_approval="owner:E15.9")
    store.register(eid, actor="local", owner_approval="owner:E15.9")
    store.start(eid, RunningDetail(t0=NOW, t0_equity=25_000.0, control_sha="abc1234"), actor="l")
    store.stop(eid, StopReason.FUTILITY, actor="local", detail=StopDetail(sigma=sigma))


def _merged(st_spec: ExperimentSpec, *, proposal: str = "XP-1") -> dict[str, str]:
    return {
        ad.spec_path(st_spec): ad.spec_file_text(st_spec, proposal_id=proposal, approval=APPROVAL)
    }


# -- adopt: ids ----------------------------------------------------------------------------


def test_next_free_id_spans_db_committed_and_main(conn: sqlite3.Connection, tmp_path: Path) -> None:
    store = _store(conn)
    _aa(store, "XP-2")  # DB: XP-2
    repo = _empty_repo(tmp_path)
    (repo / EXPERIMENTS_DIR / "xp5_old.yaml").write_text("id: XP-5\n")  # working tree
    (repo / VERDICTS_DIR / "XP-3.yaml").write_text("experiment_id: XP-3\n")
    main = FakeMain({f"{EXPERIMENTS_DIR}/xp7_new.yaml": "id: XP-7\n"})  # origin/main only
    res = ad.adopt(
        conn, _proposal(), _cfg(), owner_approval=APPROVAL, now=NOW, main=main, repo=repo
    )
    assert res.experiment_id == "XP-8"
    # each namespace alone is honoured
    assert ad.next_free_id(_store(conn)) == "XP-9"  # the DB now holds XP-8
    assert ad.next_free_id(store, ["xp12_a.yaml"]) == "XP-13"
    assert ad.next_free_id(store, ["XP-20"]) == "XP-21"


def test_comments_citing_other_ids_do_not_burn_numbers(tmp_path: Path) -> None:
    repo = _empty_repo(tmp_path)
    (repo / EXPERIMENTS_DIR / "xp1_aa.yaml").write_text("# replaces XP-40 from history\nid: XP-1\n")
    texts = ad.committed_texts(repo)
    assert "XP-40" not in " ".join(texts)
    assert ad._xp_numbers(texts) == {1}


# -- adopt: validation -------------------------------------------------------------------


def test_refuses_a_missing_or_bad_owner_approval(conn: sqlite3.Connection, tmp_path: Path) -> None:
    for bad in ("", "  ", "yes"):
        with pytest.raises(OwnerApprovalError):
            ad.adopt(conn, _proposal(), _cfg(), owner_approval=bad, now=NOW, repo=tmp_path)
    assert _store(conn).all() == []


@pytest.mark.parametrize("k", [1, 2, 4])
def test_refuses_other_than_three_treatments(
    conn: sqlite3.Connection, tmp_path: Path, k: int
) -> None:
    """D108: K = 3 (was 4 under D86); a K=4 or K=2 proposal is refused, not trimmed."""
    with pytest.raises(ad.AdoptError, match="exactly 3 treatments \\(THREE-TREATMENTS\\)"):
        _adopt(conn, tmp_path, _proposal(k=k))
    assert _store(conn).all() == []


@pytest.mark.parametrize("version", [2, 3])
def test_accepts_three_treatments_and_writes_spec_v3(
    conn: sqlite3.Connection, tmp_path: Path, version: int
) -> None:
    """D108: a K=3 proposal (v2 or v3 shape) is registered and committed as spec v3 with
    ``arms.control: {overlay: {}}``: control runs as its own flat arm, so it counts too."""
    pub = FakePublisher()
    res = _adopt(conn, tmp_path, _proposal(spec_version=version), publisher=pub)
    st = _store(conn).require(res.experiment_id)
    assert st.spec.spec_version == ad.ADOPT_SPEC_VERSION == 3 and st.spec.control_runs
    assert sorted(st.spec.arms.treatments) == ["t1", "t2", "t3"]
    assert st.spec.arm_id_for("control") == f"{res.experiment_id}:control"
    assert res.arms == 4  # control + 3 treatments on the experiment account
    doc = yaml.safe_load(pub.calls[0]["files"][res.path])
    assert doc["spec_version"] == 3
    assert doc["arms"]["control"] == {"overlay": {}}
    assert spec_hash(ExperimentSpec.model_validate(doc)) == st.registered_hash
    assert "control + 3 treatments (spec v3, PLAN D108)" in pub.calls[0]["body"]


def test_refuses_a_control_overlay(conn: sqlite3.Connection, tmp_path: Path) -> None:
    arms = {"control": {"overlay": {"exits": {"default": {"take_profit_pct": 0.4}}}}}
    arms["treatments"] = _treatments()
    with pytest.raises(ad.AdoptError, match="control arm runs production's config"):
        _adopt(conn, tmp_path, _proposal(arms=arms))


@pytest.mark.parametrize(
    ("patch", "match"),
    [
        (
            {"kind": "aa", "arms": {"treatments": {f"t{i}": {"overlay": {}} for i in range(1, 4)}}},
            "ab experiments only",
        ),
        ({"area": "nope"}, "invalid spec"),
        ({"spec_version": 1}, "writes it as v3; got spec_version 1"),
        (
            {"arms": {"treatments": {**_treatments(2), "t3": {"overlay": {"bogus": {"x": 1}}}}}},
            "invalid spec",
        ),
    ],
)
def test_refuses_bad_specs(
    conn: sqlite3.Connection, tmp_path: Path, patch: dict[str, Any], match: str
) -> None:
    with pytest.raises(ad.AdoptError, match=match):
        _adopt(conn, tmp_path, ad.load_proposal({**_proposal_spec(), **patch}, proposal_id="XP-1"))


def test_refuses_more_arms_than_the_cap(conn: sqlite3.Connection, tmp_path: Path) -> None:
    cfg = load_experiments_config()
    cfg = cfg.model_copy(
        update={"runner": cfg.runner.model_copy(update={"max_parallel_arms": 3})}, deep=True
    )
    with pytest.raises(ad.AdoptError, match="max_parallel_arms 3"):
        _adopt(conn, tmp_path, cfg=cfg)


def test_load_proposal_shapes() -> None:
    entry = {"id": "XP-4", "status": "approved", "finding_id": "A-9", "spec": _proposal_spec()}
    p = ad.load_proposal(entry)
    assert p.proposal_id == "XP-4" and p.proposed_by == "A-9" and "id" not in p.spec
    with pytest.raises(ad.AdoptError, match="only an owner-approved"):
        ad.load_proposal({**entry, "status": "proposed"})
    with pytest.raises(ad.AdoptError, match="not XP-5"):
        ad.load_proposal(entry, proposal_id="XP-5")
    with pytest.raises(ad.AdoptError, match="must look like XP-<n>"):
        ad.load_proposal(_proposal_spec())
    # D121: the spec may carry its own reserved id, never another one
    assert ad.load_proposal(_proposal_spec(id="XP-4"), proposal_id="XP-4").proposal_id == "XP-4"
    with pytest.raises(ad.AdoptError, match="its own reserved id"):
        ad.load_proposal(_proposal_spec(id="XP-5"), proposal_id="XP-4")
    # D121: P-<n> is retired for adoption; the reply points at the backlog's XP id
    with pytest.raises(ad.AdoptError, match="P-<n> ids are retired"):
        ad.load_proposal(_proposal_spec(), proposal_id="P-4")
    with pytest.raises(ad.AdoptError, match="P-<n> ids are retired"):
        ad.load_proposal({**entry, "id": "P-4"})


def test_adopt_registers_under_the_reserved_id(conn: sqlite3.Connection, tmp_path: Path) -> None:
    """D121: ``--id XP-<n>`` registers the proposal under its own reserved number (a gap
    below it is fine) and re-runs stay on it."""
    res = _adopt(conn, tmp_path, _proposal("XP-7"), experiment_id="XP-7")
    assert res.experiment_id == "XP-7" and res.created
    st = _store(conn).require("XP-7")
    assert proposal_ref_of(st) == "XP-7"
    again = _adopt(conn, tmp_path, _proposal("XP-7"), experiment_id="XP-7")
    assert again.experiment_id == "XP-7" and not again.created
    with pytest.raises(ad.AdoptError, match="already adopted as XP-7, not --id XP-8"):
        _adopt(conn, tmp_path, _proposal("XP-7"), experiment_id="XP-8")


def test_adopt_refuses_a_taken_reserved_id(conn: sqlite3.Connection, tmp_path: Path) -> None:
    """D121: a reserved id already in the DB or the committed specs is refused, never reused."""
    _adopt(conn, tmp_path, _proposal("XP-1"), experiment_id="XP-1")
    with pytest.raises(ad.AdoptError, match="XP-1 is taken in the registry .*XP-2 must re-reserve"):
        _adopt(conn, tmp_path, _proposal("XP-2", area="ranking"), experiment_id="XP-1")
    (_empty_repo(tmp_path) / EXPERIMENTS_DIR / "xp5_owner_made.yaml").write_text("id: XP-5\n")
    with pytest.raises(ad.AdoptError, match="XP-5 is taken by a committed experiment spec"):
        _adopt(conn, tmp_path, _proposal("XP-5", area="ranking"), experiment_id="XP-5")
    with pytest.raises(ad.AdoptError, match="must look like XP-<n>"):
        _adopt(conn, tmp_path, _proposal("XP-6", area="ranking"), experiment_id="P-6")
    assert {s.experiment_id for s in _store(conn).all()} == {"XP-1"}


# -- adopt: register / queue / idempotency ------------------------------------------------


def test_adopt_registers_with_proposal_ref_and_publishes(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    pub = FakePublisher()
    posted: list[str] = []
    res = _adopt(conn, tmp_path, publisher=pub, post=posted.append)
    st = _store(conn).require(res.experiment_id)
    assert res.experiment_id == "XP-1" and res.status == "registered" and res.created
    assert st.status is ExperimentStatus.REGISTERED and proposal_ref_of(st) == "XP-1"
    assert all(e.detail.get("proposal_ref") == "XP-1" for e in st.events)
    assert res.start_cmd == "arc experiment start XP-1"  # D112: the owner starts it
    assert posted == [
        "XP-1 registered, control + 3 treatments, area exits, "
        "owner to start after the spec PR merges: `arc experiment start XP-1`"
    ]
    (call,) = pub.calls
    assert call["branch"] == "xp/1"
    assert list(call["files"]) == ["config/experiments/live/xp1_take_profit_level_x3.yaml"]
    assert call["body"].startswith("Lane: fast — register XP-1 (owner approved XP-1, PLAN D86)\n")
    # the committed file re-parses to the registered lock
    doc = yaml.safe_load(call["files"][res.path])
    assert spec_hash(ExperimentSpec.model_validate(doc)) == st.registered_hash
    assert doc["proposed_by"] == "owner" and doc["min_sessions"] > 0  # defaults filled
    assert RoutineStateRepo(conn).get("xp_spec_pr:XP-1") == "https://github.com/o/r/pull/1"


def test_adopt_is_idempotent_per_proposal(conn: sqlite3.Connection, tmp_path: Path) -> None:
    pub = FakePublisher()
    posted: list[str] = []
    first = _adopt(conn, tmp_path, publisher=pub, post=posted.append)
    again = _adopt(conn, tmp_path, publisher=pub, post=posted.append)
    assert again.experiment_id == first.experiment_id and not again.created
    assert len(_store(conn).all()) == 1 and len(posted) == 1
    assert pub.calls[0]["files"] == pub.calls[1]["files"]  # the PR is found, not duplicated
    other = _adopt(conn, tmp_path, _proposal("XP-2", area="ranking"))
    assert other.experiment_id == "XP-2"


def test_a_failed_publish_keeps_the_registration_and_retries(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    from arc.experiments.publish import PublishError

    with pytest.raises(PublishError):
        _adopt(conn, tmp_path, publisher=FakePublisher(fail=True))
    assert _store(conn).require("XP-1").status is ExperimentStatus.REGISTERED
    res = _adopt(conn, tmp_path, publisher=FakePublisher())
    assert res.experiment_id == "XP-1" and res.pr_url and not res.created


def test_a_concluded_adoption_is_not_re_run(conn: sqlite3.Connection, tmp_path: Path) -> None:
    _adopt(conn, tmp_path)
    store = _store(conn)
    store.stop("XP-1", StopReason.OWNER, actor="local")
    with pytest.raises(ad.AdoptError, match="already adopted as XP-1"):
        _adopt(conn, tmp_path)


def test_adopt_queues_behind_a_busy_area_and_promotes_after_stop(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    posted: list[str] = []
    _adopt(conn, tmp_path)
    second = _adopt(conn, tmp_path, _proposal("XP-2"), post=posted.append)
    assert second.status == "queued" and second.queued_behind == ["XP-1"]
    assert second.queue_reason == "area exits held by XP-1" and second.start_cmd is None
    assert posted == ["XP-2 registered, control + 3 treatments, area exits, queued behind XP-1"]
    store = _store(conn)
    store.stop("XP-1", StopReason.FUTILITY, actor="arc.experiments")
    assert store.require("XP-2").status is ExperimentStatus.REGISTERED


def test_adopt_queues_on_the_arm_cap(conn: sqlite3.Connection, tmp_path: Path) -> None:
    _adopt(conn, tmp_path)
    _adopt(conn, tmp_path, _proposal("XP-2", area="ranking"))  # 8 of 10 arms (control + 3 each)
    third = _adopt(conn, tmp_path, _proposal("XP-3", area="entries"))
    assert third.status == "queued" and "max_parallel_arms" in (third.queue_reason or "")


def test_dry_run_writes_nothing(conn: sqlite3.Connection, tmp_path: Path) -> None:
    pub = FakePublisher()
    posted: list[str] = []
    res = _adopt(conn, tmp_path, publisher=pub, post=posted.append, dry_run=True)
    assert res.dry_run and res.experiment_id == "XP-1" and res.status == "registered"
    assert _store(conn).all() == [] and pub.calls == [] and posted == []
    assert res.file_text.startswith("# XP-1: adopted from Arc Analyst proposal XP-1")


def test_no_autostart_routine_or_knobs() -> None:
    """D112: no pre-open auto-start; a registered XP waits for the owner's manual start."""
    from arc.control.registry import REGISTRY
    from arc.routines.config import load_routines
    from arc.routines.handlers import DRY_RUN_PREVIEWS

    assert "experiments.autostart" not in load_routines().jobs()
    assert "experiments.autostart" not in DRY_RUN_PREVIEWS
    assert not any(k.startswith("experiments.autostart") for k in REGISTRY)
    assert not hasattr(load_experiments_config(), "autostart")
    assert importlib.util.find_spec("arc.experiments.autostart") is None


# -- the PR bodies pass the strategy-lane check (E21.1) -------------------------------------


def _lane(files: dict[str, str], body: str, base_main: dict[str, str] | None = None) -> Any:
    """Run the lane check on a PR that adds *files* (no config deltas)."""
    experiments: dict[str, Any] = {}
    return lane.evaluate(list(files), body, {}, experiments, LANE_CFG)


def test_adopt_pr_body_passes_the_lane_check(conn: sqlite3.Connection, tmp_path: Path) -> None:
    pub = FakePublisher()
    _adopt(conn, tmp_path, publisher=pub)
    call = pub.calls[0]
    r = _lane(call["files"], call["body"])
    assert r.ok, r.errors
    body = lane.parse_body(call["body"])
    assert body.advisory is not None


def test_verdict_pr_body_passes_the_lane_check(conn: sqlite3.Connection, tmp_path: Path) -> None:
    _adopt(conn, tmp_path)
    store = _store(conn)
    store.stop("XP-1", StopReason.HARM, actor="arc.experiments")
    st = store.require("XP-1")
    doc = verdict_doc(st, None)
    files = {verdict_path("XP-1"): verdict_file_text(st, None)}
    r = _lane(files, verdict_pr_body(st, doc))
    assert r.ok, r.errors
    xid, v = lane.parse_verdict(yaml.safe_load(files[verdict_path("XP-1")]), "verdict")
    assert xid == "XP-1" and v.verdict == "loss"


# -- verdict files -------------------------------------------------------------------------


def _report(xid: str, headline: str, wins: list[str]) -> Any:
    """The fields of an E10.3 report the verdict file reads (headline arm, control pairs)."""
    from types import SimpleNamespace

    pairs = [
        SimpleNamespace(kind="control_vs_treatment", b=t, verdict="win" if t in wins else None)
        for t in ("t1", "t2", "t3")
    ]
    digest = f"{xid}-{headline}".encode().hex().ljust(64, "0")[:64]
    return SimpleNamespace(headline_arm=headline, pairwise=pairs, report_hash=lambda: digest)


@pytest.mark.parametrize(
    ("reason", "verdict"),
    [
        (StopReason.WIN, "win"),
        (StopReason.HARM, "loss"),
        (StopReason.FUTILITY, "futility"),
        (StopReason.OWNER, "stopped"),
        (StopReason.INVALID, "invalid"),
    ],
)
def test_verdict_file_per_outcome(
    conn: sqlite3.Connection, tmp_path: Path, reason: StopReason, verdict: str
) -> None:
    _adopt(conn, tmp_path)
    store = _store(conn)
    store.stop("XP-1", reason, actor="arc.experiments", detail=StopDetail(sessions=25))
    st = store.require("XP-1")
    report = _report("XP-1", "t2", ["t2", "t3"]) if reason is StopReason.WIN else None
    text = verdict_file_text(st, report)
    doc = yaml.safe_load(text)
    assert doc["experiment_id"] == "XP-1" and doc["verdict"] == verdict
    assert doc["report_hash"] == (report.report_hash() if report else None)
    assert doc["spec_hash"] == st.registered_hash and doc["sessions"] == 25
    if verdict == "win":
        assert doc["winner"] == "t2"
        assert "may set arm t2's values" in text
    else:
        assert "winner" not in doc


def test_winner_falls_back_to_the_first_winning_pair(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    from arc.experiments.verdicts import winner_of

    _adopt(conn, tmp_path)
    st = _store(conn).require("XP-1")
    assert winner_of(_report("XP-1", "t1", ["t3"]), st) == "t3"
    assert winner_of(None, st) == "t1"


def test_pending_verdicts_publish_once_and_skip_main(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    _adopt(conn, tmp_path)
    _adopt(conn, tmp_path, _proposal("XP-2", area="ranking"))
    store = _store(conn)
    store.stop("XP-1", StopReason.FUTILITY, actor="arc.experiments")
    store.stop("XP-2", StopReason.OWNER, actor="local")
    main = FakeMain({verdict_path("XP-2"): "experiment_id: XP-2\n"})  # hand-committed
    pub = FakePublisher()
    lines, errors = publish_pending_verdicts(conn, store, pub, now=NOW, main=main)
    assert errors == [] and lines == ["XP-1 verdict PR https://github.com/o/r/pull/1"]
    assert [c["branch"] for c in pub.calls] == ["xp/1-verdict"]
    assert pub.calls[0]["title"] == "XP-1: verdict futility (D86)"
    lines, _ = publish_pending_verdicts(conn, store, pub, now=NOW, main=main)
    assert lines == [] and len(pub.calls) == 1
    lines, errors = publish_pending_verdicts(
        conn, store, FakePublisher(fail=True), now=NOW, main=main
    )
    assert lines == errors == []  # both already recorded


def test_failed_verdict_publish_is_reported_and_retried(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    _adopt(conn, tmp_path)
    store = _store(conn)
    store.stop("XP-1", StopReason.FUTILITY, actor="arc.experiments")
    lines, errors = publish_pending_verdicts(conn, store, FakePublisher(fail=True), now=NOW)
    assert lines == [] and errors == ["XP-1 verdict PR failed: gh: network down"]
    lines, errors = publish_pending_verdicts(conn, store, FakePublisher(), now=NOW)
    assert errors == [] and len(lines) == 1


def _cli(*argv: str) -> argparse.Namespace:
    p = argparse.ArgumentParser()
    add_experiment_parser(p.add_subparsers(dest="cmd"))
    return p.parse_args(["experiment", *argv])


def test_cli_adopt_dry_run_and_real(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "arc.db"
    c = connect(str(db))
    migrate(c)
    c.close()
    prop = tmp_path / "XP-7.yaml"
    prop.write_text(json.dumps(_proposal_spec()))
    common = ("--proposal", str(prop), "--db", str(db), "--no-fetch", "--now", NOW.isoformat())
    rc = run_experiment(_cli("adopt", *common, "--owner-approval", APPROVAL, "--dry-run"))
    out = capsys.readouterr().out
    assert rc == 0 and "dry-run (nothing written): XP-7 -> XP-" in out
    assert "Lane: fast — register XP-" in out and "spec file:" in out
    assert run_experiment(_cli("adopt", *common, "--dry-run")) == 2  # no approval
    assert "owner" in capsys.readouterr().err
    rc = run_experiment(
        _cli("adopt", *common, "--owner-approval", APPROVAL, "--no-pr", "--no-slack", "--json")
    )
    out = capsys.readouterr().out
    res, _ = json.JSONDecoder().raw_decode(out, out.index('{\n  "experiment_id"'))
    assert rc == 0 and res["status"] == "registered" and res["proposal_id"] == "XP-7"
    store = ExperimentStore(connect(str(db)))
    assert proposal_ref_of(store.require(res["experiment_id"])) == "XP-7"
    # D121 --id: the reserved id is used as is, and a taken one is refused
    prop8 = tmp_path / "XP-8.yaml"
    prop8.write_text(json.dumps(_proposal_spec(area="ranking")))
    common8 = ("--proposal", str(prop8), "--db", str(db), "--no-fetch", "--now", NOW.isoformat())
    taken = res["experiment_id"]
    assert run_experiment(_cli("adopt", "--id", taken, *common8, "--owner-approval", APPROVAL)) == 2
    assert f"{taken} is taken in the registry" in capsys.readouterr().err
    assert run_experiment(_cli("adopt", "--id", "XP-8", *common8, "--owner-approval", APPROVAL,
                               "--no-pr", "--no-slack")) == 0  # fmt: skip
    assert "adopted: XP-8 [registered]" in capsys.readouterr().out
    assert proposal_ref_of(ExperimentStore(connect(str(db))).require("XP-8")) == "XP-8"


def test_cli_adopt_from_a_backlog_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = tmp_path / "arc.db"
    c = connect(str(db))
    migrate(c)
    c.close()
    backlog = {
        "next_id": 3,
        "items": {
            "xp-proposal:a": {"id": "XP-2", "status": "approved", "spec": _proposal_spec()},
        },
    }
    f = tmp_path / "xp_backlog.json"
    f.write_text(json.dumps(backlog))
    args = ("--proposal", str(f), "--db", str(db), "--no-fetch", "--owner-approval", APPROVAL)
    assert run_experiment(_cli("adopt", *args, "--dry-run")) == 2  # which XP-<n>?
    assert "name the proposal" in capsys.readouterr().err
    assert run_experiment(_cli("adopt", "XP-2", *args, "--dry-run")) == 0
    # no --id: the next free id (the repo's committed xp1 spec takes XP-1); the backlog's
    # XP-2 is printed once when the ids match (D121), "XP-2 -> XP-<m>" otherwise
    assert re.search(r"dry-run \(nothing written\): XP-2( -> XP-\d+)? \[", capsys.readouterr().out)
