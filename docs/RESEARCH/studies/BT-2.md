# BT-2 · E7.2 / D4 re-run: the shipped managed-exit policy (config/exits.yaml) does not beat hold-to-expiry on the D4 menus (structure x delta) once real closing quotes replace estimated spreads.

## Header

- phase **holdout**; spec hash `a5bc56346dc9`; arm set `8020aae9714a`; owner owner; created 2026-10-15
- rule: Switch the default to a challenger only if it beats the incumbent on net P&L (higher) **and** max drawdown (not larger) in ≥ 2 of 3 sub-periods (bear, sideways, bull trend regime at entry), **and** the 90% block-bootstrap CI of its daily P&L difference vs the incumbent excludes 0 (lower bound > 0). Otherwise keep the incumbent and report the closest challenger.
- primary metric: `net_pnl`
- universe **c125** (ladder c125), version `u500-20261009-s1-e52ff301`; data partitions {'thetadata': 125}
- decisions 2024-01-01 -> 2026-10-15 (exits settle by 2026-10-15); dev 2020-01-01 -> 2023-12-31; holdout 2024-01-01 -> latest
- Not replayed: Discovery and Trending tiers (no historical LLM, Reddit or Stocktwits inputs), the LLM stance (a deterministic trend-label proxy stands in), intraday paths (EOD marks only; exits fire on closes).
- git `28afa22611cd`; variants tried 1 of max 3; earlier holdout runs 0
- arms: control = ranker incumbent, overlay `{"exits": {"default": {"close_at_dte": null, "profit_lock": null, "take_profit_pct_of_debit": null, "take_profit_pct_of_max_gain": null}, "kinds": {"iron_condor": {"close_at_dte": null, "profit_lock": null, "stop": null, "take_profit_pct_of_max_gain": null}, "long_call": {"close_at_dte": null, "profit_lock": null, "stop": null, "take_profit_pct_of_debit": null}, "long_put": {"close_at_dte": null, "profit_lock": null, "stop": null, "take_profit_pct_of_debit": null}, "vertical_credit": {"close_at_dte": null, "profit_lock": null, "stop": null, "take_profit_pct_of_max_gain": null}, "vertical_debit": {"close_at_dte": null, "profit_lock": null, "stop": null, "take_profit_pct_of_debit": null}}}}`; t1 = ranker incumbent

## Verdict

The pre-registered rule, verbatim:

> Switch the default to a challenger only if it beats the incumbent on net P&L (higher) **and** max drawdown (not larger) in ≥ 2 of 3 sub-periods (bear, sideways, bull trend regime at entry), **and** the 90% block-bootstrap CI of its daily P&L difference vs the incumbent excludes 0 (lower bound > 0). Otherwise keep the incumbent and report the closest challenger.

Original card's wording: _D4 (PLAN §0): "management rules don't beat hold-to-expiry statistically; 21-DTE exits only matter for undefined-risk; defined-risk caps tails structurally." E7.2 had no pre-registered switch rule, so the D25 rule is applied with hold-to-expiry as the incumbent: managed exits "beat" hold only if they pass it._

- VERDICT · margin · t1 vs control: keep control (sub-periods 2 [bear,sideways], P&L diff 9,673, CI [-25,646, 40,864])
- VERDICT · cash_debit · t1 vs control: keep control (sub-periods 1 [bear], P&L diff -37,115, CI [-300,382, 264,689])

A verdict only recommends: a live change still goes through D44 (`Experiment: XP-<n>` with arms copied from this spec) or an owner waiver.

## Per arm × profile

| profile | arm | trades | net_pnl | return_pct | cagr | max_dd_pct | sharpe | sortino | profit_factor | win_rate | avg_days_held | turnover_per_month | cost_share_of_gross | capacity_clipped_share |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| margin | control | 224 | -23,754.01 | -95.02 | -0.6594 | 95.88 | -0.807 | -0.679 | 0.722 | 0.567 | 36.5 | 6.7 | 0.424 | 0.366 |
| margin | t1 | 297 | -14,081.16 | -56.32 | -0.2573 | 76.87 | -0.341 | -0.309 | 0.804 | 0.684 | 25 | 8.89 | 1.778 | 0.34 |
| cash_debit | control | 270 | 16,378.47 | 65.51 | 0.1984 | 85.47 | 0.91 | 1.147 | 1.028 | 0.252 | 34.7 | 8.08 | 0.663 | 0.737 |
| cash_debit | t1 | 546 | -20,736.85 | -82.95 | -0.4702 | 95.93 | 0.398 | 0.626 | 0.89 | 0.502 | 14 | 16.34 | 4.465 | 0.368 |

