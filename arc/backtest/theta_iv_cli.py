"""``arc iv backfill --source theta`` (E7.13, D84): seed ``theta_backfill`` IV rows.

Dispatched from :mod:`arc.cli` (not :mod:`arc.iv.cli`): it runs the backtest code,
which :mod:`arc.iv` may not import. No network; ``--db`` defaults to ``data/arc.db``,
so rehearse on a scratch copy.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import argparse
    import datetime as _dt
    import sqlite3

    from arc.config import ArcSettings


def run_theta_backfill(args: argparse.Namespace) -> int:
    from arc.control.effective import effective_settings
    from arc.iv.cli import _open
    from arc.utils.calendar import now_et

    now = now_et()
    conn = _open(args)
    return _backfill_theta(args, conn, effective_settings(conn), now)


def iv_tickers(
    raw: str, conn: sqlite3.Connection, settings: ArcSettings, now: _dt.datetime
) -> list[str]:
    from arc.iv.cli import _tickers

    return _tickers(raw, conn, settings, now)


def theta_tickers(
    args: argparse.Namespace, conn: sqlite3.Connection, settings: ArcSettings, now: _dt.datetime
) -> list[str]:
    """``--tickers`` / ``--tickers-file`` / ``--universe`` (exactly one) as a ticker list."""
    given = [x for x in (args.tickers, args.tickers_file, args.universe) if x]
    if len(given) != 1:
        msg = "give exactly one of --tickers, --tickers-file, --universe"
        raise ValueError(msg)
    if args.tickers:
        return iv_tickers(args.tickers, conn, settings, now)
    if args.tickers_file:
        lines = Path(args.tickers_file).read_text().splitlines()
        names = [ln.split("#", 1)[0].strip() for ln in lines]
        return list(dict.fromkeys(n.upper() for n in names if n))
    from arc.backtest.universe import checkpoint_symbols, load_universe

    _, rows, _ = load_universe(Path(args.data_dir))
    return checkpoint_symbols(rows, args.universe)


def _backfill_theta(
    args: argparse.Namespace, conn: sqlite3.Connection, settings: ArcSettings, now: _dt.datetime
) -> int:
    """E7.13 (D84): ``theta_backfill`` rows from the cached ThetaData history (no network)."""
    from arc.backtest.theta_iv import seed

    try:
        tickers = theta_tickers(args, conn, settings, now)
    except (ValueError, FileNotFoundError) as exc:
        sys.stderr.write(f"arc iv backfill --source theta: {exc}\n")
        return 2
    if not tickers:
        sys.stderr.write("arc iv backfill --source theta: no tickers\n")
        return 2
    before = _source_counts(conn)
    rep = seed(
        conn,
        Path(args.data_dir),
        tickers,
        now=now,
        since=args.since,
        until=args.until,
        progress=_progress,
    )
    after = _source_counts(conn)
    sys.stdout.write(rep.summary() + "\n")
    sys.stdout.write("rows per source (before -> after):\n")
    for src in sorted(set(before) | set(after)):
        sys.stdout.write(f"  {src:<17} {before.get(src, 0):>7} -> {after.get(src, 0):>7}\n")
    for t in rep.errors:
        sys.stdout.write(f"  error {t.ticker}: {t.error}\n")
    return 1 if rep.tickers and len(rep.errors) == len(rep.tickers) else 0


def _source_counts(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute("SELECT source, COUNT(*) FROM iv_daily GROUP BY source").fetchall()
    return {str(r[0]): int(r[1]) for r in rows}


def _progress(msg: str) -> None:
    sys.stderr.write(msg + "\n")
