"""Tiered universe (D51 card E12.1; D56 card E13.4) → one active list.

``config/universe.yaml`` ``tiers.model`` picks the layout. ``d51`` (the control) is
four tiers, ``d56`` three (no trending; discovery comes from the Scout):

* ``core`` — the fixed list (``config/universe.yaml`` ``core:``, or the ``universe``
  D26 override when it has at most :data:`MAX_CORE` names).
* ``momentum`` — top holdings of the S&P 500 Momentum index (E12.2), read from the
  latest valid ``universe_tier`` context entry with subject ``momentum``.
* ``trending`` — daily rules-based list (E12.3), ``universe_tier`` subject ``trending``.
* ``discovery`` — D51: today's Scalp candidates that are in no other tier
  (``candidates`` rows; every row was admitted by the Scalp's universe guard). D56:
  the Scout's ``universe_tier`` entry with subject ``discovery`` (E13.7), the single
  entry point; empty until the Scout writes it.

:func:`resolve_active` is pure and deterministic: a name keeps its **highest** tier
(core > momentum > trending > discovery) and records the others in ``also_in``;
each tier is cut to its size; the deduped list is cut to ``active_max`` in tier
order, then rank, so overflow leaves from the tail of the lowest tier first (D56:
discovery, then momentum). Every cut name is listed in ``dropped`` (never silently lost).

``market_reference`` (D51: SPY, QQQ; D56: SPY, QQQ, IWM) is not part of the trade
list; Research always writes their ``regime`` entries so the D33 market guard keeps
its SPY read.

This module is imported by :mod:`arc.context.kinds` (the two payload models), so at
module level it imports pydantic and :mod:`arc.config` only. Store reads import
:mod:`arc.context.store` lazily.
"""

from __future__ import annotations

import datetime as _dt  # noqa: TC003 - pydantic resolves the field types at runtime
import enum
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import structlog
from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from arc.config import ArcSettings

log = structlog.get_logger(__name__)

__all__ = [
    "ACTIVE_SUBJECT",
    "DROP_OVER_ACTIVE_CAP",
    "DROP_OVER_TIER_SIZE",
    "MAX_CORE",
    "SEED_TIERS",
    "TIER_ORDER",
    "TIER_ORDER_D56",
    "ActiveUniverse",
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
    "tier_order",
    "tiers_model",
    "watch_tickers",
    "yaml_core",
]

MAX_CORE = 30  # hard ceiling on the core list (registry `universe` max_items)
ACTIVE_SUBJECT = "active"  # `active_universe` context subject
DROP_OVER_ACTIVE_CAP = "over_active_cap"  # journaled as universe:over_active_cap
DROP_OVER_TIER_SIZE = "over_tier_size"  # the tier's own feed listed more than its size

_FORBID = ConfigDict(extra="forbid")


def _norm(raw: str) -> str:
    """Same rule as :func:`arc.universe.master.normalize_symbol` (not imported: that
    module reaches the broker SDK lazily, and :mod:`arc.context.kinds` imports this one)."""
    return raw.strip().lstrip("$").strip().upper().replace("-", ".").replace("/", ".")


class Tier(enum.StrEnum):
    """Universe tiers, highest precedence first."""

    CORE = "core"
    MOMENTUM = "momentum"
    TRENDING = "trending"
    DISCOVERY = "discovery"


TIER_ORDER: tuple[Tier, ...] = tuple(Tier)  # D51 (and every tier, for history)
#: D56: no trending tier (the member stays for stored history).
TIER_ORDER_D56: tuple[Tier, ...] = (Tier.CORE, Tier.MOMENTUM, Tier.DISCOVERY)

Model = Literal["d51", "d56"]


def tier_order(model: str) -> tuple[Tier, ...]:
    """The precedence order of *model*'s tiers."""
    return TIER_ORDER_D56 if model == "d56" else TIER_ORDER


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


