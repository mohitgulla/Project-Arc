"""Universe page (E12.6, D51): what can Arc trade today, and why is each name there?

Read-only. The page shows the **stored** resolve (the latest ``active_universe`` context
entry, written by the Scalp every 30 min and by the tier jobs) and the latest
``universe_tier`` entry per feed tier. It never re-resolves: the dedupe and cap logic
lives in :func:`arc.universe.tiers.resolve_active` only.

When today has no resolve, the latest one is shown with its age and ``state`` says
``stale``; with none at all the core list is shown (``state = none``), which is what every
consumer reads before the first resolve of the day (:func:`~arc.universe.tiers.active_tickers`).
"""

from __future__ import annotations

import datetime as _dt  # noqa: TC003 - pydantic resolves the field types at runtime
import json
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from arc.tower.data import _has_table, parse_ts
from arc.universe.tiers import (
    ACTIVE_SUBJECT,
    MAX_CORE,
    TIER_ORDER,
    ActiveUniverse,
    Tier,
    TierMember,
    UniverseTierPayload,
    core_tickers,
    ignored_core_override,
    market_reference,
)
from arc.utils.calendar import ET

if TYPE_CHECKING:
    import sqlite3

    from arc.config import ArcSettings

__all__ = [
    "CoreOverrideIgnored",
    "UniverseActiveRow",
    "UniverseDroppedRow",
    "UniverseResponse",
    "UniverseTierRow",
    "load_universe",
]

_STRICT = ConfigDict(extra="forbid", frozen=True)

ResolveState = Literal["today", "stale", "none"]


class UniverseActiveRow(BaseModel):
    model_config = _STRICT

    ticker: str
    tier: str
    rank: int
    source: str
    reason: str
    also_in: list[str] = Field(default_factory=list, description="Lower tiers that also list it")


class UniverseDroppedRow(BaseModel):
    model_config = _STRICT

    ticker: str
    tier: str
    reason: str = Field(description="over_active_cap | over_tier_size | …")


class UniverseTierRow(BaseModel):
    model_config = _STRICT

    name: str = Field(description="core | momentum | trending | discovery (precedence order)")
    offered: int = Field(description="Names the tier offered before dedupe and caps (raw_count)")
    active: int = Field(description="Names this tier holds in the active list")
    size_cap: int | None = Field(
        description="The tier's size: core ceiling 30, momentum/trending sizes; null = no cut"
    )
    source: str | None = Field(description="Feed source (stockanalysis, reddit+…, settings, scalp)")
    url: str | None = None
    fetched_at: _dt.datetime | None = Field(description="Latest universe_tier entry's fetch time")
    age_s: int | None = None
    source_as_of: _dt.date | None = None
    partial: bool = False
    expired: bool = Field(description="The latest feed expired: read as empty by the resolve")


class CoreOverrideIgnored(BaseModel):
    model_config = _STRICT

    count: int = Field(description="Names in the `universe` override")
    note: str
    core_in_use: list[str] = Field(description="The config/universe.yaml core list in use")


class UniverseResponse(BaseModel):
    model_config = _STRICT

    as_of: _dt.datetime = Field(description="Server time of this read")
    state: ResolveState = Field(
        description="today = resolved today; stale = latest resolve is older (consumers use the "
        "core list until the next one); none = never resolved (core list shown)"
    )
    resolved_for: _dt.date | None = Field(description="The stored resolve's as_of day")
    resolved_at: _dt.datetime | None = Field(description="When the stored resolve was written")
    resolved_by: str | None = None
    age_s: int | None = None
    note: str | None = None
    config_version: int | None = Field(description="Config version the resolve ran under")
    active_max: int = Field(description="universe_active_max (effective)")
    active: list[UniverseActiveRow]
    tiers: list[UniverseTierRow]
    dropped: list[UniverseDroppedRow]
    market_reference: list[str]
    core_override_ignored: CoreOverrideIgnored | None = None
    director_diversification: str | None = Field(
        None,
        title="Research diversification",  # D56: the key keeps its pre-rename name
        description="personas.director_diversification (E12.5): strict | relaxed",
    )


def _latest(conn: sqlite3.Connection, kind: str, subject: str) -> sqlite3.Row | None:
    """The newest entry of *kind*/*subject* in any status (superseded/expired included)."""
    return conn.execute(
        "SELECT payload, valid_from, created_at, expires_at, status, produced_by"
        " FROM context_entries WHERE kind = ? AND subject = ?"
        " ORDER BY valid_from DESC, created_at DESC, rowid DESC LIMIT 1",
        (kind, subject),
    ).fetchone()


def _age(at: _dt.datetime | None, now: _dt.datetime) -> int | None:
    return None if at is None else max(0, int((now - at).total_seconds()))


def _stored_active(
    conn: sqlite3.Connection,
) -> tuple[ActiveUniverse, sqlite3.Row] | None:
    row = _latest(conn, "active_universe", ACTIVE_SUBJECT)
    if row is None:
        return None
    try:
        return ActiveUniverse.model_validate(json.loads(row["payload"])), row
    except (ValueError, ValidationError):  # a payload this build can't read: show none
        return None


