"""YouTube transcript connector.

Downloads captions from configured YouTube channels using ``yt-dlp``.
Yields one ``RawDoc`` per video with the transcript text. When a video has
no captions, falls back to local audio transcription (E4.1b, D15; see
:mod:`arc.ingest.transcribe`). The text is prefixed with
``[transcript:captions]`` or ``[transcript:audio]``.

Incremental: persists the latest video upload date per channel as
the cursor.
"""

from __future__ import annotations

import json
import random
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable

import structlog

from arc.ingest.caption_backoff import (
    CaptionBackoff,
    CaptionResult,
    CaptionStatus,
    is_sorry_page,
    load_backoff,
    parse_retry_after,
    register_rate_limit,
    save_backoff,
)
from arc.ingest.store import IngestCursorRepo, RawDocRepo, content_hash
from arc.ingest.transcribe import (
    MlxWhisperTranscriber,
    Transcriber,
    TranscriptionError,
    resolve_ffmpeg,
    transcribe_video_audio,
)
from arc.models import RawDoc, TranscriptSource
from arc.universe.ingest import IngestUniverse
from arc.utils.calendar import ET

if TYPE_CHECKING:
    from arc.config import ArcSettings

log = structlog.get_logger()

CONNECTOR = "youtube"


class YoutubeListError(RuntimeError):
    """The channel listing failed (yt-dlp missing, timed out or exited non-zero)."""


