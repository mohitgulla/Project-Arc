# BT-4 · E7.5 / E7.5b re-run: neither ev_proxy nor the unified-measure menus with managed_net_ev / rorc_day_tilted beat the profile's incumbent under the D25 rule.

## Header

- phase **holdout**; spec hash `308767167157`; arm set `26a00620f663`; owner owner; created 2026-10-15
- rule: Switch the default to a challenger only if it beats the incumbent on net P&L (higher) **and** max drawdown (not larger) in ≥ 2 of 3 sub-periods (bear, sideways, bull trend regime at entry), **and** the 90% block-bootstrap CI of its daily P&L difference vs the incumbent excludes 0 (lower bound > 0). Otherwise keep the incumbent and report the closest challenger.
- primary metric: `net_pnl`
- universe **c125** (ladder c125), version `u500-20261009-s1-e52ff301`; data partitions {'thetadata': 125}
- decisions 2024-01-01 -> 2026-10-15 (exits settle by 2026-10-15); dev 2020-01-01 -> 2023-12-31; holdout 2024-01-01 -> latest
- Not replayed: Discovery and Trending tiers (no historical LLM, Reddit or Stocktwits inputs), the LLM stance (a deterministic trend-label proxy stands in), intraday paths (EOD marks only; exits fire on closes).
- git `28afa22611cd`; variants tried 1 of max 3; earlier holdout runs 0
- arms: control = ranker incumbent; t1 = ranker ev_proxy; t2 = ranker managed_net_ev, overlay `{"ranking": {"backtest": {"direction_tilt": 0.25, "menus": {"cash_debit": [{"deltas": [0.2, 0.25, 0.3, 0.35], "expiry_mode": "all", "kinds": ["bull_call", "bear_put"], "width_pct": 0.02}, {"deltas": [0.4, 0.55, 0.7], "expiry_mode": "all", "kinds": ["long_call", "long_put"]}], "cash_long_only": [{"deltas": [0.4, 0.55, 0.7], "kinds": ["long_call", "long_put"]}], "margin": [{"deltas": [0.16, 0.2, 0.25, 0.3], "expiry_mode": "all", "kinds": ["bull_put", "bear_call", "iron_condor"], "width_pct": 0.02}]}}}}`; t3 = ranker rorc_day_tilted, overlay `{"ranking": {"backtest": {"direction_tilt": 0.25, "menus": {"cash_debit": [{"deltas": [0.2, 0.25, 0.3, 0.35], "expiry_mode": "all", "kinds": ["bull_call", "bear_put"], "width_pct": 0.02}, {"deltas": [0.4, 0.55, 0.7], "expiry_mode": "all", "kinds": ["long_call", "long_put"]}], "cash_long_only": [{"deltas": [0.4, 0.55, 0.7], "kinds": ["long_call", "long_put"]}], "margin": [{"deltas": [0.16, 0.2, 0.25, 0.3], "expiry_mode": "all", "kinds": ["bull_put", "bear_call", "iron_condor"], "width_pct": 0.02}]}}}}`

## Verdict

The pre-registered rule, verbatim:

> Switch the default to a challenger only if it beats the incumbent on net P&L (higher) **and** max drawdown (not larger) in ≥ 2 of 3 sub-periods (bear, sideways, bull trend regime at entry), **and** the 90% block-bootstrap CI of its daily P&L difference vs the incumbent excludes 0 (lower bound > 0). Otherwise keep the incumbent and report the closest challenger.

Original card's wording: _A challenger replaces the incumbent (debit_width for cash_debit, credit_width for margin) only if it beats it on net P&L (higher) **and** max drawdown (not larger) in ≥ 2 of 3 sub-periods (bear / sideways / bull trend at entry), **and** the 90 % block-bootstrap CI of the daily P&L difference excludes 0. The tilt verdict uses direction_tilt = 0.25, fixed in the card before the run._

- VERDICT · margin · t1 vs control: keep control (sub-periods 1 [sideways], P&L diff 2,522, CI [-18,132, 24,066])
- VERDICT · margin · t2 vs control: keep control (sub-periods 1 [bear], P&L diff -9,125, CI [-31,868, 11,268])
- VERDICT · margin · t3 vs control: keep control (sub-periods 2 [bear,sideways], P&L diff -6,829, CI [-32,086, 16,132])
- VERDICT · cash_debit · t1 vs control: keep control (sub-periods 1 [bear], P&L diff -3,384, CI [-38,768, 33,817])
- VERDICT · cash_debit · t2 vs control: keep control (sub-periods 1 [bear], P&L diff -4,060, CI [-54,606, 44,785])
- VERDICT · cash_debit · t3 vs control: keep control (sub-periods 1 [bear], P&L diff -4,157, CI [-68,600, 52,294])

A verdict only recommends: a live change still goes through D44 (`Experiment: XP-<n>` with arms copied from this spec) or an owner waiver.

