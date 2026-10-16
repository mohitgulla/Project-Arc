# E7.17 loss attribution: generated tables

Written by `scripts/loss_attribution.py report`. Aggregates only.

## Runs and counterfactuals

`runs.csv`

| run | profile | what | trades | net_pnl | end_equity | return_pct | max_dd_pct | win_rate | pnl_per_risk | return_on_risk | mean_r | eq_2020_06_30 | eq_2021_12_31 | eq_2022_12_31 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| legacy | margin | E7.11/E7.14 simulator (reproduces the BT-3 control book) | 440 | -17,294 | 7,706 | -69.18 | 83 | 0.732 | -0.039 | -0.0321 | -0.0509 | 16,103 | 19,759 | 10,740 |
| legacy | cash_debit | E7.11/E7.14 simulator (reproduces the BT-3 control book) | 585 | -24,880 | 120.1 | -99.52 | 99.74 | 0.504 | -0.053 | -0.0038 | -0.0211 | 6,489 | 3,894 | 477 |
| marks_only | margin | exit-book fix only (splits still ignored) | 459 | -20,435 | 4,565 | -81.74 | 87.22 | 0.708 | -0.0378 | -0.0466 | -0.0634 | 20,459 | 22,980 | 15,572 |
| marks_only | cash_debit | exit-book fix only (splits still ignored) | 441 | -24,905 | 94.73 | -99.62 | 99.78 | 0.478 | -0.1139 | -0.1933 | -0.0619 | 4,033 | 1,073 | 443 |
| fixed | margin | corrected simulator: base book for attribution | 469 | -17,354 | 7,646 | -69.42 | 83.32 | 0.721 | -0.0295 | -0.0221 | -0.0439 | 20,459 | 25,933 | 18,492 |
| fixed | cash_debit | corrected simulator: base book for attribution | 431 | -24,876 | 123.8 | -99.5 | 99.73 | 0.501 | -0.1212 | -0.1902 | -0.0402 | 4,033 | 651 | 300 |
| est_spread | margin | fills at the costs.yaml estimated spread (4 % of mid, min $0.03) not the quote | 447 | -21,976 | 3,024 | -87.9 | 91.84 | 0.702 | -0.0436 | -0.06 | -0.0697 | 20,607 | 23,191 | 12,203 |
| est_spread | cash_debit | fills at the costs.yaml estimated spread (4 % of mid, min $0.03) not the quote | 468 | -24,897 | 103 | -99.59 | 99.78 | 0.483 | -0.0723 | -0.0407 | -0.0604 | 6,289 | 1,122 | 277 |
| half_size | margin | contracts x 0.5 | 508 | -13,487 | 11,513 | -53.95 | 64.7 | 0.697 | -0.0397 | -0.0299 | -0.0561 | 20,627 | 20,737 | 15,556 |
| half_size | cash_debit | contracts x 0.5 | 676 | -24,721 | 279.3 | -98.88 | 99.11 | 0.47 | -0.0893 | -0.0703 | -0.0869 | 10,810 | 3,529 | 1,450 |
| half_slots | margin | max_open_positions 10 -> 5 | 273 | -21,285 | 3,715 | -85.14 | 87.85 | 0.674 | -0.065 | -0.0668 | -0.0774 | 22,435 | 16,302 | 11,067 |
| half_slots | cash_debit | max_open_positions 10 -> 5 | 362 | -24,903 | 97.24 | -99.61 | 99.72 | 0.472 | -0.0948 | -0.0503 | -0.0777 | 11,647 | 1,328 | 552 |
| day_risk_cap | margin | new max loss per day <= 15 % of equity (one max_alloc_pct position) | 445 | -13,483 | 11,517 | -53.93 | 68.61 | 0.71 | -0.0313 | -0.0212 | -0.0429 | 18,699 | 18,190 | 13,129 |
| day_risk_cap | cash_debit | new max loss per day <= 15 % of equity (one max_alloc_pct position) | 610 | -24,795 | 205.2 | -99.18 | 99.66 | 0.492 | -0.0473 | -0.0722 | -0.0479 | 8,003 | 20,870 | 1,133 |
| no_bull_put__no_bull_call | margin | minus bull_put (margin) / bull_call (cash_debit) | 314 | -13,966 | 11,034 | -55.86 | 68.65 | 0.643 | -0.047 | -0.0073 | -0.0556 | 12,951 | 14,910 | 11,416 |
| no_bull_put__no_bull_call | cash_debit | minus bull_put (margin) / bull_call (cash_debit) | 340 | -24,858 | 142.4 | -99.43 | 99.64 | 0.438 | -0.12 | -0.1362 | -0.0941 | 4,668 | 581 | 499 |
| no_bear_call__no_bear_put | margin | minus bear_call (margin) / bear_put (cash_debit) | 426 | -15,582 | 9,418 | -62.33 | 84.7 | 0.728 | -0.0324 | -0.024 | -0.0397 | 18,816 | 25,838 | 12,314 |
| no_bear_call__no_bear_put | cash_debit | minus bear_call (margin) / bear_put (cash_debit) | 380 | -24,846 | 154.4 | -99.38 | 99.67 | 0.482 | -0.1342 | -0.2361 | -0.0441 | 3,887 | 620 | 290 |
| no_iron_condor__no_long_call | margin | minus iron_condor (margin) / long_call (cash_debit) | 404 | -20,501 | 4,499 | -82 | 85.6 | 0.785 | -0.0511 | -0.0482 | -0.0352 | 15,840 | 16,816 | 10,450 |
| no_iron_condor__no_long_call | cash_debit | minus iron_condor (margin) / long_call (cash_debit) | 390 | -24,880 | 120.3 | -99.52 | 99.54 | 0.513 | -0.1541 | -0.0684 | -0.0351 | 5,492 | 3,290 | 608 |
| no_long_put | margin | minus long_put (cash_debit) | 469 | -17,354 | 7,646 | -69.42 | 83.32 | 0.721 | -0.0295 | -0.0221 | -0.0439 | 20,459 | 25,933 | 18,492 |
| no_long_put | cash_debit | minus long_put (cash_debit) | 461 | -24,877 | 123.4 | -99.51 | 99.85 | 0.505 | -0.0431 | -0.014 | -0.0347 | 19,028 | 707 | 299 |
| BT-3 control (E7.14) | margin |  | 440 | -17,294 | 7,706 | -69.18 | 83 | 0.732 | -0.039 | -0.0321 | -0.0509 | 16,103 | 19,759 | 10,740 |
| BT-2 hold (E7.14) | margin |  | 319 | -23,996 | 1,004 | -95.99 | 96.51 | 0.624 | -0.1023 | -0.0546 | -0.0424 | 11,593 | 20,212 | 2,624 |
| BT-2 managed (E7.14) | margin |  | 440 | -17,294 | 7,706 | -69.18 | 83 | 0.732 | -0.039 | -0.0321 | -0.0509 | 16,103 | 19,759 | 10,740 |
| BT-3 control (E7.14) | cash_debit |  | 585 | -24,880 | 120.1 | -99.52 | 99.74 | 0.504 | -0.053 | -0.0038 | -0.0211 | 6,489 | 3,894 | 477 |
| BT-2 hold (E7.14) | cash_debit |  | 142 | -24,907 | 93.02 | -99.63 | 99.8 | 0.254 | -0.343 | -0.3599 | -0.1053 | 6,774 | 198 | 103 |
| BT-2 managed (E7.14) | cash_debit |  | 585 | -24,880 | 120.1 | -99.52 | 99.74 | 0.504 | -0.053 | -0.0038 | -0.0211 | 6,489 | 3,894 | 477 |

## Sanity (bt3)

`sanity_bt3.csv`

| profile | sample | underlying | kind | entry_date | contracts | sim_entry_net | rp_entry_net | sim_exit_reason | exit_date | rp_exit_reason | rp_exit_day | quoted_sessions | sim_pnl_unit | rp_pnl_unit | d_pnl_unit | sim_fees_unit | rp_fees_unit | risk_pct_equity | splits_in_hold | exit_match |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| margin | random | BA | iron_condor | 2021-10-21 | 13 | -2.52 | -2.52 | take_profit | 2021-11-19 | take_profit | 2021-11-19 | 21/21 | 150.2 | 150.2 | 0 | 0.3224 | 0.3224 | 13.84 |  | True |
| margin | worst | BAC | bull_put | 2023-02-07 | 16 | -0.11 | -0.11 | stop | 2023-03-09 | stop | 2023-03-09 | 21/21 | -89.16 | -89.16 | 0 | 0.1612 | 0.1612 | 14.2 |  | True |
| margin | worst | C | iron_condor | 2022-02-11 | 18 | -0.6525 | -0.6525 | expiry | 2022-03-18 | stop | 2022-03-08 | 16/16 | -185 | -182.6 | 2.42 | 0.2418 | 0.3224 | 14.04 |  | False |
| margin | random | C | iron_condor | 2021-04-13 | 8 | -0.4725 | -0.4725 | take_profit | 2021-04-27 | take_profit | 2021-04-23 | 8/8 | 26.68 | 18.68 | -8 | 0.3224 | 0.3224 | 10.88 |  | False |
| margin | worst | COST | iron_condor | 2020-01-16 | 13 | -2.163 | -2.163 | expiry | 2020-02-21 | stop | 2020-02-14 | 20/20 | -284 | -238.6 | 45.42 | 0.2418 | 0.3224 | 14.27 |  | False |
| margin | worst | CVX | bull_put | 2022-06-09 | 5 | -0.5425 | -0.5425 | stop | 2022-06-23 | stop | 2022-06-23 | 9/9 | -385.9 | -385.9 | 0 | 0.1612 | 0.1612 | 13.04 |  | True |
| margin | worst | EWZ | bull_put | 2022-04-13 | 37 | -0.1225 | -0.1225 | stop | 2022-05-06 | stop | 2022-05-06 | 16/16 | -72.41 | -72.41 | 0 | 0.1612 | 0.1612 | 14.62 |  | True |
| margin | random | EWZ | bull_put | 2020-12-08 | 24 | -0.205 | -0.205 | take_profit | 2020-12-16 | take_profit | 2020-12-16 | 6/6 | 9.34 | 9.34 | 0 | 0.1612 | 0.1612 | 14.41 |  | True |
| margin | worst | GLD | iron_condor | 2022-04-12 | 11 | -1.555 | -1.555 | expiry | 2022-05-20 | stop | 2022-05-10 | 19/19 | -244.7 | -203.8 | 40.92 | 0.2418 | 0.3224 | 12.1 |  | False |
| margin | worst | GLD | iron_condor | 2020-02-14 | 21 | -1.125 | -1.125 | dte_exit | 2020-03-17 | dte_exit | 2020-03-17 | 21/21 | -88.57 | -88.57 | 0 | 0.3224 | 0.3224 | 13.83 |  | True |
| margin | random | GLD | bull_put | 2021-05-28 | 2 | -0.82 | -0.82 | stop | 2021-06-17 | stop | 2021-06-17 | 13/13 | -251.9 | -251.9 | 0 | 0.1612 | 0.1612 | 3.25 |  | True |
| margin | random | GLD | bull_put | 2020-08-07 | 1 | -0.5925 | -0.5925 | dte_exit | 2020-09-04 | dte_exit | 2020-09-04 | 20/20 | -28.16 | -28.16 | 0 | 0.1612 | 0.1612 | 1.67 |  | True |
| margin | random | GLD | bull_put | 2020-07-29 | 1 | -0.775 | -0.775 | take_profit | 2020-08-04 | take_profit | 2020-08-04 | 4/4 | 38.34 | 38.34 | 0 | 0.1612 | 0.1612 | 1.61 |  | True |
| margin | worst | IWM | bull_put | 2021-11-08 | 8 | -1.163 | -1.163 | stop | 2021-12-01 | stop | 2021-12-01 | 16/16 | -344.7 | -344.7 | 0 | 0.1612 | 0.1612 | 14.03 |  | True |
| margin | worst | IWM | iron_condor | 2020-07-13 | 15 | -1.583 | -1.583 | expiry | 2020-08-21 | stop | 2020-08-13 | 23/23 | -142 | -120.1 | 21.92 | 0.2418 | 0.3224 | 13.2 |  | False |
| margin | worst | IWM | iron_condor | 2020-09-08 | 13 | -1.452 | -1.452 | expiry | 2020-10-16 | dte_exit | 2020-10-09 | 23/23 | -155 | -92.07 | 62.92 | 0.2418 | 0.3224 | 13.57 |  | False |
| margin | worst | IWM | iron_condor | 2021-10-11 | 9 | -1.975 | -1.975 | stop | 2021-11-05 | stop | 2021-11-05 | 19/19 | -164.8 | -164.8 | 0 | 0.3224 | 0.3224 | 8.18 |  | True |
| margin | random | IWM | bull_put | 2020-10-19 | 2 | -0.4825 | -0.4825 | take_profit | 2020-11-04 | take_profit | 2020-11-04 | 12/12 | 21.59 | 21.59 | 0 | 0.1612 | 0.1612 | 7.98 |  | True |
| margin | random | IWM | bull_put | 2021-01-07 | 1 | -0.5375 | -0.5375 | take_profit | 2021-02-01 | take_profit | 2021-02-01 | 16/16 | 22.59 | 22.59 | 0 | 0.1612 | 0.1612 | 3.19 |  | True |
| margin | random | IWM | bull_put | 2020-11-25 | 2 | -0.4175 | -0.4175 | take_profit | 2020-12-07 | take_profit | 2020-12-07 | 7/7 | 17.59 | 17.59 | 0 | 0.1612 | 0.1612 | 5.49 |  | True |
| margin | worst | JNJ | bull_put | 2020-02-12 | 9 | -0.6325 | -0.6325 | expiry | 2020-03-20 | expiry | 2020-03-20 | 25/25 | -436.9 | -436.9 | 0 | 0.1612 | 0.1612 | 13.88 |  | True |
| margin | random | PFE | bull_put | 2021-04-15 | 27 | -0.17 | -0.17 | take_profit | 2021-04-19 | take_profit | 2021-04-19 | 2/2 | 8.34 | 8.34 | 0 | 0.1612 | 0.1612 | 14.99 |  | True |
| margin | worst | QQQ | bull_put | 2020-02-14 | 6 | -0.5575 | -0.5575 | stop | 2020-02-28 | stop | 2020-02-28 | 9/9 | -360.9 | -360.9 | 0 | 0.1612 | 0.1612 | 9.35 |  | True |
| margin | random | QQQ | bull_put | 2021-04-06 | 1 | -0.7525 | -0.7525 | take_profit | 2021-04-13 | take_profit | 2021-04-13 | 5/5 | 36.09 | 36.09 | 0 | 0.1612 | 0.1612 | 4.56 |  | True |
| margin | random | QQQ | bull_put | 2023-02-09 | 1 | -0.7325 | -0.7325 | take_profit | 2023-02-15 | take_profit | 2023-02-15 | 4/4 | 40.09 | 40.09 | 0 | 0.1612 | 0.1612 | 5.18 |  | True |
| margin | random | SPY | iron_condor | 2021-02-11 | 1 | -2.075 | -2.075 | take_profit | 2021-03-09 | take_profit | 2021-03-09 | 17/17 | 128.4 | 128.4 | 0 | 0.3224 | 0.3224 | 3.76 |  | True |
| margin | random | SPY | iron_condor | 2021-09-14 | 2 | -3.562 | -3.562 | take_profit | 2021-10-14 | take_profit | 2021-10-08 | 18/18 | 289.9 | 182.7 | -107.2 | 0.3224 | 0.3224 | 5.39 |  | False |
| margin | random | SPY | bull_put | 2023-06-20 | 1 | -0.7175 | -0.7175 | take_profit | 2023-06-30 | take_profit | 2023-06-30 | 8/8 | 42.09 | 42.09 | 0 | 0.1612 | 0.1612 | 13.86 |  | True |
| margin | worst | T | bull_put | 2022-01-14 | 36 | -0.0875 | -0.0875 | dte_exit | 2022-02-11 | dte_exit | 2022-02-11 | 19/19 | -57.66 | -57.66 | 0 | 0.1612 | 0.1612 | 14.89 |  | True |
| margin | worst | TLT | iron_condor | 2021-02-12 | 11 | -1.2 | -1.2 | expiry | 2021-03-19 | stop | 2021-03-12 | 19/19 | -180.2 | -178.1 | 2.17 | 0.2418 | 0.3224 | 12.75 |  | False |
| cash_debit | worst | AAPL | long_put | 2020-03-16 | 1 | 46.25 | 46.25 | stop | 2020-04-13 | stop | 2020-04-13 | 19/19 | -3,766 | -3,766 | 0 | 0.0806 | 0.0806 | 9.26 |  | True |
| cash_debit | random | AMD | long_call | 2020-04-23 | 3 | 3.15 | 3.15 | stop | 2020-05-01 | stop | 2020-05-01 | 6/6 | -267.8 | -267.8 | 0 | 0.0806 | 0.0806 | 4.9 |  | True |
| cash_debit | random | BA | bull_call | 2021-03-12 | 12 | 0.875 | 0.875 | stop | 2021-03-23 | stop | 2021-03-23 | 7/7 | -80.41 | -80.41 | 0 | 0.1612 | 0.1612 | 3.29 |  | True |
| cash_debit | worst | BABA | long_put | 2020-03-16 | 3 | 21.77 | 21.77 | stop | 2020-04-07 | stop | 2020-04-07 | 16/16 | -1,870 | -1,870 | 0 | 0.0806 | 0.0806 | 13.08 |  | True |
| cash_debit | worst | BABA | long_call | 2021-02-04 | 2 | 13.85 | 13.85 | stop | 2021-03-01 | stop | 2021-03-01 | 16/16 | -1,322 | -1,322 | 0 | 0.0806 | 0.0806 | 7.18 |  | True |
| cash_debit | worst | BAC | long_put | 2020-03-16 | 19 | 3.562 | 3.562 | stop | 2020-04-08 | stop | 2020-04-08 | 17/17 | -301.8 | -301.8 | 0 | 0.0806 | 0.0806 | 13.55 |  | True |
| cash_debit | random | BAC | long_call | 2021-01-07 | 7 | 1.335 | 1.335 | stop | 2021-01-25 | stop | 2021-01-25 | 11/11 | -105.1 | -105.1 | 0 | 0.0806 | 0.0806 | 2.47 |  | True |
| cash_debit | random | BAC | bull_call | 2023-07-28 | 1 | 0.2275 | 0.2275 | expiry | 2023-09-01 | stop | 2023-08-14 | 11/11 | -22.83 | -20.16 | 2.67 | 0.0806 | 0.1612 | 11.97 |  | False |
| cash_debit | worst | C | long_call | 2020-06-09 | 11 | 2.808 | 2.808 | stop | 2020-06-11 | stop | 2020-06-11 | 2/2 | -227.3 | -227.3 | 0 | 0.0806 | 0.0806 | 14.06 |  | True |
| cash_debit | random | C | bull_call | 2023-01-20 | 2 | 0.2525 | 0.2525 | expiry | 2023-02-24 | stop | 2023-02-17 | 20/20 | -25.33 | -20.66 | 4.67 | 0.0806 | 0.1612 | 8.45 |  | False |
| cash_debit | random | CAT | bear_put | 2022-02-08 | 5 | 1.34 | 1.34 | take_profit | 2022-02-18 | take_profit | 2022-02-18 | 8/8 | 87.09 | 87.09 | 0 | 0.1612 | 0.1612 | 11.71 |  | True |
| cash_debit | random | DHR | long_call | 2021-09-02 | 2 | 5.775 | 5.775 | stop | 2021-09-29 | stop | 2021-09-29 | 18/18 | -550.1 | -550.1 | 0 | 0.0806 | 0.0806 | 11.26 |  | True |
| cash_debit | random | DIA | bear_put | 2022-09-13 | 1 | 1.488 | 1.488 | take_profit | 2022-09-22 | take_profit | 2022-09-22 | 7/7 | 83.59 | 83.59 | 0 | 0.1612 | 0.1612 | 12.81 |  | True |
| cash_debit | worst | DIS | long_call | 2021-02-23 | 6 | 5.7 | 5.7 | stop | 2021-03-22 | stop | 2021-03-22 | 19/19 | -497.8 | -497.8 | 0 | 0.0806 | 0.0806 | 9.01 |  | True |
| cash_debit | random | DIS | long_call | 2020-08-31 | 3 | 8.162 | 8.162 | stop | 2020-09-23 | stop | 2020-09-23 | 16/16 | -711.1 | -711.1 | 0 | 0.0806 | 0.0806 | 11.82 |  | True |
| cash_debit | worst | GLD | bull_call | 2020-03-11 | 102 | 0.4325 | 0.4325 | stop | 2020-03-20 | stop | 2020-03-20 | 7/7 | -35.41 | -35.41 | 0 | 0.1612 | 0.1612 | 10.74 |  | True |
| cash_debit | random | GLD | bear_put | 2020-03-16 | 6 | 0.8775 | 0.8775 | stop | 2020-03-26 | stop | 2020-03-25 | 7/7 | -82.66 | -72.91 | 9.75 | 0.1612 | 0.1612 | 1.06 |  | False |
| cash_debit | worst | LIN | long_call | 2021-01-06 | 7 | 6.675 | 6.675 | stop | 2021-02-05 | stop | 2021-02-05 | 21/21 | -640.1 | -640.1 | 0 | 0.0806 | 0.0806 | 12.94 |  | True |
| cash_debit | worst | MSFT | bull_call | 2020-02-12 | 57 | 0.935 | 0.935 | stop | 2020-02-24 | stop | 2020-02-24 | 7/7 | -76.66 | -76.66 | 0 | 0.1612 | 0.1612 | 14.6 |  | True |
| cash_debit | random | MSFT | long_call | 2021-02-05 | 1 | 4.888 | 4.888 | expiry | 2021-03-12 | stop | 2021-03-04 | 18/18 | -488.8 | -477.6 | 11.21 | 0.0403 | 0.0806 | 1.43 |  | False |
| cash_debit | worst | ORCL | long_put | 2020-03-16 | 6 | 8.65 | 8.65 | expiry | 2020-04-17 | stop | 2020-04-16 | 22/22 | -865 | -861.6 | 3.46 | 0.0403 | 0.0806 | 10.39 |  | False |
| cash_debit | random | PFE | bull_call | 2021-11-16 | 5 | 0.175 | 0.175 | take_profit | 2021-11-22 | take_profit | 2021-11-22 | 4/4 | 4.84 | 4.84 | 0 | 0.1612 | 0.1612 | 1.64 |  | True |
| cash_debit | random | PFE | bull_call | 2021-04-21 | 4 | 0.2025 | 0.2025 | expiry | 2021-05-28 | stop | 2021-04-26 | 3/3 | -20.33 | -16.41 | 3.92 | 0.0806 | 0.1612 | 0.37 |  | False |
| cash_debit | worst | PYPL | long_call | 2021-03-01 | 4 | 8.363 | 8.363 | stop | 2021-03-08 | stop | 2021-03-08 | 5/5 | -766.3 | -766.3 | 0 | 0.0806 | 0.0806 | 10.96 |  | True |
| cash_debit | worst | QQQ | long_call | 2021-01-25 | 6 | 5.652 | 5.652 | stop | 2021-02-19 | stop | 2021-02-19 | 18/18 | -433.8 | -433.8 | 0 | 0.0806 | 0.0806 | 9.1 |  | True |
| cash_debit | random | QQQ | bear_put | 2022-09-23 | 1 | 1.945 | 1.945 | take_profit | 2022-10-14 | take_profit | 2022-10-14 | 15/15 | 129.6 | 129.6 | 0 | 0.1612 | 0.1612 | 15.11 |  | True |
| cash_debit | worst | SPY | bear_put | 2020-03-20 | 46 | 1.423 | 1.423 | stop | 2020-04-06 | stop | 2020-04-06 | 11/11 | -126.2 | -126.2 | 0 | 0.1612 | 0.1612 | 14.6 |  | True |
| cash_debit | worst | SPY | long_call | 2021-03-01 | 8 | 4.875 | 4.875 | stop | 2021-03-24 | stop | 2021-03-24 | 17/17 | -382.8 | -382.8 | 0 | 0.0806 | 0.0806 | 12.78 |  | True |
| cash_debit | worst | XLF | bull_call | 2020-06-09 | 169 | 0.1875 | 0.1875 | stop | 2020-06-22 | stop | 2020-06-18 | 7/7 | -18.41 | -16.16 | 2.25 | 0.1612 | 0.1612 | 14.55 |  | False |
| cash_debit | random | XOM | long_call | 2021-02-23 | 1 | 1.81 | 1.81 | take_profit | 2021-03-04 | take_profit | 2021-03-04 | 7/7 | 178.9 | 178.9 | 0 | 0.0806 | 0.0806 | 0.48 |  | True |

