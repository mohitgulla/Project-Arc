"""Tests for E7.5: pure rankers (arc.scanner.rank) and the ranking backtest
(arc.backtest.ranking / rank_report).

Synthetic chains are BSM-priced at a flat vol, so every leg has a known IV and
delta and the menus are deterministic.
"""

from __future__ import annotations

import datetime as dt
import math
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from arc.backtest.costs import load_cost_model
from arc.backtest.engine import prepare_chains
from arc.backtest.rank_report import root_cause, run_rank_report
from arc.backtest.ranking import (
    BootstrapSpec,
    DecisionRule,
    RankRun,
    apply_stance,
    atm_iv,
    block_bootstrap_ci,
    build_menu,
    clean_chains,
    decide,
    load_ranking_file,
    rankers_for,
    remark_chains,
    smile_deviation,
    smile_marks,
    stance_kinds,
    subperiod_stats,
)
from arc.backtest.strategies import StrategyKind, StrategySpec
from arc.config import ArcSettings
from arc.data.history.base import OptionEodRow, OptionRight, occ_symbol
from arc.data.history.store import ParquetHistoryStore
from arc.exits.policy import load_exit_config
from arc.pricing.bs import price_vectorized
from arc.scanner.rank import (
    Ranker,
    RankFilters,
    RankInputs,
    applicable,
    incumbent_for,
    load_ranking_config,
    passes_filters,
    rank,
)
from arc.utils.calendar import sessions_between

REPO = Path(__file__).resolve().parent.parent
R = 0.04
VOL = 0.20


def _c(key: str, **kw: object) -> RankInputs:
    base: dict[str, object] = {
        "key": key,
        "credit": False,
        "vertical": True,
        "ev_proxy": 0.0,
        "managed_net_ev": 1.0,
        "managed_pop": 0.5,
    }
    base.update(kw)
    return RankInputs.model_validate(base)


# ---------------------------------------------------------------------------
# Rankers (pure)
# ---------------------------------------------------------------------------


def test_credit_width_ranks_credits_by_ratio_then_debits_by_ev_ratio() -> None:
    menu = [
        _c("debit_hi", ev_ratio=0.9),
        _c("cw_low", credit=True, credit_width=0.20, ev_proxy=50.0),
        _c("cw_hi", credit=True, credit_width=0.35, ev_proxy=-5.0),
        _c("debit_lo", ev_ratio=0.1),
    ]
    got = [c.key for c in rank(menu, Ranker.CREDIT_WIDTH)]
    assert got == ["cw_hi", "cw_low", "debit_hi", "debit_lo"]


def test_debit_width_puts_verticals_before_singles() -> None:
    menu = [
        _c("long_call", vertical=False, ev_ratio=5.0),
        _c("v_2x", debit_width=2.0),
        _c("v_4x", debit_width=4.0),
    ]
    assert [c.key for c in rank(menu, Ranker.DEBIT_WIDTH)] == ["v_4x", "v_2x", "long_call"]


def test_ev_rankers_order_and_drop_unknown() -> None:
    menu = [
        _c("a", ev_proxy=10.0, managed_net_ev=5.0, rorc_day=0.001),
        _c("b", ev_proxy=20.0, managed_net_ev=3.0, rorc_day=0.004),
        _c("c", ev_proxy=15.0, managed_net_ev=8.0, rorc_day=None),
    ]
    assert [c.key for c in rank(menu, Ranker.EV_PROXY)] == ["b", "c", "a"]
    assert [c.key for c in rank(menu, Ranker.MANAGED_NET_EV)] == ["c", "a", "b"]
    assert [c.key for c in rank(menu, Ranker.RORC_DAY)] == ["b", "a"]


def test_rorc_day_vrp_gate() -> None:
    menu = [
        _c("rich", rorc_day=0.001, vrp=0.03),
        _c("cheap", rorc_day=0.009, vrp=-0.01),
        _c("unknown", rorc_day=0.02, vrp=None),
    ]
    assert [c.key for c in rank(menu, Ranker.RORC_DAY_VRP)] == ["rich"]
    assert rank(menu, Ranker.RORC_DAY_VRP, vrp_threshold=0.05) == []


