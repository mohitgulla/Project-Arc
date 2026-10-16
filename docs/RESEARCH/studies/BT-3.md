# BT-3 · E7.5 re-run: no challenger ranker (managed_net_ev, rorc_day, rorc_day_vrp) beats the profile's incumbent under the D25 rule on real quotes.

## Header

- phase **holdout**; spec hash `6bd6ea4a7b93`; arm set `924a715d3ba9`; owner owner; created 2026-10-15
- rule: Switch the default to a challenger only if it beats the incumbent on net P&L (higher) **and** max drawdown (not larger) in ≥ 2 of 3 sub-periods (bear, sideways, bull trend regime at entry), **and** the 90% block-bootstrap CI of its daily P&L difference vs the incumbent excludes 0 (lower bound > 0). Otherwise keep the incumbent and report the closest challenger.
- primary metric: `net_pnl`
- universe **c500** (ladder c125, c250, c500), version `u500-20261009-s1-e52ff301`; data partitions {'thetadata': 500}
- decisions 2024-01-01 -> 2026-10-15 (exits settle by 2026-10-15); dev 2020-01-01 -> 2023-12-31; holdout 2024-01-01 -> latest
- Not replayed: Discovery and Trending tiers (no historical LLM, Reddit or Stocktwits inputs), the LLM stance (a deterministic trend-label proxy stands in), intraday paths (EOD marks only; exits fire on closes).
- git `28afa22611cd`; variants tried 1 of max 3; earlier holdout runs 0
- arms: control = ranker incumbent; t1 = ranker managed_net_ev; t2 = ranker rorc_day; t3 = ranker rorc_day_vrp

## Verdict

The pre-registered rule, verbatim:

> Switch the default to a challenger only if it beats the incumbent on net P&L (higher) **and** max drawdown (not larger) in ≥ 2 of 3 sub-periods (bear, sideways, bull trend regime at entry), **and** the 90% block-bootstrap CI of its daily P&L difference vs the incumbent excludes 0 (lower bound > 0). Otherwise keep the incumbent and report the closest challenger.

Original card's wording: _Switch the default to a challenger only if it beats the incumbent on net P&L (higher) **and** max drawdown (not larger) in ≥ 2 of 3 sub-periods (bear, sideways, bull trend regime at entry), **and** the 90% block-bootstrap CI of its daily P&L difference vs the incumbent excludes 0 (lower bound > 0). Otherwise keep the incumbent and report the closest challenger._

- VERDICT · margin · t1 vs control: keep control (sub-periods 1 [bear], P&L diff -10,812, CI [-32,654, 7,403])
- VERDICT · margin · t2 vs control: keep control (sub-periods 2 [sideways,bull], P&L diff 4,749, CI [-18,823, 30,657])
- VERDICT · margin · t3 vs control: keep control (sub-periods 1 [bull], P&L diff -4,028, CI [-25,738, 17,171])
- VERDICT · cash_debit · t1 vs control: keep control (sub-periods 0 [–], P&L diff 46,298, CI [-196,192, 294,268])
- VERDICT · cash_debit · t2 vs control: keep control (sub-periods 0 [–], P&L diff 89,818, CI [-201,923, 385,932])
- VERDICT · cash_debit · t3 vs control: keep control (sub-periods 1 [bear], P&L diff 1,693, CI [-44,548, 52,174])

A verdict only recommends: a live change still goes through D44 (`Experiment: XP-<n>` with arms copied from this spec) or an owner waiver.

## Per arm × profile

| profile | arm | trades | net_pnl | return_pct | cagr | max_dd_pct | sharpe | sortino | profit_factor | win_rate | avg_days_held | turnover_per_month | cost_share_of_gross | capacity_clipped_share |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| margin | control | 316 | -3,441.30 | -13.77 | -0.0518 | 56.9 | 0.14 | 0.132 | 0.962 | 0.699 | 24.7 | 9.46 | 1.367 | 0.522 |
| margin | t1 | 322 | -14,252.96 | -57.01 | -0.2616 | 70.37 | -0.496 | -0.437 | 0.804 | 0.736 | 22.9 | 9.64 | 2.233 | 0.363 |
| margin | t2 | 340 | 1,307.45 | 5.23 | 0.0185 | 54.97 | 0.255 | 0.241 | 1.019 | 0.747 | 22.8 | 10.18 | 0.888 | 0.435 |
| margin | t3 | 322 | -7,469.07 | -29.88 | -0.1197 | 54.43 | -0.091 | -0.084 | 0.894 | 0.717 | 23.4 | 9.64 | 4.611 | 0.398 |
| cash_debit | control | 514 | -23,843.49 | -95.37 | -0.6684 | 97.67 | -0.124 | -0.144 | 0.857 | 0.504 | 14.3 | 15.38 | 37.12 | 0.358 |
| cash_debit | t1 | 586 | 22,454.97 | 89.82 | 0.2588 | 84.47 | 0.857 | 1.02 | 1.031 | 0.522 | 15.5 | 17.54 | 0.69 | 0.341 |
| cash_debit | t2 | 605 | 65,974.55 | 263.90 | 0.5903 | 78.73 | 1.008 | 1.248 | 1.083 | 0.516 | 14.7 | 18.11 | 0.477 | 0.463 |
| cash_debit | t3 | 335 | -22,150.14 | -88.6 | -0.5416 | 96.48 | 0.072 | 0.081 | 0.864 | 0.528 | 14.4 | 10.03 | 1.649 | 0.37 |

