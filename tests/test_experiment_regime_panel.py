"""E25.5 (D108 (c)): per-session regime tag + regime panel pooled across v3 XPs."""

from __future__ import annotations

import ast
import datetime as dt
import json
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest

from arc.context.ttl import to_db
from arc.experiments import regime_panel as rp
from arc.experiments.evaluate import ExperimentReport, store_report
from arc.experiments.production import (
    ProductionCompare,
    ProductionPanel,
    ProductionSession,
    SessionRegime,
    StartBook,
    parse_regime,
    regime_at,
)
from arc.store.db import connect
from arc.store.migrate import migrate
from arc.utils.calendar import ET, previous_session, session_close
from tests import experiment_fixtures as fx
from tests.experiment_v2_scenarios import GOLDEN_DIR
from tests.test_experiment_production import _book, _conn, _report, _v3

REPO = Path(__file__).resolve().parent.parent
ARC_ARGV = (sys.executable, "-c", "import sys; from arc.cli import main; sys.exit(main())")


def _regime_payload(
    trend: str = "bull",
    vol: str | None = "low",
    *,
    run: int | None = 5,
    margin: float | None = 0.5,
    as_of: str = "2026-10-05",
) -> dict[str, Any]:
    reg: dict[str, Any] = {"as_of": as_of, "current": trend, "stickiness": 0.95}
    if vol is not None:
        reg |= {"model": "v2", "vol_state": vol, "run_length": run, "margin_z": margin}
    return {"ticker": "SPY", "as_of": as_of, "regime": reg}


def _ctx(
    c,  # type: ignore[no-untyped-def]
    valid_from: dt.datetime,
    payload: dict[str, Any],
    *,
    expires: dt.datetime | None = None,
    subject: str = "SPY",
    status: str = "active",
) -> str:
    eid = f"ctx-{uuid.uuid4().hex[:12]}"
    c.execute(
        """INSERT INTO context_entries (id, kind, subject, payload, schema_version,
               produced_by, created_at, valid_from, expires_at, status)
           VALUES (?, 'regime', ?, ?, 6, 'research', ?, ?, ?, ?)""",
        (
            eid,
            subject,
            json.dumps(payload),
            to_db(valid_from),
            to_db(valid_from),
            to_db(expires) if expires else None,
            status,
        ),
    )
    return eid


def _morning(day: dt.date) -> dt.datetime:
    return dt.datetime.combine(day, dt.time(9, 40), tzinfo=ET)


# -- the regime tag ---------------------------------------------------------------


def test_parse_regime_v2_v1_and_garbage() -> None:
    r = parse_regime("e1", _regime_payload("bear", "high", run=2, margin=0.5))
    assert r is not None and (r.trend, r.vol_state, r.transitional) == ("bear", "high", True)
    assert r.run_length == 2 and r.model == "v2" and r.entry_id == "e1"
    # E17.2 reference setting: run 3 / margin z 0.10 (not the shipped-off defaults)
    assert parse_regime("e", _regime_payload(run=3, margin=0.10)).transitional is False  # type: ignore[union-attr]
    assert parse_regime("e", _regime_payload(run=9, margin=0.09)).transitional is True  # type: ignore[union-attr]
    v1 = parse_regime("e", _regime_payload("sideways", None))
    assert v1 is not None and v1.vol_state is None and v1.transitional is None
    assert parse_regime("e", {"regime": {"current": "crab"}}) is None
    assert parse_regime("e", {"regime": "bull"}) is None
    assert parse_regime("e", {}) is None
    odd = _regime_payload(vol="extreme", run=None, margin=float("nan"))
    got = parse_regime("e", odd)
    assert got is not None and got.vol_state is None and got.transitional is None


