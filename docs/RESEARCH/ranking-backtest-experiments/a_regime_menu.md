# Ranking backtest run (E7.5)

Tickers: SPY, QQQ, IWM, AAPL, NVDA, TSLA · entries 2024-03-01 → 2026-07-31 · profiles: margin, cash_debit · equity $100,000 · MC paths 5000 · cost x=0.25, est. spread max(0.03, 0.04·mid)

Decision sessions with a menu per ticker: SPY 606, QQQ 606, IWM 606, AAPL 606, NVDA 606, TSLA 606

Mean menu size: margin 2.1, cash_debit 2.3

Entry DTE windows: margin 30–45, cash_debit 30–60

Regime-conditional menu (backtest.stance_menus): margin: bull → none, bear → none, sideways → iron_condor; cash_debit: bull → bull_call/long_call, bear → long_put, sideways → none

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
| margin | keep credit_width. Closest: rorc_day (won 1 sub-periods, P&L diff 14,059, CI [-11,795, 42,411]) |
| cash_debit | keep debit_width. Closest: ev_proxy (won 1 sub-periods, P&L diff 18,614, CI [-7,273, 45,205]) |

## Challengers vs incumbent

| profile | challenger | subperiods_won | won | pnl_diff | ci_lo | ci_hi | switch |
|---|---|---|---|---|---|---|---|
| margin | ev_proxy | 1 | sideways | 7,092 | -23,605 | 42,319 | False |
| margin | managed_net_ev | 1 | sideways | 9,666 | -17,857 | 38,557 | False |
| margin | rorc_day | 1 | sideways | 14,059 | -11,795 | 42,411 | False |
| margin | rorc_day_vrp | 1 | sideways | 14,059 | -11,795 | 42,411 | False |
| cash_debit | ev_proxy | 1 | bull | 18,614 | -7,273 | 45,205 | False |
| cash_debit | managed_net_ev | 1 | bull | 24,786 | -9,947 | 68,404 | False |
| cash_debit | rorc_day | 0 | – | 19,873 | -25,474 | 67,123 | False |
| cash_debit | rorc_day_vrp | 1 | bear | 34,879 | -52,719 | 127,322 | False |

## Summary by ranker (default costs)

| profile | ranker | trades | net_pnl | cagr | max_dd | max_dd_pct | sharpe | sortino | win_rate | mean_managed_pop | realised_net_ev_unit | modelled_net_ev_unit | avg_days_held | turnover_per_month | cost_share_of_gross | hold_to_expiry_pnl |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| margin | credit_width | 92 | -16,654 | -0.068 | 42,715 | 0.409 | -0.331 | -0.262 | 0.641 | 0.689 | -7.885 | 18 | 26 | 2.985 | 11 | -9,825 |
| margin | ev_proxy | 103 | -9,562 | -0.038 | 33,295 | 0.317 | -0.276 | -0.203 | 0.767 | 0.791 | 2.070 | 21 | 23 | 3.342 | 7.463 | 2,709 |
| margin | managed_net_ev | 97 | -6,988 | -0.028 | 32,420 | 0.305 | -0.121 | -0.087 | 0.722 | 0.758 | 0.459 | 21 | 24 | 3.148 | 2.249 | -840 |
| margin | rorc_day | 95 | -2,595 | -0.010 | 34,550 | 0.325 | -0.007 | -0.006 | 0.726 | 0.751 | 11 | 20 | 24 | 3.083 | 1.241 | 25,841 |
| margin | rorc_day_vrp | 95 | -2,595 | -0.010 | 34,550 | 0.325 | -0.007 | -0.006 | 0.726 | 0.751 | 11 | 20 | 24 | 3.083 | 1.241 | 25,841 |
| cash_debit | debit_width | 104 | -21,358 | -0.089 | 77,311 | 0.549 | -0.096 | -0.099 | 0.385 | 0.461 | -2.163 | 92 | 21 | 3.375 | 9.363 | -29,521 |
| cash_debit | ev_proxy | 108 | -2,744 | -0.011 | 70,432 | 0.473 | 0.143 | 0.154 | 0.407 | 0.463 | -18 | 108 | 21 | 3.505 | 1.153 | -40,373 |
| cash_debit | managed_net_ev | 103 | 3,428 | 0.013 | 51,463 | 0.403 | 0.208 | 0.224 | 0.417 | 0.454 | 52 | 135 | 22 | 3.342 | 0.747 | -4,197 |
| cash_debit | rorc_day | 113 | -1,485 | -0.006 | 84,014 | 0.544 | 0.178 | 0.201 | 0.416 | 0.457 | 157 | 135 | 21 | 3.667 | 1.146 | -718 |
| cash_debit | rorc_day_vrp | 30 | 13,521 | 0.051 | 31,921 | 0.219 | 0.357 | 0.291 | 0.433 | 0.430 | 113 | 37 | 23 | 0.973 | 0.197 | 40,340 |

## Sub-periods (trend regime at entry)

