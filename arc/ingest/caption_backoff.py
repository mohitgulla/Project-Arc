"""Caption rate-limit classification and backoff for the YouTube connector (E4.1c, D15).

YouTube's caption endpoint (``/api/timedtext``) rate-limits this IP after a
handful of requests. It answers HTTP 429 with no ``Retry-After`` and Google's
HTML "Sorry..." page, and the block lasts tens of minutes to hours. The
backoff therefore has to be ours:

* every caption download is classified (:class:`CaptionStatus`);
* the first ``rate_limited`` in a run opens a per-run circuit breaker (no more
  timedtext requests that run);
* a cross-run cooldown with exponential backoff is persisted in the DB
  (``ingest_cursors`` row ``youtube:captions_backoff``) so the next run skips
  captions until it expires; the first successful download resets it.

PO-token-gated caption URLs answer **200 with an empty body**; that is
classified ``empty``, never mistaken for a rate limit or "no captions yet".
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from enum import StrEnum
from typing import TYPE_CHECKING

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from arc.ingest.store import IngestCursorRepo
from arc.utils.calendar import ET

if TYPE_CHECKING:
    import random
    import sqlite3

    from arc.config import ArcSettings

BACKOFF_KEY = "youtube:captions_backoff"


class CaptionStatus(StrEnum):
    """Outcome of one timedtext request."""

    OK = "ok"
    RATE_LIMITED = "rate_limited"  # HTTP 429, or Google's "Sorry" page on any status
    EMPTY = "empty"  # 200 with no cues (e.g. PO-token gated URL)
    ERROR = "error"  # anything else


@dataclass(frozen=True)
class CaptionResult:
    """Classified result of a caption download."""

    status: CaptionStatus
    text: str = ""
    http_status: int | None = None
    retry_after_s: float | None = None
    error: str = ""

    @classmethod
    def ok(cls, text: str) -> CaptionResult:
        return cls(CaptionStatus.OK, text=text, http_status=200)


# ---------------------------------------------------------------------------
# Classification helpers
# ---------------------------------------------------------------------------

_SORRY_MARKERS = ("google.com/sorry", "/sorry/index", "unusual traffic", "<title>sorry")


def is_sorry_page(body: str, url: str = "") -> bool:
    """True for Google's "Sorry..." (IP flagged) interstitial.

    Requires an HTML body (or a redirect to ``/sorry/``) so a caption track
    whose *spoken* text says "unusual traffic" is never misread.
    """
    if "/sorry/" in url:
        return True
    head = body.lstrip()[:2000].lower()
    if head.startswith("webvtt") or not (head.startswith("<") or "<html" in head):
        return False
    low = body[:20_000].lower()
    return any(m in low for m in _SORRY_MARKERS)


def parse_retry_after(value: str | None, *, now: datetime) -> float | None:
    """Seconds from a ``Retry-After`` header (delta-seconds or HTTP-date)."""
    if not value:
        return None
    value = value.strip()
    if re.fullmatch(r"\d+(\.\d+)?", value):
        return float(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        return None
    return max((when - now).total_seconds(), 0.0)


# ---------------------------------------------------------------------------
# Persisted backoff state
# ---------------------------------------------------------------------------


class CaptionBackoff(BaseModel):
    """Cross-run caption cooldown for one connector (stored as JSON)."""

    model_config = ConfigDict(extra="forbid")

    cooldown_until: AwareDatetime | None = None  # ET
    consecutive_rate_limits: int = Field(default=0, ge=0)

    def active(self, now: datetime) -> bool:
        return self.cooldown_until is not None and now < self.cooldown_until


def load_backoff(conn: sqlite3.Connection) -> CaptionBackoff:
    raw = IngestCursorRepo(conn).get(BACKOFF_KEY)
    if not raw:
        return CaptionBackoff()
    return CaptionBackoff.model_validate(json.loads(raw))


def save_backoff(conn: sqlite3.Connection, state: CaptionBackoff) -> None:
    IngestCursorRepo(conn).set(BACKOFF_KEY, state.model_dump_json())


def cooldown_minutes(
    consecutive: int,
    settings: ArcSettings,
    rng: random.Random,
    *,
    retry_after_s: float | None = None,
) -> float:
    """``min(base * 2**(n-1), max)`` ± jitter; never less than ``Retry-After``."""
    n = max(consecutive, 1)
    base = min(
        settings.yt_caption_cooldown_base_minutes * 2 ** (n - 1),
        settings.yt_caption_cooldown_max_minutes,
    )
    j = settings.yt_caption_cooldown_jitter
    minutes = base * (1 + rng.uniform(-j, j))
    if retry_after_s is not None:
        minutes = max(minutes, retry_after_s / 60)
    return minutes


def register_rate_limit(
    state: CaptionBackoff,
    settings: ArcSettings,
    rng: random.Random,
    *,
    now: datetime,
    retry_after_s: float | None = None,
) -> tuple[CaptionBackoff, float]:
    """New state after a rate-limited run, and the cooldown length in minutes."""
    n = state.consecutive_rate_limits + 1
    minutes = cooldown_minutes(n, settings, rng, retry_after_s=retry_after_s)
    until = (now + timedelta(minutes=minutes)).astimezone(ET)
    return CaptionBackoff(cooldown_until=until, consecutive_rate_limits=n), minutes
