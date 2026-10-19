"""E28.6 (D131): store Cboe's VIX daily closes for the backtest's regime-variant knob.

    ARC_ENV=paper .venv/bin/python scripts/fetch_vix_history.py [--data-dir data]

Fetches Cboe's free ``VIX_History.csv`` (the live market-health source,
:func:`arc.ingest.options_data.fetch_index_closes`; D3: free) once and writes
``<data-dir>/market_index/VIX.parquet`` (``date``, ``close``), which
``backtest.sim.gate.regime_vix_pct_above`` reads offline. Prints the row count and the
date range. Re-running overwrites the file with the full history.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.loss_attribution import DATA  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    from arc.backtest.market_index import write_index_closes
    from arc.ingest.options_data import fetch_index_closes

    ap = argparse.ArgumentParser(description="Store Cboe VIX daily closes for the backtest (E28.6)")
    ap.add_argument("--data-dir", type=Path, default=DATA)
    ap.add_argument("--index", default="VIX")
    args = ap.parse_args(argv)
    closes = fetch_index_closes(args.index)
    if not closes:
        sys.stderr.write(f"no {args.index} closes fetched\n")
        return 1
    p = write_index_closes(args.data_dir, args.index, closes)
    sys.stdout.write(f"{p}: {len(closes)} closes {min(closes)} -> {max(closes)}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