def test_regime_at_reads_the_entry_valid_at_each_session_close() -> None:
    c = _conn()
    d0, d1, d2, d3 = fx.sessions(4)
    # d0: two reads that morning, the later one wins; it has since expired (status)
    _ctx(c, _morning(d0), _regime_payload("bear"), expires=session_close(d0), status="expired")
    late = _ctx(
        c,
        _morning(d0) + dt.timedelta(hours=1),
        _regime_payload("bull"),
        expires=session_close(d0),
        status="superseded",
    )
    # d1: nothing valid (expired before the close) -> None
    _ctx(c, _morning(d1), _regime_payload("bear"), expires=_morning(d1) + dt.timedelta(hours=1))
    # d2: valid from after the close -> not visible; d3: QQQ only -> None
    _ctx(c, session_close(d2) + dt.timedelta(minutes=1), _regime_payload("bear"))
    _ctx(c, _morning(d3), _regime_payload("bear"), subject="QQQ")
    _ctx(c, _morning(d3), {"regime": {"current": "??"}})  # unreadable -> None
    got = regime_at(c, [d0, d1, d2, d3])
    assert got[d0] is not None and got[d0].trend == "bull" and got[d0].entry_id == late
    assert got[d1] is None and got[d3] is None
    # d2's after-close row is not visible at d2's close (and every earlier row expired);
    # at d3's close it is, but d3's unreadable row is later and wins -> untagged
    assert got[d2] is None


def test_production_sessions_carry_regime_and_control_deployed() -> None:
    c = _conn()
    fx.start(c, _v3(k=1))
    n = 4
    days = _book(
        c, "XP-30", p=[10.0] * n, ctrl=[0.0] * n, treats={"t1": [5.0] * n}, c_open=[0, 1, 1, 0]
    )
    c.execute("PRAGMA foreign_keys = OFF")  # outside a transaction, or it is a no-op
    for d in days[:3]:
        _ctx(c, _morning(d), _regime_payload("sideways", "mid"), expires=session_close(d))
    # control's one structure c-0: max loss 250/contract, 2 contracts, opened day 1,
    # 1 closed day 2 -> held 2 then 1
    c.execute(
        """INSERT INTO proposals (id, candidate_id, proposal_hash, structure_json, thesis,
               quant_json, risk_narrative, sizing_json, expires_at, created_at, arm_id)
           VALUES ('p1', 'c', 'h1', ?, '', '{}', '', '{}', ?, ?, 'XP-30:control')""",
        (json.dumps({"max_loss": "250.0"}), to_db(fx.T0), to_db(fx.T0)),
    )
    for kind, qty, day, h in (("open", 2, days[1], "h1"), ("close", 1, days[2], "hx")):
        c.execute(
            """INSERT INTO executions (proposal_hash, kind, status, token_version, band_lo,
                   band_hi, max_steps, attempts, contracts, filled_qty, fill_price,
                   started_at, finished_at, structure_id, arm_id)
               VALUES (?, ?, 'filled', 'arc2', '1', '1', 4, 1, ?, ?, '1.00', ?, ?, 'c-0',
                       'XP-30:control')""",
            (h, kind, qty, qty, to_db(fx.eod(day) - dt.timedelta(hours=3)),
             to_db(fx.eod(day) - dt.timedelta(hours=2))),
        )  # fmt: skip
    c.commit()
    pp = _report(c, "XP-30", days).production
    assert pp is not None
    tags = [s.regime.trend if s.regime else None for s in pp.sessions]
    assert tags == ["sideways", "sideways", "sideways", None]
    assert pp.regime_at_start is not None and pp.regime_at_start.vol_state == "mid"
    dep = [s.c_deployed for s in pp.sessions]
    assert dep[0] == 0.0 and dep[3] == 0.0
    assert dep[1] == pytest.approx(500.0 / fx.T0_EQUITY)
    assert dep[2] == pytest.approx(250.0 / fx.T0_EQUITY)


def test_deployed_unknown_when_a_held_structure_has_no_max_loss() -> None:
    c = _conn()
    fx.start(c, _v3(k=1))
    days = _book(c, "XP-30", p=[1.0] * 2, ctrl=[0.0] * 2, treats={"t1": [0.0] * 2}, c_open=[1, 1])
    pp = _report(c, "XP-30", days).production
    assert pp is not None and [s.c_deployed for s in pp.sessions] == [None, None]
    assert [s.c_open for s in pp.sessions] == [1, 1]


