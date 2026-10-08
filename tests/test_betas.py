"""E3.6 (D62): daily beta vs SPY, the ``betas`` table, the shared lookup and wiring."""

from __future__ import annotations

import datetime as dt
import math
import sqlite3
from decimal import Decimal as D
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from arc.betas.refresh import LOOKBACK_DAYS, refresh_betas, refresh_tickers
from arc.betas.store import MAX_AGE_SESSIONS, BetaRow, betas_used, latest_rows, upsert
from arc.config import ArcSettings
from arc.context import ContextStore
from arc.data.base import HistoryBar
from arc.features.beta import BETA_FLOOR, beta_vs, floored_beta
from arc.routines.config import RoutinesConfig, load_routines
from arc.routines.handlers import BUILTIN_HANDLERS, JobContext, betas_source
from arc.store.db import connect
from arc.store.migrate import migrate
from arc.utils.calendar import ET, sessions_between

NOW = dt.datetime(2026, 10, 8, 5, 35, tzinfo=ET)
DAY = NOW.date()


@pytest.fixture
def conn() -> Any:
    c = connect(":memory:")
    migrate(c)
    return c


def _days(n: int, end: dt.date = DAY - dt.timedelta(days=1)) -> list[dt.date]:
    return sessions_between(end - dt.timedelta(days=2 * n), end)[-n:]


def _series(days: list[dt.date], rets: list[float], start: float = 100.0) -> dict[dt.date, float]:
    out, px = {days[0]: start}, start
    for d, r in zip(days[1:], rets, strict=True):
        px *= math.exp(r)
        out[d] = px
    return out


def _bench_rets(n: int) -> list[float]:
    return [0.01 * math.sin(i * 1.7) + 0.002 * ((i % 5) - 2) for i in range(n)]


# ---------------------------------------------------------------------------
# beta math (pure)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("k", [0.5, 1.0, 1.8, 3.2, -0.7])
def test_beta_recovers_a_linear_multiple(k: float) -> None:
    days = _days(300)
    rm = _bench_rets(299)
    bench = _series(days, rm)
    name = _series(days, [k * r for r in rm])
    res = beta_vs(name, bench)
    assert res.beta == pytest.approx(k, abs=1e-9)
    assert res.n_days == 252 and res.window == 252 and res.as_of == days[-1]


def test_beta_uses_aligned_dates_and_last_window() -> None:
    days = _days(300)
    rm = _bench_rets(299)
    bench = _series(days, rm)
    # old history at beta 3, the last 253 closes at beta 2: only the last window counts
    name = _series(days, [3 * r for r in rm[:46]] + [2 * r for r in rm[46:]])
    assert beta_vs(name, bench).beta == pytest.approx(2.0, abs=1e-9)
    # a date missing from the name (halt) or with a bad close is skipped on both sides
    gap = {d: c for d, c in name.items() if d != days[-10]}
    gap[days[-20]] = 0.0
    res = beta_vs(gap, bench)
    assert res.n_days == 252 and res.beta is not None


def test_beta_too_few_days_or_flat_bench_is_none() -> None:
    days = _days(100)
    bench = _series(days, _bench_rets(99))
    res = beta_vs(bench, bench)
    assert res.beta is None and res.n_days == 99
    assert beta_vs({}, {}).as_of is None
    flat = dict.fromkeys(_days(200), 50.0)
    assert beta_vs(flat, flat).beta is None


@pytest.mark.parametrize(
    ("raw", "used"),
    [(None, 1.0), (0.4, 1.0), (1.0, 1.0), (2.37, 2.37), (-1.2, 1.0), (float("nan"), 1.0)],
)
def test_floored_beta(raw: float | None, used: float) -> None:
    assert floored_beta(raw) == used


@given(st.floats(min_value=-10, max_value=10, allow_nan=False))
def test_floored_beta_never_below_one(b: float) -> None:
    assert floored_beta(b) >= BETA_FLOOR
    assert floored_beta(b) == max(b, 1.0)


# ---------------------------------------------------------------------------
# store + shared lookup
# ---------------------------------------------------------------------------


def _row(t: str, day: dt.date, beta: float | None, n: int = 252) -> BetaRow:
    return BetaRow(t, day, beta, n, 252, "SPY", day - dt.timedelta(days=1))


def test_upsert_is_idempotent_per_ticker_day(conn: sqlite3.Connection) -> None:
    upsert(conn, [_row("mu", DAY, 2.0)], now=NOW)
    upsert(conn, [_row("MU", DAY, 3.22)], now=NOW)
    rows = conn.execute("SELECT ticker, day, beta FROM betas").fetchall()
    assert [tuple(r) for r in rows] == [("MU", DAY.isoformat(), 3.22)]
    assert [r["ticker"] for r in latest_rows(conn)] == ["MU"]


