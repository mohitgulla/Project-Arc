# BT-5 · E16.3 re-run: dropping directional long-premium candidates on stretched days does not pass the E16.3 recommend rule on cash_debit.

## Header

- phase **dev**; spec hash `46e247a2a6e6`; arm set `b0111115e027`; owner owner; created 2026-10-15
- rule: Recommend the forward experiment only if the filtered run has a smaller max drawdown **and** a net P&L not lower in ≥ 2 of 3 trend sub-periods (bear, sideways, bull), **and** the 90% moving-block bootstrap CI of the daily P&L difference has a lower bound > −10% of the incumbent's |net P&L|.
- primary metric: `net_pnl`
- universe **c125** (ladder c125, c500), version `u500-20261009-s1-e52ff301`; data partitions {'thetadata': 125}
- decisions 2020-01-01 -> 2023-12-31 (exits settle by 2024-03-30); dev 2020-01-01 -> 2023-12-31; holdout 2024-01-01 -> latest
- Not replayed: Discovery and Trending tiers (no historical LLM, Reddit or Stocktwits inputs), the LLM stance (a deterministic trend-label proxy stands in), intraday paths (EOD marks only; exits fire on closes).
- git `05d2fddaffa6`; variants tried 1 of max 3; earlier holdout runs 0
- arms: control = ranker incumbent; t1 = ranker incumbent, overlay `{"ranking": {"backtest": {"entry_filter": "anti_chase"}}}`; t2 = ranker incumbent, overlay `{"ranking": {"backtest": {"entry_filter": "anti_chase_vwap"}}}`

## Verdict

The pre-registered rule, verbatim:

> Recommend the forward experiment only if the filtered run has a smaller max drawdown **and** a net P&L not lower in ≥ 2 of 3 trend sub-periods (bear, sideways, bull), **and** the 90% moving-block bootstrap CI of the daily P&L difference has a lower bound > −10% of the incumbent's |net P&L|.

Original card's wording: _Recommend the forward experiment only if, for cash_debit, the filtered run has a smaller max drawdown **and** a net P&L that is not lower in ≥ 2 of 3 trend sub-periods, **and** the 90 % moving-block (20-day) bootstrap CI of the daily P&L difference has a lower bound > −(10 % of the incumbent's |net P&L|)._

- dev result (not a verdict) · cash_debit · t1 vs control: do not recommend (sub-periods 2 [bear,sideways], P&L diff 5, CI [-42,695, 37,460])
- dev result (not a verdict) · cash_debit · t2 vs control: do not recommend (sub-periods 2 [bear,sideways], P&L diff 3, CI [-41,275, 34,165])

A verdict only recommends: a live change still goes through D44 (`Experiment: XP-<n>` with arms copied from this spec) or an owner waiver.

## Per arm × profile

| profile | arm | trades | net_pnl | return_pct | cagr | max_dd_pct | sharpe | sortino | profit_factor | win_rate | avg_days_held | turnover_per_month | cost_share_of_gross | capacity_clipped_share |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| cash_debit | control | 585 | -24,879.91 | -99.52 | -0.7165 | 99.74 | -0.28 | -0.284 | 0.87 | 0.504 | 14.7 | 11.51 | 6.399 | 0.354 |
| cash_debit | t1 | 389 | -24,874.76 | -99.5 | -0.7136 | 99.67 | -0.638 | -0.544 | 0.675 | 0.488 | 14.6 | 7.65 | 1.109 | 0.237 |
| cash_debit | t2 | 416 | -24,876.92 | -99.51 | -0.7148 | 99.69 | -0.641 | -0.573 | 0.705 | 0.498 | 14.6 | 8.18 | 1.237 | 0.24 |

## All / Stocks / ETFs

One pool per run; the verdict is stated on All, Stocks and ETFs are diagnostic. `dd_contribution` = net P&L of the trades closed inside the run's max-drawdown window; `spread_cost` = P&L at mid − net P&L (spread + fees).

