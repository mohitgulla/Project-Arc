"""Due-time math for routine jobs. Pure functions over ET wall-clock times.

All slots are computed as ET wall-clock times and converted through
``zoneinfo`` so DST transitions behave like a person reading a clock:

- A daily ``22:00`` slot is 22:00 EDT in summer and 22:00 EST in winter.
- A slot that falls in the spring-forward gap (e.g. ``02:30`` on the second
  Sunday of March) does not exist; it is normalised forward to ``03:30`` EDT.
- A slot in the repeated fall-back hour (``01:30``) runs once, at the first
  occurrence (``fold=0``).
"""

from __future__ import annotations

import datetime as _dt
from typing import TYPE_CHECKING

from arc.routines.config import Days
from arc.utils.calendar import ET, is_session

if TYPE_CHECKING:
    from collections.abc import Iterator

    from arc.routines.config import JobSpec


def day_matches(days: Days, d: _dt.date) -> bool:
    """True if a job with *days* may run on ET date *d*."""
    if days is Days.DAILY:
        return True
    if days is Days.WEEKDAYS:
        return d.weekday() < 5
    return is_session(d)


def wall_clock(d: _dt.date, t: _dt.time) -> _dt.datetime:
    """ET wall-clock ``d t`` as an aware datetime, normalised across DST gaps."""
    naive = _dt.datetime.combine(d, t)
    local = naive.replace(tzinfo=ET, fold=0)
    # Round-trip through UTC: a non-existent (gap) time comes back as the real
    # instant it maps to (e.g. 02:30 -> 03:30 EDT on spring-forward day).
    return local.astimezone(_dt.UTC).astimezone(ET)


def _times_for_day(spec: JobSpec, d: _dt.date) -> list[_dt.time]:
    if spec.schedule:
        return sorted(spec.schedule)
    if spec.every is None:
        return []
    start = spec.window.start if spec.window else _dt.time(0, 0)
    end = spec.window.end if spec.window else None
    step = spec.every
    out: list[_dt.time] = []
    cur = _dt.datetime.combine(d, start)
    day_end = _dt.datetime.combine(d, end) if end else _dt.datetime.combine(d, _dt.time.max)
    while cur <= day_end and cur.date() == d:
        out.append(cur.time())
        cur += step
    return out


def iter_slots(spec: JobSpec, start: _dt.datetime, end: _dt.datetime) -> Iterator[_dt.datetime]:
    """Yield the job's slots in the half-open interval ``(start, end]``, ascending.

    Event-driven jobs (``trigger``) have no slots.
    """
    if spec.trigger or end <= start:
        return
    start_et = start.astimezone(ET)
    end_et = end.astimezone(ET)
    d = start_et.date()
    seen: set[_dt.datetime] = set()
    while d <= end_et.date():
        if day_matches(spec.days, d):
            for t in _times_for_day(spec, d):
                slot = wall_clock(d, t)
                if start < slot <= end and slot not in seen:
                    seen.add(slot)
                    yield slot
        d += _dt.timedelta(days=1)


def slots_between(spec: JobSpec, start: _dt.datetime, end: _dt.datetime) -> list[_dt.datetime]:
    """All slots in ``(start, end]``, ascending."""
    return sorted(iter_slots(spec, start, end))


def next_slot(
    spec: JobSpec, after: _dt.datetime, *, horizon: _dt.timedelta = _dt.timedelta(days=14)
) -> _dt.datetime | None:
    """First slot strictly after *after* (within *horizon*), or ``None``."""
    for slot in slots_between(spec, after, after + horizon):
        return slot
    return None


def default_catchup(spec: JobSpec) -> _dt.timedelta:
    """How late a missed slot may still run when the job sets no ``ttl``.

    ``every`` jobs: one period (the next slot supersedes it anyway).
    ``schedule`` jobs: 2 hours.
    """
    if spec.every is not None:
        return spec.every
    return _dt.timedelta(hours=2)


def catchup_deadline(spec: JobSpec, slot: _dt.datetime) -> _dt.datetime:
    """Latest time at which *slot* may still run (its catch-up / TTL window)."""
    if spec.ttl is not None:
        return spec.ttl.expires_at(slot)
    return slot + default_catchup(spec)
