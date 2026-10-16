# BT-3 · E7.5 re-run: no challenger ranker (managed_net_ev, rorc_day, rorc_day_vrp) beats the profile's incumbent under the D25 rule on real quotes.

## Header

- phase **dev**; spec hash `6bd6ea4a7b93`; arm set `924a715d3ba9`; owner owner; created 2026-10-15
- rule: Switch the default to a challenger only if it beats the incumbent on net P&L (higher) **and** max drawdown (not larger) in ≥ 2 of 3 sub-periods (bear, sideways, bull trend regime at entry), **and** the 90% block-bootstrap CI of its daily P&L difference vs the incumbent excludes 0 (lower bound > 0). Otherwise keep the incumbent and report the closest challenger.
- primary metric: `net_pnl`
- universe **c125** (ladder c125, c250, c500), version `u500-20261009-s1-e52ff301`; data partitions {'thetadata': 125}
- decisions 2020-01-01 -> 2023-12-31 (exits settle by 2024-03-30); dev 2020-01-01 -> 2023-12-31; holdout 2024-01-01 -> latest
- Not replayed: Discovery and Trending tiers (no historical LLM, Reddit or Stocktwits inputs), the LLM stance (a deterministic trend-label proxy stands in), intraday paths (EOD marks only; exits fire on closes).
- git `d3fa67192c84`; variants tried 1 of max 3; earlier holdout runs 0
- arms: control = ranker incumbent; t1 = ranker managed_net_ev; t2 = ranker rorc_day; t3 = ranker rorc_day_vrp

## Verdict

The pre-registered rule, verbatim:

> Switch the default to a challenger only if it beats the incumbent on net P&L (higher) **and** max drawdown (not larger) in ≥ 2 of 3 sub-periods (bear, sideways, bull trend regime at entry), **and** the 90% block-bootstrap CI of its daily P&L difference vs the incumbent excludes 0 (lower bound > 0). Otherwise keep the incumbent and report the closest challenger.

Original card's wording: _Switch the default to a challenger only if it beats the incumbent on net P&L (higher) **and** max drawdown (not larger) in ≥ 2 of 3 sub-periods (bear, sideways, bull trend regime at entry), **and** the 90% block-bootstrap CI of its daily P&L difference vs the incumbent excludes 0 (lower bound > 0). Otherwise keep the incumbent and report the closest challenger._

- dev result (not a verdict) · margin · t1 vs control: keep control (sub-periods 1 [sideways], P&L diff -1,102, CI [-17,946, 15,794])
- dev result (not a verdict) · margin · t2 vs control: keep control (sub-periods 1 [sideways], P&L diff 4,120, CI [-9,951, 17,285])
- dev result (not a verdict) · margin · t3 vs control: keep control (sub-periods 1 [sideways], P&L diff 5,084, CI [-8,630, 18,152])
- dev result (not a verdict) · cash_debit · t1 vs control: keep control (sub-periods 1 [bear], P&L diff -6, CI [-46,263, 42,881])
- dev result (not a verdict) · cash_debit · t2 vs control: keep control (sub-periods 1 [bear], P&L diff -2, CI [-43,586, 46,073])
- dev result (not a verdict) · cash_debit · t3 vs control: keep control (sub-periods 1 [bear], P&L diff 17, CI [-56,523, 56,569])

A verdict only recommends: a live change still goes through D44 (`Experiment: XP-<n>` with arms copied from this spec) or an owner waiver.

## Per arm × profile

| profile | arm | trades | net_pnl | return_pct | cagr | max_dd_pct | sharpe | sortino | profit_factor | win_rate | avg_days_held | turnover_per_month | cost_share_of_gross | capacity_clipped_share |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| margin | control | 440 | -17,294.35 | -69.18 | -0.2426 | 83 | -0.354 | -0.291 | 0.789 | 0.732 | 21 | 8.66 | 1.845 | 0.373 |
| margin | t1 | 463 | -18,395.88 | -73.58 | -0.2697 | 82.94 | -0.482 | -0.385 | 0.795 | 0.756 | 20.9 | 9.11 | 1.709 | 0.425 |
| margin | t2 | 470 | -13,174.36 | -52.7 | -0.162 | 68.03 | -0.189 | -0.158 | 0.83 | 0.766 | 20.5 | 9.25 | 3.891 | 0.4 |
| margin | t3 | 444 | -12,210.60 | -48.84 | -0.1464 | 66.27 | -0.178 | -0.148 | 0.832 | 0.761 | 21 | 8.74 | 3.697 | 0.401 |
| cash_debit | control | 585 | -24,879.91 | -99.52 | -0.7165 | 99.74 | -0.28 | -0.284 | 0.87 | 0.504 | 14.7 | 11.51 | 6.399 | 0.354 |
| cash_debit | t1 | 406 | -24,885.75 | -99.54 | -0.7198 | 99.77 | -0.665 | -0.597 | 0.757 | 0.453 | 15.2 | 7.99 | 0.65 | 0.182 |
| cash_debit | t2 | 361 | -24,881.83 | -99.53 | -0.7175 | 99.62 | -0.643 | -0.581 | 0.603 | 0.463 | 14.8 | 7.1 | 0.333 | 0.177 |
| cash_debit | t3 | 329 | -24,863.23 | -99.45 | -0.7076 | 99.76 | -0.853 | -0.682 | 0.845 | 0.492 | 15.6 | 6.47 | 2.074 | 0.325 |

