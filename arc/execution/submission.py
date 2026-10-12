"""The single order-submission path (PLAN §2.1 boundary 1; cards E3.2, E6.2).

:func:`submit` is the only function in Arc allowed to call
``BrokerAdapter.submit_mleg``. It refuses unless

0. trading is not halted *right now* (the persisted kill switch / daily halt is
   re-read at submit time, so a ``!halt`` raised while a proposal waited for
   approval still stops the order),
1. it holds a ``GateDecision`` that passed for this exact proposal, carrying a
   gate token that verifies (HMAC signature, expiry, proposal hash and order):

   - ``arc2`` (D24 price band): legs and qty match the token and the attempt's
     limit lies inside the signed band; attempt ``step`` (``0..max_steps``) is
     sent with ``client_order_id = <token>.s<step>``;
   - ``arc1`` (legacy, exact price): only step 0 at exactly the gated limit,
     with the bare token as ``client_order_id``,

2. and an ``ApprovalRecord`` with ``decision=approved`` for the same proposal hash.

The client order id is unique per attempt, so the broker rejects any re-use.
The ladder that steps the price and waits for each cancel is
:mod:`arc.execution.ladder`.

D69 / E15.2 arm tag: an experiment arm on a *shared* paper account sends
``arc2.<code>.<token rest>.s<k>``, where ``<code>`` is the arm's 1-char base62 code
(:data:`ARM_CODES`, allocated at t0, stored on ``arm_identity``). The broker's
FILL activities carry the order id only, so the tag is how a fill is attributed to
an arm. The gate token and its verification are unchanged: :func:`parse_order_id`
strips the code and the gate verifies the bare ``arc2.<token>.s<k>``. Control's
ids carry no code. This module is the only place the tag is built or parsed.
"""

from __future__ import annotations

import hmac
import string
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING, NamedTuple

import structlog

from arc.broker.base import MlegLeg, MlegOrder
from arc.config import ArcEnv
from arc.execution.guard import TradingHaltedError, require_trading_allowed
from arc.gate.rules import proposal_hash
from arc.gate.ticks import legs_grid, max_decimal_places_ok
from arc.gate.token import (
    BAND_TOKEN_VERSION,
    BandToken,
    OrderPayload,
    TokenError,
    TokenErrorCode,
    client_order_id,
    gate_secret,
    order_payload,
    parse_any,
    verify_client_order_id,
)
from arc.models import ApprovalDecision

if TYPE_CHECKING:
    import datetime as dt

    from arc.broker.base import BrokerAdapter
    from arc.config import ArcSettings
    from arc.gate.halt import HaltSwitch
    from arc.models import ApprovalRecord, GateDecision, Proposal

__all__ = [
    "ARM_CODES",
    "MAX_CLIENT_ORDER_ID",
    "OrderTag",
    "RefusalCode",
    "SubmitRefused",
    "attempt_order_id",
    "broker_arm_code",
    "build_order",
    "parse_order_id",
    "submit",
]

log = structlog.get_logger()

#: D69 arm codes, in allocation order: 62 one-char codes (hard arm ceiling is 16).
ARM_CODES = string.digits + string.ascii_uppercase + string.ascii_lowercase
#: Alpaca's ``client_order_id`` limit.
MAX_CLIENT_ORDER_ID = 128
_BAND_PREFIX = f"{BAND_TOKEN_VERSION}."


class OrderTag(NamedTuple):
    """A broker ``client_order_id`` split into its arm code and the gate's id."""

    arm_code: str | None
    gate_id: str


def parse_order_id(client_order_id: str | None) -> OrderTag:
    """``arc2.<code>.<rest>`` -> ``(code, arc2.<rest>)``; any other id -> ``(None, id)``.

    The code segment is exactly one base62 char; a gate token's first segment after
    ``arc2.`` is 22 chars, so the two never collide. Pure, no verification.
    """
    coid = client_order_id or ""
    if coid.startswith(_BAND_PREFIX):
        code, sep, rest = coid[len(_BAND_PREFIX) :].partition(".")
        if sep and len(code) == 1 and code in ARM_CODES:
            return OrderTag(code, f"{_BAND_PREFIX}{rest}")
    return OrderTag(None, coid)


class RefusalCode(StrEnum):
    """Why :func:`submit` refused. Stable, machine-readable."""

    HALTED = "halted"
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
    VENUE_SINGLE_LEG_ONLY = "venue_single_leg_only"
    OFF_GRID = "off_grid"


class SubmitRefused(Exception):
    """``submit()`` refused to send the order. Nothing reached the broker."""

    def __init__(self, code: RefusalCode, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else str(code))