## All / Stocks / ETFs

One pool per run; the verdict is stated on All, Stocks and ETFs are diagnostic. `dd_contribution` = net P&L of the trades closed inside the run's max-drawdown window; `spread_cost` = P&L at mid − net P&L (spread + fees).

| profile | arm | segment | trades | win_rate | net_pnl | mean_pnl | median_pnl | pnl_per_risk | dd_contribution | spread_cost | exit_reasons |
|---|---|---|---|---|---|---|---|---|---|---|---|
| margin | control | All | 224 | 0.567 | -23,754.01 | -106.04 | 45.05 | -0.1032 | -24,413.57 | 7,076.01 | expiry 224 |
| margin | control | Stocks | 105 | 0.552 | -16,426.39 | -156.44 | 32.34 | -0.1513 | -13,658.28 | 5,195.39 | expiry 105 |
| margin | control | ETFs | 119 | 0.58 | -7,327.62 | -61.58 | 59.09 | -0.0602 | -10,755.30 | 1,880.62 | expiry 119 |
| margin | t1 | All | 297 | 0.684 | -14,081.16 | -47.41 | 90.68 | -0.0375 | -24,265.26 | 9,012.65 | dte_exit 33, expiry 84, stop 26, take_profit 154 |
| margin | t1 | Stocks | 89 | 0.629 | -414.97 | -4.66 | 104.52 | -0.0034 | -4,216.08 | 4,849.47 | dte_exit 12, expiry 31, stop 6, take_profit 40 |
| margin | t1 | ETFs | 208 | 0.707 | -13,666.19 | -65.7 | 88.01 | -0.0541 | -20,049.17 | 4,163.19 | dte_exit 21, expiry 53, stop 20, take_profit 114 |
| cash_debit | control | All | 270 | 0.252 | 16,378.47 | 60.66 | -469.11 | 0.0193 | -135,919.50 | 32,172.03 | expiry 270 |
| cash_debit | control | Stocks | 216 | 0.245 | -45,237.65 | -209.43 | -497.33 | -0.0678 | -161,539.54 | 28,305.65 | expiry 216 |
| cash_debit | control | ETFs | 54 | 0.278 | 61,616.12 | 1,141.04 | -337.77 | 0.343 | 25,620.04 | 3,866.38 | expiry 54 |
| cash_debit | t1 | All | 546 | 0.502 | -20,736.85 | -37.98 | 1.96 | -0.0424 | -42,421.91 | 26,720.84 | dte_exit 27, expiry 15, profit_lock 36, stop 203, take_profit 265 |
| cash_debit | t1 | Stocks | 371 | 0.52 | -8,231.56 | -22.19 | 27.84 | -0.0242 | -25,471.50 | 20,732.56 | dte_exit 20, expiry 13, profit_lock 18, stop 127, take_profit 193 |
| cash_debit | t1 | ETFs | 175 | 0.463 | -12,505.29 | -71.46 | -36.66 | -0.0841 | -16,950.41 | 5,988.29 | dte_exit 7, expiry 2, profit_lock 18, stop 76, take_profit 72 |

Treatment − control net P&L by segment (**sign differs between Stocks and ETFs for 2 treatment(s)**):

| profile | treatment | All | Stocks | ETFs | sign_differs |
|---|---|---|---|---|---|
| margin | t1 | 9,672.85 | 16,011.42 | -6,338.57 | True |
| cash_debit | t1 | -37,115.32 | 37,006.09 | -74,121.41 | True |

## Exit-reason mix

| profile | arm | mix |
|---|---|---|
| margin | control | expiry 1 |
| margin | t1 | dte_exit 0.1111, expiry 0.2828, stop 0.0875, take_profit 0.5185 |
| cash_debit | control | expiry 1 |
| cash_debit | t1 | dte_exit 0.0495, expiry 0.0275, profit_lock 0.0659, stop 0.3718, take_profit 0.4853 |