| profile | arm | segment | trades | win_rate | net_pnl | mean_pnl | median_pnl | pnl_per_risk | dd_contribution | spread_cost | exit_reasons |
|---|---|---|---|---|---|---|---|---|---|---|---|
| cash_debit | control | All | 585 | 0.504 | -24,879.91 | -42.53 | 2.34 | -0.053 | -44,734.41 | 29,488.41 | dte_exit 22, expiry 18, profit_lock 34, stop 231, take_profit 280 |
| cash_debit | control | Stocks | 402 | 0.502 | -34,647.76 | -86.19 | 1.13 | -0.1151 | -39,129.42 | 17,017.76 | dte_exit 16, expiry 13, profit_lock 30, stop 153, take_profit 190 |
| cash_debit | control | ETFs | 183 | 0.508 | 9,767.85 | 53.38 | 34.42 | 0.0581 | -5,605.00 | 12,470.65 | dte_exit 6, expiry 5, profit_lock 4, stop 78, take_profit 90 |
| cash_debit | t1 | All | 389 | 0.488 | -24,874.76 | -63.95 | -2.82 | -0.1466 | -37,748.69 | 13,078.26 | dte_exit 15, expiry 12, profit_lock 27, stop 154, take_profit 181 |
| cash_debit | t1 | Stocks | 276 | 0.446 | -27,644.35 | -100.16 | -16.79 | -0.2714 | -30,145.05 | 5,259.85 | dte_exit 13, expiry 7, profit_lock 24, stop 116, take_profit 116 |
| cash_debit | t1 | ETFs | 113 | 0.593 | 2,769.59 | 24.51 | 64.09 | 0.0408 | -7,603.64 | 7,818.41 | dte_exit 2, expiry 5, profit_lock 3, stop 38, take_profit 65 |
| cash_debit | t2 | All | 416 | 0.498 | -24,876.92 | -59.8 | -0.79 | -0.131 | -39,990.16 | 13,758.42 | dte_exit 13, expiry 15, profit_lock 21, stop 165, take_profit 202 |
| cash_debit | t2 | Stocks | 290 | 0.448 | -31,068.14 | -107.13 | -16.79 | -0.272 | -33,787.43 | 5,736.14 | dte_exit 12, expiry 11, profit_lock 18, stop 123, take_profit 126 |
| cash_debit | t2 | ETFs | 126 | 0.611 | 6,191.22 | 49.14 | 45.21 | 0.0818 | -6,202.73 | 8,022.28 | dte_exit 1, expiry 4, profit_lock 3, stop 42, take_profit 76 |

Treatment − control net P&L by segment (**sign differs between Stocks and ETFs for 2 treatment(s)**):

| profile | treatment | All | Stocks | ETFs | sign_differs |
|---|---|---|---|---|---|
| cash_debit | t1 | 5.15 | 7,003.41 | -6,998.26 | True |
| cash_debit | t2 | 2.99 | 3,579.62 | -3,576.63 | True |

## Exit-reason mix

| profile | arm | mix |
|---|---|---|
| cash_debit | control | dte_exit 0.0376, expiry 0.0308, profit_lock 0.0581, stop 0.3949, take_profit 0.4786 |
| cash_debit | t1 | dte_exit 0.0386, expiry 0.0308, profit_lock 0.0694, stop 0.3959, take_profit 0.4653 |
| cash_debit | t2 | dte_exit 0.0312, expiry 0.0361, profit_lock 0.0505, stop 0.3966, take_profit 0.4856 |

## Skip reasons

| profile | arm | screen | blackout | no_candidate | unsettled | sizing | gate_per_underlying | gate_max_open_positions | gate_daily_loss | gate_max_gain | cash | capacity | no_fill | exit_delayed |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| cash_debit | control | 71615 | 0 | 24610 | 0 | 5693 | 0 | 3633 | 11245 | 0 | 64 | 492 | 196 | 52 |
| cash_debit | t1 | 71615 | 0 | 27223 | 0 | 7651 | 0 | 1356 | 8933 | 0 | 74 | 265 | 137 | 34 |
| cash_debit | t2 | 71615 | 0 | 26722 | 0 | 7452 | 0 | 1244 | 9578 | 0 | 61 | 259 | 143 | 38 |

## Sub-periods

Fixed windows by entry date.

