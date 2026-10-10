"""E16.3: the anti-chase entry filter in the ranking backtest (``backtest.entry_filter``).

No look-ahead (stretch from bars <= the decision day), only directional long premium
is dropped, the cache, the pre-declared recommend rule, and an end-to-end run where
``none`` reproduces the unfiltered report byte for byte.
"""

from __future__ import annotations

import datetime as dt
import math
from typing import TYPE_CHECKING, Any

import numpy as np
import pandas as pd
import pytest

from arc.backtest.costs import load_cost_model
from arc.backtest.entry_filter import (
    EntryFilterRule,
    OhlcStore,
    apply_entry_filter,
    entry_filter_challenge,
    load_ohlc,
    stretched_days,
)
from arc.backtest.rank_report import run_rank_report
from arc.backtest.ranking import (
    BootstrapSpec,
    DecisionRule,
    RankFilters,
    RankRun,
    load_ranking_file,
)
from arc.config import ArcSettings
from arc.features.technicals import AntiChaseRule
from arc.scanner.rank import Ranker
from arc.utils.calendar import sessions_between
from tests.test_ranking import synth  # noqa: F401 - pytest fixture

if TYPE_CHECKING:
    from pathlib import Path

RULE = AntiChaseRule()


def _ohlc(
    closes: list[float], start: dt.date = dt.date(2024, 1, 2), rng: float = 1.0
) -> pd.DataFrame:
    days = sessions_between(start, start + dt.timedelta(days=400))[: len(closes)]
    c = np.asarray(closes, dtype=float)
    return pd.DataFrame(
        {"open": c, "high": c + rng, "low": c - rng, "close": c, "vwap": c - 0.1},
        index=days,
    )


def _ramp(n_flat: int = 40, n_up: int = 12, step: float = 3.0) -> list[float]:
    """Flat ~100 (small zig-zag so RSI is defined), then a steep run-up."""
    flat = [100.0 + (0.3 if i % 2 else -0.3) for i in range(n_flat)]
    return flat + [100.0 + step * (i + 1) for i in range(n_up)]


def test_stretched_days_flags_the_run_up_only_and_never_reads_the_future() -> None:
    df = _ohlc(_ramp())
    days = list(df.index)
    out = stretched_days(df, days, RULE)
    assert all(out[d] == frozenset() for d in days[:40])  # flat: nothing stretched
    assert out[days[-1]] == frozenset({"bullish"})  # +36 in 12 bars: > 2.5 ATR, RSI ~100
    # no look-ahead: a crash after day k changes nothing up to day k
    k = days[45]
    crashed = df.copy()
    crashed.loc[crashed.index > k, ["open", "high", "low", "close", "vwap"]] *= 0.3
    again = stretched_days(crashed, days, RULE)
    assert {d: again[d] for d in days if d <= k} == {d: out[d] for d in days if d <= k}


def test_stretched_days_bearish_mirror_and_short_history() -> None:
    down = [200.0 - v + 100.0 for v in _ramp()]  # the run-up, mirrored
    df = _ohlc(down)
    out = stretched_days(df, list(df.index), RULE)
    assert out[df.index[-1]] == frozenset({"bearish"})
    short = _ohlc(_ramp()[:10])
    assert set(stretched_days(short, list(short.index), RULE).values()) == {frozenset()}
    assert stretched_days(pd.DataFrame(), [dt.date(2024, 5, 1)], RULE) == {
        dt.date(2024, 5, 1): frozenset()
    }


def test_vwap_arm_reads_the_bar_vwap() -> None:
    flat = [100.0 + (0.3 if i % 2 else -0.3) for i in range(40)]
    df = _ohlc(flat, rng=1.0)
    last = df.index[-1]
    df.loc[last, "vwap"] = df.loc[last, "close"] - 2.0  # close 2 pts over VWAP, ATR ~2
    assert stretched_days(df, [last], RULE)[last] == frozenset()
    assert stretched_days(df, [last], RULE, vwap=True)[last] == frozenset({"bullish"})
    df.loc[last, "vwap"] = float("nan")  # missing VWAP: the daily rule only
    assert stretched_days(df, [last], RULE, vwap=True)[last] == frozenset()