## Sanity summary (bt3)

`sanity_bt3_summary.csv`

| profile | n | entry_exact | exit_match | pnl_exact | sum_d_pnl_unit |
|---|---|---|---|---|---|
| cash_debit | 30 | 30 | 23 | 23 | 37.93 |
| margin | 30 | 30 | 22 | 22 | 60.52 |

## Sanity (fixed)

`sanity_fixed.csv`

| profile | sample | underlying | kind | entry_date | contracts | sim_entry_net | rp_entry_net | sim_exit_reason | exit_date | rp_exit_reason | rp_exit_day | quoted_sessions | sim_pnl_unit | rp_pnl_unit | d_pnl_unit | sim_fees_unit | rp_fees_unit | risk_pct_equity | splits_in_hold | exit_match |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| margin | worst | AAPL | iron_condor | 2021-11-08 | 9 | -1.552 | -1.552 | stop | 2021-12-07 | stop | 2021-12-07 | 20/20 | -293.3 | -293.3 | 0 | 0.3224 | 0.3224 | 10.6 |  | True |
| margin | random | AAPL | bull_put | 2021-12-09 | 9 | -0.6375 | -0.6375 | take_profit | 2021-12-27 | take_profit | 2021-12-27 | 11/11 | 50.34 | 50.34 | 0 | 0.1612 | 0.1612 | 14.37 |  | True |
| margin | random | ABNB | iron_condor | 2021-07-15 | 9 | -2.625 | -2.625 | dte_exit | 2021-08-13 | dte_exit | 2021-08-13 | 21/21 | -1.82 | -1.82 | 0 | 0.3224 | 0.3224 | 9.22 |  | True |
| margin | random | BABA | iron_condor | 2023-12-19 | 2 | -0.7225 | -0.7225 | dte_exit | 2024-01-19 | dte_exit | 2024-01-19 | 20/20 | -28.57 | -28.57 | 0 | 0.3224 | 0.3224 | 3.54 |  | True |
| margin | worst | BAC | bull_put | 2023-02-07 | 27 | -0.11 | -0.11 | stop | 2023-03-09 | stop | 2023-03-09 | 21/21 | -89.16 | -89.16 | 0 | 0.1612 | 0.1612 | 14.78 |  | True |
| margin | worst | C | iron_condor | 2022-02-11 | 25 | -0.6525 | -0.6525 | stop | 2022-03-08 | stop | 2022-03-08 | 16/16 | -182.6 | -182.6 | 0 | 0.3224 | 0.3224 | 14.85 |  | True |
| margin | worst | COST | iron_condor | 2020-01-16 | 13 | -2.163 | -2.163 | stop | 2020-02-14 | stop | 2020-02-14 | 20/20 | -238.6 | -238.6 | 0 | 0.3224 | 0.3224 | 14.55 |  | True |
| margin | random | CRWD | bull_put | 2023-11-24 | 1 | -1.113 | -1.113 | take_profit | 2023-11-30 | take_profit | 2023-11-30 | 4/4 | 100.1 | 100.1 | 0 | 0.1612 | 0.1612 | 6.64 |  | True |
| margin | worst | CSCO | iron_condor | 2020-11-11 | 31 | -0.405 | -0.405 | stop | 2020-12-03 | stop | 2020-12-03 | 15/15 | -52.07 | -52.07 | 0 | 0.3224 | 0.3224 | 8.27 |  | True |
| margin | worst | CVX | bull_put | 2022-06-09 | 7 | -0.5425 | -0.5425 | stop | 2022-06-23 | stop | 2022-06-23 | 9/9 | -385.9 | -385.9 | 0 | 0.1612 | 0.1612 | 13.72 |  | True |
| margin | worst | EWZ | bull_put | 2022-04-13 | 50 | -0.1225 | -0.1225 | stop | 2022-05-06 | stop | 2022-05-06 | 16/16 | -72.41 | -72.41 | 0 | 0.1612 | 0.1612 | 14.78 |  | True |
| margin | worst | GLD | iron_condor | 2022-02-10 | 14 | -0.64 | -0.64 | stop | 2022-03-08 | stop | 2022-03-08 | 17/17 | -219.8 | -219.8 | 0 | 0.3224 | 0.3224 | 10.44 |  | True |
| margin | worst | GLD | iron_condor | 2022-04-12 | 11 | -1.555 | -1.555 | stop | 2022-05-10 | stop | 2022-05-10 | 19/19 | -203.8 | -203.8 | 0 | 0.3224 | 0.3224 | 9.12 |  | True |
| margin | random | GLD | bull_put | 2021-05-28 | 2 | -0.7175 | -0.7175 | stop | 2021-06-17 | stop | 2021-06-17 | 13/13 | -285.9 | -285.9 | 0 | 0.1612 | 0.1612 | 2.94 |  | True |
| margin | worst | IWM | bull_put | 2021-11-08 | 9 | -1.163 | -1.163 | stop | 2021-12-01 | stop | 2021-12-01 | 16/16 | -344.7 | -344.7 | 0 | 0.1612 | 0.1612 | 11.8 |  | True |
| margin | worst | IWM | iron_condor | 2020-07-13 | 15 | -1.583 | -1.583 | stop | 2020-08-13 | stop | 2020-08-13 | 23/23 | -120.1 | -120.1 | 0 | 0.3224 | 0.3224 | 10.45 |  | True |
| margin | random | IWM | iron_condor | 2023-08-11 | 3 | -1.785 | -1.785 | take_profit | 2023-08-30 | take_profit | 2023-08-30 | 13/13 | 92.68 | 92.68 | 0 | 0.3224 | 0.3224 | 6.46 |  | True |
| margin | random | IWM | bull_put | 2022-08-08 | 9 | -0.4425 | -0.4425 | take_profit | 2022-08-12 | take_profit | 2022-08-12 | 4/4 | 22.84 | 22.84 | 0 | 0.1612 | 0.1612 | 14.43 |  | True |
| margin | random | IWM | bear_call | 2020-02-25 | 1 | -0.875 | -0.875 | take_profit | 2020-03-13 | take_profit | 2020-03-13 | 13/13 | 82.34 | 82.34 | 0 | 0.1612 | 0.1612 | 0.92 |  | True |
| margin | random | IWM | iron_condor | 2021-09-17 | 2 | -1.81 | -1.81 | take_profit | 2021-10-08 | take_profit | 2021-10-08 | 15/15 | 87.68 | 87.68 | 0 | 0.3224 | 0.3224 | 1.53 |  | True |
| margin | worst | QQQ | bull_put | 2020-02-14 | 8 | -0.5575 | -0.5575 | stop | 2020-02-28 | stop | 2020-02-28 | 9/9 | -360.9 | -360.9 | 0 | 0.1612 | 0.1612 | 13.62 |  | True |
| margin | worst | QQQ | bear_call | 2022-12-29 | 7 | -1.35 | -1.35 | stop | 2023-01-27 | stop | 2023-01-27 | 19/19 | -314.4 | -314.4 | 0 | 0.1612 | 0.1612 | 13.8 |  | True |
| margin | random | QQQ | iron_condor | 2022-09-01 | 1 | -3.105 | -3.105 | dte_exit | 2022-09-30 | dte_exit | 2022-09-30 | 20/20 | -228.8 | -228.8 | 0 | 0.3224 | 0.3224 | 1.51 |  | True |
| margin | random | SPY | bear_call | 2020-03-19 | 8 | -1.6 | -1.6 | take_profit | 2020-03-20 | take_profit | 2020-03-20 | 1/1 | 81.84 | 81.84 | 0 | 0.1612 | 0.1612 | 12.25 |  | True |
| margin | random | SPY | bull_put | 2020-12-02 | 1 | -1.115 | -1.115 | take_profit | 2020-12-24 | take_profit | 2020-12-24 | 16/16 | 62.34 | 62.34 | 0 | 0.1612 | 0.1612 | 2.98 |  | True |
| margin | random | SPY | bear_call | 2023-03-13 | 3 | -2.283 | -2.283 | stop | 2023-04-13 | stop | 2023-04-13 | 22/22 | -479.9 | -479.9 | 0 | 0.1612 | 0.1612 | 13.46 |  | True |
| margin | worst | T | bull_put | 2022-01-14 | 38 | -0.0875 | -0.0875 | dte_exit | 2022-02-11 | dte_exit | 2022-02-11 | 19/19 | -57.66 | -57.66 | 0 | 0.1612 | 0.1612 | 11.86 |  | True |
| margin | worst | TLT | iron_condor | 2021-02-12 | 11 | -1.2 | -1.2 | stop | 2021-03-12 | stop | 2021-03-12 | 19/19 | -178.1 | -178.1 | 0 | 0.3224 | 0.3224 | 8.64 |  | True |
| margin | random | TMUS | bull_put | 2023-04-14 | 1 | -0.545 | -0.545 | take_profit | 2023-05-12 | take_profit | 2023-05-12 | 20/20 | 34.84 | 34.84 | 0 | 0.1612 | 0.1612 | 4.87 |  | True |
| margin | random | WMT | iron_condor | 2020-12-09 | 5 | -1.133 | -1.133 | take_profit | 2021-01-04 | take_profit | 2021-01-04 | 16/16 | 74.93 | 74.93 | 0 | 0.3224 | 0.3224 | 9.32 |  | True |
| cash_debit | worst | AAPL | long_put | 2020-03-16 | 1 | 46.25 | 46.25 | stop | 2020-04-13 | stop | 2020-04-13 | 19/19 | -3,766 | -3,766 | 0 | 0.0806 | 0.0806 | 11.08 |  | True |
| cash_debit | random | AMD | long_call | 2020-04-17 | 1 | 4.412 | 4.412 | stop | 2020-05-04 | stop | 2020-05-04 | 11/11 | -328.1 | -328.1 | 0 | 0.0806 | 0.0806 | 4.1 |  | True |
| cash_debit | random | AMZN | bear_put | 2022-11-25 | 1 | 0.4625 | 0.4625 | take_profit | 2022-12-06 | take_profit | 2022-12-06 | 7/7 | 19.59 | 19.59 | 0 | 0.1612 | 0.1612 | 13.07 |  | True |
| cash_debit | worst | BABA | long_put | 2020-03-16 | 2 | 21.77 | 21.77 | stop | 2020-04-07 | stop | 2020-04-07 | 16/16 | -1,870 | -1,870 | 0 | 0.0806 | 0.0806 | 10.44 |  | True |
| cash_debit | worst | BAC | long_put | 2020-03-16 | 17 | 3.562 | 3.562 | stop | 2020-04-08 | stop | 2020-04-08 | 17/17 | -301.8 | -301.8 | 0 | 0.0806 | 0.0806 | 14.51 |  | True |
| cash_debit | worst | BAC | bull_call | 2020-06-09 | 62 | 0.22 | 0.22 | stop | 2020-06-22 | stop | 2020-06-22 | 9/9 | -20.66 | -20.66 | 0 | 0.1612 | 0.1612 | 11.67 |  | True |
| cash_debit | worst | C | long_call | 2020-06-09 | 6 | 2.808 | 2.808 | stop | 2020-06-11 | stop | 2020-06-11 | 2/2 | -227.3 | -227.3 | 0 | 0.0806 | 0.0806 | 14.31 |  | True |
| cash_debit | random | CSCO | bear_put | 2020-08-20 | 2 | 0.275 | 0.275 | take_profit | 2020-09-14 | take_profit | 2020-09-14 | 16/16 | 20.09 | 20.09 | 0 | 0.1612 | 0.1612 | 1.19 |  | True |
| cash_debit | worst | DIA | long_put | 2020-03-20 | 1 | 31.25 | 31.25 | stop | 2020-04-23 | stop | 2020-04-23 | 23/23 | -3,124 | -3,124 | -0 | 0.0806 | 0.0806 | 8.16 |  | True |
| cash_debit | random | DIA | long_call | 2021-04-21 | 1 | 5.525 | 5.525 | dte_exit | 2021-05-21 | dte_exit | 2021-05-21 | 22/22 | -287.8 | -287.8 | 0 | 0.0806 | 0.0806 | 13.51 |  | True |
| cash_debit | worst | DIS | long_put | 2020-03-18 | 3 | 12.35 | 12.35 | stop | 2020-04-22 | stop | 2020-04-22 | 24/24 | -1,228 | -1,228 | 0 | 0.0806 | 0.0806 | 9.3 |  | True |
| cash_debit | random | DIS | bear_put | 2021-11-23 | 1 | 1.18 | 1.18 | take_profit | 2021-11-30 | take_profit | 2021-11-30 | 4/4 | 57.84 | 57.84 | 0 | 0.1612 | 0.1612 | 13.79 |  | True |
| cash_debit | random | EEM | long_call | 2020-11-13 | 5 | 0.6925 | 0.6925 | take_profit | 2020-11-25 | take_profit | 2020-11-25 | 8/8 | 42.17 | 42.17 | 0 | 0.0806 | 0.0806 | 8.45 |  | True |
| cash_debit | worst | GLD | bull_call | 2020-03-11 | 102 | 0.4325 | 0.4325 | stop | 2020-03-20 | stop | 2020-03-20 | 7/7 | -35.41 | -35.41 | 0 | 0.1612 | 0.1612 | 10.25 |  | True |
| cash_debit | worst | GLD | bull_call | 2020-03-11 | 29 | 0.5825 | 0.5825 | stop | 2020-03-20 | stop | 2020-03-20 | 7/7 | -46.16 | -46.16 | 0 | 0.1612 | 0.1612 | 3.92 |  | True |
| cash_debit | random | IWM | bull_call | 2020-11-23 | 1 | 1.005 | 1.005 | take_profit | 2020-12-04 | take_profit | 2020-12-04 | 8/8 | 65.34 | 65.34 | 0 | 0.1612 | 0.1612 | 2.31 |  | True |
| cash_debit | random | KO | long_call | 2020-07-28 | 6 | 0.7875 | 0.7875 | stop | 2020-08-19 | stop | 2020-08-19 | 16/16 | -66.33 | -66.33 | 0 | 0.0806 | 0.0806 | 9.52 |  | True |
| cash_debit | worst | MSFT | bull_call | 2020-02-12 | 59 | 0.935 | 0.935 | stop | 2020-02-24 | stop | 2020-02-24 | 7/7 | -76.66 | -76.66 | 0 | 0.1612 | 0.1612 | 14.88 |  | True |
| cash_debit | random | MSFT | long_call | 2020-07-08 | 1 | 5.838 | 5.838 | stop | 2020-07-29 | stop | 2020-07-29 | 15/15 | -528.3 | -528.3 | 0 | 0.0806 | 0.0806 | 12.49 |  | True |
| cash_debit | worst | ORCL | long_put | 2020-03-16 | 6 | 8.65 | 8.65 | stop | 2020-04-16 | stop | 2020-04-16 | 22/22 | -861.6 | -861.6 | 0 | 0.0806 | 0.0806 | 12.44 |  | True |
| cash_debit | worst | QQQ | long_put | 2020-03-20 | 1 | 22.73 | 22.73 | stop | 2020-04-06 | stop | 2020-04-06 | 11/11 | -1,829 | -1,829 | 0 | 0.0806 | 0.0806 | 5.93 |  | True |
| cash_debit | random | QQQ | long_call | 2020-10-16 | 1 | 7.035 | 7.035 | stop | 2020-10-30 | stop | 2020-10-30 | 10/10 | -617.1 | -617.1 | 0 | 0.0806 | 0.0806 | 13.26 |  | True |
| cash_debit | random | QQQ | bear_put | 2020-09-24 | 1 | 1.725 | 1.725 | stop | 2020-10-09 | stop | 2020-10-09 | 11/11 | -140.9 | -140.9 | 0 | 0.1612 | 0.1612 | 2.46 |  | True |
| cash_debit | worst | SPY | bear_put | 2020-03-20 | 26 | 1.423 | 1.423 | stop | 2020-04-06 | stop | 2020-04-06 | 11/11 | -126.2 | -126.2 | 0 | 0.1612 | 0.1612 | 9.67 |  | True |
| cash_debit | random | SPY | long_call | 2020-08-26 | 2 | 4.465 | 4.465 | take_profit | 2020-09-02 | take_profit | 2020-09-02 | 5/5 | 572.9 | 572.9 | 0 | 0.0806 | 0.0806 | 13.25 |  | True |
| cash_debit | random | TLT | bull_call | 2020-02-19 | 1 | 0.765 | 0.765 | take_profit | 2020-02-21 | take_profit | 2020-02-21 | 2/2 | 43.34 | 43.34 | 0 | 0.1612 | 0.1612 | 0.18 |  | True |
| cash_debit | random | UBER | long_call | 2021-06-11 | 1 | 1.335 | 1.335 | stop | 2021-07-07 | stop | 2021-07-07 | 17/17 | -113.3 | -113.3 | 0 | 0.0806 | 0.0806 | 9.15 |  | True |
| cash_debit | random | WFC | long_call | 2021-01-07 | 5 | 1.65 | 1.65 | stop | 2021-01-26 | stop | 2021-01-26 | 12/12 | -133.3 | -133.3 | 0 | 0.0806 | 0.0806 | 8.91 |  | True |
| cash_debit | worst | XLF | bull_call | 2020-06-09 | 93 | 0.1875 | 0.1875 | stop | 2020-06-18 | stop | 2020-06-18 | 7/7 | -16.16 | -16.16 | 0 | 0.1612 | 0.1612 | 14.93 |  | True |
| cash_debit | worst | XOM | long_call | 2021-01-14 | 12 | 1.433 | 1.433 | stop | 2021-01-25 | stop | 2021-01-25 | 6/6 | -109.8 | -109.8 | 0 | 0.0806 | 0.0806 | 14.69 |  | True |