## All / Stocks / ETFs

One pool per run; the verdict is stated on All, Stocks and ETFs are diagnostic. `dd_contribution` = net P&L of the trades closed inside the run's max-drawdown window; `spread_cost` = P&L at mid − net P&L (spread + fees).

| profile | arm | segment | trades | win_rate | net_pnl | mean_pnl | median_pnl | pnl_per_risk | dd_contribution | spread_cost | exit_reasons |
|---|---|---|---|---|---|---|---|---|---|---|---|
| margin | control | All | 316 | 0.699 | -3,441.30 | -10.89 | 99.22 | -0.0072 | -18,260.31 | 12,810.80 | dte_exit 40, expiry 82, stop 26, take_profit 168 |
| margin | control | Stocks | 79 | 0.633 | -260.35 | -3.3 | 152.71 | -0.0016 | -7,116.27 | 6,482.35 | dte_exit 10, expiry 28, stop 7, take_profit 34 |
| margin | control | ETFs | 237 | 0.722 | -3,180.95 | -13.42 | 89.59 | -0.0101 | -11,144.04 | 6,328.45 | dte_exit 30, expiry 54, stop 19, take_profit 134 |
| margin | t1 | All | 322 | 0.736 | -14,252.96 | -44.26 | 85.43 | -0.0324 | -19,295.67 | 9,844.96 | dte_exit 36, expiry 71, stop 25, take_profit 190 |
| margin | t1 | Stocks | 80 | 0.75 | -2,076.58 | -25.96 | 162.21 | -0.0145 | -6,428.35 | 4,918.08 | dte_exit 8, expiry 21, stop 5, take_profit 46 |
| margin | t1 | ETFs | 242 | 0.731 | -12,176.38 | -50.32 | 75.47 | -0.0411 | -12,867.32 | 4,926.88 | dte_exit 28, expiry 50, stop 20, take_profit 144 |
| margin | t2 | All | 340 | 0.747 | 1,307.45 | 3.85 | 86.9 | 0.0028 | -13,996.89 | 10,416.05 | dte_exit 36, expiry 81, stop 23, take_profit 200 |
| margin | t2 | Stocks | 66 | 0.712 | 2,416.94 | 36.62 | 142.69 | 0.0201 | -957.89 | 4,505.06 | dte_exit 6, expiry 22, stop 3, take_profit 35 |
| margin | t2 | ETFs | 274 | 0.755 | -1,109.49 | -4.05 | 80.77 | -0.0032 | -13,039.01 | 5,910.99 | dte_exit 30, expiry 59, stop 20, take_profit 165 |
| margin | t3 | All | 322 | 0.717 | -7,469.07 | -23.2 | 81.97 | -0.0179 | -13,754.96 | 9,537.57 | dte_exit 40, expiry 77, stop 18, take_profit 187 |
| margin | t3 | Stocks | 72 | 0.708 | 859.70 | 11.94 | 147.69 | 0.007 | -387.39 | 4,183.30 | dte_exit 6, expiry 24, stop 3, take_profit 39 |
| margin | t3 | ETFs | 250 | 0.72 | -8,328.77 | -33.32 | 76.47 | -0.0283 | -13,367.57 | 5,354.27 | dte_exit 34, expiry 53, stop 15, take_profit 148 |
| cash_debit | control | All | 514 | 0.504 | -23,843.49 | -46.39 | 5.43 | -0.0569 | -47,646.59 | 23,217.99 | dte_exit 25, expiry 24, profit_lock 39, stop 190, take_profit 236 |
| cash_debit | control | Stocks | 334 | 0.47 | -25,672.34 | -76.86 | -16.58 | -0.0881 | -34,676.81 | 16,722.34 | dte_exit 21, expiry 24, profit_lock 23, stop 124, take_profit 142 |
| cash_debit | control | ETFs | 180 | 0.567 | 1,828.85 | 10.16 | 22.85 | 0.0143 | -12,969.79 | 6,495.65 | dte_exit 4, profit_lock 16, stop 66, take_profit 94 |
| cash_debit | t1 | All | 586 | 0.522 | 22,454.97 | 38.32 | 91.65 | 0.0112 | -29,684.15 | 50,041.03 | dte_exit 58, expiry 15, profit_lock 38, stop 207, take_profit 268 |
| cash_debit | t1 | Stocks | 418 | 0.519 | -12,293.32 | -29.41 | 149.84 | -0.0079 | -14,474.41 | 37,000.32 | dte_exit 46, expiry 15, profit_lock 26, stop 143, take_profit 188 |
| cash_debit | t1 | ETFs | 168 | 0.53 | 34,748.29 | 206.84 | 31.72 | 0.0751 | -15,209.74 | 13,040.71 | dte_exit 12, profit_lock 12, stop 64, take_profit 80 |
| cash_debit | t2 | All | 605 | 0.516 | 65,974.55 | 109.05 | 37.42 | 0.0315 | -65,147.80 | 60,253.95 | dte_exit 35, expiry 18, profit_lock 43, stop 228, take_profit 281 |
| cash_debit | t2 | Stocks | 418 | 0.512 | 40,034.85 | 95.78 | 62.75 | 0.0249 | -31,490.01 | 42,748.15 | dte_exit 29, expiry 17, profit_lock 25, stop 155, take_profit 192 |
| cash_debit | t2 | ETFs | 187 | 0.524 | 25,939.71 | 138.72 | 16.68 | 0.0534 | -33,657.79 | 17,505.79 | dte_exit 6, expiry 1, profit_lock 18, stop 73, take_profit 89 |
| cash_debit | t3 | All | 335 | 0.528 | -22,150.14 | -66.12 | 20.59 | -0.0573 | -61,856.76 | 13,789.14 | dte_exit 14, expiry 7, profit_lock 19, stop 133, take_profit 162 |
| cash_debit | t3 | Stocks | 126 | 0.46 | -35,523.03 | -281.93 | -110.21 | -0.2109 | -45,847.51 | 5,475.53 | dte_exit 5, expiry 7, profit_lock 4, stop 58, take_profit 52 |
| cash_debit | t3 | ETFs | 209 | 0.569 | 13,372.89 | 63.99 | 32.36 | 0.0614 | -16,009.26 | 8,313.61 | dte_exit 9, profit_lock 15, stop 75, take_profit 110 |

