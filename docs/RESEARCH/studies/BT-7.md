# BT-7 · E17.3 re-run: regime v2's stance labels are not worse than v1's on net P&L and max drawdown in either profile.

## Header

- phase **holdout**; spec hash `0224b099ec9c`; arm set `2084785ee1d4`; owner owner; created 2026-10-15
- rule: Keep the control if, in every profile, it is not worse than the treatment on net P&L **and** max drawdown, **and** the upper bound of the 90% block-bootstrap CI of the daily P&L difference (control − treatment) is > 0. Roll back to the treatment if the control is worse on both net P&L and drawdown in any profile. Otherwise keep the control, inconclusive.
- primary metric: `net_pnl`
- universe **c500** (ladder c125, c500), version `u500-20261009-s1-e52ff301`; data partitions {'thetadata': 500}
- decisions 2024-01-01 -> 2026-10-15 (exits settle by 2026-10-15); dev 2020-01-01 -> 2023-12-31; holdout 2024-01-01 -> latest
- Not replayed: Discovery and Trending tiers (no historical LLM, Reddit or Stocktwits inputs), the LLM stance (a deterministic trend-label proxy stands in), intraday paths (EOD marks only; exits fire on closes).
- git `28afa22611cd`; variants tried 1 of max 3; earlier holdout runs 0
- arms: control = ranker incumbent; t1 = ranker incumbent, overlay `{"ranking": {"backtest": {"sim": {"regime_model": "v1"}}}}`

## Verdict

The pre-registered rule, verbatim:

> Keep the control if, in every profile, it is not worse than the treatment on net P&L **and** max drawdown, **and** the upper bound of the 90% block-bootstrap CI of the daily P&L difference (control − treatment) is > 0. Roll back to the treatment if the control is worse on both net P&L and drawdown in any profile. Otherwise keep the control, inconclusive.

Original card's wording: _Use the incumbent ranker (margin credit_width, cash_debit debit_width). Keep v2 if, in both profiles, v2 is not worse than v1 on net P&L **and** max drawdown, **and** the upper bound of the 90 % block-bootstrap CI of the daily P&L difference (v2 − v1) is > 0. Roll back to v1 (regime.model: v1) if v2 is worse on both net P&L and drawdown in either profile. Otherwise keep v2, inconclusive._

- VERDICT · margin · t1 vs control: keep, inconclusive (sub-periods 0 [–], P&L diff 34,998, CI [3,536, 69,392])
- VERDICT · cash_debit · t1 vs control: keep control (sub-periods 0 [–], P&L diff -923, CI [-47,528, 50,214])
- VERDICT · all profiles · t1 vs control: keep, inconclusive

A verdict only recommends: a live change still goes through D44 (`Experiment: XP-<n>` with arms copied from this spec) or an owner waiver.

## Per arm × profile

| profile | arm | trades | net_pnl | return_pct | cagr | max_dd_pct | sharpe | sortino | profit_factor | win_rate | avg_days_held | turnover_per_month | cost_share_of_gross | capacity_clipped_share |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| margin | control | 316 | -3,441.30 | -13.77 | -0.0518 | 56.9 | 0.14 | 0.132 | 0.962 | 0.699 | 24.7 | 9.46 | 1.367 | 0.522 |
| margin | t1 | 317 | 31,556.31 | 126.23 | 0.3407 | 36.98 | 0.917 | 0.891 | 1.363 | 0.738 | 25.1 | 9.49 | 0.318 | 0.609 |
| cash_debit | control | 514 | -23,843.49 | -95.37 | -0.6684 | 97.67 | -0.124 | -0.144 | 0.857 | 0.504 | 14.3 | 15.38 | 37.12 | 0.358 |
| cash_debit | t1 | 381 | -24,766.72 | -99.07 | -0.8134 | 99.63 | -0.603 | -0.601 | 0.712 | 0.496 | 14.3 | 11.4 | 1.746 | 0.36 |

## All / Stocks / ETFs

One pool per run; the verdict is stated on All, Stocks and ETFs are diagnostic. `dd_contribution` = net P&L of the trades closed inside the run's max-drawdown window; `spread_cost` = P&L at mid − net P&L (spread + fees).

