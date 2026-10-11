"""E7.10 (D84): the point-in-time backtest data layer.

Covers: each quoted-mark drop rule, the fallback modes, the smile/close golden, the
rate curve, checkpoint + PIT eligibility, the D9 screen flag, the no-look-ahead ×3
test, IV30/IV rank against the live code, earnings detection on a synthetic IV
crush (and market-wide crush rejection), ``next_earnings`` projection, precision /
recall, the menu cache (hit, miss, key sensitivity, ranking-only reuse) and the CLI.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from arc.backtest.costs import load_cost_model
from arc.backtest.data import DataSettings, HistoryView, quarter_start
from arc.backtest.earnings_hist import (
    EarningsEvent,
    EarningsRule,
    detect_events,
    match_events,
    next_earnings,
)
from arc.backtest.ivhist import TermPoint, iv30_from_points, iv_points, term_points
from arc.backtest.marks import (
    DropCounts,
    RateCurve,
    clean_chains,
    mark_chains,
    quoted_marks,
    remark_chains,
)
from arc.backtest.menucache import MenuCache, MenuCacheContext, MenuCacheKey, config_hash
from arc.features.vol import (
    atm_iv_from_chain,
    iv_percentile,
    iv_rank,
)
from tests.backtest_data_fixtures import (
    R,
    marks_golden_payload,
    sessions,
    spot_path,
    synth_rows,
    write_history,
    write_underlying,
    write_universe,
)
from tests.golden_compare import assert_json_close

FIXTURES = Path(__file__).parent / "fixtures"
COST = load_cost_model()
DAY = dt.date(2025, 3, 3)


def _row(**kw: object) -> dict[str, object]:
    base: dict[str, object] = {
        "date": DAY,
        "expiration": DAY + dt.timedelta(days=30),
        "strike": 100.0,
        "right": "call",
        "symbol": "X",
        "close": 2.0,
        "volume": 10.0,
        "bid": 1.95,
        "ask": 2.05,
    }
    base.update(kw)
    return base


# ---------------------------------------------------------------------------
# Quoted marks
# ---------------------------------------------------------------------------


def _rules_frame() -> pd.DataFrame:
    return pd.DataFrame(
        [
            _row(symbol="ok", bid=1.95, ask=2.05),
            _row(symbol="noq", bid=None, ask=None),
            _row(symbol="ask0", bid=0.0, ask=0.0),
            _row(symbol="crossed", bid=2.10, ask=2.00),
            _row(symbol="locked", bid=2.00, ask=2.00),
            _row(symbol="zbid", strike=140.0, bid=0.0, ask=0.10),
            _row(symbol="wide", bid=1.50, ask=2.50),
            _row(symbol="expired", expiration=DAY),
        ]
    )


def test_quoted_mid_spread_and_iv_from_the_nbbo() -> None:
    out, counts = quoted_marks(_rules_frame(), 100.0, r=R)
    ok = out.set_index("symbol").loc["ok"]
    assert ok["mid"] == pytest.approx(2.0)
    assert ok["spread"] == pytest.approx(0.10)
    assert np.isfinite(ok["iv"]) and 0.0 < ok["delta"] < 1.0
    assert bool(ok["sellable"]) and bool(ok["quoted"])
    assert counts.contracts == 7  # the expired row is not counted
    assert counts.kept == 1


@pytest.mark.parametrize(
    ("symbol", "reason"),
    [
        ("noq", "no_quote"),
        ("ask0", "ask_le_zero"),
        ("crossed", "crossed_locked"),
        ("locked", "crossed_locked"),
        ("zbid", "zero_bid"),
        ("wide", "wide_spread"),
    ],
)
def test_each_quoted_drop_rule(symbol: str, reason: str) -> None:
    frame = _rules_frame()
    one = frame[frame["symbol"].isin([symbol])]
    out, counts = quoted_marks(one, 100.0, r=R)
    assert out.empty
    assert getattr(counts, reason) == 1
    assert counts.contracts == 1 and counts.kept == 0


def test_spread_threshold_is_per_leg_and_tunable() -> None:
    wide = pd.DataFrame([_row(symbol="w", bid=1.50, ask=2.50)])  # 50 % of mid
    assert quoted_marks(wide, 100.0, r=R, max_spread_pct=0.35)[1].wide_spread == 1
    kept, c = quoted_marks(wide, 100.0, r=R, max_spread_pct=0.60)
    assert c.kept == 1 and len(kept) == 1


def test_zero_bid_kept_by_a_loose_spread_rule_is_never_sold() -> None:
    from arc.backtest.strategies import StrategyKind, StrategySpec, select_legs

    exp = DAY + dt.timedelta(days=35)
    rows = [
        _row(symbol=f"P{k}", right="put", strike=float(k), expiration=exp, bid=b, ask=a)
        for k, b, a in ((90, 0.0, 0.40), (85, 0.05, 0.15))
    ]
    out, c = quoted_marks(pd.DataFrame(rows), 100.0, r=R, max_spread_pct=5.0)
    assert c.zero_bid == 0 and c.kept == 2
    assert not bool(out.set_index("symbol").loc["P90", "sellable"])
    spec = StrategySpec(kind=StrategyKind.BULL_PUT, delta=0.2, delta_tol=0.5, width_pct=0.05)
    legs = select_legs(out, spec, 100.0, exp)
    assert legs is None or all(p.symbol != "P90" for p in legs if p.side < 0)


def test_quoted_fallback_smile_reprices_dropped_contracts() -> None:
    days = sessions(DAY, 1)
    rows = synth_rows("XYZ", days, [100.0])[days[0]]
    raw = pd.DataFrame([r.model_dump() for r in rows])
    base = quoted_marks(raw, 100.0, r=R)[1].wide_spread  # cheap wings: $0.01 ticks
    # widen three near-ATM quotes past the rule
    exp = sorted(raw["expiration"].unique())[2]  # the front weekly has too few smile points
    hit = raw.index[
        (raw["strike"].between(97.5, 102.5)) & (raw["right"] == "call") & (raw["expiration"] == exp)
    ][:3]
    raw.loc[hit, "ask"] = raw.loc[hit, "bid"] * 3 + 1
    dropped, c1 = quoted_marks(raw, 100.0, r=R, fallback="drop")
    filled, c2 = quoted_marks(raw, 100.0, r=R, fallback="smile", cost=COST)
    assert c1.wide_spread == c2.wide_spread == base + 3
    assert c1.smile_filled == 0 and c2.smile_filled >= 3  # + any wing inside the fit
    assert len(filled) == len(dropped) + c2.smile_filled
    got = filled[filled["symbol"].isin(set(raw.loc[hit, "symbol"]))]
    assert (~got["quoted"]).all() and (got["mid"] > 0).all()
    with pytest.raises(ValueError, match="cost model"):
        quoted_marks(raw, 100.0, r=R, fallback="smile")


def test_quoted_empty_and_drop_counts_add() -> None:
    out, c = quoted_marks(pd.DataFrame(columns=list(_row())), 100.0, r=R)
    assert out.empty and c == DropCounts()
    a = DropCounts(contracts=4, wide_spread=1, kept=3)
    s = a.add(a)
    assert s.contracts == 8 and s.pct("wide_spread") == pytest.approx(25.0)
    assert DropCounts().pct("no_quote") == 0.0


def test_rate_curve_flat_and_treasury(tmp_path: Path) -> None:
    assert RateCurve.load(tmp_path, flat=0.04, source="treasury").source == "flat"
    p = tmp_path / "rates"
    p.mkdir()
    pd.DataFrame(
        {"date": [dt.date(2025, 1, 2), dt.date(2025, 3, 3)], "rate": [0.05, 0.03]}
    ).to_parquet(p / "treasury_3m.parquet")
    rc = RateCurve.load(tmp_path, flat=0.04, source="treasury")
    assert rc.source == "treasury"
    assert rc.rate(dt.date(2024, 12, 31)) == 0.04  # before the series: flat
    assert rc.rate(dt.date(2025, 3, 2)) == 0.05  # latest on/before, never later
    assert rc.rate(dt.date(2025, 3, 3)) == 0.03
    assert RateCurve.load(tmp_path, flat=0.04, source="flat").rate(DAY) == 0.04


# ---------------------------------------------------------------------------
# smile / close golden (moved from ranking.py byte-identical)
# ---------------------------------------------------------------------------


# The close marks are IVs from an iterative Black-Scholes solve; its last few
# digits move with Linux CI's libm/BLAS (3e-11 relative seen on main's CI vs the
# macOS-captured golden), above the shared 1e-12. 1e-9 still fails any real
# pricing change (a 0.0001-vol shift is ~4e-4 relative).
MARKS_GOLDEN_REL_TOL = 1e-9


def test_smile_and_close_marks_match_the_pre_move_golden() -> None:
    want = json.loads((FIXTURES / "marks_golden.json").read_text())
    assert_json_close(
        marks_golden_payload(remark_chains, clean_chains), want, rel_tol=MARKS_GOLDEN_REL_TOL
    )


def test_ranking_reexports_the_moved_functions() -> None:
    from arc.backtest import marks, ranking

    for name in ("smile_marks", "remark_chains", "clean_chains", "smile_deviation"):
        assert getattr(ranking, name) is getattr(marks, name)


def test_mark_chains_smile_close_equal_the_e75_path() -> None:
    from arc.backtest.engine import prepare_chains

    days = sessions(DAY, 2)
    spots = [100.0, 101.0]
    raw = pd.DataFrame(
        [
            r.model_dump()
            for d, rs in synth_rows("XYZ", days, spots, quoted=False).items()
            for r in rs
        ]
    )
    closes = pd.Series(spots, index=days)
    base = prepare_chains(raw, closes, cost=COST, r=R, dte_min=1, dte_max=70)
    rc = RateCurve(R)
    sm, d1 = mark_chains(raw, closes, mode="smile", cost=COST, rate=rc, dte_max=70)
    cl, d2 = mark_chains(raw, closes, mode="close", cost=COST, rate=rc, dte_max=70)
    assert d1 == d2 == {}
    for d in days:
        pd.testing.assert_frame_equal(sm[d], remark_chains(base, closes, r=R, cost=COST)[d])
        pd.testing.assert_frame_equal(cl[d], clean_chains(base, max_dev=0.15)[d])


# ---------------------------------------------------------------------------
# IV30 / IV rank
# ---------------------------------------------------------------------------


def test_term_points_and_iv30_match_live_atm_iv_from_chain() -> None:
    days = sessions(DAY, 1)
    rows = synth_rows("XYZ", days, [100.0], iv_fn=lambda i, d, e: 0.2 + (e - d).days / 1000)
    raw = pd.DataFrame([r.model_dump() for r in rows[days[0]]])
    ch, _ = quoted_marks(raw, 100.0, r=R)
    pts = term_points(ch, 100.0, days[0])
    got, n = iv30_from_points(pts, dte_min=0, dte_max=10_000)

    class C:
        def __init__(self, e: dt.date, k: float, v: float) -> None:
            self.expiration, self.strike, self.implied_volatility = e, k, v

    live = atm_iv_from_chain(
        [C(e, k, v) for e, k, v in zip(ch["expiration"], ch["strike"], ch["iv"], strict=True)],
        100.0,
        days[0],
    )
    assert n == len(pts) == 4
    assert got == pytest.approx(live, abs=1e-12)
    assert got == pytest.approx(0.23, abs=0.01)  # ~30 DTE on the synthetic term structure
    assert iv30_from_points([]) == (None, 0)
    assert term_points(ch.iloc[0:0], 100.0, days[0]) == []


def test_iv_rank_matches_live_functions_and_min_obs() -> None:
    days = sessions(dt.date(2024, 1, 2), 200)
    vals = [0.2 + 0.1 * np.sin(i / 7) for i in range(200)]
    pts = {
        d: [TermPoint(expiration=d + dt.timedelta(days=30), dte=30, iv=v)]
        for d, v in zip(days, vals, strict=True)
    }
    out = iv_points("xyz", pts, lookback=252, min_obs=120)
    s = pd.Series(vals, index=days)
    for i in (0, 118, 119, 150, 199):
        p = out[days[i]]
        assert p.iv30 == pytest.approx(vals[i])
        assert p.n_obs == i + 1
        if i + 1 < 120:
            assert p.iv_rank is None and p.iv_percentile is None
        else:
            assert p.iv_rank == pytest.approx(iv_rank(s, days[i], lookback=252, min_obs=120))
            assert p.iv_percentile == pytest.approx(
                iv_percentile(s, days[i], lookback=252, min_obs=120)
            )
    assert out[days[0]].ticker == "XYZ"


# ---------------------------------------------------------------------------
# Earnings
# ---------------------------------------------------------------------------


def _pts(day: dt.date, front: float, nxt: float, *, gap: int = 3) -> list[TermPoint]:
    e1 = day + dt.timedelta(days=gap)
    e2 = e1 + dt.timedelta(days=7)
    return [
        TermPoint(expiration=e1, dte=gap, iv=front),
        TermPoint(expiration=e2, dte=gap + 7, iv=nxt),
    ]


def _crush_series(days: list[dt.date], event_i: int) -> dict[dt.date, list[TermPoint]]:
    out: dict[dt.date, list[TermPoint]] = {}
    e1 = days[event_i] + dt.timedelta(days=4)
    e2 = e1 + dt.timedelta(days=7)
    for i, d in enumerate(days):
        front = 0.25
        if i == event_i - 1:
            front = 0.40  # priced-in event on the prior close
        out[d] = [
            TermPoint(expiration=e1, dte=max((e1 - d).days, 1), iv=front),
            TermPoint(expiration=e2, dte=(e2 - d).days, iv=0.25),
        ]
    return out


def test_detects_a_synthetic_iv_crush() -> None:
    days = sessions(dt.date(2025, 4, 1), 20)
    rule = EarningsRule()
    ev = detect_events("xyz", _crush_series(days, 10), rule)
    assert [e.day for e in ev] == [days[10]]
    e = ev[0]
    assert e.ticker == "XYZ" and e.detected_on == days[10]
    assert e.jump == pytest.approx(0.6) and e.crush == pytest.approx(0.375)
    assert detect_events("SPY", _crush_series(days, 10), rule, etf=True) == []


def test_no_event_without_jump_or_without_crush() -> None:
    d0, d1 = sessions(dt.date(2025, 4, 1), 2)
    rule = EarningsRule()
    flat = {d0: _pts(d0, 0.26, 0.25), d1: _pts(d0, 0.20, 0.25)}  # crush but no jump
    assert detect_events("X", flat, rule) == []
    held = {d0: _pts(d0, 0.40, 0.25), d1: _pts(d0, 0.39, 0.25)}  # jump, no crush
    assert detect_events("X", held, rule) == []
    gone = {d0: _pts(d0, 0.40, 0.25), d1: []}  # front not quoted after
    assert detect_events("X", gone, rule) == []
    one = {d0: _pts(d0, 0.40, 0.25)[:1], d1: _pts(d0, 0.2, 0.25)}  # no next expiry
    assert detect_events("X", one, rule) == []


def test_market_wide_crush_is_not_an_earnings_event() -> None:
    days = sessions(dt.date(2025, 4, 1), 20)
    rule = EarningsRule()
    name = _crush_series(days, 10)
    assert detect_events("X", name, rule, market=name) == []  # the market did it too
    assert len(detect_events("X", name, rule, market=_crush_series(days, 15))) == 1
    assert len(detect_events("X", name, rule, market={})) == 1  # unmeasurable → raw


def test_min_gap_keeps_one_event_per_quarter() -> None:
    days = sessions(dt.date(2025, 4, 1), 60)
    pts = _crush_series(days, 10)
    pts.update({d: v for d, v in _crush_series(days, 20).items() if d in days[18:22]})
    assert len(detect_events("X", pts, EarningsRule(earn_min_gap_sessions=30))) == 1
    assert len(detect_events("X", pts, EarningsRule(earn_min_gap_sessions=5))) == 2


def test_next_earnings_projects_from_detections_known_by_d_only() -> None:
    rule = EarningsRule()
    e1 = EarningsEvent(
        ticker="X",
        day=dt.date(2025, 1, 30),
        detected_on=dt.date(2025, 1, 30),
        front_expiration=dt.date(2025, 1, 31),
        jump=0.3,
        crush=0.3,
    )
    e2 = e1.model_copy(update={"day": dt.date(2025, 5, 1), "detected_on": dt.date(2025, 5, 1)})
    assert next_earnings([e1, e2], dt.date(2025, 1, 29), rule).basis == "unknown"
    n = next_earnings([e1, e2], dt.date(2025, 4, 15), rule)  # e2 not known yet
    assert n.basis == "projected" and n.last_detected == e1.day
    assert n.day == e1.day + dt.timedelta(days=91)
    late = next_earnings([e1], dt.date(2025, 9, 1), rule)  # stepped past d
    assert late.day == e1.day + dt.timedelta(days=91 * 3)
    assert next_earnings([e1], dt.date(2025, 9, 1), rule, etf=True).basis == "etf"


def test_match_events_precision_recall() -> None:
    days = sessions(dt.date(2025, 4, 1), 30)
    st = match_events([days[5], days[20]], [days[6], days[25]], days)
    assert (st.reference, st.detected, st.matched) == (2, 2, 1)
    assert st.precision == pytest.approx(0.5) and st.recall == pytest.approx(0.5)
    sat = days[3] + dt.timedelta(days=(5 - days[3].weekday()) % 7)  # a weekend date
    st2 = match_events([days[days.index(min(d for d in days if d > sat))]], [sat], days)
    assert st2.matched == 1
    out = match_events([dt.date(2020, 1, 2)], [dt.date(2030, 1, 2)], days)
    assert out.reference == out.detected == 0 and out.precision is None
    assert match_events([], [], []).reference == 0


# ---------------------------------------------------------------------------
# HistoryView: eligibility, PIT, screen, no look-ahead
# ---------------------------------------------------------------------------

N_DAYS = 160


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    days = sessions(dt.date(2025, 3, 3), N_DAYS)
    spots = spot_path(N_DAYS)
    rows = synth_rows("AAA", days, spots, oi=400.0)
    write_history(tmp_path, "AAA", rows)
    write_underlying(tmp_path, "AAA", days, spots)
    # BBB: alpaca partition only (fallback), no quotes
    b = synth_rows("BBB", days[:40], spots[:40], provider="alpaca", quoted=False)
    write_history(tmp_path, "BBB", b, provider="alpaca", with_oi=False)
    write_underlying(tmp_path, "BBB", days, spots)
    q2, q3 = dt.date(2025, 4, 1), dt.date(2025, 7, 1)
    write_universe(
        tmp_path,
        ["AAA", "BBB", "CCC"],
        [
            (dt.date(2025, 1, 1), "AAA", True),
            (q2, "AAA", True),
            (q3, "AAA", False),  # out of the top in Q3 -> not PIT-eligible then
            (dt.date(2025, 1, 1), "BBB", True),
            (q2, "BBB", True),
            (q3, "BBB", True),
        ],
    )
    return tmp_path


def _view(data_dir: Path, **kw: object) -> HistoryView:
    days = sessions(dt.date(2025, 3, 3), N_DAYS)
    settings = DataSettings(**kw)  # type: ignore[arg-type]
    return HistoryView(
        data_dir,
        settings=settings,
        universe="c125",
        start=days[0],
        end=days[-1],
        cost=COST,
        flat_rate=R,
        min_iv_obs=20,
    )


def test_quarter_start() -> None:
    assert quarter_start(dt.date(2025, 5, 17)) == dt.date(2025, 4, 1)
    assert quarter_start(dt.date(2025, 12, 31)) == dt.date(2025, 10, 1)


def test_checkpoint_and_pit_eligibility(data_dir: Path) -> None:
    v = _view(data_dir)
    days = sessions(dt.date(2025, 3, 3), N_DAYS)
    q2_day = next(d for d in days if d.month == 5)
    q3_day = next(d for d in days if d.month == 8)
    assert v.symbols == ["AAA", "BBB", "CCC"]
    e = v.eligibility("AAA", q2_day)
    assert e.in_checkpoint and e.in_pit and e.has_close and e.eligible
    e3 = v.eligibility("aaa", q3_day)
    assert e3.in_checkpoint and not e3.in_pit and not e3.eligible
    assert _view(data_dir, pit=False).eligibility("AAA", q3_day).eligible
    c = v.eligibility("CCC", q2_day)  # no PIT row, no close
    assert not c.in_pit and not c.has_close and not c.eligible
    assert v.eligible(q2_day) == ["AAA", "BBB"]
    assert v.eligible(q3_day) == ["BBB"]
    v2 = HistoryView(
        data_dir,
        settings=DataSettings(),
        universe="c250",
        start=days[0],
        end=days[-1],
        cost=COST,
        flat_rate=R,
    )
    assert v2.symbols == ["AAA", "BBB", "CCC"]


def test_d9_screen_is_a_flag_with_reasons(data_dir: Path) -> None:
    v = _view(data_dir)
    days = v.sessions("AAA")
    d = days[30]
    e = v.eligibility("AAA", d)
    # synthetic: price ~100, ADV 2M, near-ATM OI 3 strikes x call/put x 400, 4 % spreads
    assert e.screen_pass and e.screen_reasons == [] and e.eligible
    strict = _view(data_dir, screen={"max_atm_spread_pct": 0.01, "min_atm_open_interest": 5000})
    s = strict.eligibility("AAA", d)
    assert not s.screen_pass and s.eligible  # the flag never changes eligibility
    reasons = " ".join(s.screen_reasons)
    assert "near-ATM OI 2400 < 5000" in reasons and "ATM spread" in reasons
    m = v.liquidity_metrics(v.load("AAA"), d)
    assert m.atm_open_interest == 2400 and m.adv_shares == pytest.approx(2_000_000)
    early = v.liquidity_metrics(v.load("AAA"), days[3])
    assert early.adv_shares is None  # < 20 sessions of volume
    bm = v.liquidity_metrics(v.load("BBB"), v.sessions("BBB")[25])
    assert bm.atm_open_interest is None and bm.atm_spread_pct is None


def test_primary_partition_and_alpaca_fallback(data_dir: Path) -> None:
    v = _view(data_dir)
    a, b = v.load("AAA"), v.load("BBB")
    assert (a.provider, a.marks) == ("thetadata", "quoted")
    assert (b.provider, b.marks) == ("alpaca", "smile")
    assert v.provider_for("CCC") is None
    assert v.load("CCC").sessions == []
    assert sum(dc.kept for dc in a.drops.values()) > 0
    cd = v.day("AAA", a.sessions[50])
    assert cd.provider == "thetadata" and cd.marks == "quoted" and cd.spot is not None
    assert cd.ohlc is not None and set(cd.ohlc) >= {"open", "high", "low", "close"}
    assert cd.iv is not None and cd.iv.iv30 is not None and cd.iv.iv_rank is not None
    assert cd.rate == R and len(cd.chain) > 0 and cd.drops.contracts > 0
    none = v.day("CCC", a.sessions[50])
    assert none.chain.empty and none.spot is None and none.iv is None


def _triple_after(data_dir: Path, ticker: str, cut: dt.date, provider: str) -> None:
    """Multiply every option / underlying row dated after *cut* by 3."""
    from arc.backtest.entry_filter import OhlcStore
    from arc.backtest.underlying import UnderlyingStore
    from arc.data.history.store import ParquetHistoryStore

    st = ParquetHistoryStore(data_dir)
    for d in st.cached_dates(provider, ticker):
        if d <= cut:
            continue
        p = st.path_for(provider, ticker, d)
        df = pd.read_parquet(p)
        for c in ("close", "bid", "ask", "open_interest", "volume", "strike"):
            df[c] = df[c] * 3
        df.to_parquet(p, index=False)
    us = UnderlyingStore(data_dir)
    bars = us.read_bars(ticker)
    later = pd.Index(bars.index) > cut
    bars.loc[later, ["close", "volume"]] *= 3
    us.write_bars(ticker, bars)
    os_ = OhlcStore(data_dir)
    o = os_.read(ticker)
    o.loc[pd.Index(o.index) > cut] *= 3
    os_.write(ticker, o)


def _snapshot(v: HistoryView, t: str, d: dt.date) -> dict[str, object]:
    cd = v.day(t, d)
    return {
        "chain": cd.chain.sort_values("symbol").reset_index(drop=True),
        "drops": cd.drops,
        "iv": cd.iv,
        "earnings": cd.earnings,
        "eligibility": cd.eligibility,
        "spot": cd.spot,
        "ohlc": cd.ohlc,
    }


def test_no_look_ahead_times_three(data_dir: Path) -> None:
    v = _view(data_dir)
    days = v.sessions("AAA")
    cut = days[100]
    before = _snapshot(v, "AAA", cut)
    assert before["iv"].iv_rank is not None  # type: ignore[union-attr]
    _triple_after(data_dir, "AAA", cut, "thetadata")
    after = _snapshot(_view(data_dir), "AAA", cut)
    pd.testing.assert_frame_equal(before.pop("chain"), after.pop("chain"))  # type: ignore[arg-type]
    assert before == after
    # sanity: a later day did change
    assert _view(data_dir).day("AAA", days[120]).spot != v.day("AAA", days[120]).spot


def test_earnings_through_the_view(tmp_path: Path) -> None:
    days = sessions(dt.date(2025, 3, 3), 60)
    spots = [100.0] * 60
    ev_i = 30

    def iv_fn(i: int, d: dt.date, e: dt.date) -> float:
        front = (e - d).days <= 7 and e > days[ev_i] >= d
        return 0.45 if (front and i == ev_i - 1) else 0.25

    write_history(tmp_path, "EEE", synth_rows("EEE", days, spots, iv_fn=iv_fn))
    write_underlying(tmp_path, "EEE", days, spots)
    write_history(tmp_path, "SPY", synth_rows("SPY", days, spots))
    write_underlying(tmp_path, "SPY", days, spots)
    write_universe(
        tmp_path,
        ["SPY", "EEE"],
        [(dt.date(2025, 1, 1), "EEE", True), (dt.date(2025, 1, 1), "SPY", True)],
        asset_types={"SPY": "etf"},
    )
    v = HistoryView(
        tmp_path,
        settings=DataSettings(),
        universe="c125",
        start=days[0],
        end=days[-1],
        cost=COST,
        flat_rate=R,
    )
    assert [e.day for e in v.load("EEE").events] == [days[ev_i]]
    assert v.next_earnings("EEE", days[ev_i - 1]).basis == "unknown"
    n = v.next_earnings("EEE", days[ev_i])
    assert n.day == days[ev_i] + dt.timedelta(days=91)
    assert v.next_earnings("SPY", days[40]).basis == "etf"
    assert v.load("SPY").events == []


# ---------------------------------------------------------------------------
# Menu cache
# ---------------------------------------------------------------------------


def _key(**kw: object) -> MenuCacheKey:
    base: dict[str, object] = {
        "ticker": "AAA",
        "day": DAY,
        "menu_hash": "m1",
        "marks": "quoted:0.35:drop",
        "exit_hash": "e1",
        "n_paths": 500,
        "seed": 7,
        "provider": "thetadata",
    }
    base.update(kw)
    return MenuCacheKey(**base)  # type: ignore[arg-type]


def _menu(data_dir: Path) -> list:
    from arc.backtest.ranking import build_menu, load_ranking_file
    from arc.exits.policy import load_exit_config

    v = _view(data_dir)
    h = v.load("AAA")
    d = h.sessions[60]
    exits = load_exit_config()
    mc = exits.model.model_copy(update={"n_paths": 200})
    cfg = load_ranking_file()
    specs = cfg.backtest.specs_for("cash_debit", 20, 40)
    return build_menu(
        h.chains[d],
        h.closes,
        day=d,
        underlying="AAA",
        specs=specs,
        cost=COST,
        exits=exits,
        mc=mc,
        r=R,
    )


def test_menu_cache_hit_miss_and_round_trip(data_dir: Path) -> None:
    menu = _menu(data_dir)
    assert menu, "fixture should build candidates"
    cache = MenuCache(data_dir)
    k = _key()
    assert cache.get(k) is None and cache.misses == 1
    p = cache.put(k, menu)
    assert p.is_file() and "backtest_cache/menus/AAA" in str(p)
    got = cache.get(k)
    assert cache.hits == 1
    assert got is not None and [c.model_dump() for c in got] == [c.model_dump() for c in menu]
    st = cache.stats()
    assert st.files == 1 and st.tickers == 1 and st.bytes > 0
    assert cache.clear("ZZZ") == 0
    assert cache.clear() == 1 and cache.stats().files == 0
    assert MenuCache(data_dir / "nope").stats().files == 0


@pytest.mark.parametrize(
    "change",
    [
        {"ticker": "BBB"},
        {"day": DAY + dt.timedelta(days=1)},
        {"menu_hash": "m2"},
        {"marks": "smile:0.15:5"},
        {"exit_hash": "e2"},
        {"n_paths": 1000},
        {"seed": 8},
        {"provider": "alpaca"},
    ],
)
def test_menu_cache_key_sensitivity(tmp_path: Path, change: dict[str, object]) -> None:
    cache = MenuCache(tmp_path)
    cache.put(_key(), [])
    assert cache.get(_key()) == []
    assert cache.get(_key(**change)) is None
    assert _key().digest() != _key(**change).digest()


def test_menu_cache_rejects_a_digest_collision(tmp_path: Path) -> None:
    cache = MenuCache(tmp_path)
    k = _key()
    cache.put(k, [])
    p = cache.path_for(k)
    other = _key(seed=99)
    p2 = cache.path_for(other)
    p2.parent.mkdir(parents=True, exist_ok=True)
    p.rename(p2)  # a file sitting under the wrong key
    assert cache.get(other) is None


def test_config_hash_is_stable_and_sensitive() -> None:
    a = config_hash(COST, [1, 2], {"x": 1})
    assert a == config_hash(COST, (1, 2), {"x": 1})
    assert a != config_hash(COST.model_copy(update={"slippage_frac": 0.4}), [1, 2], {"x": 1})


def test_build_menus_uses_the_cache_and_ranking_changes_reuse_it(data_dir: Path) -> None:
    from arc.backtest.ranking import build_menus, load_ranking_file
    from arc.exits.policy import load_exit_config

    v = _view(data_dir)
    h = v.load("AAA")
    days = h.sessions[60:63]
    exits = load_exit_config()
    mc = exits.model.model_copy(update={"n_paths": 200})
    specs = load_ranking_file().backtest.specs_for("cash_debit", 20, 40)
    ctx = MenuCacheContext(
        marks="quoted", exit_hash=config_hash(exits), n_paths=200, seed=mc.seed, provider="t"
    )

    def run(cache: MenuCache, **kw: object) -> dict:
        return build_menus(
            h.chains,
            h.closes,
            days=days,
            underlying="AAA",
            specs=specs,
            cost=COST,
            exits=exits,
            mc=mc,
            r=R,
            cache=cache,
            cache_ctx=ctx,
            **kw,  # type: ignore[arg-type]
        )

    c1 = MenuCache(data_dir)
    cold = run(c1)
    assert (c1.hits, c1.misses) == (0, 3)
    c2 = MenuCache(data_dir)
    warm = run(c2)  # e.g. a run that only changed rankers / portfolio settings
    assert (c2.hits, c2.misses) == (3, 0)
    assert {d: [c.model_dump() for c in m] for d, m in warm.items()} == {
        d: [c.model_dump() for c in m] for d, m in cold.items()
    }
    c3 = MenuCache(data_dir)
    run(c3, tilt=0.2)  # a menu input changed → new keys
    assert c3.misses == 3
    chains2 = {**h.chains, days[0]: h.chains[days[0]].assign(mid=h.chains[days[0]]["mid"] * 1.01)}
    c4 = MenuCache(data_dir)
    build_menus(
        chains2, h.closes, days=days, underlying="AAA", specs=specs, cost=COST,
        exits=exits, mc=mc, r=R, cache=c4, cache_ctx=ctx,
    )  # fmt: skip
    assert (c4.hits, c4.misses) == (2, 1)  # refreshed data on one day → only it rebuilds
    nocache = build_menus(
        h.chains, h.closes, days=days, underlying="AAA", specs=specs, cost=COST,
        exits=exits, mc=mc, r=R,
    )  # fmt: skip
    assert set(nocache) == set(cold)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _db(path: Path) -> Path:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE raw_docs (source TEXT, url TEXT, tickers_hint TEXT)")
    conn.execute("CREATE TABLE iv_daily (ticker TEXT, day TEXT, source TEXT, iv30 REAL)")
    conn.commit()
    return path


def test_cli_data_check_and_cache(data_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from arc.cli import main

    days = sessions(dt.date(2025, 3, 3), N_DAYS)
    db = _db(data_dir / "audit.db")
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO raw_docs VALUES ('earnings', ?, '[\"AAA\"]')",
        (f"https://finnhub.io/calendar/earnings/AAA/{days[80].isoformat()}",),
    )
    v = _view(data_dir)
    iv = v.load("AAA").iv[days[90]].iv30
    conn.execute(
        "INSERT INTO iv_daily VALUES ('AAA', ?, 'alpaca_cm30', ?)", (days[90].isoformat(), iv)
    )
    conn.commit()
    conn.close()
    out_json = data_dir / "check.json"
    rc = main(
        [
            "backtest",
            "data",
            "check",
            "--universe",
            "c125",
            "--from",
            days[0].isoformat(),
            "--to",
            days[-1].isoformat(),
            "--data-dir",
            str(data_dir),
            "--db",
            str(db),
            "--json",
            str(out_json),
        ]
    )
    text = capsys.readouterr().out
    assert rc == 0
    assert "3 names, 2 with option history" in text
    assert "AAA    thetadata quoted" in text and "BBB    alpaca    smile" in text
    assert "IV30 cross-check" in text and "pairs 1  max |diff|" in text
    assert "earnings vs Finnhub" in text
    rows = json.loads(out_json.read_text())
    assert {r["ticker"] for r in rows} == {"AAA", "BBB"}
    with pytest.raises(FileNotFoundError):
        main(
            ["backtest", "data", "check", "--from", days[0].isoformat(), "--to",
             days[-1].isoformat(), "--data-dir", str(data_dir), "--db", str(data_dir / "x.db")]
        )  # fmt: skip
    assert not (data_dir / "x.db").exists()  # read-only open never creates a store

    MenuCache(data_dir).put(_key(), [])
    assert main(["backtest", "cache", "stats", "--data-dir", str(data_dir)]) == 0
    assert "1 files" in capsys.readouterr().out
    assert main(["backtest", "cache", "clear", "--data-dir", str(data_dir)]) == 0
    assert "removed 1" in capsys.readouterr().out


def test_rank_cli_has_no_cache_flag() -> None:
    import argparse

    from arc.backtest.cli import add_backtest_parser

    p = argparse.ArgumentParser()
    add_backtest_parser(p.add_subparsers(dest="command"))
    a = p.parse_args(
        ["backtest", "rank", "--from", "2025-01-02", "--to", "2025-02-03", "--no-cache"]
    )
    assert a.no_cache is True


def test_ranking_yaml_backtest_data_section_loads() -> None:
    from arc.backtest.ranking import load_ranking_file

    d = load_ranking_file().backtest.data
    assert d.provider == "thetadata" and d.marks == "quoted"
    assert d.max_quoted_spread_pct == 0.35 and d.quoted_fallback == "drop"
    assert d.rate_source == "flat" and d.pit is True
    assert d.earnings.earn_projection_days == 91 and d.earnings.earn_market_ref == "SPY"
    assert d.screen.min_price == 10.0 and d.screen.min_atm_open_interest == 500
    with pytest.raises(ValueError, match="extra"):
        DataSettings(bogus=1)  # type: ignore[call-arg]


def test_backtest_data_knobs_are_classified_not_exposed() -> None:
    from arc.backtest.ranking import BacktestSettings
    from arc.control.registry import NOT_EXPOSED_RANKING_SECTIONS, TunableError, lookup

    assert "backtest.data" in NOT_EXPOSED_RANKING_SECTIONS
    assert "data" in BacktestSettings.model_fields
    for leaf in DataSettings.model_fields:
        with pytest.raises(TunableError):
            lookup(f"backtest.data.{leaf}")
