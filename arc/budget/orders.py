"""Daily options order budget: pure core plus a thin repo reader (E6.5, D32).

Definitions
-----------
* **One order** = one broker submission (every D24 ladder attempt, whatever its
  outcome, for opens and closes). A ``SubmitRefused`` that never reached the
  broker is not one. Locally that is one ``orders`` row; the broker's own list
  of option orders for the day (dashboard orders included) is the cross-check
  and ``used = max(local, broker) + reserved``.
* **Reserved** = the remaining worst-case attempts of every execution still
  ``working`` (a ladder in flight elsewhere), so concurrent loops cannot
  overshoot the cap.
* **Day** = the ET calendar date (``arc.utils.calendar.ET``).

Tiers (``used`` against the config, ``limit = daily_max``)::

    used <  restrict_at              normal
    used >= restrict_at              restrictive      (stricter selection)
    used >= limit - close_reserve    opens_exhausted  (closes still allowed)
    used >= limit                    exhausted        (closes refused too)

Everything in the "pure" section reads only its arguments; the gate rule in
:mod:`arc.gate.rules` uses :func:`can_submit` semantics without importing
this package (the gate stays self-contained).
"""

from __future__ import annotations

import datetime as _dt
from enum import StrEnum
from typing import TYPE_CHECKING, Any

import structlog
from pydantic import BaseModel, ConfigDict, Field, model_validator

from arc.context.ttl import to_db
from arc.utils.calendar import ET

if TYPE_CHECKING:
    import sqlite3

    from arc.config import ArcSettings

#: Any broker adapter. Only ``option_orders_since`` is used (looked up with
#: ``getattr``), so the budget does not import :mod:`arc.broker`: that keeps it
#: importable from the read-only tower (E8.7d, import-linter contract).
AnyBroker = object

log = structlog.get_logger(__name__)

_FORBID = ConfigDict(extra="forbid", frozen=True)

#: Code-level ceiling on ``order_budget_daily_max``: no config or Slack change can
#: raise the cap above this (D32: 200 options orders per ET day).
HARD_CEILING = 200

#: ``order_events.detail`` prefix the ladder writes when ``submit()`` refused an
#: attempt before the broker saw it. Such rows never count against the budget.
REFUSED_DETAIL_PREFIX = "refused before the broker"

__all__ = [
    "HARD_CEILING",
    "OrderBudget",
    "OrderBudgetConfig",
    "OrderCount",
    "REFUSED_DETAIL_PREFIX",
    "RestrictiveConfig",
    "RestrictiveFloors",
    "Tier",
    "budget_state",
    "can_submit",
    "count_orders",
    "current_budget",
    "effective_cooldown",
    "effective_improvement_steps",
    "restrictive_floors",
    "tier_settings",
    "worst_case_attempts",
]


# ---------------------------------------------------------------------------
# Contracts
# ---------------------------------------------------------------------------


class Tier(StrEnum):
    NORMAL = "normal"
    RESTRICTIVE = "restrictive"
    OPENS_EXHAUSTED = "opens_exhausted"
    EXHAUSTED = "exhausted"

    @property
    def restricted(self) -> bool:
        """Restrictive selection applies (this tier or worse)."""
        return self is not Tier.NORMAL

    @property
    def opens_allowed(self) -> bool:
        return self in (Tier.NORMAL, Tier.RESTRICTIVE)

    @property
    def closes_allowed(self) -> bool:
        return self is not Tier.EXHAUSTED


class RestrictiveConfig(BaseModel):
    """What the pipeline tightens once ``used >= restrict_at`` (D32)."""

    model_config = _FORBID

    director_max_shortlist: int = Field(1, ge=0, le=10)
    max_new_opens_per_loop: int = Field(1, ge=0, le=10)
    min_net_ev_multiplier: float = Field(1.5, ge=1.0, le=10.0)
    min_pop_delta_pp: float = Field(5.0, ge=0.0, le=50.0)
    max_improvement_steps: int = Field(2, ge=0, le=9)
    dedupe_cooldown_multiplier: float = Field(2.0, ge=1.0, le=10.0)