Treatment − control net P&L by segment (**sign differs between Stocks and ETFs for 2 treatment(s)**):

| profile | treatment | All | Stocks | ETFs | sign_differs |
|---|---|---|---|---|---|
| margin | t1 | -10,811.66 | -1,816.23 | -8,995.43 | False |
| margin | t2 | 4,748.75 | 2,677.29 | 2,071.46 | False |
| margin | t3 | -4,027.77 | 1,120.05 | -5,147.82 | True |
| cash_debit | t1 | 46,298.46 | 13,379.02 | 32,919.44 | False |
| cash_debit | t2 | 89,818.04 | 65,707.19 | 24,110.86 | False |
| cash_debit | t3 | 1,693.35 | -9,850.69 | 11,544.04 | True |

## Exit-reason mix

| profile | arm | mix |
|---|---|---|
| margin | control | dte_exit 0.1266, expiry 0.2595, stop 0.0823, take_profit 0.5316 |
| margin | t1 | dte_exit 0.1118, expiry 0.2205, stop 0.0776, take_profit 0.5901 |
| margin | t2 | dte_exit 0.1059, expiry 0.2382, stop 0.0676, take_profit 0.5882 |
| margin | t3 | dte_exit 0.1242, expiry 0.2391, stop 0.0559, take_profit 0.5807 |
| cash_debit | control | dte_exit 0.0486, expiry 0.0467, profit_lock 0.0759, stop 0.3696, take_profit 0.4591 |
| cash_debit | t1 | dte_exit 0.099, expiry 0.0256, profit_lock 0.0648, stop 0.3532, take_profit 0.4573 |
| cash_debit | t2 | dte_exit 0.0579, expiry 0.0298, profit_lock 0.0711, stop 0.3769, take_profit 0.4645 |
| cash_debit | t3 | dte_exit 0.0418, expiry 0.0209, profit_lock 0.0567, stop 0.397, take_profit 0.4836 |

## Skip reasons

| profile | arm | screen | blackout | no_candidate | unsettled | sizing | gate_per_underlying | gate_max_open_positions | gate_daily_loss | gate_max_gain | cash | capacity | no_fill | exit_delayed |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| margin | control | 226930 | 613 | 25442 | 255 | 510 | 0 | 4424 | 2815 | 0 | 0 | 624 | 153 | 13 |
| margin | t1 | 226930 | 644 | 25556 | 255 | 974 | 0 | 3891 | 2725 | 0 | 0 | 511 | 169 | 11 |
| margin | t2 | 226930 | 692 | 25797 | 255 | 685 | 0 | 4200 | 2433 | 0 | 0 | 604 | 171 | 12 |
| margin | t3 | 226930 | 663 | 25708 | 255 | 697 | 0 | 4176 | 2584 | 0 | 0 | 556 | 141 | 14 |
| cash_debit | control | 226930 | 0 | 18691 | 221 | 2111 | 0 | 3031 | 9546 | 0 | 31 | 311 | 170 | 86 |
| cash_debit | t1 | 226930 | 0 | 17815 | 186 | 746 | 0 | 4043 | 10577 | 0 | 43 | 340 | 174 | 110 |
| cash_debit | t2 | 226930 | 0 | 17789 | 175 | 410 | 0 | 4504 | 10552 | 0 | 10 | 415 | 223 | 110 |
| cash_debit | t3 | 226930 | 0 | 21242 | 53 | 461 | 0 | 271 | 9139 | 0 | 0 | 207 | 68 | 42 |