## Sanity summary (fixed)

`sanity_fixed_summary.csv`

| profile | n | entry_exact | exit_match | pnl_exact | sum_d_pnl_unit |
|---|---|---|---|---|---|
| cash_debit | 30 | 30 | 30 | 30 | 0 |
| margin | 30 | 30 | 30 | 30 | 0 |

## Equity reconciliation (sum pnl vs delta equity)

`reconcile.csv`

| book | profile | sum_pnl | delta_equity | diff | min_equity | min_equity_day | open_at_end |
|---|---|---|---|---|---|---|---|
| legacy | margin | -17,294 | -17,294 | -0 | 4,575 | 2023-10-27 | 0 |
| legacy | cash_debit | -24,880 | -24,880 | -0 | 120.1 | 2023-10-02 | 0 |
| marks_only | margin | -20,435 | -20,435 | -0 | 3,743 | 2023-11-20 | 0 |
| marks_only | cash_debit | -24,905 | -24,905 | -0 | 94.73 | 2023-10-02 | 0 |
| fixed | margin | -17,354 | -17,354 | -0 | 5,685 | 2023-11-20 | 0 |
| fixed | cash_debit | -24,876 | -24,876 | -0 | 114.5 | 2023-05-10 | 0 |
| est_spread | margin | -21,976 | -21,976 | -0 | 2,529 | 2023-11-20 | 0 |
| est_spread | cash_debit | -24,897 | -24,897 | 0 | 103 | 2023-10-02 | 0 |
| half_size | margin | -13,487 | -13,487 | 0 | 9,393 | 2023-07-19 | 0 |
| half_size | cash_debit | -24,721 | -24,721 | -0 | 274 | 2023-11-30 | 0 |
| half_slots | margin | -21,285 | -21,285 | -0 | 3,366 | 2023-11-24 | 0 |
| half_slots | cash_debit | -24,903 | -24,903 | -0 | 97.24 | 2023-10-02 | 0 |
| day_risk_cap | margin | -13,483 | -13,483 | -0 | 8,612 | 2023-11-20 | 0 |
| day_risk_cap | cash_debit | -24,795 | -24,795 | -0 | 146.7 | 2023-11-24 | 0 |
| no_bull_put__no_bull_call | margin | -13,966 | -13,966 | -0 | 8,487 | 2023-10-27 | 0 |
| no_bull_put__no_bull_call | cash_debit | -24,858 | -24,858 | 0 | 142.4 | 2023-08-30 | 0 |
| no_bear_call__no_bear_put | margin | -15,582 | -15,582 | -0 | 4,951 | 2023-10-27 | 0 |
| no_bear_call__no_bear_put | cash_debit | -24,846 | -24,846 | -0 | 129.1 | 2023-11-28 | 0 |
| no_iron_condor__no_long_call | margin | -20,501 | -20,501 | -0 | 4,112 | 2023-11-21 | 0 |
| no_iron_condor__no_long_call | cash_debit | -24,880 | -24,880 | -0 | 120.3 | 2023-10-02 | 0 |
| no_long_put | margin | -17,354 | -17,354 | -0 | 5,685 | 2023-11-20 | 0 |
| no_long_put | cash_debit | -24,877 | -24,877 | -0 | 114.1 | 2023-05-10 | 0 |
| bt3 | margin | -17,294 | -17,294 | -0 | 4,575 | 2023-10-27 | 0 |
| bt3 | cash_debit | -24,880 | -24,880 | -0 | 120.1 | 2023-10-02 | 0 |

## Decomposition bt3 margin

`decomp_bt3_margin.csv`

| cut | class | bucket | n | net | mean | median | win | pnl_per_risk | share_of_net |
|---|---|---|---|---|---|---|---|---|---|
| kind | All | bear_call | 53 | 2,386 | 45 | 146.1 | 0.77 | 0.04 | 0.138 |
| kind | All | bull_put | 207 | -12,457 | -60.2 | 40.1 | 0.82 | -0.057 | -0.72 |
| kind | All | iron_condor | 180 | -7,223 | -40.1 | 90.4 | 0.62 | -0.044 | -0.418 |
| kind | Stocks | bear_call | 5 | 738 | 147.5 | 191.8 | 1 | 0.129 | 0.043 |
| kind | Stocks | bull_put | 55 | -3,371 | -61.3 | 67.7 | 0.82 | -0.048 | -0.195 |
| kind | Stocks | iron_condor | 57 | -1,167 | -20.5 | 110.2 | 0.65 | -0.02 | -0.067 |
| kind | ETFs | bear_call | 48 | 1,648 | 34.3 | 140.3 | 0.75 | 0.031 | 0.095 |
| kind | ETFs | bull_put | 152 | -9,087 | -59.8 | 38.3 | 0.82 | -0.061 | -0.525 |
| kind | ETFs | iron_condor | 123 | -6,057 | -49.2 | 82.9 | 0.6 | -0.057 | -0.35 |
| spec | All | bear_call_d16_w2_dte30-45 | 1 | 28 | 28.1 | 28.1 | 1 | 0.06 | 0.002 |
| spec | All | bear_call_d20_w2_dte30-45 | 1 | 88 | 87.7 | 87.7 | 1 | 0.133 | 0.005 |
| spec | All | bear_call_d25_w2_dte30-45 | 7 | -221 | -31.6 | 98.8 | 0.71 | -0.041 | -0.013 |
| spec | All | bear_call_d30_w2_dte30-45 | 44 | 2,491 | 56.6 | 190 | 0.77 | 0.047 | 0.144 |
| spec | All | bull_put_d16_w2_dte30-45 | 85 | -10,591 | -124.6 | 33.3 | 0.79 | -0.125 | -0.612 |
| spec | All | bull_put_d20_w2_dte30-45 | 53 | -6,678 | -126 | 42.8 | 0.83 | -0.124 | -0.386 |
| spec | All | bull_put_d25_w2_dte30-45 | 33 | 1,404 | 42.6 | 62.6 | 0.91 | 0.049 | 0.081 |
| spec | All | bull_put_d30_w2_dte30-45 | 36 | 3,408 | 94.7 | 81.4 | 0.81 | 0.067 | 0.197 |
| spec | All | iron_condor_d16_w2_dte30-45 | 19 | 1,278 | 67.2 | 93.2 | 0.84 | 0.074 | 0.074 |
| spec | All | iron_condor_d20_w2_dte30-45 | 24 | -1,817 | -75.7 | 97.3 | 0.75 | -0.087 | -0.105 |
| spec | All | iron_condor_d25_w2_dte30-45 | 42 | -2,362 | -56.2 | -35.1 | 0.48 | -0.062 | -0.137 |
| spec | All | iron_condor_d30_w2_dte30-45 | 95 | -4,322 | -45.5 | 138.9 | 0.6 | -0.048 | -0.25 |
| spec | Stocks | bear_call_d16_w2_dte30-45 | 1 | 28 | 28.1 | 28.1 | 1 | 0.06 | 0.002 |
| spec | Stocks | bear_call_d30_w2_dte30-45 | 4 | 710 | 177.4 | 200.6 | 1 | 0.135 | 0.041 |
| spec | Stocks | bull_put_d16_w2_dte30-45 | 26 | -5,457 | -209.9 | 34.1 | 0.73 | -0.187 | -0.316 |
| spec | Stocks | bull_put_d20_w2_dte30-45 | 11 | -3,893 | -353.9 | 67.7 | 0.82 | -0.304 | -0.225 |
| spec | Stocks | bull_put_d25_w2_dte30-45 | 7 | 1,508 | 215.5 | 170.7 | 1 | 0.158 | 0.087 |
| spec | Stocks | bull_put_d30_w2_dte30-45 | 11 | 4,471 | 406.5 | 511.1 | 0.91 | 0.237 | 0.259 |
| spec | Stocks | iron_condor_d16_w2_dte30-45 | 8 | 686 | 85.8 | 159.1 | 0.75 | 0.07 | 0.04 |
| spec | Stocks | iron_condor_d20_w2_dte30-45 | 9 | -2,227 | -247.5 | 65.8 | 0.67 | -0.234 | -0.129 |
| spec | Stocks | iron_condor_d25_w2_dte30-45 | 16 | -681 | -42.5 | 84.5 | 0.56 | -0.04 | -0.039 |
| spec | Stocks | iron_condor_d30_w2_dte30-45 | 24 | 1,055 | 44 | 240.3 | 0.67 | 0.047 | 0.061 |
| spec | ETFs | bear_call_d20_w2_dte30-45 | 1 | 88 | 87.7 | 87.7 | 1 | 0.133 | 0.005 |
| spec | ETFs | bear_call_d25_w2_dte30-45 | 7 | -221 | -31.6 | 98.8 | 0.71 | -0.041 | -0.013 |
| spec | ETFs | bear_call_d30_w2_dte30-45 | 40 | 1,782 | 44.5 | 168.4 | 0.75 | 0.037 | 0.103 |
| spec | ETFs | bull_put_d16_w2_dte30-45 | 59 | -5,134 | -87 | 33.3 | 0.81 | -0.092 | -0.297 |
| spec | ETFs | bull_put_d20_w2_dte30-45 | 42 | -2,785 | -66.3 | 40.6 | 0.83 | -0.068 | -0.161 |
| spec | ETFs | bull_put_d25_w2_dte30-45 | 26 | -104 | -4 | 59.5 | 0.88 | -0.005 | -0.006 |
| spec | ETFs | bull_put_d30_w2_dte30-45 | 25 | -1,064 | -42.6 | 47.3 | 0.76 | -0.033 | -0.062 |
| spec | ETFs | iron_condor_d16_w2_dte30-45 | 11 | 591 | 53.8 | 82.9 | 0.91 | 0.079 | 0.034 |
| spec | ETFs | iron_condor_d20_w2_dte30-45 | 15 | 411 | 27.4 | 107.4 | 0.8 | 0.036 | 0.024 |
| spec | ETFs | iron_condor_d25_w2_dte30-45 | 26 | -1,682 | -64.7 | -112.1 | 0.42 | -0.08 | -0.097 |
| spec | ETFs | iron_condor_d30_w2_dte30-45 | 71 | -5,377 | -75.7 | 122.4 | 0.58 | -0.081 | -0.311 |
| trend | All | bear | 53 | 2,386 | 45 | 146.1 | 0.77 | 0.04 | 0.138 |
| trend | All | bull | 207 | -12,457 | -60.2 | 40.1 | 0.82 | -0.057 | -0.72 |
| trend | All | sideways | 180 | -7,223 | -40.1 | 90.4 | 0.62 | -0.044 | -0.418 |
| trend | Stocks | bear | 5 | 738 | 147.5 | 191.8 | 1 | 0.129 | 0.043 |
| trend | Stocks | bull | 55 | -3,371 | -61.3 | 67.7 | 0.82 | -0.048 | -0.195 |
| trend | Stocks | sideways | 57 | -1,167 | -20.5 | 110.2 | 0.65 | -0.02 | -0.067 |
| trend | ETFs | bear | 48 | 1,648 | 34.3 | 140.3 | 0.75 | 0.031 | 0.095 |
| trend | ETFs | bull | 152 | -9,087 | -59.8 | 38.3 | 0.82 | -0.061 | -0.525 |
| trend | ETFs | sideways | 123 | -6,057 | -49.2 | 82.9 | 0.6 | -0.057 | -0.35 |
| vol | All | high | 80 | 6,324 | 79.1 | 145.5 | 0.71 | 0.067 | 0.366 |
| vol | All | low | 275 | -8,607 | -31.3 | 48.2 | 0.75 | -0.034 | -0.498 |
| vol | All | mid | 85 | -15,011 | -176.6 | 47.3 | 0.69 | -0.16 | -0.868 |
| vol | Stocks | high | 7 | 2,589 | 369.9 | 346.8 | 1 | 0.236 | 0.15 |
| vol | Stocks | low | 93 | -511 | -5.5 | 86.7 | 0.75 | -0.005 | -0.03 |
| vol | Stocks | mid | 17 | -5,878 | -345.8 | 45.7 | 0.59 | -0.207 | -0.34 |
| vol | ETFs | high | 73 | 3,735 | 51.2 | 129.7 | 0.68 | 0.045 | 0.216 |
| vol | ETFs | low | 182 | -8,097 | -44.5 | 40 | 0.75 | -0.051 | -0.468 |
| vol | ETFs | mid | 68 | -9,133 | -134.3 | 48 | 0.72 | -0.139 | -0.528 |
| year | All | 2020 | 112 | -10,806 | -96.5 | 41.6 | 0.72 | -0.096 | -0.625 |
| year | All | 2021 | 136 | 8,573 | 63 | 85.1 | 0.82 | 0.055 | 0.496 |
| year | All | 2022 | 77 | -13,266 | -172.3 | 112.4 | 0.64 | -0.126 | -0.767 |
| year | All | 2023 | 115 | -1,795 | -15.6 | 38.1 | 0.7 | -0.026 | -0.104 |
| year | Stocks | 2020 | 16 | -6,772 | -423.2 | 15.6 | 0.5 | -0.283 | -0.392 |
| year | Stocks | 2021 | 46 | 7,545 | 164 | 122.3 | 0.87 | 0.116 | 0.436 |
| year | Stocks | 2022 | 8 | -6,328 | -791 | 68.3 | 0.62 | -0.384 | -0.366 |
| year | Stocks | 2023 | 47 | 1,756 | 37.4 | 61.7 | 0.72 | 0.059 | 0.102 |
| year | ETFs | 2020 | 96 | -4,034 | -42 | 41.6 | 0.76 | -0.045 | -0.233 |
| year | ETFs | 2021 | 90 | 1,028 | 11.4 | 67.1 | 0.79 | 0.011 | 0.059 |
| year | ETFs | 2022 | 69 | -6,938 | -100.6 | 116.9 | 0.64 | -0.078 | -0.401 |
| year | ETFs | 2023 | 68 | -3,550 | -52.2 | 33.3 | 0.69 | -0.09 | -0.205 |
| period | All | 2020 crash | 38 | -8,898 | -234.2 | 42 | 0.63 | -0.17 | -0.514 |
| period | All | 2020 rebound | 74 | -1,908 | -25.8 | 41.6 | 0.77 | -0.031 | -0.11 |
| period | All | 2021 bull | 136 | 8,573 | 63 | 85.1 | 0.82 | 0.055 | 0.496 |
| period | All | 2022 bear | 77 | -13,266 | -172.3 | 112.4 | 0.64 | -0.126 | -0.767 |
| period | All | 2023 | 115 | -1,795 | -15.6 | 38.1 | 0.7 | -0.026 | -0.104 |
| period | Stocks | 2020 crash | 9 | -7,107 | -789.7 | -76.2 | 0.33 | -0.495 | -0.411 |
| period | Stocks | 2020 rebound | 7 | 335 | 47.9 | 397.6 | 0.71 | 0.035 | 0.019 |
| period | Stocks | 2021 bull | 46 | 7,545 | 164 | 122.3 | 0.87 | 0.116 | 0.436 |
| period | Stocks | 2022 bear | 8 | -6,328 | -791 | 68.3 | 0.62 | -0.384 | -0.366 |
| period | Stocks | 2023 | 47 | 1,756 | 37.4 | 61.7 | 0.72 | 0.059 | 0.102 |
| period | ETFs | 2020 crash | 29 | -1,791 | -61.8 | 50.7 | 0.72 | -0.047 | -0.104 |
| period | ETFs | 2020 rebound | 67 | -2,243 | -33.5 | 41.1 | 0.78 | -0.044 | -0.13 |
| period | ETFs | 2021 bull | 90 | 1,028 | 11.4 | 67.1 | 0.79 | 0.011 | 0.059 |
| period | ETFs | 2022 bear | 69 | -6,938 | -100.6 | 116.9 | 0.64 | -0.078 | -0.401 |
| period | ETFs | 2023 | 68 | -3,550 | -52.2 | 33.3 | 0.69 | -0.09 | -0.205 |
| tier | All | core | 311 | -397 | -1.3 | 62.6 | 0.75 | -0.001 | -0.023 |
| tier | All | momentum | 11 | -1,295 | -117.7 | 35.7 | 0.64 | -0.112 | -0.075 |
| tier | All | rest | 118 | -15,602 | -132.2 | 40.3 | 0.7 | -0.123 | -0.902 |
| tier | Stocks | core | 46 | 3,329 | 72.4 | 107.1 | 0.83 | 0.059 | 0.193 |
| tier | Stocks | momentum | 11 | -1,295 | -117.7 | 35.7 | 0.64 | -0.112 | -0.075 |
| tier | Stocks | rest | 60 | -5,834 | -97.2 | 66.7 | 0.7 | -0.086 | -0.337 |
| tier | ETFs | core | 265 | -3,727 | -14.1 | 57.3 | 0.73 | -0.015 | -0.215 |
| tier | ETFs | rest | 58 | -9,768 | -168.4 | 37.2 | 0.71 | -0.164 | -0.565 |
| liquidity | All | 50k-500k | 149 | -18,315 | -122.9 | 38.1 | 0.68 | -0.127 | -1.059 |
| liquidity | All | 5k-50k | 46 | -3,267 | -71 | 67.8 | 0.74 | -0.056 | -0.189 |
| liquidity | All | <5k | 2 | 146 | 73 | 73 | 1 | 0.109 | 0.008 |
| liquidity | All | >=500k | 243 | 4,142 | 17 | 71.5 | 0.76 | 0.017 | 0.239 |
| liquidity | Stocks | 50k-500k | 57 | -2,484 | -43.6 | 61.7 | 0.7 | -0.041 | -0.144 |
| liquidity | Stocks | 5k-50k | 38 | -3,618 | -95.2 | 88.6 | 0.71 | -0.075 | -0.209 |
| liquidity | Stocks | <5k | 2 | 146 | 73 | 73 | 1 | 0.109 | 0.008 |
| liquidity | Stocks | >=500k | 20 | 2,156 | 107.8 | 114.5 | 0.9 | 0.088 | 0.125 |
| liquidity | ETFs | 50k-500k | 92 | -15,831 | -172.1 | 33.4 | 0.67 | -0.191 | -0.915 |
| liquidity | ETFs | 5k-50k | 8 | 350 | 43.8 | 27.8 | 0.88 | 0.033 | 0.02 |
| liquidity | ETFs | >=500k | 223 | 1,985 | 8.9 | 66.2 | 0.74 | 0.009 | 0.115 |
| earnings_in_hold | All | etf | 323 | -13,495 | -41.8 | 48.6 | 0.73 | -0.044 | -0.78 |
| earnings_in_hold | All | no | 86 | -6,236 | -72.5 | 70.7 | 0.76 | -0.063 | -0.361 |
| earnings_in_hold | All | yes | 31 | 2,437 | 78.6 | 130.3 | 0.71 | 0.068 | 0.141 |
| earnings_in_hold | Stocks | no | 86 | -6,236 | -72.5 | 70.7 | 0.76 | -0.063 | -0.361 |
| earnings_in_hold | Stocks | yes | 31 | 2,437 | 78.6 | 130.3 | 0.71 | 0.068 | 0.141 |
| earnings_in_hold | ETFs | etf | 323 | -13,495 | -41.8 | 48.6 | 0.73 | -0.044 | -0.78 |
| exit_reason | All | dte_exit | 44 | -12,191 | -277.1 | -133.4 | 0.27 | -0.237 | -0.705 |
| exit_reason | All | expiry | 71 | -31,500 | -443.7 | -204.5 | 0.3 | -0.472 | -1.821 |
| exit_reason | All | stop | 36 | -29,031 | -806.4 | -516.6 | 0 | -0.849 | -1.679 |
| exit_reason | All | take_profit | 289 | 55,428 | 191.8 | 93.7 | 1 | 0.19 | 3.205 |
| exit_reason | Stocks | dte_exit | 8 | -1,214 | -151.7 | 99 | 0.62 | -0.107 | -0.07 |
| exit_reason | Stocks | expiry | 31 | -14,111 | -455.2 | -76.2 | 0.35 | -0.43 | -0.816 |
| exit_reason | Stocks | stop | 7 | -5,491 | -784.5 | -586.6 | 0 | -0.887 | -0.318 |
| exit_reason | Stocks | take_profit | 71 | 17,017 | 239.7 | 120.2 | 1 | 0.201 | 0.984 |
| exit_reason | ETFs | dte_exit | 36 | -10,977 | -304.9 | -174.9 | 0.19 | -0.275 | -0.635 |
| exit_reason | ETFs | expiry | 40 | -17,389 | -434.7 | -297.2 | 0.25 | -0.513 | -1.005 |
| exit_reason | ETFs | stop | 29 | -23,539 | -811.7 | -511.3 | 0 | -0.841 | -1.361 |
| exit_reason | ETFs | take_profit | 218 | 38,411 | 176.2 | 89.6 | 1 | 0.186 | 2.221 |
| capacity_clipped | All | False | 276 | -5,149 | -18.7 | 75.9 | 0.75 | -0.016 | -0.298 |
| capacity_clipped | All | True | 164 | -12,145 | -74.1 | 38 | 0.7 | -0.108 | -0.702 |
| capacity_clipped | Stocks | False | 80 | -6,532 | -81.6 | 111.8 | 0.74 | -0.059 | -0.378 |
| capacity_clipped | Stocks | True | 37 | 2,732 | 73.8 | 52.1 | 0.76 | 0.112 | 0.158 |
| capacity_clipped | ETFs | False | 196 | 1,383 | 7.1 | 69.1 | 0.76 | 0.006 | 0.08 |
| capacity_clipped | ETFs | True | 127 | -14,877 | -117.1 | 36.1 | 0.68 | -0.168 | -0.86 |