| profile | arm | period | trades | net_pnl | win_rate | max_dd_pct |
|---|---|---|---|---|---|---|
| cash_debit | control | 2020 crash | 53 | -18,862.39 | 0.528 | 65.98 |
| cash_debit | control | 2020-21 bull | 317 | -11,573.07 | 0.502 | 92.84 |
| cash_debit | control | 2022 bear | 144 | -2,902.56 | 0.521 | 94.42 |
| cash_debit | control | 2023 | 44 | -490.34 | 0.318 | 85.59 |
| cash_debit | control | 2024 -> latest | 0 | 0 | – | 0 |
| cash_debit | t1 | 2020 crash | 42 | -26,959.22 | 0.452 | 78.16 |
| cash_debit | t1 | 2020-21 bull | 222 | -6,650.23 | 0.491 | 94.87 |
| cash_debit | t1 | 2022 bear | 77 | -486.67 | 0.481 | 70.05 |
| cash_debit | t1 | 2023 | 19 | -298.57 | 0.316 | 74.22 |
| cash_debit | t1 | 2024 -> latest | 0 | 0 | – | 0 |
| cash_debit | t2 | 2020 crash | 41 | -28,548.53 | 0.439 | 78.31 |
| cash_debit | t2 | 2020-21 bull | 242 | -7,402.77 | 0.492 | 96.15 |
| cash_debit | t2 | 2022 bear | 67 | -127.72 | 0.552 | 53.69 |
| cash_debit | t2 | 2023 | 36 | -338.56 | 0.361 | 83.88 |
| cash_debit | t2 | 2024 -> latest | 0 | 0 | – | 0 |

## By regime

| profile | arm | key | trades | net_pnl | win_rate |
|---|---|---|---|---|---|
| cash_debit | control | bear | 138 | -34,194.76 | 0.377 |
| cash_debit | control | bull | 447 | 9,314.85 | 0.544 |
| cash_debit | t1 | bear | 81 | -21,591.97 | 0.432 |
| cash_debit | t1 | bull | 308 | -3,282.79 | 0.503 |
| cash_debit | t2 | bear | 80 | -23,540.11 | 0.412 |
| cash_debit | t2 | bull | 336 | -1,336.81 | 0.518 |

## By structure

| profile | arm | key | trades | net_pnl | win_rate |
|---|---|---|---|---|---|
| cash_debit | control | bear_put | 122 | -19,174.39 | 0.369 |
| cash_debit | control | bull_call | 190 | -3,610.58 | 0.532 |
| cash_debit | control | long_call | 257 | 12,925.43 | 0.553 |
| cash_debit | control | long_put | 16 | -15,020.37 | 0.438 |
| cash_debit | t1 | bear_put | 68 | -3,157.80 | 0.471 |
| cash_debit | t1 | bull_call | 157 | -5,349.99 | 0.554 |
| cash_debit | t1 | long_call | 151 | 2,067.20 | 0.45 |
| cash_debit | t1 | long_put | 13 | -18,434.17 | 0.231 |
| cash_debit | t2 | bear_put | 64 | -3,891.17 | 0.453 |
| cash_debit | t2 | bull_call | 176 | -4,025.72 | 0.562 |
| cash_debit | t2 | long_call | 160 | 2,688.91 | 0.469 |
| cash_debit | t2 | long_put | 16 | -19,648.94 | 0.25 |

## By universe tier

| profile | arm | key | trades | net_pnl | win_rate |
|---|---|---|---|---|---|
| cash_debit | control | core | 258 | -12,954.57 | 0.446 |
| cash_debit | control | momentum | 32 | -7,769.23 | 0.469 |
| cash_debit | control | rest | 295 | -4,156.11 | 0.559 |
| cash_debit | t1 | core | 147 | -20,833.80 | 0.422 |
| cash_debit | t1 | momentum | 32 | -1,834.42 | 0.469 |
| cash_debit | t1 | rest | 210 | -2,206.53 | 0.538 |
| cash_debit | t2 | core | 153 | -19,818.36 | 0.438 |
| cash_debit | t2 | momentum | 29 | -2,346.97 | 0.552 |
| cash_debit | t2 | rest | 234 | -2,711.59 | 0.53 |

## By sector