## Per arm × profile

| profile | arm | trades | net_pnl | return_pct | cagr | max_dd_pct | sharpe | sortino | profit_factor | win_rate | avg_days_held | turnover_per_month | cost_share_of_gross | capacity_clipped_share |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| margin | control | 297 | -14,081.16 | -56.32 | -0.2573 | 76.87 | -0.341 | -0.309 | 0.804 | 0.684 | 25 | 8.89 | 1.778 | 0.34 |
| margin | t1 | 351 | -11,559.36 | -46.24 | -0.1998 | 64.18 | -0.331 | -0.276 | 0.828 | 0.789 | 21.3 | 10.5 | 4.038 | 0.407 |
| margin | t2 | 250 | -23,205.81 | -92.82 | -0.6117 | 94.81 | -1.311 | -1.069 | 0.65 | 0.684 | 24 | 7.48 | 0.449 | 0.196 |
| margin | t3 | 315 | -20,910.28 | -83.64 | -0.4781 | 89.91 | -0.859 | -0.681 | 0.725 | 0.711 | 21.6 | 9.43 | 0.883 | 0.229 |
| cash_debit | control | 546 | -20,736.85 | -82.95 | -0.4702 | 95.93 | 0.398 | 0.626 | 0.89 | 0.502 | 14 | 16.34 | 4.465 | 0.368 |
| cash_debit | t1 | 431 | -24,120.90 | -96.48 | -0.6995 | 97.84 | -0.004 | -0.005 | 0.827 | 0.483 | 14.7 | 12.9 | 1.146 | 0.237 |
| cash_debit | t2 | 325 | -24,796.86 | -99.19 | -0.8224 | 99.49 | -0.618 | -0.563 | 0.778 | 0.458 | 13.4 | 9.73 | 0.355 | 0.068 |
| cash_debit | t3 | 429 | -24,894.12 | -99.58 | -0.8595 | 99.74 | -0.78 | -0.609 | 0.794 | 0.403 | 6.6 | 12.84 | 0.738 | 0.17 |

## All / Stocks / ETFs

One pool per run; the verdict is stated on All, Stocks and ETFs are diagnostic. `dd_contribution` = net P&L of the trades closed inside the run's max-drawdown window; `spread_cost` = P&L at mid − net P&L (spread + fees).

