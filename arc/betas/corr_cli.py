"""``arc correlations`` (E28.4, D131): stored 60-session return correlations.

    arc correlations show <T> [--day YYYY-MM-DD] [--top N] [--threshold X] [--db P]

Prints *T*'s correlation with every other name of the stored day (default: the
matrix the cluster cap uses today, i.e. the latest fresh day), descending, with the
names above the cluster threshold (D131: 0.7) marked, plus *T*'s open-book peers.
Read-only; rows are written by the ``betas`` routine / ``arc betas refresh``.
"""

from __future__ import annotations

import datetime as _dt
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import argparse
    import sqlite3

    from arc.betas.corr import CorrMatrix


def add_correlations_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    from arc.features.correlation import CLUSTER_THRESHOLD

    p = sub.add_parser(
        "correlations", help="60-session return correlations for the cluster cap (E28.4)"
    )
    csub = p.add_subparsers(dest="correlations_command", required=True)
    s = csub.add_parser("show", help="One ticker's stored correlations and cluster peers")
    s.add_argument("ticker", help="Ticker, e.g. NVDA")
    s.add_argument(
        "--day",
        type=_dt.date.fromisoformat,
        default=None,
        help="Exact stored row day (default: the latest fresh day the cap uses today)",
    )
    s.add_argument("--top", type=int, default=25, help="Rows to print (default 25)")
    s.add_argument(
        "--threshold",
        type=float,
        default=CLUSTER_THRESHOLD,
        help=f"Cluster threshold (default {CLUSTER_THRESHOLD})",
    )
    s.add_argument("--db", default=None, help="Audit DB (default data/arc.db)")


def _open_ro(db: str | None) -> sqlite3.Connection:
    import sqlite3

    from arc.store.db import DEFAULT_DB_PATH

    p = Path(db) if db else Path(DEFAULT_DB_PATH)
    if not p.is_file():
        msg = f"no store at {p}"
        raise FileNotFoundError(msg)
    conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def render_show(
    matrix: CorrMatrix,
    ticker: str,
    *,
    open_tickers: list[str],
    top: int,
    threshold: float,
) -> str:
    """The ``show`` text (pure, tested)."""
    from arc.betas.corr import cluster_peers

    t = ticker.upper()
    if matrix.day is None:
        return "no fresh correlations stored (run `arc betas refresh`)\n"
    rows = sorted(
        (
            (other, c)
            for other in matrix.tickers
            if other != t and (c := matrix.get(t, other)) is not None
        ),
        key=lambda r: (-r[1], r[0]),
    )
    lines = [f"{t} · correlations for {matrix.day} · {matrix.window}-session log returns"]
    if not rows:
        lines.append(f"{t} has no stored pairs on {matrix.day}")
    for other, c in rows[:top]:
        mark = "  cluster" if c > threshold else ""
        lines.append(f"  {other:<7} {c:6.3f}{mark}")
    if len(rows) > top:
        lines.append(f"  … {len(rows) - top} more")
    above = sum(1 for _, c in rows if c > threshold)
    peers = cluster_peers(matrix, t, open_tickers, threshold)
    unknown = sorted(o.upper() for o in open_tickers if o.upper() != t and matrix.get(t, o) is None)
    lines.append(f"{above} names above {threshold:g}")
    lines.append(
        f"open-book peers: {', '.join(peers) or 'none'}"
        + (f" · unknown pairs: {', '.join(unknown)}" if unknown else "")
    )
    return "\n".join(lines) + "\n"


def run_correlations(args: argparse.Namespace) -> int:
    from arc.betas.corr_store import corr_matrix
    from arc.universe.tiers import open_underlyings
    from arc.utils.calendar import now_et

    conn = _open_ro(args.db)
    matrix = corr_matrix(conn, now_et().date(), day=args.day)
    sys.stdout.write(
        render_show(
            matrix,
            args.ticker,
            open_tickers=open_underlyings(conn),
            top=args.top,
            threshold=args.threshold,
        )
    )
    return 0 if matrix.day is not None else 1