## Costs bt3 margin

`costs_bt3_margin.csv`

| kind | n | gross_at_mid | slippage_cost | fees | net | cost_share_of_gross | median_entry_spread_pct_of_premium |
|---|---|---|---|---|---|---|---|
| all | 440 | -6,079 | 10,872 | 344 | -17,294 | 1.85 | 12.2 |
| bear_call | 53 | 3,735 | 1,320 | 29 | 2,386 | 0.36 | 8.7 |
| bull_put | 207 | -8,168 | 4,167 | 123 | -12,457 | 0.53 | 14.6 |
| iron_condor | 180 | -1,646 | 5,386 | 191 | -7,223 | 3.39 | 11.2 |

## Calibration by kind bt3 margin

`calib_bt3_margin.csv`

| kind | n | pred_ev_per_trade | real_pnl_per_trade | pred_pop | real_win | brier | brier_base_rate | corr_ev_pnl_unit |
|---|---|---|---|---|---|---|---|---|
| all | 440 | 23 | -39.3 | 0.775 | 0.732 | 0.191 | 0.1963 | -0.02 |
| bear_call | 53 | 24.5 | 45 | 0.77 | 0.774 | 0.1729 | 0.1752 | 0.019 |
| bull_put | 207 | 15.9 | -60.2 | 0.862 | 0.821 | 0.1573 | 0.1468 | -0.211 |
| iron_condor | 180 | 30.8 | -40.1 | 0.676 | 0.617 | 0.2351 | 0.2364 | 0.053 |

## Calibration by EV decile bt3 margin

`calib_decile_bt3_margin.csv`

| decile | n | pred_ev_unit | real_pnl_unit | pred_pop | real_win |
|---|---|---|---|---|---|
| 1 | 44 | 0.286 | 13.24 | 0.777 | 0.795 |
| 2 | 44 | 0.94 | -22.25 | 0.758 | 0.682 |
| 3 | 44 | 1.833 | 1.299 | 0.768 | 0.75 |
| 4 | 44 | 2.841 | -4.704 | 0.771 | 0.773 |
| 5 | 44 | 4.141 | -17.26 | 0.773 | 0.727 |
| 6 | 44 | 5.924 | -25.6 | 0.779 | 0.727 |
| 7 | 44 | 8.139 | 8.264 | 0.762 | 0.75 |
| 8 | 44 | 11.03 | -14.77 | 0.786 | 0.75 |
| 9 | 44 | 15.59 | -71.15 | 0.826 | 0.705 |
| 10 | 44 | 32.06 | 8.412 | 0.75 | 0.659 |

## Tail bt3 margin

`tail_bt3_margin.csv`

| losers | gross_loss | top_1pct_trades_share_of_loss | top_5pct_trades_share_of_loss | top_10pct_trades_share_of_loss | gross_win | peak_day | peak_equity | trough_day | trough_equity | first_below_half | mean_open_risk_pct | p90_open_risk_pct | mean_same_direction_share |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 118 | 81,889 | 0.167 | 0.551 | 0.768 | 64,595 | 2020-02-18 | 26,919 | 2023-10-27 | 4,575 | 2020-10-16 | 48 | 75.5 | 0.72 |

## Worst days bt3 margin

`worst_days_bt3_margin.csv`

| day | equity_change | pct_of_equity | open | bullish | bearish | neutral | risk_pct_equity | names |
|---|---|---|---|---|---|---|---|---|
| 2020-02-21 | -3,330 | -12.4 | 8 | 3 | 0 | 5 | 44 | GLD,IWM,JNJ,KO,QQQ,SPY,T,VZ |
| 2022-09-13 | -2,886 | -17.7 | 9 | 6 | 0 | 3 | 41 | DIA,IWM,QQQ,SPY |
| 2021-11-30 | -2,671 | -13 | 10 | 7 | 0 | 3 | 88 | AAPL,DIA,GM,IWM,JPM,MU,QQQ,TQQQ |
| 2021-11-26 | -2,405 | -10.5 | 10 | 7 | 0 | 3 | 79 | AAPL,DIA,GM,IWM,JPM,MU,QQQ,TQQQ |
| 2021-11-15 | -2,244 | -9.9 | 10 | 6 | 0 | 4 | 103 | AAPL,AMD,BA,DIA,GM,IWM,JPM,QQQ,TQQQ |
| 2020-10-16 | -2,080 | -15.8 | 9 | 0 | 2 | 7 | 69 | CSCO,DIS,PFE,QQQ,SPY,T |
| 2022-03-18 | -1,981 | -8.3 | 5 | 0 | 0 | 5 | 20 | IWM,QQQ,SPY |
| 2022-05-20 | -1,931 | -10.1 | 4 | 0 | 2 | 2 | 14 | IWM |

## Decomposition bt3 cash_debit

`decomp_bt3_cash_debit.csv`

