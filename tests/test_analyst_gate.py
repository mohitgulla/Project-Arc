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
    real = aa.run_arc

    def fake(p: Any, args: list[str], run_dir: Path, name: str, timeout: int = 180) -> str:
        calls.append(list(args))
        if args[:2] == ["backtest", "study"]:  # E7.15: only fixture specs, keep it real
            return real(p, args, run_dir, name, timeout)
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
    # E7.15: backtest inputs from a per-test root (the worktree's data/ is not the host's)
    return aa.Paths(
        home=tmp_path / "analyst", repo=REPO, live_db=db, arc=ARC_ARGV,
        backtest_root=tmp_path / "bt",
    )  # fmt: skip


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
    assert "no new closed outcome, no halt, no experiment status change and no new backtest" in out


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
    p.idea_seed.write_text((ANALYST / "idea_seed.yaml").read_text())  # as install.sh does
    assert aa.main([], p, NOW) == 0
    out = capsys.readouterr().out
    assert "wakeAgent" not in out
    assert len(out) <= aa.MAX_STDOUT
    run_dir = _run_dir(p)
    assert out.startswith(f"RUN_DIR={run_dir}")
    assert "CLOSED_TRADES: week=1 all_time=1" in out
    # realised-vs-model table by kind x regime x sleeve (E22.5: the fixture condor
    # maps to `short` by its entry DTE since D118's 10-35 window, windows from
    # `arc scorecard sleeves --json`)
    assert "## Realised vs model, this week (kind x regime x DTE sleeve)" in out
    assert re.search(
        r"\| iron_condor \| risk_on \| short \| 1 \| 98\.00 \| 40\.00 \| \+58\.00 \|", out
    )
    # E22.5 (D97): the per-sleeve report with the keep/shrink rule
    assert "## DTE sleeves, all time (E22.5, D97" in out
    assert "- rule (D97, fixed in advance, report only): Read after >= 30 closed" in out
    assert "- short (10-35 DTE): opens 0, closed n=0" in out
    assert "verdict collect (not ready)" in out
    assert (run_dir / "sleeves-all.log").read_text().strip().startswith("{")
    assert "LOW (n<20)" in out
    assert "slippage_frac = 0.25" in out
    # scorecard (attribution + weekly), gaps, counterfactual, config
    assert "attribution by kind, regime since" in out
    assert "closed trades: 1 · low_sample below n=20" in out
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
    # E21.2 (D86): the installed idea seed's leads
    assert "## Experiment idea seed (D86: leads from retired dev drafts" in out
    assert "- S-1 [entries] Velocity-weighted trending tier" in out
    assert "- S-5 [entries] Unified managed-EV menu measure" in out
    # E21.3 (D86) / E25.4 (D108): capacity, the K=3 plan grid, the (empty) backlog, ...
    assert "## Experiment capacity (D69/D108" in out
    assert (
        "- max_parallel_arms 10: 0 arm(s) registered/running (none); "
        "a K=3 experiment needs 4 (control + 3 treatments, D108)"
    ) in out
    assert "## Sessions needed at K=3" in out
    # E25.5 (D108): the regime panel, from `arc experiment regime-panel --json`
    assert "## Regime panel (E25.5, D108" in out and "- no v3 sessions yet" in out
    assert json.loads((run_dir / "regime-panel.log").read_text())["sessions"] == 0
    # no A/A sigma in the fixture store: the grid uses the documented XP-1 A/A sigma
    assert "- sigma +0.036%/day (docs/RESEARCH/experiments/XP-1-aa.md (no A/A sigma" in out
    assert "- effect +0.020%/day -> 60 sessions\n" in out  # K=3 (D108): 63 at K=4
    assert "- effect +0.050%/day -> 10 sessions (runs at least min_sessions: use 20)" in out
    assert "## XP backlog (yours;" in out and "- empty (no XP proposal yet" in out
    assert "## Quick changes shipped since the last review" in out
    assert "- none shipped since the last review" in out
    assert "## Strategy PRs merged since the last run" in out
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
           "quick_changes_none": "no bucket has n >= 20",
           "next_experiment": "none: no bucket has n >= 20"}  # fmt: skip
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
# strategy gates enforced by the validator (D86: two tracks, MIN-SAMPLE 20)
# ---------------------------------------------------------------------------


def _qc(n: int = 24, **over: Any) -> dict[str, Any]:
    q = {
        "change": "exits.default.take_profit_pct 0.5 -> 0.4", "kind": "config",
        "evidence_kind": "live", "expected_effect": "+0.02%/day net (n=24 iron condors)",
        "risk": "fewer winners run to full credit",
        "rollback": "!arc config revert exits.default.take_profit_pct",
        "apply": "!arc config set exits.default.take_profit_pct 0.4 -- Analyst",
        "watch": {"metric": "iron_condor realised net/trade", "n": 20}, **over,
    }  # fmt: skip
    return {
        "key": "quick-change:tp-0.4", "title": "take profit at 40%", "severity": "medium",
        "category": "quick-change", "bucket": "kind=iron_condor", "n": n,
        "theme": "exit-policy", "evidence": "attribution-all.log: n=24 iron condors",
        "recommendation": "take profit at 40%", "action": "owner-decision",
        "quick_change": {k: v for k, v in q.items() if v is not None},
    }  # fmt: skip


def _overlay(v: Any) -> dict[str, Any]:
    return {"overlay": {"exits": {"default": {"take_profit_pct": v}}}}


def _spec(**over: Any) -> dict[str, Any]:
    spec = {
        "title": "Take-profit level", "area": "exits",
        "hypothesis": "an earlier take-profit adds ~0.05%/day net",
        "arms": {"control": {}, "treatments": {
            f"t{i}": _overlay(v) for i, v in enumerate((0.3, 0.6, 0.75), 1)}},
        "non_inferiority_margin": 0.5, "backtest_ref": "BT-3",
    }  # fmt: skip
    spec.update(over)
    return {k: v for k, v in spec.items() if v is not None}


def _prop(key: str = "xp-proposal:tp-level", n: int = 6, **over: Any) -> dict[str, Any]:
    p = {
        "spec": _spec(), "evidence_kind": "backtest", "evidence_ref": "BT-3",
        "evidence": "BT-3 holdout: +0.05%/day", "affected_share": 0.6, "p_success": 0.3,
        "expected_effect": {"pct_per_day": 0.05, "source": "BT-3 holdout mean diff"},
        "sessions_needed": 40, **over,
    }  # fmt: skip
    p.setdefault("value_score", aa.proposal_value(p))
    return {
        "key": key, "title": "take-profit level x3", "severity": "info",
        "category": "xp-proposal", "bucket": "kind=iron_condor", "n": n,
        "theme": "exit-policy", "evidence": "BT-3", "recommendation": "run it",
        "action": "owner-decision", "proposal": {k: v for k, v in p.items() if v is not None},
    }  # fmt: skip


def _bt_line(
    sid: str = "BT-3",
    phase: str = "holdout",
    universe: str = "c500",
    switch: bool = True,
    spec_hash: str = "a" * 64,
    overlay: dict[str, Any] | None = None,
    at: str = "2026-10-03T18:00:00-04:00",
    ranker: str | None = None,
) -> dict[str, Any]:
    t1 = {"ranker": ranker, "overlay": _overlay(0.4)["overlay"] if overlay is None else overlay}
    return {
        "at": at, "study": sid, "spec_hash": spec_hash, "arm_set_hash": "b" * 64,
        "arms": {"control": {"ranker": None, "overlay": {}}, "treatments": {"t1": t1}},
        "profiles": ["margin"], "phase": phase, "universe": universe,
        "start": "2024-01-01" if phase == "holdout" else "2020-01-01",
        "end": "2026-09-30" if phase == "holdout" else "2023-12-31",
        "git_sha": "abc123def456", "primary_metric": "net_pnl", "metrics": {},
        "verdicts": {"margin": {"t1": {"switch": switch, "pnl_diff": 1234.5 if switch else -50.0,
                                       "ci_lo": 100.0 if switch else -900.0, "ci_hi": 2400.0}}},
        "variants": 1, "overfit_warning": False, "run_dir": "x",
    }  # fmt: skip


def _studies(*lines: dict[str, Any], current: dict[str, dict[str, Any]] | None = None) -> list:
    return aa.study_overview(list(lines), current or {})


BT3_HASH = "a" * 64
BT3 = _studies(
    _bt_line(phase="dev", universe="c125", at="2026-10-01T18:00:00-04:00"), _bt_line(),
    current={"BT-3": {"spec_hash": BT3_HASH, "hypothesis": "tp 0.4 beats 0.5",
                      "max_variants": 3, "universe_ladder": ["c125", "c500"]}},
)  # fmt: skip


def _errs(findings: list[dict[str, Any]], **kw: Any) -> list[str]:
    doc = {"verdict": "findings", "themes": kw.pop("themes", _themes()), "findings": findings,
           "experiments": kw.pop("lines", []),
           "quick_changes_none": kw.pop("qc_none", "no bucket has n >= 20"),
           "next_experiment": kw.pop("next_experiment", "none: n < 20"),
           "next_slot": kw.pop("next_slot", "free now"), **kw.pop("extra", {})}  # fmt: skip
    return aa.validate_findings(
        doc, kw.pop("ledger", {"items": {}}), kw.pop("prev", None), kw.pop("registry", []),
        backlog=kw.pop("backlog", None), seed_ids=kw.pop("seed_ids", frozenset()),
        max_arms=kw.pop("max_arms", 10), studies=kw.pop("studies", BT3),
    )  # fmt: skip


def test_min_sample_is_20_for_live_evidence() -> None:
    assert aa.MIN_SAMPLE == 20
    assert any("MIN-SAMPLE" in e and "n=19" in e for e in _errs([_qc(19)]))
    assert _errs([_qc(20)]) == []
    live = _prop(n=19, evidence_kind="live", evidence_ref=None)
    assert any("MIN-SAMPLE" in e and "n=19" in e for e in _errs([live]))
    assert _errs([_prop(n=20, evidence_kind="live", evidence_ref=None)]) == []
    # option A: a BT-<n> holdout study backs a proposal at any live n, never a quick change
    assert _errs([_prop(n=2)]) == []
    assert any("backtest alone" in e for e in _errs([_qc(40, evidence_kind="backtest")]))
    # flaws are allowed at any N
    assert _errs([_flaw(n=1)]) == []
    missing_n = _flaw()
    del missing_n["n"]
    assert any("n (closed trades" in e for e in _errs([missing_n]))