## Sub-periods

Fixed windows by entry date.

| profile | arm | period | trades | net_pnl | win_rate | max_dd_pct |
|---|---|---|---|---|---|---|
| margin | control | 2020 crash | 0 | 0 | – | 0 |
| margin | control | 2020-21 bull | 0 | 0 | – | 0 |
| margin | control | 2022 bear | 0 | 0 | – | 0 |
| margin | control | 2023 | 0 | 0 | – | 0 |
| margin | control | 2024 -> latest | 316 | -3,441.30 | 0.699 | 56.9 |
| margin | t1 | 2020 crash | 0 | 0 | – | 0 |
| margin | t1 | 2020-21 bull | 0 | 0 | – | 0 |
| margin | t1 | 2022 bear | 0 | 0 | – | 0 |
| margin | t1 | 2023 | 0 | 0 | – | 0 |
| margin | t1 | 2024 -> latest | 322 | -14,252.96 | 0.736 | 70.37 |
| margin | t2 | 2020 crash | 0 | 0 | – | 0 |
| margin | t2 | 2020-21 bull | 0 | 0 | – | 0 |
| margin | t2 | 2022 bear | 0 | 0 | – | 0 |
| margin | t2 | 2023 | 0 | 0 | – | 0 |
| margin | t2 | 2024 -> latest | 340 | 1,307.45 | 0.747 | 54.97 |
| margin | t3 | 2020 crash | 0 | 0 | – | 0 |
| margin | t3 | 2020-21 bull | 0 | 0 | – | 0 |
| margin | t3 | 2022 bear | 0 | 0 | – | 0 |
| margin | t3 | 2023 | 0 | 0 | – | 0 |
| margin | t3 | 2024 -> latest | 322 | -7,469.07 | 0.717 | 54.43 |
| cash_debit | control | 2020 crash | 0 | 0 | – | 0 |
| cash_debit | control | 2020-21 bull | 0 | 0 | – | 0 |
| cash_debit | control | 2022 bear | 0 | 0 | – | 0 |
| cash_debit | control | 2023 | 0 | 0 | – | 0 |
| cash_debit | control | 2024 -> latest | 514 | -23,843.49 | 0.504 | 97.67 |
| cash_debit | t1 | 2020 crash | 0 | 0 | – | 0 |
| cash_debit | t1 | 2020-21 bull | 0 | 0 | – | 0 |
| cash_debit | t1 | 2022 bear | 0 | 0 | – | 0 |
| cash_debit | t1 | 2023 | 0 | 0 | – | 0 |
| cash_debit | t1 | 2024 -> latest | 586 | 22,454.97 | 0.522 | 84.47 |
| cash_debit | t2 | 2020 crash | 0 | 0 | – | 0 |
| cash_debit | t2 | 2020-21 bull | 0 | 0 | – | 0 |
| cash_debit | t2 | 2022 bear | 0 | 0 | – | 0 |
| cash_debit | t2 | 2023 | 0 | 0 | – | 0 |
| cash_debit | t2 | 2024 -> latest | 605 | 65,974.55 | 0.516 | 78.73 |
| cash_debit | t3 | 2020 crash | 0 | 0 | – | 0 |
| cash_debit | t3 | 2020-21 bull | 0 | 0 | – | 0 |
| cash_debit | t3 | 2022 bear | 0 | 0 | – | 0 |
| cash_debit | t3 | 2023 | 0 | 0 | – | 0 |
| cash_debit | t3 | 2024 -> latest | 335 | -22,150.14 | 0.528 | 96.48 |

## By regime

