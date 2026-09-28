"""Pre-submit guards for the execution layer.

``arc.execution.submit()`` (E6.2) must call :func:`require_trading_allowed`
first, before checking the GateToken / ApprovalRecord and before any broker
call. A halt raised after the gate passed (e.g. ``!halt`` while a proposal
waited for approval) must still stop the order, so this reads the persisted
halt state at submit time rather than trusting the gate decision.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from arc.gate.halt import HaltSwitch

log = structlog.get_logger(__name__)


class TradingHaltedError(RuntimeError):
    """Execution refused: the kill switch / daily halt is active (or unreadable)."""


def require_trading_allowed(switch: HaltSwitch) -> None:
    """Raise :class:`TradingHaltedError` unless trading is allowed right now."""
    state = switch.state()
    if not state.halted:
        return
    if state.error is not None:
        detail = f"halt state unreadable, failing closed ({state.error})"
    else:
        detail = "; ".join(
            f"{h.kind}: {h.reason or 'no reason'} (by {h.actor})" for h in state.active
        )
    log.warning("execution.refused_halted", detail=detail)
    msg = f"trading is halted: {detail}"
    raise TradingHaltedError(msg)