def test_regime_tag_never_moves_a_verdict() -> None:
    """Same arms, with and without regime rows: only the production panel differs."""
    reps = []
    for tagged in (False, True):
        c = _conn()
        fx.start(c, _v3(k=2, max_sessions=40))
        n = 12
        days = _book(
            c,
            "XP-30",
            p=[30.0 * (i % 3) for i in range(n)],
            ctrl=[10.0 * (i % 2) for i in range(n)],
            treats={"t1": [200.0] * n, "t2": [-5.0 * (i % 4) for i in range(n)]},
        )
        if tagged:
            for i, d in enumerate(days):
                _ctx(c, _morning(d), _regime_payload("bull" if i % 2 else "bear", "high"))
            c.commit()
        reps.append(_report(c, "XP-30", days))
    a, b = reps
    excl = {"production", "evaluated_at"}
    assert a.model_dump(mode="json", exclude=excl) == b.model_dump(mode="json", exclude=excl)
    assert a.production is not None and b.production is not None
    assert all(s.regime is None for s in a.production.sessions)
    assert all(s.regime is not None for s in b.production.sessions)


def test_verdict_path_never_imports_the_regime_panel() -> None:
    """D108: the regime study is descriptive. No verdict/stop/promotion module imports it."""
    for mod in ("evaluate", "multi", "verdicts", "autostart", "adopt", "stats", "runner",
                "production"):  # fmt: skip
        tree = ast.parse((REPO / "arc" / "experiments" / f"{mod}.py").read_text())
        names = {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)} | {
            a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names
        }
        assert not any("regime_panel" in x for x in names), mod


# -- pooling (synthetic multi-XP reports) ------------------------------------------


def _reg(trend: str, vol: str | None = "low", trans: bool | None = False) -> SessionRegime:
    return SessionRegime(trend=trend, vol_state=vol, transitional=trans, entry_id="x")  # type: ignore[arg-type]


def _session(
    day: dt.date, *, p: float, c: float, ti: dict[str, float], reg: SessionRegime | None,
    sb: float = 0.0, c_open: int | None = 0, c_dep: float | None = 0.0,
) -> ProductionSession:  # fmt: skip
    t0 = fx.T0_EQUITY
    return ProductionSession(
        day=day, p_pnl=p, p_start_book_pnl=sb, p_ex_start_pnl=p - sb, c_pnl=c, ti_pnl=ti,
        d_p_c=sb / t0 + (p - sb - c) / t0, start_book=sb / t0, residual=(p - sb - c) / t0,
        p_open=3, c_open=c_open, ramped=False, c_deployed=c_dep, regime=reg,
    )  # fmt: skip


def _xp(
    eid: str, t0: dt.datetime, sessions: list[ProductionSession], *, verdict: str = "continue"
) -> ExperimentReport:
    base = ExperimentReport.model_validate_json((GOLDEN_DIR / "ab_k3_with_legacy.json").read_text())
    panel = ProductionPanel(
        sessions=sessions,
        cum_p_pnl=sum(s.p_pnl for s in sessions),
        cum_c_pnl=sum(s.c_pnl for s in sessions),
        cum_d_p_c=sum(s.d_p_c for s in sessions),
        cum_start_book=sum(s.start_book for s in sessions),
        cum_residual=sum(s.residual for s in sessions),
        start_book=StartBook(ids=[]),
        compare=[ProductionCompare(arm="control", basis="total", n=0, mean=None)],
        regime_at_start=sessions[0].regime if sessions else None,
    )
    return base.model_copy(
        update={
            "experiment_id": eid, "t0": t0, "t0_equity": fx.T0_EQUITY, "report_version": 3,
            "reference": "control", "production": panel, "verdict": verdict,
        }
    )  # fmt: skip


def _three_xps() -> list[ExperimentReport]:
    d1 = fx.sessions(12)
    d2 = fx.sessions(5, start=dt.date(2026, 11, 2))
    d3 = fx.sessions(3, start=dt.date(2026, 12, 1))
    bull, bear = _reg("bull", "low"), _reg("bear", "high", trans=True)
    xp1 = _xp("XP-20", fx.T0, [
        _session(d, p=100.0, c=40.0, ti={"t1": 60.0, "t2": 20.0}, reg=bull,
                 c_open=i, c_dep=0.01 * i)
        for i, d in enumerate(d1)
    ], verdict="win")  # fmt: skip
    xp2 = _xp("XP-21", dt.datetime(2026, 11, 2, 9, tzinfo=ET), [
        _session(d, p=-50.0, c=0.0, ti={"t1": -10.0}, reg=bear if i < 4 else None,
                 sb=-20.0, c_open=2 * i, c_dep=None if i == 0 else 0.02)
        for i, d in enumerate(d2)
    ])  # fmt: skip
    xp3 = _xp("XP-22", dt.datetime(2026, 12, 1, 9, tzinfo=ET), [
        _session(d, p=0.0, c=0.0, ti={}, reg=_reg("sideways", None, None)) for d in d3
    ])  # fmt: skip
    return [xp3, xp1, xp2]  # unsorted on purpose


