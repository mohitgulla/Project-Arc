# BT-5 · E16.3 re-run: dropping directional long-premium candidates on stretched days does not pass the E16.3 recommend rule on cash_debit.

## Header

- phase **holdout**; spec hash `46e247a2a6e6`; arm set `b0111115e027`; owner owner; created 2026-10-15
- rule: Recommend the forward experiment only if the filtered run has a smaller max drawdown **and** a net P&L not lower in ≥ 2 of 3 trend sub-periods (bear, sideways, bull), **and** the 90% moving-block bootstrap CI of the daily P&L difference has a lower bound > −10% of the incumbent's |net P&L|.
- primary metric: `net_pnl`
- universe **c500** (ladder c125, c500), version `u500-20261009-s1-e52ff301`; data partitions {'thetadata': 500}
- decisions 2024-01-01 -> 2026-10-15 (exits settle by 2026-10-15); dev 2020-01-01 -> 2023-12-31; holdout 2024-01-01 -> latest
- Not replayed: Discovery and Trending tiers (no historical LLM, Reddit or Stocktwits inputs), the LLM stance (a deterministic trend-label proxy stands in), intraday paths (EOD marks only; exits fire on closes).
- git `28afa22611cd`; variants tried 1 of max 3; earlier holdout runs 0
- arms: control = ranker incumbent; t1 = ranker incumbent, overlay `{"ranking": {"backtest": {"entry_filter": "anti_chase"}}}`; t2 = ranker incumbent, overlay `{"ranking": {"backtest": {"entry_filter": "anti_chase_vwap"}}}`

## Verdict

The pre-registered rule, verbatim:

> Recommend the forward experiment only if the filtered run has a smaller max drawdown **and** a net P&L not lower in ≥ 2 of 3 trend sub-periods (bear, sideways, bull), **and** the 90% moving-block bootstrap CI of the daily P&L difference has a lower bound > −10% of the incumbent's |net P&L|.

Original card's wording: _Recommend the forward experiment only if, for cash_debit, the filtered run has a smaller max drawdown **and** a net P&L that is not lower in ≥ 2 of 3 trend sub-periods, **and** the 90 % moving-block (20-day) bootstrap CI of the daily P&L difference has a lower bound > −(10 % of the incumbent's |net P&L|)._

- VERDICT · cash_debit · t1 vs control: do not recommend (sub-periods 2 [bear,sideways], P&L diff -773, CI [-41,432, 38,988])
- VERDICT · cash_debit · t2 vs control: do not recommend (sub-periods 2 [bear,sideways], P&L diff -773, CI [-41,432, 38,988])

A verdict only recommends: a live change still goes through D44 (`Experiment: XP-<n>` with arms copied from this spec) or an owner waiver.

## Per arm × profile

| profile | arm | trades | net_pnl | return_pct | cagr | max_dd_pct | sharpe | sortino | profit_factor | win_rate | avg_days_held | turnover_per_month | cost_share_of_gross | capacity_clipped_share |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| cash_debit | control | 514 | -23,843.49 | -95.37 | -0.6684 | 97.67 | -0.124 | -0.144 | 0.857 | 0.504 | 14.3 | 15.38 | 37.12 | 0.358 |
| cash_debit | t1 | 321 | -24,616.48 | -98.47 | -0.7769 | 99.32 | -0.546 | -0.519 | 0.749 | 0.489 | 14.6 | 9.61 | 0.861 | 0.33 |
| cash_debit | t2 | 321 | -24,616.48 | -98.47 | -0.7769 | 99.32 | -0.546 | -0.519 | 0.749 | 0.489 | 14.6 | 9.61 | 0.861 | 0.33 |

## All / Stocks / ETFs

One pool per run; the verdict is stated on All, Stocks and ETFs are diagnostic. `dd_contribution` = net P&L of the trades closed inside the run's max-drawdown window; `spread_cost` = P&L at mid − net P&L (spread + fees).

