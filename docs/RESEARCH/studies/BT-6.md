# BT-6 · E18.3 re-run: exit policy v2 keeps under the E18.3 rule against v1 (and against hold) on cash_debit with real quotes.

## Header

- phase **holdout**; spec hash `ff84836ecd11`; arm set `b5e94561540b`; owner owner; created 2026-10-15
- rule: Keep the control if its net P&L is not lower than the treatment's in ≥ 2 of 3 trend sub-periods (bear, sideways, bull) **and** its max drawdown is not higher than the treatment's by more than 10%. Otherwise recommend the treatment (as a future XP arm, never a direct flip).
- primary metric: `net_pnl`
- universe **c125** (ladder c125), version `u500-20261009-s1-e52ff301`; data partitions {'thetadata': 125}
- decisions 2024-01-01 -> 2026-10-15 (exits settle by 2026-10-15); dev 2020-01-01 -> 2023-12-31; holdout 2024-01-01 -> latest
- Not replayed: Discovery and Trending tiers (no historical LLM, Reddit or Stocktwits inputs), the LLM stance (a deterministic trend-label proxy stands in), intraday paths (EOD marks only; exits fire on closes).
- git `28afa22611cd`; variants tried 1 of max 3; earlier holdout runs 0
- arms: control = ranker incumbent; t1 = ranker incumbent, overlay `{"exits": {"default": {"profit_lock": null, "take_profit_pct_of_debit": 1.0}, "kinds": {"long_call": {"profit_lock": null, "take_profit_pct_of_debit": 1.0}, "long_put": {"profit_lock": null, "take_profit_pct_of_debit": 1.0}, "vertical_debit": {"profit_lock": null, "take_profit_pct_of_debit": 1.0}}}}`; t2 = ranker incumbent, overlay `{"exits": {"default": {"close_at_dte": null, "profit_lock": null, "take_profit_pct_of_debit": null, "take_profit_pct_of_max_gain": null}, "kinds": {"iron_condor": {"close_at_dte": null, "profit_lock": null, "stop": null, "take_profit_pct_of_max_gain": null}, "long_call": {"close_at_dte": null, "profit_lock": null, "stop": null, "take_profit_pct_of_debit": null}, "long_put": {"close_at_dte": null, "profit_lock": null, "stop": null, "take_profit_pct_of_debit": null}, "vertical_credit": {"close_at_dte": null, "profit_lock": null, "stop": null, "take_profit_pct_of_max_gain": null}, "vertical_debit": {"close_at_dte": null, "profit_lock": null, "stop": null, "take_profit_pct_of_debit": null}}}}`

## Verdict

The pre-registered rule, verbatim:

> Keep the control if its net P&L is not lower than the treatment's in ≥ 2 of 3 trend sub-periods (bear, sideways, bull) **and** its max drawdown is not higher than the treatment's by more than 10%. Otherwise recommend the treatment (as a future XP arm, never a direct flip).

Original card's wording: _Keep v2 if, for cash_debit, v2's net P&L is not lower than v1's in ≥ 2 of 3 trend sub-periods **and** max drawdown is not higher by more than 10 %. Otherwise recommend rollback (or the best grid cell as a future XP arm, never a direct flip)._

- VERDICT · cash_debit · t1 vs control: keep control (sub-periods 2 [bear,sideways], P&L diff -534, CI [-65,501, 63,940])
- VERDICT · cash_debit · t2 vs control: keep control (sub-periods 2 [bear,sideways], P&L diff 37,115, CI [-264,689, 300,382])

A verdict only recommends: a live change still goes through D44 (`Experiment: XP-<n>` with arms copied from this spec) or an owner waiver.

## Per arm × profile

| profile | arm | trades | net_pnl | return_pct | cagr | max_dd_pct | sharpe | sortino | profit_factor | win_rate | avg_days_held | turnover_per_month | cost_share_of_gross | capacity_clipped_share |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| cash_debit | control | 546 | -20,736.85 | -82.95 | -0.4702 | 95.93 | 0.398 | 0.626 | 0.89 | 0.502 | 14 | 16.34 | 4.465 | 0.368 |
| cash_debit | t1 | 440 | -21,270.53 | -85.08 | -0.4951 | 96.36 | 0.253 | 0.307 | 0.923 | 0.398 | 17.8 | 13.17 | 2.201 | 0.47 |
| cash_debit | t2 | 270 | 16,378.47 | 65.51 | 0.1984 | 85.47 | 0.91 | 1.147 | 1.028 | 0.252 | 34.7 | 8.08 | 0.663 | 0.737 |

## All / Stocks / ETFs

