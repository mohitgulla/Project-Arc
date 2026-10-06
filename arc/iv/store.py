"""The ``iv_daily`` store (E4.12, D55): one 30-DTE IV per (ticker, day, source).

Our series (what IV rank / percentile are computed over) prefers the forward
``alpaca_cm30`` reading of a day and falls back to the ``alpaca_backfill`` one.
``optionstrategist`` rows are never part of it: they are an external cross-check and
a clearly labelled fallback percentile only (:func:`latest_external`).
"""

from __future__ import annotations

import datetime as _dt
import json
import sqlite3
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

import structlog

from arc.context.ttl import to_db

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence
    from pathlib import Path

log = structlog.get_logger(__name__)

IvSource = Literal["alpaca_cm30", "alpaca_backfill", "optionstrategist"]
FORWARD: IvSource = "alpaca_cm30"
BACKFILL: IvSource = "alpaca_backfill"
OPTIONSTRATEGIST: IvSource = "optionstrategist"
#: Our series, in preference order for a day that has both.
SERIES_SOURCES: tuple[IvSource, ...] = (FORWARD, BACKFILL)


@dataclass(frozen=True)
class IvRow:
    """One ``iv_daily`` row (decimals: 0.20 = 20 vol)."""

    ticker: str
    day: _dt.date
    iv30: float
    method: str
    source: IvSource
    spot: float | None = None
    spot_basis: str | None = None
    n_contracts: int | None = None
    hv20: float | None = None
    hv50: float | None = None
    hv100: float | None = None
    ext_days: int | None = None
    ext_percentile: float | None = None
    detail: dict[str, Any] = field(default_factory=dict)


def _row(r: sqlite3.Row | tuple[Any, ...]) -> IvRow:
    (ticker, day, iv30, method, source, spot, basis, n, hv20, hv50, hv100, ed, ep, detail) = tuple(
        r
    )[:14]
    return IvRow(
        ticker=str(ticker),
        day=_dt.date.fromisoformat(str(day)),
        iv30=float(iv30),
        method=str(method),
        source=source,
        spot=spot,
        spot_basis=basis,
        n_contracts=n,
        hv20=hv20,
        hv50=hv50,
        hv100=hv100,
        ext_days=ed,
        ext_percentile=ep,
        detail=json.loads(detail or "{}"),
    )


_COLS = (
    "ticker, day, iv30, method, source, spot, spot_basis, n_contracts, hv20, hv50, hv100, "
    "ext_days, ext_percentile, detail"
)