| profile | arm | segment | trades | win_rate | net_pnl | mean_pnl | median_pnl | pnl_per_risk | dd_contribution | spread_cost | exit_reasons |
|---|---|---|---|---|---|---|---|---|---|---|---|
| cash_debit | control | All | 514 | 0.504 | -23,843.49 | -46.39 | 5.43 | -0.0569 | -47,646.59 | 23,217.99 | dte_exit 25, expiry 24, profit_lock 39, stop 190, take_profit 236 |
| cash_debit | control | Stocks | 334 | 0.47 | -25,672.34 | -76.86 | -16.58 | -0.0881 | -34,676.81 | 16,722.34 | dte_exit 21, expiry 24, profit_lock 23, stop 124, take_profit 142 |
| cash_debit | control | ETFs | 180 | 0.567 | 1,828.85 | 10.16 | 22.85 | 0.0143 | -12,969.79 | 6,495.65 | dte_exit 4, profit_lock 16, stop 66, take_profit 94 |
| cash_debit | t1 | All | 321 | 0.489 | -24,616.48 | -76.69 | -9.16 | -0.1045 | -40,993.28 | 11,389.48 | dte_exit 17, expiry 12, profit_lock 18, stop 125, take_profit 149 |
| cash_debit | t1 | Stocks | 188 | 0.463 | -16,518.58 | -87.86 | -36.83 | -0.1076 | -21,460.17 | 7,733.08 | dte_exit 13, expiry 10, profit_lock 11, stop 70, take_profit 84 |
| cash_debit | t1 | ETFs | 133 | 0.526 | -8,097.90 | -60.89 | 12.18 | -0.0988 | -19,533.11 | 3,656.40 | dte_exit 4, expiry 2, profit_lock 7, stop 55, take_profit 65 |
| cash_debit | t2 | All | 321 | 0.489 | -24,616.48 | -76.69 | -9.16 | -0.1045 | -40,993.28 | 11,389.48 | dte_exit 17, expiry 12, profit_lock 18, stop 125, take_profit 149 |
| cash_debit | t2 | Stocks | 188 | 0.463 | -16,518.58 | -87.86 | -36.83 | -0.1076 | -21,460.17 | 7,733.08 | dte_exit 13, expiry 10, profit_lock 11, stop 70, take_profit 84 |
| cash_debit | t2 | ETFs | 133 | 0.526 | -8,097.90 | -60.89 | 12.18 | -0.0988 | -19,533.11 | 3,656.40 | dte_exit 4, expiry 2, profit_lock 7, stop 55, take_profit 65 |

Treatment − control net P&L by segment (**sign differs between Stocks and ETFs for 2 treatment(s)**):

| profile | treatment | All | Stocks | ETFs | sign_differs |
|---|---|---|---|---|---|
| cash_debit | t1 | -772.99 | 9,153.76 | -9,926.75 | True |
| cash_debit | t2 | -772.99 | 9,153.76 | -9,926.75 | True |

## Exit-reason mix

| profile | arm | mix |
|---|---|---|
| cash_debit | control | dte_exit 0.0486, expiry 0.0467, profit_lock 0.0759, stop 0.3696, take_profit 0.4591 |
| cash_debit | t1 | dte_exit 0.053, expiry 0.0374, profit_lock 0.0561, stop 0.3894, take_profit 0.4642 |
| cash_debit | t2 | dte_exit 0.053, expiry 0.0374, profit_lock 0.0561, stop 0.3894, take_profit 0.4642 |

## Skip reasons

| profile | arm | screen | blackout | no_candidate | unsettled | sizing | gate_per_underlying | gate_max_open_positions | gate_daily_loss | gate_max_gain | cash | capacity | no_fill | exit_delayed |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| cash_debit | control | 226930 | 0 | 18691 | 221 | 2111 | 0 | 3031 | 9546 | 0 | 31 | 311 | 170 | 86 |
| cash_debit | t1 | 226930 | 0 | 20662 | 219 | 3279 | 0 | 1455 | 7929 | 0 | 28 | 197 | 72 | 41 |
| cash_debit | t2 | 226930 | 0 | 20665 | 219 | 3277 | 0 | 1451 | 7929 | 0 | 28 | 197 | 72 | 41 |

## Sub-periods

Fixed windows by entry date.

| profile | arm | period | trades | net_pnl | win_rate | max_dd_pct |
|---|---|---|---|---|---|---|
| cash_debit | control | 2020 crash | 0 | 0 | – | 0 |
| cash_debit | control | 2020-21 bull | 0 | 0 | – | 0 |
| cash_debit | control | 2022 bear | 0 | 0 | – | 0 |
| cash_debit | control | 2023 | 0 | 0 | – | 0 |
| cash_debit | control | 2024 -> latest | 514 | -23,843.49 | 0.504 | 97.67 |
| cash_debit | t1 | 2020 crash | 0 | 0 | – | 0 |
| cash_debit | t1 | 2020-21 bull | 0 | 0 | – | 0 |
| cash_debit | t1 | 2022 bear | 0 | 0 | – | 0 |
| cash_debit | t1 | 2023 | 0 | 0 | – | 0 |
| cash_debit | t1 | 2024 -> latest | 321 | -24,616.48 | 0.489 | 99.32 |
| cash_debit | t2 | 2020 crash | 0 | 0 | – | 0 |
| cash_debit | t2 | 2020-21 bull | 0 | 0 | – | 0 |
| cash_debit | t2 | 2022 bear | 0 | 0 | – | 0 |
| cash_debit | t2 | 2023 | 0 | 0 | – | 0 |
| cash_debit | t2 | 2024 -> latest | 321 | -24,616.48 | 0.489 | 99.32 |

