"""D54 (E5.12): read pre-rename history written under the old name ``scout``.

The 30-min doc reader was called the Scout until the D54 rename; it is now the
**Sweep**, and ``scout`` names the new slow-feed persona (E5.13). The journal,
``context_entries`` and ``routine_runs`` are append-only, so rows written before the
rename keep ``persona='scout'`` / ``produced_by='scout'`` / ``job='scout'`` and
``reason_code='scout_candidate'``. Every reader that shows history maps them here:

* ``scout`` / ``scout.overnight`` **before** the cutover instant -> the Sweep;
  at or after it ``scout`` is the new Scout persona and is left alone.
* ``scout_candidate`` is always the Sweep (the new Scout uses ``scout_feed_candidate``).

The cutover instant is written once by migration ``022_scout_to_sweep.sql`` into
``routine_state`` under :data:`CUTOVER_KEY`. Pure functions plus one read; no writes.
"""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

from arc.context.ttl import from_db

if TYPE_CHECKING:
    import datetime as _dt

__all__ = [
    "CUTOVER_KEY",
    "LEGACY_REASON_CODES",
    "cutover",
    "job_name",
    "persona_key",
    "persona_label",
    "reason_code",
]

CUTOVER_KEY = "rename:scout_to_sweep"

_OLD = "scout"
_NEW = "sweep"

#: Stored reason codes renamed by D54 (always the Sweep, on either side of the cutover).
LEGACY_REASON_CODES: dict[str, str] = {"scout_candidate": "sweep_candidate"}


def cutover(conn: sqlite3.Connection) -> _dt.datetime | None:
    """The rename instant (ET-aware), or ``None`` on a store without it."""
    try:
        row = conn.execute(
            "SELECT value FROM routine_state WHERE key = ?", (CUTOVER_KEY,)
        ).fetchone()
    except sqlite3.OperationalError:  # no routine_state table (minimal fixture stores)
        return None
    if row is None or not row[0]:
        return None
    return from_db(str(row[0]))


def _is_legacy(at: _dt.datetime | None, cut: _dt.datetime | None) -> bool:
    # No cutover recorded (pre-migration copy) or no timestamp: every ``scout`` row is
    # pre-rename history, since the new Scout cannot exist without the migration.
    return cut is None or at is None or at < cut


def job_name(job: str, at: _dt.datetime | None, cut: _dt.datetime | None) -> str:
    """``scout`` / ``scout.<x>`` before the cutover -> ``sweep`` / ``sweep.<x>``."""
    if (job == _OLD or job.startswith(f"{_OLD}.")) and _is_legacy(at, cut):
        return _NEW + job[len(_OLD) :]
    return job


def persona_key(value: str, at: _dt.datetime | None, cut: _dt.datetime | None) -> str:
    """The current persona key for a stored ``persona`` / ``produced_by`` value."""
    return job_name(value, at, cut)


def persona_label(value: str, at: _dt.datetime | None, cut: _dt.datetime | None = None) -> str:
    """Display label (``Sweep``, ``Director``, ``Sweep (digest)``) for a stored persona.

    ``scout`` rows before *cut* (or with no cutover recorded) read as **Sweep**; after it
    they belong to the new Scout persona and read as **Scout**.
    """
    key = persona_key(value, at, cut)
    head, _, tail = key.partition(".")
    label = head[:1].upper() + head[1:]
    return f"{label} ({tail})" if tail else label


def reason_code(code: str) -> str:
    """Current reason code for a stored one (``scout_candidate`` -> ``sweep_candidate``)."""
    return LEGACY_REASON_CODES.get(code, code)
