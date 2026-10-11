"""Tiered universe (D56 + D58; cards E13.4 / E13.15 / E13.19) → one active list.

Four tiers, highest precedence first:

* ``core`` — the fixed list (``config/universe.yaml`` ``core:``, or the ``universe``
  D26 override when it has at most :data:`MAX_CORE` names). Never screened.
* ``momentum`` — top holdings of the S&P 500 Momentum index (E12.2), read from the
  latest valid ``universe_tier`` context entry with subject ``momentum``, cut to the
  top ``universe_momentum_size`` rows by rank.
* ``discovery`` — the Scout's ``universe_tier`` entry with subject ``discovery``
  (E13.7), cut to ``universe_discovery_size`` (25); empty until the Scout writes it.
* ``trending`` — D58 (E13.19): the ``universe.trending`` job's ``universe_tier`` entry
  (subject ``trending``), ranked by code from the daily ``retail_buzz`` pull (Reddit +
  Stocktwits), cut to ``universe_trending_size`` (25).

:func:`resolve_active` is pure and deterministic: a name keeps its **highest** tier
(core > momentum > discovery > trending) and records the others in ``also_in``; each
tier is its feed's top ``size`` rows by rank; core and momentum fill the active list
first, then the slots left under ``active_max`` go to discovery and trending by round
robin (D67: D1, T1, D2, T2, …, a tier that runs out spills to the other;
``universe.active_fill: precedence`` restores D58's discovery-before-trending cut).
Members stay grouped by tier, then rank. Every cut name is listed in ``dropped``
(never silently lost).

``market_reference`` (SPY, QQQ, IWM) is not part of the trade list; Research always
writes their ``regime`` entries so the D33 market guard keeps its SPY read.

This module is imported by :mod:`arc.context.kinds` (the two payload models), so at
module level it imports pydantic and :mod:`arc.config` only. Store reads import
:mod:`arc.context.store` lazily.
"""

from __future__ import annotations

import datetime as _dt  # noqa: TC003 - pydantic resolves the field types at runtime
import enum
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Literal

import structlog
from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from arc.config import ArcSettings

log = structlog.get_logger(__name__)

__all__ = [
    "ACTIVE_FILLS",
    "ACTIVE_SUBJECT",
    "DROP_OVER_ACTIVE_CAP",
    "DROP_OVER_TIER_SIZE",
    "MAX_CORE",
    "SEED_TIERS",
    "SHARED_TIERS",
    "TIER_ORDER",
    "ActiveFill",
    "ActiveUniverse",
    "CarryoverKnobs",
    "CarryoverSettings",
    "DroppedMember",
    "Tier",
    "TierInputs",
    "TierMember",
    "UniverseTierPayload",
    "active_by_tier",
    "active_tickers",
    "active_tickers_ro",
    "build_active",
    "core_tickers",
    "ignored_core_override",
    "load_tier_inputs",
    "market_reference",
    "open_underlyings",
    "resolve_active",
    "seed_tickers",
    "tier_floor",
    "tier_membership",
    "tier_sizes",
    "watch_tickers",
    "yaml_core",
]

MAX_CORE = 25  # D58: hard ceiling on the core list (registry `universe` max_items)
ACTIVE_SUBJECT = "active"  # `active_universe` context subject
DROP_OVER_ACTIVE_CAP = "over_active_cap"  # journaled as universe:over_active_cap
DROP_OVER_TIER_SIZE = "over_tier_size"  # the tier's own feed listed more than its size

_FORBID = ConfigDict(extra="forbid")


def _norm(raw: str) -> str:
    """Same rule as :func:`arc.universe.master.normalize_symbol` (not imported: that
    module reaches the broker SDK lazily, and :mod:`arc.context.kinds` imports this one)."""
    return raw.strip().lstrip("$").strip().upper().replace("-", ".").replace("/", ".")


class Tier(enum.StrEnum):
    """Universe tiers, highest precedence first (D58: trending is last)."""

    CORE = "core"
    MOMENTUM = "momentum"
    DISCOVERY = "discovery"
    TRENDING = "trending"