def _feed(conn: sqlite3.Connection, tier: Tier) -> UniverseTierPayload | None:
    row = _latest(conn, "universe_tier", tier.value)
    if row is None:
        return None
    try:
        return UniverseTierPayload.model_validate(json.loads(row["payload"]))
    except (ValueError, ValidationError):
        return None


def _active_row(m: TierMember) -> UniverseActiveRow:
    return UniverseActiveRow(
        ticker=m.ticker,
        tier=m.tier.value,
        rank=m.rank,
        source=m.source,
        reason=m.reason,
        also_in=[t.value for t in m.also_in],
    )


def _core_fallback(settings: ArcSettings, day: _dt.date) -> ActiveUniverse:
    """No resolve stored: the core list, as every consumer reads it (no write, no resolve)."""
    source = "config" if ignored_core_override(settings) else "settings"
    names = core_tickers(settings)
    members = [
        TierMember(ticker=t, tier=Tier.CORE, rank=i, source=source, reason="core list", as_of=day)
        for i, t in enumerate(names, 1)
    ]
    counts = {t.value: 0 for t in TIER_ORDER} | {Tier.CORE.value: len(members)}
    return ActiveUniverse(as_of=day, members=members, counts=counts, raw_counts=dict(counts))


def load_universe(
    conn: sqlite3.Connection,
    settings: ArcSettings,
    *,
    now: _dt.datetime,
    director_diversification: str | None = None,
) -> UniverseResponse:
    """The stored resolve + tier feeds + the effective core/market-reference settings."""
    now = now.astimezone(ET)
    today = now.date()
    has_ctx = _has_table(conn, "context_entries")
    stored = _stored_active(conn) if has_ctx else None
    resolved_at: _dt.datetime | None = None
    resolved_by: str | None = None
    if stored is not None:
        active, row = stored
        resolved_at = parse_ts(row["valid_from"])
        resolved_by = row["produced_by"]
        expires = parse_ts(row["expires_at"])
        valid = expires is None or expires > now
        state: ResolveState = "today" if active.as_of == today and valid else "stale"
    else:
        active = _core_fallback(settings, today)
        state = "none"
    note = {
        "today": None,
        "stale": (
            f"Not resolved today: latest resolve is for {active.as_of.isoformat()}"
            f"{'' if active.as_of != today else ' and has expired'}. Consumers use the core "
            "list until the next resolve (Scalp, every 30 min)."
        ),
        "none": "No resolve stored yet: consumers use the core list (shown here).",
    }[state]

    sizes: dict[Tier, int | None] = {
        Tier.CORE: MAX_CORE,
        Tier.MOMENTUM: settings.universe_momentum_size,
        Tier.TRENDING: settings.universe_trending_size,
        Tier.DISCOVERY: None,
    }
    core_source = next((m.source for m in active.members if m.tier is Tier.CORE), "settings")
    fixed_source: dict[Tier, str | None] = {
        Tier.CORE: core_source,
        Tier.MOMENTUM: None,
        Tier.TRENDING: None,
        Tier.DISCOVERY: "scalp",
    }
    tiers: list[UniverseTierRow] = []
    for tier in TIER_ORDER:
        feed = _feed(conn, tier) if has_ctx and tier in (Tier.MOMENTUM, Tier.TRENDING) else None
        fetched = feed.fetched_at.astimezone(ET) if feed is not None else None
        source = feed.source if feed is not None else fixed_source[tier]
        tiers.append(
            UniverseTierRow(
                name=tier.value,
                offered=int(active.raw_counts.get(tier.value, 0)),
                active=int(active.counts.get(tier.value, 0)),
                size_cap=sizes[tier],
                source=source,
                url=(feed.url or None) if feed is not None else None,
                fetched_at=fetched,
                age_s=_age(fetched, now),
                source_as_of=feed.source_as_of if feed is not None else None,
                partial=feed.partial if feed is not None else False,
                expired=tier in active.expired_tiers,
            )
        )

    ignored = ignored_core_override(settings)
    override = None
    if ignored is not None:
        core = core_tickers(settings)
        override = CoreOverrideIgnored(
            count=ignored,
            note=(
                f"The universe override has {ignored} names (> {MAX_CORE}): it is a pre-D51 "
                f"flat list and is ignored. The core list in use is config/universe.yaml core "
                f"({len(core)} names)."
            ),
            core_in_use=core,
        )

    return UniverseResponse(
        as_of=now,
        state=state,
        resolved_for=active.as_of if state != "none" else None,
        resolved_at=resolved_at,
        resolved_by=resolved_by,
        age_s=_age(resolved_at, now),
        note=note,
        config_version=active.config_version if state != "none" else settings.config_version,
        active_max=settings.universe_active_max,
        active=[_active_row(m) for m in active.members],
        tiers=tiers,
        dropped=[
            UniverseDroppedRow(ticker=d.ticker, tier=d.tier.value, reason=d.reason)
            for d in active.dropped
        ],
        market_reference=market_reference(settings),
        core_override_ignored=override,
        director_diversification=director_diversification,
    )