@pytest.mark.parametrize(
    ("finding", "needle"),
    [
        (_qc(apply="set it"), "Slack command"),
        (_qc(watch=None), "watch needs metric and n"),
        (_qc(rollback=""), "missing ['rollback']"),
        (_qc(kind="yaml"), "kind must be one of"),
        (_qc(evidence_kind="xp-winner"), "winning XP-<n>"),
        (_qc(kind="code", apply="edit arc/pipeline/steps.py"), "action new-card"),
        ({**_qc(kind="code", apply="edit steps.py"), "action": "new-card",
          "draft_card": {"title": "E1.1a · x", "body": "Goal"}}, "XP-advisory: none"),
        ({**_flaw(), "quick_change": _qc()["quick_change"]}, "only allowed on a quick-change"),
        ({**_qc(), "forward_spec": {}}, "retired (D86)"),
    ],
)  # fmt: skip
def test_quick_change_rules(finding: dict[str, Any], needle: str) -> None:
    errs = _errs([finding])
    assert any(needle in e for e in errs), errs


def test_quick_change_track_needs_items_or_a_reason() -> None:
    assert any("quick_changes_none" in e for e in _errs([], qc_none=""))
    assert _errs([_qc()], qc_none="") == []
    many = [{**_qc(), "key": f"quick-change:{i}"} for i in range(4)]
    assert any("at most 3" in e for e in _errs(many))


@pytest.mark.parametrize(
    ("finding", "needle"),
    [
        (_prop(spec=_spec(id="XP-9")), "reserves its XP-<n> when it records"),
        (_prop(spec=_spec(alpha=0.1)), "filled from config/experiments.yaml"),
        (_prop(spec=_spec(kind="aa")), "kind must be 'ab'"),
        (_prop(spec=_spec(area="vibes")), "area must be one of"),
        (_prop(spec=_spec(alpha_level=0.1)), "unknown keys"),
        (_prop(spec=_spec(hypothesis="")), "missing"),
        (_prop(spec=_spec(non_inferiority_margin=0)), "non_inferiority_margin"),
        (_prop(spec=_spec(arms={"treatment": _overlay(0.4)})), "control and treatments only"),
        (_prop(spec=_spec(arms={"control": {}, "treatments": {
            "t1": _overlay(0.3), "t2": _overlay(0.4)}})), "THREE-TREATMENTS"),
        (_prop(spec=_spec(arms={"control": {}, "treatments": {
            f"t{i}": _overlay(v) for i, v in enumerate((0.3, 0.4, 0.6, 0.7), 1)}})),
         "THREE-TREATMENTS"),
        (_prop(spec=_spec(spec_version=4)), "spec_version must be 3"),
        (_prop(spec=_spec(arms={"control": {}, "treatments": {
            "t1": _overlay(0.3), "t2": _overlay(0.4),
            "t3": {"overlay": {"ranking": {"rank_by": "x"}}}}})), "SAME-LEAVES"),
        (_prop(spec=_spec(arms={"control": {}, "treatments": {
            "t1": _overlay(0.3), "t2": _overlay(0.3), "t3": _overlay(0.6)}})),
         "identical to t1"),
        (_prop(spec=_spec(arms={"control": _overlay(0.5), "treatments": {
            f"t{i}": _overlay(v) for i, v in enumerate((0.3, 0.6, 0.7), 1)}})),
         "control arm is the production config"),
        (_prop(spec=_spec(arms={"control": {}, "treatments": {
            f"t{i}": {"overlay": {"gate": {"x": v}}} for i, v in enumerate((1, 2, 3), 1)}})),
         "not in"),
        (_prop(spec=_spec(backtest_ref="BT-4")), "backtest_ref must equal"),
        (_prop(evidence_ref="docs/x"), "BT-<n> study id"),
        (_prop(value_score=1.0), "value_score 1.0 !="),
        (_prop(sessions_needed=0), "sessions_needed must be an int"),
        (_prop(p_success=1.5), "p_success must be a number in (0, 1]"),
        (_prop(score_override={"value": 1}), "score_override needs value and reason"),
        (_prop(seed_ref="S-99"), "not a lead in the idea seed"),
        ({**_flaw(), "proposal": {}}, "only allowed on an xp-proposal"),
    ],
)  # fmt: skip
def test_proposal_rules(finding: dict[str, Any], needle: str) -> None:
    errs = _errs([finding])
    assert any(needle in e for e in errs), errs


def test_proposal_spec_is_the_live_format_once_approved() -> None:
    """The proposal spec plus what approval fills is a real v3 ``ExperimentSpec`` (D108)."""
    from arc.experiments.models import ExperimentSpec

    assert _errs([_prop()]) == []
    spec = ExperimentSpec.model_validate(
        {**_spec(), "id": "XP-9", "kind": "ab", "spec_version": 3, "proposed_by": "A-1"}
    )
    assert len(spec.arms.treatments) == 3 and spec.control_runs
    # the gate's mirror tracks the model's fields
    assert set(aa.SPEC_KEYS) == set(ExperimentSpec.model_fields)


def test_new_proposals_capped_per_run_and_backlog_capped() -> None:
    props = [_prop(key=f"xp-proposal:{i}") for i in range(3)]
    assert any("at most 2 per run" in e for e in _errs(props))
    full = aa.empty_backlog()
    for i in range(aa.BACKLOG_CAP):
        aa.apply_backlog(full, {"findings": [_prop(key=f"xp-proposal:old{i}")]}, "r0", "t0")
    assert any("cap is 12" in e for e in _errs([_prop()], backlog=full))
    drop = {"backlog_drops": [{"id": "XP-1", "reason": "weakest value"}]}
    assert _errs([_prop()], backlog=full, extra=drop) == []
    assert any("needs a reason" in e for e in _errs(
        [_prop()], backlog=full, extra={"backlog_drops": [{"id": "XP-1"}]}))  # fmt: skip


def test_apply_backlog_reserves_xp_ids_idempotently() -> None:
    b = aa.empty_backlog()
    doc = {"findings": [_prop(seed_ref="S-2")]}
    assert aa.apply_backlog(b, doc, "r1", "t1")["new"] == ["XP-1"]
    entry = b["items"]["xp-proposal:tp-level"]
    assert entry["status"] == "proposed" and entry["spec"]["area"] == "exits"
    assert b["seed"]["S-2"] == {"status": "proposed", "proposal": "XP-1"}
    again = aa.apply_backlog(b, doc, "r2", "t2")
    assert again == {"new": [], "updated": [], "dropped": [], "seed_dropped": []}
    assert len(entry["history"]) == 1
    edited = {"findings": [_prop(seed_ref="S-2", p_success=0.5)]}
    assert aa.apply_backlog(b, edited, "r3", "t3")["updated"] == ["XP-1"]
    assert entry["history"][-1]["fields"] == ["p_success", "value_score"]
    # an approved spec is frozen
    entry["status"] = "approved"
    frozen = _prop(seed_ref="S-2", spec=_spec(title="new design"))
    assert any("spec is frozen" in e for e in _errs([frozen], backlog=b, seed_ids={"S-2"}))


def test_ranking_owner_pin_then_value() -> None:
    b = aa.empty_backlog()
    aa.apply_backlog(b, {"findings": [_prop(key="xp-proposal:a", p_success=0.2),
                                      _prop(key="xp-proposal:b", p_success=0.4)]},
                     "r", "t")  # fmt: skip
    assert [e["id"] for e in aa.ranked_backlog(b)] == ["XP-2", "XP-1"]
    aa.backlog_by_id(b, "XP-1")["pinned"] = {"at": "2026-10-10T10:00:00-04:00", "reason": "x"}
    assert [e["id"] for e in aa.ranked_backlog(b)] == ["XP-1", "XP-2"]
    line = aa.value_formula(aa.backlog_by_id(b, "XP-1"))
    assert line.startswith("value 0.000150 = +0.050%/day x share 0.60 x p 0.20 / 40 sessions")
    assert "PINNED by owner" in line


# ---------------------------------------------------------------------------
# forward experiments: the Experiments section, Next XP and slots (D44, D69, D86)
# ---------------------------------------------------------------------------

REGISTRY = [
    {"id": "XP-1", "status": "stopped", "area": "other", "arms": 1},
    {"id": "XP-2", "status": "running", "area": "exits", "arms": 1},
    {"id": "XP-3", "status": "queued", "area": "exits", "arms": 1},
]
ADVICE = {"advice": "continue", "why": ""}
LINES = [
    {"experiment_id": e["id"], "status": e["status"], "note": "n=0 closed per arm",
     **({"stop_advice": ADVICE} if e["status"] == "running" else {})}
    for e in REGISTRY
]  # fmt: skip


def _ferrs(findings: list[dict[str, Any]], **kw: Any) -> list[str]:
    kw.setdefault("lines", LINES)
    kw.setdefault("registry", REGISTRY)
    return _errs(findings, **kw)


def test_experiments_section_covers_the_registry() -> None:
    errs = _ferrs([], lines=LINES[:2])
    assert any("XP-3 (queued) has no line" in e for e in errs)
    wrong = [{**LINES[1], "status": "stopped"}, LINES[0], LINES[2]]
    assert any("status 'stopped' != registry 'running'" in e for e in _ferrs([], lines=wrong))
    ghost = [*LINES, {"experiment_id": "XP-9", "status": "running", "note": "?"}]
    assert any("XP-9 is not in the experiment registry" in e for e in _ferrs([], lines=ghost))
    assert any(
        "needs a note" in e for e in _ferrs([], lines=[{**LINES[0], "note": ""}, *LINES[1:]])
    )
    no_advice = [LINES[0], {k: v for k, v in LINES[1].items() if k != "stop_advice"}, LINES[2]]
    assert any("XP-2 is running: stop_advice" in e for e in _ferrs([], lines=no_advice))
    bare = [LINES[0], {**LINES[1], "stop_advice": {"advice": "futility"}}, LINES[2]]
    assert any("futility needs a why" in e for e in _ferrs([], lines=bare))
    assert any("next_experiment is required" in e for e in _ferrs([], next_experiment=""))
    assert any("needs a reason" in e for e in _ferrs([], next_experiment="none"))
    assert _ferrs([]) == []
    # promoted / rejected experiments need no line
    done = [{"id": "XP-5", "status": "promoted", "area": "sizing"}]
    assert _ferrs([], registry=[*REGISTRY, *done]) == []