def list_channel_videos(channel_url: str, *, max_videos: int = 5) -> list[dict]:
    """Flat listing of a channel's newest *max_videos* uploads, newest first.

    Unlike :func:`_get_recent_videos` a failure raises :class:`YoutubeListError`,
    so a caller can tell "the channel posted nothing" from "we could not look"
    (E4.6: the first is no info, the second alerts).
    """
    cmd = [
        sys.executable,
        "-m",
        "yt_dlp",
        "--flat-playlist",
        "--dump-json",
        "--playlist-end",
        str(max_videos),
        "--no-warnings",
        channel_url,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        msg = f"yt-dlp unavailable: {type(exc).__name__}"
        raise YoutubeListError(msg) from exc
    if result.returncode != 0:
        msg = f"yt-dlp exit {result.returncode}: {result.stderr.strip()[:200]}"
        raise YoutubeListError(msg)
    videos: list[dict] = []
    for line in result.stdout.strip().splitlines():
        if line.strip():
            try:
                videos.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return videos


def _get_recent_videos(
    channel_url: str,
    *,
    max_videos: int = 5,
) -> list[dict]:
    """Use yt-dlp to list recent videos from a channel/playlist ([] on failure)."""
    try:
        return list_channel_videos(channel_url, max_videos=max_videos)
    except YoutubeListError as exc:
        log.warning("youtube.list_failed", channel=channel_url, error=str(exc))
        return []


def _get_video_info(video_url: str) -> dict:
    """Fetch full metadata for one video (upload time, caption tracks) via yt-dlp."""
    cmd = [
        sys.executable,
        "-m",
        "yt_dlp",
        "--dump-single-json",
        "--skip-download",
        "--no-warnings",
        video_url,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        log.warning("youtube.info_unavailable", url=video_url)
        return {}
    if result.returncode != 0:
        log.warning("youtube.info_failed", url=video_url, stderr=result.stderr[:200])
        return {}
    try:
        info = json.loads(result.stdout)
    except json.JSONDecodeError:
        return {}
    return info if isinstance(info, dict) else {}


# ``en-orig`` is the original-language ASR track. The ``en`` auto-caption entry
# is a machine-translation URL (``tlang=en``) that YouTube rate-limits with 429,
# so the original track is tried first.
_CAPTION_LANGS = ("en-orig", "en", "en-US")


MAX_CAPTION_CHARS = 50_000  # caption text cap (a ~45-50 min video at speaking pace)


def _pick_caption_url(info: dict) -> str:
    """Return a VTT caption URL: manual English subs first, then auto-captions.

    Most finance channels (e.g. StockedUp) publish auto-generated captions only,
    so falling back to ``automatic_captions`` is required to get any text.
    Untranslated tracks win over ``tlang=`` (translated) ones.
    """
    for key in ("subtitles", "automatic_captions"):
        tracks = info.get(key) or {}
        if not isinstance(tracks, dict):
            continue
        urls = [
            str(entry["url"])
            for lang in _CAPTION_LANGS
            for entry in tracks.get(lang) or []
            if entry.get("ext") == "vtt" and entry.get("url")
        ]
        if urls:
            return next((u for u in urls if "tlang=" not in u), urls[0])
    return ""


def _clean_vtt(raw: str) -> str:
    """Strip VTT header, cue timings, numbering and inline tags; join the text.

    Capped at :data:`MAX_CAPTION_CHARS`; the daily brief job reports a capped
    transcript as ``transcript_truncated`` (E4.6).
    """
    text_lines: list[str] = []
    for line in raw.splitlines():
        line = line.strip()
        # Skip VTT header, timestamps, notes
        if not line or line.startswith("WEBVTT") or "-->" in line:
            continue
        if line.startswith(("Kind:", "Language:", "NOTE")):
            continue
        if re.match(r"^\d+$", line):
            continue
        # Strip HTML tags
        line = re.sub(r"<[^>]+>", "", line)
        if line and line not in text_lines[-1:]:
            text_lines.append(line)
    return " ".join(text_lines)[:MAX_CAPTION_CHARS]


def _download_subtitle(url: str) -> CaptionResult:
    """Download a caption track and classify the outcome (E4.1c).

    ``rate_limited``: HTTP 429, or Google's "Sorry" page on any status.
    ``empty``: 200 with no cues (PO-token gated URLs look like this).
    ``error``: anything else. Logging is left to the caller, which knows the video.
    """
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            status = resp.status if isinstance(getattr(resp, "status", None), int) else 200
            final_url = resp.geturl() if callable(getattr(resp, "geturl", None)) else url
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read().decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            body = ""
        retry_after = parse_retry_after(
            exc.headers.get("Retry-After") if exc.headers else None, now=datetime.now(UTC)
        )
        if exc.code == 429 or is_sorry_page(body, str(exc.url or "")):
            return CaptionResult(
                CaptionStatus.RATE_LIMITED, http_status=exc.code, retry_after_s=retry_after
            )
        return CaptionResult(CaptionStatus.ERROR, http_status=exc.code, error=str(exc)[:200])
    except Exception as exc:  # noqa: BLE001
        return CaptionResult(CaptionStatus.ERROR, error=str(exc)[:200])

    if is_sorry_page(raw, str(final_url)):
        return CaptionResult(CaptionStatus.RATE_LIMITED, http_status=status)
    text = _clean_vtt(raw)
    if not text:
        return CaptionResult(CaptionStatus.EMPTY, http_status=status)
    return CaptionResult(CaptionStatus.OK, text=text, http_status=status)


def _extract_tickers(text: str, universe: list[str] | IngestUniverse) -> list[str]:
    """Ticker hints: seed list + (D28 seed mode) master-validated cashtags/symbols."""
    import re

    if not isinstance(universe, list):
        return universe.tickers_in(text)
    upper = text.upper()
    found = []
    for t in universe:
        # Match ticker as a whole word (with optional $ prefix)
        pattern = rf"(?:^|[\s\[\($])({re.escape(t)})(?:[\s\]\).,;:!?]|$)"
        if re.search(pattern, upper):
            found.append(t)
    return found


def _published_at(info: dict, fallback_date: str) -> datetime:
    """When the video went public: ``release_timestamp`` first, then ``timestamp``.

    D60: for a finished live stream ``timestamp`` is when the stream was *scheduled*
    (IBD's 2026-10-07 show: 10-06 14:28), ``release_timestamp`` when it aired (10-07
    17:01). The ``youtube.briefs`` picker uses the same order, so a brief's age and
    expiry match the video it picked.
    """
    for key in ("release_timestamp", "timestamp"):
        ts = info.get(key)
        if isinstance(ts, int | float):
            return datetime.fromtimestamp(ts, tz=UTC)
    date = str(info.get("upload_date") or fallback_date or "")
    try:
        return datetime.strptime(date, "%Y%m%d").replace(tzinfo=UTC)
    except ValueError:
        return datetime.now(UTC)


# ---------------------------------------------------------------------------
# Audio-transcription fallback (E4.1b, D15)
# ---------------------------------------------------------------------------

TRANSCRIPT_PREFIX = {
    TranscriptSource.CAPTIONS: "[transcript:captions]",
    TranscriptSource.AUDIO: "[transcript:audio]",
}


def transcript_source_of(text: str) -> TranscriptSource | None:
    """Recover how a stored YouTube doc was transcribed from its text prefix."""
    for src, prefix in TRANSCRIPT_PREFIX.items():
        if text.startswith(prefix):
            return src
    return None


def _video_age_minutes(info: dict, now: datetime) -> float | None:
    """Minutes since upload, or None if the upload time is unknown.

    Only an exact timestamp counts; a date-only ``upload_date`` can't tell a
    video uploaded five minutes ago from one uploaded this morning.
    """
    for key in ("release_timestamp", "timestamp"):
        ts = info.get(key)
        if isinstance(ts, int | float):
            return (now - datetime.fromtimestamp(ts, tz=UTC)).total_seconds() / 60
    return None


@dataclass
class YoutubeRunStats:
    """Per-run outcome of :func:`fetch_youtube` (captions, audio fallback, cooldown).

    Filled in place when the caller passes one; the routine handler turns it
    into the run summary so each scheduled run shows its YouTube outcome (E5.3).
    """

    captions: dict[str, int] = field(default_factory=dict)  # CaptionStatus value -> count
    captions_skipped: int = 0
    skip_reason: str | None = None  # "cooldown" | "breaker"
    audio: int = 0
    audio_failed: int = 0
    audio_wall_s: float = 0.0
    no_transcript: int = 0
    cooldown_until: datetime | None = None
    consecutive_rate_limits: int = 0

    def count(self, status: CaptionStatus) -> None:
        self.captions[status.value] = self.captions.get(status.value, 0) + 1

    def summary(self) -> str:
        caps = ", ".join(f"{s.value} {self.captions.get(s.value, 0)}" for s in CaptionStatus)
        text = f"captions: {caps}"
        if self.captions_skipped:
            text += f", skipped {self.captions_skipped} ({self.skip_reason})"
        text += f" · audio {self.audio} ({self.audio_wall_s:.0f}s wall"
        text += f", {self.audio_failed} failed)" if self.audio_failed else ")"
        if self.no_transcript:
            text += f" · {self.no_transcript} without transcript yet"
        if self.cooldown_until is not None:
            until = self.cooldown_until.astimezone(ET)
            text += (
                f" · captions cooldown until {until:%a %H:%M} ET"
                f" (streak {self.consecutive_rate_limits})"
            )
        else:
            text += " · captions cooldown: none"
        return text


@dataclass
class _AudioBudget:
    """Per-run guard rails for audio transcription."""

    remaining: int
    grace_minutes: int
    max_minutes: int
    force: bool
    now: datetime

    def skip_reason(self, info: dict) -> str | None:
        """Why this video must not be audio-transcribed now, or None if it may."""
        if info.get("is_live") or info.get("live_status") in ("is_live", "is_upcoming"):
            return "live_or_upcoming"
        duration = info.get("duration")
        if not isinstance(duration, int | float) or duration <= 0:
            return "unknown_duration"
        if duration > self.max_minutes * 60:
            return "too_long"
        if not self.force:
            age = _video_age_minutes(info, self.now)
            if age is None or age < self.grace_minutes:
                return "within_caption_grace"
        if self.remaining <= 0:
            return "run_cap_reached"
        return None


def _audio_transcript(
    video_url: str,
    info: dict,
    budget: _AudioBudget,
    transcriber: Transcriber,
    ffmpeg_bin: str,
    stats: YoutubeRunStats,
) -> str:
    reason = budget.skip_reason(info)
    if reason is not None:
        log.info(
            "youtube.audio_skipped",
            url=video_url,
            reason=reason,
            duration_s=info.get("duration"),
            max_minutes=budget.max_minutes,
        )
        return ""
    budget.remaining -= 1
    started = time.monotonic()
    try:
        text = transcribe_video_audio(video_url, transcriber, ffmpeg=resolve_ffmpeg(ffmpeg_bin))
    except TranscriptionError as exc:
        stats.audio_failed += 1
        stats.audio_wall_s += time.monotonic() - started
        log.warning("youtube.audio_failed", url=video_url, error=str(exc)[:300])
        return ""
    stats.audio += 1
    stats.audio_wall_s += time.monotonic() - started
    log.info(
        "youtube.audio_transcribed",
        url=video_url,
        backend=transcriber.name,
        duration_s=info.get("duration"),
        wall_s=round(time.monotonic() - started, 1),
        chars=len(text),
    )
    return text


@dataclass
class _CaptionGuard:
    """Per-run caption pacing, circuit breaker and cross-run cooldown (E4.1c).

    ``blocked`` is set when a persisted cooldown is active at the start of the
    run, or by the first ``rate_limited`` result (the breaker). While blocked,
    no timedtext request is sent; videos go to the audio fallback.
    """

    conn: sqlite3.Connection
    settings: ArcSettings
    rng: random.Random
    sleep: Callable[[float], None]
    now: datetime
    state: CaptionBackoff
    stats: YoutubeRunStats = field(default_factory=lambda: YoutubeRunStats())
    blocked: str | None = None  # None | "cooldown" | "breaker"
    requests: int = 0

    @classmethod
    def start(
        cls,
        conn: sqlite3.Connection,
        settings: ArcSettings,
        *,
        rng: random.Random,
        sleep: Callable[[float], None],
        now: datetime,
        stats: YoutubeRunStats,
    ) -> _CaptionGuard:
        guard = cls(conn, settings, rng, sleep, now, load_backoff(conn), stats)
        if guard.state.active(now):
            guard.blocked = "cooldown"
            log.warning(
                "youtube.captions_cooldown_active",
                until=_iso(guard.state.cooldown_until),
                consecutive_rate_limits=guard.state.consecutive_rate_limits,
            )
        return guard

    def transcript(self, info: dict, video_id: str) -> str:
        """Caption text for one video, or "" (then the audio fallback may run)."""
        url = _pick_caption_url(info)
        if not url:
            return ""
        if self.blocked:
            self.stats.captions_skipped += 1
            self.stats.skip_reason = self.blocked
            log.info(
                "youtube.captions_skipped",
                video_id=video_id,
                reason=self.blocked,
                until=_iso(self.state.cooldown_until),
            )
            return ""
        if self.requests:
            self.sleep(self.settings.yt_caption_sleep_seconds)
        self.requests += 1
        res = _download_subtitle(url)
        self.stats.count(res.status)
        if res.status is CaptionStatus.OK:
            if self.state.consecutive_rate_limits or self.state.cooldown_until:
                log.info(
                    "youtube.captions_backoff_reset",
                    previous_consecutive=self.state.consecutive_rate_limits,
                )
                self.state = CaptionBackoff()
                save_backoff(self.conn, self.state)
            return res.text
        if res.status is CaptionStatus.RATE_LIMITED:
            self._trip(res, video_id)
        elif res.status is CaptionStatus.EMPTY:
            log.warning("youtube.captions_empty", video_id=video_id, status=res.http_status)
        else:
            log.warning(
                "youtube.caption_download_failed",
                video_id=video_id,
                status=res.http_status,
                error=res.error,
            )
        return ""

    def _trip(self, res: CaptionResult, video_id: str) -> None:
        self.state, minutes = register_rate_limit(
            self.state, self.settings, self.rng, now=self.now, retry_after_s=res.retry_after_s
        )
        save_backoff(self.conn, self.state)
        self.blocked = "breaker"
        log.warning(
            "youtube.captions_rate_limited",
            video_id=video_id,
            status=res.http_status,
            retry_after=res.retry_after_s is not None,
            retry_after_s=res.retry_after_s,
            consecutive_rate_limits=self.state.consecutive_rate_limits,
            cooldown_minutes=round(minutes, 1),
            until=_iso(self.state.cooldown_until),
        )


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


# ---------------------------------------------------------------------------
# E4.6 (D45): one video at a time for the daily brief job
# ---------------------------------------------------------------------------


@dataclass
class TranscriptSession:
    """Caption pacing/breaker + audio budget shared by every channel of one run.

    The daily ``youtube.briefs`` job fetches channels one after another under one
    session, so a 429 on one channel sends the remaining channels to the audio
    fallback (one ``youtube:captions_backoff`` cooldown, one per-run breaker), and
    ``yt_max_audio_per_slot`` caps audio transcriptions across the whole run.
    """

    conn: sqlite3.Connection
    settings: ArcSettings
    now: datetime
    stats: YoutubeRunStats
    captions: _CaptionGuard
    budget: _AudioBudget
    transcriber: Transcriber

    @classmethod
    def start(
        cls,
        conn: sqlite3.Connection,
        settings: ArcSettings,
        *,
        now: datetime,
        transcriber: Transcriber | None = None,
        rng: random.Random | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> TranscriptSession:
        stats = YoutubeRunStats()
        guard = _CaptionGuard.start(
            conn,
            settings,
            rng=rng or random.Random(),  # noqa: S311 - jitter, not crypto
            sleep=sleep or time.sleep,
            now=now,
            stats=stats,
        )
        budget = _AudioBudget(
            remaining=settings.yt_max_audio_per_slot,
            grace_minutes=settings.yt_caption_grace_minutes,
            max_minutes=settings.yt_max_audio_minutes,
            force=False,
            now=now,
        )
        stt = transcriber or MlxWhisperTranscriber(model=settings.whisper_model)
        return cls(conn, settings, now, stats, guard, budget, stt)

    def transcript(
        self, info: dict, video_id: str, *, max_audio_minutes: int | None = None
    ) -> tuple[str, TranscriptSource | None, str | None]:
        """``(text, source, pending_reason)`` for one video.

        Captions first, then audio under the grace / length / per-run caps. When
        neither yields text, *pending_reason* says why (``within_caption_grace``,
        ``too_long``, ``run_cap_reached``, ``audio_failed``, ...).
        """
        text = self.captions.transcript(info, video_id)
        if text:
            return text, TranscriptSource.CAPTIONS, None
        self.budget.max_minutes = max_audio_minutes or self.settings.yt_max_audio_minutes
        reason = self.budget.skip_reason(info)
        failed_before = self.stats.audio_failed
        url = f"https://www.youtube.com/watch?v={video_id}"
        text = _audio_transcript(
            url, info, self.budget, self.transcriber, self.settings.ffmpeg_bin, self.stats
        )
        if text:
            return text, TranscriptSource.AUDIO, None
        self.stats.no_transcript += 1
        if reason is None:
            reason = "audio_failed" if self.stats.audio_failed > failed_before else "no_transcript"
        return "", None, reason

    def finish(self) -> YoutubeRunStats:
        backoff = self.captions.state
        if backoff.active(self.now):
            self.stats.cooldown_until = backoff.cooldown_until
        self.stats.consecutive_rate_limits = backoff.consecutive_rate_limits
        return self.stats


def store_transcript(
    conn: sqlite3.Connection,
    *,
    info: dict,
    video_id: str,
    transcript: str,
    source: TranscriptSource,
    source_key: str,
    universe: IngestUniverse | list[str],
) -> tuple[str, RawDoc]:
    """Store one video transcript as a ``raw_doc`` (same text shape as :func:`fetch_youtube`).

    Returns ``(raw_doc id, doc)``; an already-stored video returns the existing id.
    """
    video_url = f"https://www.youtube.com/watch?v={video_id}"
    h = content_hash(CONNECTOR, video_url)
    title = info.get("title") or ""
    channel = info.get("channel") or info.get("uploader") or ""
    header = f"[{channel}] [{title}]" if channel else f"[{title}]"
    text = f"{TRANSCRIPT_PREFIX[source]} {header} {transcript}"
    channel_id = info.get("channel_id") or None
    doc = RawDoc(
        source=CONNECTOR,
        url=video_url,
        published_at=_published_at(info, ""),
        text=text,
        tickers_hint=_extract_tickers(text, universe),
        content_hash=h,
        transcript_source=source,
        channel_id=channel_id,
        title=title,
    )
    repo = RawDocRepo(conn)
    doc_id = repo.insert(
        source=doc.source,
        url=doc.url,
        published_at=doc.published_at.isoformat(),
        text=doc.text,
        tickers_hint=doc.tickers_hint,
        hash_val=h,
        channel_id=channel_id,
        title=title,
        source_key=source_key,
    )
    if doc_id is None:
        row = conn.execute("SELECT id FROM raw_docs WHERE content_hash = ?", (h,)).fetchone()
        doc_id = str(row["id"])
    return doc_id, doc


def fetch_youtube(
    conn: sqlite3.Connection,
    settings: ArcSettings,
    *,
    force_audio: bool = False,
    transcriber: Transcriber | None = None,
    max_videos: int = 5,
    now: datetime | None = None,
    rng: random.Random | None = None,
    sleep: Callable[[float], None] | None = None,
    stats: YoutubeRunStats | None = None,
) -> list[RawDoc]:
    """Fetch transcripts from configured YouTube channels.

    Transcript order: manual subs → auto-captions → local audio transcription.
    Audio is used only for videos older than ``yt_caption_grace_minutes`` (so we
    don't transcribe what YouTube is about to caption), no longer than
    ``yt_max_audio_minutes``, and at most ``yt_max_audio_per_run`` times per run.
    ``force_audio`` skips captions entirely and ignores the grace period (the
    length and per-run caps still apply).

    Caption requests are paced (``yt_caption_sleep_seconds``). The first HTTP 429
    stops caption requests for the rest of the run and starts a DB-persisted,
    exponentially growing cooldown that later runs honour (E4.1c). ``rng`` and
    ``sleep`` are injectable for tests. Pass *stats* to receive the run's caption /
    audio / cooldown outcome (the routine handler puts it in the run summary).

    Returns only newly stored documents. A video with no transcript is not
    stored, so it is retried on the next run instead of being deduped forever.
    """
    cursor_repo = IngestCursorRepo(conn)
    doc_repo = RawDocRepo(conn)
    channels = settings.ingest_youtube_channels

    if not channels:
        log.warning("youtube.no_channels_configured")
        return []

    run_now = now or datetime.now(UTC)
    stats = stats if stats is not None else YoutubeRunStats()
    budget = _AudioBudget(
        remaining=settings.yt_max_audio_per_run,
        grace_minutes=settings.yt_caption_grace_minutes,
        max_minutes=settings.yt_max_audio_minutes,
        force=force_audio,
        now=run_now,
    )
    stt: Transcriber = transcriber or MlxWhisperTranscriber(model=settings.whisper_model)
    captions = (
        None
        if force_audio
        else _CaptionGuard.start(
            conn,
            settings,
            rng=rng or random.Random(),  # noqa: S311 - jitter, not crypto
            sleep=sleep or time.sleep,
            now=run_now,
            stats=stats,
        )
    )
    results: list[RawDoc] = []
    uni = IngestUniverse.from_settings(settings, now=now, conn=conn)

    for channel_url in channels:
        cursor_key = f"{CONNECTOR}:{channel_url}"
        last_date = cursor_repo.get(cursor_key) or ""

        log.info("youtube.fetching", channel=channel_url, cursor=last_date)
        videos = _get_recent_videos(channel_url, max_videos=max_videos)

        newest_date = last_date

        for video in videos:
            video_id = video.get("id", "")
            if not video_id:
                continue

            # Flat listings usually omit upload_date; when present, honour the cursor.
            listed_date = str(video.get("upload_date") or "")
            if listed_date and listed_date < last_date:
                continue

            video_url = f"https://www.youtube.com/watch?v={video_id}"
            h = content_hash(CONNECTOR, video_url)
            if doc_repo.exists(h):
                continue

            info = _get_video_info(video_url)
            upload_date = str(info.get("upload_date") or listed_date)
            if upload_date and upload_date < last_date:
                continue

            source = TranscriptSource.CAPTIONS
            transcript = "" if captions is None else captions.transcript(info, video_id)
            if not transcript and info:
                transcript = _audio_transcript(
                    video_url, info, budget, stt, settings.ffmpeg_bin, stats
                )
                source = TranscriptSource.AUDIO
            if not transcript:
                stats.no_transcript += 1
                log.info("youtube.no_transcript_yet", url=video_url)
                continue

            title = info.get("title") or video.get("title", "")
            channel = info.get("channel") or info.get("uploader") or ""
            header = f"[{channel}] [{title}]" if channel else f"[{title}]"
            text = f"{TRANSCRIPT_PREFIX[source]} {header} {transcript}"
            tickers = _extract_tickers(text, uni)
            channel_id = info.get("channel_id") or video.get("channel_id") or None

            doc = RawDoc(
                source=CONNECTOR,
                url=video_url,
                published_at=_published_at(info, listed_date),
                text=text,
                tickers_hint=tickers,
                content_hash=h,
                transcript_source=source,
                channel_id=channel_id,
                title=title,
            )

            doc_id = doc_repo.insert(
                source=doc.source,
                url=doc.url,
                published_at=doc.published_at.isoformat(),
                text=doc.text,
                tickers_hint=doc.tickers_hint,
                hash_val=h,
                channel_id=channel_id,
                title=title,
            )

            if doc_id is not None:
                results.append(doc)

            if upload_date > newest_date:
                newest_date = upload_date

        if newest_date > last_date:
            cursor_repo.set(cursor_key, newest_date)

    backoff = captions.state if captions is not None else load_backoff(conn)
    if backoff.active(run_now):
        stats.cooldown_until = backoff.cooldown_until
    stats.consecutive_rate_limits = backoff.consecutive_rate_limits
    log.info("youtube.done", new_docs=len(results), outcome=stats.summary())
    return results
