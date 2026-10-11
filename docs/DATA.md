# Historical options data (E7.1)

Backtest data for E7.2. Code: `arc/data/history/`. Cache: `data/` (gitignored).

## Providers (`HistoricalDataProvider` protocol, `arc/data/history/base.py`)

| name        | source                                   | history window        | fields                                   |
|-------------|------------------------------------------|-----------------------|------------------------------------------|
| `alpaca`    | Alpaca options bars (daily) + contracts API (paper) | 2024-02-01 → | OHLC, volume, trade_count, VWAP — **no bid/ask** |
| `thetadata` | ThetaData v3 `option/history/eod` (+ `option/history/open_interest` on Value+) via local Theta Terminal | per `--theta-tier`: free = last 365 days (≥ 2023-06-01), value 2020-01-01, standard 2016-01-01, pro 2012-06-01 | OHLC, volume, count, closing NBBO bid/ask + sizes, `last_trade`, `created`, `open_interest` (Value+) |

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

E7.6 (D84) added three optional columns: `last_trade`, `created` (tz
America/New_York) and `open_interest`. Files written before them read back with
those columns null, so Alpaca and ThetaData partitions load into one frame.
`open_interest` keyed to session D is OPRA's ~06:30 ET report on D, i.e. the
prior session's closing OI (what a trader saw during D, no look-ahead). A file
whose OI was fetched carries the parquet metadata key `arc.open_interest=1`;
with OI on, a session counts as complete only with that marker, and a resumed
run fills OI into quote-only sessions without re-pulling the quotes.

Each download appends a JSONL ledger to `data/history_runs/<run_id>.jsonl`:
`run_start`, one `request` line per HTTP call (ticker, kind eod|oi, range,
chunk_days, status, rows, bytes, seconds, retries, error), `range_cached` /
`oi_filled` / `range_failed`, and `ticker_done`.

## CLI

```
arc history download [--provider alpaca|thetadata ...] [--tickers SPY,QQQ] \
    [--tickers-file universe.csv] [--max-tickers 500] \
    [--start YYYY-MM-DD] [--end YYYY-MM-DD] [--max-dte 60] [--chunk 10] [--refresh] \
    [--theta-tier free|value|standard|pro] [--with-oi|--no-oi] [--concurrency N] \
    [--theta-chunk-days 7] [--theta-interval S] [--progress-every 25] \
    [--plan [--plan-rows-per-day 2000] [--plan-sec-per-request 3.5]]
arc history coverage [--provider ...] [--tickers ...] [--start ...] [--end ...]
```

ThetaData pull (D84, E7.6):
- `--theta-tier` sets the earliest date and the concurrency cap (free 1, value 2,
  standard 4, pro 8). `--concurrency` defaults to the cap and is clamped to it;
  workers run tickers in parallel, in list order. `--with-oi` defaults on for
  tier ≥ value.
- `--tickers-file`: one ticker per line (`#` comments ok) or a `.csv`/`.parquet`
  with a `ticker`/`symbol`/`underlying` column; processed in file order after any
  `--tickers`, de-duplicated, then cut at `--max-tickers`.
- Requests retry with exponential backoff on 429/474/5xx and connection errors;
  472 (no data) is an empty result. The per-ticker chunk starts at
  `--theta-chunk-days`, halves on a timeout, HTTP 570 or a body > 50 MB, and
  doubles up to 28 days when the chain is small (< 500 rows per calendar day).
- A progress line prints every `--progress-every` requests (done/planned
  requests, rows, GB, req/s, ETA). The run ends with a per-ticker table and the
  failed ranges; re-running the same command resumes (cached sessions skipped).
- `--plan` prints requests, rows, disk/wire GB and ETA without any network:
  rows per session from the ticker's cached files (this provider first, else
  another provider's cache), else `--plan-rows-per-day`.

Defaults: tickers = configured universe (D9); start = provider's earliest date;
end = the last completed session. Coverage prints a per-ticker table
(sessions / ok / empty / missing / rows / cached%) and writes the per-date CSV.

```python
from arc.data.history import ParquetHistoryStore

df = ParquetHistoryStore("data").read("alpaca", "SPY", start=date(2025, 1, 2))
```
