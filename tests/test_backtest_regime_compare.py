"""E17.3 (D77): the regime-report rules (verdict, label flips, transitional-guard split)."""

from __future__ import annotations

import datetime as dt
import math
from pathlib import Path

import pandas as pd
import pytest

from arc.backtest.ranking import BootstrapSpec, RankRun, labels_for
from arc.backtest.regime_compare import (
    guard_blocked_days,
    guard_split,
    guard_verdict,
    label_flips,
    reference_settings,
    regime_verdict,
)
from arc.config import ArcSettings

FIXTURE_2Y = Path(__file__).resolve().parent / "fixtures" / "regime" / "spy_daily_closes_2y.csv"
BOOT = BootstrapSpec(resamples=200, block_days=5)
D0 = dt.date(2025, 1, 2)


def _spy() -> pd.Series:
    df = pd.read_csv(FIXTURE_2Y)
    return pd.Series(
        df["close"].to_numpy(dtype=float), index=[dt.date.fromisoformat(d) for d in df["date"]]
    )


def _run(
    pnls: list[float], *, ranker: str = "credit_width", days: list[dt.date] | None = None
) -> RankRun:
    days = days or [D0 + dt.timedelta(days=i) for i in range(len(pnls))]
    trades = pd.DataFrame(
        {
            "entry_date": days,
            "exit_date": days,
            "pnl": pnls,
            "pnl_mid": pnls,
            "pnl_unit": pnls,
            "managed_pop": 0.5,
            "managed_net_ev_unit": 0.0,
            "days_held": 1,
            "hold_to_expiry_pnl": pnls,
            "trend": "sideways",
        }
    )
    eq = pd.Series(100_000 + pd.Series(pnls).cumsum().to_numpy(), index=days, dtype=float)
    return RankRun(
        ranker=ranker, profile="margin", slippage=0.0, trades=trades, equity=eq, skipped={}
    )


# ---------------------------------------------------------------------------
# Verdict rule
# ---------------------------------------------------------------------------


def test_verdict_keep_v2_when_better_on_both_and_ci_hi_positive() -> None:
    v1 = {"margin": _run([10, -50, 10, -50, 10] * 6), "cash_debit": _run([5, -20] * 15)}
    v2 = {"margin": _run([20, -10, 20, -10, 20] * 6), "cash_debit": _run([10, -5] * 15)}
    t, v = regime_verdict(v1, v2, starting_equity=100_000, boot=BOOT)
    assert v == "keep v2"
    assert (t["ci_hi"] > 0).all() and t["pnl_not_worse"].all() and t["dd_not_worse"].all()


def test_verdict_roll_back_when_worse_on_both_in_one_profile() -> None:
    good, bad = [20, -10] * 15, [5, -60] * 15
    v1 = {"margin": _run(good), "cash_debit": _run(good)}
    v2 = {"margin": _run(good), "cash_debit": _run(bad)}
    assert regime_verdict(v1, v2, starting_equity=100_000, boot=BOOT)[1] == "roll back to v1"


def test_verdict_inconclusive_when_mixed() -> None:
    # v2 more P&L but deeper drawdown → not worse on both, not better on both
    v1 = {"margin": _run([5, -5] * 15)}
    v2 = {"margin": _run([40, -30] * 15)}
    t, v = regime_verdict(v1, v2, starting_equity=100_000, boot=BOOT)
    assert v == "keep v2, inconclusive"
    assert bool(t.iloc[0]["pnl_not_worse"]) and not bool(t.iloc[0]["dd_not_worse"])


def test_verdict_identical_runs_is_inconclusive_not_keep() -> None:
    r = _run([10, -10] * 15)
    t, v = regime_verdict({"margin": r}, {"margin": r}, starting_equity=100_000, boot=BOOT)
    assert v == "keep v2, inconclusive" and t.iloc[0]["ci_hi"] == 0.0


def test_verdict_empty() -> None:
    assert regime_verdict({}, {}, starting_equity=1.0, boot=BOOT)[1] == "keep v2, inconclusive"


# ---------------------------------------------------------------------------
# Label flips
# ---------------------------------------------------------------------------


def test_label_flips_counts_match_direct_comparison() -> None:
    s = _spy()
    labels = {"SPY": {"v1": labels_for(s, "v1"), "v2": labels_for(s, "v2")}}
    start, end = s.index[300], s.index[-1]
    t = label_flips(labels, start, end)  # type: ignore[arg-type]
    row = t.iloc[0]
    t1, t2 = labels["SPY"]["v1"][0], labels["SPY"]["v2"][0]
    days = [d for d in t1.index if start <= d <= end]
    known = [d for d in days if t1[d] != "unknown" and t2[d] != "unknown"]
    assert row["days"] == len(known)
    assert row["trend_flips"] == sum(t1[d] != t2[d] for d in known)
    assert 0 < row["trend_flips"] < row["days"]
    assert row["v1_bear"] + row["v1_sideways"] + row["v1_bull"] == row["days"]
    assert row["v2_bear"] + row["v2_sideways"] + row["v2_bull"] == row["days"]
    assert 0 <= row["vol_flip_pct"] <= 1