class _C:
    def __init__(self, kind: str) -> None:
        self.kind = kind


def test_apply_entry_filter_drops_long_premium_on_stretched_stance_days_only() -> None:
    d1, d2, d3, d4 = (dt.date(2024, 5, i) for i in (1, 2, 3, 6))
    menus: dict[dt.date, list[Any]] = {
        d1: [_C("long_call"), _C("bull_call"), _C("bull_put")],  # bull, stretched bull
        d2: [_C("long_put"), _C("bear_put")],  # bear, only bull stretched
        d3: [_C("iron_condor")],  # sideways
        d4: [_C("long_call")],  # bull, not stretched
    }
    trend = pd.Series({d1: "bull", d2: "bear", d3: "sideways", d4: "bull"})
    stretched = {
        d1: frozenset({"bullish"}),
        d2: frozenset({"bullish"}),
        d3: frozenset({"bullish", "bearish"}),
    }
    out, hit = apply_entry_filter(menus, trend, stretched)  # type: ignore[arg-type]
    assert [c.kind for c in out[d1]] == ["bull_put"]  # credit is never filtered
    assert out[d2] is menus[d2] and out[d3] is menus[d3] and out[d4] is menus[d4]
    assert hit == 1


def test_ohlc_cache_fetches_once_then_serves_from_disk(tmp_path: Path) -> None:
    calls: list[tuple[str, dt.date, dt.date]] = []
    df = _ohlc(_ramp())

    class _Src:
        def daily_ohlc(self, s: str, a: dt.date, b: dt.date) -> pd.DataFrame:
            calls.append((s, a, b))
            return df

    store = OhlcStore(tmp_path)
    lo, hi = df.index[0], df.index[-1]
    a = load_ohlc(store, "tst", lo, hi, source=_Src())
    b = load_ohlc(store, "TST", lo, hi, source=_Src())
    assert len(calls) == 1 and store.path_for("tst").name == "TST.parquet"
    pd.testing.assert_frame_equal(a, b, check_freq=False)
    assert load_ohlc(OhlcStore(tmp_path / "empty"), "X", lo, hi).empty
    inner = load_ohlc(store, "TST", df.index[5], df.index[9])
    assert list(inner.index) == list(df.index[5:10])


def _run(daily: list[float], trends: list[str], pnls: list[float]) -> RankRun:
    days = sessions_between(dt.date(2025, 1, 2), dt.date(2025, 6, 30))[: len(daily)]
    eq = pd.Series(np.cumsum(daily) + 100_000.0, index=days)
    t = pd.DataFrame(
        {
            "trend": trends,
            "pnl": pnls,
            "entry_date": days[: len(pnls)],
            "exit_date": days[: len(pnls)],
        }
    )
    return RankRun(
        ranker="debit_width", profile="cash_debit", slippage=0.25, trades=t, equity=eq, skipped={}
    )


