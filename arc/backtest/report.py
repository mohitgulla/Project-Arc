"""Baseline + D4 grid backtest report (PLAN §4 E7.2, §7 "baseline-first research").

Two passes over the same cached history:

1. **Baseline** (Bawa-style): minimal filters, maximum trade count — every
   session, *every* expiration in a wide 20–60 DTE window, anchor |Δ| 0.25
   ± 0.10, no volume filter. Answers "is there anything here before we get
   selective?".
2. **D4 grid**: the Phase-1 whitelist (bull put, bear call, iron condor, bull
   call, bear put, long call, long put) × short/anchor |Δ| ∈ {0.16, 0.20,
   0.25, 0.30}, one expiration per session nearest the middle of 30–45 DTE,
   wings 2% of spot, volume ≥ 1 on every leg.

Plus: walk-forward selection over the D4 grid, regime-conditioned breakdowns
and a cost sensitivity grid. Outputs go to ``<out_dir>`` as CSV + ``report.md``.
"""

from __future__ import annotations

import datetime as dt
import math
from typing import TYPE_CHECKING

import pandas as pd
import structlog

from arc.backtest.costs import CostModel
from arc.backtest.engine import ExitPolicyMode, prepare_chains, run_backtest, trades_frame
from arc.backtest.metrics import (
    breakdown,
    compute_metrics,
    monthly_pnl,
    walk_forward_eval,
    walk_forward_oos,
    walk_forward_splits,
)
from arc.backtest.strategies import ExpiryMode, StrategyKind, StrategySpec

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from arc.backtest.underlying import BarsSource
    from arc.data.history.store import ParquetHistoryStore

log = structlog.get_logger()

__all__ = [
    "D4_DELTAS",
    "baseline_specs",
    "d4_specs",
    "format_table",
    "run_report",
]

D4_DELTAS: tuple[float, ...] = (0.16, 0.20, 0.25, 0.30)
DEFAULT_R = 0.045  # flat risk-free proxy (3m T-bill averaged ~4.5–5.3% over 2024–25)
SENSITIVITY: tuple[tuple[float, float], ...] = (
    (0.0, 0.0),  # mid fill, no spread: frictionless reference (fees still apply)
    (0.25, 0.02),
    (0.25, 0.04),  # default
    (0.50, 0.04),
    (0.50, 0.08),
)


def baseline_specs() -> list[StrategySpec]:
    return [
        StrategySpec(
            kind=k,
            dte_min=20,
            dte_max=60,
            delta=0.25,
            delta_tol=0.10,
            width_pct=0.02,
            expiry_mode=ExpiryMode.ALL,
            min_volume=0.0,
        )
        for k in StrategyKind
    ]


def d4_specs(deltas: Sequence[float] = D4_DELTAS) -> list[StrategySpec]:
    return [
        StrategySpec(kind=k, dte_min=30, dte_max=45, delta=d, delta_tol=0.04, width_pct=0.02)
        for k in StrategyKind
        for d in deltas
    ]


def _fmt(v: object) -> str:
    if isinstance(v, float):
        if math.isnan(v):
            return "–"
        if math.isinf(v):
            return "∞"
        return f"{v:,.3f}" if abs(v) < 10 else f"{v:,.0f}"
    return str(v)


def format_table(df: pd.DataFrame, cols: Sequence[str] | None = None) -> str:
    """Markdown table (no tabulate dependency)."""
    if df.empty:
        return "_no trades_\n"
    cols = list(cols or df.columns)
    lines = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for _, r in df[cols].iterrows():
        lines.append("| " + " | ".join(_fmt(r[c]) for c in cols) + " |")
    return "\n".join(lines) + "\n"


_SUMMARY_COLS = [
    "trades",
    "win_rate",
    "profit_factor",
    "avg_pnl",
    "total_pnl",
    "total_pnl_mid",
    "avg_ror",
    "avg_cost",
    "cost_pct_premium",
    "max_dd",
    "worst_month",
    "worst_month_pnl",
]
_SHORT_COLS = [
    "trades",
    "win_rate",
    "profit_factor",
    "avg_pnl",
    "total_pnl",
    "avg_ror",
    "avg_cost",
    "max_dd",
]


def _load(
    store: ParquetHistoryStore,
    provider: str,
    ticker: str,
    start: dt.date,
    end: dt.date,
) -> pd.DataFrame:
    df = store.read(provider, ticker, start, end)
    log.info("backtest.history_loaded", ticker=ticker, rows=len(df))
    return df


