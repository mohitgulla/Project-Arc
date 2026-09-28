"""Tests for arc.backtest — cost model, chain prep, leg selection, engine, metrics, report.

Hand-computed examples carry their derivation in comments. Synthetic chains are
priced with BSM at a flat vol so implied vols / deltas are known exactly.
"""

from __future__ import annotations

import datetime as dt
import math

import numpy as np
import pandas as pd
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from arc.backtest import (
    CostModel,
    ExpiryMode,
    StrategyKind,
    StrategySpec,
    compute_metrics,
    prepare_chains,
    run_backtest,
    trades_frame,
    walk_forward_eval,
    walk_forward_splits,
)
from arc.backtest.chain import implied_vol_vec, prepare_chain
from arc.backtest.engine import open_trade, settle
from arc.backtest.metrics import breakdown, max_drawdown, monthly_pnl, walk_forward_oos
from arc.backtest.regime import label_trend, label_vol
from arc.backtest.report import baseline_specs, d4_specs, format_table, run_report
from arc.backtest.strategies import LegPick, pick_expirations, select_legs
from arc.backtest.underlying import UnderlyingStore, load_closes
from arc.data.history.base import OptionEodRow, OptionRight, occ_symbol
from arc.data.history.store import ParquetHistoryStore
from arc.pricing.bs import price_vectorized
from arc.utils.calendar import sessions_between

R = 0.04
VOL = 0.20

# ---------------------------------------------------------------------------
# Synthetic data helpers
# ---------------------------------------------------------------------------


def _fridays(start: dt.date, end: dt.date) -> list[dt.date]:
    d = start + dt.timedelta(days=(4 - start.weekday()) % 7)
    out = []
    while d <= end:
        out.append(d)
        d += dt.timedelta(days=7)
    return out


def synth_rows(
    day: dt.date, spot: float, *, underlying: str = "TST", quoted: bool = False
) -> list[OptionEodRow]:
    """BSM-priced EOD rows: weekly expiries 1–60 DTE, $1 strikes ±25% around spot."""
    rows: list[OptionEodRow] = []
    exps = [e for e in _fridays(day + dt.timedelta(days=1), day + dt.timedelta(days=60))]
    strikes = np.arange(math.floor(spot * 0.75), math.ceil(spot * 1.25) + 1, 1.0)
    for e in exps:
        t = (e - day).days / 365.0
        for right, flag in ((OptionRight.CALL, "c"), (OptionRight.PUT, "p")):
            px = price_vectorized(
                np.full(len(strikes), flag), spot, strikes, t, R, np.full(len(strikes), VOL)
            )
            for k, p in zip(strikes, px, strict=True):
                p = round(float(p), 2)
                if p < 0.05:
                    continue
                extra = {"bid": round(p * 0.98, 2), "ask": round(p * 1.02, 2)} if quoted else {}
                rows.append(
                    OptionEodRow(
                        provider="synth",
                        underlying=underlying,
                        date=day,
                        symbol=occ_symbol(underlying, e, right, float(k)),
                        expiration=e,
                        strike=float(k),
                        right=right,
                        close=p,
                        volume=10.0,
                        **extra,
                    )
                )
    return rows


def spot_path(days: list[dt.date], s0: float = 100.0, drift: float = 0.0005) -> pd.Series:
    """Deterministic mildly-trending path with a wiggle (so regimes vary)."""
    vals = [s0 * math.exp(drift * i + 0.03 * math.sin(i / 7.0)) for i in range(len(days))]
    return pd.Series(vals, index=days, dtype=float)


@pytest.fixture(scope="module")
def synth_env(tmp_path_factory: pytest.TempPathFactory) -> dict[str, object]:
    root = tmp_path_factory.mktemp("bt")
    all_days = sessions_between(dt.date(2024, 1, 2), dt.date(2024, 9, 30))
    closes = spot_path(all_days)
    entry_days = [d for d in all_days if dt.date(2024, 3, 1) <= d <= dt.date(2024, 6, 28)]
    store = ParquetHistoryStore(root)
    for d in entry_days:
        store.write_day("synth", "TST", d, synth_rows(d, float(closes[d])))
    return {
        "root": root,
        "store": store,
        "closes": closes,
        "entry_days": entry_days,
    }


# ---------------------------------------------------------------------------
# CostModel
# ---------------------------------------------------------------------------


