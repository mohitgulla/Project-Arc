"""E27.1a: an explicit, audited re-fetch of a date range into the underlying OHLC cache.

:func:`arc.backtest.entry_filter.load_ohlc` never refetches a range the cache already
covers (backtests rely on a stable cache). When a cached bar is known to be wrong or
revised, :func:`refresh_ohlc` replaces exactly ``[start, end]`` with what the source
returns now, and nothing outside it:

* per row it reports ``added`` (date new to the cache), ``changed`` (any of
  ``open high low close vwap`` differs, NaN == NaN) with before → after, and keeps a
  cached in-range date the source no longer returns (``kept``: never deletes);
* ``dry_run`` computes the same diff and writes nothing;
* a write first copies the file to ``<SYM>.parquet.bak-pre-<tag>-<YYYYmmdd-HHMMSS>``
  (refused when that backup exists), then writes atomically (:meth:`OhlcStore.write`);
* idempotent: a second run against the same source reports 0 changes and writes nothing.

CLI: ``arc backtest ohlc refresh --tickers AMD --from 2026-10-05 --to 2026-10-09
[--dry-run] [--backup-tag e27.1a] [--data-dir data]``.
"""

from __future__ import annotations

import datetime as dt
import math
import shutil
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import pandas as pd
import structlog
from pydantic import BaseModel, ConfigDict

from arc.backtest.entry_filter import OHLC_COLUMNS, OhlcStore

if TYPE_CHECKING:
    import argparse
    from collections.abc import Callable

    from arc.backtest.entry_filter import OhlcSource

log = structlog.get_logger()

__all__ = [
    "BackupExistsError",
    "OhlcRowChange",
    "OhlcRefreshResult",
    "add_ohlc_parser",
    "refresh_ohlc",
    "run_ohlc_cli",
]

ChangeKind = Literal["added", "changed", "kept"]


class BackupExistsError(FileExistsError):
    """The backup path for this refresh already exists; nothing was written."""


