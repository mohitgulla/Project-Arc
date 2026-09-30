# Ranking backtest run (E7.5)

Tickers: SPY, QQQ, IWM, AAPL, NVDA, TSLA · entries 2024-03-01 → 2026-07-31 · profiles: margin, cash_debit · equity $100,000 · MC paths 5000 · cost x=0.25, est. spread max(0.03, 0.04·mid)

Decision sessions with a menu per ticker: SPY 606, QQQ 606, IWM 606, AAPL 606, NVDA 606, TSLA 606

Mean menu size: margin 3.7, cash_debit 2.9

Entry DTE windows: margin 30–45, cash_debit 30–60

Net EV ÷ est. cost filter: ≥ 1

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
| margin | switch candidate: ev_proxy (won 2 sub-periods, CI [824, 40,399]) |
| cash_debit | keep debit_width. Closest: ev_proxy (won 1 sub-periods, P&L diff 44,451, CI [4,356, 92,768]) |

## Challengers vs incumbent

| profile | challenger | subperiods_won | won | pnl_diff | ci_lo | ci_hi | switch |
|---|---|---|---|---|---|---|---|
| margin | ev_proxy | 2 | sideways,bull | 17,973 | 824 | 40,399 | True |
| margin | managed_net_ev | 2 | sideways,bull | 5,072 | -7,340 | 18,410 | False |
| margin | rorc_day | 0 | – | -589 | -6,107 | 4,873 | False |
| margin | rorc_day_vrp | 0 | – | -6,944 | -16,524 | 1,607 | False |
| cash_debit | ev_proxy | 1 | bear | 44,451 | 4,356 | 92,768 | False |
| cash_debit | managed_net_ev | 1 | bear | 4,809 | -23,556 | 50,179 | False |
| cash_debit | rorc_day | 1 | bear | 11,554 | -18,123 | 46,701 | False |
| cash_debit | rorc_day_vrp | 1 | bear | 11,857 | -86,472 | 122,388 | False |

## Summary by ranker (default costs)

| profile | ranker | trades | net_pnl | cagr | max_dd | max_dd_pct | sharpe | sortino | win_rate | mean_managed_pop | realised_net_ev_unit | modelled_net_ev_unit | avg_days_held | turnover_per_month | cost_share_of_gross | hold_to_expiry_pnl |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| margin | credit_width | 92 | -17,675 | -0.073 | 34,906 | 0.325 | -0.514 | -0.369 | 0.739 | 0.790 | -21 | 26 | 22 | 2.985 | 1.985 | -15,910 |
| margin | ev_proxy | 95 | 298 | 0.001 | 28,380 | 0.265 | 0.065 | 0.047 | 0.811 | 0.829 | 17 | 25 | 21 | 3.083 | 0.967 | -11,336 |
| margin | managed_net_ev | 94 | -12,604 | -0.051 | 31,615 | 0.293 | -0.339 | -0.237 | 0.745 | 0.806 | -17 | 28 | 22 | 3.050 | 5.945 | -18,635 |
| margin | rorc_day | 93 | -18,265 | -0.076 | 37,467 | 0.348 | -0.518 | -0.377 | 0.731 | 0.796 | -17 | 26 | 22 | 3.018 | 1.748 | -6,296 |
| margin | rorc_day_vrp | 92 | -24,619 | -0.104 | 39,400 | 0.373 | -0.765 | -0.560 | 0.717 | 0.795 | -28 | 27 | 22 | 2.985 | 0.821 | -17,674 |
| cash_debit | debit_width | 101 | -5,787 | -0.023 | 61,823 | 0.437 | 0.113 | 0.121 | 0.436 | 0.474 | 231 | 124 | 20 | 3.277 | 1.441 | -14,415 |
| cash_debit | ev_proxy | 99 | 38,664 | 0.136 | 67,066 | 0.423 | 0.528 | 0.617 | 0.444 | 0.465 | 191 | 152 | 21 | 3.212 | 0.271 | 23,623 |
| cash_debit | managed_net_ev | 92 | -978 | -0.004 | 48,569 | 0.411 | 0.152 | 0.168 | 0.424 | 0.463 | 92 | 162 | 22 | 2.985 | 1.143 | -3,991 |
| cash_debit | rorc_day | 102 | 5,767 | 0.022 | 74,494 | 0.519 | 0.251 | 0.292 | 0.431 | 0.470 | 189 | 146 | 20 | 3.310 | 0.687 | 2,326 |
| cash_debit | rorc_day_vrp | 17 | 6,070 | 0.023 | 14,896 | 0.128 | 0.240 | 0.165 | 0.471 | 0.444 | 126 | 51 | 23 | 0.552 | 0.310 | 1,682 |

## Sub-periods (trend regime at entry)