def test_pooling_by_regime_across_xps() -> None:
    panel = rp.build_regime_panel(_three_xps())
    assert panel.descriptive is True and panel.panel_version == 1
    assert panel.xps == ["XP-20", "XP-21", "XP-22"]  # by t0
    assert panel.sessions == 12 + 5 + 3 and panel.unknown_sessions == 1
    b = {(x.grouping, x.key): x for x in panel.buckets}
    bull = b[("trend", "bull")]
    assert bull.n == 12 and not bull.thin and bull.xps == ["XP-20"]
    assert bull.d_p_c.mean == pytest.approx(60.0 / fx.T0_EQUITY)
    assert bull.d_p_c.ci is not None and bull.d_p_c.ci.lo == pytest.approx(bull.d_p_c.mean)
    assert bull.ti_minus_c["XP-20:t1"].mean == pytest.approx(20.0 / fx.T0_EQUITY)
    assert bull.ti_minus_c["XP-20:t2"].mean == pytest.approx(-20.0 / fx.T0_EQUITY)
    assert bull.ti_minus_c["all"].n == 24 and bull.ti_minus_c["all"].mean == pytest.approx(0.0)
    assert list(bull.ti_minus_c)[0] == "all"
    bear = b[("trend", "bear")]
    assert bear.n == 4 and bear.thin
    assert bear.d_p_c.mean == pytest.approx(-50.0 / fx.T0_EQUITY)
    assert bear.residual.mean == pytest.approx(-30.0 / fx.T0_EQUITY)  # ex start book
    unknown = b[("trend", rp.UNKNOWN)]
    assert unknown.n == 1 and unknown.thin and unknown.d_p_c.ci is None  # counted, not dropped
    assert b[("vol", "na")].n == 3 and b[("trend_vol", "sideways/na")].n == 3
    assert b[("transitional", "transitional")].n == 4
    assert b[("transitional", "confirmed")].n == 12
    assert b[("transitional", "na")].n == 3
    # every grouping covers every session exactly once
    for g in rp.GROUPINGS:
        assert sum(x.n for x in panel.buckets if x.grouping == g) == panel.sessions
    # order: grouping, then key, unknown last
    keys = [x.key for x in panel.buckets if x.grouping == "trend"]
    assert keys == ["bear", "bull", "sideways", rp.UNKNOWN]


def test_start_date_rows_and_ramp_curves() -> None:
    panel = rp.build_regime_panel(_three_xps())
    starts = {s.experiment_id: s for s in panel.starts}
    assert starts["XP-20"].start_regime == "bull/low" and starts["XP-20"].status_verdict == "win"
    assert starts["XP-21"].start_regime == "bear/high"
    assert starts["XP-22"].start_regime == "sideways/na"
    assert starts["XP-21"].cum_d_p_c == pytest.approx(5 * -50.0 / fx.T0_EQUITY)
    assert starts["XP-21"].first_session == dt.date(2026, 11, 2)
    ramp = panel.ramp_by_start_regime["bear/high"]
    assert [p.c_open for p in ramp] == [0, 2, 4, 6, 8]
    assert ramp[0].c_deployed is None and ramp[1].c_deployed == pytest.approx(0.02)
    assert panel.ramp_by_start_regime["bull/low"][11].c_deployed == pytest.approx(0.11)
    assert [p.n_xps for p in ramp] == [1] * 5


def test_ramp_mean_over_xps_with_the_same_start_regime() -> None:
    d = fx.sessions(3)
    bull = _reg("bull")
    a = _xp(
        "XP-1", fx.T0, [_session(x, p=0, c=0, ti={}, reg=bull, c_open=i) for i, x in enumerate(d)]
    )
    b = _xp(
        "XP-2",
        fx.T0 + dt.timedelta(days=1),
        [_session(x, p=0, c=0, ti={}, reg=bull, c_open=3 * i) for i, x in enumerate(d[:2])],
    )
    ramp = rp.build_regime_panel([a, b]).ramp_by_start_regime["bull/low"]
    assert [(p.n_xps, p.c_open) for p in ramp] == [(2, 0.0), (2, 2.0), (1, 2.0)]