## All / Stocks / ETFs

One pool per run; the verdict is stated on All, Stocks and ETFs are diagnostic. `dd_contribution` = net P&L of the trades closed inside the run's max-drawdown window; `spread_cost` = P&L at mid − net P&L (spread + fees).

| profile | arm | segment | trades | win_rate | net_pnl | mean_pnl | median_pnl | pnl_per_risk | dd_contribution | spread_cost | exit_reasons |
|---|---|---|---|---|---|---|---|---|---|---|---|
| margin | control | All | 440 | 0.732 | -17,294.35 | -39.31 | 57.38 | -0.039 | -23,257.07 | 11,215.85 | dte_exit 44, expiry 71, stop 36, take_profit 289 |
| margin | control | Stocks | 117 | 0.744 | -3,799.55 | -32.47 | 87.18 | -0.0281 | -6,401.39 | 4,443.55 | dte_exit 8, expiry 31, stop 7, take_profit 71 |
| margin | control | ETFs | 323 | 0.728 | -13,494.80 | -41.78 | 48.59 | -0.0438 | -16,855.68 | 6,772.30 | dte_exit 36, expiry 40, stop 29, take_profit 218 |
| margin | t1 | All | 463 | 0.756 | -18,395.88 | -39.73 | 62.93 | -0.0339 | -24,702.60 | 11,605.38 | dte_exit 45, expiry 75, stop 37, take_profit 306 |
| margin | t1 | Stocks | 114 | 0.781 | -8,114.96 | -71.18 | 63.3 | -0.052 | -9,397.07 | 4,066.46 | dte_exit 5, expiry 28, stop 7, take_profit 74 |
| margin | t1 | ETFs | 349 | 0.748 | -10,280.92 | -29.46 | 62.36 | -0.0266 | -15,305.52 | 7,538.92 | dte_exit 40, expiry 47, stop 30, take_profit 232 |
| margin | t2 | All | 470 | 0.766 | -13,174.36 | -28.03 | 64.09 | -0.0267 | -17,425.35 | 10,480.86 | dte_exit 51, expiry 73, stop 32, take_profit 314 |
| margin | t2 | Stocks | 122 | 0.795 | -2,033.47 | -16.67 | 85.55 | -0.0139 | -3,940.38 | 3,959.47 | dte_exit 7, expiry 31, stop 5, take_profit 79 |
| margin | t2 | ETFs | 348 | 0.756 | -11,140.89 | -32.01 | 55.76 | -0.032 | -13,484.97 | 6,521.39 | dte_exit 44, expiry 42, stop 27, take_profit 235 |
| margin | t3 | All | 444 | 0.761 | -12,210.60 | -27.5 | 62.86 | -0.0271 | -16,549.75 | 9,611.10 | dte_exit 43, expiry 77, stop 32, take_profit 292 |
| margin | t3 | Stocks | 113 | 0.779 | -2,833.56 | -25.08 | 80.92 | -0.0213 | -5,533.52 | 3,680.56 | dte_exit 7, expiry 29, stop 6, take_profit 71 |
| margin | t3 | ETFs | 331 | 0.755 | -9,377.03 | -28.33 | 56.59 | -0.0296 | -11,016.23 | 5,930.53 | dte_exit 36, expiry 48, stop 26, take_profit 221 |
| cash_debit | control | All | 585 | 0.504 | -24,879.91 | -42.53 | 2.34 | -0.053 | -44,734.41 | 29,488.41 | dte_exit 22, expiry 18, profit_lock 34, stop 231, take_profit 280 |
| cash_debit | control | Stocks | 402 | 0.502 | -34,647.76 | -86.19 | 1.13 | -0.1151 | -39,129.42 | 17,017.76 | dte_exit 16, expiry 13, profit_lock 30, stop 153, take_profit 190 |
| cash_debit | control | ETFs | 183 | 0.508 | 9,767.85 | 53.38 | 34.42 | 0.0581 | -5,605.00 | 12,470.65 | dte_exit 6, expiry 5, profit_lock 4, stop 78, take_profit 90 |
| cash_debit | t1 | All | 406 | 0.453 | -24,885.75 | -61.29 | -22.5 | -0.1047 | -47,555.43 | 9,800.75 | dte_exit 21, expiry 14, profit_lock 26, stop 171, take_profit 174 |
| cash_debit | t1 | Stocks | 283 | 0.438 | -18,578.02 | -65.65 | -22.58 | -0.1288 | -37,172.34 | 4,105.52 | dte_exit 16, expiry 10, profit_lock 22, stop 118, take_profit 117 |
| cash_debit | t1 | ETFs | 123 | 0.488 | -6,307.72 | -51.28 | -18.58 | -0.0674 | -10,383.09 | 5,695.22 | dte_exit 5, expiry 4, profit_lock 4, stop 53, take_profit 57 |
| cash_debit | t2 | All | 361 | 0.463 | -24,881.83 | -68.92 | -18.41 | -0.1882 | -30,447.37 | 6,210.83 | dte_exit 15, expiry 12, profit_lock 20, stop 151, take_profit 163 |
| cash_debit | t2 | Stocks | 247 | 0.437 | -27,202.63 | -110.13 | -19.82 | -0.404 | -25,471.10 | 2,723.63 | dte_exit 10, expiry 9, profit_lock 16, stop 107, take_profit 105 |
| cash_debit | t2 | ETFs | 114 | 0.518 | 2,320.80 | 20.36 | 17.5 | 0.0358 | -4,976.26 | 3,487.20 | dte_exit 5, expiry 3, profit_lock 4, stop 44, take_profit 58 |
| cash_debit | t3 | All | 329 | 0.492 | -24,863.23 | -75.57 | -11.58 | -0.0665 | -54,284.46 | 16,774.73 | dte_exit 17, expiry 9, profit_lock 14, stop 139, take_profit 150 |
| cash_debit | t3 | Stocks | 181 | 0.42 | -49,405.16 | -272.96 | -44.16 | -0.2504 | -38,060.25 | 6,746.16 | dte_exit 9, expiry 5, profit_lock 9, stop 87, take_profit 71 |
| cash_debit | t3 | ETFs | 148 | 0.581 | 24,541.93 | 165.82 | 73.01 | 0.1391 | -16,224.20 | 10,028.57 | dte_exit 8, expiry 4, profit_lock 5, stop 52, take_profit 79 |

