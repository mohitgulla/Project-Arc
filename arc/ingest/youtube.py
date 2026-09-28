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
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import sqlite3

import structlog

from arc.ingest.store import IngestCursorRepo, RawDocRepo, content_hash
from arc.ingest.transcribe import (
    MlxWhisperTranscriber,
    Transcriber,
    TranscriptionError,
    resolve_ffmpeg,
    transcribe_video_audio,
)
from arc.models import RawDoc, TranscriptSource

if TYPE_CHECKING:
    from arc.config import ArcSettings

log = structlog.get_logger()

CONNECTOR = "youtube"


def _get_recent_videos(
    channel_url: str,
    *,
    max_videos: int = 5,
) -> list[dict]:
    """Use yt-dlp to list recent videos from a channel/playlist."""
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
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0:
            log.warning("youtube.list_failed", channel=channel_url, stderr=result.stderr[:200])
            return []

        videos = []
        for line in result.stdout.strip().splitlines():
            if line.strip():
                try:
                    videos.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return videos
    except (subprocess.TimeoutExpired, FileNotFoundError):
        log.warning("youtube.yt_dlp_unavailable")
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


def _get_transcript(info: dict) -> str:
    """Download and clean the best available English caption track."""
    url = _pick_caption_url(info)
    return _download_subtitle(url) if url else ""


def _download_subtitle(url: str) -> str:
    """Download and clean a subtitle file."""
    import re
    import urllib.request

    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            # Strip VTT timestamps and formatting
            lines = raw.splitlines()
            text_lines = []
            for line in lines:
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
            return " ".join(text_lines)[:50_000]
    except Exception as exc:  # noqa: BLE001
        # e.g. HTTP 429 from YouTube's timedtext endpoint: treated as "no captions",
        # so the audio fallback (E4.1b) can take over after the grace period.
        log.warning("youtube.caption_download_failed", error=str(exc)[:200])
        return ""


def _extract_tickers(text: str, universe: list[str]) -> list[str]:
    """Simple ticker mention extraction against the configured universe."""
    import re

    upper = text.upper()
    found = []
    for t in universe:
        # Match ticker as a whole word (with optional $ prefix)
        pattern = rf"(?:^|[\s\[\($])({re.escape(t)})(?:[\s\]\).,;:!?]|$)"
        if re.search(pattern, upper):
            found.append(t)
    return found


def _published_at(info: dict, fallback_date: str) -> datetime:
    ts = info.get("timestamp")
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
        log.warning("youtube.audio_failed", url=video_url, error=str(exc)[:300])
        return ""
    log.info(
        "youtube.audio_transcribed",
        url=video_url,
        backend=transcriber.name,
        duration_s=info.get("duration"),
        wall_s=round(time.monotonic() - started, 1),
        chars=len(text),
    )
    return text


def fetch_youtube(
    conn: sqlite3.Connection,
    settings: ArcSettings,
    *,
    force_audio: bool = False,
    transcriber: Transcriber | None = None,
    max_videos: int = 5,
    now: datetime | None = None,
) -> list[RawDoc]:
    """Fetch transcripts from configured YouTube channels.

    Transcript order: manual subs → auto-captions → local audio transcription.
    Audio is used only for videos older than ``yt_caption_grace_minutes`` (so we
    don't transcribe what YouTube is about to caption), no longer than
    ``yt_max_audio_minutes``, and at most ``yt_max_audio_per_run`` times per run.
    ``force_audio`` skips captions entirely and ignores the grace period (the
    length and per-run caps still apply).

    Returns only newly stored documents. A video with no transcript is not
    stored, so it is retried on the next run instead of being deduped forever.
    """
    cursor_repo = IngestCursorRepo(conn)
    doc_repo = RawDocRepo(conn)
    channels = settings.ingest_youtube_channels

    if not channels:
        log.warning("youtube.no_channels_configured")
        return []

    budget = _AudioBudget(
        remaining=settings.yt_max_audio_per_run,
        grace_minutes=settings.yt_caption_grace_minutes,
        max_minutes=settings.yt_max_audio_minutes,
        force=force_audio,
        now=now or datetime.now(UTC),
    )
    stt: Transcriber = transcriber or MlxWhisperTranscriber(model=settings.whisper_model)
    results: list[RawDoc] = []

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
            transcript = "" if force_audio else _get_transcript(info)
            if not transcript and info:
                transcript = _audio_transcript(video_url, info, budget, stt, settings.ffmpeg_bin)
                source = TranscriptSource.AUDIO
            if not transcript:
                log.info("youtube.no_transcript_yet", url=video_url)
                continue

            title = info.get("title") or video.get("title", "")
            channel = info.get("channel") or info.get("uploader") or ""
            header = f"[{channel}] [{title}]" if channel else f"[{title}]"
            text = f"{TRANSCRIPT_PREFIX[source]} {header} {transcript}"
            tickers = _extract_tickers(text, settings.universe)
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

    log.info("youtube.done", new_docs=len(results))
    return results