| profile | arm | segment | trades | win_rate | net_pnl | mean_pnl | median_pnl | pnl_per_risk | dd_contribution | spread_cost | exit_reasons |
|---|---|---|---|---|---|---|---|---|---|---|---|
| margin | control | All | 297 | 0.684 | -14,081.16 | -47.41 | 90.68 | -0.0375 | -24,265.26 | 9,012.65 | dte_exit 33, expiry 84, stop 26, take_profit 154 |
| margin | control | Stocks | 89 | 0.629 | -414.97 | -4.66 | 104.52 | -0.0034 | -4,216.08 | 4,849.47 | dte_exit 12, expiry 31, stop 6, take_profit 40 |
| margin | control | ETFs | 208 | 0.707 | -13,666.19 | -65.7 | 88.01 | -0.0541 | -20,049.17 | 4,163.19 | dte_exit 21, expiry 53, stop 20, take_profit 114 |
| margin | t1 | All | 351 | 0.789 | -11,559.36 | -32.93 | 80.36 | -0.0249 | -20,147.82 | 9,264.86 | dte_exit 26, expiry 79, stop 23, take_profit 223 |
| margin | t1 | Stocks | 98 | 0.745 | -4,895.74 | -49.96 | 105.60 | -0.0329 | -7,402.62 | 4,417.74 | dte_exit 8, expiry 32, stop 3, take_profit 55 |
| margin | t1 | ETFs | 253 | 0.806 | -6,663.62 | -26.34 | 77.18 | -0.0211 | -12,745.20 | 4,847.12 | dte_exit 18, expiry 47, stop 20, take_profit 168 |
| margin | t2 | All | 250 | 0.684 | -23,205.81 | -92.82 | 58.38 | -0.0756 | -27,421.45 | 7,189.81 | dte_exit 27, expiry 77, stop 19, take_profit 127 |
| margin | t2 | Stocks | 114 | 0.667 | -9,814.57 | -86.09 | 55.32 | -0.0683 | -13,421.17 | 5,118.57 | dte_exit 11, expiry 41, stop 10, take_profit 52 |
| margin | t2 | ETFs | 136 | 0.699 | -13,391.24 | -98.47 | 60.09 | -0.0821 | -14,000.27 | 2,071.24 | dte_exit 16, expiry 36, stop 9, take_profit 75 |
| margin | t3 | All | 315 | 0.711 | -20,910.28 | -66.38 | 69.19 | -0.0526 | -30,535.47 | 9,807.28 | dte_exit 27, expiry 78, stop 29, take_profit 181 |
| margin | t3 | Stocks | 121 | 0.678 | -10,735.25 | -88.72 | 47.68 | -0.0643 | -17,524.98 | 6,451.25 | dte_exit 14, expiry 37, stop 11, take_profit 59 |
| margin | t3 | ETFs | 194 | 0.732 | -10,175.03 | -52.45 | 77.37 | -0.0442 | -13,010.49 | 3,356.03 | dte_exit 13, expiry 41, stop 18, take_profit 122 |
| cash_debit | control | All | 546 | 0.502 | -20,736.85 | -37.98 | 1.96 | -0.0424 | -42,421.91 | 26,720.84 | dte_exit 27, expiry 15, profit_lock 36, stop 203, take_profit 265 |
| cash_debit | control | Stocks | 371 | 0.52 | -8,231.56 | -22.19 | 27.84 | -0.0242 | -25,471.50 | 20,732.56 | dte_exit 20, expiry 13, profit_lock 18, stop 127, take_profit 193 |
| cash_debit | control | ETFs | 175 | 0.463 | -12,505.29 | -71.46 | -36.66 | -0.0841 | -16,950.41 | 5,988.29 | dte_exit 7, expiry 2, profit_lock 18, stop 76, take_profit 72 |
| cash_debit | t1 | All | 431 | 0.483 | -24,120.90 | -55.96 | -7.23 | -0.0697 | -33,894.38 | 12,882.90 | dte_exit 23, expiry 10, profit_lock 37, stop 171, take_profit 190 |
| cash_debit | t1 | Stocks | 294 | 0.49 | -13,663.08 | -46.47 | -2 | -0.0588 | -12,810.87 | 9,284.08 | dte_exit 16, expiry 9, profit_lock 24, stop 112, take_profit 133 |
| cash_debit | t1 | ETFs | 137 | 0.467 | -10,457.83 | -76.33 | -40.41 | -0.0919 | -21,083.51 | 3,598.83 | dte_exit 7, expiry 1, profit_lock 13, stop 59, take_profit 57 |
| cash_debit | t2 | All | 325 | 0.458 | -24,796.86 | -76.3 | -21.08 | -0.0892 | -29,678.14 | 6,495.86 | dte_exit 59, expiry 13, profit_lock 18, stop 115, take_profit 120 |
| cash_debit | t2 | Stocks | 231 | 0.45 | -13,109.42 | -56.75 | -22.08 | -0.0657 | -19,616.30 | 5,147.92 | dte_exit 39, expiry 11, profit_lock 14, stop 83, take_profit 84 |
| cash_debit | t2 | ETFs | 94 | 0.479 | -11,687.44 | -124.33 | -17.33 | -0.1489 | -10,061.83 | 1,347.94 | dte_exit 20, expiry 2, profit_lock 4, stop 32, take_profit 36 |
| cash_debit | t3 | All | 429 | 0.403 | -24,894.12 | -58.03 | -27.41 | -0.0824 | -37,256.36 | 10,567.12 | dte_exit 136, expiry 16, profit_lock 12, stop 125, take_profit 140 |
| cash_debit | t3 | Stocks | 318 | 0.39 | -14,947.31 | -47 | -28.85 | -0.0683 | -30,287.41 | 8,696.31 | dte_exit 90, expiry 13, profit_lock 9, stop 100, take_profit 106 |
| cash_debit | t3 | ETFs | 111 | 0.441 | -9,946.82 | -89.61 | -22.91 | -0.1194 | -6,968.95 | 1,870.82 | dte_exit 46, expiry 3, profit_lock 3, stop 25, take_profit 34 |

Treatment − control net P&L by segment (**sign differs between Stocks and ETFs for 6 treatment(s)**):

| profile | treatment | All | Stocks | ETFs | sign_differs |
|---|---|---|---|---|---|
| margin | t1 | 2,521.80 | -4,480.77 | 7,002.57 | True |
| margin | t2 | -9,124.65 | -9,399.60 | 274.95 | True |
| margin | t3 | -6,829.12 | -10,320.28 | 3,491.16 | True |
| cash_debit | t1 | -3,384.05 | -5,431.52 | 2,047.46 | True |
| cash_debit | t2 | -4,060.01 | -4,877.86 | 817.85 | True |
| cash_debit | t3 | -4,157.27 | -6,715.75 | 2,558.47 | True |

## Exit-reason mix

