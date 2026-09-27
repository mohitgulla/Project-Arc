"""Parquet cache for historical option EOD rows + coverage reporting.

Layout (one file per provider / underlying / trading session)::

    <root>/options_eod/provider=<name>/underlying=<SYM>/<YYYY-MM-DD>.parquet

A session that was fetched but had no rows is still written (zero-row file with
the full schema) so it counts as *cached* and is not re-downloaded; the
coverage report distinguishes it as ``empty``.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import TYPE_CHECKING

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import structlog
from pydantic import BaseModel

from arc.data.history.base import EOD_COLUMNS, OptionEodRow
from arc.utils.calendar import sessions_between

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

log = structlog.get_logger()

SCHEMA = pa.schema(
    [
        ("provider", pa.string()),
        ("underlying", pa.string()),
        ("date", pa.date32()),
        ("symbol", pa.string()),
        ("expiration", pa.date32()),
        ("strike", pa.float64()),
        ("right", pa.string()),
        ("open", pa.float64()),
        ("high", pa.float64()),
        ("low", pa.float64()),
        ("close", pa.float64()),
        ("volume", pa.float64()),
        ("trade_count", pa.float64()),
        ("vwap", pa.float64()),
        ("bid", pa.float64()),
        ("ask", pa.float64()),
        ("bid_size", pa.float64()),
        ("ask_size", pa.float64()),
    ]
)
assert tuple(SCHEMA.names) == EOD_COLUMNS  # noqa: S101 — schema/model drift guard


class DayCoverage(BaseModel):
    """Coverage of one provider/underlying/session."""

    provider: str
    underlying: str
    date: dt.date
    status: str  # "ok" | "empty" | "missing"
    rows: int = 0
    contracts: int = 0
    with_close: int = 0
    with_quote: int = 0


class TickerCoverage(BaseModel):
    """Aggregated coverage for one provider/underlying over a date window."""

    provider: str
    underlying: str
    start: dt.date
    end: dt.date
    sessions: int
    ok: int
    empty: int
    missing: int
    rows: int
    missing_dates: list[dt.date]

    @property
    def pct_cached(self) -> float:
        return 0.0 if self.sessions == 0 else (self.ok + self.empty) / self.sessions


class ParquetHistoryStore:
    """Read/write cached EOD option rows under ``root``."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root) / "options_eod"

    # -- paths ---------------------------------------------------------------

    def _dir(self, provider: str, underlying: str) -> Path:
        return self.root / f"provider={provider}" / f"underlying={underlying.upper()}"

    def path_for(self, provider: str, underlying: str, day: dt.date) -> Path:
        return self._dir(provider, underlying) / f"{day.isoformat()}.parquet"

    def has(self, provider: str, underlying: str, day: dt.date) -> bool:
        return self.path_for(provider, underlying, day).is_file()

    def cached_dates(self, provider: str, underlying: str) -> set[dt.date]:
        d = self._dir(provider, underlying)
        if not d.is_dir():
            return set()
        out: set[dt.date] = set()
        for p in d.glob("*.parquet"):
            try:
                out.add(dt.date.fromisoformat(p.stem))
            except ValueError:
                log.warning("history_store.bad_filename", path=str(p))
        return out

    # -- write ---------------------------------------------------------------

    def write_day(
        self,
        provider: str,
        underlying: str,
        day: dt.date,
        rows: Sequence[OptionEodRow],
    ) -> Path:
        """Atomically write all rows for one session (zero rows allowed)."""
        for r in rows:
            if r.date != day or r.provider != provider or r.underlying != underlying.upper():
                msg = f"row {r.symbol} {r.date} does not belong to {provider}/{underlying}/{day}"
                raise ValueError(msg)
        records = [r.model_dump(mode="python") for r in rows]
        for rec in records:
            rec["right"] = str(rec["right"])
        table = pa.Table.from_pylist(records, schema=SCHEMA)
        path = self.path_for(provider, underlying, day)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".parquet.tmp")
        pq.write_table(table, tmp)
        tmp.replace(path)
        return path

    def write_range(
        self,
        provider: str,
        underlying: str,
        sessions: Iterable[dt.date],
        rows: Iterable[OptionEodRow],
    ) -> int:
        """Group *rows* by date and write one file per session in *sessions*.

        Sessions with no rows get an empty file. Rows outside *sessions* are
        dropped with a warning. Returns the number of files written.
        """
        wanted = sorted(set(sessions))
        by_day: dict[dt.date, list[OptionEodRow]] = {d: [] for d in wanted}
        stray = 0
        for r in rows:
            bucket = by_day.get(r.date)
            if bucket is None:
                stray += 1
            else:
                bucket.append(r)
        if stray:
            log.warning(
                "history_store.stray_rows", provider=provider, underlying=underlying, n=stray
            )
        for day, day_rows in by_day.items():
            self.write_day(provider, underlying, day, day_rows)
        return len(by_day)

    # -- read ----------------------------------------------------------------

    def read(
        self,
        provider: str,
        underlying: str,
        start: dt.date | None = None,
        end: dt.date | None = None,
    ) -> pd.DataFrame:
        """Load cached rows as a DataFrame (empty frame with schema if none)."""
        days = sorted(
            d
            for d in self.cached_dates(provider, underlying)
            if (start is None or d >= start) and (end is None or d <= end)
        )
        tables = [
            pq.read_table(self.path_for(provider, underlying, d), schema=SCHEMA) for d in days
        ]
        if not tables:
            return SCHEMA.empty_table().to_pandas()
        return pa.concat_tables(tables).to_pandas()

    # -- coverage ------------------------------------------------------------

    def day_coverage(self, provider: str, underlying: str, day: dt.date) -> DayCoverage:
        path = self.path_for(provider, underlying, day)
        if not path.is_file():
            return DayCoverage(provider=provider, underlying=underlying, date=day, status="missing")
        t = pq.read_table(path, columns=["symbol", "close", "bid", "ask"])
        n = t.num_rows
        if n == 0:
            return DayCoverage(provider=provider, underlying=underlying, date=day, status="empty")
        df = t.to_pandas()
        return DayCoverage(
            provider=provider,
            underlying=underlying,
            date=day,
            status="ok",
            rows=n,
            contracts=int(df["symbol"].nunique()),
            with_close=int(df["close"].notna().sum()),
            with_quote=int((df["bid"].notna() & df["ask"].notna()).sum()),
        )

    def coverage(
        self,
        provider: str,
        underlyings: Sequence[str],
        start: dt.date,
        end: dt.date,
    ) -> tuple[list[TickerCoverage], pd.DataFrame]:
        """Per-ticker summary and per-ticker/date detail frame for [start, end]."""
        sessions = sessions_between(start, end)
        summaries: list[TickerCoverage] = []
        detail: list[dict[str, object]] = []
        for u in underlyings:
            days = [self.day_coverage(provider, u.upper(), d) for d in sessions]
            detail.extend(d.model_dump() for d in days)
            summaries.append(
                TickerCoverage(
                    provider=provider,
                    underlying=u.upper(),
                    start=start,
                    end=end,
                    sessions=len(days),
                    ok=sum(d.status == "ok" for d in days),
                    empty=sum(d.status == "empty" for d in days),
                    missing=sum(d.status == "missing" for d in days),
                    rows=sum(d.rows for d in days),
                    missing_dates=[d.date for d in days if d.status == "missing"],
                )
            )
        cols = list(DayCoverage.model_fields.keys())
        return summaries, pd.DataFrame(detail, columns=cols)


def format_coverage(summaries: Sequence[TickerCoverage]) -> str:
    """Render per-ticker coverage as a fixed-width text table."""
    header = (
        f"{'provider':<10} {'ticker':<7} {'start':<10} {'end':<10} "
        f"{'sessions':>8} {'ok':>5} {'empty':>5} {'missing':>7} {'rows':>10} {'cached%':>8}"
    )
    lines = [header, "-" * len(header)]
    for s in summaries:
        lines.append(
            f"{s.provider:<10} {s.underlying:<7} {s.start.isoformat():<10} {s.end.isoformat():<10} "
            f"{s.sessions:>8} {s.ok:>5} {s.empty:>5} {s.missing:>7} {s.rows:>10} "
            f"{s.pct_cached * 100:>7.1f}%"
        )
    return "\n".join(lines)
