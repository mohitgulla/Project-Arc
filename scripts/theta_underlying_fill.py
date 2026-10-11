"""E7.9 (D84): fill the underlying daily data the backtest needs for every pull-list name.

For each name of an E7.7 universe (priority order): raw daily closes from Alpaca SIP via
``arc.backtest.underlying.load_closes`` (the cache ``arc backtest``'s ``closes_for`` reads)
and split-adjusted daily OHLC via ``arc.backtest.entry_filter.load_ohlc`` (``ohlc_for``).
Names Alpaca has no bars for are listed as excluded: they are the survivorship gap.

    .venv/bin/python scripts/theta_underlying_fill.py --version u500-20261009-s1-cbfbdeb9 \
        --start 2020-01-01 --end 2026-10-09 [--offline] [--json out.json]

Free data only; never touches the Theta Terminal or the audit DB. Prints aggregates.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

from arc.backtest.entry_filter import WARMUP_DAYS, OhlcStore, load_ohlc
from arc.backtest.underlying import UnderlyingStore, load_closes
from arc.backtest.universe import load_universe


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--version", default=None)
    ap.add_argument("--data-dir", type=Path, default=Path("data"))
    ap.add_argument("--start", type=dt.date.fromisoformat, default=dt.date(2020, 1, 1))
    ap.add_argument("--end", type=dt.date.fromisoformat, required=True)
    ap.add_argument("--offline", action="store_true", help="Report the cache only")
    ap.add_argument("--json", type=Path, default=None)
    args = ap.parse_args(argv)

    manifest, rows, _ = load_universe(args.data_dir, args.version)
    closes_src = ohlc_src = None
    if not args.offline:  # pragma: no cover - network
        from arc.backtest.entry_filter import AlpacaOhlcSource
        from arc.backtest.underlying import AlpacaBarsSource
        from arc.data.history.cli import _load_alpaca_env

        _load_alpaca_env()
        closes_src, ohlc_src = AlpacaBarsSource(), AlpacaOhlcSource()
    us, os_ = UnderlyingStore(args.data_dir), OhlcStore(args.data_dir)
    c_lo = args.start - dt.timedelta(days=60)  # closes_for: regime warm-up
    o_lo = args.start - dt.timedelta(days=WARMUP_DAYS * 2)  # ohlc_for: indicator warm-up
    out: list[dict[str, object]] = []
    for i, r in enumerate(rows, 1):
        rec: dict[str, object] = {"position": r.position, "symbol": r.symbol}
        try:
            c = load_closes(us, r.symbol, c_lo, args.end, source=closes_src)
            o = load_ohlc(os_, r.symbol, o_lo, args.end, source=ohlc_src)
        except Exception as exc:  # noqa: BLE001 - one bad symbol must not stop the fill
            rec["error"] = f"{type(exc).__name__}: {str(exc)[:160]}"
            c = o = None
        if c is not None:
            rec.update(
                closes=len(c),
                closes_first=str(min(c.index)) if len(c) else None,
                closes_last=str(max(c.index)) if len(c) else None,
                ohlc=len(o) if o is not None else 0,
            )
        out.append(rec)
        if i % 50 == 0 or i == len(rows):
            sys.stdout.write(f"underlying: {i}/{len(rows)}\n")
            sys.stdout.flush()
    missing = [x["symbol"] for x in out if not x.get("closes")]
    no_ohlc = [x["symbol"] for x in out if x.get("closes") and not x.get("ohlc")]
    late = [
        (x["symbol"], x["closes_first"])
        for x in out
        if x.get("closes_first") and str(x["closes_first"]) > args.start.isoformat()
    ]
    ended = [
        (x["symbol"], x["closes_last"])
        for x in out
        if x.get("closes_last")
        and dt.date.fromisoformat(str(x["closes_last"])) < args.end - dt.timedelta(days=7)
    ]
    sys.stdout.write(
        f"universe {manifest.version}: {len(out)} names · with closes {len(out) - len(missing)} · "
        f"no Alpaca bars (excluded, survivorship gap) {len(missing)}: {', '.join(missing) or '-'}\n"
        f"no split-adjusted OHLC: {len(no_ohlc)}: {', '.join(no_ohlc) or '-'}\n"
        f"start after {args.start} (IPO/listing, not a gap): {len(late)}\n"
        f"bars end before {args.end} (delisted/renamed): {len(ended)}: "
        + ", ".join(f"{s} {d}" for s, d in ended)
        + "\n"
    )
    if args.json:
        args.json.write_text(json.dumps(out, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
