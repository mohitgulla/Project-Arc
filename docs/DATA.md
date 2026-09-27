# Historical options data (E7.1)

Backtest data for E7.2. Code: `arc/data/history/`. Cache: `data/` (gitignored).

## Providers (`HistoricalDataProvider` protocol, `arc/data/history/base.py`)

| name        | source                                   | history window        | fields                                   |
|-------------|------------------------------------------|-----------------------|------------------------------------------|
| `alpaca`    | Alpaca options bars (daily) + contracts API (paper) | 2024-02-01 → | OHLC, volume, trade_count, VWAP — **no bid/ask** |
| `thetadata` | ThetaData v3 `option/history/eod` via local Theta Terminal | last ~365 days (free tier) | OHLC, volume, count, closing NBBO bid/ask + sizes |

Both return `OptionEodRow` records: one row per contract per session, with
`symbol` as an unpadded OCC symbol (`SPY240301C00500000`) so rows from both
providers can be joined.

Notes:
- Alpaca serves no *historical* option quotes (only latest), so spreads for the
  cost model have to come from ThetaData EOD NBBO.
- Alpaca daily bars have gaps even when trades exist (e.g. 2024-02-02 is
  missing for every contract). Those days are cached as zero-row files and
  show up as `empty` in the coverage report.
- ThetaData requires the v3 Theta Terminal running locally (default
  `http://127.0.0.1:25503`, `--theta-url` to override). Its credentials stay in
  the terminal's own config, never in this repo. If it isn't running, the provider raises
  `ThetaTerminalError` and the ticker gets an error in the download result.

## Storage

```
data/options_eod/provider=<p>/underlying=<SYM>/<YYYY-MM-DD>.parquet   # one file per session
data/coverage/<p>_by_date.csv                                          # per ticker/date coverage
```

Writes are atomic (tmp + rename). Downloads are incremental, so already-cached
sessions are skipped unless `--refresh` is passed.

## CLI

```
arc history download [--provider alpaca|thetadata ...] [--tickers SPY,QQQ] \
    [--start YYYY-MM-DD] [--end YYYY-MM-DD] [--max-dte 60] [--chunk 10] [--refresh]
arc history coverage [--provider ...] [--tickers ...] [--start ...] [--end ...]
```

Defaults: tickers = configured universe (D9); start = provider's earliest date;
end = the last completed session. Coverage prints a per-ticker table
(sessions / ok / empty / missing / rows / cached%) and writes the per-date CSV.

```python
from arc.data.history import ParquetHistoryStore
df = ParquetHistoryStore("data").read("alpaca", "SPY", start=date(2025, 1, 2))
```
