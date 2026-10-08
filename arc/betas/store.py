"""The ``betas`` table (migration 028) and the shared beta lookup (E3.6, D62).

:func:`betas_used` is the single lookup used by the gate inputs
(:func:`arc.pipeline.market.build_portfolio` and the proposal's ``MarketSnapshot``),
``portfolio_context``, the monitor heartbeat and the Tower. Rule: the latest stored row
with ``day <= today`` that is at most :data:`MAX_AGE_SESSIONS` trading sessions old;
its beta floored at 1.0. Anything older, missing, or computed from too few days ->
1.0 with ``source = "default"`` (logged as ``beta_default``). Never a rejection.
"""

from __future__ import annotations

import datetime as _dt
import sqlite3
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

import structlog

from arc.features.beta import BENCHMARK, BETA_FLOOR, BetaResult, floored_beta
from arc.utils.calendar import sessions_between

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

log = structlog.get_logger(__name__)

#: D62: a stored beta older than this many trading sessions is not used (1.0 instead).
MAX_AGE_SESSIONS = 5
SOURCE = "alpaca"

BetaSource = Literal["stored", "default"]


@dataclass(frozen=True)
class BetaUsed:
    """The beta a cap uses for one ticker (always >= 1.0) and where it came from."""

    ticker: str
    beta: float
    source: BetaSource
    raw: float | None = None  # stored (unfloored) beta, when a fresh row exists
    day: _dt.date | None = None


@dataclass(frozen=True)
class BetaRow:
    ticker: str
    day: _dt.date
    beta: float | None
    n_days: int
    window: int
    benchmark: str
    as_of: _dt.date | None
    source: str = SOURCE


def upsert(conn: sqlite3.Connection, rows: Iterable[BetaRow], *, now: _dt.datetime) -> int:
    """Insert or replace one row per (ticker, day). Returns rows written."""
    n = 0
    with conn:
        for r in rows:
            conn.execute(
                "INSERT INTO betas (ticker, day, beta, n_days, window, benchmark, as_of, source,"
                " created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(ticker, day) DO UPDATE SET beta = excluded.beta,"
                " n_days = excluded.n_days, window = excluded.window,"
                " benchmark = excluded.benchmark, as_of = excluded.as_of,"
                " source = excluded.source, created_at = excluded.created_at",
                (
                    r.ticker.upper(),
                    r.day.isoformat(),
                    r.beta,
                    r.n_days,
                    r.window,
                    r.benchmark,
                    r.as_of.isoformat() if r.as_of else r.day.isoformat(),
                    r.source,
                    now.astimezone(_dt.UTC).isoformat(),
                ),
            )
            n += 1
    return n


def row_from_result(ticker: str, day: _dt.date, res: BetaResult) -> BetaRow:
    return BetaRow(
        ticker=ticker.upper(),
        day=day,
        beta=None if res.beta is None else round(res.beta, 6),
        n_days=res.n_days,
        window=res.window,
        benchmark=BENCHMARK,
        as_of=res.as_of,
    )


def _age_sessions(day: _dt.date, today: _dt.date) -> int:
    """Trading sessions in (day, today]: 0 for a row computed today."""
    return len(sessions_between(day + _dt.timedelta(days=1), today))


def betas_used(
    conn: sqlite3.Connection | None, tickers: Sequence[str], today: _dt.date
) -> dict[str, BetaUsed]:
    """The beta each ticker's caps use today (>= 1.0); the one shared lookup."""
    wanted = list(dict.fromkeys(t.upper() for t in tickers))
    found: dict[str, tuple[_dt.date, float | None]] = {}
    if conn is not None and wanted:
        marks = ",".join("?" for _ in wanted)
        try:
            rows = conn.execute(
                f"SELECT ticker, day, beta FROM betas WHERE ticker IN ({marks}) AND day <= ?"  # noqa: S608 - placeholders only
                " ORDER BY day",
                (*wanted, today.isoformat()),
            ).fetchall()
        except sqlite3.OperationalError:  # store not migrated to 028 (old copy, ro view)
            rows = []
        for r in rows:  # ascending day: the last write per ticker wins
            found[str(r[0])] = (_dt.date.fromisoformat(str(r[1])), r[2])
    out: dict[str, BetaUsed] = {}
    for t in wanted:
        hit = found.get(t)
        fresh = hit is not None and _age_sessions(hit[0], today) <= MAX_AGE_SESSIONS
        if hit is not None and hit[1] is not None and fresh:
            raw = float(hit[1])
            out[t] = BetaUsed(t, floored_beta(raw), "stored", raw, hit[0])
            continue
        reason = "missing" if hit is None else ("too_few_days" if hit[1] is None else "stale")
        log.info("beta_default", ticker=t, reason=reason, day=None if hit is None else str(hit[0]))
        out[t] = BetaUsed(t, BETA_FLOOR, "default", None, None if hit is None else hit[0])
    return out


def latest_rows(conn: sqlite3.Connection, limit: int = 500) -> list[sqlite3.Row]:
    """Each ticker's latest row (``arc betas show``)."""
    return conn.execute(
        "SELECT b.* FROM betas b JOIN (SELECT ticker, MAX(day) AS day FROM betas GROUP BY ticker)"
        " m ON b.ticker = m.ticker AND b.day = m.day ORDER BY b.beta DESC NULLS LAST, b.ticker"
        " LIMIT ?",
        (limit,),
    ).fetchall()