class OrderBudgetConfig(BaseModel):
    """The budget knobs (``ArcSettings.order_budget_*``), validated together."""

    model_config = _FORBID

    daily_max: int = Field(HARD_CEILING, ge=1, le=HARD_CEILING)
    restrict_at: int = Field(100, ge=0)
    close_reserve: int = Field(25, ge=0)
    restrictive: RestrictiveConfig = Field(default_factory=RestrictiveConfig)

    @model_validator(mode="after")
    def _ordered(self) -> OrderBudgetConfig:
        if self.close_reserve >= self.daily_max:
            msg = f"close_reserve {self.close_reserve} must be below daily_max {self.daily_max}"
            raise ValueError(msg)
        if self.restrict_at > self.daily_max - self.close_reserve:
            msg = (
                f"restrict_at {self.restrict_at} must not exceed the open limit "
                f"{self.daily_max - self.close_reserve} (daily_max - close_reserve)"
            )
            raise ValueError(msg)
        return self

    @property
    def open_limit(self) -> int:
        """Opens stop once ``used`` reaches this (``daily_max - close_reserve``)."""
        return self.daily_max - self.close_reserve

    @classmethod
    def from_settings(cls, settings: ArcSettings) -> OrderBudgetConfig:
        return cls(
            daily_max=settings.order_budget_daily_max,
            restrict_at=settings.order_budget_restrict_at,
            close_reserve=settings.order_budget_close_reserve,
            restrictive=RestrictiveConfig(
                director_max_shortlist=settings.order_budget_restrictive_director_max_shortlist,
                max_new_opens_per_loop=settings.order_budget_restrictive_max_new_opens_per_loop,
                min_net_ev_multiplier=settings.order_budget_restrictive_min_net_ev_multiplier,
                min_pop_delta_pp=settings.order_budget_restrictive_min_pop_delta_pp,
                max_improvement_steps=settings.order_budget_restrictive_max_improvement_steps,
                dedupe_cooldown_multiplier=(
                    settings.order_budget_restrictive_dedupe_cooldown_multiplier
                ),
            ),
        )


class OrderCount(BaseModel):
    """What :func:`count_orders` saw. ``broker`` is None when it was not checked."""

    model_config = _FORBID

    day: _dt.date
    local: int = Field(ge=0)
    broker: int | None = Field(None, ge=0)
    reserved: int = Field(0, ge=0)

    @property
    def used(self) -> int:
        return max(self.local, self.broker or 0) + self.reserved

    @property
    def mismatch(self) -> bool:
        return self.broker is not None and self.broker != self.local


class OrderBudget(BaseModel):
    """The budget as of one count (D32); stored in manifests and heartbeats."""

    model_config = _FORBID

    day: _dt.date
    used: int = Field(ge=0)
    limit: int = Field(ge=1)
    restrict_at: int = Field(ge=0)
    close_reserve: int = Field(ge=0)
    tier: Tier
    remaining_opens: int = Field(ge=0)
    remaining_total: int = Field(ge=0)
    # provenance of ``used`` (None when built from a bare count)
    local: int | None = None
    broker: int | None = None
    reserved: int | None = None

    @property
    def open_limit(self) -> int:
        return self.limit - self.close_reserve

    def summary(self) -> str:
        return f"orders today {self.used}/{self.limit} ({self.tier.value})"

    def brief(self) -> dict[str, Any]:
        """The manifest / heartbeat form: ``used``, ``limit``, ``tier``."""
        return {"used": self.used, "limit": self.limit, "tier": self.tier.value}


# ---------------------------------------------------------------------------
# Pure rules
# ---------------------------------------------------------------------------


def tier_for(used: int, cfg: OrderBudgetConfig) -> Tier:
    if used >= cfg.daily_max:
        return Tier.EXHAUSTED
    if used >= cfg.open_limit:
        return Tier.OPENS_EXHAUSTED
    if used >= cfg.restrict_at:
        return Tier.RESTRICTIVE
    return Tier.NORMAL


