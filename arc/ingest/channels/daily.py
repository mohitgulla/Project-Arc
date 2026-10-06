"""Daily YouTube channel briefs (E4.6, PLAN D45).

One job (``youtube.briefs``, 05:00 ET on trading days) turns the newest qualifying
video per configured channel from the last ``lookback`` (24 h) into at most one
validated :class:`~arc.models.ChannelBrief` per channel::

    list newest max_videos ──▶ drop excluded (live / Shorts / title_exclude)
        ──▶ newest qualifying video inside the window (metadata fetched only until
            a video is older than now - lookback)
        ──▶ transcript (captions, then audio; shared caption breaker + audio cap)
        ──▶ raw_doc (source_key youtube.<slug>, scalp_status brief_only)
        ──▶ channel processor (LLM extraction + verbatim-quote check)
        ──▶ channel_briefs row (hard expiry = run + ttl) + channel_brief context entry

Everything except the extraction call is deterministic. A channel with no
qualifying video has no brief that day ("no video means no info"); nothing
carries over, and the other channels are not re-weighted. Channels and their
knobs are config (``config/routines.yaml``): adding a channel is one ``channels:``
entry plus one profile dir under :mod:`arc.ingest.channels`.
"""

from __future__ import annotations

import datetime as _dt
import re
import time
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

import structlog
from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator

from arc.context.categories import (
    DEFAULT_CATEGORIES,
    YOUTUBE_CATEGORIES,
    SourceCategory,
    channel_category,
    normalize_category,
    parse_youtube_category,
)
from arc.context.ttl import parse_duration
from arc.ingest.channels.base import BriefParseError
from arc.ingest.channels.briefs import STATUS_ACTIVE, ChannelBriefRepo, video_from_row
from arc.ingest.llm import ScalpLLMError
from arc.utils.calendar import ET

if TYPE_CHECKING:
    import sqlite3

    from arc.config import ArcSettings
    from arc.ingest.channels import ChannelRegistry
    from arc.ingest.channels.base import ProcessResult
    from arc.ingest.llm import PersonaLLM
    from arc.ingest.youtube import TranscriptSession
    from arc.models import ChannelBrief
    from arc.universe.ingest import IngestUniverse

log = structlog.get_logger()

JOB = "youtube.briefs"
SOURCE_PREFIX = "youtube."
SCALP_STATUS_BRIEF_ONLY = "brief_only"
LIVE_STATUSES = frozenset({"is_live", "is_upcoming", "post_live"})
SHORTS_MAX_SECONDS = 60
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


# ---------------------------------------------------------------------------
# Config (the ``youtube.briefs`` job options)
# ---------------------------------------------------------------------------