class IvStore:
    """Read/write ``iv_daily`` and ``iv_skips`` (the caller owns the connection)."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    # -- writes ----------------------------------------------------------------

    def upsert(self, rows: Iterable[IvRow], *, now: _dt.datetime) -> int:
        """Insert or replace each row's ``(ticker, day, source)``; also clears a skip."""
        n = 0
        at = to_db(now)
        with self.conn:
            for r in rows:
                if not r.iv30 > 0:
                    msg = f"{r.ticker} {r.day}: iv30 must be positive, got {r.iv30}"
                    raise ValueError(msg)
                self.conn.execute(
                    f"""INSERT INTO iv_daily ({_COLS}, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(ticker, day, source) DO UPDATE SET
                          iv30 = excluded.iv30, method = excluded.method, spot = excluded.spot,
                          spot_basis = excluded.spot_basis, n_contracts = excluded.n_contracts,
                          hv20 = excluded.hv20, hv50 = excluded.hv50, hv100 = excluded.hv100,
                          ext_days = excluded.ext_days, ext_percentile = excluded.ext_percentile,
                          detail = excluded.detail, created_at = excluded.created_at""",  # noqa: S608 - fixed columns
                    (
                        r.ticker.upper(),
                        r.day.isoformat(),
                        float(r.iv30),
                        r.method,
                        r.source,
                        r.spot,
                        r.spot_basis,
                        r.n_contracts,
                        r.hv20,
                        r.hv50,
                        r.hv100,
                        r.ext_days,
                        r.ext_percentile,
                        json.dumps(r.detail, sort_keys=True, default=str),
                        at,
                    ),
                )
                self.conn.execute(
                    "DELETE FROM iv_skips WHERE ticker = ? AND day = ? AND source = ?",
                    (r.ticker.upper(), r.day.isoformat(), r.source),
                )
                n += 1
        return n

    def skip(
        self, ticker: str, day: _dt.date, source: IvSource, reason: str, *, now: _dt.datetime
    ) -> None:
        """Record that *day* could not be reconstructed (resumable backfill)."""
        with self.conn:
            self.conn.execute(
                """INSERT INTO iv_skips (ticker, day, source, reason, created_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(ticker, day, source) DO UPDATE SET
                     reason = excluded.reason, created_at = excluded.created_at""",
                (ticker.upper(), day.isoformat(), source, reason[:500], to_db(now)),
            )

    # -- reads -----------------------------------------------------------------

    def days(self, ticker: str, source: IvSource, *, include_skips: bool = False) -> set[_dt.date]:
        """Days already stored (optionally also recorded skips) for *ticker*/*source*."""
        rows = self.conn.execute(
            "SELECT day FROM iv_daily WHERE ticker = ? AND source = ?", (ticker.upper(), source)
        ).fetchall()
        out = {_dt.date.fromisoformat(str(r[0])) for r in rows}
        if include_skips:
            skips = self.conn.execute(
                "SELECT day FROM iv_skips WHERE ticker = ? AND source = ?",
                (ticker.upper(), source),
            ).fetchall()
            out |= {_dt.date.fromisoformat(str(r[0])) for r in skips}
        return out

    def rows(
        self,
        ticker: str,
        sources: Sequence[IvSource],
        *,
        until: _dt.date | None = None,
        since: _dt.date | None = None,
    ) -> list[IvRow]:
        marks = ",".join("?" for _ in sources)
        sql = f"SELECT {_COLS} FROM iv_daily WHERE ticker = ? AND source IN ({marks})"  # noqa: S608
        args: list[Any] = [ticker.upper(), *sources]
        if until is not None:
            sql += " AND day <= ?"
            args.append(until.isoformat())
        if since is not None:
            sql += " AND day >= ?"
            args.append(since.isoformat())
        return [_row(r) for r in self.conn.execute(sql + " ORDER BY day", args).fetchall()]

    def series(self, ticker: str, *, until: _dt.date | None = None) -> dict[_dt.date, float]:
        """Our IV series for *ticker*: ``alpaca_cm30`` per day, else ``alpaca_backfill``."""
        rank = {s: i for i, s in enumerate(SERIES_SOURCES)}
        best: dict[_dt.date, tuple[int, float]] = {}
        for r in self.rows(ticker, SERIES_SOURCES, until=until):
            k = rank[r.source]
            if r.day not in best or k < best[r.day][0]:
                best[r.day] = (k, r.iv30)
        return {d: v for d, (_, v) in sorted(best.items())}

    def latest_external(self, ticker: str, *, until: _dt.date) -> IvRow | None:
        """The newest ``optionstrategist`` row on or before *until* (``None`` if none)."""
        row = self.conn.execute(
            f"SELECT {_COLS} FROM iv_daily WHERE ticker = ? AND source = ? AND day <= ? "  # noqa: S608
            "ORDER BY day DESC LIMIT 1",
            (ticker.upper(), OPTIONSTRATEGIST, until.isoformat()),
        ).fetchone()
        return _row(row) if row else None

    def rows_on(self, day: _dt.date, source: IvSource) -> list[IvRow]:
        """Every ticker's row of *source* on *day* (sorted by ticker)."""
        rows = self.conn.execute(
            f"SELECT {_COLS} FROM iv_daily WHERE day = ? AND source = ? ORDER BY ticker",  # noqa: S608
            (day.isoformat(), source),
        ).fetchall()
        return [_row(r) for r in rows]

    def counts(self) -> dict[str, int]:
        rows = self.conn.execute("SELECT source, COUNT(*) FROM iv_daily GROUP BY source").fetchall()
        return {str(r[0]): int(r[1]) for r in rows}


def safe_store(conn: sqlite3.Connection | None) -> IvStore | None:
    """An :class:`IvStore` when *conn* has the table (``None`` before migrate / no store)."""
    if conn is None:
        return None
    try:
        conn.execute("SELECT 1 FROM iv_daily LIMIT 1")
    except sqlite3.OperationalError:
        return None
    return IvStore(conn)


def import_csv_dir(
    conn: sqlite3.Connection, directory: Path, *, now: _dt.datetime
) -> dict[str, int]:
    """One-time import of the pre-E4.12 ``<TICKER>.csv`` (``date,atm_iv``) files.

    The scanner's old ``arc chains --record-iv`` wrote the nearest-30-DTE expiry's ATM
    IV, so the rows are stored as ``alpaca_cm30`` (method ``legacy_csv``). A missing
    directory is no files (none existed live when E4.12 shipped).
    """
    from arc.scanner.iv import load_iv_history

    out: dict[str, int] = {}
    if not directory.is_dir():
        return out
    store = IvStore(conn)
    for path in sorted(directory.glob("*.csv")):
        ticker = path.stem.upper()
        hist = load_iv_history(directory, ticker)
        have = store.days(ticker, FORWARD)
        rows = [
            IvRow(ticker, d, v, "legacy_csv", FORWARD, detail={"file": path.name})
            for d, v in hist.items()
            if d not in have
        ]
        out[ticker] = store.upsert(rows, now=now)
    log.info("iv.csv_imported", files=len(out), rows=sum(out.values()))
    return out
