# BT-8 · Data-only control: switching the E7.5 six-ticker walk from Alpaca smile marks to ThetaData quoted marks changes the incumbents' results by less than the D25 CI.

## Header

- phase **holdout**; spec hash `bd19b7413a6e`; arm set `e5a6ebe45788`; owner owner; created 2026-10-15
- rule: Switch the default to a challenger only if it beats the incumbent on net P&L (higher) **and** max drawdown (not larger) in ≥ 2 of 3 sub-periods (bear, sideways, bull trend regime at entry), **and** the 90% block-bootstrap CI of its daily P&L difference vs the incumbent excludes 0 (lower bound > 0). Otherwise keep the incumbent and report the closest challenger.
- primary metric: `net_pnl`
- universe **c125** (ladder c125), version `u500-20261009-s1-e52ff301`; data partitions {'thetadata': 6}
- decisions 2026-08-03 -> 2026-10-14 (exits settle by 2026-10-14); dev 2024-03-01 -> 2026-07-31; holdout 2026-08-03 -> latest
- Not replayed: Discovery and Trending tiers (no historical LLM, Reddit or Stocktwits inputs), the LLM stance (a deterministic trend-label proxy stands in), intraday paths (EOD marks only; exits fire on closes).
- git `88d6dfaaa465`; variants tried 1 of max 3; earlier holdout runs 0
- arms: control = ranker incumbent, overlay `{"ranking": {"backtest": {"data": {"fallback_provider": null, "marks": "smile", "provider": "alpaca"}, "sim": {"selection": "per_ticker"}}}}`; t1 = ranker incumbent, overlay `{"ranking": {"backtest": {"data": {"fallback_provider": null, "marks": "quoted", "provider": "thetadata"}, "sim": {"selection": "per_ticker"}}}}`

## Verdict

The pre-registered rule, verbatim:

> Switch the default to a challenger only if it beats the incumbent on net P&L (higher) **and** max drawdown (not larger) in ≥ 2 of 3 sub-periods (bear, sideways, bull trend regime at entry), **and** the 90% block-bootstrap CI of its daily P&L difference vs the incumbent excludes 0 (lower bound > 0). Otherwise keep the incumbent and report the closest challenger.

Original card's wording: _The D25 rule with the old data as the incumbent: the new data "differs" only if it passes the rule. Descriptive: no default depends on it._

- VERDICT · margin · t1 vs control: keep control (sub-periods 1 [bull], P&L diff -2,630, CI [-3,955, -585])
- VERDICT · cash_debit · t1 vs control: keep control (sub-periods 1 [bull], P&L diff -188, CI [-1,218, 3,424])

A verdict only recommends: a live change still goes through D44 (`Experiment: XP-<n>` with arms copied from this spec) or an owner waiver.

## Per arm × profile

| profile | arm | trades | net_pnl | return_pct | cagr | max_dd_pct | sharpe | sortino | profit_factor | win_rate | avg_days_held | turnover_per_month | cost_share_of_gross | capacity_clipped_share |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| margin | control | 3 | 2,282.71 | 9.13 | 0.6102 | 1.15 | 3.705 | 5.955 | – | 1 | 26.7 | 1.36 | 0.059 | 0 |
| margin | t1 | 2 | -346.79 | -1.39 | -0.0733 | 6.99 | -0.525 | -0.344 | 0.274 | 0.5 | 29 | 0.91 | 0.485 | 0 |
| cash_debit | control | 6 | -11,816.48 | -47.27 | -0.9695 | 47.27 | -4.699 | -4.174 | 0 | 0 | 18 | 2.73 | 0.016 | 0 |
| cash_debit | t1 | 5 | -12,004.04 | -48.02 | -0.9717 | 48.02 | -5.657 | -4.153 | 0 | 0 | 17.4 | 2.27 | 0.043 | 0 |

## All / Stocks / ETFs

One pool per run; the verdict is stated on All, Stocks and ETFs are diagnostic. `dd_contribution` = net P&L of the trades closed inside the run's max-drawdown window; `spread_cost` = P&L at mid − net P&L (spread + fees).