def test_filters_apply_to_every_ranker() -> None:
    f = RankFilters(min_managed_net_ev=0.0, min_managed_pop=0.4)
    menu = [
        _c("neg", managed_net_ev=-1.0, rorc_day=0.1, vrp=1.0, debit_width=9.0),
        _c("lowpop", managed_pop=0.2, rorc_day=0.1, vrp=1.0, debit_width=9.0),
        _c("nomodel", managed_net_ev=None, rorc_day=0.1, vrp=1.0, debit_width=9.0),
        _c("ok", rorc_day=0.01, vrp=1.0, debit_width=1.0),
    ]
    for r in Ranker:
        assert [c.key for c in rank(menu, r, filters=f)] == ["ok"], r
    assert passes_filters(menu[0], RankFilters(enabled=False))


def test_applicable_and_incumbent() -> None:
    assert incumbent_for(allows_credit=True) is Ranker.CREDIT_WIDTH
    assert incumbent_for(allows_credit=False) is Ranker.DEBIT_WIDTH
    assert not applicable(Ranker.CREDIT_WIDTH, allows_credit=False)
    assert not applicable(Ranker.DEBIT_WIDTH, allows_credit=True)
    assert applicable(Ranker.RORC_DAY, allows_credit=False)
    got = rankers_for([Ranker.RORC_DAY, Ranker.CREDIT_WIDTH], allows_credit=False)
    assert got == [Ranker.DEBIT_WIDTH, Ranker.RORC_DAY]


_num = st.one_of(st.none(), st.floats(-1e3, 1e3, allow_nan=False))


@st.composite
def _menus(draw: st.DrawFn) -> list[RankInputs]:
    n = draw(st.integers(0, 8))
    out = []
    for i in range(n):
        credit = draw(st.booleans())
        out.append(
            RankInputs(
                key=f"k{i}",
                credit=credit,
                vertical=draw(st.booleans()),
                credit_width=draw(_num) if credit else None,
                debit_width=None if credit else draw(_num),
                ev_proxy=draw(st.floats(-1e3, 1e3, allow_nan=False)),
                ev_ratio=draw(_num),
                managed_net_ev=draw(_num),
                managed_pop=draw(st.one_of(st.none(), st.floats(0.0, 1.0))),
                rorc_day=draw(_num),
                vrp=draw(_num),
            )
        )
    return out


@settings(max_examples=200, deadline=None)
@given(menu=_menus(), ranker=st.sampled_from(list(Ranker)), seed=st.randoms())
def test_rank_is_deterministic_and_order_independent(
    menu: list[RankInputs], ranker: Ranker, seed: object
) -> None:
    f = RankFilters(min_managed_net_ev=-1e9)
    a = rank(menu, ranker, filters=f)
    shuffled = list(menu)
    seed.shuffle(shuffled)  # type: ignore[attr-defined]
    b = rank(shuffled, ranker, filters=f)
    assert [c.key for c in a] == [c.key for c in b]
    assert {c.key for c in a} <= {c.key for c in menu}
    assert all(passes_filters(c, f) for c in a)


def test_ranking_yaml_loads_and_does_not_change_live_default() -> None:
    cfg = load_ranking_config()
    assert set(cfg.rankers) == set(Ranker)
    f = load_ranking_file()
    assert {"margin", "cash_debit", "cash_long_only"} <= set(f.backtest.menus)
    # The live scanner / pipeline defaults stay on the incumbent (D25: owner flips it).
    exits = (REPO / "config" / "exits.yaml").read_text()
    assert "rank_menu_by" not in exits or "credit_width" in exits


# ---------------------------------------------------------------------------
# Helpers: smile, IV, bootstrap, root cause
# ---------------------------------------------------------------------------


def test_smile_deviation_flags_stale_leg_and_clean_drops_it() -> None:
    e = dt.date(2025, 3, 21)
    ivs = [0.20, 0.20, 0.20, 0.30, 0.20, 0.20, 0.20]
    ch = pd.DataFrame(
        {
            "expiration": [e] * 7,
            "right": ["put"] * 7,
            "strike": [95.0, 96, 97, 98, 99, 100, 101],
            "iv": ivs,
        }
    )
    dev = smile_deviation(ch)
    assert dev.iloc[3] == pytest.approx(0.5)
    assert abs(dev.iloc[0]) < 1e-12
    out = clean_chains({e: ch}, max_dev=0.15)
    assert 98.0 not in set(out[e]["strike"])
    assert len(clean_chains({e: ch}, max_dev=None)[e]) == 7