| profile | arm | mix |
|---|---|---|
| margin | control | dte_exit 0.1111, expiry 0.2828, stop 0.0875, take_profit 0.5185 |
| margin | t1 | dte_exit 0.0741, expiry 0.2251, stop 0.0655, take_profit 0.6353 |
| margin | t2 | dte_exit 0.108, expiry 0.308, stop 0.076, take_profit 0.508 |
| margin | t3 | dte_exit 0.0857, expiry 0.2476, stop 0.0921, take_profit 0.5746 |
| cash_debit | control | dte_exit 0.0495, expiry 0.0275, profit_lock 0.0659, stop 0.3718, take_profit 0.4853 |
| cash_debit | t1 | dte_exit 0.0534, expiry 0.0232, profit_lock 0.0858, stop 0.3968, take_profit 0.4408 |
| cash_debit | t2 | dte_exit 0.1815, expiry 0.04, profit_lock 0.0554, stop 0.3538, take_profit 0.3692 |
| cash_debit | t3 | dte_exit 0.317, expiry 0.0373, profit_lock 0.028, stop 0.2914, take_profit 0.3263 |

## Skip reasons

| profile | arm | screen | blackout | no_candidate | unsettled | sizing | gate_per_underlying | gate_max_open_positions | gate_daily_loss | gate_max_gain | cash | capacity | no_fill | exit_delayed |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| margin | control | 55100 | 665 | 20311 | 252 | 929 | 0 | 3869 | 2159 | 0 | 0 | 540 | 181 | 11 |
| margin | t1 | 55100 | 413 | 20644 | 259 | 743 | 0 | 4576 | 1717 | 0 | 0 | 486 | 149 | 13 |
| margin | t2 | 55100 | 1118 | 18722 | 336 | 3762 | 0 | 3258 | 2861 | 0 | 0 | 397 | 216 | 13 |
| margin | t3 | 55100 | 1260 | 18740 | 337 | 1806 | 0 | 4790 | 2917 | 0 | 0 | 461 | 260 | 22 |
| cash_debit | control | 55100 | 0 | 14410 | 149 | 1384 | 0 | 2381 | 8681 | 0 | 12 | 337 | 192 | 78 |
| cash_debit | t1 | 55100 | 0 | 14349 | 153 | 2091 | 0 | 1655 | 8802 | 0 | 31 | 233 | 151 | 68 |
| cash_debit | t2 | 55100 | 0 | 15228 | 262 | 6911 | 0 | 2038 | 6475 | 0 | 51 | 76 | 30 | 46 |
| cash_debit | t3 | 55100 | 0 | 15761 | 143 | 7637 | 0 | 1230 | 5931 | 0 | 78 | 131 | 90 | 42 |

## Sub-periods

Fixed windows by entry date.

| profile | arm | period | trades | net_pnl | win_rate | max_dd_pct |
|---|---|---|---|---|---|---|
| margin | control | 2020 crash | 0 | 0 | – | 0 |
| margin | control | 2020-21 bull | 0 | 0 | – | 0 |
| margin | control | 2022 bear | 0 | 0 | – | 0 |
| margin | control | 2023 | 0 | 0 | – | 0 |
| margin | control | 2024 -> latest | 297 | -14,081.16 | 0.684 | 76.87 |
| margin | t1 | 2020 crash | 0 | 0 | – | 0 |
| margin | t1 | 2020-21 bull | 0 | 0 | – | 0 |
| margin | t1 | 2022 bear | 0 | 0 | – | 0 |
| margin | t1 | 2023 | 0 | 0 | – | 0 |
| margin | t1 | 2024 -> latest | 351 | -11,559.36 | 0.789 | 64.18 |
| margin | t2 | 2020 crash | 0 | 0 | – | 0 |
| margin | t2 | 2020-21 bull | 0 | 0 | – | 0 |
| margin | t2 | 2022 bear | 0 | 0 | – | 0 |
| margin | t2 | 2023 | 0 | 0 | – | 0 |
| margin | t2 | 2024 -> latest | 250 | -23,205.81 | 0.684 | 94.81 |
| margin | t3 | 2020 crash | 0 | 0 | – | 0 |
| margin | t3 | 2020-21 bull | 0 | 0 | – | 0 |
| margin | t3 | 2022 bear | 0 | 0 | – | 0 |
| margin | t3 | 2023 | 0 | 0 | – | 0 |
| margin | t3 | 2024 -> latest | 315 | -20,910.28 | 0.711 | 89.91 |
| cash_debit | control | 2020 crash | 0 | 0 | – | 0 |
| cash_debit | control | 2020-21 bull | 0 | 0 | – | 0 |
| cash_debit | control | 2022 bear | 0 | 0 | – | 0 |
| cash_debit | control | 2023 | 0 | 0 | – | 0 |
| cash_debit | control | 2024 -> latest | 546 | -20,736.85 | 0.502 | 95.93 |
| cash_debit | t1 | 2020 crash | 0 | 0 | – | 0 |
| cash_debit | t1 | 2020-21 bull | 0 | 0 | – | 0 |
| cash_debit | t1 | 2022 bear | 0 | 0 | – | 0 |
| cash_debit | t1 | 2023 | 0 | 0 | – | 0 |
| cash_debit | t1 | 2024 -> latest | 431 | -24,120.90 | 0.483 | 97.84 |
| cash_debit | t2 | 2020 crash | 0 | 0 | – | 0 |
| cash_debit | t2 | 2020-21 bull | 0 | 0 | – | 0 |
| cash_debit | t2 | 2022 bear | 0 | 0 | – | 0 |
| cash_debit | t2 | 2023 | 0 | 0 | – | 0 |
| cash_debit | t2 | 2024 -> latest | 325 | -24,796.86 | 0.458 | 99.49 |
| cash_debit | t3 | 2020 crash | 0 | 0 | – | 0 |
| cash_debit | t3 | 2020-21 bull | 0 | 0 | – | 0 |
| cash_debit | t3 | 2022 bear | 0 | 0 | – | 0 |
| cash_debit | t3 | 2023 | 0 | 0 | – | 0 |
| cash_debit | t3 | 2024 -> latest | 429 | -24,894.12 | 0.403 | 99.74 |

