# BT-1 · On the cash_debit profile, ranking the menu by managed Net EV picks better trades than the incumbent debit_width ranker.

## Header

- phase **dev**; spec hash `f6dec356b185`; arm set `da03b0a634c0`; owner owner; created 2026-10-10
- rule: Switch the default to a challenger only if it beats the incumbent on net P&L (higher) **and** max drawdown (not larger) in ≥ 2 of 3 sub-periods (bear, sideways, bull trend regime at entry), **and** the 90% block-bootstrap CI of its daily P&L difference vs the incumbent excludes 0 (lower bound > 0). Otherwise keep the incumbent and report the closest challenger.
- primary metric: `net_pnl`
- universe **c125** (ladder c125), version `u500-20261009-s1-cbfbdeb9`; data partitions {'alpaca': 8}
- decisions 2024-03-01 -> 2025-08-29 (exits settle by 2025-11-27); dev 2024-03-01 -> 2025-08-29; holdout 2025-09-02 -> latest
- Not replayed: Discovery and Trending tiers (no historical LLM, Reddit or Stocktwits inputs), the LLM stance (a deterministic trend-label proxy stands in), intraday paths (EOD marks only; exits fire on closes).
- git `b11775d4d73e`; variants tried 1 of max 3; earlier holdout runs 0
- arms: control = ranker debit_width; t1 = ranker managed_net_ev

## Verdict

The pre-registered rule, verbatim:

> Switch the default to a challenger only if it beats the incumbent on net P&L (higher) **and** max drawdown (not larger) in ≥ 2 of 3 sub-periods (bear, sideways, bull trend regime at entry), **and** the 90% block-bootstrap CI of its daily P&L difference vs the incumbent excludes 0 (lower bound > 0). Otherwise keep the incumbent and report the closest challenger.

- dev result (not a verdict) · cash_debit · t1 vs control: keep control (won 0 sub-periods [–], P&L diff -2,004, CI [-7,241, 2,851])

A verdict only recommends: a live change still goes through D44 (`Experiment: XP-<n>` with arms copied from this spec) or an owner waiver.

## Per arm × profile

| profile | arm | trades | net_pnl | return_pct | cagr | max_dd_pct | sharpe | sortino | profit_factor | win_rate | avg_days_held | turnover_per_month | cost_share_of_gross | capacity_clipped_share |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| cash_debit | control | 120 | -14,866.80 | -59.47 | -0.4051 | 70.75 | -0.329 | -0.362 | 0.684 | 0.508 | 15.1 | 5.75 | 1.087 | 0.083 |
| cash_debit | t1 | 124 | -16,871.02 | -67.48 | -0.476 | 78.63 | -0.547 | -0.577 | 0.605 | 0.508 | 16 | 5.94 | 0.406 | 0.065 |

## Exit-reason mix

| profile | arm | mix |
|---|---|---|
| cash_debit | control | dte_exit 0.0417, profit_lock 0.0833, stop 0.425, take_profit 0.45 |
| cash_debit | t1 | dte_exit 0.0565, profit_lock 0.1048, stop 0.3952, take_profit 0.4435 |

## Skip reasons

| profile | arm | screen | blackout | no_candidate | unsettled | sizing | gate_per_underlying | gate_max_open_positions | gate_daily_loss | gate_max_gain | cash | capacity | no_fill | exit_delayed |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| cash_debit | control | 0 | 0 | 1988 | 0 | 645 | 0 | 13 | 680 | 0 | 0 | 19 | 0 | 0 |
| cash_debit | t1 | 0 | 0 | 2019 | 0 | 552 | 0 | 82 | 656 | 0 | 0 | 15 | 0 | 0 |

## Sub-periods

Fixed windows by entry date.