class TestCostModel:
    def test_fill_hand_values(self) -> None:
        c = CostModel(slippage_frac=0.25)
        # mid 2.00, spread 0.20: buy 2.00 + 0.25*0.20 = 2.05; sell 1.95
        assert c.fill(2.0, 0.2, +1) == pytest.approx(2.05)
        assert c.fill(2.0, 0.2, -1) == pytest.approx(1.95)
        # sells never go below zero
        assert c.fill(0.01, 0.2, -1) == 0.0

    def test_fill_rejects_bad_side(self) -> None:
        with pytest.raises(ValueError, match="side"):
            CostModel().fill(1.0, 0.1, 0)

    def test_spread_quote_vs_estimate(self) -> None:
        c = CostModel(spread_pct=0.04, spread_min=0.03)
        assert c.spread(2.0, bid=1.9, ask=2.1) == pytest.approx(0.2)
        assert c.spread(2.0) == pytest.approx(0.08)  # 4% of 2.00
        assert c.spread(0.5) == pytest.approx(0.03)  # floor beats 0.02
        # crossed / NaN quotes fall back to the estimate
        assert c.spread(2.0, bid=2.2, ask=2.1) == pytest.approx(0.08)
        assert c.spread(2.0, bid=float("nan"), ask=2.1) == pytest.approx(0.08)
        assert c.spread(2.0, bid=0.0, ask=0.0) == pytest.approx(0.08)

    def test_mid(self) -> None:
        c = CostModel()
        assert c.mid(5.0, bid=1.0, ask=2.0) == pytest.approx(1.5)
        assert c.mid(5.0) == 5.0
        with pytest.raises(ValueError, match="neither"):
            c.mid(None)
        with pytest.raises(ValueError, match="neither"):
            c.mid(float("nan"))

    def test_fees(self) -> None:
        assert CostModel(commission_per_contract=0.65).fees(4) == pytest.approx(2.6)

    def test_validation(self) -> None:
        with pytest.raises(ValueError):
            CostModel(slippage_frac=1.5)
        with pytest.raises(ValueError):
            CostModel(commission_per_contract=-1)

    @given(
        mid=st.floats(0.01, 500),
        spread=st.floats(0, 5),
        x=st.floats(0, 1),
    )
    @settings(max_examples=200, deadline=None)
    def test_buy_ge_mid_ge_sell(self, mid: float, spread: float, x: float) -> None:
        c = CostModel(slippage_frac=x)
        buy, sell = c.fill(mid, spread, +1), c.fill(mid, spread, -1)
        assert buy >= mid - 1e-12 >= sell - 2e-12
        assert buy - sell <= 2 * x * spread + 1e-9


# ---------------------------------------------------------------------------
# Chain preparation
# ---------------------------------------------------------------------------


class TestChain:
    @given(
        k=st.floats(60, 140),
        t_days=st.integers(5, 90),
        sigma=st.floats(0.08, 1.2),
        is_call=st.booleans(),
    )
    @settings(max_examples=150, deadline=None)
    def test_iv_round_trip(self, k: float, t_days: int, sigma: float, is_call: bool) -> None:
        flag = np.array(["c" if is_call else "p"])
        t = np.array([t_days / 365.0])
        p = price_vectorized(flag, 100.0, np.array([k]), t, R, np.array([sigma]))
        p_floor = price_vectorized(flag, 100.0, np.array([k]), t, R, np.array([0.01]))
        if p[0] - p_floor[0] < 1e-3:  # no time value (deep ITM/OTM): IV ill-conditioned
            return
        iv = implied_vol_vec(p, 100.0, np.array([k]), t, R, 0.0, flag)
        assert iv[0] == pytest.approx(sigma, abs=1e-4)

    def test_iv_nan_outside_bounds(self) -> None:
        # price below intrinsic for a deep ITM call → not bracketed
        iv = implied_vol_vec(
            np.array([1.0]), 100.0, np.array([80.0]), np.array([0.1]), R, 0.0, np.array(["c"])
        )
        assert math.isnan(iv[0])

    def test_prepare_chain_columns_and_delta(self) -> None:
        day = dt.date(2024, 3, 1)
        rows = pd.DataFrame([r.model_dump() for r in synth_rows(day, 100.0)])
        rows["right"] = rows["right"].astype(str)
        ch = prepare_chain(rows, 100.0, cost=CostModel(), r=R)
        assert {"mid", "spread", "dte", "iv", "delta", "quoted"} <= set(ch.columns)
        assert (ch["dte"] > 0).all()
        assert not ch["quoted"].any()
        ok = ch[np.isfinite(ch["iv"]) & (ch["mid"] > 0.5)]
        # rounding closes to cents moves IV slightly; flat 20% vol should come back
        assert ok["iv"].median() == pytest.approx(VOL, abs=0.005)
        calls = ok[ok["right"] == "call"]
        puts = ok[ok["right"] == "put"]
        assert (calls["delta"] > 0).all() and (calls["delta"] < 1).all()
        assert (puts["delta"] < 0).all() and (puts["delta"] > -1).all()

    def test_prepare_chain_uses_quotes(self) -> None:
        day = dt.date(2024, 3, 1)
        rows = pd.DataFrame([r.model_dump() for r in synth_rows(day, 100.0, quoted=True)])
        rows["right"] = rows["right"].astype(str)
        ch = prepare_chain(rows, 100.0, cost=CostModel(), r=R)
        assert ch["quoted"].all()
        np.testing.assert_allclose(ch["spread"], ch["ask"] - ch["bid"])

    def test_prepare_chain_empty(self) -> None:
        empty = pd.DataFrame(columns=["date", "expiration", "strike", "right", "close"])
        assert prepare_chain(empty, 100.0, cost=CostModel(), r=R).empty


