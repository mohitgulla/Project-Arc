"""Deterministic risk-proxy gate: rules, token minting, halt state.

Nothing in this package may import an LLM client, touch the network, or read
prompt text (enforced by import-linter; see ``.importlinter``).
"""

from arc.gate.inputs import (
    AccountSnapshot,
    ClosedLot,
    MarketSnapshot,
    Portfolio,
    Position,
    Quote,
)
from arc.gate.rules import Derived, RuleCode, Violation, derive, evaluate, proposal_hash

__all__ = [
    "AccountSnapshot",
    "ClosedLot",
    "Derived",
    "MarketSnapshot",
    "Portfolio",
    "Position",
    "Quote",
    "RuleCode",
    "Violation",
    "derive",
    "evaluate",
    "proposal_hash",
]
