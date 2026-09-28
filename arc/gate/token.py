"""Gate token: HMAC proof that the deterministic gate passed an exact order (card E3.2).

PLAN §2.1 boundary 1: the only code path that can submit an order is
``arc.execution.submit()``, and it requires a ``GateToken`` over the *exact* order
payload plus an ``ApprovalRecord`` for the same proposal hash. This module mints
and verifies that token. It is pure: no clock reads (``now`` is an argument), no
I/O, no network. The secret is passed in by the caller (``ARC_GATE_SECRET`` via
:func:`gate_secret`).

Token wire format (ASCII, 105 chars, fits Alpaca's 128-char ``client_order_id``)::

    arc1.<proposal_hash_prefix>.<order_hash_prefix>.<expires_epoch>.<signature>

- ``proposal_hash_prefix`` / ``order_hash_prefix``: first 16 bytes of the SHA-256
  hex digests, base64url without padding (22 chars each; 128-bit binding).
- ``expires_epoch``: POSIX seconds; the token is invalid at and after this instant.
- ``signature``: base64url(HMAC-SHA256(secret, everything before the last dot)).

Using the token as the broker ``client_order_id`` makes it single-use: Alpaca
rejects a duplicate client order id.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import json
import re
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from arc.models import LegIntent, Proposal
from arc.utils.calendar import ET

if TYPE_CHECKING:
    from arc.config import ArcSettings
    from arc.models import GateDecision

__all__ = [
    "MIN_SECRET_BYTES",
    "TOKEN_VERSION",
    "GateToken",
    "OrderLeg",
    "OrderPayload",
    "TokenError",
    "TokenErrorCode",
    "gate_secret",
    "issue_token",
    "mint",
    "order_payload",
    "payload_hash",
    "verify",
]

TOKEN_VERSION = "arc1"
MIN_SECRET_BYTES = 32
_PREFIX_BYTES = 16
_B64 = r"[A-Za-z0-9_-]"
_TOKEN_RE = re.compile(
    rf"^(?P<v>arc1)\.(?P<ph>{_B64}{{22}})\.(?P<oh>{_B64}{{22}})\.(?P<exp>[1-9][0-9]{{0,11}})"
    rf"\.(?P<sig>{_B64}{{43}})$"
)


class TokenErrorCode(StrEnum):
    """Why a token (or a mint request) was refused. Stable, machine-readable."""

    MISSING = "missing"
    MALFORMED = "malformed"
    BAD_SIGNATURE = "bad_signature"
    EXPIRED = "expired"
    PROPOSAL_MISMATCH = "proposal_mismatch"
    ORDER_MISMATCH = "order_mismatch"
    NOT_PASSED = "gate_not_passed"
    BAD_SECRET = "bad_secret"
    BAD_EXPIRY = "bad_expiry"
    BAD_PAYLOAD = "bad_payload"


class TokenError(Exception):
    """A gate token could not be minted or did not verify. Always fail closed on this."""

    def __init__(self, code: TokenErrorCode, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else str(code))


# ---------------------------------------------------------------------------
# Canonical order payload
# ---------------------------------------------------------------------------


def _canon_decimal(value: object) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, Decimal | int | float | str):
        raise TokenError(TokenErrorCode.BAD_PAYLOAD, f"not a number: {value!r}")
    try:
        d = Decimal(str(value).strip())
    except InvalidOperation as exc:
        raise TokenError(TokenErrorCode.BAD_PAYLOAD, f"not a number: {value!r}") from exc
    if not d.is_finite():
        raise TokenError(TokenErrorCode.BAD_PAYLOAD, f"not finite: {value!r}")
    return d


def _canon_int(value: object, label: str) -> int:
    d = _canon_decimal(value)
    if d != d.to_integral_value() or d < 1:
        msg = f"{label} must be a positive integer: {value!r}"
        raise TokenError(TokenErrorCode.BAD_PAYLOAD, msg)
    return int(d)


def _decimal_str(d: Decimal) -> str:
    """Canonical text for a price: no exponent, no trailing zeros, ``0`` for any zero."""
    return "0" if d == 0 else format(d.normalize(), "f")


class OrderLeg(BaseModel):
    """One leg of the canonical order: OCC symbol, buy/sell, ratio."""

    model_config = ConfigDict(frozen=True)

    symbol: str = Field(..., min_length=1)
    side: Literal["buy", "sell"]
    ratio_qty: int = Field(1, ge=1)

    @field_validator("symbol")
    @classmethod
    def _norm_symbol(cls, v: str) -> str:
        return v.strip().upper()


class OrderPayload(BaseModel):
    """The exact order the gate authorises (venue-neutral, limit orders only).

    Leg order is irrelevant to the hash: legs are sorted in :meth:`canonical_json`.
    """

    model_config = ConfigDict(frozen=True)

    legs: tuple[OrderLeg, ...] = Field(..., min_length=1, max_length=4)
    qty: int = Field(..., ge=1)
    limit_price: Decimal = Field(..., allow_inf_nan=False)
    order_type: Literal["limit"] = "limit"
    time_in_force: Literal["day"] = "day"

    def canonical_json(self) -> str:
        legs = sorted((leg.symbol, leg.side, leg.ratio_qty) for leg in self.legs)
        body = {
            "legs": [{"ratio_qty": r, "side": s, "symbol": sym} for sym, s, r in legs],
            "limit_price": _decimal_str(self.limit_price),
            "order_type": self.order_type,
            "qty": self.qty,
            "time_in_force": self.time_in_force,
        }
        return json.dumps(body, sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_values(
        cls,
        legs: list[tuple[object, object, object]],
        *,
        qty: object,
        limit_price: object,
    ) -> OrderPayload:
        """Build from loosely typed values (e.g. broker-tool JSON); raises ``TokenError``."""
        try:
            return cls(
                legs=tuple(
                    OrderLeg(
                        symbol=str(sym),
                        side=str(side).strip().lower(),  # type: ignore[arg-type]
                        ratio_qty=_canon_int(ratio, "ratio_qty"),
                    )
                    for sym, side, ratio in legs
                ),
                qty=_canon_int(qty, "qty"),
                limit_price=_canon_decimal(limit_price),
            )
        except ValueError as exc:  # pydantic ValidationError is a ValueError
            raise TokenError(TokenErrorCode.BAD_PAYLOAD, str(exc)) from exc


def payload_hash(payload: OrderPayload) -> str:
    """SHA-256 hex digest of the canonical order payload."""
    return hashlib.sha256(payload.canonical_json().encode()).hexdigest()


def order_payload(proposal: Proposal) -> OrderPayload:
    """The order a proposal authorises: its legs, ``sizing.contracts`` units, limit price.

    The limit is ``proposal.limit_price`` or, when unset, the structure's net
    (mid) price — the same rule the gate's spread/tick check uses.
    """
    s = proposal.structure
    limit = proposal.limit_price if proposal.limit_price is not None else s.net_debit_credit
    return OrderPayload.from_values(
        [
            (leg.occ_symbol, "buy" if leg.side is LegIntent.LONG else "sell", leg.ratio)
            for leg in s.legs
        ],
        qty=proposal.sizing.contracts,
        limit_price=limit,
    )


# ---------------------------------------------------------------------------
# Token
# ---------------------------------------------------------------------------


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _hash_prefix(hex_digest: str) -> str:
    try:
        raw = bytes.fromhex(hex_digest)
    except ValueError as exc:
        raise TokenError(TokenErrorCode.BAD_PAYLOAD, "hash is not hex") from exc
    if len(raw) != 32:
        raise TokenError(TokenErrorCode.BAD_PAYLOAD, "hash is not a SHA-256 digest")
    return _b64(raw[:_PREFIX_BYTES])


def _check_secret(secret: bytes) -> None:
    if not isinstance(secret, bytes) or len(secret) < MIN_SECRET_BYTES:
        raise TokenError(
            TokenErrorCode.BAD_SECRET, f"gate secret must be at least {MIN_SECRET_BYTES} bytes"
        )


def _check_aware(now: dt.datetime) -> None:
    if now.tzinfo is None or now.utcoffset() is None:
        raise TokenError(TokenErrorCode.BAD_EXPIRY, "`now` must be timezone-aware")


def _sign(secret: bytes, body: str) -> str:
    return _b64(hmac.new(secret, body.encode(), hashlib.sha256).digest())


class GateToken(BaseModel):
    """Parsed gate token. ``encode()`` is the wire form stored in ``GateDecision.token``."""

    model_config = ConfigDict(frozen=True)

    version: Literal["arc1"] = TOKEN_VERSION
    proposal_prefix: str
    order_prefix: str
    expires_epoch: int
    signature: str

    @property
    def expires_at(self) -> dt.datetime:
        return dt.datetime.fromtimestamp(self.expires_epoch, tz=ET)

    @property
    def body(self) -> str:
        return f"{self.version}.{self.proposal_prefix}.{self.order_prefix}.{self.expires_epoch}"

    def encode(self) -> str:
        return f"{self.body}.{self.signature}"

    @classmethod
    def parse(cls, token: object) -> GateToken:
        if token is None or token == "":
            raise TokenError(TokenErrorCode.MISSING, "no gate token")
        if not isinstance(token, str) or len(token) > 128:
            raise TokenError(TokenErrorCode.MALFORMED, "token is not a short string")
        m = _TOKEN_RE.fullmatch(token)
        if m is None:
            raise TokenError(TokenErrorCode.MALFORMED, "token does not match arc1 format")
        return cls(
            proposal_prefix=m["ph"],
            order_prefix=m["oh"],
            expires_epoch=int(m["exp"]),
            signature=m["sig"],
        )


def mint(
    proposal_hash: str,
    decision: GateDecision,
    *,
    order: OrderPayload,
    secret: bytes,
    expires_at: dt.datetime,
    now: dt.datetime,
) -> str:
    """Mint a token for a *passed* gate decision, bound to ``order`` and ``expires_at``.

    Raises ``TokenError`` if the decision did not pass, belongs to another
    proposal, or the expiry is not in the future.
    """
    _check_secret(secret)
    _check_aware(now)
    _check_aware(expires_at)
    if not decision.passed or decision.violations:
        raise TokenError(TokenErrorCode.NOT_PASSED, "gate decision did not pass")
    if not hmac.compare_digest(decision.proposal_hash.encode(), proposal_hash.encode()):
        raise TokenError(TokenErrorCode.PROPOSAL_MISMATCH, "decision is for another proposal")
    epoch = int(expires_at.timestamp())
    if epoch <= now.timestamp():
        raise TokenError(TokenErrorCode.BAD_EXPIRY, "expiry is not in the future")
    unsigned = GateToken(
        proposal_prefix=_hash_prefix(proposal_hash),
        order_prefix=_hash_prefix(payload_hash(order)),
        expires_epoch=epoch,
        signature="",
    )
    return unsigned.model_copy(update={"signature": _sign(secret, unsigned.body)}).encode()


def verify(
    token: object,
    *,
    secret: bytes,
    now: dt.datetime,
    proposal_hash: str | None = None,
    order: OrderPayload | None = None,
) -> GateToken:
    """Verify a token; return it parsed, or raise ``TokenError`` (fail closed).

    The signature and both hash bindings are compared in constant time. Pass
    ``proposal_hash`` and/or ``order`` to require the token to bind them; the
    signature and expiry are always checked.
    """
    _check_secret(secret)
    _check_aware(now)
    t = GateToken.parse(token)
    if not hmac.compare_digest(_sign(secret, t.body).encode(), t.signature.encode()):
        raise TokenError(TokenErrorCode.BAD_SIGNATURE, "signature does not verify")
    if now.timestamp() >= t.expires_epoch:
        raise TokenError(TokenErrorCode.EXPIRED, f"token expired at {t.expires_at.isoformat()}")
    if proposal_hash is not None and not hmac.compare_digest(
        _hash_prefix(proposal_hash).encode(), t.proposal_prefix.encode()
    ):
        raise TokenError(TokenErrorCode.PROPOSAL_MISMATCH, "token is for another proposal")
    if order is not None and not hmac.compare_digest(
        _hash_prefix(payload_hash(order)).encode(), t.order_prefix.encode()
    ):
        raise TokenError(TokenErrorCode.ORDER_MISMATCH, "token is for a different order payload")
    return t


def issue_token(
    decision: GateDecision, proposal: Proposal, *, secret: bytes, now: dt.datetime
) -> GateDecision:
    """Return ``decision`` with ``token`` set when it passed (expiry = ``proposal.expires_at``).

    A failed decision is returned unchanged (``token=None``).
    """
    if not decision.passed:
        return decision
    token = mint(
        decision.proposal_hash,
        decision,
        order=order_payload(proposal),
        secret=secret,
        expires_at=proposal.expires_at,
        now=now,
    )
    return decision.model_copy(update={"token": token})


def gate_secret(config: ArcSettings) -> bytes:
    """``ARC_GATE_SECRET`` as bytes; raises ``TokenError`` when unset or too short."""
    raw = config.gate_secret.get_secret_value().encode() if config.gate_secret else b""
    _check_secret(raw)
    return raw