def run_report(
    *,
    store: ParquetHistoryStore,
    closes_by_ticker: dict[str, pd.Series],
    tickers: Sequence[str],
    start: dt.date,
    end: dt.date,
    out_dir: Path,
    provider: str = "alpaca",
    cost: CostModel | None = None,
    r: float = DEFAULT_R,
    train_months: int = 6,
    test_months: int = 2,
    sensitivity: bool = True,
    exit_policy: ExitPolicyMode = "hold_to_expiry",
) -> dict[str, pd.DataFrame]:
    """Run baseline + D4 grid on cached data, write CSVs and ``report.md``; return frames."""
    cost = cost or CostModel()
    out_dir.mkdir(parents=True, exist_ok=True)
    raw = {t: _load(store, provider, t, start, end) for t in tickers}
    chain_cache: dict[tuple[str, float, float], dict[dt.date, pd.DataFrame]] = {}

    def run(specs: list[StrategySpec], c: CostModel) -> pd.DataFrame:
        frames = []
        for t in tickers:
            closes = closes_by_ticker[t]
            key = (t, c.spread_pct, c.spread_min)  # chains depend on cost only via the spread
            if key not in chain_cache:
                chain_cache[key] = prepare_chains(
                    raw[t], closes, cost=c, r=r, dte_min=1, dte_max=60
                )
            chains = chain_cache[key]
            frames.append(
                trades_frame(
                    run_backtest(
                        chains, closes, specs, underlying=t, cost=c, exit_policy=exit_policy
                    )
                )
            )
        return pd.concat(frames, ignore_index=True) if frames else trades_frame([])

    base = run(baseline_specs(), cost)
    d4 = run(d4_specs(), cost)
    base.to_csv(out_dir / "baseline_trades.csv", index=False)
    d4.to_csv(out_dir / "d4_trades.csv", index=False)

    out: dict[str, pd.DataFrame] = {"baseline": base, "d4": d4}
    out["baseline_by_kind"] = breakdown(base, ["underlying", "kind"]) if len(base) else base
    out["d4_by_spec"] = breakdown(d4, ["kind", "spec"]) if len(d4) else d4
    out["d4_by_trend"] = breakdown(d4, ["kind", "trend"]) if len(d4) else d4
    out["d4_by_vol"] = breakdown(d4, ["kind", "vol"]) if len(d4) else d4
    out["baseline_by_trend"] = breakdown(base, ["kind", "trend"]) if len(base) else base
    splits = walk_forward_splits(start, end, train_months=train_months, test_months=test_months)
    out["walk_forward"] = walk_forward_eval(d4, splits) if len(d4) else pd.DataFrame()
    oos = walk_forward_oos(d4, out["walk_forward"]) if len(out["walk_forward"]) else d4.iloc[0:0]
    out["walk_forward_oos_summary"] = pd.DataFrame([compute_metrics(oos).model_dump()])
    for name, df in out.items():
        if name not in ("baseline", "d4"):
            df.to_csv(out_dir / f"{name}.csv", index=False)

    sens_rows = []
    if sensitivity:
        for x, sp in SENSITIVITY:
            c = cost.model_copy(update={"slippage_frac": x, "spread_pct": sp, "spread_min": 0.0})
            df = run(d4_specs((0.20,)), c)
            for kind, g in df.groupby("kind", sort=True):
                m = compute_metrics(g)
                sens_rows.append(
                    {
                        "slippage_x": x,
                        "spread_pct": sp,
                        "kind": kind,
                        "trades": m.trades,
                        "win_rate": m.win_rate,
                        "profit_factor": m.profit_factor,
                        "avg_pnl": m.avg_pnl,
                        "avg_ror": m.avg_ror,
                    }
                )
    out["sensitivity"] = pd.DataFrame(sens_rows)
    out["sensitivity"].to_csv(out_dir / "sensitivity.csv", index=False)

    months = monthly_pnl(d4) if len(d4) else pd.Series(dtype=float)
    out["d4_tail_months"] = months.sort_values().head(5).rename("pnl").reset_index()
    (out_dir / "report.md").write_text(
        _render(out, tickers=tickers, start=start, end=end, cost=cost, r=r, splits=len(splits))
    )
    return out


def _render(
    f: dict[str, pd.DataFrame],
    *,
    tickers: Sequence[str],
    start: dt.date,
    end: dt.date,
    cost: CostModel,
    r: float,
    splits: int,
) -> str:
    base, d4 = f["baseline"], f["d4"]
    parts = [
        "# Backtest run\n",
        f"Tickers: {', '.join(tickers)} · entries {start} → {end} · r = {r} · cost: "
        f"x={cost.slippage_frac}, fee=${cost.commission_per_contract}/contract, "
        f"est. spread = max({cost.spread_min}, {cost.spread_pct}·mid)\n",
        "## Baseline — all\n",
        format_table(pd.DataFrame([compute_metrics(base).model_dump()]), _SUMMARY_COLS),
        "## Baseline by underlying × kind\n",
        format_table(f["baseline_by_kind"], ["underlying", "kind", *_SHORT_COLS])
        if len(base)
        else "_no trades_\n",
        "## D4 grid by spec\n",
        format_table(f["d4_by_spec"], ["spec", *_SUMMARY_COLS]) if len(d4) else "_no trades_\n",
        "## D4 by trend regime (at entry)\n",
        format_table(f["d4_by_trend"], ["kind", "trend", *_SHORT_COLS])
        if len(d4)
        else "_no trades_\n",
        "## D4 by realised-vol regime (at entry)\n",
        format_table(f["d4_by_vol"], ["kind", "vol", *_SHORT_COLS]) if len(d4) else "_no trades_\n",
        "## D4 worst months (pnl booked at expiry, all specs summed)\n",
        format_table(f["d4_tail_months"]) if len(d4) else "_no trades_\n",
        f"## Walk-forward ({splits} splits; pick best spec by train avg RoR)\n",
        format_table(f["walk_forward"]) if len(f["walk_forward"]) else "_no splits_\n",
        "Out-of-sample aggregate (chosen spec per test window, concatenated):\n",
        format_table(f["walk_forward_oos_summary"], _SUMMARY_COLS),
        "## Cost sensitivity (D4, 20Δ)\n",
        format_table(f["sensitivity"]) if len(f["sensitivity"]) else "_skipped_\n",
    ]
    return "\n".join(parts)


def closes_for(
    tickers: Sequence[str],
    start: dt.date,
    end: dt.date,
    data_dir: Path,
    source: BarsSource | None,
) -> dict[str, pd.Series]:
    """Underlying closes from ``start − 60d`` (regime warm-up) to ``end + 70d`` (settlement)."""
    from arc.backtest.underlying import UnderlyingStore, load_closes

    us = UnderlyingStore(data_dir)
    return {
        t: load_closes(
            us, t, start - dt.timedelta(days=60), end + dt.timedelta(days=70), source=source
        )
        for t in tickers
    }