## By regime

| profile | arm | key | trades | net_pnl | win_rate |
|---|---|---|---|---|---|
| margin | control | bear | 24 | -8,870.45 | 0.5 |
| margin | control | bull | 90 | -2,296.55 | 0.867 |
| margin | control | sideways | 183 | -2,914.16 | 0.617 |
| margin | t1 | bear | 40 | -6,489.67 | 0.65 |
| margin | t1 | bull | 140 | -4,935.01 | 0.893 |
| margin | t1 | sideways | 171 | -134.67 | 0.737 |
| margin | t2 | bear | 16 | -4,626.79 | 0.562 |
| margin | t2 | bull | 92 | -7,203.21 | 0.826 |
| margin | t2 | sideways | 142 | -11,375.81 | 0.606 |
| margin | t3 | bear | 33 | -6,466.38 | 0.576 |
| margin | t3 | bull | 134 | -15,902.63 | 0.821 |
| margin | t3 | sideways | 148 | 1,458.74 | 0.642 |
| cash_debit | control | bear | 152 | -47,680.55 | 0.368 |
| cash_debit | control | bull | 394 | 26,943.71 | 0.553 |
| cash_debit | t1 | bear | 124 | -28,027.56 | 0.371 |
| cash_debit | t1 | bull | 307 | 3,906.65 | 0.528 |
| cash_debit | t2 | bear | 116 | -30,107.48 | 0.353 |
| cash_debit | t2 | bull | 209 | 5,310.62 | 0.517 |
| cash_debit | t3 | bear | 142 | -27,930.30 | 0.366 |
| cash_debit | t3 | bull | 287 | 3,036.17 | 0.422 |

## By structure

| profile | arm | key | trades | net_pnl | win_rate |
|---|---|---|---|---|---|
| margin | control | bear_call | 24 | -8,870.45 | 0.5 |
| margin | control | bull_put | 90 | -2,296.55 | 0.867 |
| margin | control | iron_condor | 183 | -2,914.16 | 0.617 |
| margin | t1 | bear_call | 40 | -6,489.67 | 0.65 |
| margin | t1 | bull_put | 140 | -4,935.01 | 0.893 |
| margin | t1 | iron_condor | 171 | -134.67 | 0.737 |
| margin | t2 | bear_call | 16 | -4,626.79 | 0.562 |
| margin | t2 | bull_put | 92 | -7,203.21 | 0.826 |
| margin | t2 | iron_condor | 142 | -11,375.81 | 0.606 |
| margin | t3 | bear_call | 33 | -6,466.38 | 0.576 |
| margin | t3 | bull_put | 134 | -15,902.63 | 0.821 |
| margin | t3 | iron_condor | 148 | 1,458.74 | 0.642 |
| cash_debit | control | bear_put | 117 | -40,464.67 | 0.342 |
| cash_debit | control | bull_call | 209 | 2,744.21 | 0.536 |
| cash_debit | control | long_call | 185 | 24,199.50 | 0.573 |
| cash_debit | control | long_put | 35 | -7,215.88 | 0.457 |
| cash_debit | t1 | bear_put | 87 | -21,483.56 | 0.356 |
| cash_debit | t1 | bull_call | 69 | 3,556.85 | 0.58 |
| cash_debit | t1 | long_call | 238 | 349.80 | 0.513 |
| cash_debit | t1 | long_put | 37 | -6,544.00 | 0.405 |
| cash_debit | t2 | bear_put | 58 | -17,410.23 | 0.362 |
| cash_debit | t2 | bull_call | 17 | 794.90 | 0.353 |
| cash_debit | t2 | long_call | 192 | 4,515.72 | 0.531 |
| cash_debit | t2 | long_put | 58 | -12,697.25 | 0.345 |
| cash_debit | t3 | bear_put | 86 | -14,325.95 | 0.36 |
| cash_debit | t3 | bull_call | 103 | -659.12 | 0.408 |
| cash_debit | t3 | long_call | 184 | 3,695.30 | 0.429 |
| cash_debit | t3 | long_put | 56 | -13,604.35 | 0.375 |