## Skip reasons

| profile | arm | screen | blackout | no_candidate | unsettled | sizing | gate_per_underlying | gate_max_open_positions | gate_daily_loss | gate_max_gain | cash | capacity | no_fill | exit_delayed |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| margin | control | 55100 | 461 | 15541 | 666 | 1832 | 0 | 11048 | 3435 | 0 | 0 | 440 | 661 | 0 |
| margin | t1 | 55100 | 665 | 20311 | 252 | 929 | 0 | 3869 | 2159 | 0 | 0 | 540 | 181 | 11 |
| cash_debit | control | 55100 | 0 | 15081 | 149 | 106 | 0 | 5418 | 7668 | 0 | 31 | 316 | 203 | 0 |
| cash_debit | t1 | 55100 | 0 | 14410 | 149 | 1384 | 0 | 2381 | 8681 | 0 | 12 | 337 | 192 | 78 |

## Sub-periods

Fixed windows by entry date.

| profile | arm | period | trades | net_pnl | win_rate | max_dd_pct |
|---|---|---|---|---|---|---|
| margin | control | 2020 crash | 0 | 0 | – | 0 |
| margin | control | 2020-21 bull | 0 | 0 | – | 0 |
| margin | control | 2022 bear | 0 | 0 | – | 0 |
| margin | control | 2023 | 0 | 0 | – | 0 |
| margin | control | 2024 -> latest | 224 | -23,754.01 | 0.567 | 95.88 |
| margin | t1 | 2020 crash | 0 | 0 | – | 0 |
| margin | t1 | 2020-21 bull | 0 | 0 | – | 0 |
| margin | t1 | 2022 bear | 0 | 0 | – | 0 |
| margin | t1 | 2023 | 0 | 0 | – | 0 |
| margin | t1 | 2024 -> latest | 297 | -14,081.16 | 0.684 | 76.87 |
| cash_debit | control | 2020 crash | 0 | 0 | – | 0 |
| cash_debit | control | 2020-21 bull | 0 | 0 | – | 0 |
| cash_debit | control | 2022 bear | 0 | 0 | – | 0 |
| cash_debit | control | 2023 | 0 | 0 | – | 0 |
| cash_debit | control | 2024 -> latest | 270 | 16,378.47 | 0.252 | 85.47 |
| cash_debit | t1 | 2020 crash | 0 | 0 | – | 0 |
| cash_debit | t1 | 2020-21 bull | 0 | 0 | – | 0 |
| cash_debit | t1 | 2022 bear | 0 | 0 | – | 0 |
| cash_debit | t1 | 2023 | 0 | 0 | – | 0 |
| cash_debit | t1 | 2024 -> latest | 546 | -20,736.85 | 0.502 | 95.93 |

## By regime

| profile | arm | key | trades | net_pnl | win_rate |
|---|---|---|---|---|---|
| margin | control | bear | 18 | -10,909.27 | 0.5 |
| margin | control | bull | 47 | 2,363.03 | 0.851 |
| margin | control | sideways | 159 | -15,207.77 | 0.491 |
| margin | t1 | bear | 24 | -8,870.45 | 0.5 |
| margin | t1 | bull | 90 | -2,296.55 | 0.867 |
| margin | t1 | sideways | 183 | -2,914.16 | 0.617 |
| cash_debit | control | bear | 73 | -90,777.02 | 0.137 |
| cash_debit | control | bull | 197 | 107,155.49 | 0.294 |
| cash_debit | t1 | bear | 152 | -47,680.55 | 0.368 |
| cash_debit | t1 | bull | 394 | 26,943.71 | 0.553 |

## By structure