def budget_state(
    used: int, cfg: OrderBudgetConfig, *, day: _dt.date, count: OrderCount | None = None
) -> OrderBudget:
    """Pure: the :class:`OrderBudget` for *used* orders under *cfg*."""
    if used < 0:
        msg = f"used must be >= 0, got {used}"
        raise ValueError(msg)
    return OrderBudget(
        day=day,
        used=used,
        limit=cfg.daily_max,
        restrict_at=cfg.restrict_at,
        close_reserve=cfg.close_reserve,
        tier=tier_for(used, cfg),
        remaining_opens=max(0, cfg.open_limit - used),
        remaining_total=max(0, cfg.daily_max - used),
        local=None if count is None else count.local,
        broker=None if count is None else count.broker,
        reserved=None if count is None else count.reserved,
    )


def can_submit(used: int, cfg: OrderBudgetConfig, *, kind: str, attempts: int = 1) -> bool:
    """May *attempts* more orders of *kind* (``open`` | ``close``) be sent?

    Opens must fit under ``daily_max - close_reserve``; closes under ``daily_max``.
    """
    if kind not in ("open", "close"):
        msg = f"kind must be 'open' or 'close', got {kind!r}"
        raise ValueError(msg)
    if attempts < 1:
        msg = f"attempts must be >= 1, got {attempts}"
        raise ValueError(msg)
    cap = cfg.daily_max if kind == "close" else cfg.open_limit
    return used + attempts <= cap


def effective_improvement_steps(settings: ArcSettings, tier: Tier) -> int:
    """The ladder's improvement steps under *tier* (restrictive caps them)."""
    steps = settings.execution_improvement_steps
    if tier.restricted:
        steps = min(steps, settings.order_budget_restrictive_max_improvement_steps)
    return steps


def worst_case_attempts(settings: ArcSettings, tier: Tier) -> int:
    """``1 + effective improvement steps``: what one proposal may cost the budget."""
    return 1 + effective_improvement_steps(settings, tier)


def tier_settings(settings: ArcSettings, tier: Tier) -> ArcSettings:
    """*settings* with the tier-adjusted ladder cap, so the band, the gate and the
    minted token all see the same number of attempts."""
    steps = effective_improvement_steps(settings, tier)
    if steps == settings.execution_improvement_steps:
        return settings
    return settings.model_copy(update={"execution_improvement_steps": steps})


def effective_cooldown(base: _dt.timedelta, tier: Tier, cfg: RestrictiveConfig) -> _dt.timedelta:
    """The dedupe cooldown under *tier* (E5.9 consumes this; restrictive multiplies it)."""
    if not tier.restricted:
        return base
    return base * cfg.dedupe_cooldown_multiplier


class RestrictiveFloors(BaseModel):
    """The stricter Net EV / PoP floors one candidate must clear in the restrictive tier."""

    model_config = _FORBID

    net_ev_floor: float
    pop_floor: float

    def passes(self, net_ev: float | None, pop: float | None) -> tuple[bool, str]:
        """``(ok, why)``: fails closed when either managed number is missing."""
        if net_ev is None or pop is None:
            return False, "no managed exit model (restrictive tier requires one)"
        fails = []
        if net_ev < self.net_ev_floor:
            fails.append(f"managed net EV ${net_ev:,.2f} < floor ${self.net_ev_floor:,.2f}")
        if pop < self.pop_floor:
            fails.append(f"managed PoP {pop:.1%} < floor {self.pop_floor:.1%}")
        return not fails, "; ".join(fails)


def restrictive_floors(
    cfg: RestrictiveConfig,
    *,
    base_net_ev_floor: float,
    base_pop_floor: float,
    round_trip_cost: float,
    max_loss: float | None,
    max_gain: float | None,
) -> RestrictiveFloors:
    """Pure: the restrictive-tier floors for one structure ($ per contract, PoP 0..1).

    * Net EV floor = ``min_net_ev_multiplier × max(base floor, round-trip cost)``:
      the edge must clear its own friction with a margin, even when the normal
      floor is 0.
    * PoP floor = ``max(base floor, breakeven PoP) + min_pop_delta_pp``, where the
      breakeven PoP is ``max_loss / (max_loss + max_gain)`` (the win rate a
      binary-payoff structure needs for zero EV). Unbounded max gain (a long
      option) has no breakeven, so only the base floor is raised.
    """
    ev_base = max(base_net_ev_floor, round_trip_cost)
    pop_base = base_pop_floor
    if max_loss is not None and max_gain is not None and max_loss + max_gain > 0:
        pop_base = max(pop_base, max_loss / (max_loss + max_gain))
    return RestrictiveFloors(
        net_ev_floor=cfg.min_net_ev_multiplier * ev_base,
        pop_floor=min(1.0, pop_base + cfg.min_pop_delta_pp / 100.0),
    )


