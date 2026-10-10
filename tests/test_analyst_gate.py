"""E9.2: the Arc Analyst pre-run gate, ledger, validators and install script.

The gate (``hermes/analyst/arc_analyst.py``) is a stdlib script the ``arc-analyst``
Hermes profile runs before each weekly review. These tests drive it against fixture
stores; ``now`` is always injected.
"""

from __future__ import annotations

import datetime as _dt
import importlib.util
import json
import re
import sqlite3
import subprocess
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import yaml

from arc.config import ArcSettings
from arc.journal.models import OutcomeRecord, OutcomeStatus
from arc.journal.store import JournalStore
from arc.pipeline.runner import fixture_run
from arc.routines.config import load_routines
from arc.store.repos import HaltRepo
from arc.utils.calendar import ET

REPO = Path(__file__).resolve().parents[1]
ANALYST = REPO / "hermes" / "analyst"
NOW = _dt.datetime(2026, 10, 4, 15, tzinfo=ET)  # a Sunday, 15:00 ET
ARC_ARGV = (sys.executable, "-c", "import sys; from arc.cli import main; sys.exit(main())")


def _load() -> Any:
    spec = importlib.util.spec_from_file_location("arc_analyst_e92", ANALYST / "arc_analyst.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


aa = _load()


@pytest.fixture(scope="module")
def _pipeline_db() -> bytes:
    conn, report = fixture_run(
        ArcSettings(_env_file=None, account_profile="margin"),  # type: ignore[call-arg]
        load_routines(),
    )
    assert len(report.proposals) == 1
    return conn.serialize()


@pytest.fixture
def _stub_views(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Replace the gate's ``arc`` subprocess views with an instant canned output.

    Each real view is a fresh ``arc`` process (~1 s of imports), ~10 per gate run.
    Tests that assert only the gate's own logic (wake/skip, cap, experiments section)
    use this; ``test_context_contains_scorecard_and_ledger`` and
    ``test_never_touches_live_db`` keep the real subprocess path end to end.
    """
    calls: list[list[str]] = []

    def fake(p: Any, args: list[str], run_dir: Path, name: str, timeout: int = 180) -> str:
        calls.append(list(args))
        out = f"[stub arc view {name}] " + " ".join(args) + "\n" + "x" * 400
        (run_dir / f"{name}.log").write_text(out)
        return out

    monkeypatch.setattr(aa, "run_arc", fake)
    return calls


def _conn(blob: bytes) -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.deserialize(blob)
    return c


def _close_one(conn: sqlite3.Connection, at: _dt.datetime) -> str:
    ph = conn.execute("SELECT proposal_hash FROM proposals WHERE kind = 'open'").fetchone()[0]
    JournalStore(conn).record_outcome(
        OutcomeRecord(
            proposal_hash=ph,
            status=OutcomeStatus.CLOSED,
            contracts=2,
            limit_price=Decimal("1.00"),
            entry_fill=Decimal("1.01"),
            slippage_usd=Decimal("2"),
            slippage_bps=10.0,
            cost_bps=25.0,
            exit_fill=Decimal("1.50"),
            realised_pnl=Decimal("98"),
            ev_total=Decimal("40"),
            pnl_vs_ev=Decimal("58"),
            days_held=3,
            exit_reason="take_profit",
            at=at,
        )
    )
    conn.commit()
    return ph


def _to_disk(conn: sqlite3.Connection, path: Path) -> Path:
    disk = sqlite3.connect(path)
    conn.backup(disk)
    disk.close()
    return path


def _paths(tmp_path: Path, db: Path) -> Any:
    return aa.Paths(home=tmp_path / "analyst", repo=REPO, live_db=db, arc=ARC_ARGV)


def _last_json(out: str) -> dict[str, Any]:
    return json.loads(out.strip().splitlines()[-1])


def _run_dir(p: Any) -> Path:
    (d,) = sorted(p.runs.iterdir())
    return d


# ---------------------------------------------------------------------------
# gate: skip / wake
# ---------------------------------------------------------------------------


def test_skip_when_no_new_outcomes(
    _pipeline_db: bytes, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = _to_disk(_conn(_pipeline_db), tmp_path / "live.db")
    p = _paths(tmp_path, db)
    assert aa.main([], p, NOW) == 0
    assert _last_json(capsys.readouterr().out) == {"wakeAgent": False}
    assert not p.runs.exists()  # no run dir, no copy left behind
    assert not (p.home / "staging-copy.db").exists()


@pytest.mark.usefixtures("_stub_views")
def test_skip_when_nothing_changed_since_last_run(
    _pipeline_db: bytes, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    conn = _conn(_pipeline_db)
    _close_one(conn, NOW - _dt.timedelta(days=2))
    db = _to_disk(conn, tmp_path / "live.db")
    p = _paths(tmp_path, db)
    assert aa.main([], p, NOW) == 0
    run_dir = _run_dir(p)
    capsys.readouterr()
    # the agent recorded and marked this run
    _write_valid_findings(run_dir)
    assert aa.main(["record", str(run_dir)], p, NOW) == 0
    assert aa.main(["mark", str(run_dir)], p, NOW) == 0
    capsys.readouterr()
    # a week later nothing new closed and no halt: silent
    assert aa.main([], p, NOW + _dt.timedelta(days=7)) == 0
    out = capsys.readouterr().out
    assert _last_json(out) == {"wakeAgent": False}
    assert "no new closed outcome, no halt and no experiment status change" in out


@pytest.mark.usefixtures("_stub_views")
def test_halt_wakes_without_new_outcome(
    _pipeline_db: bytes, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    conn = _conn(_pipeline_db)
    _close_one(conn, NOW - _dt.timedelta(days=9))
    db = _to_disk(conn, tmp_path / "live.db")
    p = _paths(tmp_path, db)
    aa.save_json(p.state, {"closed_watermark": 10**9, "last_run_at": aa.iso(NOW)})
    later = NOW + _dt.timedelta(days=7)
    live = sqlite3.connect(db)
    HaltRepo(live).halt(
        reason="daily loss 3.2% >= 3%", actor="system", kind="daily_loss",
        session_date="2026-10-08", at=aa.to_db(later - _dt.timedelta(days=3)),
    )  # fmt: skip
    live.close()
    assert aa.main([], p, later) == 0
    out = capsys.readouterr().out
    assert "wakeAgent" not in out
    assert "1 halt(s) since the last run (daily_loss)" in out
    assert "daily loss 3.2% >= 3%" in out


def test_context_contains_scorecard_and_ledger(
    _pipeline_db: bytes, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    conn = _conn(_pipeline_db)
    ph = _close_one(conn, NOW - _dt.timedelta(days=2))
    db = _to_disk(conn, tmp_path / "live.db")
    p = _paths(tmp_path, db)
    aa.save_json(
        p.ledger,
        {"next_id": 2, "items": {"flaw:auto-approve-negative-ev": {
            "id": "A-1", "key": "flaw:auto-approve-negative-ev", "status": "open",
            "severity": "high", "title": "auto-approve on with negative realised EV", "n": 4}}},
    )  # fmt: skip
    assert aa.main([], p, NOW) == 0
    out = capsys.readouterr().out
    assert "wakeAgent" not in out
    assert len(out) <= aa.MAX_STDOUT
    run_dir = _run_dir(p)
    assert out.startswith(f"RUN_DIR={run_dir}")
    assert "CLOSED_TRADES: week=1 all_time=1" in out
    # realised-vs-model table by kind x regime
    assert "## Realised vs model, this week (kind x regime)" in out
    assert re.search(r"\| iron_condor \| risk_on \| 1 \| 98\.00 \| 40\.00 \| \+58\.00 \|", out)
    assert "LOW (n<30)" in out
    assert "slippage_frac = 0.25" in out
    # scorecard (attribution + weekly), gaps, counterfactual, config
    assert "attribution by kind, regime since" in out
    assert "closed trades: 1 · low_sample below n=30" in out
    assert "# Paper scorecard: week of" in out
    assert "decision journal gaps since" in out
    assert "counterfactual since" in out
    assert "[Control] Diff" in out
    assert "[Control] History" in out
    # journal show for the closed trade
    assert f"### {ph[:12]}" in out
    assert (run_dir / f"show-{ph[:12]}.log").read_text().strip()
    # themes and ledger
    for theme in aa.THEMES:
        assert f"- {theme}:" in out
    assert "- A-1 [open] key=flaw:auto-approve-negative-ev sev=high n=4" in out
    # artefacts
    assert (run_dir / "arc-copy.db").is_file()
    assert (run_dir / "config" / "costs.yaml").is_file()
    assert (run_dir / "context.md").read_text() == out.rstrip("\n")
    assert json.loads((run_dir / "metrics.json").read_text())["closed_total"] == 1


@pytest.mark.usefixtures("_stub_views")
def test_context_is_capped(
    _pipeline_db: bytes, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip
    conn = _conn(_pipeline_db)
    _close_one(conn, NOW - _dt.timedelta(days=2))
    p = _paths(tmp_path, _to_disk(conn, tmp_path / "live.db"))
    monkeypatch.setattr(aa, "MAX_STDOUT", 2_000)
    assert aa.main([], p, NOW) == 0
    out = capsys.readouterr().out
    assert len(out.rstrip("\n")) <= 2_000
    assert "[truncated; full:" in out
    assert len((_run_dir(p) / "context.md").read_text()) > 2_000


def test_never_touches_live_db(
    _pipeline_db: bytes, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip
    conn = _conn(_pipeline_db)
    _close_one(conn, NOW - _dt.timedelta(days=2))
    live_dir = tmp_path / "live"
    live_dir.mkdir()
    db = _to_disk(conn, live_dir / "arc.db")
    wal = sqlite3.connect(db)  # the real store runs in WAL mode (arc.store.db.connect)
    assert wal.execute("PRAGMA journal_mode = WAL").fetchone()[0] == "wal"
    wal.close()
    before = db.read_bytes()
    db.chmod(0o444)  # any write attempt would fail loudly

    opened: list[str] = []
    real_connect = sqlite3.connect

    class _Spy:
        """Wraps the live-DB connection: only backup() and close() are allowed."""

        def __init__(self, inner: sqlite3.Connection) -> None:
            self._inner = inner

        def backup(self, target: sqlite3.Connection) -> None:
            opened.append("backup")
            self._inner.backup(target)

        def close(self) -> None:
            self._inner.close()

        def __getattr__(self, name: str) -> Any:
            raise AssertionError(f"live DB used for {name!r}; only backup() is allowed")

    def spy_connect(target: Any, *a: Any, **k: Any) -> Any:
        text = str(target)
        c = real_connect(target, *a, **k)
        if str(db.resolve()) in text or text == str(db):
            opened.append(text)
            assert text.startswith("file:") and text.endswith("?mode=ro"), text
            assert k.get("uri") is True
            return _Spy(c)
        return c

    monkeypatch.setattr(aa.sqlite3, "connect", spy_connect)
    argvs: list[list[str]] = []
    real_run = subprocess.run

    def spy_run(cmd: list[str], *a: Any, **k: Any) -> Any:
        argvs.append(list(cmd))
        return real_run(cmd, *a, **k)

    monkeypatch.setattr(aa.subprocess, "run", spy_run)

    p = _paths(tmp_path, db)
    assert aa.main([], p, NOW) == 0
    capsys.readouterr()

    assert opened == [f"{db.resolve().as_uri()}?mode=ro", "backup"]
    assert db.read_bytes() == before
    # a WAL reader may leave the (empty) -wal/-shm index; never a rollback journal or WAL data
    assert "arc.db-journal" not in {x.name for x in live_dir.iterdir()}
    wal_file = live_dir / "arc.db-wal"
    assert not wal_file.exists() or wal_file.stat().st_size == 0
    # the private copy leaves no sidecars behind
    run_dir = _run_dir(p)
    assert sorted(x.name for x in run_dir.glob("arc-copy.db*")) == ["arc-copy.db"]
    assert sorted(x.name for x in p.home.glob("staging-copy*")) == []
    copy = str(run_dir / "arc-copy.db")
    assert argvs, "the gate ran no arc views"
    for argv in argvs:
        assert str(db) not in " ".join(argv)
        assert argv[argv.index("--db") + 1] == copy
    # no secret reaches the arc views
    monkeypatch.setenv("ALPACA_API_KEY", "k")
    monkeypatch.setenv("ARC_GATE_SECRET", "s")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "t")
    env = aa.clean_env()
    assert not {"ALPACA_API_KEY", "ARC_GATE_SECRET", "SLACK_BOT_TOKEN"} & set(env)
    assert env["ARC_ENV"] == "paper"


def test_missing_live_db_skips_without_creating(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "data" / "arc.db"
    assert aa.main([], _paths(tmp_path, db), NOW) == 0
    assert _last_json(capsys.readouterr().out) == {"wakeAgent": False}
    assert not db.exists()


# ---------------------------------------------------------------------------
# ledger: record / mark / triage / reset
# ---------------------------------------------------------------------------


def _themes(**over: str) -> list[dict[str, str]]:
    return [
        {"theme": t, "status": over.get(t, "no-evidence"), "note": "n=1 closed trade"}
        for t in aa.THEMES
    ]


def _write_valid_findings(run_dir: Path, findings: list[dict[str, Any]] | None = None) -> None:
    doc = {"verdict": "findings" if findings else "quiet", "themes": _themes(),
           "findings": findings or [], "resolved": [], "experiments": [],
           "next_experiment": "none: no bucket has n >= 30"}  # fmt: skip
    (run_dir / "findings.json").write_text(json.dumps(doc))


def _flaw(key: str = "flaw:auto-approve-negative-ev", n: int = 4) -> dict[str, Any]:
    return {
        "key": key, "title": "auto-approve on while realised EV < 0", "severity": "high",
        "category": "flaw", "bucket": "all", "n": n, "theme": "auto-approve",
        "evidence": "auto_approve.paper = on; realised net -$120 over n=4",
        "recommendation": "turn auto_approve.paper off until the scorecard gate passes",
        "action": "owner-decision",
    }  # fmt: skip


def test_triage_updates_ledger(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    p = _paths(tmp_path, tmp_path / "unused.db")
    run_dir = p.runs / "2026-10-04-1500"
    run_dir.mkdir(parents=True)
    aa.save_json(run_dir / "metrics.json", {"now": aa.iso(NOW), "closed_max_rowid": 7})
    _write_valid_findings(run_dir, [_flaw()])
    assert aa.main(["record", str(run_dir)], p, NOW) == 0
    assert json.loads(capsys.readouterr().out)["new"] == ["A-1"]

    assert aa.main(["triage", "A-1", "wontfix", "owner wants auto on in paper"], p, NOW) == 0
    item = aa.load_json(p.ledger, {})["items"]["flaw:auto-approve-negative-ev"]
    assert item["status"] == "wontfix"
    assert item["history"][-1] == {
        "at": aa.iso(NOW),
        "event": "owner:wontfix",
        "note": "owner wants auto on in paper",
    }
    assert aa.main(["triage", "A-9", "accepted"], p, NOW) == 2
    assert aa.main(["triage", "A-1", "resolved"], p, NOW) == 2
    capsys.readouterr()

    # a wontfix finding seen again is suppressed, not re-opened
    run2 = p.runs / "2026-10-11-1500"
    run2.mkdir()
    _write_valid_findings(run2, [_flaw()])
    assert aa.main(["record", str(run2)], p, NOW) == 0
    assert json.loads(capsys.readouterr().out)["suppressed"] == ["A-1"]

    # accepted -> must be accounted for next run; resolved with evidence -> fixed -> regressed
    assert aa.main(["triage", "A-1", "accepted", "card t_x"], p, NOW) == 0
    run3 = p.runs / "2026-10-18-1500"
    run3.mkdir()
    _write_valid_findings(run3)
    assert aa.main(["record", str(run3)], p, NOW) == 2
    assert "A-1 (flaw:auto-approve-negative-ev) not accounted for" in capsys.readouterr().out
    doc = json.loads((run3 / "findings.json").read_text())
    doc["resolved"] = [{"key": "flaw:auto-approve-negative-ev", "evidence": "auto off since #80"}]
    (run3 / "findings.json").write_text(json.dumps(doc))
    assert aa.main(["record", str(run3)], p, NOW) == 0
    assert aa.load_json(p.ledger, {})["items"]["flaw:auto-approve-negative-ev"]["status"] == (
        "resolved"
    )


def test_mark_requires_record_and_advances_watermark(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    p = _paths(tmp_path, tmp_path / "unused.db")
    run_dir = p.runs / "r1"
    run_dir.mkdir(parents=True)
    aa.save_json(run_dir / "metrics.json", {"now": aa.iso(NOW), "closed_max_rowid": 7})
    assert aa.main(["mark", str(run_dir)], p, NOW) == 2
    assert "run `arc_analyst.py record RUN_DIR` first" in capsys.readouterr().out
    _write_valid_findings(run_dir)
    assert aa.main(["record", str(run_dir)], p, NOW) == 0
    assert aa.main(["mark", str(run_dir)], p, NOW) == 0
    state = aa.load_json(p.state, {})
    assert state["closed_watermark"] == 7
    assert state["last_run_at"] == aa.iso(NOW)
    assert set(state["themes"]) == set(aa.THEMES)
    assert aa.main(["reset"], p, NOW) == 0
    state = aa.load_json(p.state, {})
    assert "closed_watermark" not in state and "last_run_at" not in state
    assert "themes" in state  # reset forgets the watermark, not the theme history


# ---------------------------------------------------------------------------
# strategy gates enforced by the validator
# ---------------------------------------------------------------------------


def _rec(n: int, **exp: Any) -> dict[str, Any]:
    experiment = {
        "variable": "rank_by", "values": ["scanner", "managed_net_ev"],
        "metric": "net P&L after costs, max DD, 90% bootstrap CI vs incumbent",
        "effect_size": "+$40/trade", "status": "hypothesis, untested", **exp,
    }  # fmt: skip
    return {
        "key": "ranker:managed-net-ev", "title": "trial managed_net_ev", "severity": "info",
        "category": "recommendation", "bucket": "kind=debit_vertical", "n": n,
        "theme": "ranker", "evidence": "attribution-all.log", "recommendation": "run E7.5",
        "action": "new-card", "experiment": experiment,
        "draft_card": {"title": "E7.5b · ranker trial", "body": "Goal / Acceptance"},
    }  # fmt: skip


def _errs(findings: list[dict[str, Any]], **kw: Any) -> list[str]:
    doc = {"verdict": "findings", "themes": kw.pop("themes", _themes()), "findings": findings,
           "experiments": kw.pop("lines", []),
           "next_experiment": kw.pop("next_experiment", "none: n < 30")}  # fmt: skip
    return aa.validate_findings(doc, {"items": {}}, kw.pop("prev", None), kw.pop("registry", []))


def test_min_sample_blocks_recommendations_below_30() -> None:
    assert any("MIN-SAMPLE" in e and "n=29" in e for e in _errs([_rec(29)]))
    assert _errs([_rec(30)]) == []
    # flaws are allowed at any N
    assert _errs([_flaw(n=1)]) == []
    missing_n = _flaw()
    del missing_n["n"]
    assert any("n (closed trades" in e for e in _errs([missing_n]))


def test_one_variable_experiment_spec_required() -> None:
    no_effect = _errs([_rec(40, effect_size="")])
    assert any("ONE-VARIABLE" in e and "effect_size" in e for e in no_effect)
    assert any("one name" in e for e in _errs([_rec(40, variable=["rank_by", "dte_min"])]))
    assert any("incumbent" in e for e in _errs([_rec(40, values=["scanner"])]))
    assert any("status must be" in e for e in _errs([_rec(40, status="proven")]))
    assert any("harness_ref" in e for e in _errs([_rec(40, status="harness-run")]))
    recs = [{**_rec(40), "key": f"ranker:{i}"} for i in range(4)]
    assert any("at most 3" in e for e in _errs(recs))


# ---------------------------------------------------------------------------
# forward experiments (D44, E10.6)
# ---------------------------------------------------------------------------

REGISTRY = [
    {"id": "XP-1", "status": "stopped", "area": "other"},
    {"id": "XP-2", "status": "running", "area": "exits"},
    {"id": "XP-3", "status": "queued", "area": "exits"},
]
LINES = [
    {"experiment_id": e["id"], "status": e["status"], "note": "n=0 closed per arm"}
    for e in REGISTRY
]


def _fwd(**over: Any) -> dict[str, Any]:
    spec = {
        "id": "XP-4", "title": "managed_net_ev ranker",
        "hypothesis": "ranking by managed net EV adds ~0.05%/day net",
        "area": "ranking", "kind": "ab",
        "arms": {"treatment": {"overlay": {
            "exits": {"pipeline": {"rank_menu_by": "managed_net_ev"}}}}},
        "non_inferiority_margin": 0.5, "backtest_ref": "docs/RESEARCH/backtests/r1",
    }  # fmt: skip
    spec.update(over)
    return {k: v for k, v in spec.items() if v is not None}


def _rec_fwd(spec: dict[str, Any] | None = None, **exp: Any) -> dict[str, Any]:
    exp = {"status": "harness-run", "harness_ref": "docs/RESEARCH/backtests/r1",
           "variable": "rank_menu_by", **exp}  # fmt: skip
    return {**_rec(40, **exp), "forward_spec": spec if spec is not None else _fwd()}


def _ferrs(findings: list[dict[str, Any]], **kw: Any) -> list[str]:
    kw.setdefault("lines", LINES)
    kw.setdefault("registry", REGISTRY)
    kw.setdefault("next_experiment", "ranker:managed-net-ev")
    return _errs(findings, **kw)


def test_forward_spec_valid_after_the_harness_run() -> None:
    assert _ferrs([_rec_fwd()]) == []
    # validated against the real ExperimentSpec model, so the format really is the live one
    from arc.experiments.models import ExperimentSpec

    ExperimentSpec.model_validate({**_fwd(), "proposed_by": "A-1"})


@pytest.mark.parametrize(
    ("finding", "needle"),
    [
        (_rec_fwd(status="hypothesis, untested", harness_ref=None), "harness-run"),
        (_rec_fwd(_fwd(backtest_ref="docs/other")), "backtest_ref must equal"),
        (_rec_fwd(_fwd(kind="aa")), "kind must be 'ab'"),
        (_rec_fwd(_fwd(id="XP-2")), "already used"),
        (_rec_fwd(_fwd(id="exp-4")), "must look like XP-<n>"),
        (_rec_fwd(_fwd(area="vibes")), "area must be one of"),
        (_rec_fwd(_fwd(proposed_by="owner")), "proposed_by"),
        (_rec_fwd(_fwd(alpha_level=0.1)), "unknown keys"),
        (_rec_fwd(_fwd(hypothesis="")), "missing"),
        (_rec_fwd(_fwd(non_inferiority_margin=0)), "non_inferiority_margin"),
        (_rec_fwd(_fwd(arms={"treatment": {"overlay": {"exits": {"pipeline": {
            "rank_menu_by": "managed_net_ev", "top_n": 3}}}}})), "changes 2 values"),
        (_rec_fwd(_fwd(arms={"treatment": {"overlay": {"exits": {"take_profit_pct": 0.4}}}})),
         "experiment.variable is 'rank_menu_by'"),
        (_rec_fwd(_fwd(arms={"treatment": {"overlay": {"gate": {"x": 1}}}})), "overlay targets"),
        (_rec_fwd(_fwd(arms={"control": {"overlay": {"ranking": {"rank_by": "x"}}},
                             "treatment": {"overlay": {"ranking": {"rank_by": "y"}}}})),
         "control arm is the production config"),
        ({**_flaw(), "forward_spec": _fwd()}, "only allowed on a recommendation"),
    ],
)  # fmt: skip
def test_forward_spec_rules(finding: dict[str, Any], needle: str) -> None:
    errs = _ferrs([finding], next_experiment="none: test")
    assert any(needle in e for e in errs), errs


def test_experiments_section_covers_the_registry() -> None:
    errs = _ferrs([], lines=LINES[:2], next_experiment="none: x")
    assert any("XP-3 (queued) has no line" in e for e in errs)
    wrong = [{**LINES[1], "status": "stopped"}, LINES[0], LINES[2]]
    assert any("status 'stopped' != registry 'running'" in e
               for e in _ferrs([], lines=wrong, next_experiment="none: x"))  # fmt: skip
    ghost = [*LINES, {"experiment_id": "XP-9", "status": "running", "note": "?"}]
    assert any("XP-9 is not in the experiment registry" in e
               for e in _ferrs([], lines=ghost, next_experiment="none: x"))  # fmt: skip
    assert any("needs a note" in e for e in _ferrs(
        [], lines=[{**LINES[0], "note": ""}, *LINES[1:]], next_experiment="none: x"))  # fmt: skip
    assert any("next_experiment is required" in e for e in _ferrs([], next_experiment=""))
    assert any("must be the key of a finding with a forward_spec" in e
               for e in _ferrs([_rec(40)], next_experiment="ranker:managed-net-ev"))  # fmt: skip
    assert _ferrs([], next_experiment="none: no bucket has n >= 30") == []
    # promoted / rejected experiments need no line
    done = [{"id": "XP-5", "status": "promoted", "area": "sizing"}]
    assert _ferrs([], registry=[*REGISTRY, *done], next_experiment="none: x") == []


@pytest.fixture(scope="module")
def _experiment_db(_pipeline_db: bytes) -> bytes:
    from tests import experiment_fixtures as fx

    conn = _conn(_pipeline_db)
    fx.reviewer_registry(conn)
    return conn.serialize()


@pytest.mark.usefixtures("_stub_views")
def test_context_has_forward_experiments_and_record_writes_draft_specs(
    _experiment_db: bytes, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    conn = _conn(_experiment_db)
    _close_one(conn, NOW - _dt.timedelta(days=2))
    db = _to_disk(conn, tmp_path / "live.db")
    p = _paths(tmp_path, db)
    assert aa.main([], p, NOW) == 0
    out = capsys.readouterr().out
    assert "## Forward experiments (D44; next free id XP-4;" in out
    assert "### XP-1 [stopped (futility)] aa/other" in out
    assert "- stop: sessions 10 sigma" in out
    assert "verdict FUTILITY" in out and "always-valid CI [" in out
    assert "- calibration: sigma" in out and "MDE 10:" in out
    assert "- arm control: P&L" in out and "- arm treatment: P&L" in out
    assert "- breakdowns: no closed trade in either arm yet" in out
    assert "### XP-2 [running] ab/exits" in out and "- latest report: none stored yet" in out
    assert '- treatment overlay: {"exits": {"default": {"take_profit_pct": 0.4}}}' in out
    assert "- exits: holder XP-2 | queued XP-3" in out
    run_dir = _run_dir(p)
    metrics = aa.load_json(run_dir / "metrics.json", {})
    assert [e["id"] for e in metrics["experiments"]] == ["XP-1", "XP-2", "XP-3"]
    # record: the registry snapshot is enforced and the draft spec is written for the owner
    doc = {"verdict": "findings", "themes": _themes(), "findings": [_rec_fwd()], "resolved": [],
           "experiments": LINES, "next_experiment": "ranker:managed-net-ev"}  # fmt: skip
    (run_dir / "findings.json").write_text(json.dumps({**doc, "experiments": LINES[:1]}))
    assert aa.main(["record", str(run_dir)], p, NOW) == 2
    assert "XP-2 (running) has no line" in capsys.readouterr().out
    (run_dir / "findings.json").write_text(json.dumps(doc))
    assert aa.main(["record", str(run_dir)], p, NOW) == 0
    printed = json.loads(capsys.readouterr().out)
    draft = run_dir / "forward-specs" / "XP-4.yaml"
    assert printed["forward_specs"] == [str(draft)]
    text = draft.read_text()
    assert "DRAFT forward experiment from Arc Analyst A-1" in text
    spec = yaml.safe_load(text)
    assert spec["proposed_by"] == "A-1" and spec["kind"] == "ab"
    # the owner's `arc experiment create --spec` loader accepts the draft as written
    from arc.experiments.overlay import load_spec

    assert load_spec(draft).id == "XP-4"


@pytest.mark.usefixtures("_stub_views")
def test_experiment_status_change_wakes_the_analyst(
    _experiment_db: bytes, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    conn = _conn(_experiment_db)
    _close_one(conn, NOW - _dt.timedelta(days=30))
    db = _to_disk(conn, tmp_path / "live.db")
    p = _paths(tmp_path, db)
    # last run before the fixture's XP-2 start (2026-10-17), nothing closed since
    aa.save_json(
        p.state,
        {"closed_watermark": 10**9, "last_run_at": aa.iso(_dt.datetime(2026, 10, 15, tzinfo=ET))},
    )
    assert aa.main([], p, NOW + _dt.timedelta(days=14)) == 0
    out = capsys.readouterr().out
    assert "wakeAgent" not in out
    # XP-1 stopped (fixture clock, Oct 5) before the last run; XP-3 queued is not a wake
    assert "WAKE: experiment status change(s) since the last run: XP-2 running\n" in out
    # once those events are older than the last run (XP-2 ran at ~16:30 ET Oct 17): quiet
    aa.save_json(
        p.state, {"closed_watermark": 10**9, "last_run_at": aa.iso(NOW + _dt.timedelta(days=14))}
    )
    assert aa.main([], p, NOW + _dt.timedelta(days=15)) == 0
    assert _last_json(capsys.readouterr().out) == {"wakeAgent": False}


def test_pre_e10_store_has_no_experiments(_pipeline_db: bytes) -> None:
    conn = _conn(_pipeline_db)
    for t in ("experiment_reports", "experiment_events", "experiments"):
        conn.execute(f"DROP TABLE IF EXISTS {t}")
    assert aa.experiments_overview(conn) == []
    assert aa.experiment_lines([]) == ["- none registered yet (arc experiment list is empty)"]
    assert aa.next_experiment_id([]) == "XP-1"


def test_skill_documents_forward_experiments() -> None:
    text = _skill()
    for rule in (
        "FORWARD EXPERIMENTS (D44)",
        "you never register,\n   start, stop or promote",
        "`forward_spec`",
        "`next_experiment`",
        "RUN_DIR/forward-specs/<XP-n>.yaml",
        "*Experiments*",
        "register XP-<n>",
        "No guardrails",
    ):
        assert rule in text, rule
    assert "Experiments" in aa.REPORT_SECTIONS


def test_every_standing_theme_needs_a_status() -> None:
    themes = [t for t in _themes() if t["theme"] != "sizing"]
    assert "standing theme 'sizing' has no status line" in _errs([], themes=themes)
    prev = {"ranker": {"status": "settled"}}
    reopened = _themes(ranker="watching")
    assert any(
        "re-open it only with new_evidence" in e for e in _errs([], themes=reopened, prev=prev)
    )
    reopened[[t["theme"] for t in reopened].index("ranker")]["new_evidence"] = "n=35 now"
    assert _errs([], themes=reopened, prev=prev) == []


# ---------------------------------------------------------------------------
# report contract, skill text, install script
# ---------------------------------------------------------------------------


def _skill() -> str:
    return (ANALYST / "skills" / "arc-analyst" / "SKILL.md").read_text()


def _report_template() -> str:
    body = _skill().split("## Slack report format", 1)[1]
    return body.split("```", 2)[1].removeprefix("\n")


def test_report_template_validates_size_cap(tmp_path: Path) -> None:
    template = _report_template()
    assert aa.validate_report(template) == []
    assert len(template) <= aa.REPORT_MAX
    for theme in aa.THEMES:
        assert theme in _skill()
    long = tmp_path / "r.md"
    long.write_text(template + "x" * aa.REPORT_MAX)
    assert aa.main(["check-report", str(long)]) == 2
    missing = tmp_path / "m.md"
    missing.write_text(template.replace("Standing themes", "Themes"))
    assert aa.cmd_check_report(missing) == 2


def test_skill_frontmatter_and_rules() -> None:
    text = _skill()
    assert text.startswith("---\n")
    fm = yaml.safe_load(text[4 : text.index("\n---\n", 4)])
    assert fm["name"] == "arc-analyst"
    assert len(fm["description"]) <= 60 and fm["description"].endswith(".")
    assert len(text) <= 100_000
    for rule in (
        "MIN-SAMPLE",
        "ONE-VARIABLE",
        "STANDING THEMES",
        "OBVIOUS FLAWS",
        "hypothesis, untested",
        "at most 8 fetches",
        "create A-<n>",
        "comment A-<n>",
        "wontfix A-<n>",
        "accept A-<n>",
        "`rerun`",
        "Never open\n  `data/arc.db`",
    ):
        assert rule in text, rule
    boundary = "does the\nfix change a decision the system makes → Analyst"
    assert boundary in text
    assert "~/" not in text.split("## Owner commands")[0].split("## Procedure")[0].replace(
        "~/.hermes/skills/", ""
    )  # only profile paths, no user checkout paths in the rules


def test_install_dry_run_creates_paused_cron(tmp_path: Path) -> None:
    r = subprocess.run(
        ["bash", str(ANALYST / "install.sh"), "--dry-run"],
        capture_output=True, text=True, check=True, timeout=60,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path),
             "ARC_ANALYST_PROFILE_HOME": str(tmp_path / "prof"), "HERMES_BIN": "hermes"},
    )  # fmt: skip
    out = r.stdout
    assert not (tmp_path / "prof").exists()  # dry run writes nothing
    cron = next(line for line in out.splitlines() if "cron create" in line)
    for part in (
        "-p arc-analyst",
        "0\\ 14\\ \\*\\ \\*\\ 0",
        "--name arc-analyst-weekly-audit",
        "--skill arc-analyst",
        "--script arc_analyst.py",
        "--model anthropic/claude-fable-5.1",
        "--reasoning-effort max",
        "--deliver slack:C0C5CB72LG5",
        "--paused",
    ):
        assert part in cron, part
    assert "arc_analyst.py" in out and "skills/arc-analyst/SKILL.md" in out
    skill = (ANALYST / "skills" / "arc-analyst" / "SKILL.md").read_text()
    for helper in ("defuddle", "agent-reach"):  # D42: pinned web helpers only
        assert (REPO / "hermes" / "shared-skills" / helper / "SKILL.md").is_file(), helper
        assert f"{tmp_path / 'prof'}/skills/{helper}" in out, helper
        assert helper in skill, helper
    assert "code-review-and-quality" not in out
    for secret in ("ALPACA", "ARC_GATE_SECRET", ".env"):
        assert secret not in (ANALYST / "install.sh").read_text().split("set -euo")[1]


def test_gate_parses_under_python39() -> None:
    """The cron runs the gate with the host's system ``python3`` (macOS: 3.9), not the venv.

    ``datetime.UTC`` (3.11) broke ``reset`` there; ``zip(strict=)`` (3.10) would too.
    """
    import ast

    tree = ast.parse((ANALYST / "arc_analyst.py").read_text(), feature_version=(3, 9))
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == "UTC":
            raise AssertionError(f"datetime.UTC needs 3.11 (line {node.lineno})")
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "zip":
            assert not node.keywords, f"zip(strict=) needs 3.10 (line {node.lineno})"