| profile | arm | key | trades | net_pnl | win_rate |
|---|---|---|---|---|---|
| margin | control | bear_call | 18 | -10,909.27 | 0.5 |
| margin | control | bull_put | 47 | 2,363.03 | 0.851 |
| margin | control | iron_condor | 159 | -15,207.77 | 0.491 |
| margin | t1 | bear_call | 24 | -8,870.45 | 0.5 |
| margin | t1 | bull_put | 90 | -2,296.55 | 0.867 |
| margin | t1 | iron_condor | 183 | -2,914.16 | 0.617 |
| cash_debit | control | bear_put | 64 | -52,983.57 | 0.125 |
| cash_debit | control | bull_call | 136 | -972.11 | 0.257 |
| cash_debit | control | long_call | 61 | 108,127.60 | 0.377 |
| cash_debit | control | long_put | 9 | -37,793.45 | 0.222 |
| cash_debit | t1 | bear_put | 117 | -40,464.67 | 0.342 |
| cash_debit | t1 | bull_call | 209 | 2,744.21 | 0.536 |
| cash_debit | t1 | long_call | 185 | 24,199.50 | 0.573 |
| cash_debit | t1 | long_put | 35 | -7,215.88 | 0.457 |

## By universe tier

| profile | arm | key | trades | net_pnl | win_rate |
|---|---|---|---|---|---|
| margin | control | core | 133 | -17,471.37 | 0.511 |
| margin | control | momentum | 13 | -2,834.05 | 0.692 |
| margin | control | rest | 78 | -3,448.59 | 0.641 |
| margin | t1 | core | 205 | -22,483.67 | 0.649 |
| margin | t1 | momentum | 14 | 6,872.01 | 0.786 |
| margin | t1 | rest | 78 | 1,530.50 | 0.756 |
| cash_debit | control | core | 146 | -31,417.49 | 0.233 |
| cash_debit | control | momentum | 16 | -24,860.43 | 0.188 |
| cash_debit | control | rest | 108 | 72,656.38 | 0.287 |
| cash_debit | t1 | core | 283 | -28,121.15 | 0.452 |
| cash_debit | t1 | momentum | 33 | -6,637.62 | 0.576 |
| cash_debit | t1 | rest | 230 | 14,021.92 | 0.552 |

## By sector

| profile | arm | key | trades | net_pnl | win_rate |
|---|---|---|---|---|---|
| margin | control | broad_market | 69 | -8,644.05 | 0.536 |
| margin | control | communication | 8 | -641.95 | 0.75 |
| margin | control | consumer_discretionary | 13 | -2,384.30 | 0.308 |
| margin | control | consumer_staples | 2 | 381.10 | 1 |
| margin | control | energy | 2 | 413.61 | 1 |
| margin | control | financials | 18 | 522.13 | 0.722 |
| margin | control | health_care | 7 | -642.90 | 0.571 |
| margin | control | industrials | 4 | -3,789.43 | 0.75 |
| margin | control | technology | 39 | -5,651.02 | 0.436 |
| margin | control | unknown | 62 | -3,317.20 | 0.629 |
| margin | t1 | broad_market | 143 | -17,108.97 | 0.657 |
| margin | t1 | communication | 2 | 594.35 | 1 |
| margin | t1 | consumer_discretionary | 14 | -4,246.63 | 0.429 |
| margin | t1 | energy | 4 | 1,390.47 | 0.75 |
| margin | t1 | financials | 14 | -1,810.74 | 0.643 |
| margin | t1 | health_care | 2 | 3,810.58 | 1 |
| margin | t1 | technology | 40 | -866.39 | 0.65 |
| margin | t1 | unknown | 78 | 4,156.18 | 0.782 |
| cash_debit | control | broad_market | 30 | 72,277.07 | 0.3 |
| cash_debit | control | communication | 15 | -21,239.27 | 0.067 |
| cash_debit | control | consumer_discretionary | 20 | 3,788.99 | 0.3 |
| cash_debit | control | energy | 4 | 36,645.83 | 0.5 |
| cash_debit | control | financials | 37 | -59,972.33 | 0.216 |
| cash_debit | control | health_care | 5 | -16,278.58 | 0 |
| cash_debit | control | industrials | 1 | -9,049.09 | 0 |
| cash_debit | control | technology | 103 | -5,765.85 | 0.262 |
| cash_debit | control | unknown | 55 | 15,971.70 | 0.273 |
| cash_debit | t1 | broad_market | 98 | -12,584.32 | 0.398 |
| cash_debit | t1 | communication | 22 | 3,573.75 | 0.636 |
| cash_debit | t1 | consumer_discretionary | 48 | -14,319.84 | 0.292 |
| cash_debit | t1 | consumer_staples | 3 | -589.40 | 0.333 |
| cash_debit | t1 | energy | 12 | -3,313.31 | 0.417 |
| cash_debit | t1 | financials | 59 | 3,220.14 | 0.593 |
| cash_debit | t1 | health_care | 15 | -5,443.74 | 0.4 |
| cash_debit | t1 | industrials | 11 | 1,898.52 | 0.636 |
| cash_debit | t1 | technology | 142 | 2,177.03 | 0.549 |
| cash_debit | t1 | unknown | 136 | 4,644.32 | 0.551 |