| cut | class | bucket | n | net | mean | median | win | pnl_per_risk | share_of_net |
|---|---|---|---|---|---|---|---|---|---|
| kind | All | bear_put | 122 | -19,174 | -157.2 | -44 | 0.37 | -0.361 | -0.771 |
| kind | All | bull_call | 190 | -3,611 | -19 | 12.8 | 0.53 | -0.031 | -0.145 |
| kind | All | long_call | 257 | 12,925 | 50.3 | 48.9 | 0.55 | 0.05 | 0.52 |
| kind | All | long_put | 16 | -15,020 | -938.8 | -94.6 | 0.44 | -0.388 | -0.604 |
| kind | Stocks | bear_put | 73 | -4,096 | -56.1 | -2.7 | 0.45 | -0.215 | -0.165 |
| kind | Stocks | bull_call | 123 | -4,293 | -34.9 | -3.5 | 0.48 | -0.06 | -0.173 |
| kind | Stocks | long_call | 193 | -8,940 | -46.3 | 27.8 | 0.54 | -0.049 | -0.359 |
| kind | Stocks | long_put | 13 | -17,318 | -1,332 | -4.6 | 0.46 | -0.59 | -0.696 |
| kind | ETFs | bear_put | 49 | -15,078 | -307.7 | -140.9 | 0.24 | -0.442 | -0.606 |
| kind | ETFs | bull_call | 67 | 683 | 10.2 | 64.1 | 0.63 | 0.015 | 0.027 |
| kind | ETFs | long_call | 64 | 21,865 | 341.6 | 295.5 | 0.59 | 0.273 | 0.879 |
| kind | ETFs | long_put | 3 | 2,298 | 766 | -873.8 | 0.33 | 0.245 | 0.092 |
| spec | All | bear_put_d20_w2_dte10-60 | 36 | -782 | -21.7 | -2.6 | 0.47 | -0.089 | -0.031 |
| spec | All | bear_put_d25_w2_dte10-60 | 34 | -9,845 | -289.5 | -29.4 | 0.41 | -0.597 | -0.396 |
| spec | All | bear_put_d30_w2_dte10-60 | 31 | -5,950 | -191.9 | -149.4 | 0.29 | -0.495 | -0.239 |
| spec | All | bear_put_d35_w2_dte10-60 | 21 | -2,598 | -123.7 | -128.2 | 0.24 | -0.163 | -0.104 |
| spec | All | bull_call_d20_w2_dte10-60 | 130 | -5,100 | -39.2 | -2.8 | 0.49 | -0.058 | -0.205 |
| spec | All | bull_call_d25_w2_dte10-60 | 34 | -1,472 | -43.3 | 16.8 | 0.56 | -0.101 | -0.059 |
| spec | All | bull_call_d30_w2_dte10-60 | 17 | 2,043 | 120.2 | 166 | 0.65 | 0.234 | 0.082 |
| spec | All | bull_call_d35_w2_dte10-60 | 9 | 918 | 102 | 111.5 | 0.78 | 0.171 | 0.037 |
| spec | All | long_call_d40_dte10-60 | 172 | 11,384 | 66.2 | 58.2 | 0.56 | 0.064 | 0.458 |
| spec | All | long_call_d55_dte10-60 | 71 | 4,417 | 62.2 | 27.3 | 0.55 | 0.062 | 0.178 |
| spec | All | long_call_d70_dte10-60 | 14 | -2,876 | -205.4 | -73.2 | 0.5 | -0.245 | -0.116 |
| spec | All | long_put_d40_dte10-60 | 4 | 228 | 57 | 65.1 | 0.5 | 0.144 | 0.009 |
| spec | All | long_put_d55_dte10-60 | 5 | -6,412 | -1,282 | -4.6 | 0.4 | -0.768 | -0.258 |
| spec | All | long_put_d70_dte10-60 | 7 | -8,837 | -1,262 | -1,829 | 0.43 | -0.307 | -0.355 |
| spec | Stocks | bear_put_d20_w2_dte10-60 | 33 | -528 | -16 | 2.3 | 0.52 | -0.063 | -0.021 |
| spec | Stocks | bear_put_d25_w2_dte10-60 | 25 | -1,602 | -64.1 | 8.8 | 0.52 | -0.251 | -0.064 |
| spec | Stocks | bear_put_d30_w2_dte10-60 | 10 | -1,573 | -157.3 | -79.6 | 0.3 | -0.513 | -0.063 |
| spec | Stocks | bear_put_d35_w2_dte10-60 | 5 | -394 | -78.8 | -15.7 | 0 | -0.339 | -0.016 |
| spec | Stocks | bull_call_d20_w2_dte10-60 | 94 | -3,755 | -39.9 | -10.7 | 0.45 | -0.063 | -0.151 |
| spec | Stocks | bull_call_d25_w2_dte10-60 | 20 | -2,688 | -134.4 | -6.5 | 0.45 | -0.357 | -0.108 |
| spec | Stocks | bull_call_d30_w2_dte10-60 | 6 | 1,710 | 285.1 | 329.8 | 0.83 | 0.413 | 0.069 |
| spec | Stocks | bull_call_d35_w2_dte10-60 | 3 | 439 | 146.4 | 111.5 | 1 | 0.559 | 0.018 |
| spec | Stocks | long_call_d40_dte10-60 | 116 | -11,668 | -100.6 | 33.2 | 0.53 | -0.111 | -0.469 |
| spec | Stocks | long_call_d55_dte10-60 | 63 | 5,603 | 88.9 | 45.2 | 0.57 | 0.088 | 0.225 |
| spec | Stocks | long_call_d70_dte10-60 | 14 | -2,876 | -205.4 | -73.2 | 0.5 | -0.245 | -0.116 |
| spec | Stocks | long_put_d40_dte10-60 | 4 | 228 | 57 | 65.1 | 0.5 | 0.144 | 0.009 |
| spec | Stocks | long_put_d55_dte10-60 | 4 | -5,538 | -1,384 | 20.4 | 0.5 | -0.759 | -0.223 |
| spec | Stocks | long_put_d70_dte10-60 | 5 | -12,008 | -2,402 | -3,766 | 0.4 | -0.587 | -0.483 |
| spec | ETFs | bear_put_d20_w2_dte10-60 | 3 | -254 | -84.6 | -68.5 | 0 | -0.826 | -0.01 |
| spec | ETFs | bear_put_d25_w2_dte10-60 | 9 | -8,243 | -915.9 | -147.7 | 0.11 | -0.814 | -0.331 |
| spec | ETFs | bear_put_d30_w2_dte10-60 | 21 | -4,377 | -208.4 | -163.4 | 0.29 | -0.489 | -0.176 |
| spec | ETFs | bear_put_d35_w2_dte10-60 | 16 | -2,204 | -137.8 | -128.8 | 0.31 | -0.15 | -0.089 |
| spec | ETFs | bull_call_d20_w2_dte10-60 | 36 | -1,345 | -37.4 | 63.9 | 0.61 | -0.048 | -0.054 |
| spec | ETFs | bull_call_d25_w2_dte10-60 | 14 | 1,216 | 86.9 | 50.2 | 0.71 | 0.172 | 0.049 |
| spec | ETFs | bull_call_d30_w2_dte10-60 | 11 | 333 | 30.2 | 64.1 | 0.55 | 0.072 | 0.013 |
| spec | ETFs | bull_call_d35_w2_dte10-60 | 6 | 479 | 79.8 | 157.8 | 0.67 | 0.105 | 0.019 |
| spec | ETFs | long_call_d40_dte10-60 | 56 | 23,052 | 411.6 | 457.5 | 0.62 | 0.316 | 0.927 |
| spec | ETFs | long_call_d55_dte10-60 | 8 | -1,187 | -148.3 | -161.8 | 0.38 | -0.163 | -0.048 |
| spec | ETFs | long_put_d55_dte10-60 | 1 | -874 | -873.8 | -873.8 | 0 | -0.829 | -0.035 |
| spec | ETFs | long_put_d70_dte10-60 | 2 | 3,172 | 1,586 | 1,586 | 0.5 | 0.38 | 0.127 |
| trend | All | bear | 138 | -34,195 | -247.8 | -44 | 0.38 | -0.372 | -1.374 |
| trend | All | bull | 447 | 9,315 | 20.8 | 27.8 | 0.54 | 0.025 | 0.374 |
| trend | Stocks | bear | 86 | -21,414 | -249 | -3.7 | 0.45 | -0.443 | -0.861 |
| trend | Stocks | bull | 316 | -13,233 | -41.9 | 11.7 | 0.52 | -0.052 | -0.532 |
| trend | ETFs | bear | 52 | -12,780 | -245.8 | -144.3 | 0.25 | -0.294 | -0.514 |
| trend | ETFs | bull | 131 | 22,548 | 172.1 | 79.7 | 0.61 | 0.181 | 0.906 |
| vol | All | high | 281 | -39,672 | -141.2 | -1.8 | 0.49 | -0.169 | -1.595 |
| vol | All | low | 150 | 4,464 | 29.8 | 44.6 | 0.55 | 0.034 | 0.179 |
| vol | All | mid | 154 | 10,328 | 67.1 | -3.1 | 0.48 | 0.101 | 0.415 |
| vol | Stocks | high | 215 | -24,798 | -115.3 | 15.3 | 0.54 | -0.143 | -0.997 |
| vol | Stocks | low | 93 | -8,093 | -87 | -10.9 | 0.48 | -0.109 | -0.325 |
| vol | Stocks | mid | 94 | -1,757 | -18.7 | -18.3 | 0.44 | -0.033 | -0.071 |
| vol | ETFs | high | 66 | -14,874 | -225.4 | -90.7 | 0.35 | -0.241 | -0.598 |
| vol | ETFs | low | 57 | 12,558 | 220.3 | 79.7 | 0.65 | 0.215 | 0.505 |
| vol | ETFs | mid | 60 | 12,084 | 201.4 | 51.9 | 0.55 | 0.251 | 0.486 |
| year | All | 2020 | 197 | 13,661 | 69.3 | 77.8 | 0.59 | 0.056 | 0.549 |
| year | All | 2021 | 200 | -35,148 | -175.7 | -63.8 | 0.45 | -0.19 | -1.413 |
| year | All | 2022 | 144 | -2,903 | -20.2 | 5 | 0.52 | -0.078 | -0.117 |
| year | All | 2023 | 44 | -490 | -11.1 | -18.9 | 0.32 | -0.246 | -0.02 |
| year | Stocks | 2020 | 92 | -5,395 | -58.6 | 22.4 | 0.53 | -0.04 | -0.217 |
| year | Stocks | 2021 | 156 | -28,861 | -185 | -12.3 | 0.48 | -0.21 | -1.16 |
| year | Stocks | 2022 | 113 | -62 | -0.6 | 18.6 | 0.57 | -0.002 | -0.003 |
| year | Stocks | 2023 | 41 | -329 | -8 | -17.2 | 0.34 | -0.182 | -0.013 |
| year | ETFs | 2020 | 105 | 19,056 | 181.5 | 133.2 | 0.65 | 0.171 | 0.766 |
| year | ETFs | 2021 | 44 | -6,287 | -142.9 | -206.3 | 0.32 | -0.132 | -0.253 |
| year | ETFs | 2022 | 31 | -2,840 | -91.6 | -87.4 | 0.35 | -0.318 | -0.114 |
| year | ETFs | 2023 | 3 | -161 | -53.7 | -47.2 | 0 | -0.904 | -0.006 |
| period | All | 2020 crash | 80 | -9,914 | -123.9 | 49.1 | 0.59 | -0.087 | -0.398 |
| period | All | 2020 rebound | 117 | 23,575 | 201.5 | 133.2 | 0.6 | 0.179 | 0.948 |
| period | All | 2021 bull | 200 | -35,148 | -175.7 | -63.8 | 0.45 | -0.19 | -1.413 |
| period | All | 2022 bear | 144 | -2,903 | -20.2 | 5 | 0.52 | -0.078 | -0.117 |
| period | All | 2023 | 44 | -490 | -11.1 | -18.9 | 0.32 | -0.246 | -0.02 |
| period | Stocks | 2020 crash | 29 | -14,194 | -489.4 | -129.8 | 0.41 | -0.248 | -0.57 |
| period | Stocks | 2020 rebound | 63 | 8,798 | 139.7 | 93.6 | 0.59 | 0.115 | 0.354 |
| period | Stocks | 2021 bull | 156 | -28,861 | -185 | -12.3 | 0.48 | -0.21 | -1.16 |
| period | Stocks | 2022 bear | 113 | -62 | -0.6 | 18.6 | 0.57 | -0.002 | -0.003 |
| period | Stocks | 2023 | 41 | -329 | -8 | -17.2 | 0.34 | -0.182 | -0.013 |
| period | ETFs | 2020 crash | 51 | 4,280 | 83.9 | 79.7 | 0.69 | 0.076 | 0.172 |
| period | ETFs | 2020 rebound | 54 | 14,776 | 273.6 | 283.3 | 0.61 | 0.269 | 0.594 |
| period | ETFs | 2021 bull | 44 | -6,287 | -142.9 | -206.3 | 0.32 | -0.132 | -0.253 |
| period | ETFs | 2022 bear | 31 | -2,840 | -91.6 | -87.4 | 0.35 | -0.318 | -0.114 |
| period | ETFs | 2023 | 3 | -161 | -53.7 | -47.2 | 0 | -0.904 | -0.006 |
| tier | All | core | 258 | -12,955 | -50.2 | -46.2 | 0.45 | -0.054 | -0.521 |
| tier | All | momentum | 32 | -7,769 | -242.8 | -24.9 | 0.47 | -0.427 | -0.312 |
| tier | All | rest | 295 | -4,156 | -14.1 | 30.3 | 0.56 | -0.02 | -0.167 |
| tier | Stocks | core | 145 | -19,360 | -133.5 | -19.9 | 0.45 | -0.163 | -0.778 |
| tier | Stocks | momentum | 32 | -7,769 | -242.8 | -24.9 | 0.47 | -0.427 | -0.312 |
| tier | Stocks | rest | 225 | -7,519 | -33.4 | 15.3 | 0.54 | -0.046 | -0.302 |
| tier | ETFs | core | 113 | 6,405 | 56.7 | -75.3 | 0.44 | 0.052 | 0.257 |
| tier | ETFs | rest | 70 | 3,362 | 48 | 51.8 | 0.61 | 0.074 | 0.135 |
| liquidity | All | 50k-500k | 355 | -9,069 | -25.5 | 8.8 | 0.52 | -0.036 | -0.365 |
| liquidity | All | 5k-50k | 75 | -3,896 | -51.9 | 17.4 | 0.56 | -0.075 | -0.157 |
| liquidity | All | <5k | 18 | -5,650 | -313.9 | -175.1 | 0.5 | -0.183 | -0.227 |
| liquidity | All | >=500k | 137 | -6,264 | -45.7 | -51.7 | 0.45 | -0.046 | -0.252 |
| liquidity | Stocks | 50k-500k | 267 | -15,879 | -59.5 | -2.7 | 0.49 | -0.083 | -0.638 |
| liquidity | Stocks | 5k-50k | 68 | -3,211 | -47.2 | 18 | 0.57 | -0.069 | -0.129 |
| liquidity | Stocks | <5k | 17 | -7,295 | -429.1 | -362.6 | 0.47 | -0.262 | -0.293 |
| liquidity | Stocks | >=500k | 50 | -8,262 | -165.2 | -1.7 | 0.5 | -0.233 | -0.332 |
| liquidity | ETFs | 50k-500k | 88 | 6,810 | 77.4 | 59.2 | 0.6 | 0.113 | 0.274 |
| liquidity | ETFs | 5k-50k | 7 | -684 | -97.7 | -88.8 | 0.43 | -0.123 | -0.028 |
| liquidity | ETFs | <5k | 1 | 1,644 | 1,644 | 1,644 | 1 | 0.546 | 0.066 |
| liquidity | ETFs | >=500k | 87 | 1,998 | 23 | -112.9 | 0.41 | 0.02 | 0.08 |
| earnings_in_hold | All | etf | 183 | 9,768 | 53.4 | 34.4 | 0.51 | 0.058 | 0.393 |
| earnings_in_hold | All | no | 363 | -25,283 | -69.7 | 12.4 | 0.53 | -0.092 | -1.016 |
| earnings_in_hold | All | yes | 39 | -9,364 | -240.1 | -152.6 | 0.26 | -0.339 | -0.376 |
| earnings_in_hold | Stocks | no | 363 | -25,283 | -69.7 | 12.4 | 0.53 | -0.092 | -1.016 |
| earnings_in_hold | Stocks | yes | 39 | -9,364 | -240.1 | -152.6 | 0.26 | -0.339 | -0.376 |
| earnings_in_hold | ETFs | etf | 183 | 9,768 | 53.4 | 34.4 | 0.51 | 0.058 | 0.393 |
| exit_reason | All | dte_exit | 22 | -4,732 | -215.1 | -89.4 | 0.27 | -0.233 | -0.19 |
| exit_reason | All | expiry | 18 | -14,801 | -822.3 | -334.5 | 0 | -1 | -0.595 |
| exit_reason | All | profit_lock | 34 | -609 | -17.9 | -3.1 | 0.32 | -0.048 | -0.024 |
| exit_reason | All | stop | 231 | -167,871 | -726.7 | -348.3 | 0 | -0.822 | -6.747 |
| exit_reason | All | take_profit | 280 | 163,133 | 582.6 | 288.4 | 0.99 | 0.751 | 6.557 |
| exit_reason | Stocks | dte_exit | 16 | -1,818 | -113.6 | -69.2 | 0.25 | -0.128 | -0.073 |
| exit_reason | Stocks | expiry | 13 | -12,936 | -995.1 | -362.6 | 0 | -1 | -0.52 |
| exit_reason | Stocks | profit_lock | 30 | -533 | -17.8 | -2.6 | 0.33 | -0.048 | -0.021 |
| exit_reason | Stocks | stop | 153 | -114,340 | -747.3 | -380.3 | 0 | -0.827 | -4.596 |
| exit_reason | Stocks | take_profit | 190 | 94,980 | 499.9 | 247.8 | 0.99 | 0.763 | 3.818 |
| exit_reason | ETFs | dte_exit | 6 | -2,914 | -485.7 | -151.2 | 0.33 | -0.482 | -0.117 |
| exit_reason | ETFs | expiry | 5 | -1,865 | -373 | -216.3 | 0 | -1 | -0.075 |
| exit_reason | ETFs | profit_lock | 4 | -76 | -18.9 | -38 | 0.25 | -0.045 | -0.003 |
| exit_reason | ETFs | stop | 78 | -53,531 | -686.3 | -322.4 | 0 | -0.812 | -2.152 |
| exit_reason | ETFs | take_profit | 90 | 68,154 | 757.3 | 465.8 | 1 | 0.736 | 2.739 |
| capacity_clipped | All | False | 378 | -22,271 | -58.9 | -8 | 0.48 | -0.065 | -0.895 |
| capacity_clipped | All | True | 207 | -2,609 | -12.6 | 26.7 | 0.55 | -0.021 | -0.105 |
| capacity_clipped | Stocks | False | 290 | -22,194 | -76.5 | 1.3 | 0.5 | -0.094 | -0.892 |
| capacity_clipped | Stocks | True | 112 | -12,454 | -111.2 | 0 | 0.5 | -0.195 | -0.501 |
| capacity_clipped | ETFs | False | 88 | -77 | -0.9 | -90 | 0.41 | -0.001 | -0.003 |
| capacity_clipped | ETFs | True | 95 | 9,845 | 103.6 | 64.1 | 0.6 | 0.158 | 0.396 |

## Costs bt3 cash_debit

`costs_bt3_cash_debit.csv`

| kind | n | gross_at_mid | slippage_cost | fees | net | cost_share_of_gross | median_entry_spread_pct_of_premium |
|---|---|---|---|---|---|---|---|
| all | 585 | 4,608 | 28,911 | 578 | -24,880 | 6.4 | 9.5 |
| bear_put | 122 | -9,922 | 9,181 | 72 | -19,174 | 0.93 | 21.8 |
| bull_call | 190 | 8,971 | 12,157 | 425 | -3,611 | 1.4 | 22.2 |
| long_call | 257 | 19,919 | 6,917 | 77 | 12,925 | 0.35 | 6.6 |
| long_put | 16 | -14,360 | 656 | 4 | -15,020 | 0.05 | 4.5 |

## Calibration by kind bt3 cash_debit

`calib_bt3_cash_debit.csv`

| kind | n | pred_ev_per_trade | real_pnl_per_trade | pred_pop | real_win | brier | brier_base_rate | corr_ev_pnl_unit |
|---|---|---|---|---|---|---|---|---|
| all | 585 | 82.2 | -42.5 | 0.602 | 0.504 | 0.2538 | 0.25 | -0.011 |
| bear_put | 122 | 15.6 | -157.2 | 0.6 | 0.369 | 0.2856 | 0.2328 | -0.079 |
| bull_call | 190 | 60.7 | -19 | 0.621 | 0.532 | 0.2446 | 0.249 | 0.334 |
| long_call | 257 | 131.3 | 50.3 | 0.591 | 0.553 | 0.2453 | 0.2472 | -0.002 |
| long_put | 16 | 56.6 | -938.8 | 0.547 | 0.438 | 0.2581 | 0.2461 | -0.307 |

## Calibration by EV decile bt3 cash_debit

`calib_decile_bt3_cash_debit.csv`

| decile | n | pred_ev_unit | real_pnl_unit | pred_pop | real_win |
|---|---|---|---|---|---|
| 1 | 59 | 0.344 | -16.27 | 0.587 | 0.39 |
| 2 | 58 | 1.031 | -17.84 | 0.579 | 0.397 |
| 3 | 59 | 2.149 | -8.81 | 0.597 | 0.475 |
| 4 | 58 | 3.987 | 13.72 | 0.589 | 0.414 |
| 5 | 59 | 6.037 | -65.25 | 0.601 | 0.458 |
| 6 | 58 | 9.673 | 62.33 | 0.601 | 0.638 |
| 7 | 58 | 14.43 | 14.71 | 0.62 | 0.621 |
| 8 | 59 | 22.5 | 8.408 | 0.614 | 0.525 |
| 9 | 58 | 43.51 | 42.93 | 0.604 | 0.569 |
| 10 | 59 | 123.6 | -41.34 | 0.623 | 0.559 |

## Tail bt3 cash_debit

`tail_bt3_cash_debit.csv`

| losers | gross_loss | top_1pct_trades_share_of_loss | top_5pct_trades_share_of_loss | top_10pct_trades_share_of_loss | gross_win | peak_day | peak_equity | trough_day | trough_equity | first_below_half | mean_open_risk_pct | p90_open_risk_pct | mean_same_direction_share |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 290 | 191,350 | 0.163 | 0.462 | 0.656 | 166,470 | 2020-03-23 | 46,065 | 2023-10-02 | 120 | 2020-06-11 | 54.4 | 87.5 | 0.88 |

## Worst days bt3 cash_debit

`worst_days_bt3_cash_debit.csv`

| day | equity_change | pct_of_equity | open | bullish | bearish | neutral | risk_pct_equity | names |
|---|---|---|---|---|---|---|---|---|
| 2020-03-24 | -13,867 | -30.1 | 10 | 1 | 9 | 0 | 73 | AAPL,BABA,BAC,GLD,IWM,NFLX,ORCL,QQQ,SPY |
| 2020-04-06 | -8,454 | -31 | 5 | 0 | 5 | 0 | 89 | AAPL,BABA,BAC,IWM,ORCL |
| 2020-06-10 | -6,389 | -29.9 | 10 | 10 | 0 | 0 | 80 | BAC,C,GM,JPM,NVDA,WFC,XLF |
| 2020-03-26 | -6,098 | -19.1 | 9 | 1 | 8 | 0 | 104 | AAPL,BABA,BAC,GLD,IWM,NFLX,ORCL,QQQ,SPY |
| 2020-03-17 | -5,338 | -11.6 | 10 | 4 | 6 | 0 | 66 | AAPL,BABA,BAC,GLD,NFLX,ORCL |
| 2020-06-11 | -5,006 | -33.4 | 9 | 9 | 0 | 0 | 93 | BAC,GM,JPM,NVDA,WFC,XLF |
| 2020-03-30 | -4,739 | -16.4 | 7 | 0 | 7 | 0 | 114 | AAPL,BABA,BAC,IWM,ORCL,QQQ,SPY |
| 2020-04-02 | -4,614 | -15 | 7 | 0 | 7 | 0 | 107 | AAPL,BABA,BAC,IWM,ORCL,QQQ,SPY |

## Decomposition fixed margin

`decomp_fixed_margin.csv`