class OhlcRowChange(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    date: dt.date
    kind: ChangeKind
    before: dict[str, float | None] | None
    after: dict[str, float | None] | None


class OhlcRefreshResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    symbol: str
    start: dt.date
    end: dt.date
    dry_run: bool
    fetched_rows: int
    rows: list[OhlcRowChange]
    written: bool
    backup: Path | None

    @property
    def changes(self) -> int:
        """Rows the write alters (``added`` + ``changed``; ``kept`` alters nothing)."""
        return sum(r.kind != "kept" for r in self.rows)


def _vals(row: pd.Series) -> dict[str, float | None]:
    return {c: (None if math.isnan(float(row[c])) else float(row[c])) for c in OHLC_COLUMNS}


def _same(a: dict[str, float | None], b: dict[str, float | None]) -> bool:
    return all(a[c] == b[c] for c in OHLC_COLUMNS)


def _stamp(now: dt.datetime) -> str:
    return now.strftime("%Y%m%d-%H%M%S")


def refresh_ohlc(
    store: OhlcStore,
    symbol: str,
    start: dt.date,
    end: dt.date,
    source: OhlcSource,
    *,
    now: dt.datetime,
    dry_run: bool = False,
    backup_tag: str = "refresh",
) -> OhlcRefreshResult:
    """Re-fetch ``[start, end]`` of *symbol* from *source* into *store* (module docstring)."""
    if end < start:
        msg = f"refresh range is empty: {start} > {end}"
        raise ValueError(msg)
    sym = symbol.upper()
    cached = store.read(sym)
    fetched = source.daily_ohlc(sym, start, end)
    fidx = pd.Index(fetched.index)
    fetched = fetched[(fidx >= start) & (fidx <= end)]
    fetched = fetched[~pd.Index(fetched.index).duplicated(keep="last")]
    have = set(cached.index)
    rows: list[OhlcRowChange] = []
    for d in sorted(fetched.index):
        after = _vals(fetched.loc[d])
        if d not in have:
            rows.append(OhlcRowChange(date=d, kind="added", before=None, after=after))
            continue
        before = _vals(cached.loc[d])
        if not _same(before, after):
            rows.append(OhlcRowChange(date=d, kind="changed", before=before, after=after))
    got = set(fetched.index)
    for d in sorted(x for x in have if start <= x <= end and x not in got):
        rows.append(OhlcRowChange(date=d, kind="kept", before=_vals(cached.loc[d]), after=None))
    rows.sort(key=lambda r: r.date)
    for r in rows:
        log.info(
            "backtest.ohlc_refresh_row",
            symbol=sym,
            date=r.date.isoformat(),
            kind=r.kind,
            before=r.before,
            after=r.after,
            dry_run=dry_run,
        )
    changes = sum(r.kind != "kept" for r in rows)
    backup: Path | None = None
    written = False
    if changes and not dry_run:
        path = store.path_for(sym)
        if path.is_file():
            backup = path.with_name(f"{path.name}.bak-pre-{backup_tag}-{_stamp(now)}")
            if backup.exists():
                msg = f"backup already exists, refusing to overwrite: {backup}"
                raise BackupExistsError(msg)
            shutil.copy2(path, backup)
        replace = {r.date for r in rows if r.kind != "kept"}
        keep = cached[[d not in replace for d in cached.index]]
        merged = pd.concat([keep, fetched.loc[sorted(replace)]]).sort_index()
        store.write(sym, merged)
        written = True
    log.info(
        "backtest.ohlc_refreshed",
        symbol=sym,
        start=start.isoformat(),
        end=end.isoformat(),
        fetched=len(fetched),
        changes=changes,
        kept=len(rows) - changes,
        dry_run=dry_run,
        written=written,
        backup=str(backup) if backup else None,
    )
    return OhlcRefreshResult(
        symbol=sym,
        start=start,
        end=end,
        dry_run=dry_run,
        fetched_rows=len(fetched),
        rows=rows,
        written=written,
        backup=backup,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _date(s: str) -> dt.date:
    return dt.date.fromisoformat(s)


def add_ohlc_parser(bs: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = bs.add_parser("ohlc", help="E27.1a: the split-adjusted underlying OHLC cache")
    os_ = p.add_subparsers(dest="ohlc_command", required=True)
    r = os_.add_parser(
        "refresh",
        help="Re-fetch a date range (Alpaca SIP, split-adjusted) into <data-dir>/underlying_ohlc/",
    )
    r.add_argument("--tickers", required=True, help="Comma-separated symbols")
    r.add_argument("--from", dest="start", type=_date, required=True)
    r.add_argument("--to", dest="end", type=_date, required=True)
    r.add_argument("--data-dir", type=Path, default=Path("data"))
    r.add_argument("--dry-run", action="store_true", help="Print the diff; write nothing")
    r.add_argument(
        "--backup-tag", default="refresh", help="Backup name: <SYM>.parquet.bak-pre-<tag>-<stamp>"
    )


def _fmt(v: dict[str, float | None] | None) -> str:
    if v is None:
        return "-"
    return " ".join(
        f"{c[0] if c != 'vwap' else 'vw'}={v[c]:g}" for c in OHLC_COLUMNS if v[c] is not None
    )


def run_ohlc_cli(
    args: argparse.Namespace,
    *,
    source: OhlcSource | None = None,
    now: Callable[[], dt.datetime] | None = None,
) -> int:
    from arc.utils.calendar import now_et

    if source is None:  # pragma: no cover - network
        from arc.backtest.entry_filter import AlpacaOhlcSource
        from arc.data.history.cli import _load_alpaca_env

        _load_alpaca_env()
        source = AlpacaOhlcSource()
    clock = now or now_et
    store = OhlcStore(args.data_dir)
    tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    w = sys.stdout.write
    total = 0
    for t in tickers:
        try:
            res = refresh_ohlc(
                store,
                t,
                args.start,
                args.end,
                source,
                now=clock(),
                dry_run=args.dry_run,
                backup_tag=args.backup_tag,
            )
        except BackupExistsError as exc:
            sys.stderr.write(f"arc backtest ohlc refresh: {exc}\n")
            return 3
        total += res.changes
        mode = "dry-run" if res.dry_run else ("written" if res.written else "unchanged")
        w(
            f"{res.symbol} {res.start}..{res.end}: fetched {res.fetched_rows}, "
            f"{res.changes} changes ({mode})"
            + (f", backup {res.backup}" if res.backup else "")
            + "\n"
        )
        for r in res.rows:
            w(f"  {r.date} {r.kind:<7} {_fmt(r.before)} -> {_fmt(r.after)}\n")
    w(f"total changes: {total}{' (dry-run, nothing written)' if args.dry_run else ''}\n")
    return 0