def test_smile_marks_repair_a_stale_close_and_stay_monotone() -> None:
    day, spot = dt.date(2024, 4, 15), 100.0
    cost = load_cost_model()
    raw = pd.DataFrame([r.model_dump() for r in _rows(day, spot)])
    ch = prepare_chains(raw, pd.Series([spot], index=[day]), cost=cost, r=R, dte_min=1, dte_max=70)[
        day
    ]
    exp = sorted(ch["expiration"].unique())[4]
    stale = (ch["expiration"] == exp) & (ch["right"] == "put") & (ch["strike"] == 95.0)
    true_mid = float(ch.loc[stale, "mid"].iloc[0])
    ch.loc[stale, "mid"] = true_mid * 1.6  # a stale print far above fair
    ch.loc[stale, "iv"] = 0.32
    sm = smile_marks(ch, spot, r=R, cost=cost)
    got = sm[(sm["expiration"] == exp) & (sm["right"] == "put") & (sm["strike"] == 95.0)]
    assert float(got["mid"].iloc[0]) == pytest.approx(true_mid, rel=0.03)
    assert float(got["iv"].iloc[0]) == pytest.approx(VOL, abs=0.005)
    for (_, right), g in sm.groupby(["expiration", "right"]):
        d = np.diff(g.sort_values("strike")["mid"].to_numpy())
        assert (d >= -1e-9).all() if right == "put" else (d <= 1e-9).all()
    # same session only: an unrelated session's chain cannot change these marks
    again = remark_chains({day: ch}, pd.Series([spot], index=[day]), r=R, cost=cost)[day]
    pd.testing.assert_frame_equal(again, sm)


def test_stance_kinds_and_apply_stance() -> None:
    k = stance_kinds("cash_debit")
    assert k["bull"] == {"bull_call", "long_call"}
    assert k["bear"] == {"bear_put", "long_put"}
    assert k["sideways"] == frozenset()
    m = stance_kinds("margin")
    assert m["sideways"] == {"iron_condor"} and m["bull"] == {"bull_put"}
    d1, d2, d3 = dt.date(2025, 1, 2), dt.date(2025, 1, 3), dt.date(2025, 1, 6)

    def cand(kind: str) -> object:
        return type("C", (), {"kind": kind})()

    menus = {d: [cand("bull_call"), cand("bear_put"), cand("long_put")] for d in (d1, d2, d3)}
    trend = pd.Series({d1: "bull", d2: "bear"})  # d3 unknown → no trade
    out = apply_stance(menus, trend, k)  # type: ignore[arg-type]
    assert [c.kind for c in out[d1]] == ["bull_call"]
    assert [c.kind for c in out[d2]] == ["bear_put", "long_put"]
    assert out[d3] == []


def test_atm_iv_nearest_strike_and_missing() -> None:
    e = dt.date(2025, 3, 21)
    ch = pd.DataFrame(
        {"expiration": [e] * 4, "strike": [99.0, 99.0, 101.0, 101.0], "iv": [0.2, 0.3, 0.5, 0.5]}
    )
    assert atm_iv(ch, e, 99.4) == pytest.approx(0.25)
    assert atm_iv(ch, dt.date(2025, 4, 1), 99.4) is None


def test_block_bootstrap_deterministic_and_brackets_point() -> None:
    x = np.random.default_rng(0).normal(10.0, 5.0, 300)
    a = block_bootstrap_ci(x, resamples=500, block=20, ci=0.9, seed=7)
    b = block_bootstrap_ci(x, resamples=500, block=20, ci=0.9, seed=7)
    assert a == b
    point, lo, hi = a
    assert lo < point < hi and lo > 0
    assert block_bootstrap_ci(np.array([]), resamples=100, block=5, ci=0.9, seed=1) == (0, 0, 0)


def _row(**kw: object) -> dict[str, object]:
    base: dict[str, object] = {
        "pnl": -100.0,
        "pnl_mid": -90.0,
        "atm_iv": 0.2,
        "days_held": 10,
        "spot_entry": 100.0,
        "spot_exit": 100.5,
        "exit_reason": "dte_exit",
    }
    base.update(kw)
    return base


def test_root_cause_heuristic() -> None:
    assert root_cause(_row(pnl=-50.0, pnl_mid=10.0))[0] == "execution_slippage"
    assert root_cause(_row(spot_exit=110.0))[0] == "regime_misread"
    assert root_cause(_row(exit_reason="stop"))[0] == "exit_management"
    assert root_cause(_row())[0] == "strike_selection"


