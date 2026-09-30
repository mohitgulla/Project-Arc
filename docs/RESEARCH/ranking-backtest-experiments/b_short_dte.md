# Ranking backtest run (E7.5)

Tickers: SPY, QQQ, IWM, AAPL, NVDA, TSLA · entries 2024-03-01 → 2026-07-31 · profiles: margin, cash_debit · equity $100,000 · MC paths 5000 · cost x=0.25, est. spread max(0.03, 0.04·mid)

Decision sessions with a menu per ticker: SPY 606, QQQ 606, IWM 606, AAPL 606, NVDA 606, TSLA 606

Mean menu size: margin 3.7, cash_debit 2.8

Entry DTE windows: margin 21–30, cash_debit 21–35

## Decision rule (fixed before the run)

Switch the default to a challenger only if it beats the incumbent on net P&L (higher) **and** max drawdown (not larger) in ≥ 2 of 3 sub-periods (bear, sideways, bull trend regime at entry), **and** the 90% block-bootstrap CI of its daily P&L difference vs the incumbent excludes 0 (lower bound > 0). Otherwise keep the incumbent and report the closest challenger.

## Data caveats

- Alpaca options history has **no historical quotes**: `mid` is the session's last trade close and the bid/ask spread is *estimated* as max(0.03, 0.04·mid) (E7.1 finding). Costs, slippage and the cost-sensitivity grid are therefore modelled, not observed.
- A trade close can be hours stale, so neighbouring strikes on one Alpaca EOD row set routinely break monotonicity and put-call parity (on sampled SPY/QQQ/IWM sessions, 13–65% of near-the-money strikes sat in a non-monotone pair). Every leg is therefore **re-marked from a same-session fitted IV smile** (volume-weighted quadratic in log-moneyness over OTM IVs, 3-MAD outlier trim, no extrapolation) for entries, daily marks and early exits alike. Expiry settles on the underlying close. This removes the stale-close noise but also any real skew kinks the quadratic cannot follow.

- **Stance** (which structures are on the menu) is a deterministic Director proxy: the 20-session trend label at the decision close (bull → bullish, bear → bearish, sideways → neutral) picks the profile's `stance_strategies`. `cash_debit` has no neutral structure, so it does not trade in sideways sessions. The live Director uses more information; its stance quality is outside this test. Every ranker sees the same stance-filtered menu.