| profile | ranker | subperiod | trades | net_pnl | max_dd |
|---|---|---|---|---|---|
| margin | credit_width | bear | 7 | -12,110 | 13,331 |
| margin | credit_width | sideways | 64 | -1,525 | 19,926 |
| margin | credit_width | bull | 21 | -4,040 | 7,604 |
| margin | ev_proxy | bear | 7 | -13,125 | 14,346 |
| margin | ev_proxy | sideways | 67 | 17,269 | 15,030 |
| margin | ev_proxy | bull | 21 | -3,846 | 7,604 |
| margin | managed_net_ev | bear | 8 | -16,237 | 17,459 |
| margin | managed_net_ev | sideways | 65 | 7,645 | 15,899 |
| margin | managed_net_ev | bull | 21 | -4,011 | 7,604 |
| margin | rorc_day | bear | 8 | -14,995 | 16,216 |
| margin | rorc_day | sideways | 64 | 854 | 21,669 |
| margin | rorc_day | bull | 21 | -4,124 | 7,604 |
| margin | rorc_day_vrp | bear | 8 | -14,489 | 15,557 |
| margin | rorc_day_vrp | sideways | 64 | -5,772 | 25,657 |
| margin | rorc_day_vrp | bull | 20 | -4,359 | 7,287 |
| cash_debit | debit_width | bear | 31 | -62,472 | 62,472 |
| cash_debit | debit_width | sideways | 0 | 0.000 | 0.000 |
| cash_debit | debit_width | bull | 70 | 56,685 | 31,783 |
| cash_debit | ev_proxy | bear | 29 | -39,007 | 48,243 |
| cash_debit | ev_proxy | sideways | 0 | 0.000 | 0.000 |
| cash_debit | ev_proxy | bull | 70 | 77,671 | 42,057 |
| cash_debit | managed_net_ev | bear | 29 | -39,791 | 39,791 |
| cash_debit | managed_net_ev | sideways | 0 | 0.000 | 0.000 |
| cash_debit | managed_net_ev | bull | 63 | 38,814 | 26,337 |
| cash_debit | rorc_day | bear | 31 | -50,270 | 55,392 |
| cash_debit | rorc_day | sideways | 0 | 0.000 | 0.000 |
| cash_debit | rorc_day | bull | 71 | 56,038 | 37,608 |
| cash_debit | rorc_day_vrp | bear | 1 | 5,236 | 0.000 |
| cash_debit | rorc_day_vrp | sideways | 0 | 0.000 | 0.000 |
| cash_debit | rorc_day_vrp | bull | 16 | 834 | 18,954 |

## Cost sensitivity (slippage x of the spread)

| profile | ranker | slippage | trades | net_pnl | max_dd | sharpe |
|---|---|---|---|---|---|---|
| margin | credit_width | 0.000 | 191 | 13,709 | 36,249 | 0.329 |
| margin | ev_proxy | 0.000 | 207 | -13,413 | 37,149 | -0.188 |
| margin | managed_net_ev | 0.000 | 204 | -15,885 | 35,987 | -0.208 |
| margin | rorc_day | 0.000 | 195 | 13,841 | 31,399 | 0.344 |
| margin | rorc_day_vrp | 0.000 | 188 | 20,565 | 30,013 | 0.459 |
| margin | credit_width | 0.250 | 92 | -17,675 | 34,906 | -0.514 |
| margin | ev_proxy | 0.250 | 95 | 298 | 28,380 | 0.065 |
| margin | managed_net_ev | 0.250 | 94 | -12,604 | 31,615 | -0.339 |
| margin | rorc_day | 0.250 | 93 | -18,265 | 37,467 | -0.518 |
| margin | rorc_day_vrp | 0.250 | 92 | -24,619 | 39,400 | -0.765 |
| margin | credit_width | 0.500 | 28 | -13,425 | 18,205 | -0.833 |
| margin | ev_proxy | 0.500 | 27 | -8,156 | 11,995 | -0.602 |
| margin | managed_net_ev | 0.500 | 27 | -3,617 | 8,792 | -0.232 |
| margin | rorc_day | 0.500 | 28 | -13,825 | 18,205 | -0.851 |
| margin | rorc_day_vrp | 0.500 | 28 | -13,825 | 18,205 | -0.851 |
| cash_debit | debit_width | 0.000 | 171 | -26,370 | 94,475 | 0.058 |
| cash_debit | ev_proxy | 0.000 | 156 | -16,771 | 75,664 | 0.097 |
| cash_debit | managed_net_ev | 0.000 | 145 | -33,190 | 61,636 | -0.114 |
| cash_debit | rorc_day | 0.000 | 165 | -33,289 | 89,511 | -0.036 |
| cash_debit | rorc_day_vrp | 0.000 | 96 | -19,870 | 76,011 | -0.005 |
| cash_debit | debit_width | 0.250 | 101 | -5,787 | 61,823 | 0.113 |
| cash_debit | ev_proxy | 0.250 | 99 | 38,664 | 67,066 | 0.528 |
| cash_debit | managed_net_ev | 0.250 | 92 | -978 | 48,569 | 0.152 |
| cash_debit | rorc_day | 0.250 | 102 | 5,767 | 74,494 | 0.251 |
| cash_debit | rorc_day_vrp | 0.250 | 17 | 6,070 | 14,896 | 0.240 |
| cash_debit | debit_width | 0.500 | 91 | 12,524 | 54,914 | 0.302 |
| cash_debit | ev_proxy | 0.500 | 93 | 5,952 | 60,790 | 0.226 |
| cash_debit | managed_net_ev | 0.500 | 86 | 27,063 | 54,912 | 0.461 |
| cash_debit | rorc_day | 0.500 | 96 | 9,471 | 63,274 | 0.270 |
| cash_debit | rorc_day_vrp | 0.500 | 10 | 12,037 | 13,087 | 0.518 |