# ---------------------------------------------------------------------------
# Strategy specs & leg selection
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def chain_100() -> pd.DataFrame:
    day = dt.date(2024, 3, 1)
    rows = pd.DataFrame([r.model_dump() for r in synth_rows(day, 100.0)])
    rows["right"] = rows["right"].astype(str)
    return prepare_chain(rows, 100.0, cost=CostModel(), r=R)


class TestStrategies:
    def test_spec_validation_and_label(self) -> None:
        with pytest.raises(ValueError, match="dte_min"):
            StrategySpec(kind=StrategyKind.BULL_PUT, dte_min=50, dte_max=40)
        s = StrategySpec(kind=StrategyKind.IRON_CONDOR, delta=0.16)
        assert s.label == "iron_condor_d16_w2_dte30-45"
        assert (
            StrategySpec(kind=StrategyKind.LONG_CALL, delta=0.3).label == "long_call_d30_dte30-45"
        )

    def test_pick_expirations(self, chain_100: pd.DataFrame) -> None:
        near = StrategySpec(kind=StrategyKind.BULL_PUT)
        (e,) = pick_expirations(chain_100, near)
        # 2024-03-01 → Fridays at 35 and 42 DTE are in 30–45; 37.5 midpoint → 35 (tie → earlier)
        assert (e - dt.date(2024, 3, 1)).days in (35, 42)
        allx = near.model_copy(update={"expiry_mode": ExpiryMode.ALL})
        got = pick_expirations(chain_100, allx)
        assert all(30 <= (x - dt.date(2024, 3, 1)).days <= 45 for x in got)
        assert len(got) == 2
        none = near.model_copy(update={"dte_min": 200, "dte_max": 300})
        assert pick_expirations(chain_100, none) == []
        assert pick_expirations(chain_100.iloc[0:0], near) == []

    @pytest.mark.parametrize("kind", list(StrategyKind))
    def test_select_legs_geometry(self, chain_100: pd.DataFrame, kind: StrategyKind) -> None:
        spec = StrategySpec(kind=kind, delta=0.20, width_pct=0.03)
        (exp,) = pick_expirations(chain_100, spec)
        legs = select_legs(chain_100, spec, 100.0, exp)
        assert legs is not None
        shorts = [lg for lg in legs if lg.side < 0]
        longs = [lg for lg in legs if lg.side > 0]
        for lg in shorts or longs:
            assert abs(abs(lg.delta) - 0.20) <= spec.delta_tol
        if kind is StrategyKind.LONG_CALL:
            assert [(lg.right, lg.side) for lg in legs] == [("call", 1)]
        elif kind is StrategyKind.LONG_PUT:
            assert [(lg.right, lg.side) for lg in legs] == [("put", 1)]
        elif kind is StrategyKind.BULL_PUT:
            assert longs[0].strike < shorts[0].strike and longs[0].right == "put"
        elif kind is StrategyKind.BEAR_CALL:
            assert longs[0].strike > shorts[0].strike and longs[0].right == "call"
        elif kind is StrategyKind.BULL_CALL:
            assert longs[0].strike < shorts[0].strike and longs[0].right == "call"
        elif kind is StrategyKind.BEAR_PUT:
            assert longs[0].strike > shorts[0].strike and longs[0].right == "put"
        else:
            lp, sp, lc, sc = legs  # [put wing, short put, call wing, short call]
            assert lp.strike < sp.strike < sc.strike < lc.strike
        for lg in shorts:
            w = min(abs(lg.strike - lo.strike) for lo in longs if lo.right == lg.right)
            assert 1.5 <= w <= 6.0  # 3% of 100 within [0.5, 2]x

    def test_select_legs_none_when_untradable(self, chain_100: pd.DataFrame) -> None:
        spec = StrategySpec(kind=StrategyKind.BULL_PUT, delta=0.2, delta_tol=0.0001)
        (exp,) = pick_expirations(chain_100, spec)
        # extremely tight tolerance: nothing within 0.0001 of 0.20 on a $1 grid (probably)
        tight = select_legs(chain_100, spec, 100.0, exp)
        if tight is not None:
            assert abs(abs(tight[1].delta) - 0.2) <= 0.0001
        # min_volume above every row
        vol = spec.model_copy(update={"min_volume": 1e9, "delta_tol": 0.05})
        assert select_legs(chain_100, vol, 100.0, exp) is None
        # wing wider than any strike beyond the anchor
        wide = spec.model_copy(update={"width_pct": 5.0, "delta_tol": 0.05})
        assert select_legs(chain_100, wide, 100.0, exp) is None
        for k in StrategyKind:
            assert select_legs(chain_100, StrategySpec(kind=k), 100.0, dt.date(2030, 1, 1)) is None

    def test_iron_condor_rejects_crossed_shorts(self, chain_100: pd.DataFrame) -> None:
        # |Δ| 0.6 puts are above spot and |Δ| 0.6 calls are below → shorts cross
        spec = StrategySpec(kind=StrategyKind.IRON_CONDOR, delta=0.6, delta_tol=0.05)
        (exp,) = pick_expirations(chain_100, spec)
        assert select_legs(chain_100, spec, 100.0, exp) is None