def test_entry_filter_rule_is_the_card_rule() -> None:
    n, trends = 120, ["bear", "sideways", "bull"]
    rule, sub, boot = EntryFilterRule(), DecisionRule(), BootstrapSpec(resamples=300)
    base = _run([(-60.0 if i % 10 == 0 else 5.0) for i in range(n)], trends, [-100.0, 0.0, -200.0])
    # fewer losers, same winners: smaller DD, P&L not lower in all three sub-periods
    better = _run([(-10.0 if i % 10 == 0 else 5.0) for i in range(n)], trends, [-50.0, 0.0, -100.0])
    r = entry_filter_challenge(base, better, rule=rule, subperiods=sub, boot=boot)
    assert r["recommend"] and r["smaller_dd"] and r["subperiods_not_lower"] == 3
    assert r["ci_floor"] == pytest.approx(-0.10 * 300.0)
    # same DD (filter removed nothing): not recommended, even though P&L ties
    r = entry_filter_challenge(base, base, rule=rule, subperiods=sub, boot=boot)
    assert not r["recommend"] and not r["smaller_dd"] and r["subperiods_not_lower"] == 3
    # smaller DD but lower P&L in 2 of 3 sub-periods: not recommended
    worse = _run([(-10.0 if i % 10 == 0 else 1.0) for i in range(n)], trends, [-150.0, 0.0, -300.0])
    r = entry_filter_challenge(base, worse, rule=rule, subperiods=sub, boot=boot)
    assert not r["recommend"] and r["subperiods_not_lower"] == 1
    assert "≥ 2 of 3" in rule.text(0.9, sub.subperiods)


def test_config_default_is_none_and_thresholds_are_d78() -> None:
    bt = load_ranking_file().backtest
    assert bt.entry_filter == "none"
    assert bt.anti_chase == AntiChaseRule()
    assert bt.entry_filter_rule == EntryFilterRule()


def test_run_rank_report_with_and_without_the_filter(
    synth: dict[str, object],  # noqa: F811
    tmp_path: Path,
) -> None:
    cfg = load_ranking_file()
    bt = cfg.backtest.model_copy(
        update={"n_paths": 300, "slippage_grid": [0.25], "bootstrap": BootstrapSpec(resamples=200)}
    )
    cfg = cfg.model_copy(
        update={
            "backtest": bt,
            "ranking": cfg.ranking.model_copy(update={"filters": RankFilters(enabled=False)}),
        }
    )
    closes: pd.Series = synth["closes"]  # type: ignore[assignment]
    ohlc = pd.DataFrame(
        {
            "open": closes,
            "high": closes * 1.002,
            "low": closes * 0.998,
            "close": closes,
            "vwap": closes,
        },
    )
    kw: dict[str, Any] = {
        "store": synth["store"],
        "closes_by_ticker": {"TST": closes},
        "tickers": ["TST"],
        "start": dt.date(2024, 4, 1),
        "end": dt.date(2024, 5, 10),
        "profiles": ["cash_debit"],
        "rankers": [Ranker.DEBIT_WIDTH],
        "cost": load_cost_model(),
        "settings": ArcSettings(),
        "provider": "synth",
        "charts": False,
    }
    run_rank_report(out_dir=tmp_path / "none", cfg=cfg, **kw)
    # `none` with OHLC passed is the same report: the filter path is not entered
    run_rank_report(out_dir=tmp_path / "none2", cfg=cfg, ohlc_by_ticker={"TST": ohlc}, **kw)
    base = (tmp_path / "none" / "report.md").read_text()
    assert (tmp_path / "none2" / "report.md").read_text() == base
    assert not (tmp_path / "none" / "entry_filter_days.csv").exists()

    # a hair-trigger rule (tiny ATR band in the synthetic tape) filters some days
    loose = AntiChaseRule(combine="any", max_stretch_atr=1.0, rsi_overbought=55.0)
    fcfg = cfg.model_copy(
        update={
            "backtest": bt.model_copy(update={"entry_filter": "anti_chase", "anti_chase": loose})
        }
    )
    frames = run_rank_report(out_dir=tmp_path / "ac", cfg=fcfg, ohlc_by_ticker={"TST": ohlc}, **kw)
    days = frames["entry_filter_days"]
    assert int(days["days_filtered"].sum()) > 0
    n_none = len(pd.read_csv(tmp_path / "none" / "trades.csv"))
    assert len(frames["trades"]) <= n_none
    assert "Entry filter (E16.3): anti_chase" in (tmp_path / "ac" / "report.md").read_text()
    assert math.isfinite(float(frames["summary"]["net_pnl"].iloc[0]))
