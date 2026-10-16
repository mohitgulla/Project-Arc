# BT-7 · E17.3 re-run: regime v2's stance labels are not worse than v1's on net P&L and max drawdown in either profile.

## Header

- phase **dev**; spec hash `0224b099ec9c`; arm set `2084785ee1d4`; owner owner; created 2026-10-15
- rule: Keep the control if, in every profile, it is not worse than the treatment on net P&L **and** max drawdown, **and** the upper bound of the 90% block-bootstrap CI of the daily P&L difference (control − treatment) is > 0. Roll back to the treatment if the control is worse on both net P&L and drawdown in any profile. Otherwise keep the control, inconclusive.
- primary metric: `net_pnl`
- universe **c125** (ladder c125, c500), version `u500-20261009-s1-e52ff301`; data partitions {'thetadata': 125}
- decisions 2020-01-01 -> 2023-12-31 (exits settle by 2024-03-30); dev 2020-01-01 -> 2023-12-31; holdout 2024-01-01 -> latest
- Not replayed: Discovery and Trending tiers (no historical LLM, Reddit or Stocktwits inputs), the LLM stance (a deterministic trend-label proxy stands in), intraday paths (EOD marks only; exits fire on closes).
- git `05d2fddaffa6`; variants tried 1 of max 3; earlier holdout runs 0
- arms: control = ranker incumbent; t1 = ranker incumbent, overlay `{"ranking": {"backtest": {"sim": {"regime_model": "v1"}}}}`

## Verdict

The pre-registered rule, verbatim:

> Keep the control if, in every profile, it is not worse than the treatment on net P&L **and** max drawdown, **and** the upper bound of the 90% block-bootstrap CI of the daily P&L difference (control − treatment) is > 0. Roll back to the treatment if the control is worse on both net P&L and drawdown in any profile. Otherwise keep the control, inconclusive.

Original card's wording: _Use the incumbent ranker (margin credit_width, cash_debit debit_width). Keep v2 if, in both profiles, v2 is not worse than v1 on net P&L **and** max drawdown, **and** the upper bound of the 90 % block-bootstrap CI of the daily P&L difference (v2 − v1) is > 0. Roll back to v1 (regime.model: v1) if v2 is worse on both net P&L and drawdown in either profile. Otherwise keep v2, inconclusive._

- dev result (not a verdict) · margin · t1 vs control: keep, inconclusive (sub-periods 0 [–], P&L diff -239, CI [-13,510, 13,633])
- dev result (not a verdict) · cash_debit · t1 vs control: keep control (sub-periods 0 [–], P&L diff -6, CI [-101,250, 97,742])
- dev result (not a verdict) · all profiles · t1 vs control: keep, inconclusive

A verdict only recommends: a live change still goes through D44 (`Experiment: XP-<n>` with arms copied from this spec) or an owner waiver.

## Per arm × profile

| profile | arm | trades | net_pnl | return_pct | cagr | max_dd_pct | sharpe | sortino | profit_factor | win_rate | avg_days_held | turnover_per_month | cost_share_of_gross | capacity_clipped_share |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| margin | control | 440 | -17,294.35 | -69.18 | -0.2426 | 83 | -0.354 | -0.291 | 0.789 | 0.732 | 21 | 8.66 | 1.845 | 0.373 |
| margin | t1 | 450 | -17,533.40 | -70.13 | -0.2482 | 83.83 | -0.364 | -0.319 | 0.784 | 0.718 | 22.2 | 8.85 | 1.597 | 0.318 |
| cash_debit | control | 585 | -24,879.91 | -99.52 | -0.7165 | 99.74 | -0.28 | -0.284 | 0.87 | 0.504 | 14.7 | 11.51 | 6.399 | 0.354 |
| cash_debit | t1 | 743 | -24,886.16 | -99.54 | -0.72 | 99.91 | -0.38 | -0.362 | 0.929 | 0.522 | 14.2 | 14.62 | 1.732 | 0.497 |

