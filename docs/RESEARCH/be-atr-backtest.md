# E16.5 · Breakeven in ATR terms: walk-forward backtest (descriptive)

Status: run 2026-10-10 on main 266d98e + this card. Code: `arc/scanner/be_atr.py`
(the live filter, pure), `apply_be_filter` / `atr14_days` in
`arc/backtest/entry_filter.py`, `backtest.max_be_atr` in `config/ranking.yaml`. Plan ref:
D76, D44.

Reproduce (each ~35 min on a busy host with 3 workers; `--offline` reads the
E16.3 OHLC cache in `<data-dir>/underlying_ohlc/`):

    for x in "" "--max-be-atr 2.0" "--max-be-atr 1.5" "--max-be-atr 0.5"; do
      uv run arc backtest rank --profile margin --profile cash_debit \
          --from 2024-03-01 --to 2026-07-31 \
          --tickers SPY,QQQ,IWM,AAPL,NVDA,TSLA --workers 3 --offline --no-charts \
          --data-dir ~/GitHub/Project-Arc/data --out data/backtest/e165/<name> $x
    done

## Measure

`be_atr = |BE − spot| / (ATR14 × √DTE)` for the breakeven in the trade's direction
(the highest for a call debit, the lowest for a put debit). Credit structures have no
single direction and are never filtered. ATR14 comes from split-adjusted daily OHLC
≤ the decision day, rescaled into the chain's raw price units (raw close ÷ adjusted
close). DTE is calendar days (the same days the card's σ distance uses), so √DTE is
about 1.2× the √(trading days) a pure random walk would use. A given limit is
therefore about 20 % looser than it reads.

## TL;DR

- **At the card's thresholds (1.5 and 2.0) the filter is inert.** Neither run dropped
  a single candidate. Both runs match the unfiltered Run A trade for trade on every
  ranker in both profiles, as the table below shows.
- **Why:** the Run A menus (and the recorded live chains below) buy near-the-money
  strikes at 30–45 DTE. Their breakevens sit about 0.25 ATR√t from spot (median), with
  a 99th percentile of 0.54–0.90 and a maximum of 1.27, a single TSLA bull call debit.
  A move of 1.5 ATR√t is several times further than anything the scanner offers.
- **At 0.5 (the registry minimum), descriptive only:** the filter drops 1,049 of about
  10,400 debit candidates (mostly NVDA/TSLA wide bull-call debits and far long calls)
  and changes 3–6 trades per ranker. Net P&L goes up for 4 of 5 rankers (the
  incumbent `debit_width` −$16.6k → −$9.7k) and down for `rorc_day` (−$2.0k → −$5.6k).
  All of the change is in the bull sub-period, and only a handful of trades differ, so
  it is noise-level: no bootstrap or pre-declared rule was run (descriptive by card).
- **XP-13 arm: not recommended.** At the specced thresholds it is a no-op, and at 0.5
  the effect is a handful of trades of either sign. A forward arm would spend
  experiment capacity on a filter that almost never fires on the structures we
  actually trade. The card value stays: it is shown per breakeven on the approval
  card and the Tower as context.

## Results

### Run A, off / 2.0 / 1.5 (both profiles, x = 0.25)

`cash_debit`

| Ranker | Trades | Net P&L | Max DD | Win rate | Sortino |
|---|---|---|---|---|---|
| debit_width (incumbent) | 164 / 164 / 164 | −16,583 (all three) | 55,596 (all three) | 52 % | 0.05 |
| ev_proxy | 159 / 159 / 159 | −10,874 | 64,909 | 53 % | 0.08 |
| managed_net_ev | 145 / 145 / 145 | −18,393 | 44,032 | 52 % | −0.02 |
| rorc_day | 169 / 169 / 169 | −1,981 | 61,734 | 54 % | 0.23 |
| rorc_day_vrp | 61 / 61 / 61 | −4,647 | 39,245 | 54 % | 0.04 |

`margin` (credit structures only, never filtered): identical across the three runs
(e.g. `credit_width` 135 trades, −$24,368, max DD $42,949).

`be_filter_drops.csv`: 0 candidates dropped for every ticker and profile at 2.0 and 1.5.

### Distribution of `be_atr` over every debit candidate (Run A `cash_debit` menus)

| Ticker | Debit candidates | Median | p90 | p99 | Max | Widest kind (max) |
|---|---|---|---|---|---|---|
| SPY | 666 | 0.25 | 0.41 | 0.56 | 0.65 | bear_put 0.65 |
| QQQ | 1,164 | 0.27 | 0.46 | 0.58 | 0.71 | bull_call 0.71 |
| IWM | 1,461 | 0.27 | 0.44 | 0.54 | 0.63 | bull_call 0.63 |
| AAPL | 1,862 | 0.26 | 0.45 | 0.60 | 0.72 | bull_call 0.72 |
| NVDA | 2,475 | 0.28 | 0.56 | 0.78 | 0.89 | bull_call 0.89 |
| TSLA | 2,820 | 0.27 | 0.57 | 0.90 | 1.27 | bull_call 1.27 |

By kind, the medians are long put ≈ 0.15, bear put ≈ 0.20, long call ≈ 0.28 and bull
call ≈ 0.26–0.46. Bull call debits are the furthest, because their long strike sits
OTM and the debit adds to it.

### Run A `cash_debit`, off → 0.5 (descriptive)

| Ranker | Trades | Net P&L | Max DD | Win rate | Sortino |
|---|---|---|---|---|---|
| debit_width (incumbent) | 164 → 159 | −16,583 → −9,740 | 55,596 → 50,931 | 52 % → 53 % | 0.05 → 0.13 |
| ev_proxy | 159 → 154 | −10,874 → +206 | 64,909 → 60,616 | 53 % → 55 % | 0.08 → 0.21 |
| managed_net_ev | 145 → 141 | −18,393 → −15,516 | 44,032 → 44,032 | 52 % → 52 % | −0.02 → 0.02 |
| rorc_day | 169 → 163 | −1,981 → −5,606 | 61,734 → 61,983 | 54 % → 54 % | 0.23 → 0.18 |
| rorc_day_vrp | 61 → 58 | −4,647 → +3,100 | 39,245 → 35,352 | 54 % → 55 % | 0.04 → 0.16 |

Candidates dropped at 0.5: TSLA 423, NVDA 386, AAPL 99, QQQ 68, IWM 52, SPY 21
(1,049 total). Sub-periods for the incumbent: bear −$44.3k → −$45.9k (59 trades
either way), bull +$27.7k → +$36.2k (105 → 100 trades). For `rorc_day`: bear
−$42.6k → −$41.6k, bull +$40.7k → +$36.0k.

## Live check (descriptive)

`arc chains <T> --fixture … --db <scratch copy> --profile cash_debit` with ATR14 from
Alpaca daily bars at 2026-09-25 (the recorded chains' day):

- NVDA long calls: 0.3 to 0.5 ATR√t (ATR14 $5.83).
- PLTR long calls: 0.2 to 0.5.
- SPY bear put debits: about 0.0, because their long strike is ITM at the spot.

`--max-be-atr 0.7` drops 0 of 9 NVDA candidates. The fixture SPY iron condor card
shows `BE 743.34 (-3.6%, 0.9σ, 1.1 ATR√t)`. Each breakeven of a credit structure is
still shown on the card; it is only the filter that ignores credit structures.

## Caveats

- One decision per ticker per day at the close, with a smile-marked chain. The
  intraday spot moves and the live menu's strike grid can differ.
- Calendar-day √DTE (see Measure). Under a trading-day convention every value would be
  about 1.2× larger. Even so, the 99th percentile stays under 1.1 and 1.5 still drops
  close to nothing.
- The 0.5 run changes 3–6 trades per ranker, so its P&L differences are far inside
  run-to-run noise. No bootstrap was run (descriptive by card).