def test_next_experiment_names_a_ranked_proposal_and_its_slot() -> None:
    assert any("must be a proposed/approved XP-<n>" in e
               for e in _ferrs([], next_experiment="XP-7"))  # fmt: skip
    # the new proposal reserves XP-4 (above the registry's XP-1..3, D121; area exits);
    # XP-2 running + XP-3 queued hold the area
    errs = _ferrs([_prop()], next_experiment="XP-4", next_slot="soon")
    assert any("XP-2, XP-3" in e and "next_slot must name" in e for e in errs), errs
    assert _ferrs([_prop()], next_experiment="XP-4", next_slot="after XP-2 + XP-3 ~Nov 12") == []
    # another area is free now, but capacity counts arms: 7 used + (control + 3) > 10
    ranking = _prop(spec=_spec(area="ranking"))
    assert _ferrs([ranking], next_experiment="XP-4", next_slot="free now") == []
    heavy = [{**REGISTRY[1], "arms": 7}, REGISTRY[0], REGISTRY[2]]
    errs = _ferrs([ranking], registry=heavy, next_experiment="XP-4", next_slot="free now")
    assert any("XP-2" in e for e in errs), errs
    # not the top-ranked one: say why
    two = [_prop(key="xp-proposal:a", p_success=0.2, spec=_spec(area="ranking")),
           _prop(key="xp-proposal:b", p_success=0.4, spec=_spec(area="sizing"))]  # fmt: skip
    errs = _ferrs(two, next_experiment="XP-4")
    assert any("not the top-ranked proposal (XP-5)" in e for e in errs)
    assert _ferrs(two, next_experiment="XP-4", extra={"next_reason": "owner asked"}) == []


def test_sync_backlog_follows_the_registry_by_proposal_ref() -> None:
    b = aa.empty_backlog()
    aa.apply_backlog(b, {"findings": [_prop()]}, "r", "t")
    aa.backlog_by_id(b, "XP-1")["status"] = "approved"
    ov = [{"experiment_id": "XP-7", "status": "running", "area": "exits", "proposal_ref": "XP-1"}]
    assert aa.sync_backlog(b, ov, "t2") == ["XP-1: approved -> running (XP-7 running)"]
    assert aa.sync_backlog(b, ov, "t3") == []
    # a stopped earlier run citing the same P-<n> does not pull it back to concluded
    old = {"experiment_id": "XP-6", "status": "stopped", "area": "exits", "proposal_ref": "XP-1"}
    assert aa.sync_backlog(b, [old, *ov], "t3") == []
    ov[0]["status"] = "promoted"
    aa.sync_backlog(b, ov, "t4")
    assert aa.backlog_by_id(b, "XP-1")["status"] == "concluded"
    assert aa._proposal_ref({"owner_approval": "XP-3"}) == "XP-3"
    assert aa._proposal_ref({"owner_approval": "thread ts 1"}) is None


def _k4_backlog(status: str = "proposed") -> dict[str, Any]:
    """A backlog holding one D86-era four-treatment entry (``XP-1``)."""
    b = aa.empty_backlog()
    four = _spec(arms={"control": {}, "treatments": {
        f"t{i}": _overlay(v) for i, v in enumerate((0.3, 0.4, 0.6, 0.75), 1)}})  # fmt: skip
    aa.apply_backlog(b, {"findings": [_prop(spec=four)]}, "r0", "t0")
    aa.backlog_by_id(b, "XP-1")["status"] = status
    return b


def test_d108_constants() -> None:
    assert aa.K_TREATMENTS == 3 and aa.TREATMENT_NAMES == ("t1", "t2", "t3")
    assert aa.ARMS_PER_XP == 4 and aa.PROPOSAL_SPEC_VERSION == 3
    assert aa.PLAN_VIEW == "plan-k3"
    # capacity counts control as an arm from spec v3; v2 (D86) did not
    assert aa._treatment_count(_spec()) == 3
    assert aa._treatment_count({**_spec(), "spec_version": 3}) == 4
    assert aa._treatment_count({"arms": {"treatment": {}}}) == 1


@pytest.mark.parametrize("status", ["proposed", "approved"])
def test_k4_backlog_entry_is_flagged_needs_rewrite_never_trimmed(status: str) -> None:
    b = _k4_backlog(status)
    lines = aa.flag_needs_rewrite(b, "t1")
    e = aa.backlog_by_id(b, "XP-1")
    assert lines == [f"XP-1: needs_rewrite ({e['needs_rewrite']['reason']})"]
    assert "4 treatment(s) (t1, t2, t3, t4)" in e["needs_rewrite"]["reason"]
    assert sorted(e["spec"]["arms"]["treatments"]) == ["t1", "t2", "t3", "t4"]  # untouched
    assert e["status"] == status and e["history"][-1]["event"] == "needs_rewrite"
    assert aa.flag_needs_rewrite(b, "t2") == []  # idempotent
    assert aa.ranked_backlog(b) == []
    assert any("NEEDS REWRITE, not ranked" in ln for ln in aa.backlog_lines(b, [], 10))
    # registered/running/concluded entries are history: never flagged
    done = _k4_backlog("running")
    assert aa.flag_needs_rewrite(done, "t1") == []


def test_k4_entry_cannot_be_next_experiment_until_rewritten() -> None:
    b = _k4_backlog("approved")
    aa.flag_needs_rewrite(b, "t1")
    errs = _errs([], backlog=b, next_experiment="XP-1")
    assert any("XP-1 needs a rewrite (D108" in e for e in errs), errs
    # re-proposing the same key with 3 treatments clears the flag (frozen-spec rule waived),
    # and the old K=4 approval does not carry over
    assert _errs([_prop()], backlog=b, next_experiment="XP-1") == []
    aa.apply_backlog(b, {"findings": [_prop()]}, "r1", "t2")
    e = aa.backlog_by_id(b, "XP-1")
    assert "needs_rewrite" not in e and e["status"] == "proposed"
    assert sorted(e["spec"]["arms"]["treatments"]) == ["t1", "t2", "t3"]
    assert e["history"][-1]["event"] == "rewritten"
    assert [x["id"] for x in aa.ranked_backlog(b)] == ["XP-1"]