# ---------------------------------------------------------------------------
# Engine: open / settle (hand-computed)
# ---------------------------------------------------------------------------


def _pick(right: str, k: float, side: int, mid: float, spread: float) -> LegPick:
    return LegPick(
        symbol=occ_symbol("TST", dt.date(2024, 4, 19), OptionRight(right), k),
        right=right,
        strike=k,
        expiration=dt.date(2024, 4, 19),
        side=side,
        mid=mid,
        spread=spread,
        delta=0.2 * side,
        iv=0.2,
    )


class TestEngine:
    cost = CostModel(slippage_frac=0.5, commission_per_contract=0.65)
    spec = StrategySpec(kind=StrategyKind.BULL_PUT)

    def _bull_put(self):  # type: ignore[no-untyped-def]
        # short 95P mid 1.50 spread .10 → sell @ 1.45; long 90P mid .50 spread .10 → buy @ .55
        picks = [_pick("put", 90, +1, 0.50, 0.10), _pick("put", 95, -1, 1.50, 0.10)]
        o = open_trade(
            picks,
            spec=self.spec,
            underlying="TST",
            entry_date=dt.date(2024, 3, 15),
            spot=100.0,
            cost=self.cost,
        )
        assert o is not None
        return o

    def test_open_max_loss(self) -> None:
        o = self._bull_put()
        # credit at fills = 1.45 - .55 = .90; max loss = (5 - .90)*100 = 410 + 2*.65 fees = 411.30
        assert o.max_loss == pytest.approx(411.30)
        assert o.open_fees == pytest.approx(1.30)
        assert o.dte == 35

    def test_settle_otm_win(self) -> None:
        t = settle(self._bull_put(), 101.0, self.cost)
        # both expire worthless: +90 credit − 1.30 fees; mid ref: +100
        assert t.pnl == pytest.approx(88.70)
        assert t.pnl_mid == pytest.approx(100.0)
        assert t.cost == pytest.approx(11.30)
        assert t.entry_net == pytest.approx(-0.90)
        assert t.ror == pytest.approx(88.70 / 411.30)

    def test_settle_max_loss(self) -> None:
        t = settle(self._bull_put(), 80.0, self.cost)
        # value = +10 − 15 = −5; pnl = (−5 + .90)*100 − (1.30 + 2*.65) = −412.60
        assert t.pnl == pytest.approx(-412.60)
        assert t.max_loss == pytest.approx(412.60)  # worst case incl. closing fees
        assert t.ror == pytest.approx(-1.0)

    def test_settle_between_strikes(self) -> None:
        t = settle(self._bull_put(), 93.0, self.cost)
        # short 95P ITM by 2 → value −2; pnl = (−2 + .90)*100 − (1.30 + .65) = −111.95
        assert t.pnl == pytest.approx(-111.95)

    def test_open_rejects_non_positive_risk(self) -> None:
        # stale closes: long wing priced below zero-width arbitrage → max loss ≤ 0
        picks = [_pick("put", 90, +1, 0.0, 0.0), _pick("put", 95, -1, 6.0, 0.0)]
        o = open_trade(
            picks,
            spec=self.spec,
            underlying="TST",
            entry_date=dt.date(2024, 3, 15),
            spot=100.0,
            cost=CostModel(slippage_frac=0, commission_per_contract=0),
        )
        assert o is None

    def test_open_rejects_undefined_risk(self) -> None:
        with pytest.raises(ValueError, match="defined-risk"):
            open_trade(
                [_pick("put", 95, -1, 1.5, 0.1)],
                spec=self.spec,
                underlying="TST",
                entry_date=dt.date(2024, 3, 15),
                spot=100.0,
                cost=self.cost,
            )

    def test_long_call_settle(self) -> None:
        o = open_trade(
            [_pick("call", 105, +1, 1.00, 0.10)],
            spec=StrategySpec(kind=StrategyKind.LONG_CALL),
            underlying="TST",
            entry_date=dt.date(2024, 3, 15),
            spot=100.0,
            cost=self.cost,
        )
        assert o is not None
        t = settle(o, 110.0, self.cost)
        # buy @ 1.05; value 5 → (5 − 1.05)*100 − (.65 + .65) = 393.70
        assert t.pnl == pytest.approx(393.70)
        assert o.max_loss == pytest.approx(105.65)


