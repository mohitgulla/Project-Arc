"""The ``correlations`` table (migration 040) and its lookups (E28.4, D131).

:func:`compute_rows` turns one day's close series into pair rows (pure);
:func:`replace_day` stores them; :func:`corr_matrix` is the one read the cluster cap
(E28.5) and the Tower use: the latest stored day ``<= today`` that is at most
:data:`MAX_AGE_SESSIONS` trading sessions old, as a :class:`arc.betas.corr.CorrMatrix`.
Older or missing -> an empty matrix (``day = None``); the caller decides what an
unknown pair means.
"""

from __future__ import annotations

import datetime as _dt
import itertools
import sqlite3
from dataclasses import dataclass
from typing import TYPE_CHECKING

from arc.betas.corr import CorrMatrix, pair_key
from arc.features.correlation import MIN_OBS, WINDOW, return_corr
from arc.utils.calendar import sessions_between

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

#: Same freshness rule as the betas (D62): older stored days are not used.
MAX_AGE_SESSIONS = 5
SOURCE = "alpaca"


@dataclass(frozen=True)
class CorrRow:
    day: _dt.date
    a: str
    b: str
    corr: float
    n_obs: int
    window: int
    as_of: _dt.date
    source: str = SOURCE


def compute_rows(
    closes: Mapping[str, Mapping[_dt.date, float]],
    day: _dt.date,
    *,
    window: int = WINDOW,
    min_obs: int = MIN_OBS,
) -> list[CorrRow]:
    """One row per pair of *closes* (``a < b``) with at least *min_obs* aligned returns.

    The caller passes closes strictly before *day* (no look-ahead); this function does
    not re-filter by date, so the live job and the sim decide the cut-off once.
    """
    names = sorted({t.upper() for t in closes})
    series = {t.upper(): s for t, s in closes.items()}
    out: list[CorrRow] = []
    for a, b in itertools.combinations(names, 2):
        res = return_corr(series[a], series[b], window, min_obs)
        if res.corr is None or res.as_of is None:
            continue
        out.append(CorrRow(day, a, b, round(res.corr, 6), res.n_obs, res.window, res.as_of))
    return out


def replace_day(
    conn: sqlite3.Connection, day: _dt.date, rows: Iterable[CorrRow], *, now: _dt.datetime
) -> int:
    """Replace *day*'s rows with *rows* in one transaction. Returns rows written."""
    stamp = now.astimezone(_dt.UTC).isoformat()
    n = 0
    with conn:
        conn.execute("DELETE FROM correlations WHERE day = ?", (day.isoformat(),))
        for r in rows:
            a, b = pair_key(r.a, r.b)
            conn.execute(
                "INSERT INTO correlations (day, a, b, corr, n_obs, window, as_of, source,"
                " created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    r.day.isoformat(),
                    a,
                    b,
                    r.corr,
                    r.n_obs,
                    r.window,
                    r.as_of.isoformat(),
                    r.source,
                    stamp,
                ),
            )
            n += 1
    return n


def latest_day(conn: sqlite3.Connection | None, today: _dt.date) -> _dt.date | None:
    """The newest stored row day ``<= today`` (any age), or ``None``."""
    if conn is None:
        return None
    try:
        row = conn.execute(
            "SELECT MAX(day) FROM correlations WHERE day <= ?", (today.isoformat(),)
        ).fetchone()
    except sqlite3.OperationalError:  # store not migrated to 040 (old copy, ro view)
        return None
    return None if row is None or row[0] is None else _dt.date.fromisoformat(str(row[0]))


def _age_sessions(day: _dt.date, today: _dt.date) -> int:
    """Trading sessions in (day, today]: 0 for a row computed today."""
    return len(sessions_between(day + _dt.timedelta(days=1), today))


def corr_matrix(
    conn: sqlite3.Connection | None,
    today: _dt.date,
    *,
    day: _dt.date | None = None,
    max_age_sessions: int = MAX_AGE_SESSIONS,
) -> CorrMatrix:
    """The stored matrix used *today* (or exactly *day*'s rows when given).

    Without *day*: the latest day ``<= today`` no older than *max_age_sessions*
    sessions; none -> ``CorrMatrix(day=None)`` (every pair unknown).
    """
    use = day if day is not None else latest_day(conn, today)
    if conn is None or use is None:
        return CorrMatrix(day=None)
    if day is None and _age_sessions(use, today) > max_age_sessions:
        return CorrMatrix(day=None)
    try:
        rows = conn.execute(
            "SELECT a, b, corr, window FROM correlations WHERE day = ?", (use.isoformat(),)
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    if not rows:
        return CorrMatrix(day=None)
    pairs = {(str(r[0]), str(r[1])): float(r[2]) for r in rows}
    names = frozenset(itertools.chain.from_iterable(pairs))
    return CorrMatrix(day=use, pairs=pairs, window=int(rows[0][3]), tickers=names)
