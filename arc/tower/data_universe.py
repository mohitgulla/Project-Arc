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
import re
from typing import TYPE_CHECKING, Literal, NamedTuple

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from arc.tower.data import _has_table, parse_ts
from arc.universe.tiers import (
    ACTIVE_SUBJECT,
    DROP_OVER_ACTIVE_CAP,
    MAX_CORE,
    TIER_ORDER,
    ActiveUniverse,
    Tier,
    TierMember,
    UniverseTierPayload,
    core_tickers,
    ignored_core_override,
    market_reference,
    tier_sizes,
)
from arc.universe.velocity import VelocityOptions
from arc.utils.calendar import ET

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Mapping

    from arc.config import ArcSettings

__all__ = [
    "CoreOverrideIgnored",
    "UniverseActiveRow",
    "UniverseDroppedRow",
    "UniverseResponse",
    "UniverseTierRow",
    "discovery_fill_by_day",
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
    inputs: int | None = Field(
        None, description="D58 trending: retail_buzz inputs that listed the name (2 | 1)"
    )
    velocity: float | None = Field(
        None,
        description="E14.5 trending: Reddit mention velocity (m + k) / (m24 + k), code-computed "
        "from the retail_buzz entry the tier was ranked from (null = none / not on Reddit)",
    )
    velocity_detail: str | None = Field(
        None, description="E14.5 trending: `reddit #20 · 5.2× (157 vs 30)`"
    )
    sentiment: str | None = Field(
        None,
        description="E14.6: Stocktwits `ST 80% bull (10 tagged, 2.7h)` from the newest "
        "unexpired retail_sentiment entry (null = none); context only",
    )
    # D64 (E14.7): discovery / trending two-run carry-over, copied from the stored member
    score: float | None = Field(
        None, description="D64: combined score used for ranking (0.6 x today + 0.4 x prev)"
    )
    score_today: float | None = Field(
        None, description="D64: this run's own score (null = carried from the previous run)"
    )
    score_prev: float | None = Field(
        None, description="D64: the previous run's own score (null = not in the previous run)"
    )
    runs: list[str] | None = Field(
        None, description="D64: run dates (ET, ISO) that listed the name (1 or 2)"
    )
    stance: str | None = Field(None, description="D64 discovery: the Scout's stance")
    origins: list[str] | None = Field(
        None, description="D64 discovery: the Scout's origins (youtube:<slug>)"
    )
    # E14.8 (D64): structured Today's Pick / Ops › Universe columns (the UI never parses
    # `sentiment`; the string stays for the popover)
    origin_labels: list[str] | None = Field(
        None,
        description="E14.8 discovery: origins as channel labels (youtube.briefs `label`; an "
        "unknown slug reads as itself)",
    )
    carried: bool = Field(
        False, description="E14.8: carried from the previous run only (score_today null)"
    )
    weight_pct: float | None = Field(
        None,
        description="E14.8 momentum: SPMO weight %, stored by the writer (v5) or parsed from "
        "a pre-v5 `SPMO weight 9.48%` reason",
    )
    sentiment_bull_pct: float | None = Field(
        None,
        description="E14.8: Stocktwits bullish share of tagged messages, percent (null = no "
        "entry or too few tags)",
    )
    sentiment_tagged: int | None = Field(
        None, description="E14.8: tagged messages behind the reading (null = no entry)"
    )
    sentiment_age_s: int | None = Field(
        None, description="E14.8: age of the retail_sentiment entry (null = no entry)"
    )
    picked_20d: int = Field(
        0, description="E14.8: distinct ET days with a `candidates` row, last 20 sessions"
    )
    proposals_20d: int = Field(0, description="E14.8: `proposals` rows, last 20 sessions")
    in_tier_20d: int | None = Field(
        None,
        description="E14.8: sessions in the last 20 whose in-force `universe_tier` entry for "
        "this tier listed the name (null = core, which has no feed)",
    )


class UniverseDroppedRow(BaseModel):
    model_config = _STRICT

    ticker: str
    tier: str
    reason: str = Field(description="over_active_cap | over_tier_size | …")
    rank: int | None = Field(None, description="The name's rank in its tier (D56 resolves)")


class UniverseTierRow(BaseModel):
    model_config = _STRICT

    name: str = Field(description="core | momentum | discovery | trending (precedence order)")
    listed: int = Field(
        description="E14.8: rows the tier's feed listed before the size cut (raw_count; the "
        "momentum SPMO page lists ~24 for a top-20 tier)"
    )
    active: int = Field(description="Names this tier holds in the active list")
    carried: int = Field(
        0, description="E14.8 (D64): active names carried from the previous run only"
    )
    size_cap: int | None = Field(
        description="The tier's size: core ceiling 25, momentum/discovery/trending sizes; "
        "null = no cut"
    )
    source: str | None = Field(description="Feed source (stockanalysis, settings, config, scout)")
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
    model: Literal["d51", "d56"] = Field(
        "d56",
        description="Tier layout the shown resolve used: d56 (E13.15: the only layout); a "
        "stored pre-cutover resolve reads d51 until the next resolve",
    )
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
    tail_cuts: list[UniverseDroppedRow] = Field(
        default_factory=list,
        description="E13.14: names cut past the active cap (over_active_cap), with tier + rank",
    )
    discovery_fill: int | None = Field(
        None,
        description="E13.14 (D56): names the Scout's discovery feed listed today (0 = none "
        "yet); null for a stored pre-cutover (d51) resolve",
    )
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


def discovery_fill_by_day(
    conn: sqlite3.Connection, first: _dt.date, last: _dt.date
) -> dict[str, int]:
    """D56: per ET day in ``[first, last]``, the member count of the day's latest
    ``universe_tier`` / ``discovery`` entry (the Scout's fill). Days without one are
    absent. Read-only; payloads this build can't read are skipped."""
    if not _has_table(conn, "context_entries"):
        return {}
    lo = _dt.datetime.combine(first, _dt.time.min, tzinfo=ET)
    hi = _dt.datetime.combine(last + _dt.timedelta(days=1), _dt.time.min, tzinfo=ET)
    out: dict[str, int] = {}
    for r in conn.execute(
        "SELECT payload, valid_from FROM context_entries WHERE kind = 'universe_tier'"
        " AND subject = ? ORDER BY valid_from, created_at, rowid",
        (Tier.DISCOVERY.value,),
    ):
        at = parse_ts(r["valid_from"])
        if at is None or not lo <= at < hi:
            continue
        try:
            feed = UniverseTierPayload.model_validate(json.loads(r["payload"]))
        except (ValueError, ValidationError):
            continue
        out[at.astimezone(ET).date().isoformat()] = len(feed.members)  # latest wins
    return dict(sorted(out.items()))


def _trending_velocity(
    conn: sqlite3.Connection, feed: UniverseTierPayload | None, opts: VelocityOptions
) -> dict[str, tuple[float, str]]:
    """E14.5: ``ticker -> (velocity, "reddit #20 · 5.2× (157 vs 30)")`` from the newest
    ``retail_buzz`` entry at or before the trending feed's fetch (read-only; ``{}`` when
    there is none or it predates E14.5's 24 h counts)."""
    from arc.context.kinds import RetailBuzzPayload
    from arc.context.ttl import to_db
    from arc.universe.velocity import buzz_velocities, velocity_text

    at = feed.fetched_at if feed is not None else None
    row = conn.execute(
        "SELECT payload FROM context_entries WHERE kind = 'retail_buzz' AND subject = 'all'"
        + (" AND valid_from <= ?" if at is not None else "")
        + " ORDER BY valid_from DESC, created_at DESC, rowid DESC LIMIT 1",
        (to_db(at),) if at is not None else (),
    ).fetchone()
    if row is None:
        return {}
    try:
        buzz = RetailBuzzPayload.model_validate(json.loads(row["payload"]))
    except (ValueError, ValidationError):
        return {}
    ranks = {
        r.symbol: r.rank or r.position
        for i in buzz.inputs.values()
        if i.type == "apewisdom"
        for r in reversed(i.rows)
    }
    out: dict[str, tuple[float, str]] = {}
    for sym, vel in buzz_velocities(buzz, opts).items():
        text = velocity_text(vel)
        if vel[0] is not None and text is not None:
            out[sym] = (vel[0], f"reddit #{ranks.get(sym, '?')} · {text}")
    return out


class SentimentFacts(NamedTuple):
    """E14.8: one ticker's newest unexpired ``retail_sentiment`` reading."""

    text: str  # `ST 80% bull (10 tagged, 2.7h)` (popover)
    bull_pct: float | None  # bullish share of tagged, percent (None = too few tags)
    tagged: int
    age_s: int | None


def _sentiment_facts(conn: sqlite3.Connection, now: _dt.datetime) -> dict[str, SentimentFacts]:
    """E14.6: ``ticker -> SentimentFacts`` from each ticker's newest unexpired
    ``retail_sentiment`` entry (read-only; ``{}`` when there is none). E14.8 adds the
    structured fields from the same read, so the UI never parses the string."""
    from arc.context.retail_sentiment import sentiment_fact
    from arc.context.ttl import to_db

    rows = conn.execute(
        "SELECT subject, payload, valid_from FROM context_entries"
        " WHERE kind = 'retail_sentiment'"
        " AND valid_from <= ? AND (expires_at IS NULL OR expires_at > ?)"
        " ORDER BY valid_from, created_at, rowid",
        (to_db(now), to_db(now)),
    ).fetchall()
    out: dict[str, SentimentFacts] = {}
    for r in rows:  # oldest first: the newest per ticker wins
        try:
            p = json.loads(r["payload"])
            ratio = p.get("bull_ratio")
            out[r["subject"]] = SentimentFacts(
                text=sentiment_fact(p),
                bull_pct=None if ratio is None else round(float(ratio) * 100, 2),
                tagged=int(p.get("tagged") or 0),
                age_s=_age(parse_ts(r["valid_from"]), now),
            )
        except (ValueError, TypeError, AttributeError):
            continue
    return out


#: E14.8: the look-back of the Picked / Trades / In-tier columns, in sessions.
WINDOW_SESSIONS = 20

_SPMO_WEIGHT_RE = re.compile(r"SPMO weight\s+([0-9]+(?:\.[0-9]+)?)%")


def weight_from_reason(reason: str) -> float | None:
    """E14.8: ``SPMO weight 9.48% (row 1)`` -> ``9.48`` (pre-v5 momentum rows)."""
    m = _SPMO_WEIGHT_RE.search(reason or "")
    return float(m.group(1)) if m else None


def window_sessions(today: _dt.date, n: int = WINDOW_SESSIONS) -> list[_dt.date]:
    """The last *n* sessions ending at *today* (or the session before it), ascending."""
    from arc.utils.calendar import is_session, previous_session

    cur = today if is_session(today) else previous_session(today)
    out = [cur]
    while len(out) < n:
        cur = previous_session(cur)
        out.append(cur)
    return out[::-1]


def _count_by_ticker(conn: sqlite3.Connection, sql: str, lo: str, hi: str) -> dict[str, int]:
    return {r[0]: int(r[1]) for r in conn.execute(sql, (lo, hi)) if r[0]}


def picked_counts(conn: sqlite3.Connection, sessions: list[_dt.date]) -> dict[str, int]:
    """E14.8 Picked: distinct ET days with a ``candidates`` row, per ticker (one query)."""
    if not sessions or not _has_table(conn, "candidates"):
        return {}
    return _count_by_ticker(
        conn,
        "SELECT ticker, COUNT(DISTINCT day) FROM candidates"
        " WHERE day >= ? AND day <= ? GROUP BY ticker",
        sessions[0].isoformat(),
        sessions[-1].isoformat(),
    )


def proposal_counts(conn: sqlite3.Connection, sessions: list[_dt.date]) -> dict[str, int]:
    """E14.8 Trades: ``proposals`` rows per ticker by ``proposals.day`` (one query)."""
    if not sessions or not _has_table(conn, "proposals"):
        return {}
    return _count_by_ticker(
        conn,
        "SELECT ticker, COUNT(*) FROM proposals WHERE day >= ? AND day <= ? GROUP BY ticker",
        sessions[0].isoformat(),
        sessions[-1].isoformat(),
    )


def in_tier_counts(
    conn: sqlite3.Connection, sessions: list[_dt.date]
) -> dict[tuple[str, str], int]:
    """E14.8 In tier: ``(tier, ticker) -> sessions`` whose in-force ``universe_tier`` entry
    listed the name (one query for every feed tier).

    The entry in force on session *d* is the newest one written before the end of *d*
    that had not expired by the start of *d* (a monthly momentum list counts every day it
    is live; a daily discovery list counts its own day and, carried, the next)."""
    if not sessions or not _has_table(conn, "context_entries"):
        return {}
    from arc.context.ttl import to_db

    start = _dt.datetime.combine(sessions[0], _dt.time.min, tzinfo=ET)
    end = _dt.datetime.combine(sessions[-1] + _dt.timedelta(days=1), _dt.time.min, tzinfo=ET)
    entries: dict[str, list[tuple[_dt.datetime, _dt.datetime | None, frozenset[str]]]] = {}
    for r in conn.execute(
        "SELECT subject, payload, valid_from, expires_at FROM context_entries"
        " WHERE kind = 'universe_tier' AND valid_from < ?"
        " AND (expires_at IS NULL OR expires_at > ?)"
        " ORDER BY valid_from, created_at, rowid",
        (to_db(end), to_db(start)),
    ):
        at = parse_ts(r["valid_from"])
        if at is None:
            continue
        try:
            names = frozenset(m["ticker"] for m in json.loads(r["payload"])["members"])
        except (ValueError, TypeError, KeyError):
            continue
        entries.setdefault(r["subject"], []).append((at, parse_ts(r["expires_at"]), names))
    out: dict[tuple[str, str], int] = {}
    for tier, rows in entries.items():
        for d in sessions:
            d0 = _dt.datetime.combine(d, _dt.time.min, tzinfo=ET)
            d1 = d0 + _dt.timedelta(days=1)
            live = [n for at, exp, n in rows if at < d1 and (exp is None or exp > d0)]
            for t in live[-1] if live else ():
                out[(tier, t)] = out.get((tier, t), 0) + 1
    return out


class _RowFacts(NamedTuple):
    """E14.8: the page-wide per-ticker reads :func:`_active_row` looks up."""

    velocity: Mapping[str, tuple[float, str]]
    sentiment: Mapping[str, SentimentFacts]
    picked: Mapping[str, int]
    proposals: Mapping[str, int]
    in_tier: Mapping[tuple[str, str], int]
    channel_labels: Mapping[str, str]


_NO_FACTS = _RowFacts({}, {}, {}, {}, {}, {})


def _origin_label(origin: str, labels: Mapping[str, str]) -> str:
    """``youtube:arete`` -> ``Arete Trading`` (the youtube.briefs channel label)."""
    slug = origin.split(":", 1)[1] if ":" in origin else origin
    return labels.get(slug, slug)


def _active_row(m: TierMember, f: _RowFacts | None = None) -> UniverseActiveRow:
    f = f or _NO_FACTS
    vel = f.velocity.get(m.ticker) if m.tier is Tier.TRENDING else None
    st = f.sentiment.get(m.ticker)
    weight = m.weight_pct
    if weight is None and m.tier is Tier.MOMENTUM:
        weight = weight_from_reason(m.reason)
    return UniverseActiveRow(
        ticker=m.ticker,
        tier=m.tier.value,
        rank=m.rank,
        source=m.source,
        reason=m.reason,
        also_in=[t.value for t in m.also_in],
        inputs=m.inputs,
        velocity=vel[0] if vel else None,
        velocity_detail=vel[1] if vel else None,
        sentiment=st.text if st else None,
        score=m.score,
        score_today=m.score_today,
        score_prev=m.score_prev,
        runs=[d.isoformat() for d in m.runs] or None,
        stance=m.stance,
        origins=list(m.origins) or None,
        origin_labels=[_origin_label(o, f.channel_labels) for o in m.origins] or None,
        carried=bool(m.runs) and m.score_today is None,
        weight_pct=weight,
        sentiment_bull_pct=st.bull_pct if st else None,
        sentiment_tagged=st.tagged if st else None,
        sentiment_age_s=st.age_s if st else None,
        picked_20d=f.picked.get(m.ticker, 0),
        proposals_20d=f.proposals.get(m.ticker, 0),
        in_tier_20d=(None if m.tier is Tier.CORE else f.in_tier.get((m.tier.value, m.ticker), 0)),
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
    return ActiveUniverse(
        model="d56",
        as_of=day,
        members=members,
        counts=counts,
        raw_counts=dict(counts),
    )


def load_universe(
    conn: sqlite3.Connection,
    settings: ArcSettings,
    *,
    now: _dt.datetime,
    director_diversification: str | None = None,
    velocity: VelocityOptions | None = None,
    channel_labels: Mapping[str, str] | None = None,
) -> UniverseResponse:
    """The stored resolve + tier feeds + the effective core/market-reference settings.

    *velocity* (E14.5) = the ``universe.trending`` velocity knobs; each trending member
    gets its Reddit mention velocity (default knobs when ``None``). *channel_labels*
    (E14.8) = youtube.briefs ``slug -> label``, for the discovery Sources column."""
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

    sizes: dict[Tier, int | None] = {Tier.CORE: MAX_CORE, **tier_sizes(settings)}
    core_source = next((m.source for m in active.members if m.tier is Tier.CORE), "settings")
    fixed_source: dict[Tier, str | None] = {
        Tier.CORE: core_source,
        Tier.MOMENTUM: None,
        Tier.DISCOVERY: "scout",
        Tier.TRENDING: "retail_buzz",
    }
    feed_tiers = (Tier.MOMENTUM, Tier.DISCOVERY, Tier.TRENDING)
    tiers: list[UniverseTierRow] = []
    for tier in TIER_ORDER:
        feed = _feed(conn, tier) if has_ctx and tier in feed_tiers else None
        fetched = feed.fetched_at.astimezone(ET) if feed is not None else None
        source = feed.source if feed is not None else fixed_source[tier]
        tiers.append(
            UniverseTierRow(
                name=tier.value,
                listed=int(active.raw_counts.get(tier.value, 0)),
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

    dropped = [
        UniverseDroppedRow(ticker=d.ticker, tier=d.tier.value, reason=d.reason, rank=d.rank)
        for d in active.dropped
    ]
    fill = (
        discovery_fill_by_day(conn, today, today).get(today.isoformat(), 0)
        if active.model == "d56"
        else None
    )
    trending_vel = (
        _trending_velocity(conn, _feed(conn, Tier.TRENDING), velocity or VelocityOptions())
        if has_ctx and any(m.tier is Tier.TRENDING for m in active.members)
        else {}
    )
    sentiment = _sentiment_facts(conn, now) if has_ctx else {}
    sessions = window_sessions(today)
    facts = _RowFacts(
        velocity=trending_vel,
        sentiment=sentiment,
        picked=picked_counts(conn, sessions),
        proposals=proposal_counts(conn, sessions),
        in_tier=in_tier_counts(conn, sessions) if has_ctx else {},
        channel_labels=channel_labels or {},
    )
    rows = [_active_row(m, facts) for m in active.members]
    carried = {t.value: sum(1 for r in rows if r.tier == t.value and r.carried) for t in TIER_ORDER}
    tiers = [t.model_copy(update={"carried": carried.get(t.name, 0)}) for t in tiers]
    return UniverseResponse(
        as_of=now,
        model=active.model,
        state=state,
        resolved_for=active.as_of if state != "none" else None,
        resolved_at=resolved_at,
        resolved_by=resolved_by,
        age_s=_age(resolved_at, now),
        note=note,
        config_version=active.config_version if state != "none" else settings.config_version,
        active_max=settings.universe_active_max,
        active=rows,
        tiers=tiers,
        dropped=dropped,
        market_reference=market_reference(settings),
        tail_cuts=[d for d in dropped if d.reason == DROP_OVER_ACTIVE_CAP],
        discovery_fill=fill,
        core_override_ignored=override,
        director_diversification=director_diversification,
    )
