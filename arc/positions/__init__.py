"""Position manager (E6.4, D19): review open positions, early exits, close-to-reallocate.

- :mod:`arc.positions.evaluate`: pure per-position review (P&L, % of max gain /
  loss, DTE, theta, remaining net EV per $ BP, remaining PoP, exit signals).
- :mod:`arc.positions.reallocate`: pure D19 swap scorer (edge after switching
  costs, PoP tolerance, churn caps).
- :mod:`arc.positions.steps`: the ``positions.evaluate → quant.exits →
  risk.reallocate`` routine chain. Every close or swap open is a proposal that
  goes through the gate and approval; nothing here submits orders.
"""

from arc.positions.evaluate import ExitSignal, PositionReview, SignalKind, review_position
from arc.positions.reallocate import (
    CapacityCandidate,
    ReallocRules,
    ScoredPair,
    SwapSuggestion,
    score_swaps,
)

__all__ = [
    "CapacityCandidate",
    "ExitSignal",
    "PositionReview",
    "ReallocRules",
    "ScoredPair",
    "SignalKind",
    "SwapSuggestion",
    "review_position",
    "score_swaps",
]
