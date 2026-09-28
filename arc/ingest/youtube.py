"""YouTube transcript connector.

Downloads captions from configured YouTube channels using ``yt-dlp``.
Yields one ``RawDoc`` per video with the transcript text.

Incremental: persists the latest video upload date per channel as
the cursor.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import sqlite3

import structlog

from arc.ingest.store import IngestCursorRepo, RawDocRepo, content_hash
from arc.models import RawDoc

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


_CAPTION_LANGS = ("en", "en-US", "en-orig")


def _pick_caption_url(info: dict) -> str:
    """Return a VTT caption URL: manual English subs first, then auto-captions.

    Most finance channels (e.g. StockedUp) publish auto-generated captions only,
    so falling back to ``automatic_captions`` is required to get any text.
    """
    for key in ("subtitles", "automatic_captions"):
        tracks = info.get(key) or {}
        if not isinstance(tracks, dict):
            continue
        for lang in _CAPTION_LANGS:
            for entry in tracks.get(lang) or []:
                if entry.get("ext") == "vtt" and entry.get("url"):
                    return str(entry["url"])
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
                if re.match(r"^\d+$", line):
                    continue
                # Strip HTML tags
                line = re.sub(r"<[^>]+>", "", line)
                if line and line not in text_lines[-1:]:
                    text_lines.append(line)
            return " ".join(text_lines)[:50_000]
    except Exception:  # noqa: BLE001
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


def fetch_youtube(
    conn: sqlite3.Connection,
    settings: ArcSettings,
) -> list[RawDoc]:
    """Fetch transcripts from configured YouTube channels.

    Returns only newly stored documents. A video whose captions are not yet
    available (YouTube generates auto-captions some time after upload) is not
    stored, so it is retried on the next run instead of being deduped forever.
    """
    cursor_repo = IngestCursorRepo(conn)
    doc_repo = RawDocRepo(conn)
    channels = settings.ingest_youtube_channels

    if not channels:
        log.warning("youtube.no_channels_configured")
        return []

    results: list[RawDoc] = []

    for channel_url in channels:
        cursor_key = f"{CONNECTOR}:{channel_url}"
        last_date = cursor_repo.get(cursor_key) or ""

        log.info("youtube.fetching", channel=channel_url, cursor=last_date)
        videos = _get_recent_videos(channel_url, max_videos=5)

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

            transcript = _get_transcript(info)
            if not transcript:
                log.info("youtube.no_transcript_yet", url=video_url)
                continue

            title = info.get("title") or video.get("title", "")
            channel = info.get("channel") or info.get("uploader") or ""
            text = f"[{channel}] [{title}] {transcript}" if channel else f"[{title}] {transcript}"
            tickers = _extract_tickers(text, settings.universe)

            doc = RawDoc(
                source=CONNECTOR,
                url=video_url,
                published_at=_published_at(info, listed_date),
                text=text,
                tickers_hint=tickers,
                content_hash=h,
            )

            doc_id = doc_repo.insert(
                source=doc.source,
                url=doc.url,
                published_at=doc.published_at.isoformat(),
                text=doc.text,
                tickers_hint=doc.tickers_hint,
                hash_val=h,
            )

            if doc_id is not None:
                results.append(doc)

            if upload_date > newest_date:
                newest_date = upload_date

        if newest_date > last_date:
            cursor_repo.set(cursor_key, newest_date)

    log.info("youtube.done", new_docs=len(results))
    return results