## By universe tier

| profile | arm | key | trades | net_pnl | win_rate |
|---|---|---|---|---|---|
| margin | control | core | 205 | -22,483.67 | 0.649 |
| margin | control | momentum | 14 | 6,872.01 | 0.786 |
| margin | control | rest | 78 | 1,530.50 | 0.756 |
| margin | t1 | core | 236 | -15,432.40 | 0.763 |
| margin | t1 | momentum | 11 | -2,669.02 | 0.818 |
| margin | t1 | rest | 104 | 6,542.05 | 0.846 |
| margin | t2 | core | 143 | -22,201.99 | 0.636 |
| margin | t2 | momentum | 21 | 253.26 | 0.762 |
| margin | t2 | rest | 86 | -1,257.07 | 0.744 |
| margin | t3 | core | 190 | -17,716.63 | 0.7 |
| margin | t3 | momentum | 18 | -4,898.07 | 0.667 |
| margin | t3 | rest | 107 | 1,704.42 | 0.738 |
| cash_debit | control | core | 283 | -28,121.15 | 0.452 |
| cash_debit | control | momentum | 33 | -6,637.62 | 0.576 |
| cash_debit | control | rest | 230 | 14,021.92 | 0.552 |
| cash_debit | t1 | core | 221 | -12,704.08 | 0.48 |
| cash_debit | t1 | momentum | 29 | -4,565.45 | 0.483 |
| cash_debit | t1 | rest | 181 | -6,851.37 | 0.486 |
| cash_debit | t2 | core | 159 | -12,842.76 | 0.459 |
| cash_debit | t2 | momentum | 20 | -262.46 | 0.6 |
| cash_debit | t2 | rest | 146 | -11,691.65 | 0.438 |
| cash_debit | t3 | core | 185 | -11,602.66 | 0.4 |
| cash_debit | t3 | momentum | 24 | -2,416.55 | 0.333 |
| cash_debit | t3 | rest | 220 | -10,874.91 | 0.414 |

## By sector

| profile | arm | key | trades | net_pnl | win_rate |
|---|---|---|---|---|---|
| margin | control | broad_market | 143 | -17,108.97 | 0.657 |
| margin | control | communication | 2 | 594.35 | 1 |
| margin | control | consumer_discretionary | 14 | -4,246.63 | 0.429 |
| margin | control | energy | 4 | 1,390.47 | 0.75 |
| margin | control | financials | 14 | -1,810.74 | 0.643 |
| margin | control | health_care | 2 | 3,810.58 | 1 |
| margin | control | technology | 40 | -866.39 | 0.65 |
| margin | control | unknown | 78 | 4,156.18 | 0.782 |
| margin | t1 | broad_market | 159 | -15,171.04 | 0.767 |
| margin | t1 | communication | 2 | 212.17 | 1 |
| margin | t1 | consumer_discretionary | 13 | 928.80 | 0.692 |
| margin | t1 | consumer_staples | 1 | 426.78 | 1 |
| margin | t1 | energy | 4 | 1,469.53 | 1 |
| margin | t1 | financials | 23 | -7,895.90 | 0.696 |
| margin | t1 | health_care | 3 | 2,249.82 | 1 |
| margin | t1 | technology | 43 | -654.07 | 0.767 |
| margin | t1 | unknown | 103 | 6,874.56 | 0.845 |
| margin | t2 | broad_market | 79 | -17,100.64 | 0.62 |
| margin | t2 | communication | 4 | -78.79 | 0.5 |
| margin | t2 | consumer_discretionary | 12 | -657.83 | 0.583 |
| margin | t2 | consumer_staples | 3 | 332.57 | 1 |
| margin | t2 | energy | 3 | -1,704.01 | 0.333 |
| margin | t2 | financials | 20 | -6,345.12 | 0.7 |
| margin | t2 | health_care | 5 | 3,220.36 | 0.8 |
| margin | t2 | technology | 41 | -4,407.19 | 0.659 |
| margin | t2 | unknown | 83 | 3,534.83 | 0.771 |
| margin | t3 | broad_market | 122 | -14,940.46 | 0.689 |
| margin | t3 | communication | 3 | -518.40 | 0.333 |
| margin | t3 | consumer_discretionary | 15 | -3,715.20 | 0.533 |
| margin | t3 | consumer_staples | 2 | 284.07 | 1 |
| margin | t3 | energy | 4 | -2,436.86 | 0.5 |
| margin | t3 | financials | 23 | -1,460.16 | 0.783 |
| margin | t3 | health_care | 4 | 4,165.89 | 1 |
| margin | t3 | technology | 42 | -2,040.03 | 0.714 |
| margin | t3 | unknown | 100 | -249.12 | 0.75 |
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
| cash_debit | t1 | broad_market | 84 | -8,193.92 | 0.44 |
| cash_debit | t1 | communication | 13 | 4,549.76 | 0.769 |
| cash_debit | t1 | consumer_discretionary | 39 | -8,285.12 | 0.385 |
| cash_debit | t1 | consumer_staples | 3 | -2,806.31 | 0.333 |
| cash_debit | t1 | energy | 12 | -1,939.44 | 0.417 |
| cash_debit | t1 | financials | 52 | 6,349.19 | 0.558 |
| cash_debit | t1 | health_care | 10 | -3,100.46 | 0.3 |
| cash_debit | t1 | industrials | 12 | 3,338.79 | 0.5 |
| cash_debit | t1 | technology | 98 | -7,274.90 | 0.49 |
| cash_debit | t1 | unknown | 108 | -6,758.50 | 0.5 |
| cash_debit | t2 | broad_market | 58 | -12,626.72 | 0.5 |
| cash_debit | t2 | communication | 14 | 1,933.43 | 0.5 |
| cash_debit | t2 | consumer_discretionary | 36 | 5,558.06 | 0.556 |
| cash_debit | t2 | consumer_staples | 2 | -585.46 | 0.5 |
| cash_debit | t2 | energy | 5 | -556.57 | 0.2 |
| cash_debit | t2 | financials | 49 | -10,731.60 | 0.367 |
| cash_debit | t2 | health_care | 9 | 661.06 | 0.444 |
| cash_debit | t2 | industrials | 8 | -848.93 | 0.625 |
| cash_debit | t2 | technology | 61 | -1,763.07 | 0.443 |
| cash_debit | t2 | unknown | 83 | -5,837.05 | 0.446 |
| cash_debit | t3 | broad_market | 58 | -7,087.66 | 0.448 |
| cash_debit | t3 | communication | 17 | -1,696.91 | 0.588 |
| cash_debit | t3 | consumer_discretionary | 47 | 246.47 | 0.426 |
| cash_debit | t3 | consumer_staples | 2 | 1,078.69 | 1 |
| cash_debit | t3 | energy | 9 | 810.83 | 0.333 |
| cash_debit | t3 | financials | 61 | -11,380.35 | 0.311 |
| cash_debit | t3 | health_care | 8 | -1,104.65 | 0.5 |
| cash_debit | t3 | industrials | 8 | -4,933.50 | 0.375 |
| cash_debit | t3 | technology | 92 | 3,661.59 | 0.37 |
| cash_debit | t3 | unknown | 127 | -4,488.64 | 0.409 |

