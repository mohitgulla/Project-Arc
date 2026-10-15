"""Which session's option volume the scanner reads, and the threshold it faces (E2.5, D116).

Alpaca's raw option snapshot carries ``dailyBar`` (the latest session with a print)
and ``prevDailyBar``. Before D116 the adapter read ``dailyBar or prevDailyBar``, so a
contract that had not traded today was credited with yesterday's volume. Now:

- **From 10:00 ET on a session day** the volume is today's daily bar only (a bar dated
  any other day counts as 0), labelled ``today``. The filter's minimum is pro-rated:
  ``ceil(min_volume x elapsed_fraction)``, floor 1, where the elapsed fraction is the
  time since the open over half the regular session (early closes from
  :mod:`arc.utils.calendar`), so the full minimum applies from midday on.
- **Before 10:00 ET** (and pre-market), and on a non-session day, the volume is the
  previous session's full-day bar against the full minimum, labelled
  ``prev_session``; otherwise the first loops of the day would find nothing liquid.

Pure functions of an injected ``now``; nothing here reads the wall clock.
"""

from __future__ import annotations

import datetime as _dt
import math
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from arc.utils.calendar import ET, is_session, previous_session, session_close, session_open

__all__ = [
    "TODAY_VOLUME_FROM",
    "VolumeBasis",
    "VolumeSource",
    "bar_date",
    "pick_volume",
    "today_min_volume",
    "volume_basis",
]

VolumeSource = Literal["today", "prev_session"]

#: Before this ET time a session day reads the previous session's full-day volume.
TODAY_VOLUME_FROM = _dt.time(10, 0)


class VolumeBasis(BaseModel):
    """The session whose volume counts at a given ``now``."""

    model_config = ConfigDict(frozen=True)

    source: VolumeSource
    session: _dt.date
    #: elapsed part of the ramp to midday, in [0, 1]; 1.0 for ``prev_session``
    fraction: float


def volume_basis(now: _dt.datetime) -> VolumeBasis:
    """The volume session and pro-rating fraction for a read at *now* (module doc)."""
    at = now.astimezone(ET) if now.tzinfo is not None else now.replace(tzinfo=ET)
    day = at.date()
    if not is_session(day):
        return VolumeBasis(source="prev_session", session=previous_session(day), fraction=1.0)
    if at.time() < TODAY_VOLUME_FROM:
        return VolumeBasis(source="prev_session", session=previous_session(day), fraction=1.0)
    opened, closed = session_open(day), session_close(day)
    half = (closed - opened) / 2
    fraction = min(1.0, max(0.0, (at - opened) / half))
    return VolumeBasis(source="today", session=day, fraction=fraction)


def today_min_volume(min_volume: int, fraction: float) -> int:
    """``ceil(min_volume x fraction)``, at least 1 (0 stays 0: the rule is off)."""
    if min_volume <= 0:
        return 0
    return max(1, math.ceil(min_volume * min(1.0, max(0.0, fraction)) - 1e-9))


def bar_date(bar: dict[str, Any]) -> _dt.date | None:
    """ET date of a raw Alpaca daily bar (``t`` is ``YYYY-MM-DDT04:00:00Z``), or None."""
    raw = bar.get("t")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        stamp = _dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        return stamp.date()
    return stamp.astimezone(ET).date()


def pick_volume(snapshot: dict[str, Any], basis: VolumeBasis) -> int:
    """Volume of the daily bar dated ``basis.session`` in a raw snapshot, else 0.

    ``today``: only ``dailyBar`` counts (``prevDailyBar`` is never today's).
    ``prev_session``: whichever of ``dailyBar`` / ``prevDailyBar`` is dated the
    previous session (before today's first print ``dailyBar`` still is).
    """
    keys = ("dailyBar",) if basis.source == "today" else ("dailyBar", "prevDailyBar")
    for key in keys:
        bar = snapshot.get(key)
        if isinstance(bar, dict) and bar.get("v") is not None and bar_date(bar) == basis.session:
            return int(bar["v"])
    return 0