One pool per run; the verdict is stated on All, Stocks and ETFs are diagnostic. `dd_contribution` = net P&L of the trades closed inside the run's max-drawdown window; `spread_cost` = P&L at mid − net P&L (spread + fees).

| profile | arm | segment | trades | win_rate | net_pnl | mean_pnl | median_pnl | pnl_per_risk | dd_contribution | spread_cost | exit_reasons |
|---|---|---|---|---|---|---|---|---|---|---|---|
| cash_debit | control | All | 546 | 0.502 | -20,736.85 | -37.98 | 1.96 | -0.0424 | -42,421.91 | 26,720.84 | dte_exit 27, expiry 15, profit_lock 36, stop 203, take_profit 265 |
| cash_debit | control | Stocks | 371 | 0.52 | -8,231.56 | -22.19 | 27.84 | -0.0242 | -25,471.50 | 20,732.56 | dte_exit 20, expiry 13, profit_lock 18, stop 127, take_profit 193 |
| cash_debit | control | ETFs | 175 | 0.463 | -12,505.29 | -71.46 | -36.66 | -0.0841 | -16,950.41 | 5,988.29 | dte_exit 7, expiry 2, profit_lock 18, stop 76, take_profit 72 |
| cash_debit | t1 | All | 440 | 0.398 | -21,270.53 | -48.34 | -143.57 | -0.0354 | -64,540.30 | 38,986.53 | dte_exit 55, expiry 17, stop 207, take_profit 161 |
| cash_debit | t1 | Stocks | 323 | 0.409 | -15,813.19 | -48.96 | -132.97 | -0.0338 | -50,551.54 | 34,270.19 | dte_exit 39, expiry 15, stop 149, take_profit 120 |
| cash_debit | t1 | ETFs | 117 | 0.368 | -5,457.34 | -46.64 | -165.16 | -0.0407 | -13,988.75 | 4,716.34 | dte_exit 16, expiry 2, stop 58, take_profit 41 |
| cash_debit | t2 | All | 270 | 0.252 | 16,378.47 | 60.66 | -469.11 | 0.0193 | -135,919.50 | 32,172.03 | expiry 270 |
| cash_debit | t2 | Stocks | 216 | 0.245 | -45,237.65 | -209.43 | -497.33 | -0.0678 | -161,539.54 | 28,305.65 | expiry 216 |
| cash_debit | t2 | ETFs | 54 | 0.278 | 61,616.12 | 1,141.04 | -337.77 | 0.343 | 25,620.04 | 3,866.38 | expiry 54 |

Treatment − control net P&L by segment (**sign differs between Stocks and ETFs for 2 treatment(s)**):

| profile | treatment | All | Stocks | ETFs | sign_differs |
|---|---|---|---|---|---|
| cash_debit | t1 | -533.68 | -7,581.63 | 7,047.95 | True |
| cash_debit | t2 | 37,115.32 | -37,006.09 | 74,121.41 | True |

## Exit-reason mix

| profile | arm | mix |
|---|---|---|
| cash_debit | control | dte_exit 0.0495, expiry 0.0275, profit_lock 0.0659, stop 0.3718, take_profit 0.4853 |
| cash_debit | t1 | dte_exit 0.125, expiry 0.0386, stop 0.4705, take_profit 0.3659 |
| cash_debit | t2 | expiry 1 |

## Skip reasons

| profile | arm | screen | blackout | no_candidate | unsettled | sizing | gate_per_underlying | gate_max_open_positions | gate_daily_loss | gate_max_gain | cash | capacity | no_fill | exit_delayed |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| cash_debit | control | 55100 | 0 | 14410 | 149 | 1384 | 0 | 2381 | 8681 | 0 | 12 | 337 | 192 | 78 |
| cash_debit | t1 | 55100 | 0 | 14348 | 135 | 912 | 0 | 3017 | 8796 | 0 | 16 | 334 | 202 | 68 |
| cash_debit | t2 | 55100 | 0 | 15081 | 149 | 106 | 0 | 5418 | 7668 | 0 | 31 | 316 | 203 | 0 |

## Sub-periods

Fixed windows by entry date.