## By structure

| profile | ranker | kind | trades | net_pnl | win_rate | mean_managed_pop | avg_days_held |
|---|---|---|---|---|---|---|---|
| margin | credit_width | bear_call | 7 | -12,110 | 0.286 | 0.787 | 22 |
| margin | credit_width | bull_put | 21 | -4,040 | 0.905 | 0.893 | 13 |
| margin | credit_width | iron_condor | 64 | -1,525 | 0.734 | 0.756 | 24 |
| margin | ev_proxy | bear_call | 7 | -13,125 | 0.286 | 0.787 | 22 |
| margin | ev_proxy | bull_put | 21 | -3,846 | 0.905 | 0.895 | 13 |
| margin | ev_proxy | iron_condor | 67 | 17,269 | 0.836 | 0.812 | 24 |
| margin | managed_net_ev | bear_call | 8 | -16,237 | 0.250 | 0.789 | 21 |
| margin | managed_net_ev | bull_put | 21 | -4,011 | 0.905 | 0.895 | 13 |
| margin | managed_net_ev | iron_condor | 65 | 7,645 | 0.754 | 0.779 | 25 |
| margin | rorc_day | bear_call | 8 | -14,995 | 0.250 | 0.786 | 22 |
| margin | rorc_day | bull_put | 21 | -4,124 | 0.905 | 0.895 | 13 |
| margin | rorc_day | iron_condor | 64 | 854 | 0.734 | 0.765 | 24 |
| margin | rorc_day_vrp | bear_call | 8 | -14,489 | 0.250 | 0.786 | 22 |
| margin | rorc_day_vrp | bull_put | 20 | -4,359 | 0.900 | 0.894 | 13 |
| margin | rorc_day_vrp | iron_condor | 64 | -5,772 | 0.719 | 0.765 | 25 |
| cash_debit | debit_width | bear_put | 10 | -27,100 | 0.200 | 0.573 | 21 |
| cash_debit | debit_width | bull_call | 7 | 16,415 | 0.714 | 0.496 | 11 |
| cash_debit | debit_width | long_call | 63 | 40,270 | 0.508 | 0.460 | 20 |
| cash_debit | debit_width | long_put | 21 | -35,372 | 0.238 | 0.464 | 24 |
| cash_debit | ev_proxy | bear_put | 1 | -7,769 | 0.000 | 0.618 | 43 |
| cash_debit | ev_proxy | bull_call | 4 | 7,717 | 0.750 | 0.504 | 9.500 |
| cash_debit | ev_proxy | long_call | 66 | 69,954 | 0.515 | 0.456 | 20 |
| cash_debit | ev_proxy | long_put | 28 | -31,238 | 0.250 | 0.475 | 24 |
| cash_debit | managed_net_ev | bear_put | 1 | -5,315 | 0.000 | 0.618 | 43 |
| cash_debit | managed_net_ev | long_call | 63 | 38,814 | 0.508 | 0.455 | 21 |
| cash_debit | managed_net_ev | long_put | 28 | -34,476 | 0.250 | 0.474 | 25 |
| cash_debit | rorc_day | bear_put | 7 | -22,219 | 0.143 | 0.577 | 21 |
| cash_debit | rorc_day | bull_call | 2 | 9,313 | 1.000 | 0.488 | 10 |
| cash_debit | rorc_day | long_call | 69 | 46,725 | 0.507 | 0.461 | 20 |
| cash_debit | rorc_day | long_put | 24 | -28,051 | 0.250 | 0.463 | 22 |
| cash_debit | rorc_day_vrp | bear_put | 1 | 5,236 | 1.000 | 0.540 | 20 |
| cash_debit | rorc_day_vrp | long_call | 16 | 834 | 0.438 | 0.438 | 23 |

## Worst 10 trades per ranker