#: D58 precedence order: core > momentum > discovery > trending.
TIER_ORDER: tuple[Tier, ...] = (Tier.CORE, Tier.MOMENTUM, Tier.DISCOVERY, Tier.TRENDING)
#: D67: always filled first, in order (core <= 25 + momentum <= 25 vs the cap of 50).
_HEAD_TIERS: frozenset[Tier] = frozenset({Tier.CORE, Tier.MOMENTUM})
#: D67: the tiers that share the slots left after core + momentum, round robin in this
#: order (Discovery takes the first and the odd slot).
SHARED_TIERS: tuple[Tier, ...] = (Tier.DISCOVERY, Tier.TRENDING)
#: D67: how the shared slots are filled. ``round_robin`` (default) = D1, T1, D2, T2, …
#: with spill; ``precedence`` = D58 (all of discovery before any trending).
ActiveFill = Literal["round_robin", "precedence"]
ACTIVE_FILLS: tuple[str, ...] = ("round_robin", "precedence")
#: Every member, legacy included: sort key for stored history only.
_ALL_TIERS: tuple[Tier, ...] = tuple(Tier)

#: ``active_universe`` rows say which layout resolved them; v1 rows (pre-E13.4)
#: load as ``d51``. Every new resolve is ``d56``.
Model = Literal["d51", "d56"]


class TierMember(BaseModel):
    """One name in one tier (rank 1 = first)."""

    model_config = _FORBID

    ticker: str
    tier: Tier
    rank: int = Field(ge=1)
    source: str
    reason: str = ""
    as_of: _dt.date
    also_in: list[Tier] = Field(
        default_factory=list, description="lower tiers that also listed this name (dedupe)"
    )
    # v3 (E13.19): trending members say how many retail_buzz inputs listed them (2|1)
    inputs: int | None = Field(None, ge=1, description="trending: inputs that listed the name")
    # v4 (D64, E14.7): discovery / trending two-run carry-over (None on other tiers and
    # on pre-D64 rows; arc.universe.carryover parses those rows' score from `reason`).
    score: float | None = Field(None, description="combined score used for ranking (4 dp)")
    score_today: float | None = Field(
        None, description="this run's own score (None = not in this run, carried)"
    )
    score_prev: float | None = Field(
        None, description="the previous run's own score (None = not in the previous run)"
    )
    runs: list[_dt.date] = Field(
        default_factory=list, description="run dates (ET) that listed the name (1 or 2)"
    )
    stance: str | None = Field(None, description="discovery: the Scout's stance")
    origins: list[str] = Field(
        default_factory=list, description="discovery: the Scout's origins (youtube:<slug>)"
    )
    # v5 (E14.8, D64): momentum members carry the SPMO weight the writer ranked by
    weight_pct: float | None = Field(
        None, description="momentum: the name's SPMO weight in percent (e.g. 9.48)"
    )


class CarryoverKnobs(BaseModel):
    """D64: the carry-over knobs a merged ``universe_tier`` entry was built with."""

    model_config = _FORBID

    window_h: int
    w_today: float
    w_prev: float