| profile | arm | period | trades | net_pnl | win_rate | max_dd_pct |
|---|---|---|---|---|---|---|
| cash_debit | control | 2020 crash | 0 | 0 | – | 0 |
| cash_debit | control | 2020-21 bull | 0 | 0 | – | 0 |
| cash_debit | control | 2022 bear | 0 | 0 | – | 0 |
| cash_debit | control | 2023 | 0 | 0 | – | 0 |
| cash_debit | control | 2024 -> latest | 546 | -20,736.85 | 0.502 | 95.93 |
| cash_debit | t1 | 2020 crash | 0 | 0 | – | 0 |
| cash_debit | t1 | 2020-21 bull | 0 | 0 | – | 0 |
| cash_debit | t1 | 2022 bear | 0 | 0 | – | 0 |
| cash_debit | t1 | 2023 | 0 | 0 | – | 0 |
| cash_debit | t1 | 2024 -> latest | 440 | -21,270.53 | 0.398 | 96.36 |
| cash_debit | t2 | 2020 crash | 0 | 0 | – | 0 |
| cash_debit | t2 | 2020-21 bull | 0 | 0 | – | 0 |
| cash_debit | t2 | 2022 bear | 0 | 0 | – | 0 |
| cash_debit | t2 | 2023 | 0 | 0 | – | 0 |
| cash_debit | t2 | 2024 -> latest | 270 | 16,378.47 | 0.252 | 85.47 |

## By regime

| profile | arm | key | trades | net_pnl | win_rate |
|---|---|---|---|---|---|
| cash_debit | control | bear | 152 | -47,680.55 | 0.368 |
| cash_debit | control | bull | 394 | 26,943.71 | 0.553 |
| cash_debit | t1 | bear | 110 | -60,969.65 | 0.245 |
| cash_debit | t1 | bull | 330 | 39,699.11 | 0.448 |
| cash_debit | t2 | bear | 73 | -90,777.02 | 0.137 |
| cash_debit | t2 | bull | 197 | 107,155.49 | 0.294 |

## By structure

| profile | arm | key | trades | net_pnl | win_rate |
|---|---|---|---|---|---|
| cash_debit | control | bear_put | 117 | -40,464.67 | 0.342 |
| cash_debit | control | bull_call | 209 | 2,744.21 | 0.536 |
| cash_debit | control | long_call | 185 | 24,199.50 | 0.573 |
| cash_debit | control | long_put | 35 | -7,215.88 | 0.457 |
| cash_debit | t1 | bear_put | 85 | -45,035.01 | 0.212 |
| cash_debit | t1 | bull_call | 189 | 28,413.02 | 0.429 |
| cash_debit | t1 | long_call | 141 | 11,286.09 | 0.475 |
| cash_debit | t1 | long_put | 25 | -15,934.64 | 0.36 |
| cash_debit | t2 | bear_put | 64 | -52,983.57 | 0.125 |
| cash_debit | t2 | bull_call | 136 | -972.11 | 0.257 |
| cash_debit | t2 | long_call | 61 | 108,127.60 | 0.377 |
| cash_debit | t2 | long_put | 9 | -37,793.45 | 0.222 |

## By universe tier

| profile | arm | key | trades | net_pnl | win_rate |
|---|---|---|---|---|---|
| cash_debit | control | core | 283 | -28,121.15 | 0.452 |
| cash_debit | control | momentum | 33 | -6,637.62 | 0.576 |
| cash_debit | control | rest | 230 | 14,021.92 | 0.552 |
| cash_debit | t1 | core | 219 | 15,371.19 | 0.379 |
| cash_debit | t1 | momentum | 33 | -19,065.16 | 0.394 |
| cash_debit | t1 | rest | 188 | -17,576.56 | 0.42 |
| cash_debit | t2 | core | 146 | -31,417.49 | 0.233 |
| cash_debit | t2 | momentum | 16 | -24,860.43 | 0.188 |
| cash_debit | t2 | rest | 108 | 72,656.38 | 0.287 |

## By sector

