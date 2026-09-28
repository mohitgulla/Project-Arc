"""The single order-submission path (PLAN §2.1 boundary 1; card E3.2).

:func:`submit` is the only function in Arc allowed to call
``BrokerAdapter.submit_mleg``. It refuses unless it holds

1. a ``GateDecision`` that passed for this exact proposal, carrying a gate token
   that verifies (HMAC signature, expiry, proposal hash, *and* the exact order
   payload it is about to send), and
2. an ``ApprovalRecord`` with ``decision=approved`` for the same proposal hash.

The token doubles as the broker ``client_order_id``, so the broker rejects any
second use of it. Order working (price improvement, fills, cancels) is E6.2.
"""

from __future__ import annotations

import hmac
from enum import StrEnum
from typing import TYPE_CHECKING

import structlog

from arc.broker.base import MlegLeg, MlegOrder
from arc.config import ArcEnv
from arc.gate.rules import proposal_hash
from arc.gate.token import TokenError, TokenErrorCode, gate_secret, order_payload, verify
from arc.models import ApprovalDecision

if TYPE_CHECKING:
    import datetime as dt

    from arc.broker.base import BrokerAdapter
    from arc.config import ArcSettings
    from arc.models import ApprovalRecord, GateDecision, Proposal

__all__ = ["RefusalCode", "SubmitRefused", "build_order", "submit"]

log = structlog.get_logger()


class RefusalCode(StrEnum):
    """Why :func:`submit` refused. Stable, machine-readable."""

    NOT_PAPER = "not_paper"
    NO_DECISION = "no_decision"
    GATE_FAILED = "gate_failed"
    DECISION_MISMATCH = "decision_proposal_mismatch"
    TOKEN_INVALID = "token_invalid"
    NO_APPROVAL = "no_approval"
    APPROVAL_MISMATCH = "approval_proposal_mismatch"
    NOT_APPROVED = "not_approved"
    APPROVAL_TIME = "approval_time_invalid"
    BAD_TIME = "bad_time"


class SubmitRefused(Exception):
    """``submit()`` refused to send the order. Nothing reached the broker."""

    def __init__(self, code: RefusalCode, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else str(code))


def _same(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


def build_order(proposal: Proposal, token: str) -> MlegOrder:
    """The broker order for ``proposal``, with the gate token as ``client_order_id``."""
    payload = order_payload(proposal)
    legs = [MlegLeg(symbol=x.symbol, side=x.side, ratio_qty=x.ratio_qty) for x in payload.legs]
    return MlegOrder(
        legs=legs,
        qty=payload.qty,
        limit_price=payload.limit_price,
        time_in_force=payload.time_in_force,
        client_order_id=token,
    )


def _check(
    proposal: Proposal,
    decision: GateDecision | None,
    approval: ApprovalRecord | None,
    config: ArcSettings,
    now: dt.datetime,
) -> MlegOrder:
    if now.tzinfo is None or now.utcoffset() is None:
        raise SubmitRefused(RefusalCode.BAD_TIME, "`now` must be timezone-aware")
    if config.env is not ArcEnv.PAPER:
        raise SubmitRefused(RefusalCode.NOT_PAPER, f"ARC_ENV={config.env} (Phase 1 is paper only)")

    ph = proposal_hash(proposal)
    if decision is None:
        raise SubmitRefused(RefusalCode.NO_DECISION, "no gate decision")
    if not _same(decision.proposal_hash, ph):
        raise SubmitRefused(RefusalCode.DECISION_MISMATCH, "gate decision is for another proposal")
    if not decision.passed or decision.violations:
        raise SubmitRefused(RefusalCode.GATE_FAILED, "; ".join(decision.violations) or "not passed")

    token = decision.token
    try:
        if token is None:
            raise TokenError(TokenErrorCode.MISSING, "gate decision carries no token")
        order = order_payload(proposal)
        verify(token, secret=gate_secret(config), now=now, proposal_hash=ph, order=order)
    except TokenError as exc:
        raise SubmitRefused(RefusalCode.TOKEN_INVALID, str(exc)) from exc

    if approval is None:
        raise SubmitRefused(RefusalCode.NO_APPROVAL, "no approval record")
    if not _same(approval.proposal_hash, ph):
        raise SubmitRefused(RefusalCode.APPROVAL_MISMATCH, "approval is for another proposal")
    if approval.decision is not ApprovalDecision.APPROVED:
        raise SubmitRefused(RefusalCode.NOT_APPROVED, f"approval decision is {approval.decision}")
    if approval.at.tzinfo is None or approval.at.utcoffset() is None:
        raise SubmitRefused(RefusalCode.APPROVAL_TIME, "approval time is not timezone-aware")
    if approval.at > now or approval.at >= proposal.expires_at:
        raise SubmitRefused(RefusalCode.APPROVAL_TIME, "approval is in the future or after expiry")

    return build_order(proposal, token)


def submit(
    proposal: Proposal,
    decision: GateDecision | None,
    approval: ApprovalRecord | None,
    *,
    broker: BrokerAdapter,
    config: ArcSettings,
    now: dt.datetime,
) -> str:
    """Submit ``proposal`` as one multi-leg limit order; return the broker order id.

    Raises :class:`SubmitRefused` (and never touches ``broker``) unless the gate
    token and the approval both check out for this exact proposal and order.
    """
    try:
        order = _check(proposal, decision, approval, config, now)
    except SubmitRefused as exc:
        log.warning("execution.submit_refused", code=str(exc.code), detail=exc.detail)
        raise
    broker_id = broker.submit_mleg(order)
    log.info(
        "execution.submitted",
        broker_order_id=broker_id,
        proposal_hash=proposal_hash(proposal),
        legs=len(order.legs),
        qty=order.qty,
        limit_price=str(order.limit_price),
    )
    return broker_id