# ---------------------------------------------------------------------------
# Transitional guard
# ---------------------------------------------------------------------------


def test_guard_defaults_block_nothing_reference_blocks_some() -> None:
    s = _spy()
    off = guard_blocked_days(s, ArcSettings())
    assert not any(off.values())
    ref = guard_blocked_days(s, reference_settings())
    last = sorted(ref)[-504:]
    blocked = sum(ref[d] for d in last)
    # same construction as the E17.2 replay test (118 / 504 on macOS; band, not pin)
    assert 110 <= blocked <= 125


def test_guard_blocked_days_never_look_ahead() -> None:
    s = _spy()
    cut = s.index[300]
    full = guard_blocked_days(s, reference_settings())
    past = guard_blocked_days(s[s.index <= cut], reference_settings())
    assert all(full[d] == past[d] for d in past)


def test_guard_split_groups_and_ci() -> None:
    days = [D0 + dt.timedelta(days=i) for i in range(60)]
    blocked = {d: i % 4 == 0 for i, d in enumerate(days)}
    pnls = [-50.0 if blocked[d] else 20.0 for d in days]
    run = _run(pnls, days=days)
    split = guard_split({"margin": run, "cash_debit": run}, blocked, BOOT)
    b = split[(split["profile"] == "margin") & (split["group"] == "blocked")].iloc[0]
    r = split[(split["profile"] == "margin") & (split["group"] == "rest")].iloc[0]
    assert b["trades"] == 15 and r["trades"] == 45
    assert b["mean_pnl"] == -50 and r["mean_pnl"] == 20
    assert b["win_rate"] == 0 and r["win_rate"] == 1
    assert b["profit_factor"] == 0 and math.isinf(r["profit_factor"])
    assert b["diff_mean"] == pytest.approx(-70)
    assert b["diff_ci_hi"] < 0
    assert b["forgone_if_skipped"] == pytest.approx(-750)
    assert guard_verdict(split, ["margin", "cash_debit"]) == "turn on"


def test_guard_verdict_keep_off_cases() -> None:
    days = [D0 + dt.timedelta(days=i) for i in range(60)]
    blocked = {d: i % 4 == 0 for i, d in enumerate(days)}
    good = _run([30.0 if blocked[d] else 20.0 for d in days], days=days)
    bad = _run([-50.0 if blocked[d] else 20.0 for d in days], days=days)
    split = guard_split({"margin": bad, "cash_debit": good}, blocked, BOOT)
    assert guard_verdict(split, ["margin", "cash_debit"]) == "keep off"  # one profile fails
    assert guard_verdict(split, ["margin"]) == "turn on"
    assert guard_verdict(split, ["nope"]) == "keep off"
    # noisy difference: mean lower but CI straddles 0 → keep off
    noisy = _run(
        [
            (-30.0 if i % 8 == 0 else 60.0) if blocked[d] else (20.0 if i % 2 else -10.0)
            for i, d in enumerate(days)
        ],
        days=days,
    )
    s2 = guard_split({"margin": noisy}, blocked, BOOT)
    b = s2[s2["group"] == "blocked"].iloc[0]
    assert b["diff_ci_lo"] < 0 < b["diff_ci_hi"] or b["mean_pnl"] >= 0
    assert guard_verdict(s2, ["margin"]) == "keep off"


def test_guard_split_no_blocked_days() -> None:
    days = [D0 + dt.timedelta(days=i) for i in range(10)]
    split = guard_split({"margin": _run([1.0] * 10, days=days)}, {}, BOOT)
    b = split[split["group"] == "blocked"].iloc[0]
    assert b["trades"] == 0 and math.isnan(b["diff_mean"]) and math.isnan(b["mean_pnl"])
    assert guard_verdict(split, ["margin"]) == "keep off"


def test_guard_split_empty_trades() -> None:
    run = RankRun(
        ranker="x",
        profile="margin",
        slippage=0.0,
        trades=pd.DataFrame({"entry_date": [], "exit_date": [], "pnl": []}),
        equity=pd.Series(dtype=float),
        skipped={},
    )
    split = guard_split({"margin": run}, {D0: True}, BOOT)
    assert (split["trades"] == 0).all()