| profile | arm | key | trades | net_pnl | win_rate |
|---|---|---|---|---|---|
| cash_debit | control | broad_market | 98 | -12,584.32 | 0.398 |
| cash_debit | control | communication | 22 | 3,573.75 | 0.636 |
| cash_debit | control | consumer_discretionary | 48 | -14,319.84 | 0.292 |
| cash_debit | control | consumer_staples | 3 | -589.40 | 0.333 |
| cash_debit | control | energy | 12 | -3,313.31 | 0.417 |
| cash_debit | control | financials | 59 | 3,220.14 | 0.593 |
| cash_debit | control | health_care | 15 | -5,443.74 | 0.4 |
| cash_debit | control | industrials | 11 | 1,898.52 | 0.636 |
| cash_debit | control | technology | 142 | 2,177.03 | 0.549 |
| cash_debit | control | unknown | 136 | 4,644.32 | 0.551 |
| cash_debit | t1 | broad_market | 59 | 530.16 | 0.339 |
| cash_debit | t1 | communication | 12 | 8,584.61 | 0.75 |
| cash_debit | t1 | consumer_discretionary | 41 | -9,926.32 | 0.195 |
| cash_debit | t1 | consumer_staples | 2 | 222.41 | 0.5 |
| cash_debit | t1 | energy | 13 | -6,526.37 | 0.385 |
| cash_debit | t1 | financials | 49 | -10,504.45 | 0.49 |
| cash_debit | t1 | health_care | 15 | 871.32 | 0.4 |
| cash_debit | t1 | industrials | 10 | -11,591.44 | 0.3 |
| cash_debit | t1 | technology | 125 | 16,928.37 | 0.432 |
| cash_debit | t1 | unknown | 114 | -9,858.83 | 0.395 |
| cash_debit | t2 | broad_market | 30 | 72,277.07 | 0.3 |
| cash_debit | t2 | communication | 15 | -21,239.27 | 0.067 |
| cash_debit | t2 | consumer_discretionary | 20 | 3,788.99 | 0.3 |
| cash_debit | t2 | energy | 4 | 36,645.83 | 0.5 |
| cash_debit | t2 | financials | 37 | -59,972.33 | 0.216 |
| cash_debit | t2 | health_care | 5 | -16,278.58 | 0 |
| cash_debit | t2 | industrials | 1 | -9,049.09 | 0 |
| cash_debit | t2 | technology | 103 | -5,765.85 | 0.262 |
| cash_debit | t2 | unknown | 55 | 15,971.70 | 0.273 |

## ETF vs single stock

| profile | arm | key | trades | net_pnl | win_rate |
|---|---|---|---|---|---|
| cash_debit | control | ETF | 175 | -12,505.29 | 0.463 |
| cash_debit | control | single stock | 371 | -8,231.56 | 0.52 |
| cash_debit | t1 | ETF | 117 | -5,457.34 | 0.368 |
| cash_debit | t1 | single stock | 323 | -15,813.19 | 0.409 |
| cash_debit | t2 | ETF | 54 | 61,616.12 | 0.278 |
| cash_debit | t2 | single stock | 216 | -45,237.65 | 0.245 |

## By liquidity bucket

| profile | arm | key | trades | net_pnl | win_rate |
|---|---|---|---|---|---|
| cash_debit | control | 100k-1M | 291 | 3,556.57 | 0.512 |
| cash_debit | control | 10k-100k | 112 | -986.90 | 0.545 |
| cash_debit | control | < 10k | 5 | -4,369.75 | 0.4 |
| cash_debit | control | >= 1M | 138 | -18,936.76 | 0.449 |
| cash_debit | t1 | 100k-1M | 233 | -26,806.81 | 0.369 |
| cash_debit | t1 | 10k-100k | 98 | 11,920.26 | 0.51 |
| cash_debit | t1 | < 10k | 6 | -9,062.10 | 0.167 |
| cash_debit | t1 | >= 1M | 103 | 2,678.11 | 0.369 |
| cash_debit | t2 | 100k-1M | 162 | -143,652.55 | 0.222 |
| cash_debit | t2 | 10k-100k | 46 | 46,416.05 | 0.326 |
| cash_debit | t2 | >= 1M | 62 | 113,614.97 | 0.274 |

## Uncertainty

Moving-block bootstrap CI of the daily equity-change difference vs control (seeded), and the fixed sub-periods each treatment won on net P&L.

| profile | treatment | pnl_diff | ci_lo | ci_hi | periods_won |
|---|---|---|---|---|---|
| cash_debit | t1 | -533.69 | -65,500.83 | 63,939.54 | – |
| cash_debit | t2 | 37,115.31 | -264,689.24 | 300,381.57 | 2024 -> latest |

## Checkpoint ladder

Every ledger run of this arm set; `sign_flip_vs_first_rung` compares the primary metric's treatment − control sign with the first dev rung.

| at | phase | universe | profile | treatment | primary_diff | ci_lo | ci_hi | switch | sign_flip_vs_first_rung |
|---|---|---|---|---|---|---|---|---|---|
| 2026-10-15T23:50:19-04:00 | dev | c125 | cash_debit | t1 | -27.74 | -32,551.73 | 42,419.16 | True | False |
| 2026-10-15T23:50:19-04:00 | dev | c125 | cash_debit | t2 | -27.07 | -44,123.07 | 52,936.86 | True | False |
| 2026-10-16T06:16:52-04:00 | holdout | c125 | cash_debit | t1 | -533.68 | -65,500.83 | 63,939.54 | False | False |
| 2026-10-16T06:16:52-04:00 | holdout | c125 | cash_debit | t2 | 37,115.32 | -264,689.24 | 300,381.57 | False | True |