def test_betas_used_floor_default_and_staleness(conn: sqlite3.Connection) -> None:
    old = sessions_between(DAY - dt.timedelta(days=20), DAY)
    upsert(
        conn,
        [
            _row("MU", DAY, 3.22),
            _row("KO", DAY, 0.55),
            _row("NEW", DAY, None, n=40),
            _row("OLD", old[-(MAX_AGE_SESSIONS + 2)], 2.5),  # 6 sessions old: stale
            _row("EDGE", old[-(MAX_AGE_SESSIONS + 1)], 1.7),  # 5 sessions old: used
            _row("FUT", DAY + dt.timedelta(days=1), 4.0),  # after today: never read
        ],
        now=NOW,
    )
    used = betas_used(conn, ["mu", "KO", "NEW", "OLD", "EDGE", "FUT", "ZZZ"], DAY)
    assert (used["MU"].beta, used["MU"].source, used["MU"].raw) == (3.22, "stored", 3.22)
    assert (used["KO"].beta, used["KO"].source, used["KO"].raw) == (1.0, "stored", 0.55)
    for t in ("NEW", "OLD", "FUT", "ZZZ"):
        assert (used[t].beta, used[t].source) == (1.0, "default"), t
    assert used["EDGE"].beta == 1.7
    assert all(u.beta >= 1.0 for u in used.values())


def test_betas_used_without_table_or_conn_defaults() -> None:
    bare = sqlite3.connect(":memory:")
    assert betas_used(bare, ["MU"], DAY)["MU"].source == "default"
    assert betas_used(None, ["MU"], DAY)["MU"].beta == 1.0
    assert betas_used(bare, [], DAY) == {}


# ---------------------------------------------------------------------------
# refresh (fake market) + routine handler + budget
# ---------------------------------------------------------------------------