def _run(name: str, daily: list[float], trends: list[str], pnls: list[float]) -> RankRun:
    days = sessions_between(dt.date(2025, 1, 2), dt.date(2025, 6, 30))[: len(daily)]
    eq = pd.Series(np.cumsum(daily) + 100_000.0, index=days)
    trades = pd.DataFrame(
        {
            "trend": trends,
            "pnl": pnls,
            "entry_date": days[: len(pnls)],
            "exit_date": days[: len(pnls)],
        }
    )
    return RankRun(
        ranker=name, profile="margin", slippage=0.25, trades=trades, equity=eq, skipped={}
    )


def test_decide_switch_rule() -> None:
    n = 120
    trends = ["bear", "sideways", "bull"]
    inc = _run("credit_width", [0.0] * n, trends, [-10.0, -10.0, -10.0])
    better = _run("rorc_day", [50.0 + (i % 3) for i in range(n)], trends, [10.0, 10.0, -20.0])
    worse = _run("ev_proxy", [-5.0] * n, trends, [-50.0, -50.0, -50.0])
    rule, boot = DecisionRule(), BootstrapSpec(resamples=300)
    table, verdict = decide(
        {"credit_width": inc, "rorc_day": better, "ev_proxy": worse},
        allows_credit=True,
        rule=rule,
        boot=boot,
    )
    t = table.set_index("challenger")
    assert bool(t.loc["rorc_day", "switch"]) and t.loc["rorc_day", "subperiods_won"] == 2
    assert not bool(t.loc["ev_proxy", "switch"])
    assert verdict.startswith("switch candidate: rorc_day")
    _, keep = decide(
        {"credit_width": inc, "ev_proxy": worse}, allows_credit=True, rule=rule, boot=boot
    )
    assert keep.startswith("keep credit_width")
    _, none = decide({"ev_proxy": worse}, allows_credit=True, rule=rule, boot=boot)
    assert "not run" in none
    stats = subperiod_stats(better, rule.subperiods)
    assert stats["bull"] == (-20.0, 20.0, 1)


# ---------------------------------------------------------------------------
# Menus (no look-ahead) and the end-to-end report on synthetic data
# ---------------------------------------------------------------------------


def _fridays(start: dt.date, end: dt.date) -> list[dt.date]:
    d = start + dt.timedelta(days=(4 - start.weekday()) % 7)
    out = []
    while d <= end:
        out.append(d)
        d += dt.timedelta(days=7)
    return out


def _rows(day: dt.date, spot: float) -> list[OptionEodRow]:
    rows: list[OptionEodRow] = []
    strikes = np.arange(math.floor(spot * 0.8), math.ceil(spot * 1.2) + 1, 1.0)
    for e in _fridays(day + dt.timedelta(days=1), day + dt.timedelta(days=60)):
        t = (e - day).days / 365.0
        for right, flag in ((OptionRight.CALL, "c"), (OptionRight.PUT, "p")):
            px = price_vectorized(
                np.full(len(strikes), flag), spot, strikes, t, R, np.full(len(strikes), VOL)
            )
            for k, p in zip(strikes, px, strict=True):
                p = round(float(p), 2)
                if p < 0.05:
                    continue
                rows.append(
                    OptionEodRow(
                        provider="synth",
                        underlying="TST",
                        date=day,
                        symbol=occ_symbol("TST", e, right, float(k)),
                        expiration=e,
                        strike=float(k),
                        right=right,
                        close=p,
                        volume=10.0,
                    )
                )
    return rows


@pytest.fixture(scope="module")
def synth(tmp_path_factory: pytest.TempPathFactory) -> dict[str, object]:
    root = tmp_path_factory.mktemp("rank")
    days = sessions_between(dt.date(2024, 1, 2), dt.date(2024, 6, 28))
    closes = pd.Series(
        [100.0 * math.exp(0.0006 * i + 0.03 * math.sin(i / 6.0)) for i in range(len(days))],
        index=days,
        dtype=float,
    )
    store = ParquetHistoryStore(root)
    opt_days = [d for d in days if d >= dt.date(2024, 4, 1)]
    for d in opt_days:
        store.write_day("synth", "TST", d, _rows(d, float(closes[d])))
    return {"root": root, "store": store, "closes": closes, "days": opt_days}


