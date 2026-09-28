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

``arc2`` — price-band token (D24, card E6.2)::

    arc2.<proposal_hash_prefix>.<legs_hash_prefix>.<lo>.<hi>.<max_steps>.<expires>.<sig>

- ``legs_hash_prefix``: the order payload *without* its limit price (legs, qty,
  order type, time in force), same 128-bit prefix encoding.
- ``lo`` / ``hi``: the band in signed integer cents per share (+ debit, − credit);
  ``lo`` is the mid limit, ``hi`` the worst limit after ``max_steps`` steps.
- the signature covers every field, so the band cannot be widened.

One ``arc2`` token authorises up to ``max_steps + 1`` attempts. Attempt ``k`` is
sent with ``client_order_id = <token>.s<k>`` (unique per attempt, so the broker
still rejects any re-use) and a limit inside ``[lo, hi]``. ``arc1`` tokens keep
verifying until they expire (exact price, one attempt, bare token as the id);
new tokens are minted as ``arc2`` whenever the gate evaluated a band.
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

from arc.gate.band import MAX_BAND_STEPS, PriceBand
from arc.models import LegIntent, Proposal
from arc.utils.calendar import ET

if TYPE_CHECKING:
    from arc.config import ArcSettings
    from arc.models import GateDecision

__all__ = [
    "BAND_TOKEN_VERSION",
    "MIN_SECRET_BYTES",
    "TOKEN_VERSION",
    "BandToken",
    "GateToken",
    "OrderLeg",
    "OrderPayload",
    "TokenError",
    "TokenErrorCode",
    "gate_secret",
    "client_order_id",
    "issue_token",
    "legs_hash",
    "mint",
    "mint_band",
    "order_payload",
    "parse_any",
    "payload_hash",
    "verify",
    "verify_any",
    "verify_band",
    "verify_client_order_id",
]

TOKEN_VERSION = "arc1"
MIN_SECRET_BYTES = 32
_PREFIX_BYTES = 16
_B64 = r"[A-Za-z0-9_-]"
_TOKEN_RE = re.compile(
    rf"^(?P<v>arc1)\.(?P<ph>{_B64}{{22}})\.(?P<oh>{_B64}{{22}})\.(?P<exp>[1-9][0-9]{{0,11}})"
    rf"\.(?P<sig>{_B64}{{43}})$"
)
BAND_TOKEN_VERSION = "arc2"
_MAX_CENTS = 999_999  # |price| <= $9,999.99 per share keeps the id <= 128 chars
_CENTS = r"-?(?:0|[1-9][0-9]{0,5})"
_BAND_RE = re.compile(
    rf"^(?P<v>arc2)\.(?P<ph>{_B64}{{22}})\.(?P<lh>{_B64}{{22}})\.(?P<lo>{_CENTS})"
    rf"\.(?P<hi>{_CENTS})\.(?P<n>[0-9])\.(?P<exp>[1-9][0-9]{{0,9}})\.(?P<sig>{_B64}{{43}})$"
)
_STEP_RE = re.compile(r"^(?P<tok>.+)\.s(?P<k>[0-9])$")
_MAX_ID = 128


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
    OUT_OF_BAND = "out_of_band"
    BAD_STEP = "bad_step"
    REUSED = "reused_order_id"


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

    def canonical_json(self, *, with_limit: bool = True) -> str:
        legs = sorted((leg.symbol, leg.side, leg.ratio_qty) for leg in self.legs)
        body: dict[str, object] = {
            "legs": [{"ratio_qty": r, "side": s, "symbol": sym} for sym, s, r in legs],
            "order_type": self.order_type,
            "qty": self.qty,
            "time_in_force": self.time_in_force,
        }
        if with_limit:
            body["limit_price"] = _decimal_str(self.limit_price)
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