class _Bars:
    """Daily closes per symbol; MU = 3x SPY, KO = 0.5x SPY, BAD raises, THIN = 60 days."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dt.date, dt.date]] = []
        days = _days(300)
        rm = _bench_rets(299)
        self.series = {
            "SPY": _series(days, rm),
            "MU": _series(days, [3 * r for r in rm]),
            "KO": _series(days, [0.5 * r for r in rm]),
            "QQQ": _series(days, [1.2 * r for r in rm]),
            "IWM": _series(days, [1.1 * r for r in rm]),
            "THIN": _series(days[-60:], [r for r in rm[-59:]]),
        }

    def history_bars(self, symbol, start, end, timeframe="1Day"):  # noqa: ANN001, ANN201
        self.calls.append((symbol, start, end))
        if symbol == "BAD":
            raise RuntimeError("bars down")
        return [
            HistoryBar(
                timestamp=dt.datetime.combine(d, dt.time(4), tzinfo=dt.UTC),
                open=c,
                high=c,
                low=c,
                close=c,
                volume=1.0,
            )  # fmt: skip
            for d, c in sorted(self.series.get(symbol, {}).items())
            if start <= d <= end
        ]


def test_refresh_betas_rows_errors_and_budget(conn: sqlite3.Connection) -> None:
    m = _Bars()
    taken: list[int] = []
    res = refresh_betas(conn, m, ["mu", "KO", "BAD", "THIN", "MU"], DAY, now=NOW,
                        take=lambda: taken.append(1))  # fmt: skip
    got = {r.ticker: r.beta for r in res.rows}
    assert got["MU"] == pytest.approx(3.0) and got["KO"] == pytest.approx(0.5)
    assert got["SPY"] == pytest.approx(1.0) and got["THIN"] is None
    assert set(got) == {"MU", "KO", "THIN", "SPY", "QQQ", "IWM"}
    assert set(res.errors) == {"BAD"}
    # one request per symbol (SPY once, reused as the benchmark), each a budget slot
    assert len(m.calls) == res.calls == len(taken) == 7  # incl. the failed BAD request
    # closes strictly before the row day, ~400 calendar days back
    assert {(s, e) for _, s, e in m.calls} == {
        (DAY - dt.timedelta(days=LOOKBACK_DAYS), DAY - dt.timedelta(days=1))
    }
    assert res.metrics()["too_few_days"] == ["THIN"]
    assert betas_used(conn, ["MU", "KO", "THIN"], DAY)["MU"].beta == pytest.approx(3.0)


def test_refresh_without_benchmark_raises(conn: sqlite3.Connection) -> None:
    m = _Bars()
    m.series.pop("SPY")
    with pytest.raises(LookupError, match="no SPY closes"):
        refresh_betas(conn, m, ["MU"], DAY, now=NOW)


def test_refresh_tickers_adds_market_reference() -> None:
    assert refresh_tickers(["mu", "SPY", "MU"]) == ["MU", "SPY", "QQQ", "IWM"]


def _ctx(conn: sqlite3.Connection, tickers: list[str]) -> JobContext:
    routines = RoutinesConfig.model_validate(
        {"sources": {"betas": {"schedule": ["05:35"], "writes": [], "tickers": tickers}}}
    )
    kind, spec = routines.step("betas")
    return JobContext(
        job="betas", kind=kind, spec=spec, run_id="run-1", chain_run_id=None,
        scheduled_for=NOW, now=NOW, conn=conn, snapshot=ContextStore(conn).snapshot(NOW),
        routines=routines, settings_factory=ArcSettings,
    )  # fmt: skip


def test_betas_handler(conn: sqlite3.Connection) -> None:
    res = betas_source(_ctx(conn, ["MU", "THIN"]), market=_Bars(), take=lambda: None)
    assert res.summary == f"5 betas vs SPY for {DAY} · SPY 1.00 · 1 too few days (THIN)"
    assert res.metrics["rows"] == 5 and res.metrics["alpaca_calls"] == 5
    assert {r["ticker"] for r in latest_rows(conn)} == {"MU", "THIN", "SPY", "QQQ", "IWM"}


def test_betas_handler_partial_and_total_failure(conn: sqlite3.Connection) -> None:
    class _Down(_Bars):
        def history_bars(self, symbol, start, end, timeframe="1Day"):  # noqa: ANN001, ANN201
            if symbol != "SPY":
                raise RuntimeError("down")
            return super().history_bars(symbol, start, end)

    res = betas_source(_ctx(conn, ["MU"]), market=_Down(), take=lambda: None)
    assert res.metrics["errors"] == 3 and res.summary.endswith("· 3 errors")  # MU, QQQ, IWM

    class _NoBench(_Bars):
        def history_bars(self, symbol, start, end, timeframe="1Day"):  # noqa: ANN001, ANN201
            return []

    with pytest.raises(LookupError):
        betas_source(_ctx(conn, ["MU"]), market=_NoBench(), take=lambda: None)


def test_shipped_betas_job() -> None:
    kind, spec = load_routines().jobs()["betas"]
    assert [t.isoformat(timespec="minutes") for t in spec.schedule] == ["05:35"]
    assert spec.writes == []
    assert BUILTIN_HANDLERS["betas"] == "arc.routines.handlers:betas_source"
    # before the 06:00 Scout and the open: today's betas exist before any proposal
    scout = load_routines().jobs()["scout"][1]
    assert min(spec.schedule) < min(scout.schedule)


# ---------------------------------------------------------------------------
# gate inputs: build_portfolio + market_snapshot read the shared lookup
# ---------------------------------------------------------------------------


def test_build_portfolio_beta_weights_dollar_delta() -> None:
    from arc.pipeline.market import build_portfolio, market_snapshot, proposal_betas
    from arc.pipeline.runner import open_db
    from tests.test_pipeline_market import NOW as PNOW
    from tests.test_pipeline_market import FakeMarket, positions

    LP, SP = (p.symbol for p in reversed(positions()))  # noqa: N806
    conn = open_db(":memory:", copy=False)
    m = FakeMarket({LP: 0.2, SP: 0.19})
    base = build_portfolio(conn, positions(), m, now=PNOW, wash_sale_days=30, r=0.04)
    # no stored beta -> 1.0: beta-weighted equals plain dollar delta
    assert base.beta_dollar_delta == base.dollar_delta != D(0)
    upsert(conn, [_row("SPY", PNOW.date(), 1.5)], now=PNOW)
    breakdown: dict[str, dict[str, float | str]] = {}
    pf = build_portfolio(conn, positions(), m, now=PNOW, wash_sale_days=30, r=0.04,
                         delta_breakdown=breakdown)  # fmt: skip
    assert float(pf.beta_dollar_delta) == pytest.approx(1.5 * float(pf.dollar_delta))
    assert breakdown["SPY"]["beta"] == 1.5 and breakdown["SPY"]["beta_source"] == "stored"
    assert breakdown["SPY"]["dollar_delta"] == pytest.approx(float(pf.dollar_delta), abs=0.01)
    # the proposal path: the same floored lookup reaches MarketSnapshot.underlying_beta
    upsert(conn, [_row("KO", PNOW.date(), 0.4)], now=PNOW)
    betas = proposal_betas(conn, ["SPY", "KO", "ZZZ"], PNOW.date())
    assert betas == {"SPY": 1.5, "KO": 1.0, "ZZZ": 1.0}
    snap = market_snapshot({}, {}, {"SPY": 580.0}, betas)
    assert snap.underlying_beta == {"SPY": D("1.5"), "KO": D("1.0"), "ZZZ": D("1.0")}


def test_with_position_carries_beta_dollar_delta_forward() -> None:
    from arc.gate.inputs import Portfolio
    from arc.models import Greeks
    from arc.pipeline.steps import _with_position

    pf = Portfolio(dollar_delta=D(1000), beta_dollar_delta=D(2000))
    g = Greeks(delta=10.0)
    out = _with_position(pf, "MU", D(100), g, 2, D(100), D("3.22"))
    assert out.dollar_delta == D(3000) and out.beta_dollar_delta == D("8440.00")
    low = _with_position(pf, "KO", D(100), g, 2, D(100), D("0.5"))
    assert low.beta_dollar_delta == D(4000)  # floored at 1.0
    none = _with_position(pf, "KO", D(100), g, 2, D(100))
    assert none.beta_dollar_delta == D(4000)
