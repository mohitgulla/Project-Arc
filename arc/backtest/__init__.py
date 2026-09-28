"""Cost-aware options backtester (PLAN §4 E7.2).

In-house engine over the E7.1 parquet EOD cache (optopsy evaluation and the
rationale are in ``docs/RESEARCH/backtest-baseline.md``):

- :mod:`arc.backtest.costs` — slippage (mid ± x·spread) + per-contract fees
- :mod:`arc.backtest.chain` — per-session mid/spread/IV/Δ
- :mod:`arc.backtest.strategies` — D4 specs and delta-targeted leg selection
- :mod:`arc.backtest.engine` — hold-to-expiry simulation → :class:`Trade`
- :mod:`arc.backtest.metrics` — win rate, PF, max DD, tail months, walk-forward
- :mod:`arc.backtest.regime` — entry-time trend / realised-vol labels
- :mod:`arc.backtest.report` — baseline + D4 grid report
"""

from arc.backtest.costs import CostModel
from arc.backtest.engine import Trade, prepare_chains, run_backtest, trades_frame
from arc.backtest.metrics import (
    Metrics,
    Split,
    breakdown,
    compute_metrics,
    walk_forward_eval,
    walk_forward_splits,
)
from arc.backtest.strategies import ExpiryMode, StrategyKind, StrategySpec

__all__ = [
    "CostModel",
    "ExpiryMode",
    "Metrics",
    "Split",
    "StrategyKind",
    "StrategySpec",
    "Trade",
    "breakdown",
    "compute_metrics",
    "prepare_chains",
    "run_backtest",
    "trades_frame",
    "walk_forward_eval",
    "walk_forward_splits",
]