## ETF vs single stock

| profile | arm | key | trades | net_pnl | win_rate |
|---|---|---|---|---|---|
| margin | control | ETF | 208 | -13,666.19 | 0.707 |
| margin | control | single stock | 89 | -414.97 | 0.629 |
| margin | t1 | ETF | 253 | -6,663.62 | 0.806 |
| margin | t1 | single stock | 98 | -4,895.74 | 0.745 |
| margin | t2 | ETF | 136 | -13,391.24 | 0.699 |
| margin | t2 | single stock | 114 | -9,814.57 | 0.667 |
| margin | t3 | ETF | 194 | -10,175.03 | 0.732 |
| margin | t3 | single stock | 121 | -10,735.25 | 0.678 |
| cash_debit | control | ETF | 175 | -12,505.29 | 0.463 |
| cash_debit | control | single stock | 371 | -8,231.56 | 0.52 |
| cash_debit | t1 | ETF | 137 | -10,457.83 | 0.467 |
| cash_debit | t1 | single stock | 294 | -13,663.08 | 0.49 |
| cash_debit | t2 | ETF | 94 | -11,687.44 | 0.479 |
| cash_debit | t2 | single stock | 231 | -13,109.42 | 0.45 |
| cash_debit | t3 | ETF | 111 | -9,946.82 | 0.441 |
| cash_debit | t3 | single stock | 318 | -14,947.31 | 0.39 |

## By liquidity bucket