class UniverseTierPayload(BaseModel):
    """``universe_tier`` context entry (subject = tier name), written by E12.2 / E12.3."""

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
    trending: Sequence[TierMember] = (),
    discoveries: Sequence[TierMember] = (),
    active_max: int,
    tier_sizes: dict[Tier, int] | None = None,
    as_of: _dt.date,
    config_version: int | None = None,
    expired_tiers: Iterable[Tier] = (),
    model: Model = "d51",
) -> ActiveUniverse:
    """Dedupe the tiers (highest wins), cut each to its size, cap at *active_max*.

    Each input is taken in its given order after a stable sort by ``rank``. A
    name repeated inside one tier keeps its first row. A name cut by a tier's size
    may still be held by a lower tier that lists it (then it is not in ``dropped``).
    *tier_sizes* missing a tier means no per-tier cut. Deterministic: same inputs, same output.

    The cap keeps names in tier order then rank, so the overflow is the tail of the
    lowest tier first (D56: discovery's lowest-ranked rows, then momentum's). Under
    *model* ``d56`` there is no trending tier: *trending* must be empty.
    """
    order_tiers = tier_order(model)
    if model == "d56" and trending:
        msg = "model d56 has no trending tier"
        raise ValueError(msg)
    sizes = tier_sizes or {}
    by_tier = {
        Tier.CORE: core,
        Tier.MOMENTUM: momentum,
        Tier.TRENDING: trending,
        Tier.DISCOVERY: discoveries,
    }
    raw_counts: dict[str, int] = {}
    kept: dict[str, TierMember] = {}
    order: list[str] = []
    dropped: list[DroppedMember] = []
    for tier in order_tiers:
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
        taken = 0
        for idx, m in enumerate(offered):
            # D56: the tier is the feed's top `size` rows by rank, cut before dedupe
            # (momentum 25 -> top 20, of which names already in core count as also_in).
            # D51: dedupe first; the size counts only names this tier holds.
            cut = size is not None and (idx if model == "d56" else taken) >= size
            if m.ticker in kept:  # a higher tier already holds it
                if not (cut and model == "d56") and tier not in kept[m.ticker].also_in:
                    kept[m.ticker].also_in.append(tier)
                continue
            if cut:
                dropped.append(
                    DroppedMember(
                        ticker=m.ticker,
                        tier=tier,
                        reason=DROP_OVER_TIER_SIZE,
                        rank=m.rank if model == "d56" else None,
                    )
                )
                continue
            taken += 1
            kept[m.ticker] = m.model_copy(update={"also_in": []})
            order.append(m.ticker)
    # a name cut by a higher tier's size but held by a lower tier is active, not dropped
    dropped = [d for d in dropped if d.ticker not in kept]
    members: list[TierMember] = []
    for sym in order:
        m = kept[sym]
        if len(members) < active_max:
            members.append(m)
        else:
            dropped.append(
                DroppedMember(
                    ticker=sym,
                    tier=m.tier,
                    reason=DROP_OVER_ACTIVE_CAP,
                    rank=m.rank if model == "d56" else None,
                )
            )
    counts = {t.value: sum(1 for m in members if m.tier is t) for t in order_tiers}
    return ActiveUniverse(
        model=model,
        as_of=as_of,
        members=members,
        dropped=dropped,
        counts=counts,
        raw_counts=raw_counts,
        expired_tiers=sorted(set(expired_tiers), key=TIER_ORDER.index),
        config_version=config_version,
    )


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


def market_reference(settings: ArcSettings, *, model: str | None = None) -> list[str]:
    """Market reference symbols (D51: SPY, QQQ; D56: + IWM): regime always written,
    never a trade name. *model* previews another layout's default list."""
    tiers = _universe_cfg(settings).tiers
    if model is not None and model != tiers.model:
        tiers = tiers.model_copy(update={"model": model})
    return [_norm(t) for t in tiers.reference()]


def tiers_model(settings: ArcSettings) -> Model:
    """``config/universe.yaml`` ``tiers.model`` (D26 override included): d51 | d56."""
    model: Model = _universe_cfg(settings).tiers.model
    return model


