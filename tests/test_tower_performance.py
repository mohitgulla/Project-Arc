"""E8.7c Performance page: /api/performance and /api/performance/breakdown.

Every metric is pinned to the function the weekly scorecard (E7.3) / reconciler uses, on
the fixture DB with the performance history (``scripts/tower_fixture_db.py --history``:
~40 closed trades over 3+ months, D19 shadows, reviews, a broker smoke-test trade).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from arc.journal.attribution import calibration
from arc.journal.scorecard import (
    build_scorecard,
    calibration_points,
    closed_positions,
    execution_costs,
    funnel,
    model_vs_realised,
)
from arc.journal.tradestats import trade_stats
from arc.reconcile.performance import daily_equity, daily_returns, drawdown, sharpe, sortino
from arc.store.db import connect
from arc.store.migrate import migrate
from arc.tower.api import create_app
from arc.tower.data import connect_ro
from arc.tower.data_performance import (
    BREAKDOWNS,
    _current_persona,
    comparison_period,
    load_breakdown,
    load_performance,
    resolve_period,
    smoke_test_hashes,
)
from arc.tower.routes.performance import ResponseCache
from arc.utils.calendar import ET

REPO = Path(__file__).resolve().parents[1]
NOW = dt.datetime(2026, 9, 28, 15, 40, tzinfo=ET)
TODAY = NOW.date()


def _load(name: str, path: Path):  # noqa: ANN202 - module loaded by path
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(name, mod)
    spec.loader.exec_module(mod)
    return mod


fixture = _load("tower_fixture_db", REPO / "scripts" / "tower_fixture_db.py")


@pytest.fixture(scope="module")
def fx_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return fixture.build(tmp_path_factory.mktemp("perf") / "arc.db", NOW, history=True)


@pytest.fixture
def conn(fx_db: Path):
    c = connect_ro(fx_db)
    yield c
    c.close()


@pytest.fixture
def client(fx_db: Path):
    with TestClient(create_app(fx_db, clock=lambda: NOW)) as c:
        yield c


@pytest.fixture(scope="module")
def perf(fx_db: Path):
    c = connect_ro(fx_db)
    try:
        return load_performance(c, now=NOW, preset="90d", compare="prev")
    finally:
        c.close()


def _window(first: dt.date, last: dt.date) -> tuple[dt.datetime, dt.datetime]:
    return (
        dt.datetime.combine(first, dt.time(), tzinfo=ET),
        dt.datetime.combine(last + dt.timedelta(days=1), dt.time(), tzinfo=ET),
    )


def _non_test(conn: sqlite3.Connection, first: dt.date, last: dt.date):  # noqa: ANN202
    start, end = _window(first, last)
    tests = smoke_test_hashes(conn)
    return [
        c
        for c in closed_positions(conn, start=start, end=end, now=NOW)
        if c.open_proposal_hash not in tests
    ]


# ---------------------------------------------------------------------------
# Periods
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("preset", "first", "slot_end"),
    [
        ("week", dt.date(2026, 9, 28), dt.date(2026, 10, 4)),  # Monday
        ("mtd", dt.date(2026, 9, 1), dt.date(2026, 9, 30)),
        ("qtd", dt.date(2026, 7, 1), dt.date(2026, 9, 30)),
        ("ytd", dt.date(2026, 1, 1), dt.date(2026, 12, 31)),
        ("30d", dt.date(2026, 8, 30), TODAY),
        ("90d", dt.date(2026, 7, 1), TODAY),
        # E8.8c page ranges 1D / 1W (1M = 30d, 3M = 90d)
        ("1d", TODAY, TODAY),
        ("7d", dt.date(2026, 9, 22), TODAY),
    ],
)
def test_presets(preset: str, first: dt.date, slot_end: dt.date) -> None:
    p = resolve_period(preset, TODAY)  # type: ignore[arg-type]
    assert (p.first, p.last, p.slot_end) == (first, TODAY, slot_end)


def test_all_custom_and_comparisons() -> None:
    assert resolve_period("all", TODAY, inception=dt.date(2026, 5, 4)).first == dt.date(2026, 5, 4)
    c = resolve_period("custom", TODAY, date_from=dt.date(2026, 8, 1), date_to=dt.date(2026, 8, 31))
    assert (c.first, c.last, c.days) == (dt.date(2026, 8, 1), dt.date(2026, 8, 31), 31)
    prev = comparison_period(c, "prev")
    assert prev is not None and (prev.first, prev.last) == (
        dt.date(2026, 7, 1),
        dt.date(2026, 7, 31),
    )
    yoy = comparison_period(c, "yoy")
    assert yoy is not None and yoy.first == dt.date(2025, 8, 1)
    assert comparison_period(c, "none") is None
    with pytest.raises(ValueError, match="needs date_from"):
        resolve_period("custom", TODAY)
    with pytest.raises(ValueError, match="after"):
        resolve_period("custom", TODAY, date_from=dt.date(2026, 9, 2), date_to=dt.date(2026, 9, 1))


# ---------------------------------------------------------------------------
# Every card pinned to the scorecard / reconciler function output
# ---------------------------------------------------------------------------


def test_fixture_history_has_what_the_card_lists(conn: sqlite3.Connection) -> None:
    one = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
    assert one("SELECT COUNT(*) FROM open_structures WHERE status='closed'") >= 40
    assert one("SELECT COUNT(*) FROM outcomes WHERE hold_to_expiry_shadow_pnl IS NOT NULL") >= 40
    assert one("SELECT COUNT(*) FROM decision_reviews") >= 10
    days = one("SELECT COUNT(DISTINCT json_extract(details_json, '$.day')) FROM pnl_snapshots")
    assert days >= 70  # 3+ months of weekdays
    assert one("SELECT COUNT(*) FROM orders WHERE client_order_id LIKE 'arc-%'") == 1


def test_win_loss_matches_trade_stats(conn: sqlite3.Connection, perf) -> None:  # noqa: ANN001
    closed = _non_test(conn, perf.period.first, perf.period.last)
    assert len(closed) >= 25
    assert perf.win_loss.stats == trade_stats(closed)
    s = perf.win_loss.stats
    assert s.closed == len(closed) and 0 < s.win_rate < 1  # type: ignore[operator]
    assert s.profit_factor is not None and s.expectancy is not None
    prev = comparison_period(resolve_period("90d", TODAY), "prev")
    assert prev is not None
    assert (
        perf.win_loss.compare_win_rate
        == trade_stats(_non_test(conn, prev.first, prev.last)).win_rate
    )


def test_model_card_matches_model_vs_realised(conn: sqlite3.Connection, perf) -> None:  # noqa: ANN001
    mv = model_vs_realised(_non_test(conn, perf.period.first, perf.period.last))
    m = perf.model
    assert not m.empty and m.n == mv.n > 0
    assert m.sum_managed_ev == pytest.approx(mv.managed_net_ev)
    assert m.sum_static_ev == pytest.approx(mv.static_net_ev)
    assert m.sum_realised == pytest.approx(mv.realised)
    assert m.sum_shadow == pytest.approx(mv.shadow_hold) and m.n_shadow == mv.n_shadow
    assert m.win_rate == mv.win_rate
    assert m.mean_managed_pop == mv.mean_managed_pop and m.mean_static_pop == mv.mean_static_pop
    assert len(m.points) == m.n
    assert m.hold_win_rate is not None


def test_model_vs_realised_agrees_with_the_weekly_scorecard(conn: sqlite3.Connection) -> None:
    """The tower's wrappers are the scorecard's internals: same week, same numbers."""
    start, end = _window(dt.date(2026, 8, 31), dt.date(2026, 9, 6))
    sc = build_scorecard(conn, start=start, end=end, now=NOW)
    closed = closed_positions(conn, start=start, end=end, now=NOW)
    assert sc.closed == closed
    assert sc.model_vs_realised == model_vs_realised(closed)
    assert sc.slippage == execution_costs(conn, start, end)
    f, v = funnel(conn, start, end)
    assert (sc.funnel, sc.gate_violations) == (f, v)


def test_shadow_falls_back_to_recorded_outcomes(conn: sqlite3.Connection, perf) -> None:  # noqa: ANN001
    """Early exits whose expiry is still ahead take the D19 shadow from ``outcomes``."""
    closed = _non_test(conn, perf.period.first, perf.period.last)
    pending = [c for c in closed if c.early and c.expiration >= TODAY]
    early_known = [c for c in closed if c.early and c.shadow_hold_pnl is not None]
    assert early_known  # without the fallback none of the unexpired early exits has one
    for c in pending:
        row = conn.execute(
            "SELECT hold_to_expiry_shadow_pnl FROM outcomes WHERE proposal_hash = ?",
            (c.open_proposal_hash,),
        ).fetchone()
        assert c.shadow_hold_pnl == pytest.approx(float(row[0]))
    assert perf.net_pnl.shadow_known == sum(1 for c in closed if c.shadow_hold_pnl is not None)
    assert perf.net_pnl.shadow_delta == pytest.approx(
        sum(c.shadow_hold_pnl - c.realised_pnl for c in closed if c.shadow_hold_pnl is not None)
    )


def test_costs_match_execution_costs(conn: sqlite3.Connection, perf) -> None:  # noqa: ANN001
    start, end = _window(perf.period.first, perf.period.last)
    tests = smoke_test_hashes(conn)
    rows = [r for r in execution_costs(conn, start, end).rows if r.proposal_hash not in tests]
    c = perf.costs
    assert c.fills == len(rows) > 40
    assert c.commission == pytest.approx(sum(r.commission for r in rows))
    assert c.fees == pytest.approx(sum(r.fees for r in rows))
    # spread + slippage = fill vs mid; spread is the modelled part
    assert c.spread + c.slippage == pytest.approx(sum(r.realised_usd for r in rows))
    assert c.spread == pytest.approx(sum(r.expected_usd or 0.0 for r in rows))
    assert c.total == pytest.approx(c.commission + c.fees + c.spread + c.slippage)
    assert c.cost_pct_of_gross == pytest.approx(c.total / abs(c.gross_pnl))  # type: ignore[arg-type]
    assert c.fees_from_open > 0  # closes priced with their open's fee model
    assert sum(b.commission + b.fees + b.spread + b.slippage for b in c.bars) == pytest.approx(
        c.total
    )


def test_equity_card_matches_the_reconciled_series(conn: sqlite3.Connection, perf) -> None:  # noqa: ANN001
    series = daily_equity(conn)
    first = perf.period.first
    window = [s for s in series if s.day < first][-1:] + [s for s in series if s.day >= first]
    dd = drawdown(window)
    e = perf.equity
    assert e.max_drawdown == pytest.approx(float(dd.amount)) and e.max_drawdown < 0
    assert e.max_drawdown_pct == pytest.approx(dd.pct)
    assert (e.drawdown_peak, e.drawdown_trough) == (dd.peak_day, dd.trough_day)
    assert e.sharpe == pytest.approx(sharpe(daily_returns(window)))
    assert e.sortino is not None
    assert e.sortino == pytest.approx(sortino(daily_returns(window)))
    assert e.start_equity == pytest.approx(float(window[0].equity))
    assert e.end_equity == pytest.approx(float(series[-1].equity))
    assert e.return_pct == pytest.approx(e.end_equity / e.start_equity - 1)
    assert [p.day for p in e.points] == [s.day for s in window]


def test_net_pnl_is_the_equity_change_less_test_legs(conn: sqlite3.Connection, perf) -> None:  # noqa: ANN001
    n = perf.net_pnl
    series = daily_equity(conn)
    first = perf.period.first
    start = [s for s in series if s.day < first][-1].equity
    tests = [
        c
        for c in closed_positions(
            conn, start=_window(first, TODAY)[0], end=_window(first, TODAY)[1], now=NOW
        )
        if c.open_proposal_hash in smoke_test_hashes(conn)
    ]
    assert n.source == "equity" and n.bucket == "week"
    assert n.tests_excluded_pnl == pytest.approx(sum(c.realised_pnl for c in tests)) != 0
    assert n.net == pytest.approx(float(series[-1].equity - start) - n.tests_excluded_pnl)
    assert n.realised == pytest.approx(perf.win_loss.stats.realised)
    assert n.bars[-1].cumulative == pytest.approx(n.net)
    assert sum(b.pnl or 0.0 for b in n.bars) == pytest.approx(n.net)
    assert n.now_label == n.bars[-1].label
    assert n.compare_net is not None and n.change == pytest.approx(n.net - n.compare_net)
    assert n.bars[-1].shadow_cumulative == pytest.approx(n.net + n.shadow_delta)


def test_include_tests_puts_the_smoke_test_back(fx_db: Path) -> None:
    c = connect_ro(fx_db)
    try:
        off = load_performance(c, now=NOW, preset="90d")
        on = load_performance(c, now=NOW, preset="90d", include_tests=True)
    finally:
        c.close()
    assert on.win_loss.stats.closed == off.win_loss.stats.closed + 1
    assert on.costs.fills == off.costs.fills + 2  # its open and close fills
    assert on.net_pnl.tests_excluded_pnl == 0.0
    assert on.net_pnl.net == pytest.approx(off.net_pnl.net + off.net_pnl.tests_excluded_pnl)


def test_page_default_3m_equals_the_old_90d_view(client: TestClient) -> None:
    """E8.8c parity: the page's default request (3M -> ``preset=90d``, no ``compare``, no
    ``include_tests``) returns the same numbers as the pre-E8.8c ``?preset=90d&compare=prev``
    view; only the comparison fields differ (the page no longer asks for them)."""
    new = client.get("/api/performance", params={"preset": "90d"}).json()
    old = client.get("/api/performance", params={"preset": "90d", "compare": "prev"}).json()
    assert new["compare"] == "none" and new["compare_period"] is None
    assert new["include_tests"] is False

    compare_keys = {
        "compare_net",
        "change",
        "compare_total",
        "compare_win_rate",
        "compare_expectancy",
    }

    def strip(body: dict) -> dict:  # type: ignore[type-arg]
        out = {k: v for k, v in body.items() if k not in {"compare", "compare_period"}}
        for card in ("net_pnl", "costs", "win_loss"):
            out[card] = {k: v for k, v in body[card].items() if k not in compare_keys}
        return out

    assert strip(new) == strip(old)
    assert new["win_loss"]["stats"]["closed"] > 0 and new["net_pnl"]["net"] is not None


def test_range_presets_answer(client: TestClient) -> None:
    for preset, days in (("1d", 1), ("7d", 7), ("30d", 30), ("90d", 90)):
        r = client.get("/api/performance", params={"preset": preset})
        assert r.status_code == 200 and r.json()["period"]["days"] == days, preset
    for preset in ("ytd", "all"):
        assert client.get("/api/performance", params={"preset": preset}).status_code == 200


def test_breakdowns(conn: sqlite3.Connection, perf) -> None:  # noqa: ANN001
    closed = _non_test(conn, perf.period.first, perf.period.last)
    b = perf.breakdowns
    for by in ("ticker", "structure", "exit_reason", "profile", "regime"):
        rows = getattr(b, by)
        assert sum(r.count for r in rows) == len(closed), by
        assert sum(r.pnl for r in rows) == pytest.approx(sum(c.realised_pnl for c in closed))
        assert [r.pnl for r in rows] == sorted((r.pnl for r in rows), reverse=True)
    assert {r.key for r in b.structure} >= {"vertical_debit", "long_call", "long_put"}
    assert {r.key for r in b.regime} == {"bull", "chop", "bear"} | (
        {""} if any(r.key == "" for r in b.regime) else set()
    )
    assert {r.key for r in b.profile} >= {"cash_debit", "cash_long_only"}
    assert "exit:take_profit" in {r.key for r in b.reason_code}
    top = b.ticker[0]
    assert top.filter == {"ticker": top.key, "stage": "closed"}
    assert next(r for r in b.structure if r.key == "long_call").label == "Long Call"


def test_breakdown_route_equals_the_tab(conn: sqlite3.Connection, perf) -> None:  # noqa: ANN001
    for by in BREAKDOWNS:
        r = load_breakdown(conn, now=NOW, by=by, preset="90d")
        assert r.rows == getattr(perf.breakdowns, by), by


def test_calibration_matches_the_scorecard(conn: sqlite3.Connection, perf) -> None:  # noqa: ANN001
    start, end = _window(perf.period.first, perf.period.last)
    tests = smoke_test_hashes(conn)
    history = [
        c
        for c in closed_positions(conn, start=start, end=end, now=NOW, all_time=True)
        if c.open_proposal_hash not in tests
    ]
    buckets = calibration(
        calibration_points(conn, [(c.open_proposal_hash, c.realised_pnl > 0) for c in history])
    )
    cal = perf.calibration
    assert not cal.empty and cal.trades == len(history)
    # E13.14: the scorecard's quant_pop series is served as persona quant, stated "pop";
    # legacy persona names map to their current key (arc.journal.legacy).
    assert [(r.persona, r.stated, r.lo, r.n, r.hit_rate) for r in cal.rows] == [
        (
            "quant" if b.persona == "quant_pop" else _current_persona(b.persona),
            "pop" if b.persona == "quant_pop" else "confidence",
            b.lo,
            b.n,
            b.hit_rate,
        )
        for b in buckets
    ]
    stated = {(r.persona, r.stated) for r in cal.rows}
    assert stated >= {("research", "confidence"), ("quant", "pop")}


def test_funnel_matches(conn: sqlite3.Connection, perf) -> None:  # noqa: ANN001
    start, end = _window(perf.period.first, perf.period.last)
    f, violations = funnel(conn, start, end)
    steps = {s.key: s.count for s in perf.funnel.steps}
    assert steps == {
        "proposed": f.proposals,
        "gate_pass": f.gate_pass,
        "approved": f.approvals.click_approved + f.approvals.auto_approved,
        "filled": f.fills,
        "closed": perf.win_loss.stats.closed,
    }
    assert perf.funnel.violations == violations and violations["spread_too_wide"] >= 2
    assert perf.funnel.gate_fail == f.gate_fail


# ---------------------------------------------------------------------------
# Empty period, API, cache, read-only
# ---------------------------------------------------------------------------


def test_empty_period_is_empty_not_zero(tmp_path: Path) -> None:
    db = tmp_path / "empty.db"
    c = connect(db)
    migrate(c)
    c.close()
    ro = connect_ro(db)
    try:
        p = load_performance(ro, now=NOW, preset="mtd")
    finally:
        ro.close()
    assert p.net_pnl.empty and p.net_pnl.net is None and p.net_pnl.source == "none"
    assert p.equity.empty and p.costs.empty and p.win_loss.empty and p.model.empty
    assert p.calibration.empty and p.funnel.empty
    assert all(b.pnl is None for b in p.net_pnl.bars)
    assert len(p.net_pnl.bars) == 30  # every September slot, the future ones empty


def test_api_performance(client: TestClient, perf) -> None:  # noqa: ANN001
    r = client.get("/api/performance", params={"preset": "90d", "compare": "prev"})
    assert r.status_code == 200
    body = r.json()
    assert body["win_loss"]["stats"]["closed"] == perf.win_loss.stats.closed
    assert body["period"] == {"first": "2026-07-01", "last": "2026-09-28",
                              "slot_end": "2026-09-28", "days": 90}  # fmt: skip
    assert body["compare_period"]["last"] == "2026-06-30"
    r = client.get(
        "/api/performance",
        params={"preset": "custom", "from": "2026-08-01", "to": "2026-08-31", "compare": "yoy"},
    )
    assert r.status_code == 200 and r.json()["compare_period"]["first"] == "2025-08-01"
    bad = client.get("/api/performance", params={"preset": "custom"})
    assert bad.status_code == 422 and bad.json()["error"] == "invalid_request"
    assert client.get("/api/performance", params={"preset": "nope"}).status_code == 422
    bd = client.get("/api/performance/breakdown", params={"by": "regime"})
    assert bd.status_code == 200 and bd.json()["by"] == "regime"
    assert client.post("/api/performance").status_code == 405


def test_response_is_cached_until_the_db_changes(tmp_path: Path) -> None:
    db = fixture.build(tmp_path / "arc.db", NOW)
    app = create_app(db, clock=lambda: NOW)
    t = [0.0]
    app.state.performance_cache = ResponseCache(clock=lambda: t[0])
    with TestClient(app) as c:
        first = c.get("/api/performance").json()
        cache: ResponseCache = app.state.performance_cache
        assert (cache.hits, cache.misses) == (0, 1)
        assert c.get("/api/performance").json() == first
        assert cache.hits == 1
        t[0] = 61.0  # TTL
        c.get("/api/performance")
        assert cache.misses == 2
        w = sqlite3.connect(db)  # a write moves the mtime: a miss even inside the TTL
        w.execute("INSERT INTO ops_alerts (key, kind, message, opened_at) VALUES ('k','x','m','t')")
        w.commit()
        w.close()
        c.get("/api/performance")
        assert cache.misses == 3


def test_the_page_never_writes(fx_db: Path) -> None:
    before = hashlib.sha256(fx_db.read_bytes()).hexdigest()
    with TestClient(create_app(fx_db, clock=lambda: NOW)) as c:
        for preset in ("week", "mtd", "qtd", "ytd", "30d", "90d", "all"):
            assert c.get("/api/performance", params={"preset": preset}).status_code == 200
    assert hashlib.sha256(fx_db.read_bytes()).hexdigest() == before