| cut | class | bucket | n | net | mean | median | win | pnl_per_risk | share_of_net |
|---|---|---|---|---|---|---|---|---|---|
| kind | All | bear_call | 56 | 1,473 | 26.3 | 118.7 | 0.73 | 0.019 | 0.085 |
| kind | All | bull_put | 200 | -7,512 | -37.6 | 47 | 0.83 | -0.028 | -0.433 |
| kind | All | iron_condor | 213 | -11,315 | -53.1 | 82.9 | 0.61 | -0.047 | -0.652 |
| kind | Stocks | bear_call | 4 | 665 | 166.2 | 172.2 | 1 | 0.128 | 0.038 |
| kind | Stocks | bull_put | 49 | 2,621 | 53.5 | 114.4 | 0.9 | 0.032 | 0.151 |
| kind | Stocks | iron_condor | 59 | -6,614 | -112.1 | 62 | 0.63 | -0.075 | -0.381 |
| kind | ETFs | bear_call | 52 | 808 | 15.5 | 118.7 | 0.71 | 0.011 | 0.047 |
| kind | ETFs | bull_put | 151 | -10,134 | -67.1 | 40.6 | 0.81 | -0.054 | -0.584 |
| kind | ETFs | iron_condor | 154 | -4,702 | -30.5 | 85.9 | 0.6 | -0.031 | -0.271 |
| spec | All | bear_call_d20_w2_dte30-45 | 3 | 228 | 76.1 | 87.7 | 1 | 0.119 | 0.013 |
| spec | All | bear_call_d25_w2_dte30-45 | 8 | -600 | -75.1 | 67.6 | 0.75 | -0.087 | -0.035 |
| spec | All | bear_call_d30_w2_dte30-45 | 45 | 1,845 | 41 | 188.2 | 0.71 | 0.026 | 0.106 |
| spec | All | bull_put_d16_w2_dte30-45 | 80 | -8,515 | -106.4 | 37.6 | 0.85 | -0.077 | -0.491 |
| spec | All | bull_put_d20_w2_dte30-45 | 53 | -2,961 | -55.9 | 48.3 | 0.81 | -0.046 | -0.171 |
| spec | All | bull_put_d25_w2_dte30-45 | 33 | 2,436 | 73.8 | 90.7 | 0.91 | 0.065 | 0.14 |
| spec | All | bull_put_d30_w2_dte30-45 | 34 | 1,527 | 44.9 | 55.9 | 0.76 | 0.027 | 0.088 |
| spec | All | iron_condor_d16_w2_dte30-45 | 17 | 675 | 39.7 | 77.4 | 0.82 | 0.038 | 0.039 |
| spec | All | iron_condor_d20_w2_dte30-45 | 30 | -3,769 | -125.6 | 90.1 | 0.77 | -0.105 | -0.217 |
| spec | All | iron_condor_d25_w2_dte30-45 | 48 | -9,178 | -191.2 | -71.4 | 0.46 | -0.164 | -0.529 |
| spec | All | iron_condor_d30_w2_dte30-45 | 118 | 956 | 8.1 | 121 | 0.6 | 0.007 | 0.055 |
| spec | Stocks | bear_call_d20_w2_dte30-45 | 1 | 53 | 52.6 | 52.6 | 1 | 0.12 | 0.003 |
| spec | Stocks | bear_call_d25_w2_dte30-45 | 1 | 12 | 12.1 | 12.1 | 1 | 0.146 | 0.001 |
| spec | Stocks | bear_call_d30_w2_dte30-45 | 2 | 600 | 300.1 | 300.1 | 1 | 0.128 | 0.035 |
| spec | Stocks | bull_put_d16_w2_dte30-45 | 22 | -5,020 | -228.2 | 36.3 | 0.82 | -0.133 | -0.289 |
| spec | Stocks | bull_put_d20_w2_dte30-45 | 8 | 913 | 114.1 | 117.3 | 1 | 0.099 | 0.053 |
| spec | Stocks | bull_put_d25_w2_dte30-45 | 10 | 2,651 | 265.1 | 191.1 | 1 | 0.152 | 0.153 |
| spec | Stocks | bull_put_d30_w2_dte30-45 | 9 | 4,078 | 453.1 | 530 | 0.89 | 0.222 | 0.235 |
| spec | Stocks | iron_condor_d16_w2_dte30-45 | 7 | 453 | 64.8 | 30.4 | 0.86 | 0.051 | 0.026 |
| spec | Stocks | iron_condor_d20_w2_dte30-45 | 11 | -2,249 | -204.5 | 130.3 | 0.82 | -0.115 | -0.13 |
| spec | Stocks | iron_condor_d25_w2_dte30-45 | 18 | -3,617 | -200.9 | -22 | 0.5 | -0.142 | -0.208 |
| spec | Stocks | iron_condor_d30_w2_dte30-45 | 23 | -1,201 | -52.2 | 62 | 0.57 | -0.035 | -0.069 |
| spec | ETFs | bear_call_d20_w2_dte30-45 | 2 | 176 | 87.9 | 87.9 | 1 | 0.119 | 0.01 |
| spec | ETFs | bear_call_d25_w2_dte30-45 | 7 | -612 | -87.5 | 76.3 | 0.71 | -0.09 | -0.035 |
| spec | ETFs | bear_call_d30_w2_dte30-45 | 43 | 1,245 | 29 | 162.2 | 0.7 | 0.019 | 0.072 |
| spec | ETFs | bull_put_d16_w2_dte30-45 | 58 | -3,495 | -60.3 | 38.2 | 0.86 | -0.048 | -0.201 |
| spec | ETFs | bull_put_d20_w2_dte30-45 | 45 | -3,873 | -86.1 | 44.3 | 0.78 | -0.07 | -0.223 |
| spec | ETFs | bull_put_d25_w2_dte30-45 | 23 | -215 | -9.3 | 62.3 | 0.87 | -0.011 | -0.012 |
| spec | ETFs | bull_put_d30_w2_dte30-45 | 25 | -2,551 | -102 | 42.8 | 0.72 | -0.067 | -0.147 |
| spec | ETFs | iron_condor_d16_w2_dte30-45 | 10 | 222 | 22.2 | 81.1 | 0.8 | 0.024 | 0.013 |
| spec | ETFs | iron_condor_d20_w2_dte30-45 | 19 | -1,520 | -80 | 87.2 | 0.74 | -0.093 | -0.088 |
| spec | ETFs | iron_condor_d25_w2_dte30-45 | 30 | -5,561 | -185.4 | -91.7 | 0.43 | -0.182 | -0.32 |
| spec | ETFs | iron_condor_d30_w2_dte30-45 | 95 | 2,157 | 22.7 | 138.9 | 0.61 | 0.023 | 0.124 |
| trend | All | bear | 56 | 1,473 | 26.3 | 118.7 | 0.73 | 0.019 | 0.085 |
| trend | All | bull | 200 | -7,512 | -37.6 | 47 | 0.83 | -0.028 | -0.433 |
| trend | All | sideways | 213 | -11,315 | -53.1 | 82.9 | 0.61 | -0.047 | -0.652 |
| trend | Stocks | bear | 4 | 665 | 166.2 | 172.2 | 1 | 0.128 | 0.038 |
| trend | Stocks | bull | 49 | 2,621 | 53.5 | 114.4 | 0.9 | 0.032 | 0.151 |
| trend | Stocks | sideways | 59 | -6,614 | -112.1 | 62 | 0.63 | -0.075 | -0.381 |
| trend | ETFs | bear | 52 | 808 | 15.5 | 118.7 | 0.71 | 0.011 | 0.047 |
| trend | ETFs | bull | 151 | -10,134 | -67.1 | 40.6 | 0.81 | -0.054 | -0.584 |
| trend | ETFs | sideways | 154 | -4,702 | -30.5 | 85.9 | 0.6 | -0.031 | -0.271 |
| vol | All | high | 81 | 12,247 | 151.2 | 124.9 | 0.7 | 0.102 | 0.706 |
| vol | All | low | 281 | -9,374 | -33.4 | 59.9 | 0.75 | -0.028 | -0.54 |
| vol | All | mid | 107 | -20,228 | -189 | 45.6 | 0.64 | -0.151 | -1.166 |
| vol | Stocks | high | 7 | 3,495 | 499.2 | 453 | 0.86 | 0.227 | 0.201 |
| vol | Stocks | low | 88 | 454 | 5.2 | 75.6 | 0.77 | 0.004 | 0.026 |
| vol | Stocks | mid | 17 | -7,276 | -428 | 63.3 | 0.65 | -0.218 | -0.419 |
| vol | ETFs | high | 74 | 8,753 | 118.3 | 115.1 | 0.69 | 0.084 | 0.504 |
| vol | ETFs | low | 193 | -9,828 | -50.9 | 52.4 | 0.75 | -0.048 | -0.566 |
| vol | ETFs | mid | 90 | -12,952 | -143.9 | 45.3 | 0.64 | -0.129 | -0.746 |
| year | All | 2020 | 121 | -1,500 | -12.4 | 48.6 | 0.73 | -0.011 | -0.086 |
| year | All | 2021 | 135 | 6,698 | 49.6 | 89.7 | 0.81 | 0.033 | 0.386 |
| year | All | 2022 | 86 | -13,875 | -161.3 | 117.3 | 0.6 | -0.095 | -0.799 |
| year | All | 2023 | 127 | -8,677 | -68.3 | 39.5 | 0.7 | -0.087 | -0.5 |
| year | Stocks | 2020 | 16 | -804 | -50.3 | 268.3 | 0.69 | -0.031 | -0.046 |
| year | Stocks | 2021 | 40 | 10,882 | 272.1 | 139.9 | 0.95 | 0.135 | 0.627 |
| year | Stocks | 2022 | 10 | -6,298 | -629.8 | 270.7 | 0.7 | -0.231 | -0.363 |
| year | Stocks | 2023 | 46 | -7,107 | -154.5 | 28.9 | 0.63 | -0.168 | -0.41 |
| year | ETFs | 2020 | 105 | -696 | -6.6 | 42.8 | 0.73 | -0.006 | -0.04 |
| year | ETFs | 2021 | 95 | -4,184 | -44 | 73.9 | 0.75 | -0.035 | -0.241 |
| year | ETFs | 2022 | 76 | -7,577 | -99.7 | 109.2 | 0.59 | -0.064 | -0.437 |
| year | ETFs | 2023 | 81 | -1,570 | -19.4 | 45.2 | 0.74 | -0.027 | -0.09 |
| period | All | 2020 crash | 41 | -4,542 | -110.8 | 48.6 | 0.68 | -0.08 | -0.262 |
| period | All | 2020 rebound | 80 | 3,042 | 38 | 52.5 | 0.75 | 0.037 | 0.175 |
| period | All | 2021 bull | 135 | 6,698 | 49.6 | 89.7 | 0.81 | 0.033 | 0.386 |
| period | All | 2022 bear | 86 | -13,875 | -161.3 | 117.3 | 0.6 | -0.095 | -0.799 |
| period | All | 2023 | 127 | -8,677 | -68.3 | 39.5 | 0.7 | -0.087 | -0.5 |
| period | Stocks | 2020 crash | 9 | -1,904 | -211.5 | 35.9 | 0.56 | -0.178 | -0.11 |
| period | Stocks | 2020 rebound | 7 | 1,099 | 157.1 | 374.6 | 0.86 | 0.073 | 0.063 |
| period | Stocks | 2021 bull | 40 | 10,882 | 272.1 | 139.9 | 0.95 | 0.135 | 0.627 |
| period | Stocks | 2022 bear | 10 | -6,298 | -629.8 | 270.7 | 0.7 | -0.231 | -0.363 |
| period | Stocks | 2023 | 46 | -7,107 | -154.5 | 28.9 | 0.63 | -0.168 | -0.41 |
| period | ETFs | 2020 crash | 32 | -2,638 | -82.4 | 49.6 | 0.72 | -0.057 | -0.152 |
| period | ETFs | 2020 rebound | 73 | 1,942 | 26.6 | 42.3 | 0.74 | 0.028 | 0.112 |
| period | ETFs | 2021 bull | 95 | -4,184 | -44 | 73.9 | 0.75 | -0.035 | -0.241 |
| period | ETFs | 2022 bear | 76 | -7,577 | -99.7 | 109.2 | 0.59 | -0.064 | -0.437 |
| period | ETFs | 2023 | 81 | -1,570 | -19.4 | 45.2 | 0.74 | -0.027 | -0.09 |
| tier | All | core | 339 | 4,404 | 13 | 82.9 | 0.74 | 0.011 | 0.254 |
| tier | All | momentum | 12 | -2,911 | -242.5 | 54.7 | 0.67 | -0.136 | -0.168 |
| tier | All | rest | 118 | -18,848 | -159.7 | 36.1 | 0.66 | -0.121 | -1.086 |
| tier | Stocks | core | 43 | 3,240 | 75.4 | 130.3 | 0.84 | 0.041 | 0.187 |
| tier | Stocks | momentum | 12 | -2,911 | -242.5 | 54.7 | 0.67 | -0.136 | -0.168 |
| tier | Stocks | rest | 57 | -3,657 | -64.2 | 45.7 | 0.72 | -0.048 | -0.211 |
| tier | ETFs | core | 296 | 1,164 | 3.9 | 79.4 | 0.73 | 0.004 | 0.067 |
| tier | ETFs | rest | 61 | -15,191 | -249 | 32.2 | 0.61 | -0.19 | -0.875 |
| liquidity | All | 50k-500k | 155 | -22,720 | -146.6 | 38.3 | 0.68 | -0.117 | -1.309 |
| liquidity | All | 5k-50k | 43 | 1,556 | 36.2 | 95.1 | 0.77 | 0.02 | 0.09 |
| liquidity | All | <5k | 1 | 35 | 34.8 | 34.8 | 1 | 0.078 | 0.002 |
| liquidity | All | >=500k | 270 | 3,774 | 14 | 82.8 | 0.73 | 0.012 | 0.217 |
| liquidity | Stocks | 50k-500k | 60 | -2,862 | -47.7 | 67.9 | 0.78 | -0.034 | -0.165 |
| liquidity | Stocks | 5k-50k | 35 | 112 | 3.2 | 126.6 | 0.74 | 0.002 | 0.006 |
| liquidity | Stocks | <5k | 1 | 35 | 34.8 | 34.8 | 1 | 0.078 | 0.002 |
| liquidity | Stocks | >=500k | 16 | -612 | -38.3 | 88 | 0.69 | -0.021 | -0.035 |
| liquidity | ETFs | 50k-500k | 95 | -19,858 | -209 | 34.3 | 0.62 | -0.184 | -1.144 |
| liquidity | ETFs | 5k-50k | 8 | 1,444 | 180.5 | 30.5 | 0.88 | 0.095 | 0.083 |
| liquidity | ETFs | >=500k | 254 | 4,386 | 17.3 | 82.8 | 0.74 | 0.015 | 0.253 |
| earnings_in_hold | All | etf | 357 | -14,027 | -39.3 | 62.3 | 0.71 | -0.034 | -0.808 |
| earnings_in_hold | All | no | 85 | -6,687 | -78.7 | 83.4 | 0.78 | -0.048 | -0.385 |
| earnings_in_hold | All | yes | 27 | 3,360 | 124.4 | 124.2 | 0.7 | 0.088 | 0.194 |
| earnings_in_hold | Stocks | no | 85 | -6,687 | -78.7 | 83.4 | 0.78 | -0.048 | -0.385 |
| earnings_in_hold | Stocks | yes | 27 | 3,360 | 124.4 | 124.2 | 0.7 | 0.088 | 0.194 |
| earnings_in_hold | ETFs | etf | 357 | -14,027 | -39.3 | 62.3 | 0.71 | -0.034 | -0.808 |
| exit_reason | All | dte_exit | 77 | -21,663 | -281.3 | -122.7 | 0.26 | -0.252 | -1.248 |
| exit_reason | All | expiry | 7 | 610 | 87.1 | -76.2 | 0.43 | 0.055 | 0.035 |
| exit_reason | All | stop | 70 | -74,838 | -1,069 | -634.7 | 0 | -0.871 | -4.312 |
| exit_reason | All | take_profit | 315 | 78,536 | 249.3 | 122.9 | 1 | 0.194 | 4.525 |
| exit_reason | Stocks | dte_exit | 15 | -5,732 | -382.1 | -57.1 | 0.27 | -0.238 | -0.33 |
| exit_reason | Stocks | expiry | 6 | 903 | 150.5 | 130.5 | 0.5 | 0.109 | 0.052 |
| exit_reason | Stocks | stop | 13 | -21,612 | -1,662 | -1,382 | 0 | -0.905 | -1.245 |
| exit_reason | Stocks | take_profit | 78 | 23,114 | 296.3 | 130.5 | 1 | 0.193 | 1.332 |
| exit_reason | ETFs | dte_exit | 62 | -15,931 | -256.9 | -137 | 0.26 | -0.257 | -0.918 |
| exit_reason | ETFs | expiry | 1 | -293 | -293.1 | -293.1 | 0 | -0.103 | -0.017 |
| exit_reason | ETFs | stop | 57 | -53,225 | -933.8 | -571.8 | 0 | -0.859 | -3.067 |
| exit_reason | ETFs | take_profit | 237 | 55,422 | 233.8 | 113.3 | 1 | 0.195 | 3.194 |
| capacity_clipped | All | False | 259 | -4,340 | -16.8 | 107.2 | 0.74 | -0.01 | -0.25 |
| capacity_clipped | All | True | 210 | -13,015 | -62 | 38 | 0.7 | -0.08 | -0.75 |
| capacity_clipped | Stocks | False | 65 | -7,529 | -115.8 | 130.8 | 0.74 | -0.056 | -0.434 |
| capacity_clipped | Stocks | True | 47 | 4,202 | 89.4 | 37 | 0.79 | 0.103 | 0.242 |
| capacity_clipped | ETFs | False | 194 | 3,189 | 16.4 | 99.8 | 0.74 | 0.011 | 0.184 |
| capacity_clipped | ETFs | True | 163 | -17,216 | -105.6 | 38.1 | 0.67 | -0.141 | -0.992 |

## Costs fixed margin

`costs_fixed_margin.csv`

| kind | n | gross_at_mid | slippage_cost | fees | net | cost_share_of_gross | median_entry_spread_pct_of_premium |
|---|---|---|---|---|---|---|---|
| all | 469 | 1,685 | 18,522 | 517 | -17,354 | 11.3 | 11.6 |
| bear_call | 56 | 3,306 | 1,792 | 41 | 1,473 | 0.55 | 8.9 |
| bull_put | 200 | -2,254 | 5,108 | 151 | -7,512 | 2.33 | 13.7 |
| iron_condor | 213 | 633 | 11,622 | 326 | -11,315 | 18.89 | 10.9 |

## Calibration by kind fixed margin

`calib_fixed_margin.csv`