class CarryoverSettings(BaseModel):
    """D64 (E14.7): ``universe.carryover`` in ``config/routines.yaml`` (D26 tunables).

    Read by both writers of a merged tier (the Scout's discovery, ``universe.trending``);
    :mod:`arc.universe.carryover` applies it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = True
    window_h: Annotated[int, Field(ge=24, le=96)] = 48
    w_today: Annotated[float, Field(ge=0.5, le=1.0)] = 0.6

    @property
    def w_prev(self) -> float:
        return round(1.0 - self.w_today, 6)

    def knobs(self) -> CarryoverKnobs:
        return CarryoverKnobs(window_h=self.window_h, w_today=self.w_today, w_prev=self.w_prev)


class UniverseTierPayload(BaseModel):
    """``universe_tier`` context entry (subject = tier name).

    Writers: the momentum job (E12.2), the Scout (discovery, E13.7) and the
    ``universe.trending`` job (D58, E13.19).
    """

    model_config = _FORBID

    tier: Tier
    members: list[TierMember] = Field(default_factory=list)
    fetched_at: _dt.datetime
    source: str
    source_as_of: _dt.date | None = None
    digest: str = ""
    # v2 (E12.2): where the list came from and whether the source listed fewer rows
    # than the tier wants (Schwab fallback: first 20 rows only).
    url: str = ""
    partial: bool = False
    # v4 (D64, E14.7): the previous entry merged into this one (None = none within the
    # window, or carry-over off) and the knobs used (None = carry-over off).
    merged_from: str | None = None
    merge: CarryoverKnobs | None = None


class DroppedMember(BaseModel):
    model_config = _FORBID

    ticker: str
    tier: Tier
    reason: str
    rank: int | None = Field(None, description="v2 (E13.4): the name's rank in its tier")


class ActiveUniverse(BaseModel):
    """``active_universe`` context entry (subject ``active``): one per resolve.

    v2 (E13.4): ``model`` (the tier layout it was resolved under; v1 rows load as d51).
    """

    model_config = _FORBID

    model: Model = "d51"
    as_of: _dt.date
    members: list[TierMember] = Field(default_factory=list)
    dropped: list[DroppedMember] = Field(default_factory=list)
    counts: dict[str, int] = Field(
        default_factory=dict, description="active members per tier (every tier listed)"
    )
    raw_counts: dict[str, int] = Field(
        default_factory=dict, description="names each tier offered before dedupe and caps"
    )
    expired_tiers: list[Tier] = Field(
        default_factory=list, description="tiers whose latest feed expired (read as empty)"
    )
    config_version: int | None = None
    # v6 (D67, E14.9): how the slots after core + momentum were filled. None on older
    # rows (they were all D58 precedence).
    fill: ActiveFill | None = Field(None, description="D67: round_robin | precedence")
    open_slots: int | None = Field(
        None, description="D67: active_max minus core + momentum (the shared slots R)"
    )
    slots: dict[str, int] = Field(
        default_factory=dict, description="D67: shared slots taken per tier (discovery, trending)"
    )

    @property
    def tickers(self) -> list[str]:
        return [m.ticker for m in self.members]

    def tier_tickers(self, *tiers: Tier) -> list[str]:
        return [m.ticker for m in self.members if m.tier in tiers]


# ---------------------------------------------------------------------------
# Pure resolver
# ---------------------------------------------------------------------------


def resolve_active(
    *,
    core: Sequence[TierMember],
    momentum: Sequence[TierMember] = (),
    discoveries: Sequence[TierMember] = (),
    trending: Sequence[TierMember] = (),
    active_max: int,
    tier_sizes: dict[Tier, int] | None = None,
    as_of: _dt.date,
    config_version: int | None = None,
    expired_tiers: Iterable[Tier] = (),
    fill: ActiveFill = "round_robin",
) -> ActiveUniverse:
    """Dedupe the tiers (highest wins), cut each to its size, cap at *active_max*.

    Each input is taken in its given order after a stable sort by ``rank``. A
    name repeated inside one tier keeps its first row. A tier is its feed's top
    ``size`` rows by rank, cut before dedupe (momentum 25 -> top 20, of which names
    already in core count as ``also_in``). A name cut by a tier's size may still be
    held by a lower tier that lists it (then it is not in ``dropped``). *tier_sizes*
    missing a tier means no per-tier cut. Deterministic: same inputs, same output.

    Core and momentum are kept first (tier order, then rank). *fill* decides the
    ``open_slots`` left under the cap (D67):

    * ``round_robin`` (default): Discovery and Trending alternate by rank, Discovery
      first (D1, T1, D2, T2, …); when one tier runs out the other takes the rest.
    * ``precedence`` (D58): all of Discovery, then Trending (the old cut).

    Members stay grouped by tier, then rank; every name past the cap is ``dropped`` as
    ``over_active_cap`` with its rank.
    """
    sizes = tier_sizes or {}
    by_tier = {
        Tier.CORE: core,
        Tier.MOMENTUM: momentum,
        Tier.DISCOVERY: discoveries,
        Tier.TRENDING: trending,
    }
    raw_counts: dict[str, int] = {}
    kept: dict[str, TierMember] = {}
    order: list[str] = []
    dropped: list[DroppedMember] = []
    for tier in TIER_ORDER:
        rows = sorted(by_tier[tier], key=lambda m: m.rank)
        seen: set[str] = set()
        offered: list[TierMember] = []
        for m in rows:
            sym = _norm(m.ticker)
            if not sym or sym in seen:
                continue
            seen.add(sym)
            offered.append(m.model_copy(update={"ticker": sym, "tier": tier}))
        raw_counts[tier.value] = len(offered)
        size = sizes.get(tier)
        for idx, m in enumerate(offered):
            cut = size is not None and idx >= size
            if m.ticker in kept:  # a higher tier already holds it
                if not cut and tier not in kept[m.ticker].also_in:
                    kept[m.ticker].also_in.append(tier)
                continue
            if cut:
                dropped.append(
                    DroppedMember(
                        ticker=m.ticker, tier=tier, reason=DROP_OVER_TIER_SIZE, rank=m.rank
                    )
                )
                continue
            kept[m.ticker] = m.model_copy(update={"also_in": []})
            order.append(m.ticker)
    # a name cut by a higher tier's size but held by a lower tier is active, not dropped
    dropped = [d for d in dropped if d.ticker not in kept]
    head = [s for s in order if kept[s].tier in _HEAD_TIERS][:active_max]
    open_slots = active_max - len(head)
    shared = {t: [s for s in order if kept[s].tier is t] for t in SHARED_TIERS}
    if fill == "round_robin":
        rr = _round_robin(shared[Tier.DISCOVERY], shared[Tier.TRENDING], open_slots)
        chosen = set(head) | set(rr)
    else:  # precedence (D58): the deduped list in tier order, cut at the cap
        chosen = set(order[:active_max])
    members: list[TierMember] = []
    for sym in order:  # grouped by tier, rank order inside a tier (both fills)
        m = kept[sym]
        if sym in chosen:
            members.append(m)
        else:
            dropped.append(
                DroppedMember(ticker=sym, tier=m.tier, reason=DROP_OVER_ACTIVE_CAP, rank=m.rank)
            )
    counts = {t.value: sum(1 for m in members if m.tier is t) for t in TIER_ORDER}
    return ActiveUniverse(
        model="d56",
        as_of=as_of,
        members=members,
        dropped=dropped,
        counts=counts,
        raw_counts=raw_counts,
        expired_tiers=sorted(set(expired_tiers), key=_ALL_TIERS.index),
        config_version=config_version,
        fill=fill,
        open_slots=open_slots,
        slots={t.value: counts[t.value] for t in SHARED_TIERS},
    )


def _round_robin(first: Sequence[str], second: Sequence[str], n: int) -> list[str]:
    """D67: ``first[0], second[0], first[1], second[1], …`` up to *n* names; once one
    queue is empty the other keeps filling (spill), so no slot is left empty."""
    out: list[str] = []
    i = j = 0
    while len(out) < n and (i < len(first) or j < len(second)):
        if i < len(first) and (j >= len(second) or i <= j):
            out.append(first[i])
            i += 1
        else:
            out.append(second[j])
            j += 1
    return out


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


def _universe_cfg(settings: ArcSettings) -> Any:
    from arc.universe.config import universe_config

    return universe_config(settings)


def core_tickers(settings: ArcSettings) -> list[str]:
    """The effective core list.

    ``settings.universe`` (the ``universe`` registry key, D26 override included) when
    it holds at most :data:`MAX_CORE` names; a longer list is a pre-D51 flat seed
    override, ignored (logged ``universe.core_override_ignored``) in favour of
    ``config/universe.yaml`` ``core:``. ``config_changes`` rows are never rewritten.
    """
    names = list(dict.fromkeys(_norm(t) for t in settings.universe if _norm(t)))
    if len(names) <= MAX_CORE:
        return names
    core = yaml_core(settings)
    log.warning(
        "universe.core_override_ignored",
        override=len(names),
        ceiling=MAX_CORE,
        core=len(core),
        hint="run `!arc config universe <core>` (or reset it) so the Tower shows the core",
    )
    return core


def yaml_core(settings: ArcSettings) -> list[str]:
    """``config/universe.yaml`` ``core:`` (normalised, deduped): the core list in use
    whenever the ``universe`` override is longer than :data:`MAX_CORE`."""
    return list(dict.fromkeys(_norm(t) for t in _universe_cfg(settings).core))


def ignored_core_override(settings: ArcSettings) -> int | None:
    """The ``universe`` override's name count when :func:`core_tickers` ignores it
    (more than :data:`MAX_CORE` names), else ``None``. Pure: no log (the Tower reads it)."""
    n = len(dict.fromkeys(_norm(t) for t in settings.universe if _norm(t)))
    return n if n > MAX_CORE else None


def market_reference(settings: ArcSettings) -> list[str]:
    """Market reference symbols (SPY, QQQ, IWM): regime always written, never a trade
    name."""
    return [_norm(t) for t in _universe_cfg(settings).tiers.reference()]


def tier_sizes(settings: ArcSettings) -> dict[Tier, int]:
    """Per-tier size cuts (core is bounded by MAX_CORE, never cut here)."""
    return {
        Tier.MOMENTUM: settings.universe_momentum_size,
        Tier.DISCOVERY: settings.universe_discovery_size,
        Tier.TRENDING: settings.universe_trending_size,
    }


def tier_floor(settings: ArcSettings, tier: Tier | None) -> float | None:
    """Scalp confidence floor of *tier* (``universe_floor_<tier>``); ``None`` for a
    name in no tier (never admitted)."""
    if tier is Tier.CORE:
        return settings.universe_floor_core
    if tier is Tier.MOMENTUM:
        return settings.universe_floor_momentum
    if tier is Tier.DISCOVERY:
        return settings.universe_floor_discovery
    if tier is Tier.TRENDING:
        return settings.universe_floor_trending
    return None


class TierInputs(BaseModel):
    """What :func:`resolve_active` reads, as gathered from config + store."""

    model_config = _FORBID

    core: list[TierMember]
    momentum: list[TierMember] = Field(default_factory=list)
    discoveries: list[TierMember] = Field(default_factory=list)
    trending: list[TierMember] = Field(default_factory=list)
    expired_tiers: list[Tier] = Field(default_factory=list)


def _today(now: _dt.datetime) -> _dt.date:
    from arc.utils.calendar import ET

    return now.astimezone(ET).date()


def _core_members(settings: ArcSettings, day: _dt.date) -> list[TierMember]:
    source = "config" if len(settings.universe) > MAX_CORE else "settings"
    return [
        TierMember(ticker=t, tier=Tier.CORE, rank=i, source=source, reason="core list", as_of=day)
        for i, t in enumerate(core_tickers(settings), 1)
    ]


def _feed_tier(
    conn: sqlite3.Connection, tier: Tier, now: _dt.datetime
) -> tuple[list[TierMember], bool]:
    """``(members, expired)`` from the latest valid ``universe_tier`` entry for *tier*.

    No valid entry = empty. ``expired`` is True when an entry for the tier exists
    but none is valid at *now* (journaled by the caller via ``expired_tiers``).
    """
    from arc.context.store import ContextStore

    try:
        valid = ContextStore(conn).query(as_of=now, kinds=["universe_tier"], subjects=[tier.value])
    except sqlite3.OperationalError:  # store not migrated: no tiers
        return [], False
    if valid:
        payload = UniverseTierPayload.model_validate(valid[-1].payload)
        return [m.model_copy(update={"tier": tier}) for m in payload.members], False
    try:
        row = conn.execute(
            "SELECT 1 FROM context_entries WHERE kind = 'universe_tier' AND subject = ? LIMIT 1",
            (tier.value,),
        ).fetchone()
    except sqlite3.OperationalError:
        row = None
    if row is not None:
        log.warning("universe.tier_expired", tier=tier.value, as_of=now.isoformat())
    return [], row is not None


def load_tier_inputs(
    conn: sqlite3.Connection, settings: ArcSettings, now: _dt.datetime
) -> TierInputs:
    """Core (config/settings), momentum, discovery and trending (context; discovery is
    written by the Scout, trending by ``universe.trending``)."""
    core = _core_members(settings, _today(now))
    momentum, m_exp = _feed_tier(conn, Tier.MOMENTUM, now)
    discovery, d_exp = _feed_tier(conn, Tier.DISCOVERY, now)
    trending, t_exp = _feed_tier(conn, Tier.TRENDING, now)
    expired = ((Tier.MOMENTUM, m_exp), (Tier.DISCOVERY, d_exp), (Tier.TRENDING, t_exp))
    return TierInputs(
        core=core,
        momentum=momentum,
        discoveries=discovery,
        trending=trending,
        expired_tiers=[t for t, e in expired if e],
    )


def build_active(
    conn: sqlite3.Connection, settings: ArcSettings, now: _dt.datetime
) -> tuple[ActiveUniverse, TierInputs]:
    """Gather the inputs and resolve today's active list (no write)."""
    inputs = load_tier_inputs(conn, settings, now)
    fill: ActiveFill = _universe_cfg(settings).active_fill
    active = resolve_active(
        core=inputs.core,
        momentum=inputs.momentum,
        discoveries=inputs.discoveries,
        trending=inputs.trending,
        active_max=settings.universe_active_max,
        tier_sizes=tier_sizes(settings),
        as_of=_today(now),
        config_version=settings.config_version,
        expired_tiers=inputs.expired_tiers,
        fill=fill,
    )
    return active, inputs