| profile | ranker | subperiod | trades | net_pnl | max_dd |
|---|---|---|---|---|---|
| margin | credit_width | bear | 0 | 0.000 | 0.000 |
| margin | credit_width | sideways | 92 | -16,654 | 41,184 |
| margin | credit_width | bull | 0 | 0.000 | 0.000 |
| margin | ev_proxy | bear | 0 | 0.000 | 0.000 |
| margin | ev_proxy | sideways | 103 | -9,562 | 33,793 |
| margin | ev_proxy | bull | 0 | 0.000 | 0.000 |
| margin | managed_net_ev | bear | 0 | 0.000 | 0.000 |
| margin | managed_net_ev | sideways | 97 | -6,988 | 33,217 |
| margin | managed_net_ev | bull | 0 | 0.000 | 0.000 |
| margin | rorc_day | bear | 0 | 0.000 | 0.000 |
| margin | rorc_day | sideways | 95 | -2,595 | 35,403 |
| margin | rorc_day | bull | 0 | 0.000 | 0.000 |
| margin | rorc_day_vrp | bear | 0 | 0.000 | 0.000 |
| margin | rorc_day_vrp | sideways | 95 | -2,595 | 35,403 |
| margin | rorc_day_vrp | bull | 0 | 0.000 | 0.000 |
| cash_debit | debit_width | bear | 27 | -41,951 | 46,543 |
| cash_debit | debit_width | sideways | 0 | 0.000 | 0.000 |
| cash_debit | debit_width | bull | 77 | 20,593 | 44,005 |
| cash_debit | ev_proxy | bear | 29 | -46,859 | 56,430 |
| cash_debit | ev_proxy | sideways | 0 | 0.000 | 0.000 |
| cash_debit | ev_proxy | bull | 79 | 44,115 | 42,693 |
| cash_debit | managed_net_ev | bear | 31 | -45,805 | 45,805 |
| cash_debit | managed_net_ev | sideways | 0 | 0.000 | 0.000 |
| cash_debit | managed_net_ev | bull | 72 | 49,233 | 32,619 |
| cash_debit | rorc_day | bear | 34 | -44,704 | 59,574 |
| cash_debit | rorc_day | sideways | 0 | 0.000 | 0.000 |
| cash_debit | rorc_day | bull | 79 | 43,218 | 47,806 |
| cash_debit | rorc_day_vrp | bear | 5 | -618 | 6,910 |
| cash_debit | rorc_day_vrp | sideways | 0 | 0.000 | 0.000 |
| cash_debit | rorc_day_vrp | bull | 25 | 14,138 | 25,591 |

## Cost sensitivity (slippage x of the spread)

| profile | ranker | slippage | trades | net_pnl | max_dd | sharpe |
|---|---|---|---|---|---|---|
| margin | credit_width | 0.000 | 117 | 46,680 | 43,598 | 0.785 |
| margin | ev_proxy | 0.000 | 137 | 10,261 | 34,986 | 0.325 |
| margin | managed_net_ev | 0.000 | 122 | 16,977 | 38,937 | 0.406 |
| margin | rorc_day | 0.000 | 120 | 49,199 | 36,695 | 0.844 |
| margin | rorc_day_vrp | 0.000 | 120 | 49,199 | 36,695 | 0.844 |
| margin | credit_width | 0.250 | 92 | -16,654 | 42,715 | -0.331 |
| margin | ev_proxy | 0.250 | 103 | -9,562 | 33,295 | -0.276 |
| margin | managed_net_ev | 0.250 | 97 | -6,988 | 32,420 | -0.121 |
| margin | rorc_day | 0.250 | 95 | -2,595 | 34,550 | -0.007 |
| margin | rorc_day_vrp | 0.250 | 95 | -2,595 | 34,550 | -0.007 |
| margin | credit_width | 0.500 | 72 | -17,540 | 35,082 | -0.594 |
| margin | ev_proxy | 0.500 | 75 | -2,636 | 23,140 | -0.072 |
| margin | managed_net_ev | 0.500 | 72 | -6,003 | 29,120 | -0.188 |
| margin | rorc_day | 0.500 | 73 | -5,825 | 28,952 | -0.157 |
| margin | rorc_day_vrp | 0.500 | 73 | -5,825 | 28,952 | -0.157 |
| cash_debit | debit_width | 0.000 | 136 | 32,214 | 86,339 | 0.464 |
| cash_debit | ev_proxy | 0.000 | 132 | 53,081 | 65,996 | 0.604 |
| cash_debit | managed_net_ev | 0.000 | 128 | 33,896 | 68,793 | 0.481 |
| cash_debit | rorc_day | 0.000 | 140 | 51,116 | 90,222 | 0.580 |
| cash_debit | rorc_day_vrp | 0.000 | 69 | 80,778 | 69,112 | 0.906 |
| cash_debit | debit_width | 0.250 | 104 | -21,358 | 77,311 | -0.096 |
| cash_debit | ev_proxy | 0.250 | 108 | -2,744 | 70,432 | 0.143 |
| cash_debit | managed_net_ev | 0.250 | 103 | 3,428 | 51,463 | 0.208 |
| cash_debit | rorc_day | 0.250 | 113 | -1,485 | 84,014 | 0.178 |
| cash_debit | rorc_day_vrp | 0.250 | 30 | 13,521 | 31,921 | 0.357 |
| cash_debit | debit_width | 0.500 | 97 | -13,945 | 61,349 | -0.008 |
| cash_debit | ev_proxy | 0.500 | 102 | 13,206 | 65,011 | 0.310 |
| cash_debit | managed_net_ev | 0.500 | 94 | -6,395 | 55,494 | 0.083 |
| cash_debit | rorc_day | 0.500 | 102 | -3,752 | 63,190 | 0.143 |
| cash_debit | rorc_day_vrp | 0.500 | 19 | 6,694 | 18,938 | 0.252 |