| kind | n | pred_ev_per_trade | real_pnl_per_trade | pred_pop | real_win | brier | brier_base_rate | corr_ev_pnl_unit |
|---|---|---|---|---|---|---|---|---|
| all | 469 | 29 | -37 | 0.766 | 0.721 | 0.194 | 0.2013 | -0.039 |
| bear_call | 56 | 29.2 | 26.3 | 0.77 | 0.732 | 0.1974 | 0.1961 | -0.224 |
| bull_put | 200 | 20.1 | -37.6 | 0.863 | 0.835 | 0.1444 | 0.1378 | -0.114 |
| iron_condor | 213 | 37.2 | -53.1 | 0.674 | 0.61 | 0.2397 | 0.2378 | 0.005 |

## Calibration by EV decile fixed margin

`calib_decile_fixed_margin.csv`

| decile | n | pred_ev_unit | real_pnl_unit | pred_pop | real_win |
|---|---|---|---|---|---|
| 1 | 47 | 0.302 | 11.72 | 0.774 | 0.809 |
| 2 | 47 | 1.005 | -18.12 | 0.752 | 0.681 |
| 3 | 47 | 1.934 | -7.219 | 0.766 | 0.745 |
| 4 | 47 | 2.975 | 2.085 | 0.75 | 0.723 |
| 5 | 47 | 4.5 | -35.23 | 0.769 | 0.702 |
| 6 | 46 | 6.556 | -7.75 | 0.783 | 0.761 |
| 7 | 47 | 8.741 | 16.84 | 0.756 | 0.745 |
| 8 | 47 | 11.57 | -17.11 | 0.794 | 0.723 |
| 9 | 47 | 16.77 | -13.84 | 0.785 | 0.723 |
| 10 | 47 | 37.99 | -14.36 | 0.736 | 0.596 |

## Tail fixed margin

`tail_fixed_margin.csv`

| losers | gross_loss | top_1pct_trades_share_of_loss | top_5pct_trades_share_of_loss | top_10pct_trades_share_of_loss | gross_win | peak_day | peak_equity | trough_day | trough_equity | first_below_half | mean_open_risk_pct | p90_open_risk_pct | mean_same_direction_share |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 131 | 100,083 | 0.175 | 0.521 | 0.787 | 82,729 | 2021-10-13 | 34,084 | 2023-11-20 | 5,685 | 2023-03-15 | 46.1 | 70.5 | 0.75 |

## Worst days fixed margin

`worst_days_fixed_margin.csv`

| day | equity_change | pct_of_equity | open | bullish | bearish | neutral | risk_pct_equity | names |
|---|---|---|---|---|---|---|---|---|
| 2022-09-13 | -3,174 | -14.3 | 10 | 6 | 0 | 4 | 49 | DIA,EWZ,IWM,QQQ,SPY |
| 2021-11-15 | -3,054 | -10.2 | 10 | 6 | 1 | 3 | 91 | AAPL,AMD,BA,DIA,IWM,JPM,QQQ,SPY,TQQQ |
| 2023-04-13 | -2,955 | -26.4 | 7 | 3 | 2 | 2 | 65 | AAPL,DIA,GLD,IWM,TQQQ,WMT |
| 2021-11-30 | -2,858 | -9.8 | 9 | 6 | 0 | 3 | 66 | AAPL,DIA,IWM,JPM,MU,QQQ,TQQQ,WMT |
| 2022-03-08 | -2,400 | -8.5 | 7 | 0 | 0 | 7 | 24 | GLD,IWM,QQQ,SPY |
| 2022-10-28 | -2,240 | -11.5 | 4 | 0 | 4 | 0 | 20 | IWM,QQQ,SPY |
| 2020-03-13 | -2,074 | -9.4 | 6 | 1 | 3 | 2 | 47 | GLD,HD,KO,QQQ,SPY |
| 2021-02-25 | -2,074 | -9.2 | 7 | 3 | 0 | 4 | 65 | IWM,QQQ,SPY,TLT,XLK |

## Decomposition fixed cash_debit

`decomp_fixed_cash_debit.csv`

| cut | class | bucket | n | net | mean | median | win | pnl_per_risk | share_of_net |
|---|---|---|---|---|---|---|---|---|---|
| kind | All | bear_put | 70 | -4,564 | -65.2 | -2.3 | 0.47 | -0.208 | -0.183 |
| kind | All | bull_call | 170 | 165 | 1 | 24.5 | 0.59 | 0.003 | 0.007 |
| kind | All | long_call | 172 | 6,136 | 35.7 | -41.7 | 0.46 | 0.068 | 0.247 |
| kind | All | long_put | 19 | -26,613 | -1,401 | -493 | 0.21 | -0.714 | -1.07 |
| kind | Stocks | bear_put | 52 | -1,699 | -32.7 | 2 | 0.5 | -0.321 | -0.068 |
| kind | Stocks | bull_call | 113 | -1,807 | -16 | 5.8 | 0.52 | -0.064 | -0.073 |
| kind | Stocks | long_call | 117 | -6,435 | -55 | -79.6 | 0.42 | -0.115 | -0.259 |
| kind | Stocks | long_put | 14 | -20,450 | -1,461 | -241.9 | 0.21 | -0.683 | -0.822 |
| kind | ETFs | bear_put | 18 | -2,865 | -159.2 | -110.9 | 0.39 | -0.172 | -0.115 |
| kind | ETFs | bull_call | 57 | 1,972 | 34.6 | 67.6 | 0.72 | 0.073 | 0.079 |
| kind | ETFs | long_call | 55 | 12,571 | 228.6 | 65.4 | 0.55 | 0.366 | 0.505 |
| kind | ETFs | long_put | 5 | -6,164 | -1,233 | -873.8 | 0.2 | -0.841 | -0.248 |
| spec | All | bear_put_d20_w2_dte10-60 | 29 | -999 | -34.5 | 4.1 | 0.52 | -0.276 | -0.04 |
| spec | All | bear_put_d25_w2_dte10-60 | 19 | -3,893 | -204.9 | -22.9 | 0.42 | -0.787 | -0.156 |
| spec | All | bear_put_d30_w2_dte10-60 | 12 | -1,504 | -125.3 | -108.1 | 0.25 | -0.473 | -0.06 |
| spec | All | bear_put_d35_w2_dte10-60 | 10 | 1,832 | 183.2 | 27.8 | 0.7 | 0.18 | 0.074 |
| spec | All | bull_call_d20_w2_dte10-60 | 115 | -3,583 | -31.2 | 6.3 | 0.52 | -0.089 | -0.144 |
| spec | All | bull_call_d25_w2_dte10-60 | 29 | -110 | -3.8 | 26.7 | 0.72 | -0.02 | -0.004 |
| spec | All | bull_call_d30_w2_dte10-60 | 19 | 2,147 | 113 | 78.8 | 0.68 | 0.356 | 0.086 |
| spec | All | bull_call_d35_w2_dte10-60 | 7 | 1,711 | 244.4 | 83.6 | 0.86 | 0.488 | 0.069 |
| spec | All | long_call_d40_dte10-60 | 124 | 5,002 | 40.3 | -42.6 | 0.45 | 0.076 | 0.201 |
| spec | All | long_call_d55_dte10-60 | 37 | 2,225 | 60.1 | 107.4 | 0.51 | 0.113 | 0.089 |
| spec | All | long_call_d70_dte10-60 | 11 | -1,091 | -99.2 | -94.1 | 0.36 | -0.215 | -0.044 |
| spec | All | long_put_d40_dte10-60 | 7 | -778 | -111.2 | -38.1 | 0.29 | -0.447 | -0.031 |
| spec | All | long_put_d55_dte10-60 | 4 | -6,897 | -1,724 | -829.7 | 0 | -0.826 | -0.277 |
| spec | All | long_put_d70_dte10-60 | 8 | -18,938 | -2,367 | -3,404 | 0.25 | -0.697 | -0.761 |
| spec | Stocks | bear_put_d20_w2_dte10-60 | 28 | -978 | -34.9 | 8.6 | 0.54 | -0.272 | -0.039 |
| spec | Stocks | bear_put_d25_w2_dte10-60 | 16 | -391 | -24.4 | -7 | 0.5 | -0.398 | -0.016 |
| spec | Stocks | bear_put_d30_w2_dte10-60 | 6 | -340 | -56.7 | -35 | 0.33 | -0.519 | -0.014 |
| spec | Stocks | bear_put_d35_w2_dte10-60 | 2 | 11 | 5.3 | 5.3 | 0.5 | 0.169 | 0 |
| spec | Stocks | bull_call_d20_w2_dte10-60 | 86 | -2,697 | -31.4 | -2.8 | 0.47 | -0.107 | -0.108 |
| spec | Stocks | bull_call_d25_w2_dte10-60 | 17 | 82 | 4.8 | 15.4 | 0.65 | 0.075 | 0.003 |
| spec | Stocks | bull_call_d30_w2_dte10-60 | 8 | 675 | 84.3 | 67.1 | 0.75 | 0.382 | 0.027 |
| spec | Stocks | bull_call_d35_w2_dte10-60 | 2 | 133 | 66.5 | 66.5 | 1 | 0.54 | 0.005 |
| spec | Stocks | long_call_d40_dte10-60 | 75 | -7,849 | -104.7 | -91.1 | 0.37 | -0.232 | -0.316 |
| spec | Stocks | long_call_d55_dte10-60 | 33 | 2,368 | 71.8 | 107.4 | 0.52 | 0.131 | 0.095 |
| spec | Stocks | long_call_d70_dte10-60 | 9 | -954 | -106 | -95.1 | 0.44 | -0.222 | -0.038 |
| spec | Stocks | long_put_d40_dte10-60 | 6 | -285 | -47.5 | -23.8 | 0.33 | -0.259 | -0.011 |
| spec | Stocks | long_put_d55_dte10-60 | 3 | -6,024 | -2,008 | -785.6 | 0 | -0.825 | -0.242 |
| spec | Stocks | long_put_d70_dte10-60 | 5 | -14,141 | -2,828 | -3,740 | 0.2 | -0.656 | -0.568 |
| spec | ETFs | bear_put_d20_w2_dte10-60 | 1 | -21 | -20.7 | -20.7 | 0 | -0.902 | -0.001 |
| spec | ETFs | bear_put_d25_w2_dte10-60 | 3 | -3,502 | -1,167 | -124.2 | 0 | -0.884 | -0.141 |
| spec | ETFs | bear_put_d30_w2_dte10-60 | 6 | -1,164 | -193.9 | -246.6 | 0.17 | -0.462 | -0.047 |
| spec | ETFs | bear_put_d35_w2_dte10-60 | 8 | 1,821 | 227.7 | 58.1 | 0.75 | 0.18 | 0.073 |
| spec | ETFs | bull_call_d20_w2_dte10-60 | 29 | -886 | -30.5 | 73.2 | 0.69 | -0.058 | -0.036 |
| spec | ETFs | bull_call_d25_w2_dte10-60 | 12 | -192 | -16 | 44.6 | 0.83 | -0.043 | -0.008 |
| spec | ETFs | bull_call_d30_w2_dte10-60 | 11 | 1,472 | 133.9 | 178.8 | 0.64 | 0.345 | 0.059 |
| spec | ETFs | bull_call_d35_w2_dte10-60 | 5 | 1,578 | 315.6 | 232 | 0.8 | 0.484 | 0.063 |
| spec | ETFs | long_call_d40_dte10-60 | 49 | 12,851 | 262.3 | 109.9 | 0.57 | 0.402 | 0.517 |
| spec | ETFs | long_call_d55_dte10-60 | 4 | -143 | -35.8 | -66.5 | 0.5 | -0.091 | -0.006 |
| spec | ETFs | long_call_d70_dte10-60 | 2 | -137 | -68.6 | -68.6 | 0 | -0.177 | -0.006 |
| spec | ETFs | long_put_d40_dte10-60 | 1 | -493 | -493 | -493 | 0 | -0.771 | -0.02 |
| spec | ETFs | long_put_d55_dte10-60 | 1 | -874 | -873.8 | -873.8 | 0 | -0.829 | -0.035 |
| spec | ETFs | long_put_d70_dte10-60 | 3 | -4,797 | -1,599 | -1,829 | 0.33 | -0.85 | -0.193 |
| trend | All | bear | 89 | -31,177 | -350.3 | -22.9 | 0.42 | -0.527 | -1.253 |
| trend | All | bull | 342 | 6,301 | 18.4 | 9.6 | 0.52 | 0.043 | 0.253 |
| trend | Stocks | bear | 66 | -22,148 | -335.6 | -15.1 | 0.44 | -0.629 | -0.89 |
| trend | Stocks | bull | 230 | -8,242 | -35.8 | -9 | 0.47 | -0.098 | -0.331 |
| trend | ETFs | bear | 23 | -9,028 | -392.5 | -129.4 | 0.35 | -0.377 | -0.363 |
| trend | ETFs | bull | 112 | 14,543 | 129.8 | 66.5 | 0.63 | 0.236 | 0.585 |
| vol | All | high | 190 | -40,382 | -212.5 | 0.1 | 0.5 | -0.355 | -1.623 |
| vol | All | low | 124 | 7,926 | 63.9 | 4.7 | 0.51 | 0.175 | 0.319 |
| vol | All | mid | 117 | 7,579 | 64.8 | -0.2 | 0.5 | 0.164 | 0.305 |
| vol | Stocks | high | 148 | -27,955 | -188.9 | 4.6 | 0.51 | -0.364 | -1.124 |
| vol | Stocks | low | 76 | -3,721 | -49 | -22.5 | 0.41 | -0.201 | -0.15 |
| vol | Stocks | mid | 72 | 1,285 | 17.8 | -14.2 | 0.42 | 0.053 | 0.052 |
| vol | ETFs | high | 42 | -12,427 | -295.9 | -34.6 | 0.45 | -0.336 | -0.5 |
| vol | ETFs | low | 48 | 11,647 | 242.7 | 73.6 | 0.67 | 0.434 | 0.468 |
| vol | ETFs | mid | 45 | 6,294 | 139.9 | 65.4 | 0.62 | 0.29 | 0.253 |
| year | All | 2020 | 177 | -15,722 | -88.8 | 43.3 | 0.55 | -0.103 | -0.632 |
| year | All | 2021 | 170 | -8,580 | -50.5 | -28.1 | 0.45 | -0.18 | -0.345 |
| year | All | 2022 | 64 | -424 | -6.6 | 5.9 | 0.53 | -0.108 | -0.017 |
| year | All | 2023 | 20 | -150 | -7.5 | -17.5 | 0.35 | -0.308 | -0.006 |
| year | Stocks | 2020 | 81 | -22,107 | -272.9 | -42.6 | 0.44 | -0.28 | -0.889 |
| year | Stocks | 2021 | 141 | -7,808 | -55.4 | -18.4 | 0.46 | -0.21 | -0.314 |
| year | Stocks | 2022 | 55 | -347 | -6.3 | 5.8 | 0.53 | -0.106 | -0.014 |
| year | Stocks | 2023 | 19 | -130 | -6.8 | -17.2 | 0.37 | -0.278 | -0.005 |
| year | ETFs | 2020 | 96 | 6,385 | 66.5 | 91.6 | 0.65 | 0.086 | 0.257 |
| year | ETFs | 2021 | 29 | -772 | -26.6 | -66.3 | 0.41 | -0.073 | -0.031 |
| year | ETFs | 2022 | 9 | -77 | -8.6 | 17.3 | 0.56 | -0.118 | -0.003 |
| year | ETFs | 2023 | 1 | -21 | -20.7 | -20.7 | 0 | -0.902 | -0.001 |
| period | All | 2020 crash | 73 | -16,991 | -232.8 | 43.3 | 0.58 | -0.17 | -0.683 |
| period | All | 2020 rebound | 104 | 1,269 | 12.2 | 52.8 | 0.54 | 0.024 | 0.051 |
| period | All | 2021 bull | 170 | -8,580 | -50.5 | -28.1 | 0.45 | -0.18 | -0.345 |
| period | All | 2022 bear | 64 | -424 | -6.6 | 5.9 | 0.53 | -0.108 | -0.017 |
| period | All | 2023 | 20 | -150 | -7.5 | -17.5 | 0.35 | -0.308 | -0.006 |
| period | Stocks | 2020 crash | 26 | -19,476 | -749.1 | -194.4 | 0.27 | -0.393 | -0.783 |
| period | Stocks | 2020 rebound | 55 | -2,631 | -47.8 | 6.3 | 0.53 | -0.09 | -0.106 |
| period | Stocks | 2021 bull | 141 | -7,808 | -55.4 | -18.4 | 0.46 | -0.21 | -0.314 |
| period | Stocks | 2022 bear | 55 | -347 | -6.3 | 5.8 | 0.53 | -0.106 | -0.014 |
| period | Stocks | 2023 | 19 | -130 | -6.8 | -17.2 | 0.37 | -0.278 | -0.005 |
| period | ETFs | 2020 crash | 47 | 2,485 | 52.9 | 118 | 0.74 | 0.049 | 0.1 |
| period | ETFs | 2020 rebound | 49 | 3,900 | 79.6 | 67.6 | 0.55 | 0.164 | 0.157 |
| period | ETFs | 2021 bull | 29 | -772 | -26.6 | -66.3 | 0.41 | -0.073 | -0.031 |
| period | ETFs | 2022 bear | 9 | -77 | -8.6 | 17.3 | 0.56 | -0.118 | -0.003 |
| period | ETFs | 2023 | 1 | -21 | -20.7 | -20.7 | 0 | -0.902 | -0.001 |
| tier | All | core | 169 | -17,152 | -101.5 | -32.7 | 0.44 | -0.158 | -0.689 |
| tier | All | momentum | 28 | -774 | -27.6 | 7 | 0.54 | -0.235 | -0.031 |
| tier | All | rest | 234 | -6,951 | -29.7 | 13.1 | 0.54 | -0.075 | -0.279 |
| tier | Stocks | core | 98 | -20,218 | -206.3 | -33.7 | 0.41 | -0.401 | -0.813 |
| tier | Stocks | momentum | 28 | -774 | -27.6 | 7 | 0.54 | -0.235 | -0.031 |
| tier | Stocks | rest | 170 | -9,399 | -55.3 | -2.1 | 0.48 | -0.142 | -0.378 |
| tier | ETFs | core | 71 | 3,067 | 43.2 | -24.4 | 0.48 | 0.053 | 0.123 |
| tier | ETFs | rest | 64 | 2,448 | 38.3 | 59.2 | 0.7 | 0.09 | 0.098 |
| liquidity | All | 50k-500k | 292 | -21,076 | -72.2 | -1.5 | 0.49 | -0.184 | -0.847 |
| liquidity | All | 5k-50k | 42 | -2,562 | -61 | 4.2 | 0.52 | -0.147 | -0.103 |
| liquidity | All | <5k | 6 | 2,382 | 396.9 | -3.5 | 0.5 | 0.332 | 0.096 |
| liquidity | All | >=500k | 91 | -3,620 | -39.8 | 5.9 | 0.52 | -0.055 | -0.146 |
| liquidity | Stocks | 50k-500k | 215 | -21,797 | -101.4 | -10.4 | 0.44 | -0.29 | -0.876 |
| liquidity | Stocks | 5k-50k | 35 | -3,236 | -92.5 | 2.1 | 0.51 | -0.221 | -0.13 |
| liquidity | Stocks | <5k | 6 | 2,382 | 396.9 | -3.5 | 0.5 | 0.332 | 0.096 |
| liquidity | Stocks | >=500k | 40 | -7,739 | -193.5 | 5.5 | 0.53 | -0.338 | -0.311 |
| liquidity | ETFs | 50k-500k | 77 | 721 | 9.4 | 54.3 | 0.64 | 0.018 | 0.029 |
| liquidity | ETFs | 5k-50k | 7 | 674 | 96.3 | 25.3 | 0.57 | 0.243 | 0.027 |
| liquidity | ETFs | >=500k | 51 | 4,119 | 80.8 | 17.3 | 0.51 | 0.095 | 0.166 |
| earnings_in_hold | All | etf | 135 | 5,515 | 40.8 | 46.1 | 0.59 | 0.065 | 0.222 |
| earnings_in_hold | All | no | 272 | -26,810 | -98.6 | -2.7 | 0.48 | -0.237 | -1.078 |
| earnings_in_hold | All | yes | 24 | -3,581 | -149.2 | -55.4 | 0.29 | -0.543 | -0.144 |
| earnings_in_hold | Stocks | no | 272 | -26,810 | -98.6 | -2.7 | 0.48 | -0.237 | -1.078 |
| earnings_in_hold | Stocks | yes | 24 | -3,581 | -149.2 | -55.4 | 0.29 | -0.543 | -0.144 |
| earnings_in_hold | ETFs | etf | 135 | 5,515 | 40.8 | 46.1 | 0.59 | 0.065 | 0.222 |
| exit_reason | All | dte_exit | 15 | -2,146 | -143.1 | -101.3 | 0.07 | -0.393 | -0.086 |
| exit_reason | All | expiry | 1 | -43 | -43.2 | -43.2 | 0 | -0.066 | -0.002 |
| exit_reason | All | profit_lock | 21 | -599 | -28.5 | -2.1 | 0.33 | -0.148 | -0.024 |
| exit_reason | All | stop | 183 | -85,306 | -466.2 | -202.8 | 0.01 | -0.845 | -3.429 |
| exit_reason | All | take_profit | 211 | 63,218 | 299.6 | 99.7 | 0.98 | 0.672 | 2.541 |
| exit_reason | Stocks | dte_exit | 10 | -1,440 | -144 | -96.2 | 0 | -0.413 | -0.058 |
| exit_reason | Stocks | profit_lock | 17 | -262 | -15.4 | -2.1 | 0.29 | -0.106 | -0.011 |
| exit_reason | Stocks | stop | 135 | -59,038 | -437.3 | -153.8 | 0.01 | -0.849 | -2.373 |
| exit_reason | Stocks | take_profit | 134 | 30,349 | 226.5 | 73.5 | 0.98 | 0.686 | 1.22 |
| exit_reason | ETFs | dte_exit | 5 | -706 | -141.2 | -131.6 | 0.2 | -0.356 | -0.028 |
| exit_reason | ETFs | expiry | 1 | -43 | -43.2 | -43.2 | 0 | -0.066 | -0.002 |
| exit_reason | ETFs | profit_lock | 4 | -337 | -84.2 | -11 | 0.5 | -0.212 | -0.014 |
| exit_reason | ETFs | stop | 48 | -26,268 | -547.2 | -306.3 | 0 | -0.836 | -1.056 |
| exit_reason | ETFs | take_profit | 77 | 32,869 | 426.9 | 275.4 | 0.99 | 0.66 | 1.321 |
| capacity_clipped | All | False | 327 | -21,579 | -66 | -9.9 | 0.48 | -0.133 | -0.867 |
| capacity_clipped | All | True | 104 | -3,297 | -31.7 | 19.9 | 0.58 | -0.076 | -0.133 |
| capacity_clipped | Stocks | False | 243 | -22,597 | -93 | -17.2 | 0.46 | -0.217 | -0.908 |
| capacity_clipped | Stocks | True | 53 | -7,794 | -147.1 | -3.5 | 0.47 | -0.499 | -0.313 |
| capacity_clipped | ETFs | False | 84 | 1,018 | 12.1 | 23 | 0.52 | 0.018 | 0.041 |
| capacity_clipped | ETFs | True | 51 | 4,497 | 88.2 | 67.6 | 0.69 | 0.163 | 0.181 |