| profile | arm | segment | trades | win_rate | net_pnl | mean_pnl | median_pnl | pnl_per_risk | dd_contribution | spread_cost | exit_reasons |
|---|---|---|---|---|---|---|---|---|---|---|---|
| margin | control | All | 316 | 0.699 | -3,441.30 | -10.89 | 99.22 | -0.0072 | -18,260.31 | 12,810.80 | dte_exit 40, expiry 82, stop 26, take_profit 168 |
| margin | control | Stocks | 79 | 0.633 | -260.35 | -3.3 | 152.71 | -0.0016 | -7,116.27 | 6,482.35 | dte_exit 10, expiry 28, stop 7, take_profit 34 |
| margin | control | ETFs | 237 | 0.722 | -3,180.95 | -13.42 | 89.59 | -0.0101 | -11,144.04 | 6,328.45 | dte_exit 30, expiry 54, stop 19, take_profit 134 |
| margin | t1 | All | 317 | 0.738 | 31,556.31 | 99.55 | 140.18 | 0.0545 | -12,797.03 | 14,686.69 | dte_exit 44, expiry 86, stop 14, take_profit 173 |
| margin | t1 | Stocks | 72 | 0.681 | -401.02 | -5.57 | 224.21 | -0.002 | -13,317.61 | 7,698.52 | dte_exit 9, expiry 23, stop 4, take_profit 36 |
| margin | t1 | ETFs | 245 | 0.755 | 31,957.33 | 130.44 | 123.39 | 0.0846 | 520.58 | 6,988.17 | dte_exit 35, expiry 63, stop 10, take_profit 137 |
| cash_debit | control | All | 514 | 0.504 | -23,843.49 | -46.39 | 5.43 | -0.0569 | -47,646.59 | 23,217.99 | dte_exit 25, expiry 24, profit_lock 39, stop 190, take_profit 236 |
| cash_debit | control | Stocks | 334 | 0.47 | -25,672.34 | -76.86 | -16.58 | -0.0881 | -34,676.81 | 16,722.34 | dte_exit 21, expiry 24, profit_lock 23, stop 124, take_profit 142 |
| cash_debit | control | ETFs | 180 | 0.567 | 1,828.85 | 10.16 | 22.85 | 0.0143 | -12,969.79 | 6,495.65 | dte_exit 4, profit_lock 16, stop 66, take_profit 94 |
| cash_debit | t1 | All | 381 | 0.496 | -24,766.72 | -65 | -1.64 | -0.1203 | -40,621.60 | 15,748.22 | dte_exit 14, expiry 18, profit_lock 26, stop 139, take_profit 184 |
| cash_debit | t1 | Stocks | 264 | 0.477 | -13,110.99 | -49.66 | -16.78 | -0.0898 | -17,531.78 | 12,649.49 | dte_exit 11, expiry 17, profit_lock 14, stop 98, take_profit 124 |
| cash_debit | t1 | ETFs | 117 | 0.538 | -11,655.73 | -99.62 | 9.68 | -0.195 | -23,089.82 | 3,098.73 | dte_exit 3, expiry 1, profit_lock 12, stop 41, take_profit 60 |

Treatment − control net P&L by segment (**sign differs between Stocks and ETFs for 2 treatment(s)**):

| profile | treatment | All | Stocks | ETFs | sign_differs |
|---|---|---|---|---|---|
| margin | t1 | 34,997.61 | -140.67 | 35,138.28 | True |
| cash_debit | t1 | -923.23 | 12,561.35 | -13,484.58 | True |

## Exit-reason mix

| profile | arm | mix |
|---|---|---|
| margin | control | dte_exit 0.1266, expiry 0.2595, stop 0.0823, take_profit 0.5316 |
| margin | t1 | dte_exit 0.1388, expiry 0.2713, stop 0.0442, take_profit 0.5457 |
| cash_debit | control | dte_exit 0.0486, expiry 0.0467, profit_lock 0.0759, stop 0.3696, take_profit 0.4591 |
| cash_debit | t1 | dte_exit 0.0367, expiry 0.0472, profit_lock 0.0682, stop 0.3648, take_profit 0.4829 |

## Skip reasons

| profile | arm | screen | blackout | no_candidate | unsettled | sizing | gate_per_underlying | gate_max_open_positions | gate_daily_loss | gate_max_gain | cash | capacity | no_fill | exit_delayed |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| margin | control | 226930 | 613 | 25442 | 255 | 510 | 0 | 4424 | 2815 | 0 | 0 | 624 | 153 | 13 |
| margin | t1 | 226930 | 428 | 26017 | 256 | 344 | 0 | 4587 | 2318 | 0 | 0 | 573 | 134 | 19 |
| cash_debit | control | 226930 | 0 | 18691 | 221 | 2111 | 0 | 3031 | 9546 | 0 | 31 | 311 | 170 | 86 |
| cash_debit | t1 | 226930 | 0 | 17015 | 715 | 7432 | 0 | 3401 | 8629 | 0 | 31 | 243 | 87 | 41 |

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
| margin | t1 | 2024 -> latest | 317 | 31,556.31 | 0.738 | 36.98 |
| cash_debit | control | 2020 crash | 0 | 0 | – | 0 |
| cash_debit | control | 2020-21 bull | 0 | 0 | – | 0 |
| cash_debit | control | 2022 bear | 0 | 0 | – | 0 |
| cash_debit | control | 2023 | 0 | 0 | – | 0 |
| cash_debit | control | 2024 -> latest | 514 | -23,843.49 | 0.504 | 97.67 |
| cash_debit | t1 | 2020 crash | 0 | 0 | – | 0 |
| cash_debit | t1 | 2020-21 bull | 0 | 0 | – | 0 |
| cash_debit | t1 | 2022 bear | 0 | 0 | – | 0 |
| cash_debit | t1 | 2023 | 0 | 0 | – | 0 |
| cash_debit | t1 | 2024 -> latest | 381 | -24,766.72 | 0.496 | 99.63 |