## By structure

| profile | ranker | kind | trades | net_pnl | win_rate | mean_managed_pop | avg_days_held |
|---|---|---|---|---|---|---|---|
| margin | credit_width | iron_condor | 92 | -16,654 | 0.641 | 0.689 | 26 |
| margin | ev_proxy | iron_condor | 103 | -9,562 | 0.767 | 0.791 | 23 |
| margin | managed_net_ev | iron_condor | 97 | -6,988 | 0.722 | 0.758 | 24 |
| margin | rorc_day | iron_condor | 95 | -2,595 | 0.726 | 0.751 | 24 |
| margin | rorc_day_vrp | iron_condor | 95 | -2,595 | 0.726 | 0.751 | 24 |
| cash_debit | debit_width | bull_call | 20 | -12,749 | 0.400 | 0.496 | 16 |
| cash_debit | debit_width | long_call | 57 | 33,341 | 0.491 | 0.446 | 20 |
| cash_debit | debit_width | long_put | 27 | -41,951 | 0.148 | 0.467 | 28 |
| cash_debit | ev_proxy | bull_call | 15 | 1,415 | 0.533 | 0.506 | 15 |
| cash_debit | ev_proxy | long_call | 64 | 42,700 | 0.484 | 0.449 | 20 |
| cash_debit | ev_proxy | long_put | 29 | -46,859 | 0.172 | 0.470 | 27 |
| cash_debit | managed_net_ev | bull_call | 2 | 9,645 | 1.000 | 0.464 | 20 |
| cash_debit | managed_net_ev | long_call | 70 | 39,587 | 0.486 | 0.448 | 21 |
| cash_debit | managed_net_ev | long_put | 31 | -45,805 | 0.226 | 0.467 | 26 |
| cash_debit | rorc_day | bull_call | 3 | 15,652 | 1.000 | 0.477 | 15 |
| cash_debit | rorc_day | long_call | 76 | 27,566 | 0.474 | 0.455 | 20 |
| cash_debit | rorc_day | long_put | 34 | -44,704 | 0.235 | 0.460 | 23 |
| cash_debit | rorc_day_vrp | long_call | 25 | 14,138 | 0.480 | 0.426 | 22 |
| cash_debit | rorc_day_vrp | long_put | 5 | -618 | 0.200 | 0.454 | 29 |

## Worst 10 trades per ranker

