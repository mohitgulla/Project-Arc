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


def _get_transcript(video_url: str) -> str:
    """Use yt-dlp to download auto/manual captions as text."""
    cmd = [
        sys.executable,
        "-m",
        "yt_dlp",
        "--write-auto-subs",
        "--write-subs",
        "--sub-lang",
        "en",
        "--sub-format",
        "vtt",
        "--skip-download",
        "--print",
        "%(subtitles)j",
        "--no-warnings",
        video_url,
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            # Fallback: try getting description as text
            return ""

        # Parse VTT subtitle data from stdout
        output = result.stdout.strip()
        if not output or output == "null" or output == "NA":
            return ""

        # yt-dlp --print %(subtitles)j returns JSON with subtitle data
        try:
            subs_data = json.loads(output)
            if isinstance(subs_data, dict):
                for _lang, entries in subs_data.items():
                    if isinstance(entries, list):
                        for entry in entries:
                            if entry.get("ext") in ("vtt", "srv1", "srv2", "srv3", "json3"):
                                # The URL to the subtitle file
                                sub_url = entry.get("url", "")
                                if sub_url:
                                    return _download_subtitle(sub_url)
        except json.JSONDecodeError:
            pass

        return ""
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return ""


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


def fetch_youtube(
    conn: sqlite3.Connection,
    settings: ArcSettings,
) -> list[RawDoc]:
    """Fetch transcripts from configured YouTube channels.

    Returns only newly stored documents.
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
        last_cursor = cursor_repo.get(cursor_key)
        last_date = last_cursor or ""

        log.info("youtube.fetching", channel=channel_url, cursor=last_cursor)
        videos = _get_recent_videos(channel_url, max_videos=5)

        newest_date = last_date

        for video in videos:
            video_id = video.get("id", "")
            upload_date = video.get("upload_date", "")  # YYYYMMDD
            title = video.get("title", "")

            if not video_id:
                continue

            # Skip if older than cursor
            if upload_date and upload_date <= last_date:
                continue

            video_url = f"https://www.youtube.com/watch?v={video_id}"
            h = content_hash(CONNECTOR, video_url)

            # Check dedupe before expensive transcript fetch
            if doc_repo.exists(h):
                continue

            transcript = _get_transcript(video_url)
            text = f"[{title}] {transcript}" if transcript else f"[{title}] (no transcript)"

            tickers = _extract_tickers(text, settings.universe)

            try:
                pub_dt = datetime.strptime(upload_date, "%Y%m%d").replace(tzinfo=UTC)
            except (ValueError, TypeError):
                pub_dt = datetime.now(UTC)

            doc = RawDoc(
                source=CONNECTOR,
                url=video_url,
                published_at=pub_dt,
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
