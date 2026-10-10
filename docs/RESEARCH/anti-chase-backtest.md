# E16.3 · Anti-chase entry filter: walk-forward backtest

Status: run 2026-10-10 on main 7d99da7 + this card. Code: `arc/backtest/entry_filter.py`
(filter, OHLC cache, decision rule), `backtest.entry_filter` in `config/ranking.yaml`,
the live rule `arc.features.technicals.is_stretched`. Plan ref: D76, D78, D44.

Reproduce (three runs, ~45 min in parallel on 12 CPUs; ~15 min alone on 6 workers):

    for f in none anti_chase anti_chase_vwap; do
      uv run arc backtest rank --profile margin --profile cash_debit \
          --from 2024-03-01 --to 2026-07-31 \
          --tickers SPY,QQQ,IWM,AAPL,NVDA,TSLA --workers 4 \
          --data-dir ~/GitHub/Project-Arc/data --out data/backtest/e163/$f \
          --entry-filter $f
    done

The first run without `--offline` caches split-adjusted daily OHLC (+ bar VWAP) under
`<data-dir>/underlying_ohlc/` (one Alpaca request per ticker).

## TL;DR

- **Verdict (pre-declared rule): do not recommend the forward experiment.** No
  `cash_debit` ranker passes: the filter cuts the incumbent's max drawdown
  ($55.6k → $48.1k), but it costs **−$10.4k** net P&L (−$16.6k → −$26.9k), and the 90 %
  bootstrap CI of the daily P&L difference is **[−$32.3k, +$10.7k]**. The lower bound is
  far below the rule's floor of −$1.7k (10 % of the incumbent's |net P&L|).
- **Where the loss comes from:** the filter removed 15 of the incumbent's 164 trades.
  13 were bull-trend calls worth **+$14.7k** together, mostly take-profit winners on
  strong trends (AAPL Jun–Jul 2024, QQQ/NVDA Jun 2024). The 2 bear-trend puts it
  removed were losers (−$8.2k), and the replacement trades lost $4.9k. Bull sub-period
  P&L fell from +$27.7k to +$12.4k; bear improved −$44.3k → −$39.3k. In this sample a
  2.5-ATR, RSI-75 move kept going more often than it reversed, so the stretched names
  were momentum, not a top.
- **`margin` is untouched** (identical runs): its stances map to credit spreads,
  which the filter never touches (live rule too).
- **VWAP arm:** same result except `managed_net_ev` (−$21.4k vs −$30.8k with the daily
  rule only; still no pass). The extra VWAP condition at the close fired on 4 more
  decision days than the daily rule alone (124 vs 120 ticker-days).
- **Live store replay (12 closed opens, descriptive only):** the D78 rule and the D76
  `any` rule drop **none** of them. The closest call was CRWD on 2026-10-06 at +2.48
  ATR, RSI 70 (−$3.0k and −$0.9k), just under both limits. META (−$8.8k) was at +1.3
  ATR, RSI 63: the big live losses were not chases by this definition.
- **This conflicts with D78**, which already ships `personas.anti_chase: on` without an
  experiment. Flipping it is the owner's call (D78 waiver); this card changes no
  default. The rollback is `arc config set personas.anti_chase off` (the pipeline is
  then exactly the pre-E16.3 one). The draft XP-13 has a t1 arm (both D78 switches
  off) that measures this forward.

## Method

Run A of [ranking-backtest.md](ranking-backtest.md), unchanged except the filter:

| | |
|---|---|
| Universe | SPY, QQQ, IWM, AAPL, NVDA, TSLA |
| Decisions | Daily at the close, 2024-03-01 → 2026-07-31 (606 sessions per ticker), top-1 per ticker |
| Stance | 20-session trend label at the decision close (bull → bullish, bear → bearish, sideways → neutral; `cash_debit` has no neutral structure) |
| Marks / costs / exits | smile marks, x = 0.25 (`config/costs.yaml`), `config/exits.yaml` **as on main today** (D78/E18.1: debit TP 0.60, profit lock), so the `cash_debit` baseline differs from the 2026-09-28 Run A table |
| Filter | after the stance and before any ranker picks: on a day the trend stance is stretched, the menu loses its long-premium candidates (long call/put, debit verticals). Credit kinds and sideways days pass |
| Stretch | `is_stretched` (the live rule, D78 defaults: `combine: all`, stretch ≥ 2.5 ATR over SMA20 **and** RSI14 ≥ 75; bears mirror). Daily OHLC **≤ the decision day only** (a test changes every later bar and asserts the same verdicts), split-adjusted so a split is not a crash |
| VWAP arm | also stretched when (close − the day's bar VWAP) / ATR14 ≥ 0.75 (mirrored): the live VWAP stretch evaluated at the close, the backtest's only decision time |

**Decision rule (fixed in the card before the run):** recommend the forward experiment
only if, for `cash_debit`, the filtered run has a smaller max drawdown **and** a net
P&L that is not lower in ≥ 2 of 3 trend sub-periods, **and** the 90 % moving-block
(20-day) bootstrap CI of the daily P&L difference has a lower bound > −(10 % of the
incumbent's |net P&L|). (`EntryFilterRule`, `entry_filter_challenge`.)

## Results (`cash_debit`, x = 0.25)

| Ranker | Trades none → filtered | Net P&L none → filtered | Max DD none → filtered | Sub-period P&L bear/sideways/bull: none → filtered | Not lower | CI of daily diff | Floor | Pass |
|---|---|---|---|---|---|---|---|---|
| **debit_width** (incumbent) | 164 → 157 | −16,583 → −26,939 | 55,596 → 48,102 | −44,291/0/27,708 → −39,324/0/12,385 | bear, sideways | [−32,257, 10,732] | −1,658 | no |
| ev_proxy | 159 → 151 | −10,874 → −24,407 | 64,909 → 61,171 | −36,776/0/25,902 → −34,064/0/9,657 | bear, sideways | [−36,318, 8,187] | −1,087 | no |
| managed_net_ev | 145 → 138 | −18,393 → −30,836 | 44,032 → 46,021 | −35,382/0/16,989 → −29,891/0/−945 | bear, sideways | [−38,456, 9,583] | −1,839 | no |
| rorc_day | 169 → 164 | −1,981 → −12,299 | 61,734 → 59,518 | −42,633/0/40,652 → −36,251/0/23,952 | bear, sideways | [−28,390, 15,236] | −198 | no |
| rorc_day_vrp | 61 → 56 | −4,647 → +1,034 | 39,245 → 34,785 | −11,648/0/7,002 → −2,823/0/3,857 | bear, sideways | [−9,107, 22,878] | −465 | no |

`anti_chase_vwap` gives the same rows except `managed_net_ev`: 140 trades, −21,411,
DD 42,569, CI [−26,155, 21,292], no pass. `margin`: every ranker identical with the
filter on (0 filtered days), as designed.

"Sideways not lower" is trivially true (0 = 0: `cash_debit` does not trade sideways),
so the sub-period leg of the rule really rests on bear (filter better) vs bull (filter
worse). Only `rorc_day_vrp` gains P&L (+$5.7k on 61 trades), and its CI still fails the
floor. One ranker on few trades is not a basis for a recommendation.

## Live store replay (descriptive)

The 12 closed live opens (2026-10-01 → 10-08; the store's `outcomes` joined to
`proposals` and `candidates`), technicals recomputed from split-adjusted daily bars
through the close before entry (the store has no technicals at entry: E16.2 landed
after them):

| Day | Ticker | Stance | P&L | stretch ATR | RSI14 | D78 | D76 any |
|---|---|---|---|---|---|---|---|
| 10-01 | IWM | bearish | −20 | −2.49 | 29.7 | ok | ok |
| 10-01 | SPY | bearish | −23 | −0.37 | 47.8 | ok | ok |
| 10-01 | NFLX | neutral | −70 | −2.65 | 35.7 | n/a | n/a |
| 10-06 | CRWD ×2 | bullish | −3,045 / −935 | +2.48 | 69.7 | ok | ok |
| 10-06 | INTC | bullish | −662 | +0.59 | 56.5 | ok | ok |
| 10-06 | MRVL | bullish | −410 | +1.76 | 64.2 | ok | ok |
| 10-06 | VST | bullish | +160 | +0.54 | 55.6 | ok | ok |
| 10-07 | META ×2 | bullish | −8,765 / −75 | +1.27 | 63.0 | ok | ok |
| 10-08 | TSM | bullish | −90 | +2.31 | 64.5 | ok | ok |
| 10-08 | HOOD | bearish | −30 | −0.94 | 45.7 | ok | ok |

Net −$13,965; dropped by either rule: 0. n = 12 is far too few to prove anything.

## Caveats

Same as the ranking report: estimated spreads (no quotes), EOD decisions and marks
only (stops and take-profits on closes), the trend label is a deterministic proxy for
Research's stance (the live filter acts on Research's picks, which this cannot
replay), 6 liquid names, one 29-month window dominated by a bull market (which favours
not filtering momentum). The VWAP arm uses the daily bar's VWAP at the close; the live
check runs intraday on 5-min bars, so it is only an approximation of the live input.
Positions block re-entry on the same underlying, so a filtered day can shift a trade
to a later day (8 replacement trades for the incumbent) instead of removing it.