class DailyChannel(BaseModel):
    """One ``channels:`` entry. Defaults equal the global YouTube settings."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    slug: str = Field(..., description="channel profile slug (arc/ingest/channels/<slug>/)")
    channel: str = Field(..., min_length=3, description="UC... id or a full channel URL")
    category: SourceCategory = Field(
        None,  # type: ignore[assignment]  # validated: missing raises (D49)
        validate_default=True,
        description="D49: youtube_macro | youtube_micro (required)",
    )
    label: str = ""
    max_videos: int = Field(5, ge=1, le=50)
    max_audio_minutes: int | None = Field(None, ge=1)
    skip_shorts: bool = True
    title_exclude: list[str] = Field(default_factory=list)

    @field_validator("slug")
    @classmethod
    def _slug(cls, v: str) -> str:
        if not _SLUG_RE.match(v):
            msg = f"channel slug must be lower-case [a-z0-9_-], got {v!r}"
            raise ValueError(msg)
        return v

    @field_validator("category", mode="before")
    @classmethod
    def _category(cls, v: Any, info: ValidationInfo) -> Any:
        slug = (info.data or {}).get("slug", "?")
        return parse_youtube_category(v, where=f"youtube.briefs.channels.{slug}")

    @field_validator("title_exclude")
    @classmethod
    def _patterns(cls, v: list[str]) -> list[str]:
        for p in v:
            try:
                re.compile(p)
            except re.error as exc:
                msg = f"invalid title_exclude pattern {p!r}: {exc}"
                raise ValueError(msg) from exc
        return v

    @property
    def source_key(self) -> str:
        return f"{SOURCE_PREFIX}{self.slug}"

    @property
    def display(self) -> str:
        return self.label or self.slug

    @property
    def url(self) -> str:
        if self.channel.startswith(("http://", "https://")):
            return self.channel
        return f"https://www.youtube.com/channel/{self.channel}/videos"

    @property
    def channel_id(self) -> str | None:
        m = re.search(r"(UC[A-Za-z0-9_-]{20,})", self.channel)
        return m.group(1) if m else None


class DailyBriefConfig(BaseModel):
    """Options of the ``youtube.briefs`` job (``lookback`` + ``channels``)."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    lookback: _dt.timedelta = _dt.timedelta(hours=24)
    channels: list[DailyChannel] = Field(..., min_length=1)

    @field_validator("lookback", mode="before")
    @classmethod
    def _lookback(cls, v: Any) -> Any:
        return parse_duration(v) if isinstance(v, str) else v

    @field_validator("channels")
    @classmethod
    def _unique(cls, v: list[DailyChannel]) -> list[DailyChannel]:
        slugs = [c.slug for c in v]
        if len(set(slugs)) != len(slugs):
            msg = "youtube.briefs: a channel slug is listed twice"
            raise ValueError(msg)
        return v

    @classmethod
    def from_options(cls, options: Mapping[str, Any]) -> DailyBriefConfig:
        return cls.model_validate(dict(options))


# ---------------------------------------------------------------------------
# Pick (pure: listing + metadata in, decision out)
# ---------------------------------------------------------------------------


class ExcludeReason(StrEnum):
    LIVE = "live"
    SHORT = "short"
    TITLE = "title_exclude"
    NO_TIMESTAMP = "no_timestamp"
    NO_METADATA = "no_metadata"


@dataclass(frozen=True)
class Excluded:
    video_id: str
    title: str
    reason: str

    def as_dict(self) -> dict[str, str]:
        return {"video_id": self.video_id, "title": self.title, "reason": self.reason}


@dataclass
class Pick:
    """What :func:`pick_video` decided for one channel."""

    scanned: int = 0
    metadata_fetched: int = 0
    excluded: list[Excluded] = field(default_factory=list)
    info: dict[str, Any] | None = None  # the picked video's metadata
    published_at: _dt.datetime | None = None

    @property
    def video_id(self) -> str | None:
        return str(self.info["id"]) if self.info else None


def _published(info: Mapping[str, Any]) -> _dt.datetime | None:
    for key in ("release_timestamp", "timestamp"):
        ts = info.get(key)
        if isinstance(ts, int | float):
            return _dt.datetime.fromtimestamp(ts, tz=_dt.UTC).astimezone(ET)
    return None


def _exclude_reason(entry: Mapping[str, Any], ch: DailyChannel) -> ExcludeReason | None:
    """Exclusion from listing or metadata fields (live, Shorts, title regex)."""
    if entry.get("live_status") in LIVE_STATUSES or entry.get("is_live"):
        return ExcludeReason.LIVE
    if ch.skip_shorts:
        url = str(entry.get("url") or entry.get("webpage_url") or "")
        duration = entry.get("duration")
        is_short = "/shorts/" in url or (
            isinstance(duration, int | float) and 0 < duration <= SHORTS_MAX_SECONDS
        )
        if is_short:
            return ExcludeReason.SHORT
    title = str(entry.get("title") or "")
    if any(re.search(p, title, flags=re.IGNORECASE) for p in ch.title_exclude):
        return ExcludeReason.TITLE
    return None