def test_build_menu_ignores_future_closes(synth: dict[str, object]) -> None:
    closes: pd.Series = synth["closes"]  # type: ignore[assignment]
    store: ParquetHistoryStore = synth["store"]  # type: ignore[assignment]
    day = dt.date(2024, 4, 15)
    cost = load_cost_model()
    raw = store.read("synth", "TST", day, day)
    chain = prepare_chains(raw, closes, cost=cost, r=R, dte_min=1, dte_max=70)[day]
    exits = load_exit_config()
    mc = exits.model.model_copy(update={"n_paths": 400})
    specs = [
        StrategySpec(kind=StrategyKind.BULL_PUT, dte_min=30, dte_max=45, delta=0.2),
        StrategySpec(kind=StrategyKind.LONG_CALL, dte_min=30, dte_max=45, delta=0.5),
    ]
    kw = {
        "day": day,
        "underlying": "TST",
        "specs": specs,
        "cost": cost,
        "exits": exits,
        "mc": mc,
        "r": R,
    }
    a = build_menu(chain, closes, **kw)  # type: ignore[arg-type]
    future = closes.copy()
    future[future.index > day] *= 3.0  # a crash / squeeze after the decision
    b = build_menu(chain, future, **kw)  # type: ignore[arg-type]
    assert a and [c.model_dump() for c in a] == [c.model_dump() for c in b]
    by_kind = {c.kind: c for c in a}
    assert by_kind["bull_put"].inputs.credit and by_kind["bull_put"].inputs.credit_width
    assert not by_kind["long_call"].inputs.credit and not by_kind["long_call"].inputs.vertical
    assert by_kind["bull_put"].atm_iv == pytest.approx(VOL, abs=0.01)
    assert build_menu(chain, closes[closes.index < day], **kw) == []  # type: ignore[arg-type]


def test_run_rank_report_end_to_end(synth: dict[str, object], tmp_path: Path) -> None:
    cfg = load_ranking_file()
    bt = cfg.backtest.model_copy(
        update={"n_paths": 300, "slippage_grid": [0.25], "bootstrap": BootstrapSpec(resamples=200)}
    )
    cfg = cfg.model_copy(
        update={
            "backtest": bt,
            # a fair-priced flat-vol chain has no edge after costs: turn the EV floor off
            # so both profiles trade (the filter itself is tested above)
            "ranking": cfg.ranking.model_copy(update={"filters": RankFilters(enabled=False)}),
        }
    )
    closes: pd.Series = synth["closes"]  # type: ignore[assignment]
    kw = {
        "store": synth["store"],
        "closes_by_ticker": {"TST": closes},
        "tickers": ["TST"],
        "start": dt.date(2024, 4, 1),
        "end": dt.date(2024, 5, 10),
        "profiles": ["margin", "cash_debit"],
        "rankers": list(Ranker),
        "cfg": cfg,
        "cost": load_cost_model(),
        "settings": ArcSettings(),
        "provider": "synth",
        "charts": False,
    }
    frames = run_rank_report(out_dir=tmp_path / "a", **kw)  # type: ignore[arg-type]
    s = frames["summary"]
    assert set(s[s.profile == "margin"]["ranker"]) == set(Ranker) - {Ranker.DEBIT_WIDTH}
    assert set(s[s.profile == "cash_debit"]["ranker"]) == set(Ranker) - {Ranker.CREDIT_WIDTH}
    t = frames["trades"]
    assert len(t) > 0
    # D18 sizing: every position's max loss within 5% of starting equity (equity only grows
    # by realised P&L, so allow the cap at the peak).
    assert (t["max_loss_unit"] * t["contracts"] <= 0.05 * 100_000 * 1.5).all()
    # cash_debit never opens a credit structure
    cd = t[t.profile == "cash_debit"]
    assert len(cd) > 0 and len(t[t.profile == "margin"]) > 0
    assert (cd["entry_net"] > 0).all()
    report = (tmp_path / "a" / "report.md").read_text()
    assert "Decision rule (fixed before the run)" in report and "## Verdict" in report
    # deterministic: same inputs, same report
    run_rank_report(out_dir=tmp_path / "b", **kw)  # type: ignore[arg-type]
    assert (tmp_path / "b" / "report.md").read_text() == report
