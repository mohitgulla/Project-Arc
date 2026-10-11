"""Historical earnings dates detected from the option chain (E7.10, D84; no paid source).

**Detection** (one ticker, sessions in order; ``prev`` = the session before ``d``):

* on ``prev`` take the *front* expiry = the nearest expiry strictly after ``d`` (so it
  spans the ``prev`` → ``d`` gap) and the *next* expiry after it, both with an ATM IV
  (:func:`arc.backtest.ivhist.term_points`);
* **jump**: ``front_iv(prev) / next_iv(prev) − 1 ≥ earn_iv_jump`` — an event priced
  into the front expiry only (normal term structure is flat or upward);
* **crush**: the same front expiry on ``d``: ``1 − front_iv(d) / front_iv(prev) ≥
  earn_crush`` — the event passed overnight;
* **market-relative** (``earn_market_ref``, default SPY): the same jump and crush are
  measured on the reference at the ticker's front/next expiries, and only the *excess* over a
  positive market value counts (``jump − max(mkt_jump, 0)``, likewise the crush). A
  market-wide vol event (an election, FOMC, a tariff pause) crushes every front
  expiry at once and is not an earnings event; on the real 2024–26 Alpaca history the
  raw rule fired on those days and the min-gap rule then hid the true report after;
* both → an event on ``d`` (the reaction session; a report after ``prev``'s close or
  before ``d``'s open), detected at ``d``'s close. Events closer than
  ``earn_min_gap_sessions`` to the previous one are dropped (one per quarter).

ETFs never have events.

**``next_earnings(d)``** uses only events *detected by d* (``detected_on ≤ d``): the
last one plus ``earn_projection_days`` (91 days, one quarter), stepped forward a
quarter at a time until it is after ``d``. Before any detection it is unknown
(``None``). This is an **approximation**: the real date wanders by a week or two
around the projection, and the first quarter of a name's history has no estimate.
Using the later detection itself would be look-ahead, so it is never used.
"""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

    from arc.backtest.ivhist import TermPoint

__all__ = [
    "EarningsEvent",
    "EarningsRule",
    "MatchStats",
    "NextEarnings",
    "detect_events",
    "match_events",
    "next_earnings",
]


class EarningsRule(BaseModel):
    """``backtest.data.earnings`` in ``config/ranking.yaml``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    earn_iv_jump: float = Field(0.10, gt=0.0, description="front/next IV − 1 on the prior day")
    earn_crush: float = Field(0.15, gt=0.0, lt=1.0, description="1 − front IV after/before")
    earn_min_gap_sessions: int = Field(30, ge=1)
    earn_projection_days: int = Field(91, ge=1)
    earn_market_ref: str | None = Field(
        "SPY", description="Subtract this name's same-day jump/crush (None = raw rule)"
    )


class EarningsEvent(BaseModel):
    """One detected earnings event."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ticker: str
    day: dt.date = Field(..., description="Reaction session (the IV crush is on this close)")
    detected_on: dt.date = Field(..., description="First close at which it is known (= day)")
    front_expiration: dt.date
    jump: float = Field(..., description="front/next IV − 1 on the prior session")
    crush: float = Field(..., description="1 − front IV on day / prior session")