class TestRunBacktest:
    def test_end_to_end_synthetic(self, synth_env: dict[str, object]) -> None:
        store: ParquetHistoryStore = synth_env["store"]  # type: ignore[assignment]
        closes: pd.Series = synth_env["closes"]  # type: ignore[assignment]
        hist = store.read("synth", "TST")
        cost = CostModel()
        chains = prepare_chains(hist, closes, cost=cost, r=R)
        assert set(chains) == set(synth_env["entry_days"])  # type: ignore[arg-type]
        specs = d4_specs((0.20,))
        trades = run_backtest(chains, closes, specs, underlying="TST", cost=cost)
        df = trades_frame(trades)
        n_days = len(chains)
        # nearly every (day, spec) pair trades on a clean synthetic chain
        assert len(df) >= 0.9 * n_days * len(specs)
        assert set(df["kind"]) == {str(k) for k in StrategyKind}
        assert (df["max_loss"] > 0).all()
        assert (df["pnl"] >= -df["max_loss"] - 1e-6).all()  # never lose more than max loss
        assert (df["pnl"] <= df["pnl_mid"] + 1e-9).all()  # costs only ever hurt
        assert set(df["trend"]) <= {"bull", "bear", "sideways", "unknown"}
        assert (df["entry_date"] < df["expiration"]).all()
        # settlement uses the close on the expiry date
        row = df.iloc[0]
        assert row["spot_exit"] == pytest.approx(float(closes[row["expiration"]]))

    def test_open_trades_past_data_are_dropped(self, synth_env: dict[str, object]) -> None:
        store: ParquetHistoryStore = synth_env["store"]  # type: ignore[assignment]
        closes: pd.Series = synth_env["closes"]  # type: ignore[assignment]
        cost = CostModel()
        chains = prepare_chains(store.read("synth", "TST"), closes, cost=cost, r=R)
        short = closes[pd.Index(closes.index) <= dt.date(2024, 5, 15)]
        spec = [StrategySpec(kind=StrategyKind.LONG_CALL)]
        days = [d for d in chains if d <= dt.date(2024, 5, 15)]
        trades = run_backtest(
            {d: chains[d] for d in days}, short, spec, underlying="TST", cost=cost
        )
        assert all(t.expiration <= dt.date(2024, 5, 15) for t in trades)
        explicit = run_backtest(
            chains, closes, spec, underlying="TST", cost=cost, entry_dates=days[:3]
        )
        assert {t.entry_date for t in explicit} <= set(days[:3])

    def test_prepare_chains_skips_days_without_spot(self) -> None:
        day = dt.date(2024, 3, 1)
        rows = pd.DataFrame([r.model_dump() for r in synth_rows(day, 100.0)])
        rows["right"] = rows["right"].astype(str)
        assert prepare_chains(rows, pd.Series(dtype=float), cost=CostModel(), r=R) == {}
        assert prepare_chains(rows.iloc[0:0], pd.Series(dtype=float), cost=CostModel(), r=R) == {}