| profile | arm | key | trades | net_pnl | win_rate |
|---|---|---|---|---|---|
| margin | control | bear | 21 | -8,592.71 | 0.524 |
| margin | control | bull | 101 | -1,553.40 | 0.842 |
| margin | control | sideways | 194 | 6,704.81 | 0.644 |
| margin | t1 | bear | 26 | -6,347.35 | 0.538 |
| margin | t1 | bull | 118 | -2,890.77 | 0.839 |
| margin | t1 | sideways | 178 | -5,014.84 | 0.697 |
| margin | t2 | bear | 27 | -10,060.69 | 0.556 |
| margin | t2 | bull | 139 | 865.68 | 0.863 |
| margin | t2 | sideways | 174 | 10,502.45 | 0.684 |
| margin | t3 | bear | 24 | -9,037.16 | 0.583 |
| margin | t3 | bull | 117 | 2,143.54 | 0.863 |
| margin | t3 | sideways | 181 | -575.46 | 0.641 |
| cash_debit | control | bear | 155 | -28,140.64 | 0.406 |
| cash_debit | control | bull | 359 | 4,297.15 | 0.546 |
| cash_debit | t1 | bear | 162 | -46,874.63 | 0.383 |
| cash_debit | t1 | bull | 424 | 69,329.60 | 0.575 |
| cash_debit | t2 | bear | 174 | -69,540.86 | 0.425 |
| cash_debit | t2 | bull | 431 | 135,515.41 | 0.552 |
| cash_debit | t3 | bear | 76 | -19,531.25 | 0.461 |
| cash_debit | t3 | bull | 259 | -2,618.89 | 0.548 |

## By structure

| profile | arm | key | trades | net_pnl | win_rate |
|---|---|---|---|---|---|
| margin | control | bear_call | 21 | -8,592.71 | 0.524 |
| margin | control | bull_put | 101 | -1,553.40 | 0.842 |
| margin | control | iron_condor | 194 | 6,704.81 | 0.644 |
| margin | t1 | bear_call | 26 | -6,347.35 | 0.538 |
| margin | t1 | bull_put | 118 | -2,890.77 | 0.839 |
| margin | t1 | iron_condor | 178 | -5,014.84 | 0.697 |
| margin | t2 | bear_call | 27 | -10,060.69 | 0.556 |
| margin | t2 | bull_put | 139 | 865.68 | 0.863 |
| margin | t2 | iron_condor | 174 | 10,502.45 | 0.684 |
| margin | t3 | bear_call | 24 | -9,037.16 | 0.583 |
| margin | t3 | bull_put | 117 | 2,143.54 | 0.863 |
| margin | t3 | iron_condor | 181 | -575.46 | 0.641 |
| cash_debit | control | bear_put | 117 | -20,070.89 | 0.376 |
| cash_debit | control | bull_call | 205 | 3,903.30 | 0.551 |
| cash_debit | control | long_call | 154 | 393.85 | 0.539 |
| cash_debit | control | long_put | 38 | -8,069.75 | 0.5 |
| cash_debit | t1 | bear_put | 41 | -26,350.60 | 0.195 |
| cash_debit | t1 | bull_call | 23 | -16,434.85 | 0.565 |
| cash_debit | t1 | long_call | 401 | 85,764.45 | 0.576 |
| cash_debit | t1 | long_put | 121 | -20,524.03 | 0.446 |
| cash_debit | t2 | bear_put | 66 | -26,663.17 | 0.348 |
| cash_debit | t2 | bull_call | 66 | -14,371.58 | 0.485 |
| cash_debit | t2 | long_call | 365 | 149,886.99 | 0.564 |
| cash_debit | t2 | long_put | 108 | -42,877.69 | 0.472 |
| cash_debit | t3 | bear_put | 74 | -15,733.60 | 0.459 |
| cash_debit | t3 | bull_call | 85 | -4,332.54 | 0.635 |
| cash_debit | t3 | long_call | 174 | 1,713.65 | 0.506 |
| cash_debit | t3 | long_put | 2 | -3,797.65 | 0.5 |

## By universe tier

| profile | arm | key | trades | net_pnl | win_rate |
|---|---|---|---|---|---|
| margin | control | core | 217 | -10,789.39 | 0.673 |
| margin | control | momentum | 3 | 3,238.31 | 1 |
| margin | control | rest | 96 | 4,109.78 | 0.75 |
| margin | t1 | core | 224 | -20,733.28 | 0.705 |
| margin | t1 | momentum | 7 | 1,072.17 | 0.857 |
| margin | t1 | rest | 91 | 5,408.14 | 0.802 |
| margin | t2 | core | 231 | -2,565.51 | 0.727 |
| margin | t2 | momentum | 6 | 2,295.08 | 1 |
| margin | t2 | rest | 103 | 1,577.89 | 0.777 |
| margin | t3 | core | 215 | -16,857.49 | 0.688 |
| margin | t3 | momentum | 6 | 2,295.08 | 1 |
| margin | t3 | rest | 101 | 7,093.34 | 0.762 |
| cash_debit | control | core | 201 | -8,621.54 | 0.438 |
| cash_debit | control | momentum | 16 | -2,410.71 | 0.375 |
| cash_debit | control | rest | 297 | -12,811.24 | 0.556 |
| cash_debit | t1 | core | 260 | 20,140.95 | 0.473 |
| cash_debit | t1 | momentum | 36 | -24,227.20 | 0.556 |
| cash_debit | t1 | rest | 290 | 26,541.22 | 0.562 |
| cash_debit | t2 | core | 246 | -18,184.46 | 0.455 |
| cash_debit | t2 | momentum | 31 | 10,931.45 | 0.613 |
| cash_debit | t2 | rest | 328 | 73,227.57 | 0.552 |
| cash_debit | t3 | core | 162 | -4,791.16 | 0.463 |
| cash_debit | t3 | momentum | 5 | 3,109.61 | 0.4 |
| cash_debit | t3 | rest | 168 | -20,468.59 | 0.595 |