Treatment − control net P&L by segment (**sign differs between Stocks and ETFs for 4 treatment(s)**):

| profile | treatment | All | Stocks | ETFs | sign_differs |
|---|---|---|---|---|---|
| margin | t1 | -1,101.53 | -4,315.41 | 3,213.88 | True |
| margin | t2 | 4,119.99 | 1,766.08 | 2,353.91 | False |
| margin | t3 | 5,083.75 | 965.99 | 4,117.77 | False |
| cash_debit | t1 | -5.84 | 16,069.74 | -16,075.57 | True |
| cash_debit | t2 | -1.92 | 7,445.13 | -7,447.05 | True |
| cash_debit | t3 | 16.68 | -14,757.40 | 14,774.08 | True |

## Exit-reason mix

| profile | arm | mix |
|---|---|---|
| margin | control | dte_exit 0.1, expiry 0.1614, stop 0.0818, take_profit 0.6568 |
| margin | t1 | dte_exit 0.0972, expiry 0.162, stop 0.0799, take_profit 0.6609 |
| margin | t2 | dte_exit 0.1085, expiry 0.1553, stop 0.0681, take_profit 0.6681 |
| margin | t3 | dte_exit 0.0968, expiry 0.1734, stop 0.0721, take_profit 0.6577 |
| cash_debit | control | dte_exit 0.0376, expiry 0.0308, profit_lock 0.0581, stop 0.3949, take_profit 0.4786 |
| cash_debit | t1 | dte_exit 0.0517, expiry 0.0345, profit_lock 0.064, stop 0.4212, take_profit 0.4286 |
| cash_debit | t2 | dte_exit 0.0416, expiry 0.0332, profit_lock 0.0554, stop 0.4183, take_profit 0.4515 |
| cash_debit | t3 | dte_exit 0.0517, expiry 0.0274, profit_lock 0.0426, stop 0.4225, take_profit 0.4559 |