# ---------------------------------------------------------------------------
# I/O: the count
# ---------------------------------------------------------------------------


def _day_bounds(day: _dt.date) -> tuple[_dt.datetime, _dt.datetime]:
    start = _dt.datetime.combine(day, _dt.time(0), tzinfo=ET)
    return start, start + _dt.timedelta(days=1)


def _local_count(conn: sqlite3.Connection, day: _dt.date) -> int:
    """``orders`` rows created on *day*, minus attempts refused before the broker."""
    lo, hi = _day_bounds(day)
    row = conn.execute(
        """SELECT COUNT(*) FROM orders o
           WHERE o.created_at >= ? AND o.created_at < ?
             AND NOT EXISTS (
               SELECT 1 FROM order_events e
                WHERE e.order_id = o.id AND e.to_state = 'cancelled' AND e.detail LIKE ?)""",
        (to_db(lo), to_db(hi), REFUSED_DETAIL_PREFIX + "%"),
    ).fetchone()
    return int(row[0])


def _reserved(conn: sqlite3.Connection, exclude_proposal_hash: str | None) -> int:
    """Remaining worst-case attempts of every ladder still ``working``."""
    rows = conn.execute(
        "SELECT proposal_hash, max_steps, attempts FROM executions WHERE status = 'working'"
    ).fetchall()
    total = 0
    for r in rows:
        if exclude_proposal_hash is not None and r[0] == exclude_proposal_hash:
            continue
        total += max(0, int(r[1]) + 1 - int(r[2]))
    return total


def _broker_count(broker: AnyBroker | None, day: _dt.date) -> int | None:
    """Option orders the broker lists for *day*, or None when unavailable."""
    if broker is None:
        return None
    lister = getattr(broker, "option_orders_since", None)
    if lister is None:
        return None
    lo, hi = _day_bounds(day)
    try:
        ids = lister(lo)
    except Exception as exc:  # noqa: BLE001 - the local count still stands; log it
        log.warning("order_budget.broker_unavailable", error=f"{type(exc).__name__}: {exc}")
        return None
    return sum(1 for o in ids if o.submitted_at is None or lo <= o.submitted_at < hi)


def count_orders(
    conn: sqlite3.Connection,
    broker: AnyBroker | None,
    day: _dt.date,
    *,
    exclude_proposal_hash: str | None = None,
) -> OrderCount:
    """Local ``orders`` rows for *day*, the broker's option orders (when a broker
    is given and supports ``option_orders_since``) and the reserved attempts of
    working ladders (*exclude_proposal_hash* leaves the caller's own ladder out).
    """
    local = _local_count(conn, day)
    remote = _broker_count(broker, day)
    reserved = _reserved(conn, exclude_proposal_hash)
    count = OrderCount(day=day, local=local, broker=remote, reserved=reserved)
    if count.mismatch:
        log.warning("order_budget.mismatch", day=day.isoformat(), local=local, broker=remote)
    return count


def current_budget(
    conn: sqlite3.Connection,
    broker: AnyBroker | None,
    settings: ArcSettings,
    *,
    now: _dt.datetime,
    exclude_proposal_hash: str | None = None,
) -> OrderBudget:
    """:func:`count_orders` for the ET day of *now*, as an :class:`OrderBudget`."""
    day = now.astimezone(ET).date()
    cfg = OrderBudgetConfig.from_settings(settings)
    count = count_orders(conn, broker, day, exclude_proposal_hash=exclude_proposal_hash)
    return budget_state(count.used, cfg, day=day, count=count)