## All / Stocks / ETFs

One pool per run; the verdict is stated on All, Stocks and ETFs are diagnostic. `dd_contribution` = net P&L of the trades closed inside the run's max-drawdown window; `spread_cost` = P&L at mid − net P&L (spread + fees).

| profile | arm | segment | trades | win_rate | net_pnl | mean_pnl | median_pnl | pnl_per_risk | dd_contribution | spread_cost | exit_reasons |
|---|---|---|---|---|---|---|---|---|---|---|---|
| margin | control | All | 440 | 0.732 | -17,294.35 | -39.31 | 57.38 | -0.039 | -23,257.07 | 11,215.85 | dte_exit 44, expiry 71, stop 36, take_profit 289 |
| margin | control | Stocks | 117 | 0.744 | -3,799.55 | -32.47 | 87.18 | -0.0281 | -6,401.39 | 4,443.55 | dte_exit 8, expiry 31, stop 7, take_profit 71 |
| margin | control | ETFs | 323 | 0.728 | -13,494.80 | -41.78 | 48.59 | -0.0438 | -16,855.68 | 6,772.30 | dte_exit 36, expiry 40, stop 29, take_profit 218 |
| margin | t1 | All | 450 | 0.718 | -17,533.40 | -38.96 | 53.35 | -0.0429 | -21,073.43 | 10,781.40 | dte_exit 48, expiry 81, stop 48, take_profit 273 |
| margin | t1 | Stocks | 118 | 0.712 | -3,778.46 | -32.02 | 64.97 | -0.0305 | -3,969.12 | 3,939.96 | dte_exit 9, expiry 38, stop 9, take_profit 62 |
| margin | t1 | ETFs | 332 | 0.72 | -13,754.93 | -41.43 | 48.59 | -0.0483 | -17,104.30 | 6,841.43 | dte_exit 39, expiry 43, stop 39, take_profit 211 |
| cash_debit | control | All | 585 | 0.504 | -24,879.91 | -42.53 | 2.34 | -0.053 | -44,734.41 | 29,488.41 | dte_exit 22, expiry 18, profit_lock 34, stop 231, take_profit 280 |
| cash_debit | control | Stocks | 402 | 0.502 | -34,647.76 | -86.19 | 1.13 | -0.1151 | -39,129.42 | 17,017.76 | dte_exit 16, expiry 13, profit_lock 30, stop 153, take_profit 190 |
| cash_debit | control | ETFs | 183 | 0.508 | 9,767.85 | 53.38 | 34.42 | 0.0581 | -5,605.00 | 12,470.65 | dte_exit 6, expiry 5, profit_lock 4, stop 78, take_profit 90 |
| cash_debit | t1 | All | 743 | 0.522 | -24,886.16 | -33.49 | 9.84 | -0.0271 | -101,878.00 | 58,865.16 | dte_exit 29, expiry 23, profit_lock 57, stop 270, take_profit 364 |
| cash_debit | t1 | Stocks | 538 | 0.506 | -52,076.87 | -96.8 | 4.71 | -0.0744 | -84,402.18 | 49,482.87 | dte_exit 19, expiry 17, profit_lock 40, stop 200, take_profit 262 |
| cash_debit | t1 | ETFs | 205 | 0.566 | 27,190.71 | 132.64 | 65.34 | 0.1252 | -17,475.82 | 9,382.29 | dte_exit 10, expiry 6, profit_lock 17, stop 70, take_profit 102 |

Treatment − control net P&L by segment (**sign differs between Stocks and ETFs for 2 treatment(s)**):

| profile | treatment | All | Stocks | ETFs | sign_differs |
|---|---|---|---|---|---|
| margin | t1 | -239.05 | 21.09 | -260.13 | True |
| cash_debit | t1 | -6.25 | -17,429.11 | 17,422.86 | True |

## Exit-reason mix