## By sector

| profile | arm | key | trades | net_pnl | win_rate |
|---|---|---|---|---|---|
| margin | control | broad_market | 159 | -10,937.16 | 0.673 |
| margin | control | communication | 1 | 80.93 | 1 |
| margin | control | consumer_discretionary | 14 | -7,429.91 | 0.429 |
| margin | control | consumer_staples | 1 | 469.45 | 1 |
| margin | control | energy | 3 | 1,052.45 | 0.667 |
| margin | control | financials | 14 | -448.64 | 0.643 |
| margin | control | health_care | 3 | 2,338.95 | 0.667 |
| margin | control | technology | 32 | 4,512.85 | 0.719 |
| margin | control | unknown | 89 | 6,919.77 | 0.787 |
| margin | t1 | broad_market | 166 | -18,272.12 | 0.687 |
| margin | t1 | communication | 2 | -1,878.06 | 0.5 |
| margin | t1 | consumer_discretionary | 10 | -505.39 | 0.6 |
| margin | t1 | energy | 3 | 751.18 | 0.667 |
| margin | t1 | financials | 12 | -1,224.57 | 0.833 |
| margin | t1 | health_care | 3 | 2,209.61 | 0.667 |
| margin | t1 | technology | 35 | -4,182.43 | 0.743 |
| margin | t1 | unknown | 91 | 8,848.81 | 0.835 |
| margin | t2 | broad_market | 183 | -3,423.41 | 0.727 |
| margin | t2 | communication | 1 | 175.93 | 1 |
| margin | t2 | consumer_discretionary | 8 | -875.21 | 0.625 |
| margin | t2 | consumer_staples | 1 | 384.10 | 1 |
| margin | t2 | energy | 3 | 1,194.13 | 0.667 |
| margin | t2 | financials | 15 | -742.14 | 0.667 |
| margin | t2 | health_care | 3 | 1,950.94 | 0.667 |
| margin | t2 | technology | 26 | -1,429.42 | 0.692 |
| margin | t2 | unknown | 100 | 4,072.53 | 0.82 |
| margin | t3 | broad_market | 163 | -11,658.80 | 0.693 |
| margin | t3 | communication | 1 | 175.93 | 1 |
| margin | t3 | consumer_discretionary | 11 | 1,184.92 | 0.727 |
| margin | t3 | consumer_staples | 1 | 384.10 | 1 |
| margin | t3 | energy | 3 | 390.10 | 0.667 |
| margin | t3 | financials | 13 | -3,807.60 | 0.538 |
| margin | t3 | health_care | 3 | 1,692.26 | 0.667 |
| margin | t3 | technology | 29 | -3,199.33 | 0.69 |
| margin | t3 | unknown | 98 | 7,369.34 | 0.786 |
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
| cash_debit | t1 | broad_market | 94 | 6,860.47 | 0.415 |
| cash_debit | t1 | communication | 14 | -11,671.28 | 0.571 |
| cash_debit | t1 | consumer_discretionary | 46 | -54,837.88 | 0.348 |
| cash_debit | t1 | energy | 3 | 1,252.61 | 0.333 |
| cash_debit | t1 | financials | 35 | 22,827.84 | 0.571 |
| cash_debit | t1 | health_care | 21 | 14,940.87 | 0.571 |
| cash_debit | t1 | industrials | 6 | -1,405.39 | 0.167 |
| cash_debit | t1 | technology | 170 | 39,282.33 | 0.576 |
| cash_debit | t1 | unknown | 197 | 5,205.41 | 0.563 |
| cash_debit | t2 | broad_market | 85 | 18,138.90 | 0.471 |
| cash_debit | t2 | communication | 14 | 19,359.48 | 0.571 |
| cash_debit | t2 | consumer_discretionary | 34 | -110,968.57 | 0.235 |
| cash_debit | t2 | energy | 6 | -6,390.35 | 0.167 |
| cash_debit | t2 | financials | 44 | -5,256.75 | 0.455 |
| cash_debit | t2 | health_care | 23 | 645.29 | 0.565 |
| cash_debit | t2 | industrials | 5 | 185.27 | 0.4 |
| cash_debit | t2 | technology | 174 | 133,227.76 | 0.563 |
| cash_debit | t2 | unknown | 220 | 17,033.54 | 0.555 |
| cash_debit | t3 | broad_market | 101 | 16,240.12 | 0.475 |
| cash_debit | t3 | communication | 2 | 543.07 | 1 |
| cash_debit | t3 | consumer_discretionary | 12 | -5,562.09 | 0.333 |
| cash_debit | t3 | consumer_staples | 1 | -628.48 | 0 |
| cash_debit | t3 | energy | 12 | -6,350.40 | 0.417 |
| cash_debit | t3 | financials | 31 | 1,636.11 | 0.516 |
| cash_debit | t3 | health_care | 7 | -2,054.31 | 0.286 |
| cash_debit | t3 | industrials | 2 | -1,207.76 | 0.5 |
| cash_debit | t3 | technology | 27 | -13,153.19 | 0.407 |
| cash_debit | t3 | unknown | 140 | -11,613.20 | 0.629 |

