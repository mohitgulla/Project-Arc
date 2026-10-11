"""E7.9 step 5c (D84): ThetaData interest-rate history → ``<data-dir>/rates/``.

``arc history rates --provider thetadata`` fetches every series in
:data:`~arc.data.history.thetadata.RATE_SYMBOLS` (SOFR + Treasury M1..Y30) with one
request each through :class:`ThetaDataEodProvider` (same backoff/retry; credentials
stay in the Terminal). Each lands in ``<data-dir>/rates/<SYMBOL>.parquet`` (``date``,
``rate`` as a decimal), and ``TREASURY_M3`` is also written to
``<data-dir>/rates/treasury_3m.parquet``, the file ``arc.backtest.marks.RateCurve``
reads for ``rate_source: treasury``. A re-run merges into the cached series (fetched
rows win), so a later run on a higher rate tier extends the history backwards.

The rate subscription is separate from the options one (``--rate-tier``): an Options
Value account still reports ``INTEREST_RATE: FREE``, which serves 2024-01-01 on only.
Nothing here flips ``rate_source``.
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

import pandas as pd
import structlog
from pydantic import BaseModel, ConfigDict

from arc.data.history.thetadata import (
    RATE_FIRST_DATE,
    RATE_SYMBOLS,
    THETA_DEFAULT_URL,
    ThetaTerminalError,
    ThetaTier,
)

if TYPE_CHECKING:
    import argparse
    from collections.abc import Sequence

log = structlog.get_logger()

RATES_DIR = "rates"
TREASURY_3M_SOURCE = "TREASURY_M3"
TREASURY_3M_FILE = "treasury_3m.parquet"


class RateSource(Protocol):
    def fetch_interest_rate(
        self, symbol: str, start: dt.date, end: dt.date
    ) -> list[tuple[dt.date, float]]: ...


class RateSeriesResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str
    fetched: int = 0
    rows: int = 0
    first: dt.date | None = None
    last: dt.date | None = None
    error: str | None = None


def rates_dir(data_dir: Path) -> Path:
    return Path(data_dir) / RATES_DIR


def read_series(data_dir: Path, symbol: str) -> pd.DataFrame:
    p = rates_dir(data_dir) / f"{symbol.upper()}.parquet"
    if not p.is_file():
        return pd.DataFrame({"date": pd.Series(dtype=object), "rate": pd.Series(dtype=float)})
    return pd.read_parquet(p)


def _write(path: Path, df: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".parquet.tmp")
    df.to_parquet(tmp, index=False)
    tmp.replace(path)


def merge_series(old: pd.DataFrame, fresh: Sequence[tuple[dt.date, float]]) -> pd.DataFrame:
    """Old ∪ fresh by date (fresh wins), sorted, columns ``date`` / ``rate``."""
    new = pd.DataFrame(
        {"date": [d for d, _ in fresh], "rate": [float(r) for _, r in fresh]},
    )
    both = pd.concat([old[["date", "rate"]], new], ignore_index=True) if not old.empty else new
    both = both.drop_duplicates(subset="date", keep="last").sort_values("date")
    return both.reset_index(drop=True)


def pull_rates(
    source: RateSource,
    data_dir: Path,
    start: dt.date,
    end: dt.date,
    symbols: Sequence[str] = RATE_SYMBOLS,
) -> list[RateSeriesResult]:
    """Fetch each series, merge into its parquet, mirror TREASURY_M3 to treasury_3m."""
    out: list[RateSeriesResult] = []
    for sym in symbols:
        s = sym.upper()
        try:
            fresh = source.fetch_interest_rate(s, start, end)
        except ThetaTerminalError as exc:
            log.warning("rates.fetch_failed", symbol=s, error=str(exc)[:300])
            old = read_series(data_dir, s)
            out.append(
                RateSeriesResult(
                    symbol=s,
                    rows=len(old),
                    first=old["date"].min() if not old.empty else None,
                    last=old["date"].max() if not old.empty else None,
                    error=str(exc)[:300],
                )
            )
            continue
        merged = merge_series(read_series(data_dir, s), fresh)
        if not merged.empty:
            _write(rates_dir(data_dir) / f"{s}.parquet", merged)
            if s == TREASURY_3M_SOURCE:
                _write(rates_dir(data_dir) / TREASURY_3M_FILE, merged)
        out.append(
            RateSeriesResult(
                symbol=s,
                fetched=len(fresh),
                rows=len(merged),
                first=merged["date"].min() if not merged.empty else None,
                last=merged["date"].max() if not merged.empty else None,
            )
        )
    return out


def format_results(results: Sequence[RateSeriesResult]) -> str:
    lines = [f"{'series':<14}{'fetched':>8}{'rows':>7}  {'first':<10}  {'last':<10}  error"]
    for r in results:
        lines.append(
            f"{r.symbol:<14}{r.fetched:>8}{r.rows:>7}  {r.first or '-'!s:<10}  "
            f"{r.last or '-'!s:<10}  {r.error or ''}"
        )
    return "\n".join(lines)


def add_rates_parser(hs: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = hs.add_parser("rates", help="ThetaData interest-rate history (SOFR + Treasuries)")
    p.add_argument("--provider", choices=["thetadata"], default="thetadata")
    p.add_argument("--data-dir", type=Path, default=Path("data"))
    p.add_argument("--theta-url", default=THETA_DEFAULT_URL)
    p.add_argument(
        "--rate-tier",
        choices=[t.value for t in ThetaTier],
        default=ThetaTier.FREE.value,
        help="The account's INTEREST_RATE subscription (free: 2024-01-01 on)",
    )
    p.add_argument("--start", type=dt.date.fromisoformat, help="Default: the rate tier's first")
    p.add_argument("--end", type=dt.date.fromisoformat, help="Default: today")
    p.add_argument("--symbols", help=f"Comma-separated; default all {len(RATE_SYMBOLS)}")


def run_rates(args: argparse.Namespace, source: RateSource | None = None) -> int:
    from arc.utils.calendar import now_et

    tier = ThetaTier(args.rate_tier)
    start = args.start or RATE_FIRST_DATE[tier]
    end = args.end or now_et().date()
    if source is None:  # pragma: no cover - real Terminal
        from arc.data.history.thetadata import ThetaDataEodProvider

        source = ThetaDataEodProvider(base_url=args.theta_url, max_retries=3)
    symbols = (
        [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
        if args.symbols
        else list(RATE_SYMBOLS)
    )
    results = pull_rates(source, args.data_dir, start, end, symbols)
    sys.stdout.write(format_results(results) + "\n")
    sys.stdout.write(f"rates: {rates_dir(args.data_dir)}\n")
    return 1 if any(r.error for r in results) else 0