| profile | arm | key | trades | net_pnl | win_rate |
|---|---|---|---|---|---|
| margin | control | 100k-1M | 118 | -8,668.85 | 0.669 |
| margin | control | 10k-100k | 35 | 10,586.26 | 0.743 |
| margin | control | < 10k | 1 | 192.68 | 1 |
| margin | control | >= 1M | 143 | -16,191.23 | 0.678 |
| margin | t1 | 100k-1M | 147 | 2,857.70 | 0.796 |
| margin | t1 | 10k-100k | 48 | -2,163.56 | 0.792 |
| margin | t1 | < 10k | 1 | 59.03 | 1 |
| margin | t1 | >= 1M | 155 | -12,312.54 | 0.781 |
| margin | t2 | 100k-1M | 125 | -10,992.82 | 0.672 |
| margin | t2 | 10k-100k | 46 | 391.49 | 0.739 |
| margin | t2 | < 10k | 3 | -127.02 | 0.667 |
| margin | t2 | >= 1M | 76 | -12,477.45 | 0.671 |
| margin | t3 | 100k-1M | 137 | -12,311.31 | 0.686 |
| margin | t3 | 10k-100k | 53 | 6,072.40 | 0.811 |
| margin | t3 | < 10k | 1 | 178.18 | 1 |
| margin | t3 | >= 1M | 124 | -14,849.55 | 0.694 |
| cash_debit | control | 100k-1M | 291 | 3,556.57 | 0.512 |
| cash_debit | control | 10k-100k | 112 | -986.90 | 0.545 |
| cash_debit | control | < 10k | 5 | -4,369.75 | 0.4 |
| cash_debit | control | >= 1M | 138 | -18,936.76 | 0.449 |
| cash_debit | t1 | 100k-1M | 210 | -9,030.89 | 0.5 |
| cash_debit | t1 | 10k-100k | 127 | -5,966.91 | 0.48 |
| cash_debit | t1 | < 10k | 2 | 432.30 | 0.5 |
| cash_debit | t1 | >= 1M | 92 | -9,555.40 | 0.446 |
| cash_debit | t2 | 100k-1M | 137 | -10,034.80 | 0.438 |
| cash_debit | t2 | 10k-100k | 111 | -11,654.72 | 0.414 |
| cash_debit | t2 | < 10k | 8 | -257.93 | 0.5 |
| cash_debit | t2 | >= 1M | 69 | -2,849.41 | 0.565 |
| cash_debit | t3 | 100k-1M | 208 | -9,982.91 | 0.385 |
| cash_debit | t3 | 10k-100k | 130 | -5,621.62 | 0.377 |
| cash_debit | t3 | < 10k | 7 | 2,556.48 | 0.714 |
| cash_debit | t3 | >= 1M | 84 | -11,846.08 | 0.464 |

## Uncertainty

Moving-block bootstrap CI of the daily equity-change difference vs control (seeded), and the fixed sub-periods each treatment won on net P&L.

| profile | treatment | pnl_diff | ci_lo | ci_hi | periods_won |
|---|---|---|---|---|---|
| margin | t1 | 2,521.79 | -18,131.80 | 24,066.13 | 2024 -> latest |
| margin | t2 | -9,124.65 | -31,868.17 | 11,268.13 | – |
| margin | t3 | -6,829.12 | -32,085.82 | 16,132.26 | – |
| cash_debit | t1 | -3,384.06 | -38,768.17 | 33,816.84 | – |
| cash_debit | t2 | -4,060.01 | -54,606.35 | 44,784.95 | – |
| cash_debit | t3 | -4,157.28 | -68,599.60 | 52,293.89 | – |

## Checkpoint ladder

Every ledger run of this arm set; `sign_flip_vs_first_rung` compares the primary metric's treatment − control sign with the first dev rung.

| at | phase | universe | profile | treatment | primary_diff | ci_lo | ci_hi | switch | sign_flip_vs_first_rung |
|---|---|---|---|---|---|---|---|---|---|
| 2026-10-16T01:09:48-04:00 | dev | c125 | cash_debit | t1 | 0.36 | -41,773.10 | 41,134.58 | False | False |
| 2026-10-16T01:09:48-04:00 | dev | c125 | cash_debit | t2 | -20.38 | -64,840.64 | 63,269.31 | False | False |
| 2026-10-16T01:09:48-04:00 | dev | c125 | cash_debit | t3 | -66.43 | -48,952.34 | 69,196.30 | False | False |
| 2026-10-16T01:09:48-04:00 | dev | c125 | margin | t1 | 1,860.86 | -19,978.64 | 23,457.22 | False | False |
| 2026-10-16T01:09:48-04:00 | dev | c125 | margin | t2 | -3,032.17 | -23,725.88 | 16,088.18 | False | False |
| 2026-10-16T01:09:48-04:00 | dev | c125 | margin | t3 | 5,746.96 | -30,368.00 | 41,256.83 | False | False |
| 2026-10-16T07:20:16-04:00 | holdout | c125 | cash_debit | t1 | -3,384.05 | -38,768.17 | 33,816.84 | False | True |
| 2026-10-16T07:20:16-04:00 | holdout | c125 | cash_debit | t2 | -4,060.01 | -54,606.35 | 44,784.95 | False | False |
| 2026-10-16T07:20:16-04:00 | holdout | c125 | cash_debit | t3 | -4,157.27 | -68,599.60 | 52,293.89 | False | False |
| 2026-10-16T07:20:16-04:00 | holdout | c125 | margin | t1 | 2,521.80 | -18,131.80 | 24,066.13 | False | False |
| 2026-10-16T07:20:16-04:00 | holdout | c125 | margin | t2 | -9,124.65 | -31,868.17 | 11,268.13 | False | False |
| 2026-10-16T07:20:16-04:00 | holdout | c125 | margin | t3 | -6,829.12 | -32,085.82 | 16,132.26 | False | True |