## ETF vs single stock

| profile | arm | key | trades | net_pnl | win_rate |
|---|---|---|---|---|---|
| margin | control | ETF | 237 | -3,180.95 | 0.722 |
| margin | control | single stock | 79 | -260.35 | 0.633 |
| margin | t1 | ETF | 242 | -12,176.38 | 0.731 |
| margin | t1 | single stock | 80 | -2,076.58 | 0.75 |
| margin | t2 | ETF | 274 | -1,109.49 | 0.755 |
| margin | t2 | single stock | 66 | 2,416.94 | 0.712 |
| margin | t3 | ETF | 250 | -8,328.77 | 0.72 |
| margin | t3 | single stock | 72 | 859.70 | 0.708 |
| cash_debit | control | ETF | 180 | 1,828.85 | 0.567 |
| cash_debit | control | single stock | 334 | -25,672.34 | 0.47 |
| cash_debit | t1 | ETF | 168 | 34,748.29 | 0.53 |
| cash_debit | t1 | single stock | 418 | -12,293.32 | 0.519 |
| cash_debit | t2 | ETF | 187 | 25,939.71 | 0.524 |
| cash_debit | t2 | single stock | 418 | 40,034.85 | 0.512 |
| cash_debit | t3 | ETF | 209 | 13,372.89 | 0.569 |
| cash_debit | t3 | single stock | 126 | -35,523.03 | 0.46 |

## By liquidity bucket

| profile | arm | key | trades | net_pnl | win_rate |
|---|---|---|---|---|---|
| margin | control | 100k-1M | 124 | 1,188.34 | 0.702 |
| margin | control | 10k-100k | 33 | 7,178.93 | 0.697 |
| margin | control | >= 1M | 159 | -11,808.57 | 0.698 |
| margin | t1 | 100k-1M | 122 | -4,684.07 | 0.762 |
| margin | t1 | 10k-100k | 37 | 7,681.61 | 0.865 |
| margin | t1 | >= 1M | 163 | -17,250.50 | 0.687 |
| margin | t2 | 100k-1M | 131 | -5,271.09 | 0.718 |
| margin | t2 | 10k-100k | 37 | 7,888.40 | 0.838 |
| margin | t2 | >= 1M | 172 | -1,309.86 | 0.75 |
| margin | t3 | 100k-1M | 126 | 375.97 | 0.722 |
| margin | t3 | 10k-100k | 34 | 5,081.99 | 0.794 |
| margin | t3 | >= 1M | 162 | -12,927.03 | 0.698 |
| cash_debit | control | 100k-1M | 278 | -6,343.49 | 0.54 |
| cash_debit | control | 10k-100k | 94 | -22.81 | 0.521 |
| cash_debit | control | < 10k | 32 | -11,029.65 | 0.406 |
| cash_debit | control | >= 1M | 110 | -6,447.55 | 0.427 |
| cash_debit | t1 | 100k-1M | 315 | -36,844.95 | 0.521 |
| cash_debit | t1 | 10k-100k | 78 | 25,299.76 | 0.59 |
| cash_debit | t1 | < 10k | 47 | 14,482.24 | 0.489 |
| cash_debit | t1 | >= 1M | 146 | 19,517.92 | 0.5 |
| cash_debit | t2 | 100k-1M | 332 | -22,384.23 | 0.479 |
| cash_debit | t2 | 10k-100k | 100 | -3,369.14 | 0.55 |
| cash_debit | t2 | < 10k | 48 | 1,989.17 | 0.646 |
| cash_debit | t2 | >= 1M | 125 | 89,738.75 | 0.536 |
| cash_debit | t3 | 100k-1M | 154 | -21,978.32 | 0.584 |
| cash_debit | t3 | 10k-100k | 65 | -11,844.38 | 0.492 |
| cash_debit | t3 | < 10k | 18 | -8,672.27 | 0.444 |
| cash_debit | t3 | >= 1M | 98 | 20,344.83 | 0.48 |

## Uncertainty

Moving-block bootstrap CI of the daily equity-change difference vs control (seeded), and the fixed sub-periods each treatment won on net P&L.