def tier_sizes(settings: ArcSettings, model: str) -> dict[Tier, int]:
    """Per-tier size cuts of *model* (core is bounded by MAX_CORE, never cut here)."""
    if model == "d56":
        return {
            Tier.MOMENTUM: settings.universe_momentum_size_d56,
            Tier.DISCOVERY: settings.universe_discovery_size,
        }
    return {
        Tier.MOMENTUM: settings.universe_momentum_size,
        Tier.TRENDING: settings.universe_trending_size,
    }


def tier_floor(settings: ArcSettings, tier: Tier | None) -> float | None:
    """D56 Scalp confidence floor of *tier* (``universe_floor_<tier>``); ``None`` for a
    name in no tier (never admitted) or a tier without a D56 floor (trending)."""
    if tier is Tier.CORE:
        return settings.universe_floor_core
    if tier is Tier.MOMENTUM:
        return settings.universe_floor_momentum
    if tier is Tier.DISCOVERY:
        return settings.universe_floor_discovery
    return None


class TierInputs(BaseModel):
    """What :func:`resolve_active` reads, as gathered from config + store."""

    model_config = _FORBID

    core: list[TierMember]
    momentum: list[TierMember] = Field(default_factory=list)
    trending: list[TierMember] = Field(default_factory=list)
    discoveries: list[TierMember] = Field(default_factory=list)
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


