"""Expiry guard (E11.4, D73): flat by DTE 1, an expiry-day cutoff, DNE, classification.

Pure functions over their inputs: no DB, no network, no broker, no LLM. The
callers are the deterministic ``exits.mandatory`` step (:mod:`arc.positions.steps`)
and the post-market reconcile (:mod:`arc.reconcile.engine`).

Rules (owner decisions 2026-10-09, D73):

* **Closing window.** ``in_window`` from the session before ``flat_by`` on, i.e.
  ``dte <= flat_by_dte + 1`` calendar days for a mid-week expiry (Wed for a Fri
  expiry), and Thu for a Mon expiry (flat_by = Fri, the last session before the
  weekend), so a weekend never eats the retry session. Outside the window a
  structure gets one close proposal
  per ET day (unchanged); inside it up to ``attempts_per_day``, each a fresh
  proposal (new mid, new D24 band with ``steps`` improvement steps, gate, token,
  approval).
* **Flat by.** ``flat_by = expiry - flat_by_dte`` calendar days (the previous
  session when that is not one). A structure still open from 15 minutes before
  that session's close is *not flat*: alert + an opens-only halt.
* **Expiry-day cutoff.** ``session_close(expiry) - cutoff_minutes_before_close``
  (15:15 ET, 12:15 on an early close). Nothing is proposed from the cutoff on;
  near-the-money long legs get a do-not-exercise instruction (``dne``).
* **Classification.** A leg the broker no longer holds on/after expiry is
  ``expired`` / ``exercised`` / ``assigned`` from the broker's OPEXP / OPEXC /
  OPASN activities, else inferred from the share footprint (±100 x ratio x
  contracts per leg, sign from the leg). Anything else is ``unknown`` and the
  structure stays a mismatch (fail closed).
"""

from __future__ import annotations

import datetime as _dt
import itertools
from decimal import Decimal
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field

from arc.models import LegIntent
from arc.pricing.bs import OptionKind
from arc.structures import parse_occ
from arc.utils.calendar import ET, dte_calendar, is_session, previous_session, session_close

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from arc.models import Structure
    from arc.structures import OccSymbol

__all__ = [
    "NOT_FLAT_LEAD",
    "ActivityType",
    "BrokerActivityView",
    "ClosingWindow",
    "DneMode",
    "ExpiryClassification",
    "ExpiryGuard",
    "LegEvent",
    "classify_expiry",
    "closing_window",
    "dne_candidates",
    "expiry_cutoff",
    "intrinsic",
    "leg_shares",
    "may_attempt",
    "not_flat",
]

DneMode = Literal["never", "near_money", "all_longs"]
ActivityType = Literal["OPASN", "OPEXC", "OPEXP", "OPTRD"]

# Rule 3: "end of the flat_by session" = from this long before its close.
NOT_FLAT_LEAD = _dt.timedelta(minutes=15)
_SHARES_PER_CONTRACT = 100


class ExpiryGuard(BaseModel):
    """``positions.expiry_guard`` in ``config/exits.yaml`` (D73 defaults)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    flat_by_dte: int = Field(
        1, ge=0, le=5, description="Calendar days before expiry by whose session end every "
        "structure must be closed (1 = the day before)"
    )  # fmt: skip
    attempts_per_day: int = Field(
        4, ge=1, le=8, description="Close proposals per structure per ET day inside the window"
    )
    steps: int = Field(
        6, ge=0, le=9, description="D24 improvement steps of a close inside the window"
    )
    cutoff_minutes_before_close: int = Field(
        45, ge=15, le=120, description="Expiry day: no proposals from close minus this"
    )
    dne: DneMode = Field(
        "near_money", description="Do-not-exercise for long legs at the cutoff (paper only)"
    )
    pin_band: Decimal = Field(
        Decimal("0.50"), ge=0, description="near_money: |spot - strike| <= this, $ per share"
    )


class ClosingWindow(BaseModel):
    """Where a structure stands against the guard on *today*."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    expiry: _dt.date
    dte: int
    flat_by: _dt.date
    in_window: bool
    expiry_day: bool
    cutoff: _dt.datetime | None = Field(None, description="Expiry-day cutoff (ET)")
    attempts_allowed: int = Field(..., ge=0, description="Close proposals allowed today")