## Costs fixed cash_debit

`costs_fixed_cash_debit.csv`

| kind | n | gross_at_mid | slippage_cost | fees | net | cost_share_of_gross | median_entry_spread_pct_of_premium |
|---|---|---|---|---|---|---|---|
| all | 431 | -9,314 | 15,310 | 252 | -24,876 | 1.67 | 10.8 |
| bear_put | 70 | 2,120 | 6,647 | 37 | -4,564 | 3.15 | 28.4 |
| bull_call | 170 | 6,119 | 5,775 | 180 | 165 | 0.97 | 22.1 |
| long_call | 172 | 8,410 | 2,242 | 32 | 6,136 | 0.27 | 6.2 |
| long_put | 19 | -25,962 | 647 | 4 | -26,613 | 0.03 | 5.6 |

## Calibration by kind fixed cash_debit

`calib_fixed_cash_debit.csv`

| kind | n | pred_ev_per_trade | real_pnl_per_trade | pred_pop | real_win | brier | brier_base_rate | corr_ev_pnl_unit |
|---|---|---|---|---|---|---|---|---|
| all | 431 | 50.8 | -57.7 | 0.603 | 0.501 | 0.2532 | 0.25 | -0.197 |
| bear_put | 70 | 9.5 | -65.2 | 0.603 | 0.471 | 0.2593 | 0.2492 | 0.027 |
| bull_call | 170 | 35.4 | 1 | 0.622 | 0.588 | 0.2345 | 0.2422 | 0.177 |
| long_call | 172 | 74.5 | 35.7 | 0.59 | 0.459 | 0.2633 | 0.2483 | -0.148 |
| long_put | 19 | 127.2 | -1,401 | 0.557 | 0.211 | 0.3073 | 0.1662 | -0.172 |

## Calibration by EV decile fixed cash_debit

`calib_decile_fixed_cash_debit.csv`

| decile | n | pred_ev_unit | real_pnl_unit | pred_pop | real_win |
|---|---|---|---|---|---|
| 1 | 44 | 0.267 | -11.91 | 0.585 | 0.432 |
| 2 | 43 | 0.806 | 10.67 | 0.593 | 0.581 |
| 3 | 43 | 1.451 | -20.84 | 0.585 | 0.419 |
| 4 | 43 | 3.136 | -12.01 | 0.601 | 0.465 |
| 5 | 43 | 5.227 | -66.45 | 0.595 | 0.581 |
| 6 | 43 | 7.668 | -94.75 | 0.6 | 0.488 |
| 7 | 43 | 12.23 | 81.86 | 0.61 | 0.628 |
| 8 | 43 | 19.07 | -26.69 | 0.618 | 0.535 |
| 9 | 43 | 34.15 | -42.7 | 0.617 | 0.419 |
| 10 | 43 | 151 | -199.5 | 0.63 | 0.465 |

## Tail fixed cash_debit

`tail_fixed_cash_debit.csv`

| losers | gross_loss | top_1pct_trades_share_of_loss | top_5pct_trades_share_of_loss | top_10pct_trades_share_of_loss | gross_win | peak_day | peak_equity | trough_day | trough_equity | first_below_half | mean_open_risk_pct | p90_open_risk_pct | mean_same_direction_share |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 215 | 88,997 | 0.209 | 0.572 | 0.709 | 64,121 | 2020-03-11 | 43,192 | 2023-05-10 | 115 | 2020-04-08 | 47.9 | 92.1 | 0.9 |

## Worst days fixed cash_debit

`worst_days_fixed_cash_debit.csv`

| day | equity_change | pct_of_equity | open | bullish | bearish | neutral | risk_pct_equity | names |
|---|---|---|---|---|---|---|---|---|
| 2020-03-24 | -14,296 | -34 | 10 | 1 | 9 | 0 | 82 | AAPL,BABA,BAC,DIA,DIS,IWM,NFLX,ORCL,QQQ,SPY |
| 2020-04-06 | -8,530 | -36.4 | 7 | 0 | 7 | 0 | 120 | AAPL,BABA,BAC,DIA,DIS,IWM,ORCL |
| 2020-03-26 | -7,893 | -27.5 | 9 | 0 | 9 | 0 | 119 | AAPL,BABA,BAC,DIA,DIS,IWM,ORCL,QQQ,SPY |
| 2020-03-30 | -4,751 | -18.8 | 9 | 0 | 9 | 0 | 135 | AAPL,BABA,BAC,DIA,DIS,IWM,ORCL,QQQ,SPY |
| 2020-04-02 | -4,569 | -17 | 9 | 0 | 9 | 0 | 127 | AAPL,BABA,BAC,DIA,DIS,IWM,ORCL,QQQ,SPY |
| 2020-03-17 | -4,309 | -10.3 | 7 | 3 | 4 | 0 | 64 | AAPL,BABA,BAC,GLD,NFLX,ORCL |
| 2020-06-10 | -4,087 | -34.7 | 9 | 9 | 0 | 0 | 76 | BAC,C,GM,JPM,WFC,XLF |
| 2020-02-28 | -3,926 | -10 | 8 | 8 | 0 | 0 | 15 | GLD,NFLX |

## BT-8 (6 tickers, 2024-03 -> 2026-07, existing run)

`bt8.csv`

| profile | arm | trades | net_pnl | end_equity | return_pct | max_dd_pct | win_rate | pnl_per_risk | return_on_risk | mean_r | eq_2020_06_30 | eq_2021_12_31 | eq_2022_12_31 | net_dte_exit | net_expiry | net_stop | net_take_profit | net_profit_lock |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| margin | control | 127 | -17,879 | 7,121 | -71.51 | 84.54 | 0.732 | -0.0914 | -0.0406 | -0.0501 | None | None | None | -8,126 | -2,818 | -26,169 | 19,234 |  |
| margin | t1 | 130 | -16,046 | 8,954 | -64.18 | 80.2 | 0.762 | -0.0419 | -0.0152 | -0.0217 | None | None | None | -7,578 | -17,337 | -40,194 | 49,063 |  |
| cash_debit | control | 122 | -15,710 | 9,290 | -62.84 | 83.89 | 0.533 | -0.0705 | 0.0049 | 0.0253 | None | None | None | -7,134 |  | -82,049 | 74,165 | -693 |
| cash_debit | t1 | 132 | -21,510 | 3,490 | -86.04 | 94.13 | 0.515 | -0.0896 | -0.0752 | -0.0326 | None | None | None | -7,089 |  | -88,578 | 73,804 | 353 |

## Live closed trades

`live_closed.csv`

| store | ticker | kind | legs | opened | closed | exit_reason | pnl |
|---|---|---|---|---|---|---|---|
| data | HOOD | long_put | 1 | 2026-10-12 | 2026-10-13 | research_review | -80 |
| data | NVDA | long_call | 1 | 2026-10-12 | 2026-10-13 | research_review | 40 |
| data | IREN | vertical_debit | 2 | 2026-10-12 | 2026-10-14 | stop | -260 |
| data | INTC | vertical_debit | 2 | 2026-10-14 | 2026-10-15 | research_review | -372 |
| 20261011-1315-pre-d80 | IWM | vertical_debit | 2 | 2026-10-01 | 2026-10-01 | remaining_ev_floor | -20 |
| 20261011-1315-pre-d80 | SPY | vertical_debit | 2 | 2026-10-01 | 2026-10-01 | remaining_ev_floor | -23 |
| 20261011-1315-pre-d80 | NFLX | vertical_debit | 2 | 2026-10-01 | 2026-10-01 | remaining_ev_floor | -70 |
| 20261011-1315-pre-d80 | CRWD | long_call | 1 | 2026-10-06 | 2026-10-08 | research_review | -935 |
| 20261011-1315-pre-d80 | INTC | vertical_debit | 2 | 2026-10-06 | 2026-10-09 | research_review | -662 |
| 20261011-1315-pre-d80 | MRVL | vertical_debit | 2 | 2026-10-06 | 2026-10-08 | research_review | -410 |
| 20261011-1315-pre-d80 | VST | vertical_debit | 2 | 2026-10-06 | 2026-10-07 | research_review | 160 |
| 20261011-1315-pre-d80 | META | long_call | 1 | 2026-10-07 | 2026-10-08 | research_review | -75 |
| 20261011-1315-pre-d80 | TSM | vertical_debit | 2 | 2026-10-08 | 2026-10-08 | research_review | -90 |
| 20261011-1315-pre-d80 | HOOD | vertical_debit | 2 | 2026-10-08 | 2026-10-08 | research_review | -30 |

## Live closed trades by kind x exit reason

`live_summary.csv`

| kind | exit_reason | n | net | mean |
|---|---|---|---|---|
| long_call | research_review | 3 | -970 | -323.3 |
| long_put | research_review | 1 | -80 | -80 |
| vertical_debit | remaining_ev_floor | 3 | -113 | -37.7 |
| vertical_debit | research_review | 6 | -1,404 | -234 |
| vertical_debit | stop | 1 | -260 | -260 |

## Counterfactuals (one variable each)

`counterfactuals.csv`

| counterfactual | profile | trades | net_pnl | max_dd_pct | return_on_risk | mean_r | eq_2020_06_30 | eq_2021_12_31 | eq_2022_12_31 | d_net_vs_fixed | d_return_on_risk_vs_fixed |
|---|---|---|---|---|---|---|---|---|---|---|---|
| legacy | margin | 440 | -17,294 | 83 | -0.0321 | -0.0509 | 16,103 | 19,759 | 10,740 | 60 | -0.01 |
| legacy | cash_debit | 585 | -24,880 | 99.74 | -0.0038 | -0.0211 | 6,489 | 3,894 | 477 | -4 | 0.1864 |
| marks_only | margin | 459 | -20,435 | 87.22 | -0.0466 | -0.0634 | 20,459 | 22,980 | 15,572 | -3,080 | -0.0245 |
| marks_only | cash_debit | 441 | -24,905 | 99.78 | -0.1933 | -0.0619 | 4,033 | 1,073 | 443 | -29 | -0.0031 |
| fixed | margin | 469 | -17,354 | 83.32 | -0.0221 | -0.0439 | 20,459 | 25,933 | 18,492 | 0 | 0 |
| fixed | cash_debit | 431 | -24,876 | 99.73 | -0.1902 | -0.0402 | 4,033 | 651 | 300 | 0 | 0 |
| est_spread | margin | 447 | -21,976 | 91.84 | -0.06 | -0.0697 | 20,607 | 23,191 | 12,203 | -4,621 | -0.0379 |
| est_spread | cash_debit | 468 | -24,897 | 99.78 | -0.0407 | -0.0604 | 6,289 | 1,122 | 277 | -21 | 0.1495 |
| half_size | margin | 508 | -13,487 | 64.7 | -0.0299 | -0.0561 | 20,627 | 20,737 | 15,556 | 3,867 | -0.0078 |
| half_size | cash_debit | 676 | -24,721 | 99.11 | -0.0703 | -0.0869 | 10,810 | 3,529 | 1,450 | 156 | 0.1199 |
| half_slots | margin | 273 | -21,285 | 87.85 | -0.0668 | -0.0774 | 22,435 | 16,302 | 11,067 | -3,930 | -0.0447 |
| half_slots | cash_debit | 362 | -24,903 | 99.72 | -0.0503 | -0.0777 | 11,647 | 1,328 | 552 | -27 | 0.1399 |
| day_risk_cap | margin | 445 | -13,483 | 68.61 | -0.0212 | -0.0429 | 18,699 | 18,190 | 13,129 | 3,872 | 0.0009 |
| day_risk_cap | cash_debit | 610 | -24,795 | 99.66 | -0.0722 | -0.0479 | 8,003 | 20,870 | 1,133 | 81 | 0.118 |
| no_bull_put__no_bull_call | margin | 314 | -13,966 | 68.65 | -0.0073 | -0.0556 | 12,951 | 14,910 | 11,416 | 3,389 | 0.0148 |
| no_bull_put__no_bull_call | cash_debit | 340 | -24,858 | 99.64 | -0.1362 | -0.0941 | 4,668 | 581 | 499 | 19 | 0.054 |
| no_bear_call__no_bear_put | margin | 426 | -15,582 | 84.7 | -0.024 | -0.0397 | 18,816 | 25,838 | 12,314 | 1,773 | -0.0019 |
| no_bear_call__no_bear_put | cash_debit | 380 | -24,846 | 99.67 | -0.2361 | -0.0441 | 3,887 | 620 | 290 | 31 | -0.0459 |
| no_iron_condor__no_long_call | margin | 404 | -20,501 | 85.6 | -0.0482 | -0.0352 | 15,840 | 16,816 | 10,450 | -3,147 | -0.0261 |
| no_iron_condor__no_long_call | cash_debit | 390 | -24,880 | 99.54 | -0.0684 | -0.0351 | 5,492 | 3,290 | 608 | -3 | 0.1218 |
| no_long_put | margin | 469 | -17,354 | 83.32 | -0.0221 | -0.0439 | 20,459 | 25,933 | 18,492 | 0 | 0 |
| no_long_put | cash_debit | 461 | -24,877 | 99.85 | -0.014 | -0.0347 | 19,028 | 707 | 299 | -0 | 0.1762 |
| BT-3 control (E7.14) | margin | 440 | -17,294 | 83 | -0.0321 | -0.0509 | 16,103 | 19,759 | 10,740 | 60 | -0.01 |
| BT-2 hold (E7.14) | margin | 319 | -23,996 | 96.51 | -0.0546 | -0.0424 | 11,593 | 20,212 | 2,624 | -6,642 | -0.0325 |
| BT-2 managed (E7.14) | margin | 440 | -17,294 | 83 | -0.0321 | -0.0509 | 16,103 | 19,759 | 10,740 | 60 | -0.01 |
| BT-3 control (E7.14) | cash_debit | 585 | -24,880 | 99.74 | -0.0038 | -0.0211 | 6,489 | 3,894 | 477 | -4 | 0.1864 |
| BT-2 hold (E7.14) | cash_debit | 142 | -24,907 | 99.8 | -0.3599 | -0.1053 | 6,774 | 198 | 103 | -31 | -0.1697 |
| BT-2 managed (E7.14) | cash_debit | 585 | -24,880 | 99.74 | -0.0038 | -0.0211 | 6,489 | 3,894 | 477 | -4 | 0.1864 |