| profile | arm | mix |
|---|---|---|
| margin | control | dte_exit 0.1, expiry 0.1614, stop 0.0818, take_profit 0.6568 |
| margin | t1 | dte_exit 0.1067, expiry 0.18, stop 0.1067, take_profit 0.6067 |
| cash_debit | control | dte_exit 0.0376, expiry 0.0308, profit_lock 0.0581, stop 0.3949, take_profit 0.4786 |
| cash_debit | t1 | dte_exit 0.039, expiry 0.031, profit_lock 0.0767, stop 0.3634, take_profit 0.4899 |

## Skip reasons

| profile | arm | screen | blackout | no_candidate | unsettled | sizing | gate_per_underlying | gate_max_open_positions | gate_daily_loss | gate_max_gain | cash | capacity | no_fill | exit_delayed |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| margin | control | 71615 | 739 | 33046 | 0 | 1559 | 0 | 2970 | 4506 | 0 | 0 | 1001 | 227 | 24 |
| margin | t1 | 71615 | 596 | 32544 | 0 | 1438 | 0 | 3169 | 4976 | 0 | 0 | 893 | 222 | 20 |
| cash_debit | control | 71615 | 0 | 24610 | 0 | 5693 | 0 | 3633 | 11245 | 0 | 64 | 492 | 196 | 52 |
| cash_debit | t1 | 71615 | 0 | 20940 | 0 | 5637 | 0 | 10790 | 11649 | 0 | 114 | 780 | 284 | 73 |

## Sub-periods

Fixed windows by entry date.

| profile | arm | period | trades | net_pnl | win_rate | max_dd_pct |
|---|---|---|---|---|---|---|
| margin | control | 2020 crash | 18 | -7,279.60 | 0.5 | 40.18 |
| margin | control | 2020-21 bull | 210 | 6,664.44 | 0.8 | 39.67 |
| margin | control | 2022 bear | 77 | -13,266.13 | 0.636 | 59.39 |
| margin | control | 2023 | 115 | -1,794.86 | 0.704 | 59.13 |
| margin | control | 2024 -> latest | 0 | 0 | – | 3.56 |
| margin | t1 | 2020 crash | 19 | -6,245.71 | 0.474 | 36.14 |
| margin | t1 | 2020-21 bull | 213 | 3,973.91 | 0.775 | 57.2 |
| margin | t1 | 2022 bear | 89 | -13,853.25 | 0.629 | 65.44 |
| margin | t1 | 2023 | 111 | -120.49 | 0.712 | 58.11 |
| margin | t1 | 2024 -> latest | 0 | 0 | – | 3.94 |
| cash_debit | control | 2020 crash | 53 | -18,862.39 | 0.528 | 65.98 |
| cash_debit | control | 2020-21 bull | 317 | -11,573.07 | 0.502 | 92.84 |
| cash_debit | control | 2022 bear | 144 | -2,902.56 | 0.521 | 94.42 |
| cash_debit | control | 2023 | 44 | -490.34 | 0.318 | 85.59 |
| cash_debit | control | 2024 -> latest | 0 | 0 | – | 0 |
| cash_debit | t1 | 2020 crash | 46 | -12,528.10 | 0.435 | 56.73 |
| cash_debit | t1 | 2020-21 bull | 381 | 3,595.25 | 0.546 | 96.26 |
| cash_debit | t1 | 2022 bear | 239 | -22,763.39 | 0.515 | 98.23 |
| cash_debit | t1 | 2023 | 53 | -604.52 | 0.434 | 91.32 |
| cash_debit | t1 | 2024 -> latest | 0 | 0 | – | 0 |

## By regime