| profile | ranker | underlying | kind | entry_date | exit_date | contracts | pnl | exit_reason | managed_net_ev_unit | managed_pop | vrp | trend | move_sigma | root_cause | legs |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| margin | credit_width | QQQ | iron_condor | 2025-02-25 | 2025-04-04 | 6 | -4,620 | expiry | 19 | 0.795 | 0.036 | sideways | -2.917 | regime_misread | +QQQ250404P00465000 -QQQ250404P00475000 +QQQ250404C00555000 -QQQ250404C00545000 |
| margin | credit_width | SPY | iron_condor | 2025-02-10 | 2025-03-21 | 7 | -4,580 | expiry | 49 | 0.663 | 0.038 | sideways | -1.325 | strike_selection | +SPY250321P00579000 -SPY250321P00591000 +SPY250321C00636000 -SPY250321C00624000 |
| margin | credit_width | AAPL | bull_put | 2024-12-26 | 2025-01-21 | 11 | -4,514 | stop | 12 | 0.899 | 0.079 | bull | -2.456 | regime_misread | +AAPL250131P00235000 -AAPL250131P00240000 |
| margin | credit_width | QQQ | bull_put | 2024-07-05 | 2024-08-02 | 5 | -3,921 | stop | 8.530 | 0.878 | 0.028 | bull | -2.295 | regime_misread | +QQQ240809P00462500 -QQQ240809P00472500 |
| margin | credit_width | IWM | bear_call | 2026-03-30 | 2026-04-15 | 12 | -3,685 | stop | 18 | 0.789 | 0.118 | bear | 1.686 | regime_misread | +IWM260508C00260000 -IWM260508C00255000 |
| margin | credit_width | AAPL | iron_condor | 2025-08-04 | 2025-08-13 | 14 | -3,554 | stop | 31 | 0.712 | 0.087 | sideways | 3.331 | regime_misread | +AAPL250912P00190000 -AAPL250912P00195000 +AAPL250912C00220000 -AAPL250912C00215000 |
| margin | credit_width | SPY | bear_call | 2025-04-08 | 2025-05-01 | 6 | -3,504 | stop | 42 | 0.781 | 0.135 | bear | 1.149 | exit_management | +SPY250516C00542000 -SPY250516C00532000 |
| margin | credit_width | SPY | iron_condor | 2024-08-02 | 2024-08-30 | 8 | -3,353 | dte_exit | 51 | 0.674 | 0.057 | sideways | 1.022 | strike_selection | +SPY240906P00506000 -SPY240906P00517000 +SPY240906C00562000 -SPY240906C00551000 |
| margin | credit_width | IWM | iron_condor | 2025-08-01 | 2025-08-28 | 15 | -3,196 | stop | 16 | 0.714 | 0.068 | sideways | 1.388 | exit_management | +IWM250905P00200000 -IWM250905P00204000 +IWM250905C00231000 -IWM250905C00227000 |
| margin | credit_width | QQQ | bear_call | 2025-04-08 | 2025-05-01 | 5 | -3,167 | stop | 26 | 0.793 | 0.127 | bear | 1.306 | exit_management | +QQQ250516C00465000 -QQQ250516C00455000 |
| margin | ev_proxy | SPY | iron_condor | 2025-02-14 | 2025-03-21 | 5 | -4,745 | expiry | 59 | 0.846 | 0.047 | sideways | -1.527 | regime_misread | +SPY250321P00569000 -SPY250321P00581000 +SPY250321C00655000 -SPY250321C00644000 |
| margin | ev_proxy | QQQ | iron_condor | 2025-02-25 | 2025-04-04 | 6 | -4,620 | expiry | 19 | 0.795 | 0.036 | sideways | -2.917 | regime_misread | +QQQ250404P00465000 -QQQ250404P00475000 +QQQ250404C00555000 -QQQ250404C00545000 |
| margin | ev_proxy | AAPL | bull_put | 2024-12-26 | 2025-01-21 | 11 | -4,514 | stop | 12 | 0.899 | 0.079 | bull | -2.456 | regime_misread | +AAPL250131P00235000 -AAPL250131P00240000 |
| margin | ev_proxy | IWM | bear_call | 2026-03-30 | 2026-04-15 | 13 | -3,992 | stop | 18 | 0.789 | 0.118 | bear | 1.686 | regime_misread | +IWM260508C00260000 -IWM260508C00255000 |
| margin | ev_proxy | QQQ | bull_put | 2024-07-05 | 2024-08-02 | 5 | -3,921 | stop | 8.530 | 0.878 | 0.028 | bull | -2.295 | regime_misread | +QQQ240809P00462500 -QQQ240809P00472500 |
| margin | ev_proxy | QQQ | bear_call | 2025-04-08 | 2025-05-01 | 6 | -3,801 | stop | 26 | 0.793 | 0.127 | bear | 1.306 | exit_management | +QQQ250516C00465000 -QQQ250516C00455000 |
| margin | ev_proxy | AAPL | iron_condor | 2025-08-04 | 2025-09-12 | 10 | -3,792 | expiry | 32 | 0.862 | 0.087 | sideways | 1.637 | regime_misread | +AAPL250912P00180000 -AAPL250912P00185000 +AAPL250912C00225000 -AAPL250912C00220000 |
| margin | ev_proxy | SPY | bear_call | 2025-04-08 | 2025-05-01 | 6 | -3,504 | stop | 42 | 0.781 | 0.135 | bear | 1.149 | exit_management | +SPY250516C00542000 -SPY250516C00532000 |
| margin | ev_proxy | IWM | iron_condor | 2025-02-20 | 2025-03-26 | 10 | -3,307 | dte_exit | 8.630 | 0.799 | 0.033 | sideways | -1.528 | regime_misread | +IWM250328P00205000 -IWM250328P00211000 +IWM250328C00242000 -IWM250328C00238000 |
| margin | ev_proxy | IWM | bear_call | 2025-04-04 | 2025-05-02 | 14 | -2,218 | dte_exit | 10 | 0.791 | 0.118 | bear | 0.876 | strike_selection | +IWM250509C00201000 -IWM250509C00197000 |
| margin | managed_net_ev | QQQ | iron_condor | 2025-02-25 | 2025-04-04 | 6 | -4,620 | expiry | 19 | 0.795 | 0.036 | sideways | -2.917 | regime_misread | +QQQ250404P00465000 -QQQ250404P00475000 +QQQ250404C00555000 -QQQ250404C00545000 |
| margin | managed_net_ev | AAPL | bull_put | 2024-12-26 | 2025-01-21 | 11 | -4,514 | stop | 12 | 0.899 | 0.079 | bull | -2.456 | regime_misread | +AAPL250131P00235000 -AAPL250131P00240000 |
| margin | managed_net_ev | SPY | iron_condor | 2025-02-24 | 2025-04-04 | 4 | -4,475 | expiry | 38 | 0.818 | 0.034 | sideways | -3.248 | regime_misread | +SPY250404P00550000 -SPY250404P00564000 +SPY250404C00640000 -SPY250404C00626000 |
| margin | managed_net_ev | SPY | iron_condor | 2026-02-20 | 2026-03-30 | 5 | -4,254 | stop | 67 | 0.680 | 0.047 | sideways | -1.685 | regime_misread | +SPY260331P00658000 -SPY260331P00672000 +SPY260331C00723000 -SPY260331C00709000 |
| margin | managed_net_ev | SPY | bear_call | 2026-03-30 | 2026-04-14 | 5 | -4,053 | stop | 84 | 0.803 | 0.109 | bear | 1.859 | regime_misread | +SPY260508C00673000 -SPY260508C00660000 |
| margin | managed_net_ev | QQQ | bull_put | 2024-07-05 | 2024-08-02 | 5 | -3,921 | stop | 8.530 | 0.878 | 0.028 | bull | -2.295 | regime_misread | +QQQ240809P00462500 -QQQ240809P00472500 |
| margin | managed_net_ev | AAPL | iron_condor | 2025-08-04 | 2025-09-12 | 10 | -3,792 | expiry | 32 | 0.862 | 0.087 | sideways | 1.637 | regime_misread | +AAPL250912P00180000 -AAPL250912P00185000 +AAPL250912C00225000 -AAPL250912C00220000 |
| margin | managed_net_ev | QQQ | iron_condor | 2026-03-17 | 2026-04-17 | 6 | -3,694 | dte_exit | 52 | 0.659 | 0.056 | sideways | 1.156 | strike_selection | +QQQ260424P00571000 -QQQ260424P00583000 +QQQ260424C00640000 -QQQ260424C00626000 |
| margin | managed_net_ev | IWM | bear_call | 2026-03-30 | 2026-04-15 | 12 | -3,685 | stop | 18 | 0.789 | 0.118 | bear | 1.686 | regime_misread | +IWM260508C00260000 -IWM260508C00255000 |
| margin | managed_net_ev | SPY | bear_call | 2025-04-08 | 2025-05-01 | 6 | -3,504 | stop | 42 | 0.781 | 0.135 | bear | 1.149 | exit_management | +SPY250516C00542000 -SPY250516C00532000 |
| margin | rorc_day | QQQ | iron_condor | 2025-02-25 | 2025-04-04 | 6 | -4,620 | expiry | 19 | 0.795 | 0.036 | sideways | -2.917 | regime_misread | +QQQ250404P00465000 -QQQ250404P00475000 +QQQ250404C00555000 -QQQ250404C00545000 |
| margin | rorc_day | SPY | iron_condor | 2025-02-10 | 2025-03-21 | 7 | -4,580 | expiry | 49 | 0.663 | 0.038 | sideways | -1.325 | strike_selection | +SPY250321P00579000 -SPY250321P00591000 +SPY250321C00636000 -SPY250321C00624000 |
| margin | rorc_day | AAPL | bull_put | 2024-12-26 | 2025-01-21 | 11 | -4,514 | stop | 12 | 0.899 | 0.079 | bull | -2.456 | regime_misread | +AAPL250131P00235000 -AAPL250131P00240000 |
| margin | rorc_day | QQQ | bull_put | 2024-07-05 | 2024-08-02 | 5 | -3,921 | stop | 8.530 | 0.878 | 0.028 | bull | -2.295 | regime_misread | +QQQ240809P00462500 -QQQ240809P00472500 |
| margin | rorc_day | SPY | bear_call | 2025-04-08 | 2025-05-01 | 6 | -3,504 | stop | 42 | 0.781 | 0.135 | bear | 1.149 | exit_management | +SPY250516C00542000 -SPY250516C00532000 |
| margin | rorc_day | IWM | bear_call | 2026-03-30 | 2026-04-15 | 11 | -3,378 | stop | 18 | 0.789 | 0.118 | bear | 1.686 | regime_misread | +IWM260508C00260000 -IWM260508C00255000 |
| margin | rorc_day | SPY | iron_condor | 2024-08-02 | 2024-08-30 | 8 | -3,353 | dte_exit | 51 | 0.674 | 0.057 | sideways | 1.022 | strike_selection | +SPY240906P00506000 -SPY240906P00517000 +SPY240906C00562000 -SPY240906C00551000 |
| margin | rorc_day | SPY | bear_call | 2026-03-26 | 2026-04-15 | 4 | -3,350 | stop | 45 | 0.774 | 0.091 | bear | 1.537 | regime_misread | +SPY260501C00685000 -SPY260501C00672000 |
| margin | rorc_day | AAPL | iron_condor | 2025-08-04 | 2025-08-13 | 13 | -3,300 | stop | 31 | 0.712 | 0.087 | sideways | 3.331 | regime_misread | +AAPL250912P00190000 -AAPL250912P00195000 +AAPL250912C00220000 -AAPL250912C00215000 |
| margin | rorc_day | IWM | iron_condor | 2025-08-01 | 2025-08-28 | 15 | -3,196 | stop | 16 | 0.714 | 0.068 | sideways | 1.388 | exit_management | +IWM250905P00200000 -IWM250905P00204000 +IWM250905C00231000 -IWM250905C00227000 |
| margin | rorc_day_vrp | AAPL | bull_put | 2024-12-26 | 2025-01-21 | 10 | -4,104 | stop | 12 | 0.899 | 0.079 | bull | -2.456 | regime_misread | +AAPL250131P00235000 -AAPL250131P00240000 |
| margin | rorc_day_vrp | SPY | iron_condor | 2025-02-10 | 2025-03-21 | 6 | -3,926 | expiry | 49 | 0.663 | 0.038 | sideways | -1.325 | strike_selection | +SPY250321P00579000 -SPY250321P00591000 +SPY250321C00636000 -SPY250321C00624000 |
| margin | rorc_day_vrp | QQQ | bull_put | 2024-07-05 | 2024-08-02 | 5 | -3,921 | stop | 8.530 | 0.878 | 0.028 | bull | -2.295 | regime_misread | +QQQ240809P00462500 -QQQ240809P00472500 |
| margin | rorc_day_vrp | QQQ | iron_condor | 2025-02-25 | 2025-04-04 | 5 | -3,850 | expiry | 19 | 0.795 | 0.036 | sideways | -2.917 | regime_misread | +QQQ250404P00465000 -QQQ250404P00475000 +QQQ250404C00555000 -QQQ250404C00545000 |
| margin | rorc_day_vrp | SPY | iron_condor | 2024-06-03 | 2024-07-05 | 6 | -3,431 | dte_exit | 20 | 0.789 | 0.022 | sideways | 1.385 | strike_selection | +SPY240712P00495000 -SPY240712P00505000 +SPY240712C00555000 -SPY240712C00545000 |
| margin | rorc_day_vrp | IWM | bear_call | 2026-03-30 | 2026-04-15 | 11 | -3,378 | stop | 18 | 0.789 | 0.118 | bear | 1.686 | regime_misread | +IWM260508C00260000 -IWM260508C00255000 |
| margin | rorc_day_vrp | SPY | iron_condor | 2024-08-02 | 2024-08-30 | 8 | -3,353 | dte_exit | 51 | 0.674 | 0.057 | sideways | 1.022 | strike_selection | +SPY240906P00506000 -SPY240906P00517000 +SPY240906C00562000 -SPY240906C00551000 |
| margin | rorc_day_vrp | SPY | bear_call | 2026-03-26 | 2026-04-15 | 4 | -3,350 | stop | 45 | 0.774 | 0.091 | bear | 1.537 | regime_misread | +SPY260501C00685000 -SPY260501C00672000 |
| margin | rorc_day_vrp | AAPL | iron_condor | 2025-08-04 | 2025-08-13 | 13 | -3,300 | stop | 31 | 0.712 | 0.087 | sideways | 3.331 | regime_misread | +AAPL250912P00190000 -AAPL250912P00195000 +AAPL250912C00220000 -AAPL250912C00215000 |
| margin | rorc_day_vrp | QQQ | bear_call | 2025-04-08 | 2025-05-01 | 5 | -3,167 | stop | 26 | 0.793 | 0.127 | bear | 1.306 | exit_management | +QQQ250516C00465000 -QQQ250516C00455000 |
| cash_debit | debit_width | NVDA | long_put | 2024-07-25 | 2024-08-19 | 9 | -5,457 | stop | 519 | 0.595 | -2.011 | bear | 0.896 | exit_management | +NVDA240906P00109000 |
| cash_debit | debit_width | QQQ | bear_put | 2026-07-30 | 2026-09-11 | 13 | -5,315 | expiry | 101 | 0.618 | -0.014 | bear | 0.550 | strike_selection | +QQQ260911P00672000 -QQQ260911P00657000 |
| cash_debit | debit_width | NVDA | bull_call | 2024-08-19 | 2024-08-29 | 80 | -5,186 | stop | 36 | 0.513 | -2.189 | bull | -1.000 | exit_management | +NVDA240927C00155000 -NVDA240927C00160000 |
| cash_debit | debit_width | TSLA | long_put | 2026-07-23 | 2026-09-04 | 2 | -5,157 | expiry | 352 | 0.484 | -0.216 | bear | 0.628 | strike_selection | +TSLA260904P00330000 |
| cash_debit | debit_width | AAPL | long_call | 2024-07-10 | 2024-07-24 | 12 | -5,115 | stop | 58 | 0.448 | -0.052 | bull | -1.344 | exit_management | +AAPL240823C00240000 |
| cash_debit | debit_width | SPY | bear_put | 2024-08-12 | 2024-08-19 | 33 | -5,093 | stop | 34 | 0.551 | -0.004 | bear | 2.039 | regime_misread | +SPY240927P00516000 -SPY240927P00505000 |
| cash_debit | debit_width | TSLA | long_call | 2024-07-24 | 2024-08-07 | 7 | -5,055 | stop | 216 | 0.488 | -0.233 | bull | -1.202 | exit_management | +TSLA240830C00230000 |
| cash_debit | debit_width | TSLA | long_put | 2024-08-07 | 2024-09-05 | 2 | -5,035 | stop | 152 | 0.476 | -0.116 | bear | 1.125 | exit_management | +TSLA240920P00215000 |
| cash_debit | debit_width | AAPL | long_put | 2024-08-09 | 2024-08-29 | 7 | -4,897 | stop | 18 | 0.435 | -0.013 | bear | 0.989 | exit_management | +AAPL240920P00220000 |
| cash_debit | debit_width | NVDA | bear_put | 2024-06-10 | 2024-07-09 | 101 | -4,827 | stop | 43 | 0.602 | -6.046 | bear | 0.621 | exit_management | +NVDA240726P00111000 -NVDA240726P00109000 |
| cash_debit | ev_proxy | QQQ | bear_put | 2026-07-30 | 2026-09-11 | 19 | -7,769 | expiry | 101 | 0.618 | -0.014 | bear | 0.550 | strike_selection | +QQQ260911P00672000 -QQQ260911P00657000 |
| cash_debit | ev_proxy | TSLA | long_put | 2026-07-23 | 2026-09-04 | 3 | -7,735 | expiry | 352 | 0.484 | -0.216 | bear | 0.628 | strike_selection | +TSLA260904P00330000 |
| cash_debit | ev_proxy | AAPL | long_call | 2026-07-16 | 2026-07-31 | 8 | -6,160 | stop | 102 | 0.455 | -0.047 | bull | -1.330 | exit_management | +AAPL260828C00345000 |
| cash_debit | ev_proxy | NVDA | bull_call | 2024-08-19 | 2024-08-29 | 95 | -6,158 | stop | 36 | 0.513 | -2.189 | bull | -1.000 | exit_management | +NVDA240927C00155000 -NVDA240927C00160000 |
| cash_debit | ev_proxy | AAPL | long_call | 2024-07-10 | 2024-07-24 | 14 | -5,967 | stop | 58 | 0.448 | -0.052 | bull | -1.344 | exit_management | +AAPL240823C00240000 |
| cash_debit | ev_proxy | NVDA | long_call | 2026-05-21 | 2026-06-10 | 9 | -5,858 | stop | 28 | 0.414 | -0.036 | bull | -1.016 | exit_management | +NVDA260702C00230000 |
| cash_debit | ev_proxy | NVDA | long_put | 2024-08-05 | 2024-08-19 | 3 | -5,791 | stop | 902 | 0.593 | -1.952 | bear | 1.628 | regime_misread | +NVDA240920P00121000 |
| cash_debit | ev_proxy | TSLA | long_call | 2024-07-24 | 2024-08-07 | 8 | -5,778 | stop | 216 | 0.488 | -0.233 | bull | -1.202 | exit_management | +TSLA240830C00230000 |
| cash_debit | ev_proxy | AAPL | long_put | 2024-08-09 | 2024-08-29 | 8 | -5,597 | stop | 18 | 0.435 | -0.013 | bear | 0.989 | exit_management | +AAPL240920P00220000 |
| cash_debit | ev_proxy | IWM | long_put | 2025-04-09 | 2025-05-12 | 4 | -5,473 | stop | 138 | 0.485 | -0.068 | bear | 0.952 | exit_management | +IWM250523P00202000 |
| cash_debit | managed_net_ev | QQQ | bear_put | 2026-07-30 | 2026-09-11 | 13 | -5,315 | expiry | 101 | 0.618 | -0.014 | bear | 0.550 | strike_selection | +QQQ260911P00672000 -QQQ260911P00657000 |
| cash_debit | managed_net_ev | TSLA | long_put | 2026-07-23 | 2026-09-04 | 2 | -5,157 | expiry | 352 | 0.484 | -0.216 | bear | 0.628 | strike_selection | +TSLA260904P00330000 |
| cash_debit | managed_net_ev | AAPL | long_call | 2024-07-17 | 2024-08-05 | 10 | -5,040 | stop | 12 | 0.413 | -0.011 | bull | -1.564 | regime_misread | +AAPL240830C00235000 |
| cash_debit | managed_net_ev | NVDA | long_call | 2026-05-21 | 2026-06-10 | 7 | -4,556 | stop | 28 | 0.414 | -0.036 | bull | -1.016 | exit_management | +NVDA260702C00230000 |
| cash_debit | managed_net_ev | AAPL | long_call | 2026-07-16 | 2026-07-31 | 4 | -4,510 | stop | 103 | 0.448 | -0.047 | bull | -1.330 | exit_management | +AAPL260828C00335000 |
| cash_debit | managed_net_ev | AAPL | long_put | 2024-08-09 | 2024-08-29 | 6 | -4,198 | stop | 18 | 0.435 | -0.013 | bear | 0.989 | exit_management | +AAPL240920P00220000 |
| cash_debit | managed_net_ev | IWM | long_put | 2025-04-09 | 2025-05-12 | 3 | -4,104 | stop | 138 | 0.485 | -0.068 | bear | 0.952 | exit_management | +IWM250523P00202000 |
| cash_debit | managed_net_ev | IWM | long_put | 2024-08-12 | 2024-08-30 | 4 | -4,075 | stop | 43 | 0.461 | -0.020 | bear | 1.327 | exit_management | +IWM240927P00216000 |
| cash_debit | managed_net_ev | AAPL | long_put | 2026-06-30 | 2026-07-09 | 4 | -4,026 | stop | 28 | 0.432 | -0.033 | bear | 1.996 | regime_misread | +AAPL260807P00295000 |
| cash_debit | managed_net_ev | AAPL | long_call | 2024-08-29 | 2024-09-16 | 8 | -3,973 | stop | 35 | 0.435 | -0.017 | bull | -1.155 | exit_management | +AAPL241011C00235000 |
| cash_debit | rorc_day | QQQ | bear_put | 2026-07-30 | 2026-09-11 | 14 | -5,724 | expiry | 101 | 0.618 | -0.014 | bear | 0.550 | strike_selection | +QQQ260911P00672000 -QQQ260911P00657000 |
| cash_debit | rorc_day | NVDA | long_put | 2024-07-25 | 2024-08-19 | 9 | -5,457 | stop | 519 | 0.595 | -2.011 | bear | 0.896 | exit_management | +NVDA240906P00109000 |
| cash_debit | rorc_day | AAPL | long_call | 2024-07-10 | 2024-07-24 | 12 | -5,115 | stop | 58 | 0.448 | -0.052 | bull | -1.344 | exit_management | +AAPL240823C00240000 |
| cash_debit | rorc_day | SPY | bear_put | 2024-08-12 | 2024-08-19 | 28 | -5,100 | stop | 55 | 0.590 | -0.004 | bear | 2.039 | regime_misread | +SPY240927P00522500 -SPY240927P00512000 |
| cash_debit | rorc_day | TSLA | long_call | 2024-07-24 | 2024-08-07 | 7 | -5,055 | stop | 216 | 0.488 | -0.233 | bull | -1.202 | exit_management | +TSLA240830C00230000 |
| cash_debit | rorc_day | AAPL | long_put | 2024-08-09 | 2024-08-29 | 7 | -4,897 | stop | 18 | 0.435 | -0.013 | bear | 0.989 | exit_management | +AAPL240920P00220000 |
| cash_debit | rorc_day | IWM | long_put | 2024-08-12 | 2024-08-23 | 7 | -4,866 | stop | 28 | 0.442 | -0.020 | bear | 1.729 | regime_misread | +IWM240927P00209000 |
| cash_debit | rorc_day | TSLA | long_put | 2026-07-23 | 2026-09-04 | 3 | -4,632 | expiry | 285 | 0.487 | -0.216 | bear | 0.628 | strike_selection | +TSLA260904P00310000 |
| cash_debit | rorc_day | AAPL | long_call | 2026-07-16 | 2026-07-31 | 6 | -4,620 | stop | 102 | 0.455 | -0.047 | bull | -1.330 | exit_management | +AAPL260828C00345000 |
| cash_debit | rorc_day | NVDA | long_call | 2026-05-21 | 2026-06-10 | 7 | -4,556 | stop | 28 | 0.414 | -0.036 | bull | -1.016 | exit_management | +NVDA260702C00230000 |
| cash_debit | rorc_day_vrp | AAPL | long_call | 2026-07-27 | 2026-07-31 | 7 | -4,784 | stop | 22 | 0.413 | 0.004 | bull | -2.850 | regime_misread | +AAPL260904C00350000 |
| cash_debit | rorc_day_vrp | AAPL | long_call | 2024-10-14 | 2024-11-01 | 12 | -4,376 | stop | 20 | 0.421 | 0.004 | bull | -0.694 | exit_management | +AAPL241129C00240000 |
| cash_debit | rorc_day_vrp | IWM | long_call | 2025-07-03 | 2025-07-31 | 13 | -4,277 | stop | 20 | 0.428 | 0.001 | bull | -0.282 | exit_management | +IWM250815C00229000 |
| cash_debit | rorc_day_vrp | SPY | long_call | 2024-11-29 | 2024-12-18 | 9 | -4,267 | stop | 88 | 0.476 | 0.000 | bull | -1.019 | exit_management | +SPY250110C00612000 |
| cash_debit | rorc_day_vrp | IWM | long_call | 2025-08-27 | 2025-10-10 | 11 | -4,145 | stop | 16 | 0.421 | 0.005 | bull | 0.105 | exit_management | +IWM251017C00243000 |
| cash_debit | rorc_day_vrp | IWM | long_call | 2026-05-28 | 2026-07-06 | 10 | -3,942 | dte_exit | 27 | 0.429 | 0.010 | bull | 0.327 | strike_selection | +IWM260710C00300000 |
| cash_debit | rorc_day_vrp | IWM | long_call | 2026-04-20 | 2026-05-18 | 9 | -3,936 | stop | 15 | 0.418 | 0.014 | bull | -0.077 | exit_management | +IWM260529C00284000 |
| cash_debit | rorc_day_vrp | QQQ | long_call | 2025-12-19 | 2026-01-20 | 5 | -3,847 | stop | 101 | 0.457 | 0.001 | bull | -0.285 | exit_management | +QQQ260130C00630000 |
| cash_debit | rorc_day_vrp | AAPL | long_call | 2025-11-04 | 2025-12-12 | 9 | -2,823 | dte_exit | 12 | 0.413 | 0.004 | bull | 0.388 | strike_selection | +AAPL251219C00280000 |
| cash_debit | rorc_day_vrp | AAPL | long_call | 2025-09-25 | 2025-10-31 | 8 | 405 | dte_exit | 34 | 0.431 | 0.005 | bull | 0.593 | strike_selection | +AAPL251107C00265000 |