## Skip reasons

| profile | arm | screen | blackout | no_candidate | unsettled | sizing | gate_per_underlying | gate_max_open_positions | gate_daily_loss | gate_max_gain | cash | capacity | no_fill | exit_delayed |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| margin | control | 71615 | 739 | 33046 | 0 | 1559 | 0 | 2970 | 4506 | 0 | 0 | 1001 | 227 | 24 |
| margin | t1 | 71615 | 701 | 33343 | 0 | 1309 | 0 | 3300 | 4148 | 0 | 0 | 1083 | 241 | 22 |
| margin | t2 | 71615 | 721 | 33408 | 0 | 1308 | 0 | 3429 | 4069 | 0 | 0 | 984 | 210 | 20 |
| margin | t3 | 71615 | 684 | 33942 | 0 | 1189 | 0 | 3564 | 3621 | 0 | 0 | 935 | 195 | 18 |
| cash_debit | control | 71615 | 0 | 24610 | 0 | 5693 | 0 | 3633 | 11245 | 0 | 64 | 492 | 196 | 52 |
| cash_debit | t1 | 71615 | 0 | 26207 | 0 | 8368 | 0 | 1677 | 9503 | 0 | 29 | 227 | 148 | 46 |
| cash_debit | t2 | 71615 | 0 | 26292 | 0 | 8815 | 0 | 1222 | 9450 | 0 | 34 | 212 | 109 | 26 |
| cash_debit | t3 | 71615 | 0 | 30636 | 0 | 2198 | 0 | 671 | 8173 | 0 | 1 | 238 | 83 | 36 |

## Sub-periods

Fixed windows by entry date.

| profile | arm | period | trades | net_pnl | win_rate | max_dd_pct |
|---|---|---|---|---|---|---|
| margin | control | 2020 crash | 18 | -7,279.60 | 0.5 | 40.18 |
| margin | control | 2020-21 bull | 210 | 6,664.44 | 0.8 | 39.67 |
| margin | control | 2022 bear | 77 | -13,266.13 | 0.636 | 59.39 |
| margin | control | 2023 | 115 | -1,794.86 | 0.704 | 59.13 |
| margin | control | 2024 -> latest | 0 | 0 | – | 3.56 |
| margin | t1 | 2020 crash | 25 | -9,617.67 | 0.52 | 31.65 |
| margin | t1 | 2020-21 bull | 212 | 7,498.12 | 0.807 | 30.98 |
| margin | t1 | 2022 bear | 82 | -12,039.17 | 0.683 | 50.21 |
| margin | t1 | 2023 | 123 | -8,020.41 | 0.724 | 70.06 |
| margin | t1 | 2024 -> latest | 0 | 0 | – | 4.65 |
| margin | t2 | 2020 crash | 23 | -11,845.52 | 0.522 | 46.28 |
| margin | t2 | 2020-21 bull | 208 | 7,796.91 | 0.832 | 22.37 |
| margin | t2 | 2022 bear | 80 | -12,626.66 | 0.637 | 60.08 |
| margin | t2 | 2023 | 139 | 1,849.06 | 0.77 | 36.74 |
| margin | t2 | 2024 -> latest | 0 | 0 | – | 3.35 |
| margin | t3 | 2020 crash | 20 | -10,149.91 | 0.55 | 41.62 |
| margin | t3 | 2020-21 bull | 203 | 2,838.96 | 0.813 | 24.71 |
| margin | t3 | 2022 bear | 68 | -9,458.54 | 0.632 | 54.06 |
| margin | t3 | 2023 | 133 | 2,907.06 | 0.767 | 26.78 |
| margin | t3 | 2024 -> latest | 0 | 0 | – | 3.91 |
| cash_debit | control | 2020 crash | 53 | -18,862.39 | 0.528 | 65.98 |
| cash_debit | control | 2020-21 bull | 317 | -11,573.07 | 0.502 | 92.84 |
| cash_debit | control | 2022 bear | 144 | -2,902.56 | 0.521 | 94.42 |
| cash_debit | control | 2023 | 44 | -490.34 | 0.318 | 85.59 |
| cash_debit | control | 2024 -> latest | 0 | 0 | – | 0 |
| cash_debit | t1 | 2020 crash | 44 | -20,724.90 | 0.432 | 84.43 |
| cash_debit | t1 | 2020-21 bull | 257 | -6,082.70 | 0.459 | 93.96 |
| cash_debit | t1 | 2022 bear | 56 | -799.80 | 0.446 | 76.63 |
| cash_debit | t1 | 2023 | 23 | -233.53 | 0.348 | 73.2 |
| cash_debit | t1 | 2024 -> latest | 0 | 0 | – | 0 |
| cash_debit | t2 | 2020 crash | 37 | -27,587.48 | 0.216 | 79.43 |
| cash_debit | t2 | 2020-21 bull | 198 | -4,148.10 | 0.485 | 95.33 |
| cash_debit | t2 | 2022 bear | 76 | -585.35 | 0.5 | 74.34 |
| cash_debit | t2 | 2023 | 24 | -250.85 | 0.333 | 73.6 |
| cash_debit | t2 | 2024 -> latest | 0 | 0 | – | 0 |
| cash_debit | t3 | 2020 crash | 40 | -12,876.94 | 0.625 | 57.71 |
| cash_debit | t3 | 2020-21 bull | 216 | -18,065.66 | 0.463 | 97.59 |
| cash_debit | t3 | 2022 bear | 34 | -864.05 | 0.382 | 69.86 |
| cash_debit | t3 | 2023 | 12 | -310.09 | 0.167 | 71.57 |
| cash_debit | t3 | 2024 -> latest | 0 | 0 | – | 0 |