| profile | arm | segment | trades | win_rate | net_pnl | mean_pnl | median_pnl | pnl_per_risk | dd_contribution | spread_cost | exit_reasons |
|---|---|---|---|---|---|---|---|---|---|---|---|
| margin | control | All | 3 | 1 | 2,282.71 | 760.90 | 917.02 | 0.2335 | 0 | 142.46 | expiry 1, take_profit 2 |
| margin | control | Stocks | 0 | – | 0 | – | – | – | 0 | 0 | – |
| margin | control | ETFs | 3 | 1 | 2,282.71 | 760.90 | 917.02 | 0.2335 | 0 | 142.46 | expiry 1, take_profit 2 |
| margin | t1 | All | 2 | 0.5 | -346.79 | -173.39 | -173.39 | -0.052 | -477.97 | 113.29 | expiry 1, take_profit 1 |
| margin | t1 | Stocks | 0 | – | 0 | – | – | – | 0 | 0 | – |
| margin | t1 | ETFs | 2 | 0.5 | -346.79 | -173.39 | -173.39 | -0.052 | -477.97 | 113.29 | expiry 1, take_profit 1 |
| cash_debit | control | All | 6 | 0 | -11,816.48 | -1,969.41 | -1,824.18 | -0.7882 | -11,816.48 | 181.71 | stop 6 |
| cash_debit | control | Stocks | 3 | 0 | -5,646.98 | -1,882.33 | -1,776.33 | -0.795 | -5,646.98 | 85.51 | stop 3 |
| cash_debit | control | ETFs | 3 | 0 | -6,169.50 | -2,056.50 | -1,872.02 | -0.782 | -6,169.50 | 96.2 | stop 3 |
| cash_debit | t1 | All | 5 | 0 | -12,004.04 | -2,400.81 | -2,339.47 | -0.8018 | -12,004.04 | 498.54 | stop 5 |
| cash_debit | t1 | Stocks | 3 | 0 | -7,127.83 | -2,375.94 | -2,305.32 | -0.8151 | -7,127.83 | 443.33 | stop 3 |
| cash_debit | t1 | ETFs | 2 | 0 | -4,876.21 | -2,438.10 | -2,438.10 | -0.7831 | -4,876.21 | 55.21 | stop 2 |

Treatment − control net P&L by segment (**sign differs between Stocks and ETFs for 1 treatment(s)**):

| profile | treatment | All | Stocks | ETFs | sign_differs |
|---|---|---|---|---|---|
| margin | t1 | -2,629.50 | 0 | -2,629.50 | False |
| cash_debit | t1 | -187.56 | -1,480.85 | 1,293.29 | True |

## Exit-reason mix

| profile | arm | mix |
|---|---|---|
| margin | control | expiry 0.3333, take_profit 0.6667 |
| margin | t1 | expiry 0.5, take_profit 0.5 |
| cash_debit | control | stop 1 |
| cash_debit | t1 | stop 1 |

## Skip reasons

| profile | arm | screen | blackout | no_candidate | unsettled | sizing | gate_per_underlying | gate_max_open_positions | gate_daily_loss | gate_max_gain | cash | capacity | no_fill | exit_delayed |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| margin | control | 0 | 0 | 218 | 62 | 0 | 11 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| margin | t1 | 0 | 0 | 226 | 58 | 0 | 7 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| cash_debit | control | 0 | 0 | 16 | 11 | 0 | 7 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| cash_debit | t1 | 0 | 0 | 8 | 19 | 0 | 8 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |

## Sub-periods

Fixed windows by entry date.

| profile | arm | period | trades | net_pnl | win_rate | max_dd_pct |
|---|---|---|---|---|---|---|
| margin | control | 2020 crash | 0 | 0 | – | 0 |
| margin | control | 2020-21 bull | 0 | 0 | – | 0 |
| margin | control | 2022 bear | 0 | 0 | – | 0 |
| margin | control | 2023 | 0 | 0 | – | 0 |
| margin | control | 2024 -> latest | 3 | 2,282.71 | 1 | 1.15 |
| margin | t1 | 2020 crash | 0 | 0 | – | 0 |
| margin | t1 | 2020-21 bull | 0 | 0 | – | 0 |
| margin | t1 | 2022 bear | 0 | 0 | – | 0 |
| margin | t1 | 2023 | 0 | 0 | – | 0 |
| margin | t1 | 2024 -> latest | 2 | -346.79 | 0.5 | 6.99 |
| cash_debit | control | 2020 crash | 0 | 0 | – | 0 |
| cash_debit | control | 2020-21 bull | 0 | 0 | – | 0 |
| cash_debit | control | 2022 bear | 0 | 0 | – | 0 |
| cash_debit | control | 2023 | 0 | 0 | – | 0 |
| cash_debit | control | 2024 -> latest | 6 | -11,816.48 | 0 | 47.27 |
| cash_debit | t1 | 2020 crash | 0 | 0 | – | 0 |
| cash_debit | t1 | 2020-21 bull | 0 | 0 | – | 0 |
| cash_debit | t1 | 2022 bear | 0 | 0 | – | 0 |
| cash_debit | t1 | 2023 | 0 | 0 | – | 0 |
| cash_debit | t1 | 2024 -> latest | 5 | -12,004.04 | 0 | 48.02 |

## By regime