## By regime

| profile | arm | key | trades | net_pnl | win_rate |
|---|---|---|---|---|---|
| cash_debit | control | bear | 155 | -28,140.64 | 0.406 |
| cash_debit | control | bull | 359 | 4,297.15 | 0.546 |
| cash_debit | t1 | bear | 92 | -22,428.76 | 0.391 |
| cash_debit | t1 | bull | 229 | -2,187.72 | 0.528 |
| cash_debit | t2 | bear | 92 | -22,428.76 | 0.391 |
| cash_debit | t2 | bull | 229 | -2,187.72 | 0.528 |

## By structure

| profile | arm | key | trades | net_pnl | win_rate |
|---|---|---|---|---|---|
| cash_debit | control | bear_put | 117 | -20,070.89 | 0.376 |
| cash_debit | control | bull_call | 205 | 3,903.30 | 0.551 |
| cash_debit | control | long_call | 154 | 393.85 | 0.539 |
| cash_debit | control | long_put | 38 | -8,069.75 | 0.5 |
| cash_debit | t1 | bear_put | 65 | -11,395.82 | 0.4 |
| cash_debit | t1 | bull_call | 134 | 2,003.34 | 0.59 |
| cash_debit | t1 | long_call | 95 | -4,191.06 | 0.442 |
| cash_debit | t1 | long_put | 27 | -11,032.94 | 0.37 |
| cash_debit | t2 | bear_put | 65 | -11,395.82 | 0.4 |
| cash_debit | t2 | bull_call | 134 | 2,003.34 | 0.59 |
| cash_debit | t2 | long_call | 95 | -4,191.06 | 0.442 |
| cash_debit | t2 | long_put | 27 | -11,032.94 | 0.37 |

## By universe tier

| profile | arm | key | trades | net_pnl | win_rate |
|---|---|---|---|---|---|
| cash_debit | control | core | 201 | -8,621.54 | 0.438 |
| cash_debit | control | momentum | 16 | -2,410.71 | 0.375 |
| cash_debit | control | rest | 297 | -12,811.24 | 0.556 |
| cash_debit | t1 | core | 114 | -15,074.99 | 0.43 |
| cash_debit | t1 | momentum | 8 | -703.61 | 0.375 |
| cash_debit | t1 | rest | 199 | -8,837.89 | 0.528 |
| cash_debit | t2 | core | 114 | -15,074.99 | 0.43 |
| cash_debit | t2 | momentum | 8 | -703.61 | 0.375 |
| cash_debit | t2 | rest | 199 | -8,837.89 | 0.528 |

## By sector

| profile | arm | key | trades | net_pnl | win_rate |
|---|---|---|---|---|---|
| cash_debit | control | broad_market | 70 | -5,245.86 | 0.386 |
| cash_debit | control | communication | 10 | 2,724.91 | 0.7 |
| cash_debit | control | consumer_discretionary | 32 | 369.91 | 0.406 |
| cash_debit | control | consumer_staples | 1 | 110.52 | 1 |
| cash_debit | control | energy | 6 | -2,811.32 | 0.5 |
| cash_debit | control | financials | 53 | 5,242.19 | 0.509 |
| cash_debit | control | health_care | 8 | -3,870.56 | 0.375 |
| cash_debit | control | industrials | 8 | 993.89 | 0.375 |
| cash_debit | control | technology | 111 | -1,351.12 | 0.505 |
| cash_debit | control | unknown | 215 | -20,006.03 | 0.553 |
| cash_debit | t1 | broad_market | 40 | -10,504.43 | 0.35 |
| cash_debit | t1 | communication | 5 | 2,060.36 | 1 |
| cash_debit | t1 | consumer_discretionary | 15 | -689.79 | 0.467 |
| cash_debit | t1 | consumer_staples | 1 | -628.48 | 0 |
| cash_debit | t1 | energy | 3 | -729.82 | 0 |
| cash_debit | t1 | financials | 38 | 221.87 | 0.553 |
| cash_debit | t1 | health_care | 6 | 997.63 | 0.5 |
| cash_debit | t1 | industrials | 3 | 4,899.56 | 0.667 |
| cash_debit | t1 | technology | 55 | -5,727.48 | 0.491 |
| cash_debit | t1 | unknown | 155 | -14,515.91 | 0.503 |
| cash_debit | t2 | broad_market | 40 | -10,504.43 | 0.35 |
| cash_debit | t2 | communication | 5 | 2,060.36 | 1 |
| cash_debit | t2 | consumer_discretionary | 15 | -689.79 | 0.467 |
| cash_debit | t2 | consumer_staples | 1 | -628.48 | 0 |
| cash_debit | t2 | energy | 3 | -729.82 | 0 |
| cash_debit | t2 | financials | 38 | 221.87 | 0.553 |
| cash_debit | t2 | health_care | 6 | 997.63 | 0.5 |
| cash_debit | t2 | industrials | 3 | 4,899.56 | 0.667 |
| cash_debit | t2 | technology | 55 | -5,727.48 | 0.491 |
| cash_debit | t2 | unknown | 155 | -14,515.91 | 0.503 |