def _discoveries(conn: sqlite3.Connection, day: _dt.date, exclude: set[str]) -> list[TierMember]:
    """Today's candidates outside the other tiers: confidence, then corroboration."""
    try:
        rows = conn.execute(
            "SELECT ticker, confidence, corroboration FROM candidates WHERE day = ?",
            (day.isoformat(),),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    best: dict[str, tuple[float, int]] = {}
    for r in rows:
        sym = _norm(str(r[0]))
        if not sym or sym in exclude:
            continue
        score = (float(r[1] or 0.0), int(r[2] or 0))
        if sym not in best or score > best[sym]:
            best[sym] = score
    ordered = sorted(best.items(), key=lambda kv: (-kv[1][0], -kv[1][1], kv[0]))
    return [
        TierMember(
            ticker=sym,
            tier=Tier.DISCOVERY,
            rank=i,
            source="scalp",
            reason=f"candidate confidence {conf:.2f}, corroboration {corr}",
            as_of=day,
        )
        for i, (sym, (conf, corr)) in enumerate(ordered, 1)
    ]


def load_tier_inputs(
    conn: sqlite3.Connection,
    settings: ArcSettings,
    now: _dt.datetime,
    *,
    model: str | None = None,
) -> TierInputs:
    """D51: core (config/settings), momentum + trending (context), discoveries
    (candidates). D56: core, momentum + discovery (context, discovery written by the
    Scout); trending is not read."""
    day = _today(now)
    core = _core_members(settings, day)
    if (model or tiers_model(settings)) == "d56":
        momentum, m_exp = _feed_tier(conn, Tier.MOMENTUM, now)
        discovery, d_exp = _feed_tier(conn, Tier.DISCOVERY, now)
        return TierInputs(
            core=core,
            momentum=momentum,
            discoveries=discovery,
            expired_tiers=[t for t, e in ((Tier.MOMENTUM, m_exp), (Tier.DISCOVERY, d_exp)) if e],
        )
    momentum, m_exp = _feed_tier(conn, Tier.MOMENTUM, now)
    trending, t_exp = _feed_tier(conn, Tier.TRENDING, now)
    tiered = {_norm(m.ticker) for m in (*core, *momentum, *trending)}
    return TierInputs(
        core=core,
        momentum=momentum,
        trending=trending,
        discoveries=_discoveries(conn, day, tiered),
        expired_tiers=[t for t, e in ((Tier.MOMENTUM, m_exp), (Tier.TRENDING, t_exp)) if e],
    )


def build_active(
    conn: sqlite3.Connection,
    settings: ArcSettings,
    now: _dt.datetime,
    *,
    model: Model | None = None,
) -> tuple[ActiveUniverse, TierInputs]:
    """Gather the inputs and resolve today's active list (no write).

    *model* overrides ``tiers.model`` (``arc universe tiers --model``, read-only preview).
    """
    m: Model = model or tiers_model(settings)
    inputs = load_tier_inputs(conn, settings, now, model=m)
    active = resolve_active(
        core=inputs.core,
        momentum=inputs.momentum,
        trending=inputs.trending,
        discoveries=inputs.discoveries,
        active_max=settings.universe_active_max,
        tier_sizes=tier_sizes(settings, m),
        as_of=_today(now),
        config_version=settings.config_version,
        expired_tiers=inputs.expired_tiers,
        model=m,
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
                    **({"model": active.model} if active.model != "d51" else {}),
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
    """The Scalp's watch list: active core + momentum + trending (D51: no discoveries,
    the Scalp makes them). D56: every active tier (discovery comes from the Scout)."""
    if conn is not None and (active := _stored_active(conn, now)) is not None:
        if active.model == "d56":
            return active.tickers
        return active.tier_tickers(Tier.CORE, Tier.MOMENTUM, Tier.TRENDING)
    return core_tickers(settings)


def seed_tickers(
    conn: sqlite3.Connection | None, settings: ArcSettings, now: _dt.datetime
) -> list[str]:
    """Names the Scalp admits without the screen: core ∪ valid momentum members (D51);
    D56: core only (momentum takes the standard screen)."""
    seed = unscreened_tiers(tiers_model(settings))
    return [t for t, tier in tier_membership(conn, settings, now).items() if tier in seed]


#: D51 / E12.4: tiers admitted without the liquidity screen and the Scalp confidence floor.
SEED_TIERS: frozenset[Tier] = frozenset({Tier.CORE, Tier.MOMENTUM})


def unscreened_tiers(model: str) -> frozenset[Tier]:
    """Tiers admitted without a liquidity screen: D51 core + momentum, D56 core."""
    return frozenset({Tier.CORE}) if model == "d56" else SEED_TIERS


def tier_membership(
    conn: sqlite3.Connection | None, settings: ArcSettings, now: _dt.datetime
) -> dict[str, Tier]:
    """``ticker -> highest tier`` (admission, E12.4 / E13.4).

    D51: core, the valid momentum feed and the valid trending feed, read whole (not
    cut by a size or the active cap): a tier member keeps its admission rule even when
    the active list overflows. Discoveries are not listed (every other name is one).

    D56: core, momentum and the Scout's discovery feed, each feed cut to its tier
    size by rank (momentum rows 21-25 are in no tier). A name not listed here is in no
    tier and is never admitted (``not_in_tier``).
    """
    out: dict[str, Tier] = {t: Tier.CORE for t in core_tickers(settings)}
    if conn is None:
        return out
    model = tiers_model(settings)
    if model == "d56":
        sizes = tier_sizes(settings, model)
        for tier in (Tier.MOMENTUM, Tier.DISCOVERY):
            members, _ = _feed_tier(conn, tier, now)
            seen: list[str] = []
            for m in sorted(members, key=lambda r: r.rank):
                sym = _norm(m.ticker)
                if sym and sym not in seen:
                    seen.append(sym)
            for sym in seen[: sizes[tier]]:
                out.setdefault(sym, tier)
        return out
    for tier in (Tier.MOMENTUM, Tier.TRENDING):
        members, _ = _feed_tier(conn, tier, now)
        for m in members:
            out.setdefault(_norm(m.ticker), tier)
    return out


def active_by_tier(
    conn: sqlite3.Connection | None, settings: ArcSettings, now: _dt.datetime
) -> dict[Tier, list[str]]:
    """Today's stored active list split by tier (every tier listed); the core list
    under ``core`` before the first resolve of the day or without a store."""
    out: dict[Tier, list[str]] = {t: [] for t in TIER_ORDER}
    if conn is not None and (active := _stored_active(conn, now)) is not None:
        for m in active.members:
            out[m.tier].append(m.ticker)
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