## ETF vs single stock

| profile | arm | key | trades | net_pnl | win_rate |
|---|---|---|---|---|---|
| margin | control | ETF | 119 | -7,327.62 | 0.58 |
| margin | control | single stock | 105 | -16,426.39 | 0.552 |
| margin | t1 | ETF | 208 | -13,666.19 | 0.707 |
| margin | t1 | single stock | 89 | -414.97 | 0.629 |
| cash_debit | control | ETF | 54 | 61,616.12 | 0.278 |
| cash_debit | control | single stock | 216 | -45,237.65 | 0.245 |
| cash_debit | t1 | ETF | 175 | -12,505.29 | 0.463 |
| cash_debit | t1 | single stock | 371 | -8,231.56 | 0.52 |

## By liquidity bucket

| profile | arm | key | trades | net_pnl | win_rate |
|---|---|---|---|---|---|
| margin | control | 100k-1M | 110 | -9,199.76 | 0.564 |
| margin | control | 10k-100k | 37 | 7,125.79 | 0.811 |
| margin | control | < 10k | 1 | -2,885.24 | 0 |
| margin | control | >= 1M | 76 | -18,794.79 | 0.461 |
| margin | t1 | 100k-1M | 118 | -8,668.85 | 0.669 |
| margin | t1 | 10k-100k | 35 | 10,586.26 | 0.743 |
| margin | t1 | < 10k | 1 | 192.68 | 1 |
| margin | t1 | >= 1M | 143 | -16,191.23 | 0.678 |
| cash_debit | control | 100k-1M | 162 | -143,652.55 | 0.222 |
| cash_debit | control | 10k-100k | 46 | 46,416.05 | 0.326 |
| cash_debit | control | >= 1M | 62 | 113,614.97 | 0.274 |
| cash_debit | t1 | 100k-1M | 291 | 3,556.57 | 0.512 |
| cash_debit | t1 | 10k-100k | 112 | -986.90 | 0.545 |
| cash_debit | t1 | < 10k | 5 | -4,369.75 | 0.4 |
| cash_debit | t1 | >= 1M | 138 | -18,936.76 | 0.449 |

## Uncertainty

Moving-block bootstrap CI of the daily equity-change difference vs control (seeded), and the fixed sub-periods each treatment won on net P&L.

| profile | treatment | pnl_diff | ci_lo | ci_hi | periods_won |
|---|---|---|---|---|---|
| margin | t1 | 9,672.86 | -25,646.21 | 40,864.14 | 2024 -> latest |
| cash_debit | t1 | -37,115.31 | -300,381.57 | 264,689.24 | – |

## Checkpoint ladder

Every ledger run of this arm set; `sign_flip_vs_first_rung` compares the primary metric's treatment − control sign with the first dev rung.

| at | phase | universe | profile | treatment | primary_diff | ci_lo | ci_hi | switch | sign_flip_vs_first_rung |
|---|---|---|---|---|---|---|---|---|---|
| 2026-10-16T00:44:47-04:00 | dev | c125 | cash_debit | t1 | 27.07 | -52,936.86 | 44,123.07 | False | False |
| 2026-10-16T00:44:47-04:00 | dev | c125 | margin | t1 | 6,702.15 | -18,771.39 | 38,257.45 | False | False |
| 2026-10-16T06:59:20-04:00 | holdout | c125 | cash_debit | t1 | -37,115.32 | -300,381.57 | 264,689.24 | False | True |
| 2026-10-16T06:59:20-04:00 | holdout | c125 | margin | t1 | 9,672.85 | -25,646.21 | 40,864.14 | False | False |