## By regime

| profile | arm | key | trades | net_pnl | win_rate |
|---|---|---|---|---|---|
| margin | control | bear | 53 | 2,386.00 | 0.774 |
| margin | control | bull | 207 | -12,457.29 | 0.821 |
| margin | control | sideways | 180 | -7,223.06 | 0.617 |
| margin | t1 | bear | 65 | 1,063.82 | 0.754 |
| margin | t1 | bull | 219 | -17,525.86 | 0.822 |
| margin | t1 | sideways | 179 | -1,933.84 | 0.676 |
| margin | t2 | bear | 57 | 2,976.99 | 0.772 |
| margin | t2 | bull | 230 | -17,571.91 | 0.835 |
| margin | t2 | sideways | 183 | 1,420.56 | 0.678 |
| margin | t3 | bear | 52 | 2,788.28 | 0.808 |
| margin | t3 | bull | 207 | -14,099.65 | 0.845 |
| margin | t3 | sideways | 185 | -899.23 | 0.654 |
| cash_debit | control | bear | 138 | -34,194.76 | 0.377 |
| cash_debit | control | bull | 447 | 9,314.85 | 0.544 |
| cash_debit | t1 | bear | 78 | -15,544.73 | 0.359 |
| cash_debit | t1 | bull | 328 | -9,341.02 | 0.476 |
| cash_debit | t2 | bear | 75 | -13,865.34 | 0.387 |
| cash_debit | t2 | bull | 286 | -11,016.48 | 0.483 |
| cash_debit | t3 | bear | 52 | -20,318.35 | 0.288 |
| cash_debit | t3 | bull | 277 | -4,544.88 | 0.531 |

## By structure

| profile | arm | key | trades | net_pnl | win_rate |
|---|---|---|---|---|---|
| margin | control | bear_call | 53 | 2,386.00 | 0.774 |
| margin | control | bull_put | 207 | -12,457.29 | 0.821 |
| margin | control | iron_condor | 180 | -7,223.06 | 0.617 |
| margin | t1 | bear_call | 65 | 1,063.82 | 0.754 |
| margin | t1 | bull_put | 219 | -17,525.86 | 0.822 |
| margin | t1 | iron_condor | 179 | -1,933.84 | 0.676 |
| margin | t2 | bear_call | 57 | 2,976.99 | 0.772 |
| margin | t2 | bull_put | 230 | -17,571.91 | 0.835 |
| margin | t2 | iron_condor | 183 | 1,420.56 | 0.678 |
| margin | t3 | bear_call | 52 | 2,788.28 | 0.808 |
| margin | t3 | bull_put | 207 | -14,099.65 | 0.845 |
| margin | t3 | iron_condor | 185 | -899.23 | 0.654 |
| cash_debit | control | bear_put | 122 | -19,174.39 | 0.369 |
| cash_debit | control | bull_call | 190 | -3,610.58 | 0.532 |
| cash_debit | control | long_call | 257 | 12,925.43 | 0.553 |
| cash_debit | control | long_put | 16 | -15,020.37 | 0.438 |
| cash_debit | t1 | bear_put | 58 | -1,933.86 | 0.379 |
| cash_debit | t1 | bull_call | 67 | 955.00 | 0.567 |
| cash_debit | t1 | long_call | 261 | -10,296.01 | 0.452 |
| cash_debit | t1 | long_put | 20 | -13,610.87 | 0.3 |
| cash_debit | t2 | bear_put | 61 | -3,821.21 | 0.426 |
| cash_debit | t2 | bull_call | 116 | 1,447.72 | 0.603 |
| cash_debit | t2 | long_call | 170 | -12,464.20 | 0.4 |
| cash_debit | t2 | long_put | 14 | -10,044.14 | 0.214 |
| cash_debit | t3 | bear_put | 45 | -7,524.98 | 0.289 |
| cash_debit | t3 | bull_call | 86 | -1,085.58 | 0.616 |
| cash_debit | t3 | long_call | 191 | -3,459.30 | 0.492 |
| cash_debit | t3 | long_put | 7 | -12,793.36 | 0.286 |

