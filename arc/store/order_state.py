"""Event-sourced order state machine.

Every state transition is validated against a fixed transition table.
Illegal transitions raise ``IllegalTransitionError``.  Each valid
transition produces an ``OrderEvent`` row with ``run_id`` and ``actor``.

The transition table matches PLAN.md §2.3:

    proposed → gated → approved → submitted →
        partially_filled → filled | cancelled | rejected | expired

With additional edges:
    proposed → cancelled (withdrawn before gate)
    gated → cancelled (withdrawn before approval)
    approved → cancelled | expired
    submitted → cancelled | rejected | expired
    partially_filled → cancelled | filled
"""

from __future__ import annotations

from arc.models import OrderState

# ---------------------------------------------------------------------------
# Transition table: from_state → set of legal to_states
# ---------------------------------------------------------------------------

VALID_TRANSITIONS: dict[OrderState, frozenset[OrderState]] = {
    OrderState.PROPOSED: frozenset(
        {
            OrderState.GATED,
            OrderState.CANCELLED,
        }
    ),
    OrderState.GATED: frozenset(
        {
            OrderState.APPROVED,
            OrderState.CANCELLED,
        }
    ),
    OrderState.APPROVED: frozenset(
        {
            OrderState.SUBMITTED,
            OrderState.CANCELLED,
            OrderState.EXPIRED,
        }
    ),
    OrderState.SUBMITTED: frozenset(
        {
            OrderState.PARTIALLY_FILLED,
            OrderState.FILLED,
            OrderState.CANCELLED,
            OrderState.REJECTED,
            OrderState.EXPIRED,
        }
    ),
    OrderState.PARTIALLY_FILLED: frozenset(
        {
            OrderState.FILLED,
            OrderState.CANCELLED,
        }
    ),
    # Terminal states have no outgoing edges.
    OrderState.FILLED: frozenset(),
    OrderState.CANCELLED: frozenset(),
    OrderState.REJECTED: frozenset(),
    OrderState.EXPIRED: frozenset(),
}


class IllegalTransitionError(Exception):
    """Raised when an order transition violates the state machine."""

    def __init__(self, from_state: OrderState, to_state: OrderState) -> None:
        self.from_state = from_state
        self.to_state = to_state
        super().__init__(f"Illegal transition: {from_state.value} → {to_state.value}")


def validate_transition(from_state: OrderState, to_state: OrderState) -> None:
    """Raise ``IllegalTransitionError`` if the transition is not allowed."""
    allowed = VALID_TRANSITIONS.get(from_state, frozenset())
    if to_state not in allowed:
        raise IllegalTransitionError(from_state, to_state)


def is_terminal(state: OrderState) -> bool:
    """Return True if the state has no outgoing edges."""
    return len(VALID_TRANSITIONS.get(state, frozenset())) == 0