## ETF vs single stock

| profile | arm | key | trades | net_pnl | win_rate |
|---|---|---|---|---|---|
| cash_debit | control | ETF | 180 | 1,828.85 | 0.567 |
| cash_debit | control | single stock | 334 | -25,672.34 | 0.47 |
| cash_debit | t1 | ETF | 133 | -8,097.90 | 0.526 |
| cash_debit | t1 | single stock | 188 | -16,518.58 | 0.463 |
| cash_debit | t2 | ETF | 133 | -8,097.90 | 0.526 |
| cash_debit | t2 | single stock | 188 | -16,518.58 | 0.463 |

## By liquidity bucket

| profile | arm | key | trades | net_pnl | win_rate |
|---|---|---|---|---|---|
| cash_debit | control | 100k-1M | 278 | -6,343.49 | 0.54 |
| cash_debit | control | 10k-100k | 94 | -22.81 | 0.521 |
| cash_debit | control | < 10k | 32 | -11,029.65 | 0.406 |
| cash_debit | control | >= 1M | 110 | -6,447.55 | 0.427 |
| cash_debit | t1 | 100k-1M | 182 | -3,823.99 | 0.5 |
| cash_debit | t1 | 10k-100k | 63 | 2,440.40 | 0.587 |
| cash_debit | t1 | < 10k | 21 | -8,436.45 | 0.333 |
| cash_debit | t1 | >= 1M | 55 | -14,796.44 | 0.4 |
| cash_debit | t2 | 100k-1M | 182 | -3,823.99 | 0.5 |
| cash_debit | t2 | 10k-100k | 63 | 2,440.40 | 0.587 |
| cash_debit | t2 | < 10k | 21 | -8,436.45 | 0.333 |
| cash_debit | t2 | >= 1M | 55 | -14,796.44 | 0.4 |

## Uncertainty

Moving-block bootstrap CI of the daily equity-change difference vs control (seeded), and the fixed sub-periods each treatment won on net P&L.

| profile | treatment | pnl_diff | ci_lo | ci_hi | periods_won |
|---|---|---|---|---|---|
| cash_debit | t1 | -772.99 | -41,432.23 | 38,988.25 | – |
| cash_debit | t2 | -772.99 | -41,432.23 | 38,988.25 | – |

## Checkpoint ladder

Every ledger run of this arm set; `sign_flip_vs_first_rung` compares the primary metric's treatment − control sign with the first dev rung.

| at | phase | universe | profile | treatment | primary_diff | ci_lo | ci_hi | switch | sign_flip_vs_first_rung |
|---|---|---|---|---|---|---|---|---|---|
| 2026-10-15T23:00:03-04:00 | dev | c125 | cash_debit | t1 | 5.15 | -42,695.01 | 37,460.06 | False | False |
| 2026-10-15T23:00:03-04:00 | dev | c125 | cash_debit | t2 | 2.99 | -41,275.46 | 34,164.93 | False | False |
| 2026-10-16T10:38:43-04:00 | dev | c500 | cash_debit | t1 | 5.51 | -79,703.59 | 78,349.53 | False | False |
| 2026-10-16T10:38:43-04:00 | dev | c500 | cash_debit | t2 | -8.37 | -83,706.83 | 84,927.11 | False | True |
| 2026-10-16T13:15:03-04:00 | holdout | c500 | cash_debit | t1 | -772.99 | -41,432.23 | 38,988.25 | False | True |
| 2026-10-16T13:15:03-04:00 | holdout | c500 | cash_debit | t2 | -772.99 | -41,432.23 | 38,988.25 | False | True |