## By universe tier

| profile | arm | key | trades | net_pnl | win_rate |
|---|---|---|---|---|---|
| margin | control | core | 311 | -397.20 | 0.746 |
| margin | control | momentum | 11 | -1,295.17 | 0.636 |
| margin | control | rest | 118 | -15,601.97 | 0.703 |
| margin | t1 | core | 332 | 2,320.64 | 0.765 |
| margin | t1 | momentum | 15 | 74.58 | 0.733 |
| margin | t1 | rest | 116 | -20,791.10 | 0.733 |
| margin | t2 | core | 329 | -271.36 | 0.766 |
| margin | t2 | momentum | 14 | 473.14 | 0.786 |
| margin | t2 | rest | 127 | -13,376.15 | 0.764 |
| margin | t3 | core | 309 | 1,855.43 | 0.761 |
| margin | t3 | momentum | 12 | 504.65 | 0.75 |
| margin | t3 | rest | 123 | -14,570.68 | 0.764 |
| cash_debit | control | core | 258 | -12,954.57 | 0.446 |
| cash_debit | control | momentum | 32 | -7,769.23 | 0.469 |
| cash_debit | control | rest | 295 | -4,156.11 | 0.559 |
| cash_debit | t1 | core | 165 | -22,749.62 | 0.412 |
| cash_debit | t1 | momentum | 27 | -1,939.69 | 0.407 |
| cash_debit | t1 | rest | 214 | -196.43 | 0.491 |
| cash_debit | t2 | core | 128 | -11,028.38 | 0.398 |
| cash_debit | t2 | momentum | 22 | -368.71 | 0.545 |
| cash_debit | t2 | rest | 211 | -13,484.74 | 0.493 |
| cash_debit | t3 | core | 162 | -4,242.81 | 0.463 |
| cash_debit | t3 | momentum | 19 | -5,992.01 | 0.368 |
| cash_debit | t3 | rest | 148 | -14,628.40 | 0.541 |

## By sector