def pick_video(
    listing: Iterable[Mapping[str, Any]],
    fetch_info: Callable[[str], Mapping[str, Any]],
    ch: DailyChannel,
    *,
    now: _dt.datetime,
    lookback: _dt.timedelta,
) -> Pick:
    """The newest qualifying video of *ch* published in ``(now - lookback, now]``.

    *listing* is the channel's flat playlist, newest first (``max_videos`` long).
    Listing-level exclusions are applied without a metadata call; metadata is
    fetched one video at a time and the scan stops at the first video older
    than the window, so an old channel costs one metadata call.
    """
    out = Pick()
    since = now - lookback
    for entry in listing:
        vid = str(entry.get("id") or "")
        if not vid:
            continue
        out.scanned += 1
        title = str(entry.get("title") or "")
        reason = _exclude_reason(entry, ch)
        if reason is not None:
            out.excluded.append(Excluded(vid, title, reason.value))
            continue
        info = dict(fetch_info(f"https://www.youtube.com/watch?v={vid}") or {})
        out.metadata_fetched += 1
        if not info:
            out.excluded.append(Excluded(vid, title, ExcludeReason.NO_METADATA.value))
            continue
        info.setdefault("id", vid)
        published = _published(info)
        if published is None:
            out.excluded.append(Excluded(vid, title, ExcludeReason.NO_TIMESTAMP.value))
            continue
        if published <= since:
            break  # newest first: everything further down is older still
        reason = _exclude_reason(info, ch)
        if reason is not None:
            out.excluded.append(Excluded(vid, str(info.get("title") or title), reason.value))
            continue
        if published > now:
            continue  # scheduled premiere (not out yet at *now*)
        out.info = info
        out.published_at = published
        break
    return out


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------


class Outcome(StrEnum):
    BRIEFED = "briefed"  # new brief stored and written to context
    EXISTING = "existing"  # this video already has an active brief (re-run)
    NO_VIDEO = "no_video"  # nothing qualifying in the window: no info today
    PENDING = "pending"  # a video exists but no transcript yet (grace / too long / cap)
    ERROR = "error"  # listing / LLM / parse failure: alerts


@dataclass
class ChannelRun:
    """Per-channel facts for the run summary and manifest."""

    channel: DailyChannel
    outcome: Outcome = Outcome.NO_VIDEO
    pick: Pick = field(default_factory=Pick)
    transcript_source: str | None = None
    transcript_chars: int = 0
    transcript_truncated: bool = False
    pending_reason: str | None = None
    raw_doc_id: str | None = None
    brief: ChannelBrief | None = None
    brief_status: str | None = None
    items: int = 0
    dropped: int = 0
    dropped_by_reason: dict[str, int] = field(default_factory=dict)
    model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    wall_s: float = 0.0
    error: str | None = None

    @property
    def present(self) -> bool:
        return self.outcome in (Outcome.BRIEFED, Outcome.EXISTING)

    def manifest(self) -> dict[str, Any]:
        """JSON-able per-channel record (run manifest metrics)."""
        p = self.pick
        return {
            "outcome": self.outcome.value,
            "category": self.channel.category.value,
            "scanned": p.scanned,
            "metadata_fetched": p.metadata_fetched,
            "excluded": [e.as_dict() for e in p.excluded],
            "video_id": p.video_id,
            "title": (p.info or {}).get("title"),
            "published_at": p.published_at.isoformat() if p.published_at else None,
            "duration_s": (p.info or {}).get("duration"),
            "transcript_source": self.transcript_source,
            "transcript_chars": self.transcript_chars,
            "transcript_truncated": self.transcript_truncated,
            "pending_reason": self.pending_reason,
            "raw_doc_id": self.raw_doc_id,
            "brief_id": self.brief.brief_id if self.brief else None,
            "brief_status": self.brief_status,
            "items": self.items,
            "dropped": self.dropped,
            "dropped_by_reason": self.dropped_by_reason,
            "model": self.model,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cost_usd": self.cost_usd,
            "wall_s": round(self.wall_s, 1),
            "error": self.error,
        }

    def short(self) -> str:
        """``StockedUp ✓`` / ``TradeBrigade – (no video 24h)`` for the summary line."""
        name = self.channel.display.replace(" ", "")
        if self.present:
            return f"{name} ✓"
        if self.outcome is Outcome.NO_VIDEO:
            return f"{name} – (no video {{lookback}})"
        if self.outcome is Outcome.PENDING:
            return f"{name} – (pending: {self.pending_reason})"
        return f"{name} ✗ ({self.error})"