## By regime

| profile | arm | key | trades | net_pnl | win_rate |
|---|---|---|---|---|---|
| margin | control | bear | 21 | -8,592.71 | 0.524 |
| margin | control | bull | 101 | -1,553.40 | 0.842 |
| margin | control | sideways | 194 | 6,704.81 | 0.644 |
| margin | t1 | bear | 19 | 215.31 | 0.789 |
| margin | t1 | bull | 75 | -1,331.71 | 0.853 |
| margin | t1 | sideways | 223 | 32,672.70 | 0.695 |
| cash_debit | control | bear | 155 | -28,140.64 | 0.406 |
| cash_debit | control | bull | 359 | 4,297.15 | 0.546 |
| cash_debit | t1 | bear | 140 | -14,365.03 | 0.414 |
| cash_debit | t1 | bull | 241 | -10,401.69 | 0.544 |

## By structure

| profile | arm | key | trades | net_pnl | win_rate |
|---|---|---|---|---|---|
| margin | control | bear_call | 21 | -8,592.71 | 0.524 |
| margin | control | bull_put | 101 | -1,553.40 | 0.842 |
| margin | control | iron_condor | 194 | 6,704.81 | 0.644 |
| margin | t1 | bear_call | 19 | 215.31 | 0.789 |
| margin | t1 | bull_put | 75 | -1,331.71 | 0.853 |
| margin | t1 | iron_condor | 223 | 32,672.70 | 0.695 |
| cash_debit | control | bear_put | 117 | -20,070.89 | 0.376 |
| cash_debit | control | bull_call | 205 | 3,903.30 | 0.551 |
| cash_debit | control | long_call | 154 | 393.85 | 0.539 |
| cash_debit | control | long_put | 38 | -8,069.75 | 0.5 |
| cash_debit | t1 | bear_put | 114 | -6,736.60 | 0.421 |
| cash_debit | t1 | bull_call | 173 | -10,093.41 | 0.538 |
| cash_debit | t1 | long_call | 68 | -308.28 | 0.559 |
| cash_debit | t1 | long_put | 26 | -7,628.43 | 0.385 |

## By universe tier

| profile | arm | key | trades | net_pnl | win_rate |
|---|---|---|---|---|---|
| margin | control | core | 217 | -10,789.39 | 0.673 |
| margin | control | momentum | 3 | 3,238.31 | 1 |
| margin | control | rest | 96 | 4,109.78 | 0.75 |
| margin | t1 | core | 213 | 14,407.52 | 0.709 |
| margin | t1 | momentum | 5 | 3,924.15 | 1 |
| margin | t1 | rest | 99 | 13,224.64 | 0.788 |
| cash_debit | control | core | 201 | -8,621.54 | 0.438 |
| cash_debit | control | momentum | 16 | -2,410.71 | 0.375 |
| cash_debit | control | rest | 297 | -12,811.24 | 0.556 |
| cash_debit | t1 | core | 134 | -13,685.86 | 0.425 |
| cash_debit | t1 | momentum | 16 | 2,844.77 | 0.5 |
| cash_debit | t1 | rest | 231 | -13,925.63 | 0.537 |

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
| margin | t1 | broad_market | 164 | 17,611.61 | 0.726 |
| margin | t1 | communication | 1 | 248.59 | 1 |
| margin | t1 | consumer_discretionary | 12 | -15,765.43 | 0.333 |
| margin | t1 | consumer_staples | 1 | 384.10 | 1 |
| margin | t1 | energy | 1 | -431.81 | 0 |
| margin | t1 | financials | 12 | 8,186.00 | 0.833 |
| margin | t1 | health_care | 3 | 2,338.95 | 0.667 |
| margin | t1 | technology | 29 | 4,888.20 | 0.724 |
| margin | t1 | unknown | 94 | 14,096.10 | 0.809 |
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
| cash_debit | t1 | broad_market | 26 | -2,120.94 | 0.308 |
| cash_debit | t1 | communication | 2 | 239.29 | 0.5 |
| cash_debit | t1 | consumer_discretionary | 22 | -2,786.86 | 0.409 |
| cash_debit | t1 | energy | 2 | -116.66 | 0.5 |
| cash_debit | t1 | financials | 44 | -1,294.64 | 0.455 |
| cash_debit | t1 | health_care | 5 | 1,863.90 | 0.4 |
| cash_debit | t1 | industrials | 7 | 1,652.61 | 0.429 |
| cash_debit | t1 | technology | 115 | -9,501.29 | 0.496 |
| cash_debit | t1 | unknown | 158 | -12,702.12 | 0.557 |

