"""Deterministic risk-proxy gate: rules, token minting, halt state.

Nothing in this package may import an LLM client, touch the network, or read
prompt text (enforced by import-linter; see ``.importlinter``).
"""

from arc.gate.halt import (
    HaltKind,
    HaltRecord,
    HaltState,
    HaltSwitch,
    ResumeNotAuthorizedError,
    daily_loss_breach,
    evaluate_with_halt,
)
from arc.gate.inputs import (
    AccountSnapshot,
    ClosedLot,
    MarketSnapshot,
    Portfolio,
    Position,
    Quote,
)
from arc.gate.rules import Derived, RuleCode, Violation, derive, evaluate, proposal_hash
from arc.gate.token import (
    GateToken,
    OrderLeg,
    OrderPayload,
    TokenError,
    TokenErrorCode,
    gate_secret,
    issue_token,
    mint,
    order_payload,
    payload_hash,
    verify,
)

__all__ = [
    "AccountSnapshot",
    "ClosedLot",
    "Derived",
    "GateToken",
    "HaltKind",
    "HaltRecord",
    "HaltState",
    "HaltSwitch",
    "MarketSnapshot",
    "OrderLeg",
    "OrderPayload",
    "Portfolio",
    "Position",
    "Quote",
    "ResumeNotAuthorizedError",
    "RuleCode",
    "TokenError",
    "TokenErrorCode",
    "Violation",
    "daily_loss_breach",
    "derive",
    "evaluate",
    "evaluate_with_halt",
    "gate_secret",
    "issue_token",
    "mint",
    "order_payload",
    "payload_hash",
    "proposal_hash",
    "verify",
]