def record_active(
    conn: sqlite3.Connection,
    active: ActiveUniverse,
    *,
    at: _dt.datetime,
    write: Any,
    run_id: str | None = None,
    chain_run_id: str | None = None,
) -> int:
    """Write *active* via *write* (``JobContext.write``-shaped: ``(kind, subject,
    payload)``) and journal each name cut past the cap as ``universe:over_active_cap``.

    Idempotent per day and ticker: a name already journaled today is not journaled
    again by a later resolve (the Scalp resolves every 30 min). Returns the number of
    new journal rows.
    """
    from arc.context.ttl import to_db
    from arc.journal.reasons import Choice, JournalPersona, ReasonCode, Stage
    from arc.journal.store import JournalStore
    from arc.utils.calendar import ET

    write("active_universe", ACTIVE_SUBJECT, active)
    over = [d for d in active.dropped if d.reason == DROP_OVER_ACTIVE_CAP]
    ranks = {d.ticker: d.rank for d in over}
    day = active.as_of
    start = _dt.datetime(day.year, day.month, day.day, tzinfo=ET)
    done = (
        {
            r[0]
            for r in conn.execute(
                "SELECT subject FROM decisions WHERE reason_code = ? AND at >= ? AND at < ?",
                (
                    ReasonCode.UNIVERSE_OVER_ACTIVE_CAP.value,
                    to_db(start),
                    to_db(start + _dt.timedelta(days=1)),
                ),
            ).fetchall()
        }
        if over
        else set()
    )
    store = JournalStore(conn)
    n = 0
    with conn:
        for d in over:
            if d.ticker in done:
                continue
            store.record(
                persona=JournalPersona.SYSTEM,
                stage=Stage.CANDIDATE,
                subject=d.ticker,
                choice=Choice.REJECTED,
                reason_code=ReasonCode.UNIVERSE_OVER_ACTIVE_CAP,
                reason_text=(
                    f"{d.tier.value} name past the active-list cap of {len(active.members)}"
                ),
                at=at,
                run_id=run_id,
                chain_run_id=chain_run_id,
                payload={
                    "tier": d.tier.value,
                    "as_of": day.isoformat(),
                    **({"rank": ranks[d.ticker]} if ranks.get(d.ticker) is not None else {}),
                    "model": active.model,
                    # D67 (E14.9): how the shared slots were filled when this name was cut
                    **({"fill": active.fill} if active.fill is not None else {}),
                    **({"slots": dict(active.slots)} if active.slots else {}),
                },
            )
            n += 1
    for tier in active.expired_tiers:
        log.info("universe.tier_expired_empty", tier=tier.value, as_of=day.isoformat())
    log.info(
        "universe.active_resolved",
        as_of=day.isoformat(),
        active=len(active.members),
        counts=active.counts,
        fill=active.fill,
        open_slots=active.open_slots,
        slots=active.slots,
        over_cap=len(over),
        journaled=n,
    )
    return n