def test_approve_refuses_a_k4_entry(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # a repo with no committed specs: XP-1 is free (the real repo's xp1 A/A would move it)
    p = aa.Paths(**{**_paths(tmp_path, tmp_path / "unused.db").__dict__, "repo": tmp_path})
    aa.save_json(p.backlog, _k4_backlog("proposed"))
    approval = ["--approval", "slack:1791668051.305679"]
    assert aa.main(["approve", "XP-1", "go", *approval], p, NOW) == 2
    assert "XP-1 needs a rewrite before approval" in capsys.readouterr().out
    e = aa.backlog_by_id(aa.load_backlog(p.backlog), "XP-1")
    assert e["status"] == "proposed" and e["needs_rewrite"]
    assert not (p.home / "proposals" / "XP-1.yaml").exists()
    assert aa.main(["backlog"], p, NOW) == 0
    assert "XP-1 [proposed] NEEDS REWRITE (D108: 3 treatments)" in capsys.readouterr().out


def test_v3_report_lines_show_verdicts_vs_control_and_production_as_context() -> None:
    ci = {"estimate": 0.001, "lo": -0.001, "hi": 0.003, "level": 0.95}
    payload = {
        "reference": "control",
        "pairwise": [
            {"kind": "control_vs_treatment", "a": "control", "b": f"t{i}", "n": 20,
             "mean": 0.001, "ci": ci, "holm_p": 0.4, "verdict": "continue"}
            for i in (1, 2, 3)
        ] + [{"kind": "treatment_vs_treatment", "a": "t1", "b": "t2"}],
        "production": {
            "cum_p_pnl": 120.0, "cum_c_pnl": 80.0, "cum_d_p_c": 0.0016,
            "cum_start_book": 0.001, "cum_residual": 0.0006, "ramped_from": None,
            "start_book": {"ids": ["s1", "s2"], "close_dates": {"s1": "2026-10-10", "s2": None}},
            "compare": [
                {"arm": "t1", "basis": "total", "n": 20, "mean": -0.001, "ci": ci},
                {"arm": "control", "basis": "total", "n": 20, "mean": -0.002, "ci": ci},
                {"arm": "control", "basis": "post_ramp", "n": 0, "mean": None, "ci": None},
            ],
        },
    }  # fmt: skip
    v = aa._verdict_pair_lines(payload)
    assert v[0] == "- verdicts (Ti vs C, C = control arm; Holm across K=3):"
    assert v[1].startswith("  - t1 vs control: CONTINUE n=20 mean +0.100%/day")
    assert len(v) == 4  # Ti vs Tj is descriptive, not listed as a verdict
    prod = aa._production_lines(payload)
    assert prod[0].startswith("- production panel (DESCRIPTIVE, never a winner")
    assert "start book 2 structures, 1 still open | ramped from not yet" in prod[0]
    assert "P - C +0.160% = start book +0.100% + residual +0.060%" in prod[0]
    assert [ln.split(" (")[0] for ln in prod[1:]] == ["  - t1 - P", "  - control - P"]
    assert "post_ramp" in prod[2]
    # a v2 report: no production panel; C named as production
    v2 = {"pairwise": payload["pairwise"][:1]}
    assert aa._production_lines(v2) == []
    assert "C = control (production, v2)" in aa._verdict_pair_lines(v2)[0]


def test_regime_panel_lines_render_buckets_and_starts(tmp_path: Path) -> None:
    """E25.5 (D108): the pooled regime panel as compact, descriptive context lines."""
    ci = {"estimate": 0.001, "lo": -0.001, "hi": 0.003, "level": 0.95}
    mc = {"n": 4, "mean": 0.001, "ci": ci}
    panel = {
        "panel_version": 1, "descriptive": True, "thin_n": 10, "xps": ["XP-20", "XP-21"],
        "sessions": 16, "unknown_sessions": 1,
        "buckets": [
            {"grouping": "trend", "key": "bull", "n": 12, "thin": False, "xps": ["XP-20"],
             "d_p_c": mc, "residual": mc, "ti_minus_c": {"all": mc}},
            {"grouping": "trend", "key": "unknown", "n": 1, "thin": True, "xps": ["XP-21"],
             "d_p_c": {"n": 1, "mean": -0.002, "ci": None}, "residual": mc,
             "ti_minus_c": {}},
        ],
        "starts": [{"experiment_id": "XP-20", "t0": "2026-10-05T09:00:00-04:00",
                    "start_regime": "bull/low", "sessions": 12, "cum_d_p_c": 0.0072,
                    "cum_residual": 0.0072, "ramped_from": None, "status_verdict": "win"}],
    }  # fmt: skip
    lines = aa.regime_panel_lines(json.dumps(panel), tmp_path)
    assert lines[0] == "- XPs XP-20, XP-21; sessions 16; untagged (unknown) 1; thin = n < 10"
    assert lines[1] == (
        "- trend=bull: n 12 P-C +0.100% [-0.100%, +0.300%] residual +0.100% "
        "[-0.100%, +0.300%] Ti-C(all) +0.100% [-0.100%, +0.300%]"
    )
    assert lines[2].startswith("- trend=unknown: n 1 THIN P-C -0.200% residual")
    assert lines[2].endswith("Ti-C(all) n/a")
    assert lines[3] == (
        "- start XP-20 t0 2026-10-05 regime bull/low: 12 sessions, cum P-C +0.720%, "
        "residual +0.720%, ramped not yet [win]"
    )
    assert (tmp_path / "regime-buckets.log").read_text().count("trend=") == 2
    empty = {**panel, "sessions": 0, "buckets": [], "starts": []}
    assert aa.regime_panel_lines(json.dumps(empty), tmp_path) == ["- no v3 sessions yet"]
    assert aa.regime_panel_lines("(exit 2; boom)", tmp_path)[0].startswith("- unavailable:")
    assert aa.regime_panel_lines(json.dumps({"panel_version": 9}), tmp_path) == [
        "- unsupported regime panel shape (want v1)"
    ]


@pytest.fixture(scope="module")
def _experiment_db(_pipeline_db: bytes) -> bytes:
    from tests import experiment_fixtures as fx

    conn = _conn(_pipeline_db)
    fx.reviewer_registry(conn)
    return conn.serialize()


@pytest.mark.usefixtures("_stub_views")
def test_context_has_backlog_and_record_reserves_xp_ids(
    _experiment_db: bytes, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    conn = _conn(_experiment_db)
    _close_one(conn, NOW - _dt.timedelta(days=2))
    db = _to_disk(conn, tmp_path / "live.db")
    p = _paths(tmp_path, db)
    assert aa.main([], p, NOW) == 0
    out = capsys.readouterr().out
    assert "## Forward experiments (D44; next free XP id XP-4 (D121:" in out
    assert "### XP-1 [stopped (futility)] aa/other" in out
    assert "- stop: sessions 10 sigma" in out
    assert "verdict FUTILITY" in out and "always-valid CI [" in out
    assert "- arm control: P&L" in out and "- arm treatment: P&L" in out
    assert "### XP-2 [running] ab/exits" in out and "- latest report: none stored yet" in out
    assert '- treatment overlay: {"exits": {"default": {"take_profit_pct": 0.4}}}' in out
    assert "- exits: holder XP-2 | queued XP-3" in out
    assert "- max_parallel_arms 10: 1 arm(s) registered/running (XP-2=1)" in out
    assert "- exits: area exits held by XP-2, XP-3" in out
    run_dir = _run_dir(p)
    metrics = aa.load_json(run_dir / "metrics.json", {})
    assert [e["id"] for e in metrics["experiments"]] == ["XP-1", "XP-2", "XP-3"]
    assert metrics["max_parallel_arms"] == 10
    # E7.15: no ledger under the test's backtest root -> no studies; record resolves BT-<n>
    # refs against this snapshot, so give it the BT-3 holdout the proposal cites
    assert metrics["backtest"] == {"studies": [], "next_study_id": "BT-1"}
    metrics["backtest"]["studies"] = BT3
    aa.save_json(run_dir / "metrics.json", metrics)
    # record: the registry snapshot is enforced; the proposal reserves XP-4 (D121: above
    # the registry's XP-1..3 and the committed xp1 spec)
    doc = {"verdict": "findings", "themes": _themes(), "findings": [_prop()], "resolved": [],
           "experiments": LINES, "quick_changes_none": "n < 20 in every bucket",
           "next_experiment": "XP-4", "next_slot": "after XP-2, then XP-3 (queued)"}  # fmt: skip
    (run_dir / "findings.json").write_text(json.dumps({**doc, "experiments": LINES[:1]}))
    assert aa.main(["record", str(run_dir)], p, NOW) == 2
    assert "XP-2 (running) has no line" in capsys.readouterr().out
    assert not p.backlog.exists()  # rejection writes nothing
    (run_dir / "findings.json").write_text(json.dumps(doc))
    assert aa.main(["record", str(run_dir)], p, NOW) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["new"] == ["A-1"] and printed["backlog"]["new"] == ["XP-4"]
    backlog = aa.load_backlog(p.backlog)
    assert backlog["version"] == 2 and backlog["next_id"] == 5
    entry = aa.backlog_by_id(backlog, "XP-4")
    assert entry["finding_id"] == "A-1" and entry["status"] == "proposed"
    assert backlog["next_experiment"]["value"] == "XP-4"
    # the owner approves; adopt registers it under XP-4 (queued: exits is held by XP-2)
    from arc.experiments.store import ExperimentStore
    from arc.store.db import connect
    from tests import experiment_fixtures as fx

    live = connect(str(db))
    st = ExperimentStore(live, now=lambda: NOW)
    st.create(
        fx.spec("XP-4"),
        actor="arc.experiments.adopt",
        owner_approval="slack:1.2",
        proposal_ref="XP-4",
    )
    st.register("XP-4", actor="arc.experiments.adopt", owner_approval="slack:1.2",
                proposal_ref="XP-4")  # fmt: skip
    live.commit()
    live.close()
    # the next run's context ranks it with its formula and slot
    aa.main(["reset"], p, NOW)
    capsys.readouterr()
    assert aa.main([], p, NOW) == 0
    capsys.readouterr()
    out = (_run_dir(p) / "context.md").read_text()  # stdout is capped at 30k
    # the registry names proposal_ref XP-4: the backlog entry moves on, same id
    assert "- registry sync: XP-4: proposed -> registered (XP-4 queued)" in out
    assert "- XP-4 [registered] -> XP-4 (in the registry)" in out
    entry = aa.backlog_by_id(aa.load_backlog(p.backlog), "XP-4")
    assert entry["status"] == "registered" and entry["experiment_id"] == "XP-4"
    # a proposed entry is ranked with its formula, slot and three treatments
    b = aa.load_backlog(p.backlog)
    aa.apply_backlog(b, {"findings": [_prop(key="xp-proposal:other")]}, "r", "t")
    aa.save_json(p.backlog, b)
    aa.main(["reset"], p, NOW)
    capsys.readouterr()
    assert aa.main([], p, NOW) == 0
    capsys.readouterr()
    out = (_run_dir(p) / "context.md").read_text()
    assert "- #1 XP-5 [proposed] exits: Take-profit level" in out
    assert "waits: area exits held by XP-2, XP-3" in out
    assert "value 0.000225 = +0.050%/day x share 0.60 x p 0.30 / 40 sessions" in out
    assert '  t3: {"exits": {"default": {"take_profit_pct": 0.75}}}' in out
    assert "  t4:" not in out


def test_owner_approve_drop_reprioritise_backlog(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    p = _paths(tmp_path, tmp_path / "unused.db")
    b = aa.empty_backlog()
    aa.apply_backlog(b, {"findings": [_prop(key="xp-proposal:a"),
                                      _prop(key="xp-proposal:b", p_success=0.6)]},
                     "r", "t", used={1})  # fmt: skip  # XP-1 is the committed A/A spec
    assert [e["id"] for e in b["items"].values()] == ["XP-2", "XP-3"]
    aa.save_json(p.backlog, b)
    assert aa.main(["approve", "XP-9", "--approval", "slack:1791668051.305679"], p, NOW) == 2
    assert "no proposal XP-9" in capsys.readouterr().out
    assert aa.main(["approve", "XP-2", "go"], p, NOW) == 2  # E21.4: the owner's Slack ts
    assert "needs --approval slack:<ts>" in capsys.readouterr().out
    calls: list[tuple[str, Path, str]] = []

    def fake_adopt(paths: Any, pid: str, spec_path: Path, approval: str) -> tuple[int, Any]:
        calls.append((pid, spec_path, approval))
        return 0, {
            "experiment_id": pid, "status": "registered", "area": "exits", "arms": 4,
            "pr_url": "https://github.com/o/r/pull/9", "start_cmd": f"arc experiment start {pid}",
            "queued_behind": [], "queue_reason": None,
        }  # fmt: skip

    monkeypatch.setattr(aa, "adopt_proposal", fake_adopt)
    approval = ["--approval", "slack:1791668051.305679"]
    assert aa.main(["approve", "XP-2", "go", *approval], p, NOW) == 0
    out = capsys.readouterr().out
    spec_path = p.home / "proposals" / "XP-2.yaml"
    assert calls == [("XP-2", spec_path, "slack:1791668051.305679")]
    assert "XP-2 [registered], area exits, 4 arms" in out  # D121: same id, no "->"
    assert "Spec PR: https://github.com/o/r/pull/9" in out
    assert "owner to start: `arc experiment start XP-2`" in out
    spec = yaml.safe_load(spec_path.read_text())
    # D121: the spec carries its reserved id; adopt registers it with --id
    assert spec["id"] == "XP-2" and spec["kind"] == "ab" and spec["spec_version"] == 3
    assert "--id XP-2" in spec_path.read_text()
    assert spec["arms"]["control"] == {"overlay": {}}  # D108: control runs as its own arm
    from arc.experiments.models import ExperimentSpec

    adopted = ExperimentSpec.model_validate(spec)
    assert len(adopted.arms.treatments) == 3 and adopted.control_runs
    b = aa.load_backlog(p.backlog)
    entry = aa.backlog_by_id(b, "XP-2")
    assert entry["status"] == "registered" and entry["experiment_id"] == "XP-2"
    assert aa.main(["approve", "XP-2", *approval], p, NOW) == 2  # already registered
    assert "XP-2 is registered (XP-2); nothing to do" in capsys.readouterr().out
    aa.backlog_by_id(b, "XP-2")["status"] = "approved"  # back to approved for the ranking part
    aa.save_json(p.backlog, b)
    assert aa.main(["reprioritise", "XP-2", "owner wants exits first"], p, NOW) == 0
    assert "XP-2 -> rank #1" in capsys.readouterr().out
    assert aa.main(["reprioritise", "XP-3", "why", "--score", "x"], p, NOW) == 2
    assert aa.main(["drop", "XP-3", ""], p, NOW) == 2
    assert aa.main(["drop", "XP-3", "superseded"], p, NOW) == 0
    assert aa.backlog_by_id(aa.load_backlog(p.backlog), "XP-3")["status"] == "dropped"
    capsys.readouterr()
    assert aa.main(["backlog"], p, NOW) == 0
    out = capsys.readouterr().out
    assert "#1 XP-2 [approved] exits: Take-profit level" in out and "XP-3 [dropped]" in out
    # D121: a dropped number is never reused: the next proposal reserves XP-4
    assert "next proposal id: XP-4 (reserved at record)" in out


def test_owner_commands_route_on_the_id_prefix(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """D121: ``XP-`` acts, a retired ``P-<n>`` replies with the XP id, ``BT-`` is E26.2."""
    p = _paths(tmp_path, tmp_path / "unused.db")
    b = aa.empty_backlog()
    aa.apply_backlog(b, {"findings": [_prop()]}, "r", "t", used={1})
    e = aa.backlog_by_id(b, "XP-2")
    e["legacy_id"] = "P-1"  # what the v1 -> v2 migration leaves on the entry
    aa.save_json(p.backlog, b)
    approval = ["--approval", "slack:1791668051.305679"]
    for verb in (["approve", "P-1", *approval], ["drop", "P-1", "x"], ["reprioritise", "P-1", "x"]):
        assert aa.main(verb, p, NOW) == 2
        assert "P-ids are retired (D121): use XP-2 (P-1 is now XP-2)" in capsys.readouterr().out
    assert aa.main(["approve", "P-7", *approval], p, NOW) == 2
    assert "P-ids are retired (D121): use XP-<n>; no proposal was P-7" in capsys.readouterr().out
    assert aa.main(["approve", "BT-3", *approval], p, NOW) == 2
    assert "backtest approvals are not wired yet (E26.2)" in capsys.readouterr().out
    assert aa.main(["drop", "A-3", "x"], p, NOW) == 2
    assert "is not a proposal id: use XP-<n>" in capsys.readouterr().out
    assert [aa.owner_id_kind(x) for x in ("XP-2", "BT-1", "P-1", "S-1", "XP-0")] == [
        "xp", "bt", "legacy", None, None,
    ]  # fmt: skip
    assert aa.backlog_by_id(aa.load_backlog(p.backlog), "XP-2")["status"] == "proposed"


def test_reservation_skips_ids_taken_anywhere_and_never_reuses() -> None:
    """D121: the next XP id is above the registry, the specs, open xp/<n> PRs, the backlog
    (dropped entries too) and the backlog's high-water mark."""
    b = aa.empty_backlog()
    assert aa.reserve_proposal_id(b, {1, 2, 3}) == "XP-4" and b["next_id"] == 5
    assert aa.reserve_proposal_id(b, {1}) == "XP-5"  # high-water mark: 4 stays used
    b2 = aa.empty_backlog()
    aa.apply_backlog(b2, {"findings": [_prop(key="xp-proposal:a")]}, "r", "t", used={9})
    e = aa.backlog_by_id(b2, "XP-10")
    e["status"] = "dropped"
    b2["next_id"] = 1  # even with the mark lost, the dropped id stays taken
    assert aa.reserve_proposal_id(b2, set()) == "XP-11"
    # an id taken outside the backlog since it was proposed moves the entry on
    b3 = aa.empty_backlog()
    aa.apply_backlog(b3, {"findings": [_prop()]}, "r", "t")
    e = aa.backlog_by_id(b3, "XP-1")
    e["status"] = "approved"
    assert aa.rereserve_collisions(b3, {1}, "t2") == [
        "XP-1 -> XP-2 (its number was taken outside the backlog)"
    ]
    assert e["id"] == "XP-2" and e["previous_ids"] == ["XP-1"] and e["status"] == "proposed"
    assert aa.rereserve_collisions(b3, {1}, "t3") == []
    assert aa.reserve_proposal_id(b3, set()) == "XP-3"  # XP-1 is never handed out again
    # an entry adopted under its own id is the registry's holder, not a collision
    e["experiment_id"], e["status"] = "XP-2", "approved"
    assert aa.rereserve_collisions(b3, {1, 2}, "t4") == []


def test_spec_and_registry_numbers(tmp_path: Path) -> None:
    live = tmp_path / "config" / "experiments" / "live"
    (live / "verdicts").mkdir(parents=True)
    (live / "xp1_aa_k3.yaml").write_text("id: XP-1\n")
    (live / "verdicts" / "XP-6.yaml").write_text("experiment_id: XP-6\n")
    (live / "README.md").write_text("XP-99 is not a spec\n")
    assert aa.spec_numbers(tmp_path) == {1, 6}
    assert aa.spec_numbers(REPO) >= {1}
    assert aa.registry_numbers(tmp_path / "missing.db") == set()
    from arc.experiments.store import ExperimentStore
    from arc.store.db import connect
    from arc.store.migrate import migrate
    from tests import experiment_fixtures as fx

    db = tmp_path / "arc.db"
    c = connect(str(db))
    migrate(c)
    ExperimentStore(c, now=lambda: NOW).create(fx.spec("XP-8"), actor="t", owner_approval="XP-8")
    c.commit()
    c.close()
    assert aa.registry_numbers(db) == {8}
    assert aa.registry_info(db, "XP-8") == ("draft", "XP-8")
    assert aa.registry_info(db, "XP-9") == (None, None)


def test_spec_pr_numbers_reads_open_xp_branches(tmp_path: Path) -> None:
    gh = tmp_path / "gh"
    gh.write_text(
        "#!/bin/sh\n"
        'echo \'[{"headRefName": "xp/7"}, {"headRefName": "xp/9-verdict"}, '
        '{"headRefName": "project-arc/t_1"}]\'\n'
    )
    gh.chmod(0o755)
    p = aa.Paths(**{**_paths(tmp_path, tmp_path / "x.db").__dict__, "gh": (str(gh),)})
    assert aa.spec_pr_numbers(p) == {7, 9}
    bad = aa.Paths(**{**p.__dict__, "gh": (str(tmp_path / "nope"),)})
    assert aa.spec_pr_numbers(bad) == set()
    assert aa.spec_pr_numbers(_paths(tmp_path, tmp_path / "x.db")) == set()  # no gh: skip


def test_v1_backlog_migrates_to_xp_ids(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """D121: a v1 backlog's ``P-<n>`` entries get XP ids (an adopted one keeps its registry
    id), keep ``legacy_id``, and seed / next_experiment follow. Idempotent."""
    v1 = {
        "version": 1, "next_id": 4,
        "items": {
            "xp-proposal:a": {"id": "P-1", "status": "registered", "experiment_id": "XP-2",
                              "spec": _spec(), "history": []},
            "xp-proposal:b": {"id": "P-2", "status": "dropped", "spec": _spec(), "history": []},
            "xp-proposal:c": {"id": "P-3", "status": "proposed", "spec": _spec(), "history": []},
        },
        "seed": {"S-2": {"status": "proposed", "proposal": "P-3"}},
        "next_experiment": {"value": "P-3", "at": "t"},
    }  # fmt: skip
    b = json.loads(json.dumps(v1))
    lines = aa.migrate_backlog(b, used={1, 2}, when="t1")
    assert lines == ["P-1 -> XP-2", "P-2 -> XP-3", "P-3 -> XP-4"]
    assert b["version"] == 2 and b["next_id"] == 5
    c = b["items"]["xp-proposal:c"]
    assert c["id"] == "XP-4" and c["legacy_id"] == "P-3"
    assert c["history"][-1] == {"at": "t1", "event": "migrated", "legacy_id": "P-3",
                                "id": "XP-4", "by": "D121"}  # fmt: skip
    assert b["seed"]["S-2"]["proposal"] == "XP-4" and b["next_experiment"]["value"] == "XP-4"
    assert aa.backlog_by_ref(b, "P-1")["id"] == "XP-2"  # old proposal_ref rows still resolve
    assert aa.migrate_backlog(b, used={1, 2}) == []  # idempotent
    # the CLI migrates on first touch and persists it
    p = _paths(tmp_path, tmp_path / "unused.db")
    aa.save_json(p.backlog, v1)
    assert aa.main(["backlog"], p, NOW) == 0
    out = capsys.readouterr().out
    assert "#1 XP-4 [proposed]" in out and not re.search(r"(?<!X)P-[0-9]", out)
    saved = aa.load_json(p.backlog, {})
    assert saved["version"] == 2
    assert {e["legacy_id"] for e in saved["items"].values()} == {"P-1", "P-2", "P-3"}


def test_ship_then_the_next_run_must_review(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    p = _paths(tmp_path, tmp_path / "unused.db")
    run_dir = p.runs / "r1"
    run_dir.mkdir(parents=True)
    aa.save_json(run_dir / "metrics.json", {"now": aa.iso(NOW), "closed_max_rowid": 7})
    _write_valid_findings(run_dir, [_qc()])
    assert aa.main(["record", str(run_dir)], p, NOW) == 0
    capsys.readouterr()
    assert aa.main(["ship", "A-9"], p, NOW) == 2
    assert aa.main(["ship", "A-1", "sent in Slack"], p, NOW) == 0
    out = capsys.readouterr().out
    assert "apply (owner, in Slack): !arc config set exits.default.take_profit_pct 0.4" in out
    item = aa.load_json(p.ledger, {})["items"]["quick-change:tp-0.4"]
    assert item["status"] == "shipped" and item["shipped_at"] == aa.iso(NOW)
    assert aa.review_lines(aa.load_json(p.ledger, {}))[0].startswith(
        f"- A-1 shipped {aa.iso(NOW)}: exits.default.take_profit_pct 0.5 -> 0.4"
    )
    # `triage ... shipped` is not the way in: only `ship` stamps shipped_at
    assert aa.main(["triage", "A-1", "shipped"], p, NOW) == 2
    run2 = p.runs / "r2"
    run2.mkdir()
    _write_valid_findings(run2, [_qc()])
    assert aa.main(["record", str(run2)], p, NOW) == 2
    assert "quick change A-1 shipped since it was proposed" in capsys.readouterr().out
    doc = json.loads((run2 / "findings.json").read_text())
    doc["quick_change_reviews"] = [{"id": "A-1", "verdict": "keep", "n": 22, "note": "x"}]
    (run2 / "findings.json").write_text(json.dumps(doc))
    assert aa.main(["record", str(run2)], p, NOW) == 2
    assert "needs before and after" in capsys.readouterr().out
    doc["quick_change_reviews"][0].update(before="$4.10 (n=24)", after="$6.30 (n=22)")
    (run2 / "findings.json").write_text(json.dumps(doc))
    assert aa.main(["record", str(run2)], p, NOW) == 0
    assert json.loads(capsys.readouterr().out)["reviews"] == ["A-1 keep"]
    item = aa.load_json(p.ledger, {})["items"]["quick-change:tp-0.4"]
    assert item["status"] == "kept"
    assert aa.review_lines(aa.load_json(p.ledger, {})) == ["- none shipped since the last review"]


def test_seed_leads_disposed_by_proposal_or_drop() -> None:
    ideas, errs = aa.load_seed(ANALYST / aa.IDEA_SEED_FILE)
    assert not errs and ideas
    sids = frozenset(x["id"] for x in ideas)
    b = aa.empty_backlog()
    aa.apply_backlog(b, {"findings": [_prop(seed_ref="S-1")],
                         "seed_drops": [{"seed_id": "S-2", "reason": "no evidence"}]},
                     "r", "t")  # fmt: skip
    lines = aa.seed_lines(ideas, [], b["seed"])
    assert not any(x.startswith(("- S-1 ", "- S-2 ")) for x in lines)
    assert "- (disposed: S-1 proposed, S-2 dropped)" in lines
    both = {"seed_drops": [{"seed_id": "S-1", "reason": "x"}]}
    assert any("both proposed and dropped" in e for e in _errs(
        [_prop(seed_ref="S-1")], seed_ids=sids, extra=both))  # fmt: skip
    assert any("already dropped" in e for e in _errs(
        [], backlog=b, seed_ids=sids, extra={"seed_drops": [{"seed_id": "S-2", "reason": "x"}]}
    ))  # fmt: skip


def test_merged_pr_lines_and_max_arms(tmp_path: Path, _pipeline_db: bytes) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    prs = [{"number": 221, "title": "E21.2: retire", "mergedAt": "2026-10-09T20:00:00Z",
            "body": "Summary\n\nXP-advisory: might deserve an XP on trending velocity\n"},
           {"number": 222, "title": "D88: Movers", "mergedAt": "2026-10-09T21:00:00Z",
            "body": "no lane line"}]  # fmt: skip
    fake = tmp_path / "gh.py"
    fake.write_text(f"import sys\nprint({json.dumps(json.dumps(prs))})\n")
    p = aa.Paths(**{**_paths(tmp_path, tmp_path / "x.db").__dict__,
                    "gh": (sys.executable, str(fake))})  # fmt: skip
    lines = aa.merged_pr_lines(p, "2026-10-04T15:00:00-04:00", run_dir)
    assert lines == [
        "- #221 E21.2: retire (merged 2026-10-09): "
        "XP-advisory: might deserve an XP on trending velocity"
    ]
    assert aa.merged_pr_lines(p, None, run_dir) == ["- skipped (no gh, or first run)"]
    conn = _conn(_pipeline_db)
    assert aa.max_parallel_arms(conn, REPO) == 10
    conn.execute(
        "INSERT INTO config_changes (key, old, new, actor, source, status, direction, at) "
        "VALUES (?, '10', '12', 'U1', 'slack', 'applied', 'riskier', '2026-10-01T00:00:00Z')",
        (aa.MAX_ARMS_KEY,),
    )
    assert aa.max_parallel_arms(conn, REPO) == 12


def test_experiment_plan_effect_grid_matches_stats() -> None:
    """The `arc experiment plan --k 3 --effects` grid the gate reads (D86, K=3 per D108)."""
    from arc.experiments import cli as xcli
    from arc.experiments import stats

    grid = xcli.effect_grid(
        sigma=0.004, effects=[0.0005, 0.002], k=aa.K_TREATMENTS, alpha=0.05, power=0.8,
        min_sessions=20, max_sessions=60, mde=0.001,
    )  # fmt: skip
    tau = stats.mixing_tau(0.004, mde=0.001, min_sessions=20)
    want = stats.sessions_needed(0.004, 0.002, tau=tau, alpha=0.05, power=0.8, k=3)
    assert grid[1] == {"effect": 0.002, "sessions_needed": want, "fits_window": want <= 60}
    k1 = stats.sessions_needed(0.004, 0.002, tau=tau, alpha=0.05, power=0.8, k=1)
    assert want >= k1  # Holm over 3 treatments costs sessions
    with pytest.raises(ValueError, match="positive"):
        xcli._parse_effects("0.001,-1")


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


# ---------------------------------------------------------------------------
# E7.15 (D84/D86): the backtest study framework as evidence
# ---------------------------------------------------------------------------


def _bt_root(tmp_path: Path, *lines: dict[str, Any]) -> Path:
    """A backtest root: ledger + one committed report + a real BT-3 spec on the base config."""
    root = tmp_path / "bt"
    (root / "data" / "backtest").mkdir(parents=True)
    (root / "data" / "backtest" / "ledger.jsonl").write_text(
        "".join(json.dumps(x) + "\n" for x in lines) + "not json\n"
    )
    studies = root / "docs" / "RESEARCH" / "studies"
    studies.mkdir(parents=True)
    (studies / "BT-2.json").write_text(json.dumps(
        {"header": {"hypothesis": "report-only study", "max_variants": 3}}))  # fmt: skip
    specs = root / "config" / "backtest_studies"
    specs.mkdir(parents=True)
    (specs / "BT-3.yaml").write_text(yaml.safe_dump({
        "id": "BT-3", "hypothesis": "take profit 0.4 beats 0.5 on margin", "owner": "owner",
        "created": "2026-10-01", "profiles": ["margin"], "universe_ladder": ["c125", "c500"],
        "arms": {"treatments": {"t1": {"overlay": {"exits": {"default": {
            "take_profit_pct_of_max_gain": 0.4}}}}}},
    }))  # fmt: skip
    return root


def _real_hash(root: Path) -> str:
    from arc.backtest.study import load_study, study_hash

    return study_hash(load_study(root / "config" / "backtest_studies" / "BT-3.yaml"))


TP = {"exits": {"default": {"take_profit_pct_of_max_gain": 0.4}}}


def test_backtest_snapshot_reads_ledger_reports_and_the_spec_hash(tmp_path: Path) -> None:
    root = _bt_root(tmp_path)
    h = _real_hash(root)
    ledger = [
        _bt_line(phase="dev", universe="c125", spec_hash=h, overlay=TP,
                 at="2026-10-01T18:00:00-04:00"),
        _bt_line(spec_hash=h, overlay=TP),
    ]  # fmt: skip
    root = _bt_root(tmp_path / "x", *ledger)
    p = aa.Paths(home=tmp_path / "h", repo=REPO, live_db=tmp_path / "db", arc=ARC_ARGV,
                 backtest_root=root)  # fmt: skip
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    bt = aa.backtest_snapshot(p, run_dir)  # the real `arc backtest study show --json`
    assert bt["errors"] == ["ledger.jsonl:3 is not JSON (Expecting value: line 1 column 1 "
                            "(char 0))"]  # fmt: skip
    assert [s["id"] for s in bt["studies"]] == ["BT-2", "BT-3"]
    assert bt["next_study_id"] == "BT-4"
    bt3 = bt["studies"][1]
    assert bt3["current_hash"] == h and not bt3["hash_changed"]
    assert bt3["variables"] == ["exits.default.take_profit_pct_of_max_gain"]
    assert bt3["rungs"] == ["c125", "c500"] and bt3["holdout"]["passes"]
    assert aa.study_evidence_class(bt3) == "C500 holdout PASS (may back an XP proposal)"
    assert bt["holdouts"] == [f"BT-3:{h[:12]}:2026-10-03T18:00:00-04:00"]
    assert json.loads((run_dir / "study-show-BT-3.log").read_text())["spec_hash"] == h
    # a spec the CLI refuses is named, not fatal
    (root / "config" / "backtest_studies" / "BT-5.yaml").write_text("id: BT-5\n")
    bt = aa.backtest_snapshot(p, run_dir)
    assert any("BT-5: `arc backtest study show --json` failed" in e for e in bt["errors"])


def test_study_lines_name_ladder_verdict_ci_variants_and_evidence_class() -> None:
    dev_only = _studies(_bt_line(sid="BT-1", phase="dev", universe="c125"))
    c125_hold = _studies(_bt_line(sid="BT-2", universe="c125"))
    edited = _studies(_bt_line(), current={"BT-3": {"spec_hash": "c" * 64, "max_variants": 1,
                                                   "variants_tried": 2}})  # fmt: skip
    lines = aa.study_lines(dev_only + c125_hold + BT3 + edited)
    assert lines[0].startswith("- BT-1 (no spec/report text) · arms control=incumbent | "
                               "t1=incumbent +exits.default.take_profit_pct · ladder reached "
                               "c125 (planned ?) · dev c125 2020-01-01..2023-12-31: margin/t1 "
                               "SWITCH candidate diff +1,234 CI [100, 2,400]")  # fmt: skip
    assert "holdout pending" in lines[0] and "hypothesis only (dev, no holdout)" in lines[0]
    assert "hypothesis only (c125 holdout, not C500)" in lines[1]
    assert "(no loadable spec: hash unverifiable)" in lines[1]
    assert "- BT-3 tp 0.4 beats 0.5 · " in lines[2]
    assert "ladder reached c125,c500 (planned c125->c500)" in lines[2]
    assert "HOLDOUT c500 2024-01-01..2026-09-30: margin/t1 SWITCH candidate" in lines[2]
    assert "variants 1/3 · git abc123def456 · 2026-10-03" in lines[2]
    assert lines[2].endswith("C500 holdout PASS (may back an XP proposal)")
    assert "variants 2/1 OVERFIT-WARNING" in lines[3]
    assert "NOT citable (spec edited since the holdout)" in lines[3]
    lose = _studies(_bt_line(switch=False), current={"BT-3": {"spec_hash": "a" * 64}})
    assert "keep control (evidence against)" in aa.study_lines(lose)[0]
    assert aa.study_lines([])[0].startswith("- none yet")


def test_capped_section_keeps_full_list_in_run_dir(tmp_path: Path) -> None:
    lines = [f"- BT-{i} " + "x" * 300 for i in range(1, 30)]
    out = aa.capped_section(lines, aa.BT_SECTION_MAX, tmp_path / "backtest-studies.log")
    assert sum(len(x) + 1 for x in out[:-1]) <= aa.BT_SECTION_MAX
    assert out[-1].startswith("- ...[") and "backtest-studies.log" in out[-1]
    assert (tmp_path / "backtest-studies.log").read_text().count("- BT-") == 29


def test_checkpoints_and_drift_sections(tmp_path: Path) -> None:
    cp = tmp_path / "checkpoints.json"
    assert "absent" in aa.checkpoint_lines(cp)[0]
    cp.write_text(json.dumps({"c125": {"complete_at": "2026-10-12", "sessions": 1700,
                                       "gb": 9.8}}))  # fmt: skip
    assert aa.checkpoint_lines(cp) == [
        "- c125: complete 2026-10-12 · sessions 1700 · 9.8 GB",
        "- c250: incomplete",
        "- c500: incomplete",
    ]
    rec = tmp_path / "reconciliation.json"
    assert aa.drift_lines(rec)[0].startswith("- no reconciliation yet")
    rec.write_text(json.dumps({"generated_at": "2026-10-11", "git_sha": "abc",
        "sim_slippage_frac": 0.25, "rows": [
            {"kind": "debit_vertical", "regime": "risk_on", "n": 12, "entry_pred": 1.0,
             "entry_actual": 1.08, "exit_pred": 1.6, "exit_actual": 1.5,
             "exit_reason_match": 0.75, "pnl_tracking_error": 18.4,
             "pnl_bias": -12.0}]}))  # fmt: skip
    head, row = aa.drift_lines(rec)
    assert "sim slippage_frac 0.25" in head
    assert row == (
        "- debit_vertical · risk_on n=12 LOW (n<20): entry pred 1.00 vs actual 1.08 (+0.08)"
        " · exit pred 1.60 vs actual 1.50 (-0.10) · exit-reason match 75%"
        " · P&L tracking error 18.40 (bias -12.00)"
    )


@pytest.mark.parametrize(
    ("studies", "prop", "needle"),
    [
        ([], _prop(), "BT-3 is not in the study ledger"),
        (_studies(_bt_line(phase="dev", universe="c125"),
                  current={"BT-3": {"spec_hash": "a" * 64}}),
         _prop(), "no holdout run (dev-only results are a hypothesis"),
        (_studies(_bt_line(universe="c125"), current={"BT-3": {"spec_hash": "a" * 64}}),
         _prop(), "holdout ran on c125, not c500"),
        (_studies(_bt_line(), current={"BT-3": {"spec_hash": "c" * 64}}),
         _prop(), "spec hash changed since its holdout (aaaaaaaaaaaa -> cccccccccccc)"),
        (_studies(_bt_line()), _prop(), "no loadable spec file"),
        (_studies(_bt_line(switch=False), current={"BT-3": {"spec_hash": "a" * 64}}),
         _prop(), "holdout verdict is keep-control"),
        (BT3, _prop(spec=_spec(arms={"control": {}, "treatments": {
            f"t{i}": {"overlay": {"ranking": {"rank_by": v}}}
            for i, v in enumerate("abcd", 1)}})), "the proposal varies ['ranking.rank_by']"),
        (BT3, _prop(spec=_spec(backtest_ref="docs/RESEARCH/old.md"), evidence_kind="live",
                    evidence_ref=None, n=25), "must be a BT-<n> study id or 'none'"),
        (BT3, _prop(spec=_spec(backtest_ref="BT-9"), evidence_kind="live", evidence_ref=None,
                    n=25), "BT-9 is not in the study ledger"),
    ],
)  # fmt: skip
def test_bt_ref_rejections(studies: list, prop: dict[str, Any], needle: str) -> None:
    errs = _errs([prop], studies=studies)
    assert any(needle in e for e in errs), errs


def test_bt_ref_accepts_a_c500_holdout_and_grandfathers_legacy_refs() -> None:
    assert _errs([_prop()]) == []
    # a ranker study backs a menu-ranking proposal
    rk = _studies(_bt_line(overlay={}, ranker="managed_net_ev"),
                  current={"BT-3": {"spec_hash": "a" * 64}})  # fmt: skip
    menu = _prop(spec=_spec(area="ranking", arms={"control": {}, "treatments": {
        f"t{i}": {"overlay": {"exits": {"pipeline": {"rank_menu_by": v}}}}
        for i, v in enumerate(("managed_net_ev", "rorc_day", "vrp"), 1)
    }}))  # fmt: skip
    assert _errs([menu], studies=rk) == []
    # a live proposal may cite none, or a known study
    live = {"evidence_kind": "live", "evidence_ref": None, "n": 25}
    assert _errs([_prop(spec=_spec(backtest_ref="none"), **live)]) == []
    assert _errs([_prop(**live)]) == []
    # legacy path: only on a backlog entry that already held it
    old = _prop(spec=_spec(backtest_ref="docs/RESEARCH/e75.md"), **live)
    b = aa.empty_backlog()
    aa.apply_backlog(b, {"findings": [old]}, "r0", "t0")
    assert _errs([old], backlog=b) == []
    assert any("must be a BT-<n>" in e for e in _errs([old]))


def _draft(**over: Any) -> dict[str, Any]:
    d = {"hypothesis": "take profit 0.4 beats 0.5 on margin condors",
         "profiles": ["margin"],
         "arms": {"control": {}, "treatments": {
             "t1": {"overlay": TP},
             "t2": {"overlay": {"exits": {"default": {"take_profit_pct_of_max_gain": 0.3}}}}}},
         **over}  # fmt: skip
    return {k: v for k, v in d.items() if v is not None}


def _theme_finding(**over: Any) -> dict[str, Any]:
    return {
        "key": "theme:tp-level", "title": "take-profit level untested", "severity": "info",
        "category": "theme", "bucket": "kind=iron_condor", "n": 6, "theme": "exit-policy",
        "evidence": "n=6 condors", "recommendation": "test it in a study",
        "action": "no-action", "study_spec": _draft(), **over,
    }  # fmt: skip


@pytest.mark.parametrize(
    ("spec", "needle"),
    [
        (_draft(id="BT-7"), "id is assigned by `record`"),
        (_draft(universe_ladder=["c125"]), "fixed at [c125, c500]"),
        (_draft(decision_rule={"kind": "x"}), "D25 rule is copied verbatim"),
        (_draft(hypothesis=""), "hypothesis is required"),
        (_draft(profiles=["cash"]), "profiles must be"),
        (_draft(color="red"), "unknown keys ['color']"),
        (_draft(primary_metric="vibes"), "primary_metric must be"),
        (_draft(max_variants=0), "max_variants must be an int >= 1"),
        (_draft(period={"dev_end": "2024-06-01"}), "need dev_start <= dev_end < holdout_start"),
        (_draft(period={"holdout_end": "2023-01-01"}), "holdout_end is before"),
        (_draft(period={"dev_end": "soon"}), "must be an ISO date"),
        (_draft(period={"settle_days": -1}), "settle_days must be"),
        (_draft(period={"x": 1}), "holds only"),
        (_draft(arms={"control": {}, "treatments": {}}), "1-3 arms named t1..t3"),
        (_draft(arms={"control": {}, "treatments": {"t4": {"overlay": TP}}}), "t1..t3"),
        (_draft(arms={"control": {}, "treatment": {}}), "control and treatments only"),
        (_draft(arms={"control": {}, "treatments": {"t1": {"overlay": TP, "x": 1}}}),
         "holds only ranker and overlay"),
        (_draft(arms={"control": {}, "treatments": {"t1": {"ranker": "magic"}}}),
         "ranker must be one of"),
        (_draft(arms={"control": {}, "treatments": {"t1": {"overlay": {"gate": {"x": 1}}}}}),
         "not in ('ranking', 'exits', 'costs')"),
        (_draft(arms={"control": {}, "treatments": {"t1": {"overlay": {"exits": {}}}}}),
         "empty overlay target"),
        (_draft(arms={"control": {}, "treatments": {"t1": {"overlay": []}}}),
         "overlay must be a mapping"),
        (_draft(arms={"control": {"overlay": TP}, "treatments": {"t1": {"ranker": "rorc_day"}}}),
         "control is the base config"),
        (_draft(arms={"control": {}, "treatments": {"t1": {}}}), "identical to control"),
        (_draft(arms={"control": {}, "treatments": {"t1": {"overlay": TP},
                                                    "t2": {"overlay": TP}}}), "identical to t1"),
        (_draft(arms={"control": {}, "treatments": {
            "t1": {"overlay": TP}, "t2": {"ranker": "rorc_day"}}}), "ONE-VARIABLE"),
        (_draft(arms={"control": {}, "treatments": {
            "t1": {"overlay": TP, "ranker": "rorc_day"}}}), "ONE-VARIABLE"),
        ("not a dict", "must be an object"),
    ],
)  # fmt: skip
def test_study_spec_rules(spec: Any, needle: str) -> None:
    errs = _errs([_theme_finding(study_spec=spec)])
    assert any(needle in e for e in errs), errs


def test_study_spec_only_on_untested_hypotheses() -> None:
    assert _errs([_theme_finding()]) == []
    assert any("only on" in e for e in _errs([{**_flaw(), "study_spec": _draft()}]))
    tested = {**_prop(), "study_spec": _draft()}
    assert any("with backtest evidence: it is tested" in e for e in _errs([tested]))
    live = {**_prop(evidence_kind="live", evidence_ref=None, n=25,
                    spec=_spec(backtest_ref="none")), "study_spec": _draft()}  # fmt: skip
    assert _errs([live]) == []


def test_record_writes_draft_study_specs_that_load_as_real_specs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from arc.backtest.study import load_study

    p = _paths(tmp_path, tmp_path / "unused.db")
    run_dir = p.runs / "2026-10-11-1500"
    run_dir.mkdir(parents=True)
    aa.save_json(run_dir / "metrics.json", {"now": aa.iso(NOW), "closed_max_rowid": 7,
        "backtest": {"studies": BT3, "next_study_id": "BT-4"}})  # fmt: skip
    ranker = _draft(arms={"control": {"ranker": "credit_width"}, "treatments": {
        "t1": {"ranker": "managed_net_ev"}, "t2": {"ranker": "rorc_day"}}})  # fmt: skip
    _write_valid_findings(run_dir, [
        _theme_finding(),
        _theme_finding(key="data-gap:ranker", category="data-gap", study_spec=ranker),
        {**_theme_finding(), "key": "theme:bad", "study_spec": _draft(id="BT-1")},
    ])  # fmt: skip
    assert aa.main(["record", str(run_dir)], p, NOW) == 2
    assert "id is assigned by `record`" in capsys.readouterr().out
    assert not (run_dir / "study-specs").exists()  # rejection writes nothing
    doc = json.loads((run_dir / "findings.json").read_text())
    doc["findings"].pop()
    (run_dir / "findings.json").write_text(json.dumps(doc))
    assert aa.main(["record", str(run_dir)], p, NOW) == 0
    out = json.loads(capsys.readouterr().out)
    assert [s.split(" (")[0] for s in out["study_specs"]] == ["A-1 -> BT-4", "A-2 -> BT-5"]
    for sid in ("BT-4", "BT-5"):
        path = run_dir / "study-specs" / f"{sid}.yaml"
        text = path.read_text()
        assert text.startswith("# DRAFT study spec by the Arc Analyst")
        spec = load_study(path)  # the real StudySpec + overlay validation on the base config
        assert spec.id == sid and spec.universe_ladder == ["c125", "c500"]
        assert spec.decision_rule.kind == "d25_challenge" and spec.owner.startswith(
            "Arc Analyst A-"
        )
    item = aa.load_json(p.ledger, {})["items"]["theme:tp-level"]
    assert item["study_spec"]["id"] == "BT-4"


def test_study_spec_mirror_tracks_the_model() -> None:
    from arc.backtest.study import STUDY_TARGETS, StudyPeriod, StudySpec
    from arc.scanner.rank import Ranker

    assert set(aa.STUDY_SPEC_KEYS) == set(StudySpec.model_fields)
    assert set(aa.STUDY_PERIOD_KEYS) == set(StudyPeriod.model_fields)
    assert aa.STUDY_TARGETS == STUDY_TARGETS
    assert set(aa.STUDY_RANKERS) == {r.value for r in Ranker}
    assert StudySpec.model_fields["universe_ladder"].get_default(call_default_factory=True) == (
        aa.STUDY_LADDER
    )


@pytest.mark.usefixtures("_stub_views")
def test_new_holdout_verdict_wakes_the_analyst_once(
    _pipeline_db: bytes, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    conn = _conn(_pipeline_db)
    _close_one(conn, NOW - _dt.timedelta(days=30))
    p = _paths(tmp_path, _to_disk(conn, tmp_path / "live.db"))
    aa.save_json(p.state, {"closed_watermark": 10**9, "last_run_at": aa.iso(NOW)})
    later = NOW + _dt.timedelta(days=7)
    _bt_root(tmp_path, _bt_line(phase="dev", universe="c125"))  # dev-only: no wake
    assert aa.main([], p, later) == 0
    assert _last_json(capsys.readouterr().out) == {"wakeAgent": False}
    _bt_root(tmp_path / "y", _bt_line(phase="dev", universe="c125"), _bt_line())
    (tmp_path / "bt" / "data" / "backtest" / "ledger.jsonl").write_text(
        (tmp_path / "y" / "bt" / "data" / "backtest" / "ledger.jsonl").read_text()
    )
    assert aa.main([], p, later) == 0
    out = capsys.readouterr().out
    assert "WAKE: new backtest holdout verdict(s) since the last run: BT-3\n" in out
    # the context carries the study, drift and checkpoint sections
    assert "## Backtest studies (D84; E7.12 ledger data/backtest/ledger.jsonl" in out
    assert "next free id BT-4" in out
    assert "- WARNING: ledger.jsonl:3 is not JSON" in out
    assert "- BT-3 (no spec/report text)" not in out  # the real spec's hypothesis is shown
    assert "- BT-3 take profit 0.4 beats 0.5 on margin · " in out
    assert "NOT citable (spec edited since the holdout)" in out  # fixture hash != real hash
    assert "## Backtest <-> live drift (E7.14" in out and "- no reconciliation yet" in out
    assert "### Data checkpoints (E7.9 checkpoints.json)" in out
    run_dir = _run_dir(p)
    assert (run_dir / "backtest-studies.log").read_text().startswith("- BT-2 report-only study")
    # record + mark store the holdout as seen: the same verdict does not wake again
    meta = aa.load_json(run_dir / "metrics.json", {})
    assert meta["bt_holdouts"] == [f"BT-3:{'a' * 12}:2026-10-03T18:00:00-04:00"]
    _write_valid_findings(run_dir)
    assert aa.main(["record", str(run_dir)], p, later) == 0
    assert aa.main(["mark", str(run_dir)], p, later) == 0
    assert aa.load_json(p.state, {})["bt_holdouts_seen"] == meta["bt_holdouts"]
    capsys.readouterr()
    assert aa.main([], p, later + _dt.timedelta(days=7)) == 0
    assert _last_json(capsys.readouterr().out) == {"wakeAgent": False}
    assert aa.main(["reset"], p, later) == 0
    assert "bt_holdouts_seen" not in aa.load_json(p.state, {})


def test_skill_documents_the_two_tracks_and_the_backlog() -> None:
    text = _skill()
    for rule in (
        "MIN-SAMPLE = 20 (D86)",
        "QUICK CHANGES (D86",
        "XP PROPOSALS (D86",
        "PRIORITISATION",
        "you never register, start, stop or promote",
        "**exactly 3** treatments",
        "value_score = pct_per_day × affected_share × p_success ÷ sessions_needed",
        "xp_backlog.json",
        "`quick_change_reviews`",
        "`next_experiment`",
        "`next_slot`",
        "approve XP-<n>",
        "ship A-<n>",
        "drop XP-<n>",
        "reprioritise XP-<n>",
        # D121: the Slack footer names XP ids; the id is reserved, not registered, until approved
        "`approve XP-<n>` · `drop XP-<n> <why>` · `reprioritise XP-<n> <why>`",
        "The id is *reserved*, not registered",
        "`approve P-<n>` (a retired id) replies with the XP id to use",
        "*Quick changes*",
        "*Quick-change review*",
        "*Experiments*",
        "*Next XP:*",
        "register XP-<n>",
        "No guardrails",
    ):
        assert rule in text, rule
    for gone in ("FORWARD EXPERIMENTS (D44)", "forward-specs", "ONE-VARIABLE"):
        assert gone not in text, gone
    # D121: P-<n> survives only in the retired-id sentences
    stray = [ln for ln in text.splitlines() if re.search(r"(?<![A-Za-z0-9_-])P-<n>", ln)]
    assert all("retired" in ln for ln in stray), stray
    for section in ("Quick changes", "Quick-change review", "Experiments", "Next XP"):
        assert section in aa.REPORT_SECTIONS


def test_skill_documents_the_backtest_study_evidence() -> None:
    text = _skill()
    for rule in (
        "**Cite C500 holdout verdicts; a C125-only or dev-only result is a hypothesis, not\n"
        "   evidence.**",
        "BACKTEST STUDIES (D84, E7.12 study framework; you never run one)",
        "`study_spec`",
        "RUN_DIR/study-specs/BT-<n>.yaml",
        "## Backtest <-> live drift",
        "## Backtest studies (D84)",
        "RUN_DIR/backtest-studies.log",
        "*Backtest*",
        "a new backtest holdout verdict",
    ):
        assert rule in text, rule
    assert "E7.5 run" not in text and "E7.5 harness" not in text
    assert "Backtest" in aa.REPORT_SECTIONS
    prompt = (ANALYST / "prompt.md").read_text()
    assert "cite only C500 holdout verdicts" in prompt and "never run a backtest" in prompt


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
        "Two tracks",
        "STANDING THEMES",
        "OBVIOUS FLAWS",
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