| profile | arm | key | trades | net_pnl | win_rate |
|---|---|---|---|---|---|
| margin | control | broad_market | 261 | -2,869.72 | 0.732 |
| margin | control | communication | 13 | -2,304.24 | 0.538 |
| margin | control | consumer_discretionary | 5 | 1,461.04 | 1 |
| margin | control | consumer_staples | 2 | -69.32 | 0.5 |
| margin | control | energy | 3 | -677.97 | 0.667 |
| margin | control | financials | 25 | -4,171.39 | 0.68 |
| margin | control | health_care | 10 | -1,763.29 | 0.9 |
| margin | control | industrials | 4 | 1,514.76 | 0.75 |
| margin | control | technology | 30 | 1,888.25 | 0.833 |
| margin | control | unknown | 87 | -10,302.46 | 0.713 |
| margin | t1 | broad_market | 286 | 3,119.31 | 0.762 |
| margin | t1 | communication | 8 | -1,331.56 | 0.875 |
| margin | t1 | consumer_discretionary | 8 | 708.61 | 0.75 |
| margin | t1 | consumer_staples | 1 | 6.84 | 1 |
| margin | t1 | energy | 2 | -1,420.78 | 0.5 |
| margin | t1 | financials | 24 | -6,320.80 | 0.667 |
| margin | t1 | health_care | 10 | -1,346.54 | 0.8 |
| margin | t1 | industrials | 9 | 303.71 | 0.778 |
| margin | t1 | technology | 25 | -3,489.40 | 0.8 |
| margin | t1 | unknown | 90 | -8,625.26 | 0.733 |
| margin | t2 | broad_market | 282 | -882.75 | 0.762 |
| margin | t2 | communication | 14 | -1,419.00 | 0.714 |
| margin | t2 | consumer_discretionary | 6 | -611.35 | 0.5 |
| margin | t2 | consumer_staples | 1 | 6.84 | 1 |
| margin | t2 | energy | 3 | -1,071.03 | 0.667 |
| margin | t2 | financials | 26 | -2,742.75 | 0.692 |
| margin | t2 | health_care | 12 | -1,665.05 | 0.917 |
| margin | t2 | industrials | 4 | 2,289.14 | 1 |
| margin | t2 | technology | 30 | 44.15 | 0.9 |
| margin | t2 | unknown | 92 | -7,122.56 | 0.75 |
| margin | t3 | broad_market | 269 | 2,548.36 | 0.762 |
| margin | t3 | communication | 12 | -1,133.86 | 0.75 |
| margin | t3 | consumer_discretionary | 6 | -611.35 | 0.5 |
| margin | t3 | consumer_staples | 1 | 6.84 | 1 |
| margin | t3 | energy | 2 | -1,663.85 | 0.5 |
| margin | t3 | financials | 22 | -3,322.06 | 0.636 |
| margin | t3 | health_care | 11 | -2,115.88 | 0.909 |
| margin | t3 | industrials | 3 | 1,917.11 | 1 |
| margin | t3 | technology | 26 | 42.12 | 0.885 |
| margin | t3 | unknown | 92 | -7,878.02 | 0.75 |
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
| cash_debit | t1 | broad_market | 72 | -6,739.08 | 0.5 |
| cash_debit | t1 | communication | 34 | -4,637.07 | 0.441 |
| cash_debit | t1 | consumer_discretionary | 9 | 2,701.57 | 0.778 |
| cash_debit | t1 | consumer_staples | 7 | -101.09 | 0.429 |
| cash_debit | t1 | energy | 17 | 29.45 | 0.588 |
| cash_debit | t1 | financials | 70 | -10,367.52 | 0.271 |
| cash_debit | t1 | health_care | 10 | 1,646.56 | 0.8 |
| cash_debit | t1 | industrials | 6 | 1,223.27 | 0.333 |
| cash_debit | t1 | technology | 60 | -8,056.76 | 0.367 |
| cash_debit | t1 | unknown | 121 | -585.09 | 0.512 |
| cash_debit | t2 | broad_market | 45 | -360.57 | 0.489 |
| cash_debit | t2 | communication | 30 | -2,142.95 | 0.467 |
| cash_debit | t2 | consumer_discretionary | 2 | 134.18 | 1 |
| cash_debit | t2 | consumer_staples | 9 | -2,873.42 | 0.333 |
| cash_debit | t2 | energy | 14 | -414.39 | 0.643 |
| cash_debit | t2 | financials | 64 | -7,914.94 | 0.297 |
| cash_debit | t2 | health_care | 14 | 264.26 | 0.643 |
| cash_debit | t2 | industrials | 4 | 255.46 | 0.5 |
| cash_debit | t2 | technology | 64 | -8,094.42 | 0.406 |
| cash_debit | t2 | unknown | 115 | -3,735.04 | 0.53 |
| cash_debit | t3 | broad_market | 74 | 25,631.55 | 0.568 |
| cash_debit | t3 | communication | 11 | -2,366.40 | 0.364 |
| cash_debit | t3 | consumer_discretionary | 5 | 2,146.28 | 0.6 |
| cash_debit | t3 | consumer_staples | 2 | -29.35 | 0.5 |
| cash_debit | t3 | energy | 13 | -5,586.98 | 0.538 |
| cash_debit | t3 | financials | 54 | -8,039.68 | 0.296 |
| cash_debit | t3 | health_care | 8 | 626.02 | 0.5 |
| cash_debit | t3 | industrials | 1 | 39.36 | 1 |
| cash_debit | t3 | technology | 48 | -15,719.58 | 0.417 |
| cash_debit | t3 | unknown | 113 | -21,564.43 | 0.566 |

## ETF vs single stock

| profile | arm | key | trades | net_pnl | win_rate |
|---|---|---|---|---|---|
| margin | control | ETF | 323 | -13,494.80 | 0.728 |
| margin | control | single stock | 117 | -3,799.55 | 0.744 |
| margin | t1 | ETF | 349 | -10,280.92 | 0.748 |
| margin | t1 | single stock | 114 | -8,114.96 | 0.781 |
| margin | t2 | ETF | 348 | -11,140.89 | 0.756 |
| margin | t2 | single stock | 122 | -2,033.47 | 0.795 |
| margin | t3 | ETF | 331 | -9,377.03 | 0.755 |
| margin | t3 | single stock | 113 | -2,833.56 | 0.779 |
| cash_debit | control | ETF | 183 | 9,767.85 | 0.508 |
| cash_debit | control | single stock | 402 | -34,647.76 | 0.502 |
| cash_debit | t1 | ETF | 123 | -6,307.72 | 0.488 |
| cash_debit | t1 | single stock | 283 | -18,578.02 | 0.438 |
| cash_debit | t2 | ETF | 114 | 2,320.80 | 0.518 |
| cash_debit | t2 | single stock | 247 | -27,202.63 | 0.437 |
| cash_debit | t3 | ETF | 148 | 24,541.93 | 0.581 |
| cash_debit | t3 | single stock | 181 | -49,405.16 | 0.42 |