| profile | arm | key | trades | net_pnl | win_rate |
|---|---|---|---|---|---|
| margin | control | bull | 1 | 130.40 | 1 |
| margin | control | sideways | 2 | 2,152.31 | 1 |
| margin | t1 | bull | 1 | 131.18 | 1 |
| margin | t1 | sideways | 1 | -477.97 | 0 |
| cash_debit | control | bear | 1 | -1,679.33 | 0 |
| cash_debit | control | bull | 5 | -10,137.16 | 0 |
| cash_debit | t1 | bear | 1 | -3,042.42 | 0 |
| cash_debit | t1 | bull | 4 | -8,961.61 | 0 |

## By structure

| profile | arm | key | trades | net_pnl | win_rate |
|---|---|---|---|---|---|
| margin | control | bull_put | 1 | 130.40 | 1 |
| margin | control | iron_condor | 2 | 2,152.31 | 1 |
| margin | t1 | bull_put | 1 | 131.18 | 1 |
| margin | t1 | iron_condor | 1 | -477.97 | 0 |
| cash_debit | control | long_call | 5 | -10,137.16 | 0 |
| cash_debit | control | long_put | 1 | -1,679.33 | 0 |
| cash_debit | t1 | bear_put | 1 | -3,042.42 | 0 |
| cash_debit | t1 | bull_call | 1 | -2,339.47 | 0 |
| cash_debit | t1 | long_call | 3 | -6,622.14 | 0 |

## By universe tier

| profile | arm | key | trades | net_pnl | win_rate |
|---|---|---|---|---|---|
| margin | control | core | 3 | 2,282.71 | 1 |
| margin | t1 | core | 2 | -346.79 | 0.5 |
| cash_debit | control | core | 6 | -11,816.48 | 0 |
| cash_debit | t1 | core | 5 | -12,004.04 | 0 |

## By sector

| profile | arm | key | trades | net_pnl | win_rate |
|---|---|---|---|---|---|
| margin | control | broad_market | 3 | 2,282.71 | 1 |
| margin | t1 | broad_market | 2 | -346.79 | 0.5 |
| cash_debit | control | broad_market | 3 | -6,169.50 | 0 |
| cash_debit | control | consumer_discretionary | 2 | -3,455.66 | 0 |
| cash_debit | control | technology | 1 | -2,191.32 | 0 |
| cash_debit | t1 | broad_market | 2 | -4,876.21 | 0 |
| cash_debit | t1 | consumer_discretionary | 2 | -4,822.51 | 0 |
| cash_debit | t1 | technology | 1 | -2,305.32 | 0 |

## ETF vs single stock

| profile | arm | key | trades | net_pnl | win_rate |
|---|---|---|---|---|---|
| margin | control | ETF | 3 | 2,282.71 | 1 |
| margin | t1 | ETF | 2 | -346.79 | 0.5 |
| cash_debit | control | ETF | 3 | -6,169.50 | 0 |
| cash_debit | control | single stock | 3 | -5,646.98 | 0 |
| cash_debit | t1 | ETF | 2 | -4,876.21 | 0 |
| cash_debit | t1 | single stock | 3 | -7,127.83 | 0 |

## By liquidity bucket

| profile | arm | key | trades | net_pnl | win_rate |
|---|---|---|---|---|---|
| margin | control | < 10k | 3 | 2,282.71 | 1 |
| margin | t1 | < 10k | 2 | -346.79 | 0.5 |
| cash_debit | control | < 10k | 6 | -11,816.48 | 0 |
| cash_debit | t1 | < 10k | 5 | -12,004.04 | 0 |

## Uncertainty

Moving-block bootstrap CI of the daily equity-change difference vs control (seeded), and the fixed sub-periods each treatment won on net P&L.

| profile | treatment | pnl_diff | ci_lo | ci_hi | periods_won |
|---|---|---|---|---|---|
| margin | t1 | -2,629.50 | -3,955.16 | -585.36 | – |
| cash_debit | t1 | -187.55 | -1,217.89 | 3,424.42 | – |

## Checkpoint ladder

Every ledger run of this arm set; `sign_flip_vs_first_rung` compares the primary metric's treatment − control sign with the first dev rung.

| at | phase | universe | profile | treatment | primary_diff | ci_lo | ci_hi | switch | sign_flip_vs_first_rung |
|---|---|---|---|---|---|---|---|---|---|
| 2026-10-15T14:24:15-04:00 | dev | c125 | cash_debit | t1 | -5,800.46 | -29,760.84 | 12,100.49 | False | False |
| 2026-10-15T14:24:15-04:00 | dev | c125 | margin | t1 | 1,832.40 | -41,446.55 | 37,943.30 | False | False |
| 2026-10-15T15:39:21-04:00 | holdout | c125 | cash_debit | t1 | -187.56 | -1,217.89 | 3,424.42 | False | False |
| 2026-10-15T15:39:21-04:00 | holdout | c125 | margin | t1 | -2,629.50 | -3,955.16 | -585.36 | False | True |