| profile | arm | key | trades | net_pnl | win_rate |
|---|---|---|---|---|---|
| margin | control | bear | 53 | 2,386.00 | 0.774 |
| margin | control | bull | 207 | -12,457.29 | 0.821 |
| margin | control | sideways | 180 | -7,223.06 | 0.617 |
| margin | t1 | bear | 55 | -3,679.03 | 0.582 |
| margin | t1 | bull | 201 | -13,804.33 | 0.796 |
| margin | t1 | sideways | 194 | -50.04 | 0.675 |
| cash_debit | control | bear | 138 | -34,194.76 | 0.377 |
| cash_debit | control | bull | 447 | 9,314.85 | 0.544 |
| cash_debit | t1 | bear | 230 | -53,063.61 | 0.448 |
| cash_debit | t1 | bull | 513 | 28,177.45 | 0.556 |

## By structure

| profile | arm | key | trades | net_pnl | win_rate |
|---|---|---|---|---|---|
| margin | control | bear_call | 53 | 2,386.00 | 0.774 |
| margin | control | bull_put | 207 | -12,457.29 | 0.821 |
| margin | control | iron_condor | 180 | -7,223.06 | 0.617 |
| margin | t1 | bear_call | 55 | -3,679.03 | 0.582 |
| margin | t1 | bull_put | 201 | -13,804.33 | 0.796 |
| margin | t1 | iron_condor | 194 | -50.04 | 0.675 |
| cash_debit | control | bear_put | 122 | -19,174.39 | 0.369 |
| cash_debit | control | bull_call | 190 | -3,610.58 | 0.532 |
| cash_debit | control | long_call | 257 | 12,925.43 | 0.553 |
| cash_debit | control | long_put | 16 | -15,020.37 | 0.438 |
| cash_debit | t1 | bear_put | 190 | -29,264.01 | 0.442 |
| cash_debit | t1 | bull_call | 242 | -58,789.54 | 0.471 |
| cash_debit | t1 | long_call | 271 | 86,966.98 | 0.631 |
| cash_debit | t1 | long_put | 40 | -23,799.60 | 0.475 |

## By universe tier

| profile | arm | key | trades | net_pnl | win_rate |
|---|---|---|---|---|---|
| margin | control | core | 311 | -397.20 | 0.746 |
| margin | control | momentum | 11 | -1,295.17 | 0.636 |
| margin | control | rest | 118 | -15,601.97 | 0.703 |
| margin | t1 | core | 325 | -6,192.87 | 0.726 |
| margin | t1 | momentum | 14 | -248.59 | 0.714 |
| margin | t1 | rest | 111 | -11,091.95 | 0.694 |
| cash_debit | control | core | 258 | -12,954.57 | 0.446 |
| cash_debit | control | momentum | 32 | -7,769.23 | 0.469 |
| cash_debit | control | rest | 295 | -4,156.11 | 0.559 |
| cash_debit | t1 | core | 394 | -6,509.54 | 0.503 |
| cash_debit | t1 | momentum | 40 | -19,489.49 | 0.5 |
| cash_debit | t1 | rest | 309 | 1,112.87 | 0.55 |

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
| margin | t1 | broad_market | 271 | -4,808.51 | 0.723 |
| margin | t1 | communication | 12 | -1,339.10 | 0.583 |
| margin | t1 | consumer_discretionary | 9 | -536.87 | 0.667 |
| margin | t1 | energy | 4 | -1,410.69 | 0.5 |
| margin | t1 | financials | 21 | -3,586.93 | 0.619 |
| margin | t1 | health_care | 9 | 2,763.24 | 1 |
| margin | t1 | industrials | 4 | 905.71 | 0.5 |
| margin | t1 | technology | 35 | -263.79 | 0.771 |
| margin | t1 | unknown | 85 | -9,256.45 | 0.718 |
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
| cash_debit | t1 | broad_market | 124 | 36,523.16 | 0.589 |
| cash_debit | t1 | communication | 52 | -3,981.04 | 0.596 |
| cash_debit | t1 | consumer_discretionary | 35 | -7,189.48 | 0.457 |
| cash_debit | t1 | consumer_staples | 5 | 606.31 | 0.8 |
| cash_debit | t1 | energy | 45 | 29,514.32 | 0.733 |
| cash_debit | t1 | financials | 80 | -10,307.16 | 0.512 |
| cash_debit | t1 | health_care | 13 | -1,230.48 | 0.462 |
| cash_debit | t1 | industrials | 15 | 636.49 | 0.4 |
| cash_debit | t1 | technology | 202 | -53,283.46 | 0.411 |
| cash_debit | t1 | unknown | 172 | -16,174.82 | 0.552 |