| profile | treatment | pnl_diff | ci_lo | ci_hi | periods_won |
|---|---|---|---|---|---|
| margin | t1 | -10,811.66 | -32,654.26 | 7,403.07 | – |
| margin | t2 | 4,748.75 | -18,822.56 | 30,656.99 | 2024 -> latest |
| margin | t3 | -4,027.78 | -25,737.54 | 17,171.12 | – |
| cash_debit | t1 | 46,298.46 | -196,191.84 | 294,267.56 | 2024 -> latest |
| cash_debit | t2 | 89,818.05 | -201,923.03 | 385,931.71 | 2024 -> latest |
| cash_debit | t3 | 1,693.36 | -44,547.58 | 52,173.92 | 2024 -> latest |

## Checkpoint ladder

Every ledger run of this arm set; `sign_flip_vs_first_rung` compares the primary metric's treatment − control sign with the first dev rung.

| at | phase | universe | profile | treatment | primary_diff | ci_lo | ci_hi | switch | sign_flip_vs_first_rung |
|---|---|---|---|---|---|---|---|---|---|
| 2026-10-15T22:49:42-04:00 | dev | c125 | cash_debit | t1 | -5.84 | -46,262.80 | 42,880.63 | False | False |
| 2026-10-15T22:49:42-04:00 | dev | c125 | cash_debit | t2 | -1.92 | -43,586.08 | 46,073.31 | False | False |
| 2026-10-15T22:49:42-04:00 | dev | c125 | cash_debit | t3 | 16.68 | -56,522.78 | 56,569.11 | False | False |
| 2026-10-15T22:49:42-04:00 | dev | c125 | margin | t1 | -1,101.53 | -17,945.65 | 15,794.08 | False | False |
| 2026-10-15T22:49:42-04:00 | dev | c125 | margin | t2 | 4,119.99 | -9,951.27 | 17,285.09 | False | False |
| 2026-10-15T22:49:42-04:00 | dev | c125 | margin | t3 | 5,083.75 | -8,630.32 | 18,152.39 | False | False |
| 2026-10-16T12:37:04-04:00 | dev | c250 | cash_debit | t1 | 17.28 | -54,886.10 | 52,883.29 | False | True |
| 2026-10-16T12:37:04-04:00 | dev | c250 | cash_debit | t2 | 42.33 | -42,356.71 | 46,210.51 | False | True |
| 2026-10-16T12:37:04-04:00 | dev | c250 | cash_debit | t3 | 24.66 | -37,583.47 | 37,667.31 | False | False |
| 2026-10-16T12:37:04-04:00 | dev | c250 | margin | t1 | -3,812.05 | -17,570.65 | 10,568.56 | False | False |
| 2026-10-16T12:37:04-04:00 | dev | c250 | margin | t2 | -2,762.45 | -15,275.61 | 10,614.28 | False | True |
| 2026-10-16T12:37:04-04:00 | dev | c250 | margin | t3 | -1,823.72 | -13,926.55 | 10,682.01 | False | True |
| 2026-10-16T10:10:58-04:00 | dev | c500 | cash_debit | t1 | 57.09 | -77,134.62 | 73,670.82 | False | True |
| 2026-10-16T10:10:58-04:00 | dev | c500 | cash_debit | t2 | 33.09 | -66,284.82 | 73,642.86 | False | True |
| 2026-10-16T10:10:58-04:00 | dev | c500 | cash_debit | t3 | 64.84 | -69,542.47 | 66,784.00 | False | False |
| 2026-10-16T10:10:58-04:00 | dev | c500 | margin | t1 | -2,355.32 | -19,951.18 | 15,880.50 | False | False |
| 2026-10-16T10:10:58-04:00 | dev | c500 | margin | t2 | -6,708.56 | -23,580.58 | 10,122.10 | False | True |
| 2026-10-16T10:10:58-04:00 | dev | c500 | margin | t3 | -1,721.43 | -21,277.63 | 17,454.24 | False | True |
| 2026-10-16T12:53:32-04:00 | holdout | c500 | cash_debit | t1 | 46,298.46 | -196,191.84 | 294,267.56 | False | True |
| 2026-10-16T12:53:32-04:00 | holdout | c500 | cash_debit | t2 | 89,818.04 | -201,923.03 | 385,931.71 | False | True |
| 2026-10-16T12:53:32-04:00 | holdout | c500 | cash_debit | t3 | 1,693.35 | -44,547.58 | 52,173.92 | False | False |
| 2026-10-16T12:53:32-04:00 | holdout | c500 | margin | t1 | -10,811.66 | -32,654.26 | 7,403.07 | False | False |
| 2026-10-16T12:53:32-04:00 | holdout | c500 | margin | t2 | 4,748.75 | -18,822.56 | 30,656.99 | False | False |
| 2026-10-16T12:53:32-04:00 | holdout | c500 | margin | t3 | -4,027.77 | -25,737.54 | 17,171.12 | False | True |