## By liquidity bucket

| profile | arm | key | trades | net_pnl | win_rate |
|---|---|---|---|---|---|
| margin | control | 100k-1M | 185 | -16,854.43 | 0.719 |
| margin | control | 10k-100k | 86 | -10,195.01 | 0.674 |
| margin | control | < 10k | 7 | 1,704.68 | 1 |
| margin | control | >= 1M | 162 | 8,050.41 | 0.765 |
| margin | t1 | 100k-1M | 195 | -23,623.25 | 0.738 |
| margin | t1 | 10k-100k | 86 | -155.13 | 0.779 |
| margin | t1 | < 10k | 8 | -363.63 | 0.75 |
| margin | t1 | >= 1M | 174 | 5,746.14 | 0.764 |
| margin | t2 | 100k-1M | 197 | -21,471.21 | 0.741 |
| margin | t2 | 10k-100k | 96 | 2,498.16 | 0.771 |
| margin | t2 | < 10k | 11 | -2,032.30 | 0.818 |
| margin | t2 | >= 1M | 166 | 7,830.99 | 0.789 |
| margin | t3 | 100k-1M | 182 | -19,156.86 | 0.747 |
| margin | t3 | 10k-100k | 84 | -1,298.60 | 0.75 |
| margin | t3 | < 10k | 10 | -2,343.91 | 0.7 |
| margin | t3 | >= 1M | 168 | 10,588.77 | 0.786 |
| cash_debit | control | 100k-1M | 319 | -24,218.29 | 0.489 |
| cash_debit | control | 10k-100k | 170 | 14,789.09 | 0.594 |
| cash_debit | control | < 10k | 24 | -8,334.74 | 0.458 |
| cash_debit | control | >= 1M | 72 | -7,115.97 | 0.375 |
| cash_debit | t1 | 100k-1M | 215 | -13,103.63 | 0.456 |
| cash_debit | t1 | 10k-100k | 129 | -4,300.77 | 0.488 |
| cash_debit | t1 | < 10k | 15 | 1,218.91 | 0.467 |
| cash_debit | t1 | >= 1M | 47 | -8,700.27 | 0.34 |
| cash_debit | t2 | 100k-1M | 213 | -18,509.43 | 0.446 |
| cash_debit | t2 | 10k-100k | 106 | -8,121.33 | 0.462 |
| cash_debit | t2 | < 10k | 8 | -1,244.84 | 0.5 |
| cash_debit | t2 | >= 1M | 34 | 2,993.78 | 0.559 |
| cash_debit | t3 | 100k-1M | 178 | -12,338.33 | 0.522 |
| cash_debit | t3 | 10k-100k | 87 | -515.10 | 0.471 |
| cash_debit | t3 | < 10k | 14 | -10,342.57 | 0.5 |
| cash_debit | t3 | >= 1M | 50 | -1,667.23 | 0.42 |

## Uncertainty

Moving-block bootstrap CI of the daily equity-change difference vs control (seeded), and the fixed sub-periods each treatment won on net P&L.

| profile | treatment | pnl_diff | ci_lo | ci_hi | periods_won |
|---|---|---|---|---|---|
| margin | t1 | -1,101.53 | -17,945.65 | 15,794.08 | 2020-21 bull, 2022 bear |
| margin | t2 | 4,119.99 | -9,951.27 | 17,285.09 | 2020-21 bull, 2022 bear, 2023 |
| margin | t3 | 5,083.75 | -8,630.32 | 18,152.39 | 2022 bear, 2023 |
| cash_debit | t1 | -5.84 | -46,262.80 | 42,880.63 | 2020-21 bull, 2022 bear, 2023 |
| cash_debit | t2 | -1.92 | -43,586.08 | 46,073.31 | 2020-21 bull, 2022 bear, 2023 |
| cash_debit | t3 | 16.68 | -56,522.78 | 56,569.11 | 2020 crash, 2022 bear, 2023 |

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