def legs_hash(payload: OrderPayload) -> str:
    """SHA-256 hex digest of the canonical payload *without* its limit price (arc2)."""
    return hashlib.sha256(payload.canonical_json(with_limit=False).encode()).hexdigest()


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
    decision: GateDecision,
    proposal: Proposal,
    *,
    secret: bytes,
    now: dt.datetime,
    band: PriceBand | None = None,
) -> GateDecision:
    """Return ``decision`` with ``token`` set when it passed (expiry = ``proposal.expires_at``).

    With ``band`` (the band the gate evaluated) the token is ``arc2``; without, the
    legacy exact-price ``arc1``. A failed decision is returned unchanged (``token=None``).
    """
    if not decision.passed:
        return decision
    order = order_payload(proposal)
    ph = decision.proposal_hash
    if band is None:
        token = mint(
            ph, decision, order=order, secret=secret, expires_at=proposal.expires_at, now=now
        )
    else:
        token = mint_band(
            ph,
            decision,
            order=order,
            band=band,
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


# ---------------------------------------------------------------------------
# arc2: price-band token (D24)
# ---------------------------------------------------------------------------


def _to_cents(price: Decimal) -> int:
    cents = price * 100
    if cents != cents.to_integral_value() or abs(cents) > _MAX_CENTS:
        raise TokenError(TokenErrorCode.BAD_PAYLOAD, f"band price {price} is not whole cents")
    return int(cents)


class BandToken(BaseModel):
    """Parsed ``arc2`` token. ``encode()`` is the wire form stored in ``GateDecision.token``."""

    model_config = ConfigDict(frozen=True)

    version: Literal["arc2"] = BAND_TOKEN_VERSION
    proposal_prefix: str
    legs_prefix: str
    lo_cents: int
    hi_cents: int
    max_steps: int
    expires_epoch: int
    signature: str

    @property
    def expires_at(self) -> dt.datetime:
        return dt.datetime.fromtimestamp(self.expires_epoch, tz=ET)

    @property
    def band(self) -> PriceBand:
        """The signed band (raises ``ValueError`` if it is not a valid band)."""
        return PriceBand(
            lo=Decimal(self.lo_cents) / 100,
            hi=Decimal(self.hi_cents) / 100,
            max_steps=self.max_steps,
        )

    @property
    def body(self) -> str:
        return (
            f"{self.version}.{self.proposal_prefix}.{self.legs_prefix}.{self.lo_cents}."
            f"{self.hi_cents}.{self.max_steps}.{self.expires_epoch}"
        )

    def encode(self) -> str:
        return f"{self.body}.{self.signature}"

    @classmethod
    def parse(cls, token: object) -> BandToken:
        if token is None or token == "":
            raise TokenError(TokenErrorCode.MISSING, "no gate token")
        if not isinstance(token, str) or len(token) > _MAX_ID:
            raise TokenError(TokenErrorCode.MALFORMED, "token is not a short string")
        m = _BAND_RE.fullmatch(token)
        if m is None:
            raise TokenError(TokenErrorCode.MALFORMED, "token does not match arc2 format")
        return cls(
            proposal_prefix=m["ph"],
            legs_prefix=m["lh"],
            lo_cents=int(m["lo"]),
            hi_cents=int(m["hi"]),
            max_steps=int(m["n"]),
            expires_epoch=int(m["exp"]),
            signature=m["sig"],
        )


def client_order_id(token: str, step: int) -> str:
    """The broker id for attempt ``step`` of an ``arc2`` token: ``<token>.s<step>``."""
    if not 0 <= step <= MAX_BAND_STEPS:
        raise TokenError(TokenErrorCode.BAD_STEP, f"step {step} outside 0..{MAX_BAND_STEPS}")
    return f"{token}.s{step}"


def parse_any(token: object) -> GateToken | BandToken:
    """Parse an ``arc1`` or ``arc2`` token (no signature check)."""
    if isinstance(token, str) and token.startswith(f"{BAND_TOKEN_VERSION}."):
        return BandToken.parse(token)
    return GateToken.parse(token)


def mint_band(
    proposal_hash: str,
    decision: GateDecision,
    *,
    order: OrderPayload,
    band: PriceBand,
    secret: bytes,
    expires_at: dt.datetime,
    now: dt.datetime,
) -> str:
    """Mint an ``arc2`` token for a passed decision over ``band``.

    ``order`` is the proposal's order at its (mid) limit, which must equal
    ``band.lo``: the band starts where the gate-checked limit is.
    """
    _check_secret(secret)
    _check_aware(now)
    _check_aware(expires_at)
    if not decision.passed or decision.violations:
        raise TokenError(TokenErrorCode.NOT_PASSED, "gate decision did not pass")
    if not hmac.compare_digest(decision.proposal_hash.encode(), proposal_hash.encode()):
        raise TokenError(TokenErrorCode.PROPOSAL_MISMATCH, "decision is for another proposal")
    if order.limit_price != band.lo:
        msg = f"band starts at {band.lo}, order limit is {order.limit_price}"
        raise TokenError(TokenErrorCode.BAD_PAYLOAD, msg)
    epoch = int(expires_at.timestamp())
    if epoch <= now.timestamp() or epoch >= 10**10:
        raise TokenError(TokenErrorCode.BAD_EXPIRY, "expiry is not in the future")
    unsigned = BandToken(
        proposal_prefix=_hash_prefix(proposal_hash),
        legs_prefix=_hash_prefix(legs_hash(order)),
        lo_cents=_to_cents(band.lo),
        hi_cents=_to_cents(band.hi),
        max_steps=band.max_steps,
        expires_epoch=epoch,
        signature="",
    )
    return unsigned.model_copy(update={"signature": _sign(secret, unsigned.body)}).encode()


def verify_band(
    token: object,
    *,
    secret: bytes,
    now: dt.datetime,
    proposal_hash: str | None = None,
    order: OrderPayload | None = None,
    step: int | None = None,
) -> BandToken:
    """Verify an ``arc2`` token; return it parsed, or raise ``TokenError`` (fail closed).

    Always checks the signature, the expiry and that the signed band is valid.
    ``order``: its legs/qty must match and its limit must lie inside the band.
    ``step``: must be within ``0..max_steps``.
    """
    _check_secret(secret)
    _check_aware(now)
    t = BandToken.parse(token)
    if not hmac.compare_digest(_sign(secret, t.body).encode(), t.signature.encode()):
        raise TokenError(TokenErrorCode.BAD_SIGNATURE, "signature does not verify")
    if now.timestamp() >= t.expires_epoch:
        raise TokenError(TokenErrorCode.EXPIRED, f"token expired at {t.expires_at.isoformat()}")
    try:
        band = t.band
    except ValueError as exc:
        raise TokenError(TokenErrorCode.MALFORMED, f"signed band is invalid: {exc}") from exc
    if proposal_hash is not None and not hmac.compare_digest(
        _hash_prefix(proposal_hash).encode(), t.proposal_prefix.encode()
    ):
        raise TokenError(TokenErrorCode.PROPOSAL_MISMATCH, "token is for another proposal")
    if order is not None:
        if not hmac.compare_digest(_hash_prefix(legs_hash(order)).encode(), t.legs_prefix.encode()):
            raise TokenError(TokenErrorCode.ORDER_MISMATCH, "token is for different legs or qty")
        if not band.contains(order.limit_price):
            msg = f"limit {order.limit_price} outside band [{band.lo}, {band.hi}]"
            raise TokenError(TokenErrorCode.OUT_OF_BAND, msg)
    if step is not None and not 0 <= step <= t.max_steps:
        raise TokenError(TokenErrorCode.BAD_STEP, f"step {step} outside 0..{t.max_steps}")
    return t


def verify_any(token: object, *, secret: bytes, now: dt.datetime) -> GateToken | BandToken:
    """Signature + expiry check for either version (``arc execute --token``)."""
    if isinstance(token, str) and token.startswith(f"{BAND_TOKEN_VERSION}."):
        return verify_band(token, secret=secret, now=now)
    return verify(token, secret=secret, now=now)


def verify_client_order_id(
    coid: object,
    *,
    secret: bytes,
    now: dt.datetime,
    order: OrderPayload,
    proposal_hash: str | None = None,
) -> tuple[GateToken | BandToken, int]:
    """Verify a broker ``client_order_id`` against the exact order it is sent with.

    ``arc2``: ``<token>.s<k>`` with ``k <= max_steps`` and the limit inside the band.
    ``arc1`` (legacy): the bare token, bound to this exact order; always step 0.
    Returns ``(token, step)``.
    """
    if not isinstance(coid, str) or len(coid) > _MAX_ID:
        if coid is None or coid == "":
            raise TokenError(TokenErrorCode.MISSING, "no client_order_id")
        raise TokenError(TokenErrorCode.MALFORMED, "client_order_id is not a short string")
    if coid.startswith(f"{BAND_TOKEN_VERSION}."):
        m = _STEP_RE.fullmatch(coid)
        if m is None:
            raise TokenError(TokenErrorCode.BAD_STEP, "arc2 order id needs a .s<k> step suffix")
        k = int(m["k"])
        t = verify_band(
            m["tok"], secret=secret, now=now, proposal_hash=proposal_hash, order=order, step=k
        )
        return t, k
    return verify(coid, secret=secret, now=now, proposal_hash=proposal_hash, order=order), 0