# ---------------------------------------------------------------------------
# Consumers
# ---------------------------------------------------------------------------


def _stored_active(conn: sqlite3.Connection, now: _dt.datetime) -> ActiveUniverse | None:
    """Today's latest valid ``active_universe`` entry, if any."""
    from arc.context.store import ContextStore

    try:
        rows = ContextStore(conn).query(
            as_of=now, kinds=["active_universe"], subjects=[ACTIVE_SUBJECT]
        )
    except sqlite3.OperationalError:
        return None
    if not rows:
        return None
    active = ActiveUniverse.model_validate(rows[-1].payload)
    return active if active.as_of == _today(now) else None


def active_tickers(
    conn: sqlite3.Connection | None, settings: ArcSettings, now: _dt.datetime
) -> list[str]:
    """Today's active list (stored resolve), else the core list.

    Every former ``settings.universe`` consumer reads this (D51).
    """
    if conn is not None and (active := _stored_active(conn, now)) is not None:
        return active.tickers
    return core_tickers(settings)


def watch_tickers(
    conn: sqlite3.Connection | None, settings: ArcSettings, now: _dt.datetime
) -> list[str]:
    """The Scalp's watch list: every tier of today's active list (discovery comes
    from the Scout, trending from the daily retail_buzz ranking)."""
    if conn is not None and (active := _stored_active(conn, now)) is not None:
        return active.tickers
    return core_tickers(settings)