class BrokerActivityView(BaseModel):
    """The fields of a broker non-trade activity the classifier reads."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    id: str
    activity_type: ActivityType
    symbol: str
    qty: Decimal = Decimal(0)


class LegEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    occ: str
    kind: Literal["expired", "exercised", "assigned", "closed_by_broker", "unknown"]
    via: Literal["activity", "inferred"]
    shares: int = Field(0, description="Signed shares this leg delivered (+ long / - short)")
    value: Decimal | None = Field(
        None, description="Per-share close value of the leg (ladder sign: + paid / - received)"
    )
    activity_id: str | None = None


class ExpiryClassification(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    structure_id: str
    root: str
    legs: list[LegEvent]
    close_net: Decimal | None = Field(None, description="Per-share close of the structure")
    shares_net: int = 0
    basis: Decimal | None = Field(None, description="Per-share basis of shares_net (the settle)")
    confident: bool

    @property
    def events(self) -> list[LegEvent]:
        """Legs the broker exercised or assigned."""
        return [leg for leg in self.legs if leg.kind in ("exercised", "assigned")]


# ---------------------------------------------------------------------------
# Window / cutoff
# ---------------------------------------------------------------------------


def expiry_cutoff(expiry_day: _dt.date, guard: ExpiryGuard) -> _dt.datetime:
    """``session_close(expiry_day) - cutoff_minutes_before_close`` (ET). Raises off-session."""
    close = session_close(expiry_day)
    return close - _dt.timedelta(minutes=guard.cutoff_minutes_before_close)


def _flat_by(expiry: _dt.date, guard: ExpiryGuard) -> _dt.date:
    d = expiry - _dt.timedelta(days=guard.flat_by_dte)
    return d if is_session(d) else previous_session(d)


def closing_window(expiry: _dt.date, today: _dt.date, guard: ExpiryGuard) -> ClosingWindow:
    """The guard's view of a structure expiring *expiry* on *today*.

    ``attempts_allowed``: 1 outside the window (today's rule), ``attempts_per_day``
    inside it, 0 once expired (``dte < 0``: the reconcile settles it, nothing is
    priced on a dead chain).
    """
    dte = dte_calendar(today, expiry)
    flat_by = _flat_by(expiry, guard)
    in_window = dte <= guard.flat_by_dte + 1 or today >= previous_session(flat_by)
    expiry_day = dte == 0
    cutoff = expiry_cutoff(expiry, guard) if expiry_day and is_session(expiry) else None
    if dte < 0:
        allowed = 0
    elif in_window:
        allowed = guard.attempts_per_day
    else:
        allowed = 1
    return ClosingWindow(
        expiry=expiry,
        dte=dte,
        flat_by=flat_by,
        in_window=in_window,
        expiry_day=expiry_day,
        cutoff=cutoff,
        attempts_allowed=allowed,
    )


def may_attempt(attempts_today: int, window: ClosingWindow, now: _dt.datetime) -> bool:
    """May one more close be proposed for the structure now?"""
    if window.attempts_allowed == 0:
        return False
    if window.cutoff is not None and now.astimezone(ET) >= window.cutoff:
        return False
    return attempts_today < window.attempts_allowed


def not_flat(window: ClosingWindow, now: _dt.datetime) -> bool:
    """Rule 3: still open from ``session_close(flat_by) - 15 min`` on (and not expired)."""
    if window.dte < 0:
        return False
    return now.astimezone(ET) >= session_close(window.flat_by) - NOT_FLAT_LEAD


# ---------------------------------------------------------------------------
# Do-not-exercise
# ---------------------------------------------------------------------------


def dne_candidates(structure: Structure, spot: Decimal | None, guard: ExpiryGuard) -> list[str]:
    """OCC symbols of the long legs that get a DNE instruction at the cutoff.

    ``never`` -> none; ``all_longs`` -> every long leg; ``near_money`` -> long legs
    with ``|spot - strike| <= pin_band`` (none without a spot: fail safe, Alpaca's
    own expiry handling applies). Short legs never (not applicable).
    """
    if guard.dne == "never":
        return []
    out: list[str] = []
    for leg in structure.legs:
        if leg.side != LegIntent.LONG:
            continue
        occ = parse_occ(leg.occ_symbol)
        if (
            guard.dne == "all_longs"
            or spot is not None
            and abs(spot - occ.strike) <= guard.pin_band
        ):
            out.append(occ.format())
    return out


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def leg_shares(side: LegIntent, kind: OptionKind, ratio: int, contracts: int) -> int:
    """Signed shares a leg delivers when exercised/assigned.

    Long call / short put -> +shares; long put / short call -> -shares.
    """
    n = _SHARES_PER_CONTRACT * ratio * contracts
    plus = (side == LegIntent.LONG) == (kind == OptionKind.CALL)
    return n if plus else -n


def intrinsic(kind: OptionKind, strike: Decimal, spot: Decimal) -> Decimal:
    if kind == OptionKind.CALL:
        return max(spot - strike, Decimal(0))
    return max(strike - spot, Decimal(0))


def _value(
    side: LegIntent, ratio: int, kind: OptionKind, strike: Decimal, spot: Decimal
) -> Decimal:
    """Per-share close value of one leg at *spot* (ladder sign: + paid / - received)."""
    sign = -1 if side == LegIntent.LONG else 1
    return sign * ratio * intrinsic(kind, strike, spot)


def classify_expiry(
    *,
    structure_id: str,
    structure: Structure,
    contracts: int,
    held: set[str],
    shares: int,
    activities: Sequence[BrokerActivityView] | None,
    settle: Decimal | None,
) -> ExpiryClassification:
    """Classify every leg of an expired structure the broker no longer holds.

    *held*: OCC symbols (formatted) the broker still holds; *shares*: the signed
    share position in the root attributable to this structure (before any unwind
    today); *activities*: the broker's OPEXP/OPEXC/OPASN rows (``None`` = the
    adapter has no activities feed); *settle*: the root's close on the expiry day.

    Booking needs *settle* (legs at intrinsic, shares at basis = settle). A leg
    still held, a missing settle, or a share footprint no leg set explains makes
    the classification not ``confident``.
    """
    occs = [parse_occ(leg.occ_symbol) for leg in structure.legs]
    root = occs[0].root
    by_symbol: dict[str, BrokerActivityView] = {}
    for a in activities or []:
        if a.activity_type == "OPTRD":
            continue
        try:
            by_symbol[parse_occ(a.symbol).format()] = a
        except ValueError:
            continue

    events: list[LegEvent] = []
    unresolved: list[int] = []  # leg indexes with no activity
    for i, (leg, occ) in enumerate(zip(structure.legs, occs, strict=True)):
        sym = occ.format()
        if sym in held:
            events.append(LegEvent(occ=sym, kind="unknown", via="inferred"))
            continue
        act = by_symbol.get(sym)
        delivered = leg_shares(leg.side, occ.kind, leg.ratio, contracts)
        value = (
            None if settle is None else _value(leg.side, leg.ratio, occ.kind, occ.strike, settle)
        )
        if act is None:
            unresolved.append(i)
            events.append(LegEvent(occ=sym, kind="unknown", via="inferred", value=value))
        elif act.activity_type == "OPEXP":
            events.append(LegEvent(occ=sym, kind="expired", via="activity", shares=0,
                                   value=Decimal(0), activity_id=act.id))  # fmt: skip
        else:
            kind: Literal["exercised", "assigned"] = (
                "exercised" if act.activity_type == "OPEXC" else "assigned"
            )
            events.append(LegEvent(occ=sym, kind=kind, via="activity", shares=delivered,
                                   value=value, activity_id=act.id))  # fmt: skip

    known = sum(e.shares for e in events)
    rest = shares - known
    if unresolved:
        resolved = _infer(structure, occs, contracts, unresolved, rest, settle)
        if resolved is not None:
            for i, delivered in resolved.items():
                leg = structure.legs[i]
                kind_i: Literal["expired", "exercised", "assigned"] = (
                    "expired"
                    if delivered == 0
                    else ("exercised" if leg.side == LegIntent.LONG else "assigned")
                )
                ev = events[i]
                events[i] = ev.model_copy(
                    update={
                        "kind": kind_i,
                        "shares": delivered,
                        "value": Decimal(0) if delivered == 0 else ev.value,
                    }
                )
    shares_net = sum(e.shares for e in events)
    all_known = all(e.kind != "unknown" for e in events)
    confident = all_known and settle is not None and shares_net == shares
    close_net = (
        sum((e.value or Decimal(0) for e in events), start=Decimal(0)) if confident else None
    )
    return ExpiryClassification(
        structure_id=structure_id,
        root=root,
        legs=events,
        close_net=close_net,
        shares_net=shares_net,
        basis=settle if confident and shares_net else None,
        confident=confident,
    )


def _infer(
    structure: Structure,
    occs: Sequence[OccSymbol],
    contracts: int,
    idx: Sequence[int],
    rest: int,
    settle: Decimal | None,
) -> Mapping[int, int] | None:
    """Which unresolved legs delivered the *rest* of the share footprint.

    With a settle: the in-the-money legs (intrinsic > 0) must explain *rest*
    exactly; out-of-the-money legs expired. Without one: exactly one subset of
    the legs may explain it (else ambiguous -> ``None``).
    """
    deliver = {
        i: leg_shares(structure.legs[i].side, occs[i].kind, structure.legs[i].ratio, contracts)
        for i in idx
    }
    if settle is not None:
        itm = {i for i in idx if intrinsic(occs[i].kind, occs[i].strike, settle) > 0}
        if sum(deliver[i] for i in itm) == rest:
            return {i: deliver[i] if i in itm else 0 for i in idx}
        return None
    matches = [
        set(combo)
        for r in range(len(idx) + 1)
        for combo in itertools.combinations(idx, r)
        if sum(deliver[i] for i in combo) == rest
    ]
    if len(matches) != 1:
        return None
    (pick,) = matches
    return {i: deliver[i] if i in pick else 0 for i in idx}