| profile | ranker | underlying | kind | entry_date | exit_date | contracts | pnl | exit_reason | managed_net_ev_unit | managed_pop | vrp | trend | move_sigma | root_cause | legs |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| margin | credit_width | QQQ | iron_condor | 2024-07-12 | 2024-08-05 | 8 | -4,493 | stop | 4.400 | 0.597 | 0.023 | sideways | -3.021 | regime_misread | +QQQ240816P00475000 -QQQ240816P00485000 +QQQ240816C00520000 -QQQ240816C00510000 |
| margin | credit_width | IWM | iron_condor | 2024-07-03 | 2024-07-16 | 19 | -4,342 | stop | 1.320 | 0.669 | 0.023 | sideways | 3.323 | regime_misread | +IWM240809P00191000 -IWM240809P00195000 +IWM240809C00214000 -IWM240809C00210000 |
| margin | credit_width | IWM | iron_condor | 2024-10-25 | 2024-11-22 | 20 | -4,030 | stop | 7.900 | 0.634 | 0.066 | sideways | 1.276 | exit_management | +IWM241129P00207000 -IWM241129P00211000 +IWM241129C00233000 -IWM241129C00229000 |
| margin | credit_width | SPY | iron_condor | 2024-12-05 | 2025-01-10 | 5 | -3,961 | expiry | 6.540 | 0.634 | 0.017 | sideways | -1.281 | strike_selection | +SPY250110P00585000 -SPY250110P00597500 +SPY250110C00630000 -SPY250110C00619000 |
| margin | credit_width | AAPL | iron_condor | 2025-03-05 | 2025-04-04 | 10 | -3,905 | stop | 0.830 | 0.758 | 0.038 | sideways | -2.880 | regime_misread | +AAPL250411P00210000 -AAPL250411P00215000 +AAPL250411C00260000 -AAPL250411C00255000 |
| margin | credit_width | QQQ | iron_condor | 2025-02-25 | 2025-04-04 | 7 | -3,846 | expiry | 9.440 | 0.619 | 0.036 | sideways | -2.917 | regime_misread | +QQQ250404P00485000 -QQQ250404P00495000 +QQQ250404C00543000 -QQQ250404C00533000 |
| margin | credit_width | AAPL | iron_condor | 2024-11-12 | 2024-12-09 | 14 | -3,775 | stop | 2.000 | 0.653 | 0.027 | sideways | 1.663 | regime_misread | +AAPL241220P00210000 -AAPL241220P00215000 +AAPL241220C00240000 -AAPL241220C00235000 |
| margin | credit_width | IWM | iron_condor | 2025-02-18 | 2025-03-28 | 13 | -3,745 | expiry | 3.620 | 0.632 | 0.033 | sideways | -2.009 | regime_misread | +IWM250328P00215000 -IWM250328P00220000 +IWM250328C00241000 -IWM250328C00236000 |
| margin | credit_width | SPY | iron_condor | 2026-03-31 | 2026-04-17 | 5 | -3,683 | stop | 25 | 0.626 | 0.045 | sideways | 1.918 | regime_misread | +SPY260508P00615000 -SPY260508P00629000 +SPY260508C00690000 -SPY260508C00675000 |
| margin | credit_width | QQQ | iron_condor | 2026-03-31 | 2026-05-08 | 5 | -3,652 | expiry | 8.010 | 0.656 | 0.043 | sideways | 2.594 | regime_misread | +QQQ260508P00534000 -QQQ260508P00546000 +QQQ260508C00620000 -QQQ260508C00608000 |
| margin | ev_proxy | AAPL | iron_condor | 2024-04-26 | 2024-05-31 | 12 | -4,998 | expiry | 7.150 | 0.823 | 0.038 | sideways | 1.646 | regime_misread | +AAPL240531P00150000 -AAPL240531P00155000 +AAPL240531C00190000 -AAPL240531C00185000 |
| margin | ev_proxy | QQQ | iron_condor | 2024-06-03 | 2024-07-05 | 8 | -4,313 | stop | 26 | 0.762 | 0.035 | sideways | 1.929 | regime_misread | +QQQ240712P00425000 -QQQ240712P00433000 +QQQ240712C00480000 -QQQ240712C00472500 |
| margin | ev_proxy | AAPL | iron_condor | 2025-03-05 | 2025-04-04 | 11 | -4,295 | stop | 0.830 | 0.758 | 0.038 | sideways | -2.880 | regime_misread | +AAPL250411P00210000 -AAPL250411P00215000 +AAPL250411C00260000 -AAPL250411C00255000 |
| margin | ev_proxy | IWM | iron_condor | 2025-02-18 | 2025-03-26 | 14 | -4,164 | stop | 4.070 | 0.786 | 0.033 | sideways | -1.647 | regime_misread | +IWM250328P00209000 -IWM250328P00213000 +IWM250328C00245000 -IWM250328C00242000 |
| margin | ev_proxy | QQQ | iron_condor | 2024-07-12 | 2024-08-05 | 6 | -3,926 | stop | 17 | 0.780 | 0.023 | sideways | -3.021 | regime_misread | +QQQ240816P00460000 -QQQ240816P00470000 +QQQ240816C00530000 -QQQ240816C00520000 |
| margin | ev_proxy | QQQ | iron_condor | 2025-02-25 | 2025-04-04 | 5 | -3,850 | expiry | 19 | 0.795 | 0.036 | sideways | -2.917 | regime_misread | +QQQ250404P00465000 -QQQ250404P00475000 +QQQ250404C00555000 -QQQ250404C00545000 |
| margin | ev_proxy | SPY | iron_condor | 2025-02-14 | 2025-03-21 | 4 | -3,796 | expiry | 59 | 0.846 | 0.047 | sideways | -1.527 | regime_misread | +SPY250321P00569000 -SPY250321P00581000 +SPY250321C00655000 -SPY250321C00644000 |
| margin | ev_proxy | AAPL | iron_condor | 2025-08-04 | 2025-09-12 | 10 | -3,792 | expiry | 32 | 0.862 | 0.087 | sideways | 1.637 | regime_misread | +AAPL250912P00180000 -AAPL250912P00185000 +AAPL250912C00225000 -AAPL250912C00220000 |
| margin | ev_proxy | AAPL | iron_condor | 2024-11-12 | 2024-12-13 | 12 | -3,744 | stop | 6.490 | 0.775 | 0.027 | sideways | 1.642 | regime_misread | +AAPL241220P00205000 -AAPL241220P00210000 +AAPL241220C00245000 -AAPL241220C00240000 |
| margin | ev_proxy | IWM | iron_condor | 2026-04-02 | 2026-05-01 | 12 | -3,435 | dte_exit | 2.130 | 0.737 | 0.046 | sideways | 1.355 | strike_selection | +IWM260508P00225000 -IWM260508P00230000 +IWM260508C00276000 -IWM260508C00271000 |
| margin | managed_net_ev | AAPL | iron_condor | 2024-04-26 | 2024-05-31 | 12 | -4,998 | expiry | 7.150 | 0.823 | 0.038 | sideways | 1.646 | regime_misread | +AAPL240531P00150000 -AAPL240531P00155000 +AAPL240531C00190000 -AAPL240531C00185000 |
| margin | managed_net_ev | AAPL | iron_condor | 2025-03-05 | 2025-04-04 | 12 | -4,686 | stop | 0.830 | 0.758 | 0.038 | sideways | -2.880 | regime_misread | +AAPL250411P00210000 -AAPL250411P00215000 +AAPL250411C00260000 -AAPL250411C00255000 |
| margin | managed_net_ev | SPY | iron_condor | 2025-02-24 | 2025-04-04 | 4 | -4,475 | expiry | 38 | 0.818 | 0.034 | sideways | -3.248 | regime_misread | +SPY250404P00550000 -SPY250404P00564000 +SPY250404C00640000 -SPY250404C00626000 |
| margin | managed_net_ev | QQQ | iron_condor | 2024-07-12 | 2024-08-05 | 7 | -4,368 | stop | 19 | 0.721 | 0.023 | sideways | -3.021 | regime_misread | +QQQ240816P00465000 -QQQ240816P00475000 +QQQ240816C00525000 -QQQ240816C00515000 |
| margin | managed_net_ev | IWM | iron_condor | 2025-02-18 | 2025-03-28 | 12 | -4,334 | expiry | 6.030 | 0.737 | 0.033 | sideways | -2.009 | regime_misread | +IWM250328P00211000 -IWM250328P00216000 +IWM250328C00245000 -IWM250328C00240000 |
| margin | managed_net_ev | SPY | iron_condor | 2026-02-20 | 2026-03-30 | 5 | -4,254 | stop | 67 | 0.680 | 0.047 | sideways | -1.685 | regime_misread | +SPY260331P00658000 -SPY260331P00672000 +SPY260331C00723000 -SPY260331C00709000 |
| margin | managed_net_ev | QQQ | iron_condor | 2024-06-03 | 2024-07-03 | 6 | -3,993 | stop | 29 | 0.805 | 0.035 | sideways | 1.764 | regime_misread | +QQQ240712P00420000 -QQQ240712P00430000 +QQQ240712C00485000 -QQQ240712C00475000 |
| margin | managed_net_ev | QQQ | iron_condor | 2025-02-25 | 2025-04-04 | 5 | -3,850 | expiry | 19 | 0.795 | 0.036 | sideways | -2.917 | regime_misread | +QQQ250404P00465000 -QQQ250404P00475000 +QQQ250404C00555000 -QQQ250404C00545000 |
| margin | managed_net_ev | AAPL | iron_condor | 2025-08-04 | 2025-09-12 | 10 | -3,792 | expiry | 32 | 0.862 | 0.087 | sideways | 1.637 | regime_misread | +AAPL250912P00180000 -AAPL250912P00185000 +AAPL250912C00225000 -AAPL250912C00220000 |
| margin | managed_net_ev | AAPL | iron_condor | 2024-11-12 | 2024-12-13 | 12 | -3,744 | stop | 6.490 | 0.775 | 0.027 | sideways | 1.642 | regime_misread | +AAPL241220P00205000 -AAPL241220P00210000 +AAPL241220C00245000 -AAPL241220C00240000 |
| margin | rorc_day | AAPL | iron_condor | 2024-04-26 | 2024-05-31 | 12 | -4,998 | expiry | 7.150 | 0.823 | 0.038 | sideways | 1.646 | regime_misread | +AAPL240531P00150000 -AAPL240531P00155000 +AAPL240531C00190000 -AAPL240531C00185000 |
| margin | rorc_day | QQQ | iron_condor | 2024-07-12 | 2024-08-05 | 7 | -4,368 | stop | 19 | 0.721 | 0.023 | sideways | -3.021 | regime_misread | +QQQ240816P00465000 -QQQ240816P00475000 +QQQ240816C00525000 -QQQ240816C00515000 |
| margin | rorc_day | IWM | iron_condor | 2025-02-18 | 2025-03-28 | 12 | -4,334 | expiry | 6.030 | 0.737 | 0.033 | sideways | -2.009 | regime_misread | +IWM250328P00211000 -IWM250328P00216000 +IWM250328C00245000 -IWM250328C00240000 |
| margin | rorc_day | QQQ | iron_condor | 2024-06-03 | 2024-07-05 | 8 | -4,313 | stop | 26 | 0.762 | 0.035 | sideways | 1.929 | regime_misread | +QQQ240712P00425000 -QQQ240712P00433000 +QQQ240712C00480000 -QQQ240712C00472500 |
| margin | rorc_day | AAPL | iron_condor | 2025-03-05 | 2025-04-04 | 11 | -4,295 | stop | 0.830 | 0.758 | 0.038 | sideways | -2.880 | regime_misread | +AAPL250411P00210000 -AAPL250411P00215000 +AAPL250411C00260000 -AAPL250411C00255000 |
| margin | rorc_day | SPY | iron_condor | 2025-02-10 | 2025-03-21 | 6 | -3,926 | expiry | 49 | 0.663 | 0.038 | sideways | -1.325 | strike_selection | +SPY250321P00579000 -SPY250321P00591000 +SPY250321C00636000 -SPY250321C00624000 |
| margin | rorc_day | QQQ | iron_condor | 2025-02-25 | 2025-04-04 | 5 | -3,850 | expiry | 19 | 0.795 | 0.036 | sideways | -2.917 | regime_misread | +QQQ250404P00465000 -QQQ250404P00475000 +QQQ250404C00555000 -QQQ250404C00545000 |
| margin | rorc_day | AAPL | iron_condor | 2024-11-12 | 2024-12-13 | 12 | -3,744 | stop | 6.490 | 0.775 | 0.027 | sideways | 1.642 | regime_misread | +AAPL241220P00205000 -AAPL241220P00210000 +AAPL241220C00245000 -AAPL241220C00240000 |
| margin | rorc_day | AAPL | iron_condor | 2025-08-04 | 2025-08-13 | 13 | -3,300 | stop | 31 | 0.712 | 0.087 | sideways | 3.331 | regime_misread | +AAPL250912P00190000 -AAPL250912P00195000 +AAPL250912C00220000 -AAPL250912C00215000 |
| margin | rorc_day | SPY | iron_condor | 2024-09-06 | 2024-10-04 | 6 | -3,152 | dte_exit | 8.140 | 0.730 | 0.026 | sideways | 1.179 | strike_selection | +SPY241011P00500000 -SPY241011P00510000 +SPY241011C00574000 -SPY241011C00563000 |
| margin | rorc_day_vrp | AAPL | iron_condor | 2024-04-26 | 2024-05-31 | 12 | -4,998 | expiry | 7.150 | 0.823 | 0.038 | sideways | 1.646 | regime_misread | +AAPL240531P00150000 -AAPL240531P00155000 +AAPL240531C00190000 -AAPL240531C00185000 |
| margin | rorc_day_vrp | QQQ | iron_condor | 2024-07-12 | 2024-08-05 | 7 | -4,368 | stop | 19 | 0.721 | 0.023 | sideways | -3.021 | regime_misread | +QQQ240816P00465000 -QQQ240816P00475000 +QQQ240816C00525000 -QQQ240816C00515000 |
| margin | rorc_day_vrp | IWM | iron_condor | 2025-02-18 | 2025-03-28 | 12 | -4,334 | expiry | 6.030 | 0.737 | 0.033 | sideways | -2.009 | regime_misread | +IWM250328P00211000 -IWM250328P00216000 +IWM250328C00245000 -IWM250328C00240000 |
| margin | rorc_day_vrp | QQQ | iron_condor | 2024-06-03 | 2024-07-05 | 8 | -4,313 | stop | 26 | 0.762 | 0.035 | sideways | 1.929 | regime_misread | +QQQ240712P00425000 -QQQ240712P00433000 +QQQ240712C00480000 -QQQ240712C00472500 |
| margin | rorc_day_vrp | AAPL | iron_condor | 2025-03-05 | 2025-04-04 | 11 | -4,295 | stop | 0.830 | 0.758 | 0.038 | sideways | -2.880 | regime_misread | +AAPL250411P00210000 -AAPL250411P00215000 +AAPL250411C00260000 -AAPL250411C00255000 |
| margin | rorc_day_vrp | SPY | iron_condor | 2025-02-10 | 2025-03-21 | 6 | -3,926 | expiry | 49 | 0.663 | 0.038 | sideways | -1.325 | strike_selection | +SPY250321P00579000 -SPY250321P00591000 +SPY250321C00636000 -SPY250321C00624000 |
| margin | rorc_day_vrp | QQQ | iron_condor | 2025-02-25 | 2025-04-04 | 5 | -3,850 | expiry | 19 | 0.795 | 0.036 | sideways | -2.917 | regime_misread | +QQQ250404P00465000 -QQQ250404P00475000 +QQQ250404C00555000 -QQQ250404C00545000 |
| margin | rorc_day_vrp | AAPL | iron_condor | 2024-11-12 | 2024-12-13 | 12 | -3,744 | stop | 6.490 | 0.775 | 0.027 | sideways | 1.642 | regime_misread | +AAPL241220P00205000 -AAPL241220P00210000 +AAPL241220C00245000 -AAPL241220C00240000 |
| margin | rorc_day_vrp | AAPL | iron_condor | 2025-08-04 | 2025-08-13 | 13 | -3,300 | stop | 31 | 0.712 | 0.087 | sideways | 3.331 | regime_misread | +AAPL250912P00190000 -AAPL250912P00195000 +AAPL250912C00220000 -AAPL250912C00215000 |
| margin | rorc_day_vrp | SPY | iron_condor | 2024-09-06 | 2024-10-04 | 6 | -3,152 | dte_exit | 8.140 | 0.730 | 0.026 | sideways | 1.179 | strike_selection | +SPY241011P00500000 -SPY241011P00510000 +SPY241011C00574000 -SPY241011C00563000 |
| cash_debit | debit_width | AAPL | bull_call | 2024-07-09 | 2024-08-02 | 76 | -5,385 | stop | 8.770 | 0.488 | -0.065 | bull | -0.649 | exit_management | +AAPL240823C00245000 -AAPL240823C00250000 |
| cash_debit | debit_width | TSLA | bull_call | 2024-07-24 | 2024-08-07 | 64 | -5,253 | stop | 5.820 | 0.503 | -0.233 | bull | -1.202 | exit_management | +TSLA240830C00245000 -TSLA240830C00250000 |
| cash_debit | debit_width | TSLA | long_call | 2024-07-11 | 2024-07-24 | 3 | -5,061 | stop | 24 | 0.418 | -0.009 | bull | -0.944 | exit_management | +TSLA240823C00240000 |
| cash_debit | debit_width | NVDA | long_call | 2024-06-04 | 2024-07-19 | 1 | -4,685 | expiry | 377 | 0.434 | -0.067 | bull | -15 | regime_misread | +NVDA240719C01230000 |
| cash_debit | debit_width | QQQ | long_put | 2024-08-08 | 2024-08-19 | 2 | -4,202 | stop | 16 | 0.446 | 0.005 | bear | 1.608 | regime_misread | +QQQ240920P00470000 |
| cash_debit | debit_width | AAPL | long_put | 2024-08-09 | 2024-08-29 | 6 | -4,198 | stop | 18 | 0.435 | -0.013 | bear | 0.989 | exit_management | +AAPL240920P00220000 |
| cash_debit | debit_width | IWM | bull_call | 2024-03-01 | 2024-04-02 | 53 | -4,196 | stop | 0.330 | 0.444 | -0.037 | bull | -0.106 | exit_management | +IWM240412C00216000 -IWM240412C00220000 |
| cash_debit | debit_width | NVDA | bull_call | 2024-08-23 | 2024-08-29 | 65 | -4,171 | stop | 32 | 0.509 | -2.157 | bull | -1.193 | exit_management | +NVDA241004C00155000 -NVDA241004C00160000 |
| cash_debit | debit_width | NVDA | bull_call | 2024-03-20 | 2024-04-08 | 14 | -4,103 | stop | 21 | 0.508 | -0.202 | bull | -0.334 | exit_management | +NVDA240426C01030000 -NVDA240426C01050000 |
| cash_debit | debit_width | IWM | long_put | 2024-08-12 | 2024-08-30 | 4 | -4,075 | stop | 43 | 0.461 | -0.020 | bear | 1.327 | exit_management | +IWM240927P00216000 |
| cash_debit | ev_proxy | AAPL | bull_call | 2024-07-09 | 2024-08-02 | 80 | -5,668 | stop | 8.770 | 0.488 | -0.065 | bull | -0.649 | exit_management | +AAPL240823C00245000 -AAPL240823C00250000 |
| cash_debit | ev_proxy | TSLA | bull_call | 2024-07-24 | 2024-08-07 | 68 | -5,581 | stop | 5.820 | 0.503 | -0.233 | bull | -1.202 | exit_management | +TSLA240830C00245000 -TSLA240830C00250000 |
| cash_debit | ev_proxy | NVDA | bull_call | 2024-08-19 | 2024-08-29 | 81 | -5,251 | stop | 36 | 0.513 | -2.189 | bull | -1.000 | exit_management | +NVDA240927C00155000 -NVDA240927C00160000 |
| cash_debit | ev_proxy | TSLA | long_put | 2026-07-23 | 2026-09-04 | 2 | -5,157 | expiry | 352 | 0.484 | -0.216 | bear | 0.628 | strike_selection | +TSLA260904P00330000 |
| cash_debit | ev_proxy | SPY | long_call | 2026-05-08 | 2026-06-10 | 6 | -5,142 | stop | 7.680 | 0.412 | 0.025 | bull | -0.363 | exit_management | +SPY260618C00751000 |
| cash_debit | ev_proxy | TSLA | long_call | 2024-07-11 | 2024-07-24 | 3 | -5,061 | stop | 24 | 0.418 | -0.009 | bull | -0.944 | exit_management | +TSLA240823C00240000 |
| cash_debit | ev_proxy | TSLA | long_put | 2024-08-07 | 2024-09-05 | 2 | -5,035 | stop | 152 | 0.476 | -0.116 | bear | 1.125 | exit_management | +TSLA240920P00215000 |
| cash_debit | ev_proxy | AAPL | long_put | 2024-08-09 | 2024-08-29 | 7 | -4,897 | stop | 18 | 0.435 | -0.013 | bear | 0.989 | exit_management | +AAPL240920P00220000 |
| cash_debit | ev_proxy | NVDA | long_call | 2024-06-03 | 2024-07-19 | 1 | -4,705 | expiry | 314 | 0.425 | -0.069 | bull | -15 | regime_misread | +NVDA240719C01220000 |
| cash_debit | ev_proxy | SPY | long_put | 2024-08-12 | 2024-08-23 | 3 | -4,603 | stop | 159 | 0.477 | -0.004 | bear | 1.778 | regime_misread | +SPY240927P00550000 |
| cash_debit | managed_net_ev | NVDA | long_call | 2026-05-21 | 2026-06-10 | 8 | -5,207 | stop | 28 | 0.414 | -0.036 | bull | -1.016 | exit_management | +NVDA260702C00230000 |
| cash_debit | managed_net_ev | TSLA | long_put | 2026-07-23 | 2026-09-04 | 2 | -5,157 | expiry | 352 | 0.484 | -0.216 | bear | 0.628 | strike_selection | +TSLA260904P00330000 |
| cash_debit | managed_net_ev | SPY | long_call | 2026-05-08 | 2026-06-10 | 6 | -5,142 | stop | 7.680 | 0.412 | 0.025 | bull | -0.363 | exit_management | +SPY260618C00751000 |
| cash_debit | managed_net_ev | AAPL | long_call | 2025-07-03 | 2025-08-01 | 7 | -4,640 | stop | 65 | 0.449 | -0.048 | bull | -0.720 | exit_management | +AAPL250815C00215000 |
| cash_debit | managed_net_ev | AAPL | long_put | 2026-06-29 | 2026-07-06 | 4 | -4,604 | stop | 22 | 0.434 | -0.019 | bear | 2.609 | regime_misread | +AAPL260807P00290000 |
| cash_debit | managed_net_ev | IWM | long_call | 2026-07-07 | 2026-07-29 | 11 | -4,601 | stop | 9.840 | 0.418 | 0.032 | bull | -0.511 | exit_management | +IWM260821C00305000 |
| cash_debit | managed_net_ev | AAPL | long_call | 2024-07-16 | 2024-07-25 | 7 | -4,580 | stop | 8.890 | 0.418 | -0.008 | bull | -2.013 | regime_misread | +AAPL240830C00235000 |
| cash_debit | managed_net_ev | TSLA | long_call | 2024-07-24 | 2024-08-07 | 4 | -4,539 | stop | 278 | 0.484 | -0.233 | bull | -1.202 | exit_management | +TSLA240830C00215000 |
| cash_debit | managed_net_ev | AAPL | long_call | 2026-07-16 | 2026-07-31 | 4 | -4,510 | stop | 103 | 0.448 | -0.047 | bull | -1.330 | exit_management | +AAPL260828C00335000 |
| cash_debit | managed_net_ev | AAPL | long_call | 2024-08-29 | 2024-09-16 | 9 | -4,470 | stop | 35 | 0.435 | -0.017 | bull | -1.155 | exit_management | +AAPL241011C00235000 |
| cash_debit | rorc_day | AAPL | long_call | 2024-07-10 | 2024-07-24 | 14 | -5,967 | stop | 58 | 0.448 | -0.052 | bull | -1.344 | exit_management | +AAPL240823C00240000 |
| cash_debit | rorc_day | TSLA | long_call | 2024-07-24 | 2024-08-07 | 8 | -5,778 | stop | 216 | 0.488 | -0.233 | bull | -1.202 | exit_management | +TSLA240830C00230000 |
| cash_debit | rorc_day | IWM | long_put | 2024-08-12 | 2024-08-23 | 8 | -5,561 | stop | 28 | 0.442 | -0.020 | bear | 1.729 | regime_misread | +IWM240927P00209000 |
| cash_debit | rorc_day | NVDA | long_put | 2024-07-25 | 2024-08-19 | 9 | -5,457 | stop | 519 | 0.595 | -2.011 | bear | 0.896 | exit_management | +NVDA240906P00109000 |
| cash_debit | rorc_day | TSLA | long_put | 2024-08-07 | 2024-08-16 | 6 | -5,335 | stop | 88 | 0.444 | -0.116 | bear | 1.322 | exit_management | +TSLA240920P00185000 |
| cash_debit | rorc_day | TSLA | long_put | 2024-08-16 | 2024-09-13 | 6 | -5,262 | stop | 210 | 0.492 | -0.208 | bear | 0.464 | exit_management | +TSLA240927P00210000 |
| cash_debit | rorc_day | SPY | long_call | 2026-05-08 | 2026-06-10 | 6 | -5,142 | stop | 7.680 | 0.412 | 0.025 | bull | -0.363 | exit_management | +SPY260618C00751000 |
| cash_debit | rorc_day | TSLA | long_call | 2024-07-11 | 2024-07-24 | 3 | -5,061 | stop | 24 | 0.418 | -0.009 | bull | -0.944 | exit_management | +TSLA240823C00240000 |
| cash_debit | rorc_day | NVDA | long_call | 2024-08-19 | 2024-08-29 | 9 | -4,984 | stop | 492 | 0.493 | -2.189 | bull | -1.000 | exit_management | +NVDA240927C00140000 |
| cash_debit | rorc_day | AAPL | long_put | 2024-08-09 | 2024-08-29 | 7 | -4,897 | stop | 18 | 0.435 | -0.013 | bear | 0.989 | exit_management | +AAPL240920P00220000 |
| cash_debit | rorc_day_vrp | SPY | long_call | 2026-05-08 | 2026-06-10 | 7 | -6,000 | stop | 7.680 | 0.412 | 0.025 | bull | -0.363 | exit_management | +SPY260618C00751000 |
| cash_debit | rorc_day_vrp | AAPL | long_call | 2026-07-27 | 2026-07-31 | 8 | -5,467 | stop | 22 | 0.413 | 0.004 | bull | -2.850 | regime_misread | +AAPL260904C00350000 |
| cash_debit | rorc_day_vrp | IWM | long_call | 2025-07-02 | 2025-08-01 | 13 | -5,181 | stop | 2.520 | 0.407 | 0.011 | bull | -0.438 | exit_management | +IWM250815C00227000 |
| cash_debit | rorc_day_vrp | IWM | long_call | 2026-07-07 | 2026-07-29 | 12 | -5,020 | stop | 9.840 | 0.418 | 0.032 | bull | -0.511 | exit_management | +IWM260821C00305000 |
| cash_debit | rorc_day_vrp | IWM | long_call | 2026-05-28 | 2026-07-06 | 12 | -4,731 | dte_exit | 27 | 0.429 | 0.010 | bull | 0.327 | strike_selection | +IWM260710C00300000 |
| cash_debit | rorc_day_vrp | QQQ | long_call | 2025-12-19 | 2026-01-20 | 6 | -4,617 | stop | 101 | 0.457 | 0.001 | bull | -0.285 | exit_management | +QQQ260130C00630000 |
| cash_debit | rorc_day_vrp | AAPL | long_call | 2024-10-14 | 2024-11-01 | 12 | -4,376 | stop | 20 | 0.421 | 0.004 | bull | -0.694 | exit_management | +AAPL241129C00240000 |
| cash_debit | rorc_day_vrp | IWM | long_call | 2026-04-20 | 2026-05-18 | 10 | -4,374 | stop | 15 | 0.418 | 0.014 | bull | -0.077 | exit_management | +IWM260529C00284000 |
| cash_debit | rorc_day_vrp | SPY | long_call | 2024-11-29 | 2024-12-18 | 9 | -4,267 | stop | 88 | 0.476 | 0.000 | bull | -1.019 | exit_management | +SPY250110C00612000 |
| cash_debit | rorc_day_vrp | QQQ | long_put | 2024-08-08 | 2024-08-19 | 2 | -4,202 | stop | 16 | 0.446 | 0.005 | bear | 1.608 | regime_misread | +QQQ240920P00470000 |