#: Tiers admitted without a liquidity screen (D56: core only; momentum takes the
#: standard screen).
SEED_TIERS: frozenset[Tier] = frozenset({Tier.CORE})


def seed_tickers(
    conn: sqlite3.Connection | None, settings: ArcSettings, now: _dt.datetime
) -> list[str]:
    """Names the Scalp admits without the screen: core."""
    return [t for t, tier in tier_membership(conn, settings, now).items() if tier in SEED_TIERS]


def tier_membership(
    conn: sqlite3.Connection | None, settings: ArcSettings, now: _dt.datetime
) -> dict[str, Tier]:
    """``ticker -> highest tier`` (admission, E13.4).

    Core, momentum, the Scout's discovery feed and the trending feed (D58), each feed
    cut to its tier size by rank (momentum rows 21-25 are in no tier). Not cut by the
    active cap: a tier member keeps its admission rule even when the active list
    overflows. A name not listed here is in no tier and is never admitted (``not_in_tier``).
    """
    out: dict[str, Tier] = {t: Tier.CORE for t in core_tickers(settings)}
    if conn is None:
        return out
    sizes = tier_sizes(settings)
    for tier in (Tier.MOMENTUM, Tier.DISCOVERY, Tier.TRENDING):
        members, _ = _feed_tier(conn, tier, now)
        seen: list[str] = []
        for m in sorted(members, key=lambda r: r.rank):
            sym = _norm(m.ticker)
            if sym and sym not in seen:
                seen.append(sym)
        for sym in seen[: sizes[tier]]:
            out.setdefault(sym, tier)
    return out