@dataclass
class DailyBriefRun:
    channels: list[ChannelRun]
    lookback: _dt.timedelta

    @property
    def present(self) -> int:
        return sum(1 for c in self.channels if c.present)

    @property
    def errors(self) -> list[ChannelRun]:
        return [c for c in self.channels if c.outcome is Outcome.ERROR]

    def summary(self) -> str:
        hours = self.lookback.total_seconds() / 3600
        lb = f"{hours:g}h"
        parts = " ".join(c.short().replace("{lookback}", lb) for c in self.channels)
        return f"briefs {self.present}/{len(self.channels)} · {parts}"


def _no_text_reason(err: Exception) -> str:
    return f"{type(err).__name__}: {str(err)[:160]}"


def expire_at(now: _dt.datetime, ttl: _dt.timedelta) -> _dt.datetime:
    """Hard brief expiry (rule 5): *ttl* after the run that built it."""
    return now + ttl


def run_daily_briefs(
    conn: sqlite3.Connection,
    settings: ArcSettings,
    cfg: DailyBriefConfig,
    *,
    llm: PersonaLLM,
    session: TranscriptSession,
    now: _dt.datetime,
    ttl: _dt.timedelta,
    universe: IngestUniverse,
    registry: ChannelRegistry,
    list_videos: Callable[[str, int], list[dict[str, Any]]],
    fetch_info: Callable[[str], Mapping[str, Any]],
    write_brief: Callable[[ChannelRun], None],
    price_lookup: Callable[[str], float | None] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> DailyBriefRun:
    """Run every configured channel in order (one shared caption session).

    *write_brief* is called once per channel that ends with a brief (the job
    writes the ``channel_brief`` context entry there). Listing failures, LLM and
    parse errors mark that channel ``error`` and the run carries on.
    """
    from arc.ingest.youtube import (
        MAX_CAPTION_CHARS,
        TranscriptSource,
        YoutubeListError,
        store_transcript,
    )

    now = now.astimezone(ET)
    repo = ChannelBriefRepo(conn)
    runs: list[ChannelRun] = []
    for ch in cfg.channels:
        started = clock()
        cr = ChannelRun(channel=ch)
        runs.append(cr)
        proc = registry.for_slug(ch.slug)
        if proc is None:
            cr.outcome, cr.error = Outcome.ERROR, f"no channel profile {ch.slug!r}"
            log.error("youtube.brief_failed", channel=ch.slug, error=cr.error)
            continue
        try:
            listing = list_videos(ch.url, ch.max_videos)
        except YoutubeListError as exc:
            cr.outcome, cr.error = Outcome.ERROR, _no_text_reason(exc)
            log.error("youtube.brief_failed", channel=ch.slug, error=cr.error)
            continue
        cr.pick = pick_video(listing, fetch_info, ch, now=now, lookback=cfg.lookback)
        log.info(
            "youtube.brief_pick",
            channel=ch.slug,
            scanned=cr.pick.scanned,
            excluded=[e.as_dict() for e in cr.pick.excluded],
            video_id=cr.pick.video_id,
            published=cr.pick.published_at.isoformat() if cr.pick.published_at else None,
        )
        if cr.pick.info is None:
            cr.outcome = Outcome.NO_VIDEO
            cr.wall_s = clock() - started
            log.info("youtube.brief_no_video", channel=ch.slug, lookback=str(cfg.lookback))
            continue
        vid = str(cr.pick.video_id)
        if repo.exists(ch.slug, vid):
            row = conn.execute(
                "SELECT id, status FROM channel_briefs WHERE channel_slug = ? AND video_id = ?",
                (ch.slug, vid),
            ).fetchone()
            cr.brief_status = str(row["status"])
            active = [b for b in repo.active(now, channel_slug=ch.slug) if b.video_id == vid]
            if cr.brief_status == STATUS_ACTIVE and active:
                cr.outcome = Outcome.EXISTING
                cr.brief = active[0]
                cr.items = _items(cr.brief)
                write_brief(cr)
            else:  # expired / superseded: today's window still points at it, but no info
                cr.outcome = Outcome.NO_VIDEO
            cr.wall_s = clock() - started
            continue

        text, source, pending = session.transcript(
            cr.pick.info, vid, max_audio_minutes=ch.max_audio_minutes
        )
        if not text:
            cr.outcome, cr.pending_reason = Outcome.PENDING, pending
            cr.wall_s = clock() - started
            log.info("youtube.brief_pending", channel=ch.slug, video_id=vid, reason=pending)
            continue
        cr.transcript_source = source.value if source else None
        cr.transcript_chars = len(text)
        cr.transcript_truncated = (
            source is TranscriptSource.CAPTIONS and len(text) >= MAX_CAPTION_CHARS
        )
        if cr.transcript_truncated:
            log.warning(
                "youtube.transcript_truncated",
                channel=ch.slug,
                video_id=vid,
                chars=len(text),
                duration_s=cr.pick.info.get("duration"),
            )
        info = dict(cr.pick.info)
        info.setdefault("channel_id", ch.channel_id)
        doc_id, _ = store_transcript(
            conn,
            info=info,
            video_id=vid,
            transcript=text,
            source=source,  # type: ignore[arg-type]
            source_key=ch.source_key,
            universe=universe,
        )
        cr.raw_doc_id = doc_id
        mark_brief_only(conn, [doc_id])
        row = conn.execute("SELECT * FROM raw_docs WHERE id = ?", (doc_id,)).fetchone()
        video = video_from_row(dict(row))
        try:
            result: ProcessResult = proc.process(
                video,
                llm,
                universe=universe.mention_universe(video.transcript),
                price_lookup=price_lookup,
            )
        except (ScalpLLMError, BriefParseError) as exc:
            cr.outcome, cr.error = Outcome.ERROR, _no_text_reason(exc)
            cr.wall_s = clock() - started
            log.error("youtube.brief_failed", channel=ch.slug, video_id=vid, error=cr.error)
            continue
        cr.brief_status = repo.store(
            result, proc, now=now, raw_doc_id=doc_id, expires=expire_at(now, ttl)
        )
        cr.brief = result.brief
        cr.items = result.kept
        cr.dropped = len(result.dropped)
        cr.dropped_by_reason = result.dropped_by_reason
        cr.model = result.model
        cr.input_tokens = result.input_tokens
        cr.output_tokens = result.output_tokens
        cr.cost_usd = result.cost_usd
        if cr.brief_status == STATUS_ACTIVE:
            cr.outcome = Outcome.BRIEFED
            write_brief(cr)
        else:
            cr.outcome = Outcome.NO_VIDEO
        cr.wall_s = clock() - started
        log.info(
            "youtube.brief_done",
            channel=ch.slug,
            video_id=vid,
            brief_id=cr.brief.brief_id,
            status=cr.brief_status,
            items=cr.items,
            dropped=cr.dropped_by_reason,
            transcript=cr.transcript_source,
            wall_s=round(cr.wall_s, 1),
        )
    return DailyBriefRun(channels=runs, lookback=cfg.lookback)


def _items(brief: ChannelBrief) -> int:
    return (
        (1 if brief.market_bias else 0)
        + len(brief.levels)
        + len(brief.calls)
        + len(brief.catalysts)
        + len(brief.risk_flags)
    )


def mark_brief_only(conn: sqlite3.Connection, doc_ids: list[str]) -> None:
    """Close YouTube docs for the Scalp: they reach trading only through briefs (D45)."""
    from arc.ingest.store import RawDocRepo

    RawDocRepo(conn).mark_brief_only(doc_ids, run_id=JOB)


# ---------------------------------------------------------------------------
# Research view (rule 6): code-built presence line + cross-channel agreement
# ---------------------------------------------------------------------------


def _channel_cat(c: Mapping[str, Any]) -> SourceCategory | None:
    return normalize_category(c.get("category"))


def category_channels(
    channels: Sequence[Mapping[str, Any]], category: SourceCategory | None
) -> list[Mapping[str, Any]]:
    """The configured channels in *category* (all of them when *category* is None)."""
    if category is None:
        return list(channels)
    return [c for c in channels if _channel_cat(c) is category]


def brief_presence_line(
    present: Iterable[str],
    channels: Sequence[Mapping[str, Any]],
    category: SourceCategory | None = None,
) -> str:
    """``YouTube macro briefs: n/N channels (missing: …)`` from config + active briefs.

    D49: with *category*, the denominator is the channels configured in that category
    (never all channels, never renormalised over the ones present). Without it, every
    channel (``YouTube briefs: …``).
    """
    have = set(present)
    chs = category_channels(channels, category)
    missing = [c.get("label") or c["slug"] for c in chs if c["slug"] not in have]
    n = sum(1 for c in chs if c["slug"] in have)
    name = "YouTube" if category is None else DEFAULT_CATEGORIES[category].label
    line = f"{name} briefs: {n}/{len(chs)} channels"
    return f"{line} (missing: {', '.join(missing)})" if missing else line


def brief_agreement(
    briefs: Iterable[Mapping[str, Any]],
    channels: Sequence[Mapping[str, Any]],
    category: SourceCategory | None = None,
) -> list[str]:
    """Distinct-channel agreement per (ticker, stance), counted by code.

    Each channel is one equal-weight voice: a channel's several calls on one
    ticker count once, and the denominator is every configured channel *in the
    category* (D49; all channels when *category* is None), with no renormalisation
    over the ones present. Briefs from channels outside the category never count.
    The market bias counts as ticker ``market``. Sorted by count, then ticker.
    """
    chs = category_channels(channels, category)
    labels = {c["slug"]: c.get("label") or c["slug"] for c in chs}
    voices: dict[tuple[str, str], set[str]] = {}
    for b in briefs:
        slug = str(b.get("channel_slug"))
        if slug not in labels:
            continue
        bias = b.get("market_bias")
        if isinstance(bias, Mapping) and bias.get("stance"):
            voices.setdefault(("market", str(bias["stance"])), set()).add(slug)
        for call in b.get("calls") or []:
            voices.setdefault((str(call["ticker"]), str(call["stance"])), set()).add(slug)
    total = len(chs)
    lines = []
    for (ticker, stance), slugs in sorted(
        voices.items(), key=lambda kv: (-len(kv[1]), kv[0][0], kv[0][1])
    ):
        names = ", ".join(sorted(labels.get(s, s) for s in slugs))
        lines.append(f"{ticker} {stance}: {len(slugs)}/{total} channels ({names})")
    return lines


def youtube_groups(
    channels: Sequence[Mapping[str, Any]],
) -> list[tuple[SourceCategory | None, list[Mapping[str, Any]]]]:
    """``[(category, channels)]`` in display order (D49), one per YouTube category.

    Channels recorded before D49 carry no category; then one ungrouped
    ``(None, channels)`` entry keeps an old prompt input readable.
    """
    if channels and all(_channel_cat(c) is None for c in channels):
        return [(None, list(channels))]
    return [(cat, category_channels(channels, cat)) for cat in YOUTUBE_CATEGORIES]


def brief_category(
    brief: Mapping[str, Any], channels: Sequence[Mapping[str, Any]]
) -> SourceCategory | None:
    """D49: a ``channel_brief``'s category, from its channel's config entry."""
    return channel_category(brief.get("channel_slug"), channels)


def prompt_brief(b: Mapping[str, Any], label: str) -> dict[str, Any]:
    """The part of a brief Research reads (no ids or bookkeeping fields)."""
    keep = ("title", "published_at", "applies_to_session", "market_bias", "levels", "calls")
    out: dict[str, Any] = {"channel": label}
    out.update({k: b.get(k) for k in keep if b.get(k) not in (None, [])})
    for k in ("catalysts", "risk_flags"):
        if b.get(k):
            out[k] = b[k]
    return out


def configured_channels(routines_options: Mapping[str, Any] | None) -> list[dict[str, str]]:
    """``[{slug, label, category}]`` of the ``youtube.briefs`` job, ``[]`` if not configured."""
    if not routines_options or not routines_options.get("channels"):
        return []
    cfg = DailyBriefConfig.from_options(routines_options)
    return [
        {"slug": c.slug, "label": c.display, "category": c.category.value} for c in cfg.channels
    ]


def present_count(runs: Iterable[ChannelRun]) -> Counter[str]:
    return Counter(r.outcome.value for r in runs)