- Daily EOD decisions and marks only: stops and take-profits are checked on closes, so intraday paths are not seen (matches the owner's relaxed, end-of-day stop preference).
- Menus hold one expiration (nearest the middle of the profile's DTE window) and a fixed delta grid, not the full live scanner menu; contracts with no trade that session are missing. ThetaData EOD was not used (no coverage in this store).
- At most one new position per ticker per session (open positions stack up to the gate's per-underlying and max-open caps), sized by D18 with no Risk persona (the equity cap binds).

## Verdict

| profile | verdict |
|---|---|
| margin | keep credit_width. Closest: ev_proxy (won 3 sub-periods, P&L diff 5,824, CI [-24,058, 36,180]) |
| cash_debit | keep debit_width. Closest: rorc_day_vrp (won 1 sub-periods, P&L diff -12,173, CI [-120,888, 95,671]) |

## Challengers vs incumbent

| profile | challenger | subperiods_won | won | pnl_diff | ci_lo | ci_hi | switch |
|---|---|---|---|---|---|---|---|
| margin | ev_proxy | 3 | bear,sideways,bull | 5,824 | -24,058 | 36,180 | False |
| margin | managed_net_ev | 1 | bull | 4,145 | -20,448 | 27,303 | False |
| margin | rorc_day | 2 | sideways,bull | 12,942 | -5,925 | 30,733 | False |
| margin | rorc_day_vrp | 2 | sideways,bull | 17,248 | -1,652 | 36,113 | False |
| cash_debit | ev_proxy | 0 | – | 34,252 | -14,057 | 83,511 | False |
| cash_debit | managed_net_ev | 0 | – | 71,721 | -15,631 | 165,558 | False |
| cash_debit | rorc_day | 0 | – | 17,655 | -23,970 | 50,786 | False |
| cash_debit | rorc_day_vrp | 1 | bear | -12,173 | -120,888 | 95,671 | False |

## Summary by ranker (default costs)

| profile | ranker | trades | net_pnl | cagr | max_dd | max_dd_pct | sharpe | sortino | win_rate | mean_managed_pop | realised_net_ev_unit | modelled_net_ev_unit | avg_days_held | turnover_per_month | cost_share_of_gross | hold_to_expiry_pnl |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| margin | credit_width | 190 | -22,040 | -0.092 | 44,312 | 0.428 | -0.390 | -0.317 | 0.726 | 0.735 | -3.348 | 12 | 15 | 6.165 | 93 | -23,347 |
| margin | ev_proxy | 201 | -16,215 | -0.067 | 36,864 | 0.354 | -0.373 | -0.300 | 0.766 | 0.785 | -3.301 | 14 | 14 | 6.522 | 139 | -16,242 |
| margin | managed_net_ev | 196 | -17,895 | -0.074 | 41,793 | 0.396 | -0.386 | -0.310 | 0.755 | 0.773 | -1.460 | 15 | 14 | 6.360 | 19 | -20,438 |
| margin | rorc_day | 194 | -9,098 | -0.036 | 34,805 | 0.331 | -0.136 | -0.109 | 0.763 | 0.771 | 9.245 | 15 | 14 | 6.295 | 1.952 | -7,564 |
| margin | rorc_day_vrp | 189 | -4,791 | -0.019 | 30,366 | 0.290 | -0.033 | -0.026 | 0.767 | 0.770 | 12 | 15 | 15 | 6.133 | 1.339 | -16,840 |
| cash_debit | debit_width | 166 | 33,685 | 0.120 | 56,135 | 0.475 | 0.470 | 0.532 | 0.476 | 0.460 | 166 | 46 | 13 | 5.387 | 0.648 | 143,154 |
| cash_debit | ev_proxy | 162 | 67,936 | 0.224 | 65,288 | 0.445 | 0.640 | 0.744 | 0.457 | 0.456 | 163 | 72 | 14 | 5.257 | 0.408 | 172,832 |
| cash_debit | managed_net_ev | 155 | 105,405 | 0.324 | 79,530 | 0.411 | 0.798 | 0.948 | 0.471 | 0.452 | 215 | 116 | 14 | 5.030 | 0.235 | 210,253 |
| cash_debit | rorc_day | 168 | 51,339 | 0.175 | 58,666 | 0.495 | 0.560 | 0.653 | 0.452 | 0.455 | 97 | 95 | 13 | 5.451 | 0.421 | 164,403 |
| cash_debit | rorc_day_vrp | 42 | 21,511 | 0.079 | 24,971 | 0.205 | 0.436 | 0.311 | 0.452 | 0.443 | 123 | 16 | 14 | 1.363 | 0.401 | 28,607 |

## Sub-periods (trend regime at entry)

| profile | ranker | subperiod | trades | net_pnl | max_dd |
|---|---|---|---|---|---|
| margin | credit_width | bear | 27 | -22,572 | 22,572 |
| margin | credit_width | sideways | 107 | 2,778 | 25,499 |
| margin | credit_width | bull | 56 | -2,245 | 10,728 |
| margin | ev_proxy | bear | 30 | -19,723 | 22,365 |
| margin | ev_proxy | sideways | 115 | 4,076 | 22,834 |
| margin | ev_proxy | bull | 56 | -569 | 8,985 |
| margin | managed_net_ev | bear | 31 | -25,452 | 27,643 |
| margin | managed_net_ev | sideways | 109 | 8,067 | 28,291 |
| margin | managed_net_ev | bull | 56 | -509 | 8,788 |
| margin | rorc_day | bear | 29 | -23,845 | 27,275 |
| margin | rorc_day | sideways | 109 | 15,085 | 22,668 |
| margin | rorc_day | bull | 56 | -337 | 9,142 |
| margin | rorc_day_vrp | bear | 26 | -23,111 | 25,218 |
| margin | rorc_day_vrp | sideways | 109 | 19,175 | 20,163 |
| margin | rorc_day_vrp | bull | 54 | -855 | 9,541 |
| cash_debit | debit_width | bear | 59 | -50,151 | 67,372 |
| cash_debit | debit_width | sideways | 0 | 0.000 | 0.000 |
| cash_debit | debit_width | bull | 107 | 83,836 | 27,947 |
| cash_debit | ev_proxy | bear | 54 | -47,967 | 76,342 |
| cash_debit | ev_proxy | sideways | 0 | 0.000 | 0.000 |
| cash_debit | ev_proxy | bull | 108 | 115,904 | 36,784 |
| cash_debit | managed_net_ev | bear | 52 | -49,327 | 97,131 |
| cash_debit | managed_net_ev | sideways | 0 | 0.000 | 0.000 |
| cash_debit | managed_net_ev | bull | 103 | 154,733 | 45,915 |
| cash_debit | rorc_day | bear | 57 | -51,260 | 72,143 |
| cash_debit | rorc_day | sideways | 0 | 0.000 | 0.000 |
| cash_debit | rorc_day | bull | 111 | 102,599 | 29,415 |
| cash_debit | rorc_day_vrp | bear | 19 | -9,537 | 16,143 |
| cash_debit | rorc_day_vrp | sideways | 0 | 0.000 | 0.000 |
| cash_debit | rorc_day_vrp | bull | 23 | 31,048 | 19,789 |

## Cost sensitivity (slippage x of the spread)

| profile | ranker | slippage | trades | net_pnl | max_dd | sharpe |
|---|---|---|---|---|---|---|
| margin | credit_width | 0.000 | 261 | 58,119 | 34,905 | 0.788 |
| margin | ev_proxy | 0.000 | 278 | 1,659 | 45,868 | 0.146 |
| margin | managed_net_ev | 0.000 | 275 | 53,854 | 32,073 | 0.810 |
| margin | rorc_day | 0.000 | 272 | 51,325 | 45,940 | 0.767 |
| margin | rorc_day_vrp | 0.000 | 260 | 25,948 | 42,630 | 0.485 |
| margin | credit_width | 0.250 | 190 | -22,040 | 44,312 | -0.390 |
| margin | ev_proxy | 0.250 | 201 | -16,215 | 36,864 | -0.373 |
| margin | managed_net_ev | 0.250 | 196 | -17,895 | 41,793 | -0.386 |
| margin | rorc_day | 0.250 | 194 | -9,098 | 34,805 | -0.136 |
| margin | rorc_day_vrp | 0.250 | 189 | -4,791 | 30,366 | -0.033 |
| margin | credit_width | 0.500 | 140 | -19,868 | 29,709 | -0.555 |
| margin | ev_proxy | 0.500 | 141 | -21,219 | 28,383 | -0.778 |
| margin | managed_net_ev | 0.500 | 141 | -17,870 | 27,345 | -0.552 |
| margin | rorc_day | 0.500 | 140 | -3,701 | 25,104 | -0.062 |
| margin | rorc_day_vrp | 0.500 | 139 | -6,686 | 27,618 | -0.158 |
| cash_debit | debit_width | 0.000 | 228 | 44,308 | 150,282 | 0.546 |
| cash_debit | ev_proxy | 0.000 | 205 | 32,838 | 101,378 | 0.483 |
| cash_debit | managed_net_ev | 0.000 | 195 | 2,118 | 72,129 | 0.295 |
| cash_debit | rorc_day | 0.000 | 217 | 40,050 | 126,110 | 0.521 |
| cash_debit | rorc_day_vrp | 0.000 | 115 | -26,672 | 52,045 | -0.027 |
| cash_debit | debit_width | 0.250 | 166 | 33,685 | 56,135 | 0.470 |
| cash_debit | ev_proxy | 0.250 | 162 | 67,936 | 65,288 | 0.640 |
| cash_debit | managed_net_ev | 0.250 | 155 | 105,405 | 79,530 | 0.798 |
| cash_debit | rorc_day | 0.250 | 168 | 51,339 | 58,666 | 0.560 |
| cash_debit | rorc_day_vrp | 0.250 | 42 | 21,511 | 24,971 | 0.436 |
| cash_debit | debit_width | 0.500 | 131 | 58,465 | 60,100 | 0.599 |
| cash_debit | ev_proxy | 0.500 | 132 | 30,768 | 57,042 | 0.449 |
| cash_debit | managed_net_ev | 0.500 | 130 | 67,267 | 54,431 | 0.654 |
| cash_debit | rorc_day | 0.500 | 138 | 19,298 | 54,035 | 0.379 |
| cash_debit | rorc_day_vrp | 0.500 | 17 | 12,885 | 17,131 | 0.400 |

## By structure

| profile | ranker | kind | trades | net_pnl | win_rate | mean_managed_pop | avg_days_held |
|---|---|---|---|---|---|---|---|
| margin | credit_width | bear_call | 27 | -22,572 | 0.481 | 0.751 | 13 |
| margin | credit_width | bull_put | 56 | -2,245 | 0.857 | 0.836 | 11 |
| margin | credit_width | iron_condor | 107 | 2,778 | 0.720 | 0.678 | 18 |
| margin | ev_proxy | bear_call | 30 | -19,723 | 0.633 | 0.756 | 12 |
| margin | ev_proxy | bull_put | 56 | -569 | 0.857 | 0.859 | 11 |
| margin | ev_proxy | iron_condor | 115 | 4,076 | 0.757 | 0.756 | 16 |
| margin | managed_net_ev | bear_call | 31 | -25,452 | 0.613 | 0.766 | 12 |
| margin | managed_net_ev | bull_put | 56 | -509 | 0.857 | 0.859 | 11 |
| margin | managed_net_ev | iron_condor | 109 | 8,067 | 0.743 | 0.730 | 17 |
| margin | rorc_day | bear_call | 29 | -23,845 | 0.621 | 0.767 | 12 |
| margin | rorc_day | bull_put | 56 | -337 | 0.857 | 0.859 | 11 |
| margin | rorc_day | iron_condor | 109 | 15,085 | 0.752 | 0.726 | 17 |
| margin | rorc_day_vrp | bear_call | 26 | -23,111 | 0.615 | 0.768 | 12 |
| margin | rorc_day_vrp | bull_put | 54 | -855 | 0.852 | 0.857 | 11 |
| margin | rorc_day_vrp | iron_condor | 109 | 19,175 | 0.761 | 0.727 | 17 |
| cash_debit | debit_width | bear_put | 47 | -62,233 | 0.340 | 0.496 | 13 |
| cash_debit | debit_width | bull_call | 28 | 23,945 | 0.607 | 0.475 | 12 |
| cash_debit | debit_width | long_call | 79 | 59,890 | 0.506 | 0.434 | 13 |
| cash_debit | debit_width | long_put | 12 | 12,082 | 0.500 | 0.452 | 14 |
| cash_debit | ev_proxy | bear_put | 25 | -56,742 | 0.320 | 0.487 | 15 |
| cash_debit | ev_proxy | bull_call | 19 | 18,849 | 0.579 | 0.483 | 11 |
| cash_debit | ev_proxy | long_call | 89 | 97,054 | 0.494 | 0.441 | 13 |
| cash_debit | ev_proxy | long_put | 29 | 8,774 | 0.379 | 0.460 | 16 |
| cash_debit | managed_net_ev | bear_put | 13 | -33,103 | 0.308 | 0.477 | 16 |
| cash_debit | managed_net_ev | bull_call | 3 | 22,393 | 1.000 | 0.471 | 9.667 |
| cash_debit | managed_net_ev | long_call | 100 | 132,339 | 0.510 | 0.442 | 13 |
| cash_debit | managed_net_ev | long_put | 39 | -16,224 | 0.385 | 0.469 | 17 |
| cash_debit | rorc_day | bear_put | 29 | -41,662 | 0.345 | 0.493 | 14 |
| cash_debit | rorc_day | bull_call | 6 | 16,445 | 0.667 | 0.474 | 9.333 |
| cash_debit | rorc_day | long_call | 105 | 86,154 | 0.486 | 0.443 | 13 |
| cash_debit | rorc_day | long_put | 28 | -9,599 | 0.393 | 0.455 | 14 |
| cash_debit | rorc_day_vrp | bear_put | 19 | -9,537 | 0.421 | 0.481 | 15 |
| cash_debit | rorc_day_vrp | long_call | 23 | 31,048 | 0.478 | 0.413 | 14 |

## Worst 10 trades per ranker

| profile | ranker | underlying | kind | entry_date | exit_date | contracts | pnl | exit_reason | managed_net_ev_unit | managed_pop | vrp | trend | move_sigma | root_cause | legs |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| margin | credit_width | IWM | iron_condor | 2024-11-01 | 2024-11-27 | 18 | -4,639 | stop | 20 | 0.688 | 0.093 | sideways | 1.364 | exit_management | +IWM241129P00207500 -IWM241129P00212000 +IWM241129C00232500 -IWM241129C00228000 |
| margin | credit_width | IWM | iron_condor | 2024-07-05 | 2024-07-16 | 19 | -4,478 | stop | 0.370 | 0.613 | 0.025 | sideways | 3.860 | regime_misread | +IWM240802P00193000 -IWM240802P00197000 +IWM240802C00211000 -IWM240802C00207000 |
| margin | credit_width | AAPL | iron_condor | 2024-04-26 | 2024-05-15 | 13 | -4,342 | stop | 12 | 0.723 | 0.055 | sideways | 1.872 | regime_misread | +AAPL240524P00155000 -AAPL240524P00160000 +AAPL240524C00185000 -AAPL240524C00180000 |
| margin | credit_width | QQQ | iron_condor | 2026-04-01 | 2026-04-15 | 6 | -4,261 | stop | 0.380 | 0.625 | 0.034 | sideways | 1.851 | regime_misread | +QQQ260424P00550000 -QQQ260424P00560000 +QQQ260424C00619000 -QQQ260424C00607000 |
| margin | credit_width | TSLA | iron_condor | 2026-03-17 | 2026-04-06 | 9 | -4,098 | stop | 9.820 | 0.620 | 0.097 | sideways | -1.274 | exit_management | +TSLA260410P00370000 -TSLA260410P00380000 +TSLA260410C00435000 -TSLA260410C00425000 |
| margin | credit_width | NVDA | bear_call | 2026-03-27 | 2026-04-14 | 11 | -4,081 | stop | 1.980 | 0.775 | 0.054 | bear | 1.824 | regime_misread | +NVDA260424C00185000 -NVDA260424C00180000 |
| margin | credit_width | IWM | bear_call | 2026-03-30 | 2026-04-13 | 12 | -3,878 | stop | 17 | 0.742 | 0.123 | bear | 1.534 | regime_misread | +IWM260424C00256000 -IWM260424C00251000 |
| margin | credit_width | SPY | iron_condor | 2024-12-06 | 2024-12-31 | 5 | -3,813 | expiry | 2.210 | 0.626 | 0.016 | sideways | -1.309 | strike_selection | +SPY241231P00589000 -SPY241231P00601000 +SPY241231C00630000 -SPY241231C00618000 |
| margin | credit_width | SPY | iron_condor | 2024-04-30 | 2024-05-15 | 7 | -3,810 | stop | 2.120 | 0.649 | 0.017 | sideways | 1.830 | regime_misread | +SPY240524P00480000 -SPY240524P00490000 +SPY240524C00526000 -SPY240524C00516000 |
| margin | credit_width | IWM | iron_condor | 2026-02-20 | 2026-03-19 | 17 | -3,675 | stop | 17 | 0.644 | 0.055 | sideways | -1.010 | exit_management | +IWM260320P00251000 -IWM260320P00256000 +IWM260320C00279000 -IWM260320C00274000 |
| margin | ev_proxy | IWM | iron_condor | 2024-07-05 | 2024-07-16 | 16 | -4,583 | stop | 4.690 | 0.778 | 0.025 | sideways | 3.860 | regime_misread | +IWM240802P00188000 -IWM240802P00192000 +IWM240802C00215000 -IWM240802C00211000 |
| margin | ev_proxy | NVDA | bear_call | 2026-03-27 | 2026-04-14 | 12 | -4,453 | stop | 1.980 | 0.775 | 0.054 | bear | 1.824 | regime_misread | +NVDA260424C00185000 -NVDA260424C00180000 |
| margin | ev_proxy | IWM | bear_call | 2026-03-30 | 2026-04-13 | 13 | -4,201 | stop | 17 | 0.742 | 0.123 | bear | 1.534 | regime_misread | +IWM260424C00256000 -IWM260424C00251000 |
| margin | ev_proxy | QQQ | iron_condor | 2026-04-02 | 2026-04-17 | 5 | -4,137 | stop | 5.560 | 0.744 | 0.035 | sideways | 2.129 | regime_misread | +QQQ260430P00530000 -QQQ260430P00540000 +QQQ260430C00631000 -QQQ260430C00619000 |
| margin | ev_proxy | SPY | bear_call | 2026-03-27 | 2026-04-13 | 5 | -4,097 | stop | 85 | 0.789 | 0.120 | bear | 1.393 | exit_management | +SPY260424C00672000 -SPY260424C00659000 |
| margin | ev_proxy | IWM | iron_condor | 2025-02-18 | 2025-03-14 | 10 | -4,057 | expiry | 4.040 | 0.749 | 0.024 | sideways | -2.389 | regime_misread | +IWM250314P00211000 -IWM250314P00216000 +IWM250314C00242500 -IWM250314C00238000 |
| margin | ev_proxy | SPY | iron_condor | 2024-07-12 | 2024-08-09 | 5 | -4,004 | expiry | 29 | 0.798 | 0.022 | sideways | -1.689 | regime_misread | +SPY240809P00532000 -SPY240809P00543000 +SPY240809C00585000 -SPY240809C00575000 |
| margin | ev_proxy | AAPL | bear_call | 2026-01-20 | 2026-02-04 | 10 | -3,629 | stop | 20 | 0.766 | 0.175 | bear | 1.703 | regime_misread | +AAPL260213C00265000 -AAPL260213C00260000 |
| margin | ev_proxy | QQQ | bear_call | 2026-07-28 | 2026-08-21 | 4 | -3,505 | expiry | 17 | 0.728 | 0.033 | bear | 0.807 | strike_selection | +QQQ260821C00715000 -QQQ260821C00701000 |
| margin | ev_proxy | SPY | bear_call | 2024-08-12 | 2024-08-30 | 5 | -3,485 | stop | 11 | 0.746 | 0.003 | bear | 1.404 | exit_management | +SPY240906C00557500 -SPY240906C00547000 |
| margin | managed_net_ev | AAPL | iron_condor | 2024-04-26 | 2024-05-15 | 14 | -4,676 | stop | 12 | 0.723 | 0.055 | sideways | 1.872 | regime_misread | +AAPL240524P00155000 -AAPL240524P00160000 +AAPL240524C00185000 -AAPL240524C00180000 |
| margin | managed_net_ev | IWM | iron_condor | 2024-11-01 | 2024-11-27 | 18 | -4,639 | stop | 20 | 0.688 | 0.093 | sideways | 1.364 | exit_management | +IWM241129P00207500 -IWM241129P00212000 +IWM241129C00232500 -IWM241129C00228000 |
| margin | managed_net_ev | IWM | iron_condor | 2024-07-05 | 2024-07-16 | 15 | -4,296 | stop | 4.690 | 0.778 | 0.025 | sideways | 3.860 | regime_misread | +IWM240802P00188000 -IWM240802P00192000 +IWM240802C00215000 -IWM240802C00211000 |
| margin | managed_net_ev | SPY | iron_condor | 2026-02-26 | 2026-03-20 | 5 | -4,149 | expiry | 61 | 0.687 | 0.046 | sideways | -1.515 | regime_misread | +SPY260320P00661000 -SPY260320P00675000 +SPY260320C00718000 -SPY260320C00704000 |
| margin | managed_net_ev | NVDA | bear_call | 2026-03-27 | 2026-04-14 | 11 | -4,081 | stop | 1.980 | 0.775 | 0.054 | bear | 1.824 | regime_misread | +NVDA260424C00185000 -NVDA260424C00180000 |
| margin | managed_net_ev | IWM | iron_condor | 2025-02-18 | 2025-03-14 | 10 | -4,057 | expiry | 4.040 | 0.749 | 0.024 | sideways | -2.389 | regime_misread | +IWM250314P00211000 -IWM250314P00216000 +IWM250314C00242500 -IWM250314C00238000 |
| margin | managed_net_ev | SPY | iron_condor | 2024-07-12 | 2024-08-09 | 5 | -3,896 | expiry | 30 | 0.723 | 0.022 | sideways | -1.689 | regime_misread | +SPY240809P00539000 -SPY240809P00550000 +SPY240809C00585000 -SPY240809C00572500 |
| margin | managed_net_ev | IWM | bear_call | 2026-03-30 | 2026-04-13 | 12 | -3,878 | stop | 17 | 0.742 | 0.123 | bear | 1.534 | regime_misread | +IWM260424C00256000 -IWM260424C00251000 |
| margin | managed_net_ev | AAPL | bear_call | 2026-03-30 | 2026-04-17 | 12 | -3,834 | stop | 13 | 0.758 | 0.111 | bear | 1.311 | exit_management | +AAPL260424C00265000 -AAPL260424C00260000 |
| margin | managed_net_ev | AAPL | bear_call | 2026-01-20 | 2026-02-04 | 10 | -3,629 | stop | 20 | 0.766 | 0.175 | bear | 1.703 | regime_misread | +AAPL260213C00265000 -AAPL260213C00260000 |
| margin | rorc_day | IWM | iron_condor | 2024-11-01 | 2024-11-27 | 20 | -5,154 | stop | 20 | 0.688 | 0.093 | sideways | 1.364 | exit_management | +IWM241129P00207500 -IWM241129P00212000 +IWM241129C00232500 -IWM241129C00228000 |
| margin | rorc_day | IWM | iron_condor | 2025-02-18 | 2025-03-14 | 11 | -4,462 | expiry | 4.040 | 0.749 | 0.024 | sideways | -2.389 | regime_misread | +IWM250314P00211000 -IWM250314P00216000 +IWM250314C00242500 -IWM250314C00238000 |
| margin | rorc_day | NVDA | bear_call | 2026-03-27 | 2026-04-14 | 12 | -4,453 | stop | 1.980 | 0.775 | 0.054 | bear | 1.824 | regime_misread | +NVDA260424C00185000 -NVDA260424C00180000 |
| margin | rorc_day | IWM | iron_condor | 2024-07-05 | 2024-07-16 | 15 | -4,296 | stop | 4.690 | 0.778 | 0.025 | sideways | 3.860 | regime_misread | +IWM240802P00188000 -IWM240802P00192000 +IWM240802C00215000 -IWM240802C00211000 |
| margin | rorc_day | IWM | bear_call | 2026-03-30 | 2026-04-13 | 13 | -4,201 | stop | 17 | 0.742 | 0.123 | bear | 1.534 | regime_misread | +IWM260424C00256000 -IWM260424C00251000 |
| margin | rorc_day | SPY | iron_condor | 2025-02-14 | 2025-03-11 | 4 | -4,170 | stop | 4.530 | 0.755 | 0.008 | sideways | -2.807 | regime_misread | +SPY250314P00575000 -SPY250314P00588000 +SPY250314C00640000 -SPY250314C00630000 |
| margin | rorc_day | SPY | iron_condor | 2026-02-26 | 2026-03-20 | 5 | -4,149 | expiry | 61 | 0.687 | 0.046 | sideways | -1.515 | regime_misread | +SPY260320P00661000 -SPY260320P00675000 +SPY260320C00718000 -SPY260320C00704000 |
| margin | rorc_day | SPY | bear_call | 2026-03-27 | 2026-04-13 | 5 | -4,097 | stop | 85 | 0.789 | 0.120 | bear | 1.393 | exit_management | +SPY260424C00672000 -SPY260424C00659000 |
| margin | rorc_day | AAPL | iron_condor | 2024-11-22 | 2024-12-13 | 14 | -4,012 | stop | 6.490 | 0.715 | 0.022 | sideways | 1.538 | regime_misread | +AAPL241220P00212500 -AAPL241220P00217500 +AAPL241220C00245000 -AAPL241220C00240000 |
| margin | rorc_day | AAPL | bear_call | 2026-01-20 | 2026-02-04 | 11 | -3,992 | stop | 20 | 0.766 | 0.175 | bear | 1.703 | regime_misread | +AAPL260213C00265000 -AAPL260213C00260000 |
| margin | rorc_day_vrp | IWM | iron_condor | 2024-11-01 | 2024-11-27 | 20 | -5,154 | stop | 20 | 0.688 | 0.093 | sideways | 1.364 | exit_management | +IWM241129P00207500 -IWM241129P00212000 +IWM241129C00232500 -IWM241129C00228000 |
| margin | rorc_day_vrp | SPY | iron_condor | 2026-02-26 | 2026-03-20 | 6 | -4,979 | expiry | 61 | 0.687 | 0.046 | sideways | -1.515 | regime_misread | +SPY260320P00661000 -SPY260320P00675000 +SPY260320C00718000 -SPY260320C00704000 |
| margin | rorc_day_vrp | NVDA | bear_call | 2026-03-27 | 2026-04-14 | 13 | -4,824 | stop | 1.980 | 0.775 | 0.054 | bear | 1.824 | regime_misread | +NVDA260424C00185000 -NVDA260424C00180000 |
| margin | rorc_day_vrp | IWM | bear_call | 2026-03-30 | 2026-04-13 | 14 | -4,524 | stop | 17 | 0.742 | 0.123 | bear | 1.534 | regime_misread | +IWM260424C00256000 -IWM260424C00251000 |
| margin | rorc_day_vrp | IWM | iron_condor | 2025-02-18 | 2025-03-14 | 11 | -4,462 | expiry | 4.040 | 0.749 | 0.024 | sideways | -2.389 | regime_misread | +IWM250314P00211000 -IWM250314P00216000 +IWM250314C00242500 -IWM250314C00238000 |
| margin | rorc_day_vrp | AAPL | bear_call | 2026-01-20 | 2026-02-04 | 12 | -4,355 | stop | 20 | 0.766 | 0.175 | bear | 1.703 | regime_misread | +AAPL260213C00265000 -AAPL260213C00260000 |
| margin | rorc_day_vrp | IWM | iron_condor | 2024-07-05 | 2024-07-16 | 15 | -4,296 | stop | 4.690 | 0.778 | 0.025 | sideways | 3.860 | regime_misread | +IWM240802P00188000 -IWM240802P00192000 +IWM240802C00215000 -IWM240802C00211000 |
| margin | rorc_day_vrp | QQQ | iron_condor | 2026-04-01 | 2026-04-15 | 6 | -4,215 | stop | 9.380 | 0.674 | 0.034 | sideways | 1.851 | regime_misread | +QQQ260424P00540000 -QQQ260424P00552000 +QQQ260424C00622500 -QQQ260424C00611000 |
| margin | rorc_day_vrp | SPY | iron_condor | 2025-02-14 | 2025-03-11 | 4 | -4,170 | stop | 4.530 | 0.755 | 0.008 | sideways | -2.807 | regime_misread | +SPY250314P00575000 -SPY250314P00588000 +SPY250314C00640000 -SPY250314C00630000 |
| margin | rorc_day_vrp | AAPL | bear_call | 2026-03-30 | 2026-04-17 | 13 | -4,153 | stop | 13 | 0.758 | 0.111 | bear | 1.311 | exit_management | +AAPL260424C00265000 -AAPL260424C00260000 |
| cash_debit | debit_width | TSLA | bear_put | 2026-07-23 | 2026-08-21 | 63 | -7,588 | expiry | 7.040 | 0.508 | -0.202 | bear | 0.920 | strike_selection | +TSLA260821P00290000 -TSLA260821P00285000 |
| cash_debit | debit_width | QQQ | bear_put | 2026-07-30 | 2026-08-28 | 27 | -7,522 | expiry | 6.890 | 0.456 | 0.002 | bear | 0.659 | strike_selection | +QQQ260828P00654000 -QQQ260828P00640000 |
| cash_debit | debit_width | NVDA | bear_put | 2026-06-26 | 2026-07-10 | 71 | -6,828 | stop | 7.440 | 0.469 | -0.067 | bear | 1.273 | exit_management | +NVDA260724P00180000 -NVDA260724P00175000 |
| cash_debit | debit_width | AAPL | bear_put | 2026-06-26 | 2026-07-02 | 78 | -6,811 | stop | 9.280 | 0.485 | -0.049 | bear | 2.537 | regime_misread | +AAPL260724P00270000 -AAPL260724P00265000 |
| cash_debit | debit_width | NVDA | long_call | 2026-05-21 | 2026-06-05 | 12 | -6,640 | stop | 6.160 | 0.395 | -0.020 | bull | -0.840 | exit_management | +NVDA260618C00228000 |
| cash_debit | debit_width | AAPL | long_call | 2026-07-16 | 2026-07-31 | 10 | -6,386 | stop | 50 | 0.425 | -0.032 | bull | -1.263 | exit_management | +AAPL260814C00345000 |
| cash_debit | debit_width | SPY | long_call | 2026-05-28 | 2026-06-05 | 10 | -6,107 | stop | 20 | 0.411 | 0.010 | bull | -1.151 | exit_management | +SPY260626C00764000 |
| cash_debit | debit_width | NVDA | long_call | 2026-07-22 | 2026-07-29 | 11 | -5,953 | stop | 5.970 | 0.401 | -0.000 | bull | -2.061 | regime_misread | +NVDA260821C00220000 |
| cash_debit | debit_width | IWM | long_call | 2026-05-27 | 2026-06-05 | 16 | -5,937 | stop | 11 | 0.410 | 0.016 | bull | -0.854 | exit_management | +IWM260626C00297000 |
| cash_debit | debit_width | TSLA | bear_put | 2025-07-24 | 2025-08-08 | 54 | -5,869 | stop | 1.590 | 0.486 | -0.113 | bear | 0.803 | exit_management | +TSLA250822P00280000 -TSLA250822P00275000 |
| cash_debit | ev_proxy | TSLA | bear_put | 2026-07-23 | 2026-08-21 | 83 | -9,996 | expiry | 7.040 | 0.508 | -0.202 | bear | 0.920 | strike_selection | +TSLA260821P00290000 -TSLA260821P00285000 |
| cash_debit | ev_proxy | AAPL | bear_put | 2026-06-26 | 2026-07-02 | 70 | -8,389 | stop | 10 | 0.491 | -0.049 | bear | 2.537 | regime_misread | +AAPL260724P00275000 -AAPL260724P00270000 |
| cash_debit | ev_proxy | AAPL | long_call | 2026-07-16 | 2026-07-31 | 13 | -8,302 | stop | 50 | 0.425 | -0.032 | bull | -1.263 | exit_management | +AAPL260814C00345000 |
| cash_debit | ev_proxy | NVDA | long_call | 2026-05-21 | 2026-06-05 | 15 | -8,300 | stop | 6.160 | 0.395 | -0.020 | bull | -0.840 | exit_management | +NVDA260618C00228000 |
| cash_debit | ev_proxy | NVDA | bear_put | 2026-06-26 | 2026-07-10 | 63 | -8,288 | stop | 6.440 | 0.473 | -0.067 | bear | 1.273 | exit_management | +NVDA260724P00185000 -NVDA260724P00180000 |
| cash_debit | ev_proxy | SPY | long_call | 2026-05-28 | 2026-06-05 | 13 | -7,939 | stop | 20 | 0.411 | 0.010 | bull | -1.151 | exit_management | +SPY260626C00764000 |
| cash_debit | ev_proxy | TSLA | bear_put | 2025-07-24 | 2025-08-08 | 70 | -7,609 | stop | 1.590 | 0.486 | -0.113 | bear | 0.803 | exit_management | +TSLA250822P00280000 -TSLA250822P00275000 |
| cash_debit | ev_proxy | NVDA | long_call | 2026-07-22 | 2026-07-29 | 14 | -7,576 | stop | 5.970 | 0.401 | -0.000 | bull | -2.061 | regime_misread | +NVDA260821C00220000 |
| cash_debit | ev_proxy | IWM | long_call | 2026-05-27 | 2026-06-05 | 20 | -7,422 | stop | 11 | 0.410 | 0.016 | bull | -0.854 | exit_management | +IWM260626C00297000 |
| cash_debit | ev_proxy | AAPL | long_call | 2025-09-22 | 2025-10-09 | 21 | -6,995 | stop | 21 | 0.411 | -0.019 | bull | -0.151 | exit_management | +AAPL251017C00262500 |
| cash_debit | managed_net_ev | TSLA | long_put | 2026-07-23 | 2026-08-21 | 5 | -11,468 | expiry | 321 | 0.483 | -0.202 | bear | 0.920 | strike_selection | +TSLA260821P00330000 |
| cash_debit | managed_net_ev | AAPL | long_call | 2026-07-16 | 2026-07-31 | 16 | -10,218 | stop | 50 | 0.425 | -0.032 | bull | -1.263 | exit_management | +AAPL260814C00345000 |
| cash_debit | managed_net_ev | NVDA | long_call | 2026-05-21 | 2026-06-05 | 18 | -9,960 | stop | 6.160 | 0.395 | -0.020 | bull | -0.840 | exit_management | +NVDA260618C00228000 |
| cash_debit | managed_net_ev | QQQ | long_put | 2026-07-20 | 2026-08-14 | 3 | -9,577 | expiry | 44 | 0.446 | 0.017 | bear | 0.715 | strike_selection | +QQQ260814P00722000 |
| cash_debit | managed_net_ev | NVDA | long_call | 2026-07-22 | 2026-07-29 | 17 | -9,200 | stop | 5.970 | 0.401 | -0.000 | bull | -2.061 | regime_misread | +NVDA260821C00220000 |
| cash_debit | managed_net_ev | SPY | long_call | 2026-05-28 | 2026-06-05 | 15 | -9,160 | stop | 20 | 0.411 | 0.010 | bull | -1.151 | exit_management | +SPY260626C00764000 |
| cash_debit | managed_net_ev | IWM | long_call | 2026-05-27 | 2026-06-05 | 24 | -8,906 | stop | 11 | 0.410 | 0.016 | bull | -0.854 | exit_management | +IWM260626C00297000 |
| cash_debit | managed_net_ev | TSLA | long_put | 2025-07-24 | 2025-08-11 | 5 | -8,815 | stop | 169 | 0.461 | -0.113 | bear | 1.001 | exit_management | +TSLA250822P00315000 |
| cash_debit | managed_net_ev | AAPL | long_call | 2025-09-22 | 2025-10-09 | 26 | -8,661 | stop | 21 | 0.411 | -0.019 | bull | -0.151 | exit_management | +AAPL251017C00262500 |
| cash_debit | managed_net_ev | IWM | bear_put | 2025-11-21 | 2025-12-03 | 56 | -8,436 | stop | 0.730 | 0.471 | 0.056 | bear | 1.226 | exit_management | +IWM251219P00235000 -IWM251219P00230000 |
| cash_debit | rorc_day | QQQ | bear_put | 2026-07-30 | 2026-08-28 | 23 | -8,589 | expiry | 22 | 0.483 | 0.002 | bear | 0.659 | strike_selection | +QQQ260828P00666000 -QQQ260828P00652000 |
| cash_debit | rorc_day | TSLA | long_put | 2026-07-23 | 2026-08-21 | 5 | -7,412 | expiry | 276 | 0.483 | -0.202 | bear | 0.920 | strike_selection | +TSLA260821P00315000 |
| cash_debit | rorc_day | NVDA | long_call | 2026-05-21 | 2026-06-05 | 13 | -7,193 | stop | 6.160 | 0.395 | -0.020 | bull | -0.840 | exit_management | +NVDA260618C00228000 |
| cash_debit | rorc_day | AAPL | long_call | 2026-07-16 | 2026-07-31 | 11 | -7,025 | stop | 50 | 0.425 | -0.032 | bull | -1.263 | exit_management | +AAPL260814C00345000 |
| cash_debit | rorc_day | AAPL | bear_put | 2026-06-26 | 2026-07-02 | 80 | -6,986 | stop | 9.280 | 0.485 | -0.049 | bear | 2.537 | regime_misread | +AAPL260724P00270000 -AAPL260724P00265000 |
| cash_debit | rorc_day | SPY | long_call | 2026-05-28 | 2026-06-05 | 11 | -6,717 | stop | 20 | 0.411 | 0.010 | bull | -1.151 | exit_management | +SPY260626C00764000 |
| cash_debit | rorc_day | NVDA | long_call | 2026-07-22 | 2026-07-29 | 12 | -6,494 | stop | 5.970 | 0.401 | -0.000 | bull | -2.061 | regime_misread | +NVDA260821C00220000 |
| cash_debit | rorc_day | IWM | long_call | 2026-05-27 | 2026-06-05 | 17 | -6,309 | stop | 11 | 0.410 | 0.016 | bull | -0.854 | exit_management | +IWM260626C00297000 |
| cash_debit | rorc_day | IWM | bear_put | 2025-04-09 | 2025-04-29 | 69 | -6,039 | stop | 12 | 0.532 | -0.050 | bear | 0.420 | exit_management | +IWM250509P00182000 -IWM250509P00178000 |
| cash_debit | rorc_day | TSLA | long_call | 2025-05-13 | 2025-06-05 | 4 | -6,034 | stop | 177 | 0.432 | -0.163 | bull | -1.057 | exit_management | +TSLA250613C00355000 |
| cash_debit | rorc_day_vrp | QQQ | bear_put | 2026-07-30 | 2026-08-28 | 17 | -6,348 | expiry | 22 | 0.483 | 0.002 | bear | 0.659 | strike_selection | +QQQ260828P00666000 -QQQ260828P00652000 |
| cash_debit | rorc_day_vrp | SPY | long_call | 2026-05-28 | 2026-06-05 | 9 | -5,496 | stop | 20 | 0.411 | 0.010 | bull | -1.151 | exit_management | +SPY260626C00764000 |
| cash_debit | rorc_day_vrp | AAPL | long_call | 2026-07-24 | 2026-07-31 | 8 | -5,207 | stop | 0.440 | 0.393 | 0.001 | bull | -1.848 | regime_misread | +AAPL260821C00342500 |
| cash_debit | rorc_day_vrp | IWM | long_call | 2026-05-27 | 2026-06-05 | 14 | -5,195 | stop | 11 | 0.410 | 0.016 | bull | -0.854 | exit_management | +IWM260626C00297000 |
| cash_debit | rorc_day_vrp | QQQ | bear_put | 2024-08-08 | 2024-08-20 | 20 | -5,065 | stop | 12 | 0.493 | 0.015 | bear | 1.434 | exit_management | +QQQ240906P00441000 -QQQ240906P00432500 |
| cash_debit | rorc_day_vrp | IWM | bear_put | 2025-04-10 | 2025-04-24 | 39 | -4,742 | stop | 5.560 | 0.525 | 0.032 | bear | 0.774 | exit_management | +IWM250509P00179000 -IWM250509P00175000 |
| cash_debit | rorc_day_vrp | AAPL | long_call | 2024-12-23 | 2025-01-02 | 18 | -4,666 | stop | 19 | 0.427 | 0.021 | bull | -1.512 | regime_misread | +AAPL250117C00260000 |
| cash_debit | rorc_day_vrp | AAPL | bear_put | 2024-08-06 | 2024-08-13 | 41 | -4,522 | stop | 2.790 | 0.467 | 0.023 | bear | 1.575 | regime_misread | +AAPL240906P00200000 -AAPL240906P00195000 |
| cash_debit | rorc_day_vrp | IWM | bear_put | 2025-11-21 | 2025-12-03 | 29 | -4,369 | stop | 0.730 | 0.471 | 0.056 | bear | 1.226 | exit_management | +IWM251219P00235000 -IWM251219P00230000 |
| cash_debit | rorc_day_vrp | SPY | bear_put | 2024-08-05 | 2024-08-13 | 16 | -4,332 | stop | 15 | 0.497 | 0.140 | bear | 1.040 | exit_management | +SPY240830P00514000 -SPY240830P00504000 |