# ---------------------------------------------------------------------------
# Metrics & walk-forward
# ---------------------------------------------------------------------------


def _tf(rows: list[tuple[str, str, str, float, float]]) -> pd.DataFrame:
    """(spec, entry, expiry, pnl, max_loss) → minimal trades frame."""
    return pd.DataFrame(
        [
            {
                "spec": s,
                "kind": s,
                "entry_date": dt.date.fromisoformat(e),
                "expiration": dt.date.fromisoformat(x),
                "pnl": p,
                "pnl_mid": p + 5.0,
                "entry_net_mid": -1.0,
                "max_loss": ml,
                "trend": "sideways",
            }
            for s, e, x, p, ml in rows
        ]
    )


class TestMetrics:
    def test_max_drawdown_hand(self) -> None:
        # equity 10, 5, 25, 5, 15 → peak 25 → trough 5 → dd 20
        assert max_drawdown([10, -5, 20, -20, 10]) == 20
        assert max_drawdown([-3, -4]) == 7  # from the 0 start
        assert max_drawdown([]) == 0

    @given(st.lists(st.floats(-1000, 1000), max_size=40))
    @settings(max_examples=200, deadline=None)
    def test_max_drawdown_brute_force(self, xs: list[float]) -> None:
        eq = np.concatenate([[0.0], np.cumsum(xs)])
        brute = max((eq[i] - eq[j] for i in range(len(eq)) for j in range(i, len(eq))), default=0)
        assert max_drawdown(xs) == pytest.approx(max(brute, 0.0), abs=1e-6)

    def test_compute_metrics_hand(self) -> None:
        df = _tf(
            [
                ("a", "2024-01-02", "2024-02-16", 100, 400),
                ("a", "2024-01-03", "2024-02-16", -300, 400),
                ("a", "2024-01-04", "2024-03-15", 50, 400),
            ]
        )
        m = compute_metrics(df)
        assert m.trades == 3
        assert m.win_rate == pytest.approx(2 / 3)
        assert m.profit_factor == pytest.approx(150 / 300)
        assert m.total_pnl == -150
        assert m.avg_ror == pytest.approx((0.25 - 0.75 + 0.125) / 3)
        assert m.avg_cost == pytest.approx(5.0)
        assert m.cost_pct_premium == pytest.approx(15 / 300 * 100)
        # order by expiry: +100, −300, +50 → dd 300
        assert m.max_dd == 300
        assert m.worst_month == "2024-02"
        assert m.worst_month_pnl == -200

    def test_compute_metrics_edge(self) -> None:
        empty = compute_metrics(_tf([]).reindex(columns=["pnl", "pnl_mid", "max_loss"]))
        assert empty.trades == 0 and math.isnan(empty.win_rate)
        allwin = compute_metrics(_tf([("a", "2024-01-02", "2024-02-16", 10, 100)]))
        assert math.isinf(allwin.profit_factor)
        zero_prem = _tf([("a", "2024-01-02", "2024-02-16", 10, 100)]).assign(entry_net_mid=0.0)
        assert math.isnan(compute_metrics(zero_prem).cost_pct_premium)

    def test_monthly_and_breakdown(self) -> None:
        df = _tf(
            [
                ("a", "2024-01-02", "2024-02-16", 100, 400),
                ("b", "2024-01-03", "2024-03-15", -30, 400),
            ]
        )
        mp = monthly_pnl(df)
        assert list(mp.index) == ["2024-02", "2024-03"]
        assert monthly_pnl(df.iloc[0:0]).empty
        b = breakdown(df, "spec")
        assert list(b["spec"]) == ["a", "b"] and list(b["trades"]) == [1, 1]
        b2 = breakdown(df, ["spec", "trend"])
        assert list(b2.columns[:2]) == ["spec", "trend"]

    def test_walk_forward_splits(self) -> None:
        sp = walk_forward_splits(dt.date(2024, 2, 1), dt.date(2025, 3, 21))
        assert sp[0].train_start == dt.date(2024, 2, 1)
        assert sp[0].train_end == dt.date(2024, 7, 31)
        assert sp[0].test_start == dt.date(2024, 8, 1)
        assert sp[0].test_end == dt.date(2024, 9, 30)
        assert sp[-1].test_end == dt.date(2025, 3, 21)
        for a, b in zip(sp, sp[1:], strict=False):
            assert b.test_start == a.test_end + dt.timedelta(days=1)  # tiled
        for s in sp:
            assert s.train_end < s.test_start  # no overlap
        with pytest.raises(ValueError):
            walk_forward_splits(dt.date(2024, 1, 1), dt.date(2024, 12, 31), train_months=0)
        assert walk_forward_splits(dt.date(2024, 1, 1), dt.date(2024, 3, 1)) == []

    @given(
        start=st.dates(dt.date(2020, 1, 1), dt.date(2026, 1, 1)),
        span=st.integers(0, 1500),
        tr=st.integers(1, 12),
        te=st.integers(1, 6),
    )
    @settings(max_examples=100, deadline=None)
    def test_walk_forward_splits_property(
        self, start: dt.date, span: int, tr: int, te: int
    ) -> None:
        end = start + dt.timedelta(days=span)
        for s in walk_forward_splits(start, end, train_months=tr, test_months=te):
            assert start <= s.train_start <= s.train_end < s.test_start <= s.test_end <= end

    def test_walk_forward_eval_no_lookahead(self) -> None:
        split = walk_forward_splits(dt.date(2024, 1, 1), dt.date(2024, 4, 30), train_months=2)[0]
        rows = []
        # 'good' looks great only via a trade expiring AFTER train end → must be ignored
        rows += [("good", "2024-01-05", "2024-01-26", -10, 100)] * 10
        rows += [("good", "2024-02-20", "2024-03-15", 10_000, 100)]
        rows += [("ok", "2024-01-05", "2024-01-26", 5, 100)] * 10
        rows += [("ok", "2024-03-05", "2024-04-19", 7, 100)] * 3
        rows += [("thin", "2024-01-05", "2024-01-26", 50, 100)] * 2  # < min_train_trades
        out = walk_forward_eval(_tf(rows), [split], min_train_trades=10)
        assert out.loc[0, "chosen"] == "ok"
        assert out.loc[0, "oos_trades"] == 3
        assert out.loc[0, "oos_total_pnl"] == 21
        oos = walk_forward_oos(_tf(rows), out)
        assert len(oos) == 3 and set(oos["spec"]) == {"ok"}
        assert walk_forward_oos(_tf(rows), out.assign(chosen=None)).empty

    def test_walk_forward_eval_no_eligible(self) -> None:
        split = walk_forward_splits(dt.date(2024, 1, 1), dt.date(2024, 4, 30), train_months=2)[0]
        out = walk_forward_eval(_tf([("a", "2024-01-05", "2024-01-26", 5, 100)]), [split])
        assert out.loc[0, "chosen"] is None