| profile | arm | key | trades | net_pnl | win_rate |
|---|---|---|---|---|---|
| cash_debit | control | broad_market | 106 | 10,632.61 | 0.472 |
| cash_debit | control | communication | 50 | 971.23 | 0.56 |
| cash_debit | control | consumer_discretionary | 11 | 907.52 | 0.636 |
| cash_debit | control | consumer_staples | 5 | 134.46 | 0.6 |
| cash_debit | control | energy | 27 | 5,946.57 | 0.704 |
| cash_debit | control | financials | 76 | -13,244.92 | 0.382 |
| cash_debit | control | health_care | 21 | 1,250.81 | 0.667 |
| cash_debit | control | industrials | 14 | -3,550.41 | 0.429 |
| cash_debit | control | technology | 107 | -11,304.15 | 0.477 |
| cash_debit | control | unknown | 168 | -16,623.64 | 0.524 |
| cash_debit | t1 | broad_market | 44 | 1,027.04 | 0.568 |
| cash_debit | t1 | communication | 32 | 644.25 | 0.594 |
| cash_debit | t1 | consumer_discretionary | 7 | -3,530.96 | 0.857 |
| cash_debit | t1 | consumer_staples | 7 | -111.85 | 0.429 |
| cash_debit | t1 | energy | 14 | 192.32 | 0.5 |
| cash_debit | t1 | financials | 73 | -10,793.59 | 0.329 |
| cash_debit | t1 | health_care | 9 | 389.83 | 0.778 |
| cash_debit | t1 | industrials | 7 | -55.1 | 0.286 |
| cash_debit | t1 | technology | 70 | -11,245.81 | 0.386 |
| cash_debit | t1 | unknown | 126 | -1,390.88 | 0.556 |
| cash_debit | t2 | broad_market | 49 | 2,640.77 | 0.531 |
| cash_debit | t2 | communication | 36 | 32.48 | 0.556 |
| cash_debit | t2 | consumer_discretionary | 6 | -3,542.88 | 0.667 |
| cash_debit | t2 | consumer_staples | 5 | 32.07 | 0.6 |
| cash_debit | t2 | energy | 13 | -88.77 | 0.538 |
| cash_debit | t2 | financials | 81 | -9,328.68 | 0.37 |
| cash_debit | t2 | health_care | 8 | 198.50 | 0.625 |
| cash_debit | t2 | industrials | 8 | -400.39 | 0.25 |
| cash_debit | t2 | technology | 73 | -13,015.66 | 0.425 |
| cash_debit | t2 | unknown | 137 | -1,404.37 | 0.577 |

## ETF vs single stock

| profile | arm | key | trades | net_pnl | win_rate |
|---|---|---|---|---|---|
| cash_debit | control | ETF | 183 | 9,767.85 | 0.508 |
| cash_debit | control | single stock | 402 | -34,647.76 | 0.502 |
| cash_debit | t1 | ETF | 113 | 2,769.59 | 0.593 |
| cash_debit | t1 | single stock | 276 | -27,644.35 | 0.446 |
| cash_debit | t2 | ETF | 126 | 6,191.22 | 0.611 |
| cash_debit | t2 | single stock | 290 | -31,068.14 | 0.448 |

## By liquidity bucket

| profile | arm | key | trades | net_pnl | win_rate |
|---|---|---|---|---|---|
| cash_debit | control | 100k-1M | 319 | -24,218.29 | 0.489 |
| cash_debit | control | 10k-100k | 170 | 14,789.09 | 0.594 |
| cash_debit | control | < 10k | 24 | -8,334.74 | 0.458 |
| cash_debit | control | >= 1M | 72 | -7,115.97 | 0.375 |
| cash_debit | t1 | 100k-1M | 224 | -24,405.92 | 0.478 |
| cash_debit | t1 | 10k-100k | 123 | 4,768.15 | 0.52 |
| cash_debit | t1 | < 10k | 14 | -3,758.51 | 0.357 |
| cash_debit | t1 | >= 1M | 28 | -1,478.49 | 0.5 |
| cash_debit | t2 | 100k-1M | 239 | -22,164.03 | 0.494 |
| cash_debit | t2 | 10k-100k | 131 | 2,337.93 | 0.534 |
| cash_debit | t2 | < 10k | 13 | -2,302.89 | 0.385 |
| cash_debit | t2 | >= 1M | 33 | -2,747.94 | 0.424 |

## Uncertainty

Moving-block bootstrap CI of the daily equity-change difference vs control (seeded), and the fixed sub-periods each treatment won on net P&L.

| profile | treatment | pnl_diff | ci_lo | ci_hi | periods_won |
|---|---|---|---|---|---|
| cash_debit | t1 | 5.15 | -42,695.01 | 37,460.06 | 2020-21 bull, 2022 bear, 2023 |
| cash_debit | t2 | 2.99 | -41,275.46 | 34,164.93 | 2020-21 bull, 2022 bear, 2023 |

## Checkpoint ladder

Every ledger run of this arm set; `sign_flip_vs_first_rung` compares the primary metric's treatment − control sign with the first dev rung.

| at | phase | universe | profile | treatment | primary_diff | ci_lo | ci_hi | switch | sign_flip_vs_first_rung |
|---|---|---|---|---|---|---|---|---|---|
| 2026-10-15T23:00:03-04:00 | dev | c125 | cash_debit | t1 | 5.15 | -42,695.01 | 37,460.06 | False | False |
| 2026-10-15T23:00:03-04:00 | dev | c125 | cash_debit | t2 | 2.99 | -41,275.46 | 34,164.93 | False | False |