## ETF vs single stock

| profile | arm | key | trades | net_pnl | win_rate |
|---|---|---|---|---|---|
| margin | control | ETF | 237 | -3,180.95 | 0.722 |
| margin | control | single stock | 79 | -260.35 | 0.633 |
| margin | t1 | ETF | 245 | 31,957.33 | 0.755 |
| margin | t1 | single stock | 72 | -401.02 | 0.681 |
| cash_debit | control | ETF | 180 | 1,828.85 | 0.567 |
| cash_debit | control | single stock | 334 | -25,672.34 | 0.47 |
| cash_debit | t1 | ETF | 117 | -11,655.73 | 0.538 |
| cash_debit | t1 | single stock | 264 | -13,110.99 | 0.477 |

## By liquidity bucket

| profile | arm | key | trades | net_pnl | win_rate |
|---|---|---|---|---|---|
| margin | control | 100k-1M | 124 | 1,188.34 | 0.702 |
| margin | control | 10k-100k | 33 | 7,178.93 | 0.697 |
| margin | control | >= 1M | 159 | -11,808.57 | 0.698 |
| margin | t1 | 100k-1M | 124 | 9,838.56 | 0.71 |
| margin | t1 | 10k-100k | 30 | 13,728.38 | 0.833 |
| margin | t1 | >= 1M | 163 | 7,989.37 | 0.742 |
| cash_debit | control | 100k-1M | 278 | -6,343.49 | 0.54 |
| cash_debit | control | 10k-100k | 94 | -22.81 | 0.521 |
| cash_debit | control | < 10k | 32 | -11,029.65 | 0.406 |
| cash_debit | control | >= 1M | 110 | -6,447.55 | 0.427 |
| cash_debit | t1 | 100k-1M | 228 | -21,157.03 | 0.513 |
| cash_debit | t1 | 10k-100k | 69 | 3,999.02 | 0.493 |
| cash_debit | t1 | < 10k | 15 | -4,026.84 | 0.533 |
| cash_debit | t1 | >= 1M | 69 | -3,581.86 | 0.435 |

## Uncertainty

Moving-block bootstrap CI of the daily equity-change difference vs control (seeded), and the fixed sub-periods each treatment won on net P&L.

| profile | treatment | pnl_diff | ci_lo | ci_hi | periods_won |
|---|---|---|---|---|---|
| margin | t1 | 34,997.61 | 3,535.57 | 69,392.32 | 2024 -> latest |
| cash_debit | t1 | -923.23 | -47,528.02 | 50,214.07 | – |

## Checkpoint ladder

Every ledger run of this arm set; `sign_flip_vs_first_rung` compares the primary metric's treatment − control sign with the first dev rung.

| at | phase | universe | profile | treatment | primary_diff | ci_lo | ci_hi | switch | sign_flip_vs_first_rung |
|---|---|---|---|---|---|---|---|---|---|
| 2026-10-15T23:30:39-04:00 | dev | c125 | cash_debit | t1 | -6.25 | -101,250.26 | 97,741.95 | False | False |
| 2026-10-15T23:30:39-04:00 | dev | c125 | margin | t1 | -239.05 | -13,509.87 | 13,632.83 | False | False |
| 2026-10-16T11:50:42-04:00 | dev | c500 | cash_debit | t1 | -11.15 | -67,335.75 | 78,858.43 | False | False |
| 2026-10-16T11:50:42-04:00 | dev | c500 | margin | t1 | -3,751.24 | -20,306.15 | 14,090.28 | False | False |
| 2026-10-16T14:12:43-04:00 | holdout | c500 | cash_debit | t1 | -923.23 | -47,528.02 | 50,214.07 | False | False |
| 2026-10-16T14:12:43-04:00 | holdout | c500 | margin | t1 | 34,997.61 | 3,535.57 | 69,392.32 | False | True |