def test_thin_threshold_and_non_v3_reports_skipped() -> None:
    d = fx.sessions(10)
    ten = _xp("XP-1", fx.T0, [_session(x, p=1, c=0, ti={}, reg=_reg("bull")) for x in d])
    v2 = ten.model_copy(update={"experiment_id": "XP-9", "report_version": 2})
    no_panel = ten.model_copy(update={"experiment_id": "XP-8", "production": None})
    panel = rp.build_regime_panel([ten, v2, no_panel])
    assert panel.xps == ["XP-1"]
    bull = next(x for x in panel.buckets if (x.grouping, x.key) == ("trend", "bull"))
    assert bull.n == rp.THIN_N and not bull.thin


def test_empty_panel_text_and_json() -> None:
    panel = rp.build_regime_panel([])
    assert rp.panel_lines(panel) == ["no v3 sessions yet"]
    assert json.loads(rp.panel_json(panel))["sessions"] == 0


def test_panel_text_marks_thin_and_unknown() -> None:
    got: list[str] = []
    lines = rp.panel_lines(rp.build_regime_panel(_three_xps()), out=got.append)
    assert got == lines
    text = "\n".join(lines)
    assert "descriptive only: never a verdict, stop or promotion input" in text
    assert "untagged (unknown) 1" in text
    assert "by trend:" in text and "by transitional:" in text
    bear = next(x for x in lines if x.strip().startswith("bear ") and "thin" in x)
    assert "n   4 thin" in bear
    assert any(x.strip().startswith("unknown") for x in lines)
    assert "XP-20   2026-10-05 bull/low" in text and "[win]" in text
    assert "control ramp by regime at start" in text


# -- store + CLI (read-only) --------------------------------------------------------


def _store_with_reports(path: Path) -> None:
    c = connect(str(path))
    migrate(c)
    for rep in _three_xps():
        store_report(c, rep)
    # a later v2 report for XP-21 replaces its v3 one as "latest"
    xp21 = next(r for r in _three_xps() if r.experiment_id == "XP-21")
    store_report(c, xp21.model_copy(update={"report_version": 2, "production": None}))
    c.commit()
    c.close()


def test_load_v3_reports_takes_each_experiments_latest(tmp_path: Path) -> None:
    db = tmp_path / "arc.db"
    _store_with_reports(db)
    c = connect(str(db))
    try:
        assert [r.experiment_id for r in rp.load_v3_reports(c)] == ["XP-22", "XP-20"]
    finally:
        c.close()
    bare = connect(":memory:")
    assert rp.load_v3_reports(bare) == []


def _cli(*args: str) -> subprocess.CompletedProcess[str]:
    env = {"ARC_ENV": "paper", "PATH": "/usr/bin:/bin", "HOME": str(REPO)}
    return subprocess.run(  # noqa: S603 - fixed argv
        [*ARC_ARGV, "experiment", "regime-panel", *args],
        cwd=REPO, env=env, capture_output=True, text=True, check=False, timeout=120,
    )  # fmt: skip


def test_cli_regime_panel_reads_mode_ro(tmp_path: Path) -> None:
    db = tmp_path / "arc.db"
    _store_with_reports(db)
    before = db.read_bytes()
    out = _cli("--db", str(db))
    assert out.returncode == 0, out.stderr
    assert "XPs 2 (XP-20, XP-22)" in out.stdout
    js = _cli("--db", str(db), "--json")
    assert js.returncode == 0 and json.loads(js.stdout)["xps"] == ["XP-20", "XP-22"]
    assert db.read_bytes() == before  # never written, migrated or identity-stamped


def test_cli_regime_panel_missing_store_never_creates_one(tmp_path: Path) -> None:
    db = tmp_path / "nope.db"
    out = _cli("--db", str(db))
    assert out.returncode == 2 and "never create" in out.stderr and not db.exists()


def test_cli_regime_panel_empty_store(tmp_path: Path) -> None:
    db = tmp_path / "arc.db"
    c = connect(str(db))
    migrate(c)
    c.close()
    out = _cli("--db", str(db))
    assert out.returncode == 0 and out.stdout.strip().endswith("no v3 sessions yet")


def test_previous_session_fixture_sanity() -> None:
    assert previous_session(fx.sessions(1)[0]) < fx.T0.date()