# ---------------------------------------------------------------------------
# Regime labels
# ---------------------------------------------------------------------------


class TestRegime:
    def test_trend_labels(self) -> None:
        idx = list(range(30))
        up = pd.Series([100 * 1.01**i for i in idx], index=idx, dtype=float)
        lab = label_trend(up)
        assert (lab.iloc[:20] == "unknown").all()
        assert (lab.iloc[20:] == "bull").all()  # 1.01^20 − 1 ≈ 22%
        down = pd.Series([100 * 0.99**i for i in idx], index=idx, dtype=float)
        assert (label_trend(down).iloc[20:] == "bear").all()
        flat = pd.Series([100.0] * 30, index=idx)
        assert (label_trend(flat).iloc[20:] == "sideways").all()

    def test_vol_labels(self) -> None:
        idx = list(range(40))
        flat = pd.Series([100.0] * 40, index=idx)
        assert (label_vol(flat).iloc[21:] == "low").all()
        zig = pd.Series([100 * (1.03 if i % 2 else 1.0) for i in idx], index=idx)
        assert (label_vol(zig).iloc[21:] == "high").all()
        assert (label_vol(flat).iloc[:20] == "unknown").all()
        mid = pd.Series([100 * (1.0095 if i % 2 else 1.0) for i in idx], index=idx)
        assert (label_vol(mid).iloc[21:] == "mid").all()  # ≈ 15% annualised

    @given(cut=st.integers(25, 59))
    @settings(max_examples=30, deadline=None)
    def test_no_lookahead(self, cut: int) -> None:
        rng = np.random.default_rng(0)
        s = pd.Series(100 * np.exp(np.cumsum(rng.normal(0, 0.01, 60))))
        full_t, full_v = label_trend(s), label_vol(s)
        part_t, part_v = label_trend(s.iloc[:cut]), label_vol(s.iloc[:cut])
        assert (full_t.iloc[:cut] == part_t).all()
        assert (full_v.iloc[:cut] == part_v).all()