| profile | arm | period | trades | net_pnl | win_rate | max_dd_pct |
|---|---|---|---|---|---|---|
| cash_debit | control | 2020 crash | 0 | 0 | – | 0 |
| cash_debit | control | 2020-21 bull | 0 | 0 | – | 0 |
| cash_debit | control | 2022 bear | 0 | 0 | – | 0 |
| cash_debit | control | 2023 | 0 | 0 | – | 0 |
| cash_debit | control | 2024 -> latest | 120 | -14,866.80 | 0.508 | 70.75 |
| cash_debit | t1 | 2020 crash | 0 | 0 | – | 0 |
| cash_debit | t1 | 2020-21 bull | 0 | 0 | – | 0 |
| cash_debit | t1 | 2022 bear | 0 | 0 | – | 0 |
| cash_debit | t1 | 2023 | 0 | 0 | – | 0 |
| cash_debit | t1 | 2024 -> latest | 124 | -16,871.02 | 0.508 | 78.63 |

## By regime

| profile | arm | key | trades | net_pnl | win_rate |
|---|---|---|---|---|---|
| cash_debit | control | bear | 38 | -13,717.97 | 0.421 |
| cash_debit | control | bull | 82 | -1,148.83 | 0.549 |
| cash_debit | t1 | bear | 41 | -14,794.37 | 0.39 |
| cash_debit | t1 | bull | 83 | -2,076.65 | 0.566 |

## By universe tier

| profile | arm | key | trades | net_pnl | win_rate |
|---|---|---|---|---|---|
| cash_debit | control | core | 120 | -14,866.80 | 0.508 |
| cash_debit | t1 | core | 124 | -16,871.02 | 0.508 |

## By sector

| profile | arm | key | trades | net_pnl | win_rate |
|---|---|---|---|---|---|
| cash_debit | control | broad_market | 60 | -3,887.50 | 0.517 |
| cash_debit | control | consumer_discretionary | 21 | -9,050.04 | 0.333 |
| cash_debit | control | technology | 39 | -1,929.26 | 0.59 |
| cash_debit | t1 | broad_market | 59 | -7,051.25 | 0.508 |
| cash_debit | t1 | consumer_discretionary | 19 | -8,179.43 | 0.263 |
| cash_debit | t1 | technology | 46 | -1,640.35 | 0.609 |

## ETF vs single stock

| profile | arm | key | trades | net_pnl | win_rate |
|---|---|---|---|---|---|
| cash_debit | control | ETF | 60 | -3,887.50 | 0.517 |
| cash_debit | control | single stock | 60 | -10,979.30 | 0.5 |
| cash_debit | t1 | ETF | 59 | -7,051.25 | 0.508 |
| cash_debit | t1 | single stock | 65 | -9,819.78 | 0.508 |

## By liquidity bucket

| profile | arm | key | trades | net_pnl | win_rate |
|---|---|---|---|---|---|
| cash_debit | control | 100k-1M | 34 | -7,524.72 | 0.5 |
| cash_debit | control | >= 1M | 86 | -7,342.08 | 0.512 |
| cash_debit | t1 | 100k-1M | 34 | -6,438.03 | 0.471 |
| cash_debit | t1 | >= 1M | 90 | -10,432.99 | 0.522 |

## Uncertainty

Moving-block bootstrap CI of the daily equity-change difference vs control (seeded), and the fixed sub-periods each treatment won on net P&L.

| profile | treatment | pnl_diff | ci_lo | ci_hi | periods_won |
|---|---|---|---|---|---|
| cash_debit | t1 | -2,004.22 | -7,240.89 | 2,851.22 | – |

## Checkpoint ladder

Every ledger run of this arm set; `sign_flip_vs_first_rung` compares the primary metric's treatment − control sign with the first dev rung.

| at | phase | universe | profile | treatment | primary_diff | ci_lo | ci_hi | switch | sign_flip_vs_first_rung |
|---|---|---|---|---|---|---|---|---|---|
| 2026-10-11T02:11:50-04:00 | dev | c125 | cash_debit | t1 | -2,004.22 | -7,240.89 | 2,851.22 | False | False |
