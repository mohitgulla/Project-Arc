"""Channel brief storage, lifecycle and bridges (E4.4, PLAN D14).

Lifecycle ("informs trading until replaced")::

    stored ──▶ active ──(newer brief for the same channel)──▶ superseded
                  └────(now >= expires_at)────────────────▶ expired

* One active brief per channel (enforced by a partial unique index).
* ``expires_at`` = close of the last session the brief may inform: session
  ``applies_to_session`` plus ``profile.active_sessions - 1`` more trading
  sessions (so a ``trading_daily`` channel covers at most 2 sessions),
  computed with :mod:`arc.utils.calendar` so weekends and holidays are
  skipped.
* :func:`active_briefs` is what downstream (Scalp / Research context) reads;
  a stale brief never comes back from it.

:func:`brief_to_candidates` is the deterministic bridge to the E4.2
``Candidate`` funnel. :func:`process_new_videos` wires ingestion to the
processors. No broker access anywhere in this module.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

from arc.ingest.channels import ChannelRegistry, default_registry
from arc.ingest.channels.base import (
    BriefParseError,
    ChannelProcessor,
    ProcessResult,
    VideoDoc,
    video_id_from_url,
)
from arc.ingest.llm import FixtureScalpLLM, ScalpLLMError
from arc.ingest.scalp import merge_candidates
from arc.models import (
    BriefCatalystKind,
    CallHorizon,
    Candidate,
    CatalystType,
    ChannelBrief,
    ExpectedImpact,
    Stance,
)
from arc.universe.ingest import IngestUniverse
from arc.utils.calendar import ET, add_sessions, now_et, session_close

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable, Iterable

    from arc.config import ArcSettings
    from arc.ingest.llm import PersonaLLM

log = structlog.get_logger()

STATUS_ACTIVE = "active"
STATUS_SUPERSEDED = "superseded"
STATUS_EXPIRED = "expired"

# Confidence given to a ticker catalyst that has no matching call (before the
# channel trust weight is applied): half-conviction.
CATALYST_BASE_CONVICTION = 0.5


def _utc_iso(ts: _dt.datetime) -> str:
    return ts.astimezone(_dt.UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def expires_at(brief: ChannelBrief, processor: ChannelProcessor) -> _dt.datetime:
    """Close (ET) of the last trading session *brief* may inform."""
    last = add_sessions(brief.applies_to_session, processor.profile.active_sessions - 1)
    return session_close(last)


# ---------------------------------------------------------------------------
# Repository
# ---------------------------------------------------------------------------


class ChannelBriefRepo:
    """Persist briefs and run the supersede / expire state machine."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def exists(self, channel_slug: str, video_id: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM channel_briefs WHERE channel_slug = ? AND video_id = ?",
            (channel_slug, video_id),
        ).fetchone()
        return row is not None

    def latest_published(self, channel_slug: str) -> str | None:
        row = self.conn.execute(
            "SELECT MAX(published_at) FROM channel_briefs WHERE channel_slug = ?",
            (channel_slug,),
        ).fetchone()
        return row[0] if row else None

    def store(
        self,
        result: ProcessResult,
        processor: ChannelProcessor,
        *,
        now: _dt.datetime,
        raw_doc_id: str | None = None,
        expires: _dt.datetime | None = None,
    ) -> str:
        """Store a brief; supersede the channel's previous active brief.

        A brief that is already past its expiry is stored as ``expired``; one
        older than the current active brief is stored as ``superseded``.
        *expires* (E4.6 / D45: run time + the job's context TTL) replaces the
        profile's session-based expiry when given. Returns the stored status.
        """
        brief = result.brief
        exp = expires if expires is not None else expires_at(brief, processor)
        now_iso = _utc_iso(now)
        published = brief.published_at.astimezone(ET).isoformat()
        slug = brief.channel_slug

        current = self.conn.execute(
            "SELECT id, published_at FROM channel_briefs WHERE channel_slug = ? AND status = ?",
            (slug, STATUS_ACTIVE),
        ).fetchone()
        newer_active = current is not None and _parse(current["published_at"]) > brief.published_at

        if now >= exp:
            status = STATUS_EXPIRED
        elif newer_active:
            status = STATUS_SUPERSEDED
        else:
            status = STATUS_ACTIVE

        with self.conn:
            if status == STATUS_ACTIVE and current is not None:
                self.conn.execute(
                    """UPDATE channel_briefs SET status = ?, superseded_by = ?, updated_at = ?
                       WHERE id = ?""",
                    (STATUS_SUPERSEDED, brief.brief_id, now_iso, current["id"]),
                )
                log.info("channel.brief.superseded", channel=slug, old=current["id"])
            self.conn.execute(
                """INSERT INTO channel_briefs
                   (id, channel_slug, channel_id, video_id, video_url, raw_doc_id,
                    published_at, applies_to_session, expires_at, guidelines_version,
                    status, superseded_by, brief_json, kept, dropped, model,
                    prompt_sha256, raw_response, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    brief.brief_id,
                    slug,
                    processor.profile.channel_id,
                    brief.video_id,
                    brief.video_url,
                    raw_doc_id,
                    published,
                    brief.applies_to_session.isoformat(),
                    _utc_iso(exp),
                    brief.guidelines_version,
                    status,
                    current["id"] if status == STATUS_SUPERSEDED and current else None,
                    brief.model_dump_json(),
                    result.kept,
                    json.dumps([d.as_dict() for d in result.dropped], default=str),
                    result.model or "unknown",
                    hashlib.sha256(result.prompt.encode()).hexdigest(),
                    result.raw_response or None,
                    now_iso,
                    now_iso,
                ),
            )
        log.info("channel.brief.stored", channel=slug, brief_id=brief.brief_id, status=status)
        return status

    def expire_stale(self, now: _dt.datetime) -> int:
        """Mark active briefs past ``expires_at`` as expired. Returns rows changed."""
        now_iso = _utc_iso(now)
        with self.conn:
            cur = self.conn.execute(
                """UPDATE channel_briefs SET status = ?, updated_at = ?
                   WHERE status = ? AND expires_at <= ?""",
                (STATUS_EXPIRED, now_iso, STATUS_ACTIVE, now_iso),
            )
        return cur.rowcount

    def active(self, now: _dt.datetime, *, channel_slug: str | None = None) -> list[ChannelBrief]:
        self.expire_stale(now)
        sql = "SELECT brief_json FROM channel_briefs WHERE status = ? AND expires_at > ?"
        params: list[Any] = [STATUS_ACTIVE, _utc_iso(now)]
        if channel_slug:
            sql += " AND channel_slug = ?"
            params.append(channel_slug)
        sql += " ORDER BY published_at DESC"
        rows = self.conn.execute(sql, params).fetchall()
        return [ChannelBrief.model_validate_json(r["brief_json"]) for r in rows]

    def status_of(self, brief_id: str) -> str | None:
        row = self.conn.execute(
            "SELECT status FROM channel_briefs WHERE id = ?", (brief_id,)
        ).fetchone()
        return row[0] if row else None

    def row(self, brief_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM channel_briefs WHERE id = ?", (brief_id,)).fetchone()
        return dict(row) if row else None


def _parse(ts: str) -> _dt.datetime:
    return _dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))


def active_briefs(
    conn: sqlite3.Connection,
    now: _dt.datetime | None = None,
    *,
    channel_slug: str | None = None,
) -> list[ChannelBrief]:
    """Briefs that may inform trading at *now* (newest first, at most one per channel)."""
    return ChannelBriefRepo(conn).active(
        (now or now_et()).astimezone(ET), channel_slug=channel_slug
    )


# ---------------------------------------------------------------------------
# Bridge to the E4.2 Candidate funnel (deterministic, no LLM)
# ---------------------------------------------------------------------------

_CATALYST_TYPE: dict[BriefCatalystKind, CatalystType] = {
    BriefCatalystKind.EARNINGS: CatalystType.EARNINGS,
    BriefCatalystKind.MACRO: CatalystType.MACRO,
    BriefCatalystKind.FED: CatalystType.MACRO,
    BriefCatalystKind.SECTOR: CatalystType.SECTOR,
    BriefCatalystKind.GEOPOLITICAL: CatalystType.NEWS,
    BriefCatalystKind.OTHER: CatalystType.NEWS,
}

_IMPACT_STANCE: dict[ExpectedImpact, Stance] = {
    ExpectedImpact.BULLISH: Stance.BULLISH,
    ExpectedImpact.BEARISH: Stance.BEARISH,
    ExpectedImpact.VOLATILE: Stance.NEUTRAL,
    ExpectedImpact.UNKNOWN: Stance.NEUTRAL,
}

_SESSION_HORIZONS = {CallHorizon.INTRADAY, CallHorizon.NEXT_SESSION}


def _et_midnight(d: _dt.date) -> _dt.datetime:
    return _dt.datetime(d.year, d.month, d.day, tzinfo=ET)


def brief_to_candidates(
    brief: ChannelBrief,
    settings: ArcSettings,
    *,
    registry: ChannelRegistry | None = None,
    universe: Iterable[str] | None = None,
) -> list[Candidate]:
    """Map a brief's universe-ticker calls and ticker catalysts to ``Candidate``s.

    * ``confidence = conviction × trust_weight`` (catalysts without a call use
      :data:`CATALYST_BASE_CONVICTION`).
    * ``catalyst_type``: a call on a ticker that also has an earnings catalyst
      in the brief is ``earnings``; a call with a named level is
      ``technical``; otherwise ``news``. Catalysts map by kind.
    * ``sources = [video_url]``; ``created_at = published_at``.
    * Tickers outside the active list (D51; *universe*, default the core) are
      logged as proposed additions (D9) and do not become candidates here: this
      bridge bypasses the Scalp's D28 liquidity screen, so open-universe names
      reach Research only via the Scalp (which reads the same transcript).
      One candidate per ticker (merged).
    """
    reg = registry or default_registry()
    proc = reg.for_slug(brief.channel_slug) or reg.default
    trust = proc.profile.trust_weight
    if universe is None:  # D51: the caller passes today's active list; else the core
        from arc.universe.tiers import core_tickers

        universe = core_tickers(settings)
    universe = {t.upper() for t in universe}

    earnings = {
        t: c for c in brief.catalysts if c.kind is BriefCatalystKind.EARNINGS for t in c.tickers
    }
    leveled = {lvl.ticker for lvl in brief.levels}
    out: dict[str, Candidate] = {}
    proposed: set[str] = set()

    def add(c: Candidate) -> None:
        out[c.ticker] = merge_candidates(out[c.ticker], c) if c.ticker in out else c

    for call in brief.calls:
        if call.ticker not in universe:
            proposed.add(call.ticker)
            continue
        if call.ticker in earnings:
            ctype, cdate = CatalystType.EARNINGS, earnings[call.ticker].date
        else:
            ctype = CatalystType.TECHNICAL if call.ticker in leveled else CatalystType.NEWS
            cdate = brief.applies_to_session if call.horizon in _SESSION_HORIZONS else None
        add(
            Candidate(
                ticker=call.ticker,
                stance=call.stance,
                catalyst_type=ctype,
                catalyst_date=_et_midnight(cdate) if cdate else None,
                confidence=round(call.conviction * trust, 6),
                sources=[brief.video_url],
                created_at=brief.published_at,
            )
        )

    called = {c.ticker for c in brief.calls}
    for cat in brief.catalysts:
        for t in cat.tickers:
            if t not in universe:
                proposed.add(t)
                continue
            if t in called:
                continue  # the call already carries this ticker (and its catalyst type)
            add(
                Candidate(
                    ticker=t,
                    stance=_IMPACT_STANCE[cat.expected_impact],
                    catalyst_type=_CATALYST_TYPE[cat.kind],
                    catalyst_date=_et_midnight(cat.date) if cat.date else None,
                    confidence=round(CATALYST_BASE_CONVICTION * trust, 6),
                    sources=[brief.video_url],
                    created_at=brief.published_at,
                )
            )

    if proposed:
        log.info(
            "channel.brief.proposed_universe_additions",
            channel=brief.channel_slug,
            brief_id=brief.brief_id,
            tickers=sorted(proposed),
        )
    return sorted(out.values(), key=lambda c: (-c.confidence, c.ticker))


# ---------------------------------------------------------------------------
# Runner: new YouTube transcripts → stored briefs
# ---------------------------------------------------------------------------

_PREFIX = re.compile(r"^\s*(?:\[[^\]\n]{0,200}\]\s*){1,2}")
_VTT_HEADER = re.compile(r"^\s*Kind:\s*\w+\s+Language:\s*[\w-]+\s*")


def video_from_row(row: dict[str, Any]) -> VideoDoc:
    """Build a :class:`VideoDoc` from a ``raw_docs`` row (strips the connector prefix)."""
    raw = row["text"]
    title = row.get("title") or ""
    marker = f"[{title}]" if title else ""
    if marker and marker in raw[: len(marker) + 250]:
        text = raw[raw.index(marker) + len(marker) :]
    else:
        text = _PREFIX.sub("", raw, count=1)
        if not title:
            m = re.match(r"^\s*(?:\[[^\]]*\]\s*)?\[([^\]]*)\]", raw)
            title = m.group(1) if m else ""
    text = _VTT_HEADER.sub("", text.lstrip(), count=1)
    published = _parse(row["published_at"])
    if published.tzinfo is None:
        published = published.replace(tzinfo=_dt.UTC)
    return VideoDoc(
        video_id=video_id_from_url(row["url"]),
        video_url=row["url"],
        title=title,
        published_at=published.astimezone(ET),
        transcript=text,
        channel_id=row.get("channel_id"),
        raw_doc_id=row.get("id"),
    )


def _processor_for_row(registry: ChannelRegistry, row: dict[str, Any]) -> ChannelProcessor:
    if row.get("channel_id"):
        return registry.for_channel(row["channel_id"])
    # Rows ingested before migration 005 have no channel_id: match "[<channel>]".
    for proc in registry.by_slug.values():
        if row["text"].startswith(f"[{proc.profile.display_name}]"):
            return proc
    return registry.default


@dataclass
class BriefRunResult:
    processed: int = 0
    stored: dict[str, int] = field(default_factory=dict)
    skipped: int = 0
    failed: int = 0
    results: list[ProcessResult] = field(default_factory=list)
    candidates: list[Candidate] = field(default_factory=list)


def process_new_videos(
    conn: sqlite3.Connection,
    settings: ArcSettings,
    llm: PersonaLLM,
    *,
    registry: ChannelRegistry | None = None,
    price_lookup: Callable[[str], float | None] | None = None,
    now: _dt.datetime | None = None,
) -> BriefRunResult:
    """Run each channel's processor on its newest un-briefed transcript.

    Per channel only the newest stored video without a brief is processed,
    and only when it is newer than the channel's latest brief (older videos
    would be superseded immediately). LLM or parse failures leave the video
    un-briefed so the next run retries it.
    """
    registry = registry or default_registry()
    now = (now or now_et()).astimezone(ET)
    repo = ChannelBriefRepo(conn)
    run = BriefRunResult()
    uni = IngestUniverse.from_settings(settings, now=now, conn=conn)

    rows = [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM raw_docs WHERE source = 'youtube' ORDER BY published_at DESC, id"
        ).fetchall()
    ]
    newest: dict[str, tuple[ChannelProcessor, dict[str, Any]]] = {}
    for row in rows:
        proc = _processor_for_row(registry, row)
        slug = proc.profile.slug
        if slug == registry.default.profile.slug:
            # Unprofiled channels are keyed per channel id so they don't supersede each other.
            slug = f"{slug}:{row.get('channel_id') or row['url']}"
        if slug in newest:
            continue
        newest[slug] = (proc, row)

    for proc, row in newest.values():
        video = video_from_row(row)
        slug = proc.profile.slug
        latest = repo.latest_published(slug)
        if repo.exists(slug, video.video_id) or (latest and _parse(latest) >= video.published_at):
            run.skipped += 1
            continue
        try:
            result = proc.process(
                video,
                llm,
                universe=uni.mention_universe(video.transcript),
                price_lookup=price_lookup,
            )
        except (ScalpLLMError, BriefParseError) as exc:
            run.failed += 1
            log.warning("channel.brief.failed", channel=slug, video=video.video_id, error=str(exc))
            continue
        status = repo.store(result, proc, now=now, raw_doc_id=video.raw_doc_id)
        run.processed += 1
        run.stored[status] = run.stored.get(status, 0) + 1
        run.results.append(result)
        run.candidates.extend(
            brief_to_candidates(result.brief, settings, registry=registry, universe=uni.seed)
        )
    return run


# ---------------------------------------------------------------------------
# Dry-run fixtures
# ---------------------------------------------------------------------------


def load_channel_fixture(conn: sqlite3.Connection, processor: ChannelProcessor) -> str | None:
    """Seed ``raw_docs`` with the channel's fixture transcript. Returns the row id."""
    from arc.ingest.store import RawDocRepo

    fx = processor.fixtures_dir
    if fx is None:
        return None
    meta = json.loads((fx / "video.json").read_text())
    transcript = (fx / "transcript.txt").read_text()
    name = processor.profile.display_name
    return RawDocRepo(conn).insert(
        source="youtube",
        url=meta["video_url"],
        published_at=meta["published_at"],
        text=f"[{name}] [{meta['title']}] {transcript}",
        tickers_hint=[],
        channel_id=processor.profile.channel_id,
        title=meta["title"],
    )


def fixture_llm(processor: ChannelProcessor) -> FixtureScalpLLM:
    fx = processor.fixtures_dir
    path = fx / "llm_reply.json" if fx else Path("/nonexistent")
    return FixtureScalpLLM([path.read_text()] if path.exists() else [])