# ---------------------------------------------------------------------------
# Underlying closes cache
# ---------------------------------------------------------------------------


class _FakeSource:
    def __init__(self, s: pd.Series) -> None:
        self.s = s
        self.calls = 0

    def daily_closes(self, symbol: str, start: dt.date, end: dt.date) -> pd.Series:
        self.calls += 1
        i = pd.Index(self.s.index)
        return self.s[(i >= start) & (i <= end)]


class TestUnderlying:
    def test_load_and_cache(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        days = sessions_between(dt.date(2024, 1, 2), dt.date(2024, 2, 29))
        src = _FakeSource(spot_path(days))
        us = UnderlyingStore(tmp_path)
        assert us.read("TST").empty
        a = load_closes(us, "TST", days[0], days[-1], source=src)
        assert len(a) == len(days) and src.calls == 1
        b = load_closes(us, "TST", days[5], days[10], source=src)  # served from cache
        assert src.calls == 1 and len(b) == 6
        assert b.iloc[0] == pytest.approx(a.iloc[5])
        c = load_closes(us, "TST", days[0], dt.date(2024, 3, 29))  # no source: partial
        assert len(c) == len(days)


# ---------------------------------------------------------------------------
# Report + CLI
# ---------------------------------------------------------------------------


class TestReport:
    def test_specs(self) -> None:
        d4 = d4_specs()
        assert len(d4) == 7 * 4
        assert all(s.dte_min == 30 and s.dte_max == 45 for s in d4)
        assert {s.delta for s in d4} == {0.16, 0.20, 0.25, 0.30}
        base = baseline_specs()
        assert all(s.expiry_mode is ExpiryMode.ALL and s.min_volume == 0 for s in base)

    def test_format_table(self) -> None:
        df = pd.DataFrame({"a": [1.23456, float("nan"), float("inf"), 12345.6], "b": list("wxyz")})
        t = format_table(df)
        assert "1.235" in t and "–" in t and "∞" in t and "12,346" in t
        assert format_table(df.iloc[0:0]) == "_no trades_\n"

    def test_run_report_synthetic(self, synth_env: dict[str, object], tmp_path) -> None:  # type: ignore[no-untyped-def]
        out = run_report(
            store=synth_env["store"],  # type: ignore[arg-type]
            closes_by_ticker={"TST": synth_env["closes"]},  # type: ignore[dict-item]
            tickers=["TST"],
            start=dt.date(2024, 3, 1),
            end=dt.date(2024, 6, 28),
            out_dir=tmp_path,
            provider="synth",
            train_months=2,
            test_months=1,
        )
        assert len(out["baseline"]) > len(out["d4"]) / 4  # baseline maximises trade count
        assert len(out["d4"]) > 0
        assert not out["walk_forward"].empty
        assert set(out["sensitivity"]["slippage_x"]) == {0.0, 0.25, 0.5}
        # more friction never helps
        s = out["sensitivity"].groupby("slippage_x")["avg_pnl"].mean()
        assert s[0.0] >= s[0.5]
        text = (tmp_path / "report.md").read_text()
        for h in ("Baseline", "D4 grid", "trend regime", "vol regime", "worst months", "Walk"):
            assert h in text
        assert (tmp_path / "d4_trades.csv").is_file()

    def test_run_report_empty(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        out = run_report(
            store=ParquetHistoryStore(tmp_path),
            closes_by_ticker={"NONE": pd.Series(dtype=float)},
            tickers=["NONE"],
            start=dt.date(2024, 3, 1),
            end=dt.date(2024, 6, 28),
            out_dir=tmp_path / "o",
            provider="synth",
            sensitivity=False,
        )
        assert out["d4"].empty
        assert "_no trades_" in (tmp_path / "o" / "report.md").read_text()

    def test_cli_offline(self, synth_env: dict[str, object], tmp_path) -> None:  # type: ignore[no-untyped-def]
        from arc.cli import main

        root = synth_env["root"]
        UnderlyingStore(root).write("TST", synth_env["closes"])  # type: ignore[arg-type]
        rc = main(
            [
                "backtest",
                "--tickers",
                "TST",
                "--provider",
                "synth",
                "--start",
                "2024-03-01",
                "--end",
                "2024-04-30",
                "--data-dir",
                str(root),
                "--out",
                str(tmp_path),
                "--offline",
                "--no-sensitivity",
            ]
        )
        assert rc == 0
        assert (tmp_path / "report.md").is_file()