def _same(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


def attempt_order_id(token: str, step: int, *, arm_code: str | None = None) -> str:
    """Broker ``client_order_id`` for attempt ``step``: ``<arc2>.s<k>``, or the bare arc1 token.

    *arm_code* (D69, a shared-account arm): ``arc2.<code>.<token rest>.s<k>``. Only an
    ``arc2`` token can carry it; an arc1 token or a malformed code is refused.
    """
    if isinstance(parse_any(token), BandToken):
        coid = client_order_id(token, step)
        if arm_code is None:
            return coid
        if len(arm_code) != 1 or arm_code not in ARM_CODES:
            raise TokenError(TokenErrorCode.MALFORMED, f"arm code {arm_code!r} is not base62")
        tagged = f"{_BAND_PREFIX}{arm_code}.{coid[len(_BAND_PREFIX) :]}"
        if len(tagged) > MAX_CLIENT_ORDER_ID:  # unreachable at the gate's price cap
            raise TokenError(TokenErrorCode.MALFORMED, f"order id is {len(tagged)} chars")
        return tagged
    if arm_code is not None:
        raise TokenError(TokenErrorCode.MALFORMED, "an arm order needs an arc2 token")
    if step != 0:
        raise TokenError(TokenErrorCode.BAD_STEP, "an arc1 token authorises one attempt only")
    return token


def _payload_at(proposal: Proposal, limit_price: Decimal | None) -> OrderPayload:
    payload = order_payload(proposal)
    if limit_price is None:
        return payload
    return payload.model_copy(update={"limit_price": Decimal(limit_price)})


def build_order(
    proposal: Proposal,
    token: str,
    *,
    step: int = 0,
    limit_price: Decimal | None = None,
    arm_code: str | None = None,
) -> MlegOrder:
    """Broker order for attempt ``step`` of ``proposal`` at ``limit_price`` (default: its limit)."""
    payload = _payload_at(proposal, limit_price)
    legs = [MlegLeg(symbol=x.symbol, side=x.side, ratio_qty=x.ratio_qty) for x in payload.legs]
    return MlegOrder(
        legs=legs,
        qty=payload.qty,
        limit_price=payload.limit_price,
        time_in_force=payload.time_in_force,
        client_order_id=attempt_order_id(token, step, arm_code=arm_code),
    )


def broker_arm_code(broker: object) -> str | None:
    """The arm code a broker tags its orders with (a shared-mode arm broker), else None."""
    code = getattr(broker, "arm_code", None)
    return code if isinstance(code, str) else None


def _check(
    proposal: Proposal,
    decision: GateDecision | None,
    approval: ApprovalRecord | None,
    config: ArcSettings,
    now: dt.datetime,
    *,
    halt: HaltSwitch | None,
    step: int,
    limit_price: Decimal | None,
    supports_mleg: bool = True,
    closing: bool = False,
    arm_code: str | None = None,
) -> MlegOrder:
    if now.tzinfo is None or now.utcoffset() is None:
        raise SubmitRefused(RefusalCode.BAD_TIME, "`now` must be timezone-aware")
    if halt is None:
        raise SubmitRefused(RefusalCode.HALTED, "no halt switch given: failing closed")
    try:
        require_trading_allowed(halt, closing=closing)
    except TradingHaltedError as exc:
        raise SubmitRefused(RefusalCode.HALTED, str(exc)) from exc
    if config.env is not ArcEnv.PAPER:
        raise SubmitRefused(RefusalCode.NOT_PAPER, f"ARC_ENV={config.env} (Phase 1 is paper only)")
    # E13.11 (D1/D56): a single-leg-only venue (Robinhood) never receives a spread.
    if not supports_mleg and len(proposal.structure.legs) > 1:
        raise SubmitRefused(
            RefusalCode.VENUE_SINGLE_LEG_ONLY,
            f"broker venue is single-leg only; proposal has {len(proposal.structure.legs)} legs",
        )

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
        order = build_order(proposal, token, step=step, limit_price=limit_price, arm_code=arm_code)
        # D69: the gate verifies its own id; an arm's code is stripped first.
        verify_client_order_id(
            parse_order_id(order.client_order_id).gate_id,
            secret=gate_secret(config),
            now=now,
            proposal_hash=ph,
            order=_payload_at(proposal, limit_price),
        )
    except TokenError as exc:
        raise SubmitRefused(RefusalCode.TOKEN_INVALID, str(exc)) from exc

    # D66 defence in depth: the gate and the ladder already keep every price on
    # the order's exchange grid; an off-grid limit is never sent.
    grid = legs_grid(proposal.structure.legs, config.ticks)
    if not max_decimal_places_ok(order.limit_price) or not grid.on_grid(order.limit_price):
        tick = grid.tick_at(order.limit_price)
        raise SubmitRefused(
            RefusalCode.OFF_GRID,
            f"limit {order.limit_price} is not a multiple of tick {tick} "
            f"({grid.describe(order.limit_price)})",
        )

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

    return order


def submit(
    proposal: Proposal,
    decision: GateDecision | None,
    approval: ApprovalRecord | None,
    *,
    broker: BrokerAdapter,
    config: ArcSettings,
    now: dt.datetime,
    halt: HaltSwitch | None,
    step: int = 0,
    limit_price: Decimal | None = None,
    closing: bool = False,
) -> str:
    """Submit attempt ``step`` of ``proposal`` as one limit order; return the broker order id.

    Raises :class:`SubmitRefused` (and never touches ``broker``) when trading is
    halted (``halt`` is required: ``None`` refuses), when the broker is single-leg
    only (``supports_mleg is False``) and the proposal has more than one leg, or
    (E11.4, D73) an opens-only halt is active and *closing* is False, or
    unless the gate token and the approval both check out for this exact
    proposal, step and price.
    """
    try:
        order = _check(
            proposal,
            decision,
            approval,
            config,
            now,
            halt=halt,
            step=step,
            limit_price=limit_price,
            supports_mleg=getattr(broker, "supports_mleg", True) is not False,
            closing=closing,
            arm_code=broker_arm_code(broker),
        )
    except SubmitRefused as exc:
        log.warning("execution.submit_refused", code=str(exc.code), detail=exc.detail, step=step)
        raise
    broker_id = broker.submit_mleg(order)
    log.info(
        "execution.submitted",
        broker_order_id=broker_id,
        client_order_id=order.client_order_id,
        proposal_hash=proposal_hash(proposal),
        step=step,
        legs=len(order.legs),
        qty=order.qty,
        limit_price=str(order.limit_price),
    )
    return broker_id