def active_by_tier(
    conn: sqlite3.Connection | None, settings: ArcSettings, now: _dt.datetime
) -> dict[Tier, list[str]]:
    """Today's stored active list split by tier (every tier listed); the core list
    under ``core`` before the first resolve of the day or without a store."""
    out: dict[Tier, list[str]] = {t: [] for t in TIER_ORDER}
    if conn is not None and (active := _stored_active(conn, now)) is not None:
        for m in active.members:
            out.setdefault(m.tier, []).append(m.ticker)
        return out
    out[Tier.CORE] = core_tickers(settings)
    return out


def open_underlyings(conn: sqlite3.Connection | None) -> list[str]:
    """Underlyings of open structures, sorted (empty without a store or before migrate)."""
    if conn is None:
        return []
    try:
        rows = conn.execute(
            "SELECT DISTINCT ticker FROM open_structures WHERE status = 'open' ORDER BY ticker"
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [_norm(str(r[0])) for r in rows if _norm(str(r[0]))]


def active_tickers_ro(settings: ArcSettings, now: _dt.datetime) -> list[str]:
    """:func:`active_tickers` against ``settings.db_path`` opened read-only (CLI use)."""
    from arc.store.db import DEFAULT_DB_PATH

    p = Path(settings.db_path or DEFAULT_DB_PATH)
    if str(p) == ":memory:" or not p.is_file():
        return core_tickers(settings)
    conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return active_tickers(conn, settings, now)
    finally:
        conn.close()