class NextEarnings(BaseModel):
    """``next_earnings(d)``: the projected date and what it rests on."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    day: dt.date | None = None
    basis: Literal["projected", "unknown", "etf"] = "unknown"
    last_detected: dt.date | None = None


class MatchStats(BaseModel):
    """Detected vs reference (Finnhub) dates on the sessions both cover."""

    model_config = ConfigDict(extra="forbid")

    reference: int = 0
    detected: int = 0
    matched: int = 0
    precision: float | None = None
    recall: float | None = None


def _front_next(points: Sequence[TermPoint], after: dt.date) -> tuple[TermPoint, TermPoint] | None:
    later = sorted((p for p in points if p.expiration > after), key=lambda p: p.expiration)
    return (later[0], later[1]) if len(later) >= 2 else None


def _jump_crush(
    points_by_day: Mapping[dt.date, Sequence[TermPoint]],
    prev: dt.date,
    d: dt.date,
    expiries: tuple[dt.date, dt.date] | None = None,
) -> tuple[float, float, dt.date, dt.date] | None:
    """(jump on prev, crush prev → d, front expiry, next expiry) or None.

    *expiries* pins the (front, next) pair: the market reference is measured on the
    ticker's own expiries, so a daily-expiry index's weekend decay isn't subtracted.
    """
    if prev not in points_by_day or d not in points_by_day:
        return None
    on_prev = {p.expiration: p for p in points_by_day[prev]}
    if expiries is None:
        fn = _front_next(points_by_day[prev], d)
        if fn is None:
            return None
        front, nxt = fn
    else:
        front, nxt = on_prev.get(expiries[0]), on_prev.get(expiries[1])  # type: ignore[assignment]
        if front is None or nxt is None:
            return None
    if nxt.iv <= 0 or front.iv <= 0:
        return None
    after = [p for p in points_by_day[d] if p.expiration == front.expiration]
    if not after:
        return None
    return (
        front.iv / nxt.iv - 1.0,
        1.0 - after[0].iv / front.iv,
        front.expiration,
        nxt.expiration,
    )


def detect_events(
    ticker: str,
    points_by_day: Mapping[dt.date, Sequence[TermPoint]],
    rule: EarningsRule,
    *,
    etf: bool = False,
    market: Mapping[dt.date, Sequence[TermPoint]] | None = None,
) -> list[EarningsEvent]:
    """Events in session order (see the module docstring). Reads day ``d`` and ``prev`` only.

    *market*: the reference's term points (``earn_market_ref``); a day the reference
    can't measure uses the raw values.
    """
    if etf:
        return []
    days = sorted(points_by_day)
    out: list[EarningsEvent] = []
    last_i: int | None = None
    for i in range(1, len(days)):
        prev, d = days[i - 1], days[i]
        jc = _jump_crush(points_by_day, prev, d)
        if jc is None:
            continue
        jump, crush, front_exp, next_exp = jc
        if (
            market is not None
            and (m := _jump_crush(market, prev, d, (front_exp, next_exp))) is not None
        ):
            jump -= max(m[0], 0.0)
            crush -= max(m[1], 0.0)
        if jump < rule.earn_iv_jump or crush < rule.earn_crush:
            continue
        if last_i is not None and i - last_i < rule.earn_min_gap_sessions:
            continue
        last_i = i
        out.append(
            EarningsEvent(
                ticker=ticker.upper(),
                day=d,
                detected_on=d,
                front_expiration=front_exp,
                jump=round(jump, 6),
                crush=round(crush, 6),
            )
        )
    return out


def next_earnings(
    events: Iterable[EarningsEvent], day: dt.date, rule: EarningsRule, *, etf: bool = False
) -> NextEarnings:
    """Projected next earnings date after *day* from events detected by *day* only."""
    if etf:
        return NextEarnings(basis="etf")
    known = sorted(e.day for e in events if e.detected_on <= day)
    if not known:
        return NextEarnings()
    last = known[-1]
    step = dt.timedelta(days=rule.earn_projection_days)
    nxt = last + step
    while nxt <= day:
        nxt += step
    return NextEarnings(day=nxt, basis="projected", last_detected=last)


def match_events(
    detected: Iterable[dt.date],
    reference: Iterable[dt.date],
    sessions: Sequence[dt.date],
    *,
    tolerance_sessions: int = 1,
) -> MatchStats:
    """Precision/recall of *detected* vs *reference* dates inside *sessions*.

    A reference date (Finnhub's report date: the session of a pre-open report, or the
    session before the reaction for an after-close one) matches a detection within
    ``tolerance_sessions`` sessions. Dates outside the covered sessions are ignored.
    """
    if not sessions:
        return MatchStats()
    lo, hi = min(sessions), max(sessions)
    pos = {d: i for i, d in enumerate(sorted(sessions))}

    def idx(d: dt.date) -> int | None:
        if d < lo or d > hi:
            return None
        if d in pos:
            return pos[d]
        later = [i for s, i in pos.items() if s > d]  # a non-session date → next session
        return min(later) if later else None

    det = sorted({i for d in detected if (i := idx(d)) is not None})
    ref = sorted({i for d in reference if (i := idx(d)) is not None})
    matched_ref = {r for r in ref if any(abs(r - x) <= tolerance_sessions for x in det)}
    matched_det = {x for x in det if any(abs(r - x) <= tolerance_sessions for r in ref)}
    return MatchStats(
        reference=len(ref),
        detected=len(det),
        matched=len(matched_ref),
        precision=None if not det else len(matched_det) / len(det),
        recall=None if not ref else len(matched_ref) / len(ref),
    )