## ETF vs single stock

| profile | arm | key | trades | net_pnl | win_rate |
|---|---|---|---|---|---|
| margin | control | ETF | 323 | -13,494.80 | 0.728 |
| margin | control | single stock | 117 | -3,799.55 | 0.744 |
| margin | t1 | ETF | 332 | -13,754.93 | 0.72 |
| margin | t1 | single stock | 118 | -3,778.46 | 0.712 |
| cash_debit | control | ETF | 183 | 9,767.85 | 0.508 |
| cash_debit | control | single stock | 402 | -34,647.76 | 0.502 |
| cash_debit | t1 | ETF | 205 | 27,190.71 | 0.566 |
| cash_debit | t1 | single stock | 538 | -52,076.87 | 0.506 |

## By liquidity bucket

| profile | arm | key | trades | net_pnl | win_rate |
|---|---|---|---|---|---|
| margin | control | 100k-1M | 185 | -16,854.43 | 0.719 |
| margin | control | 10k-100k | 86 | -10,195.01 | 0.674 |
| margin | control | < 10k | 7 | 1,704.68 | 1 |
| margin | control | >= 1M | 162 | 8,050.41 | 0.765 |
| margin | t1 | 100k-1M | 203 | -18,689.60 | 0.7 |
| margin | t1 | 10k-100k | 78 | -3,157.83 | 0.705 |
| margin | t1 | < 10k | 9 | 2,038.84 | 0.778 |
| margin | t1 | >= 1M | 160 | 2,275.19 | 0.744 |
| cash_debit | control | 100k-1M | 319 | -24,218.29 | 0.489 |
| cash_debit | control | 10k-100k | 170 | 14,789.09 | 0.594 |
| cash_debit | control | < 10k | 24 | -8,334.74 | 0.458 |
| cash_debit | control | >= 1M | 72 | -7,115.97 | 0.375 |
| cash_debit | t1 | 100k-1M | 405 | -35,258.63 | 0.509 |
| cash_debit | t1 | 10k-100k | 194 | 11,393.47 | 0.567 |
| cash_debit | t1 | < 10k | 20 | -9,054.50 | 0.65 |
| cash_debit | t1 | >= 1M | 124 | 8,033.50 | 0.476 |

## Uncertainty

Moving-block bootstrap CI of the daily equity-change difference vs control (seeded), and the fixed sub-periods each treatment won on net P&L.

| profile | treatment | pnl_diff | ci_lo | ci_hi | periods_won |
|---|---|---|---|---|---|
| margin | t1 | -239.05 | -13,509.87 | 13,632.83 | 2020 crash, 2023 |
| cash_debit | t1 | -6.25 | -101,250.26 | 97,741.95 | 2020 crash, 2020-21 bull |

## Checkpoint ladder

Every ledger run of this arm set; `sign_flip_vs_first_rung` compares the primary metric's treatment − control sign with the first dev rung.

| at | phase | universe | profile | treatment | primary_diff | ci_lo | ci_hi | switch | sign_flip_vs_first_rung |
|---|---|---|---|---|---|---|---|---|---|
| 2026-10-15T23:30:39-04:00 | dev | c125 | cash_debit | t1 | -6.25 | -101,250.26 | 97,741.95 | False | False |
| 2026-10-15T23:30:39-04:00 | dev | c125 | margin | t1 | -239.05 | -13,509.87 | 13,632.83 | False | False |
