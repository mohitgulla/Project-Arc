# E7.19 sim alignment: generated tables

Written by `scripts/sim_alignment_report.py`; the narrative is `../sim-alignment.md`.
`old` = E7.18 books (one expiry nearest the window middle); `pre128` = the D125
sleeve-aligned sim on pre-D128 config (credit-only margin, width incumbents);
`new` = the incumbent: the same sim on post-D128 config (margin superset, the
live `menu_measure` ranker `managed_net_ev`, debit-scoped anti-chase, margin
buying power, D131 rules); `*_noproxy` = the same with `intraday_proxy: none`.

## books
`books.csv`

| book | variant | profile | class | trades | net_pnl | end_equity | return_pct | max_dd_pct | win_rate | pnl_per_risk | return_on_risk | mean_r | eq_2020_06_30 | eq_2021_12_31 | eq_2022_12_31 | net | net_at_mid | per_risk | win |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| c500_dev | old | margin | All | 480 | -17,739 | 7,261 | -70.96 | 78.68 | 0.706 | -0.0375 | -0.0273 | -0.0546 | 21,402 | 18,194 | 10,585 | -17,739 | -547 | -0.0375 | 0.706 |
| c500_dev | old | margin | Stocks | 106 |  |  |  |  |  |  |  |  |  |  |  | -1,404 | 5,308 | -0.0107 | 0.736 |
| c500_dev | old | margin | ETFs | 374 |  |  |  |  |  |  |  |  |  |  |  | -16,335 | -5,855 | -0.0478 | 0.698 |
| c500_dev | old | cash_debit | All | 674 | -24,926 | 73.86 | -99.7 | 99.87 | 0.493 | -0.0392 | -0.1005 | -0.0512 | 13,084 | 13,900 | 466 | -24,926 | 9,956 | -0.0392 | 0.493 |
| c500_dev | old | cash_debit | Stocks | 428 |  |  |  |  |  |  |  |  |  |  |  | -32,286 | -8,178 | -0.0747 | 0.502 |
| c500_dev | old | cash_debit | ETFs | 246 |  |  |  |  |  |  |  |  |  |  |  | 7,360 | 18,133 | 0.0361 | 0.476 |
| c500_dev | pre128 | margin | All | 509 | -14,976 | 10,024 | -59.9 | 80.15 | 0.721 | -0.0271 | 0.0118 | -0.0277 | 13,282 | 14,623 | 8,633 | -14,976 | 11,530 | -0.0271 | 0.721 |
| c500_dev | pre128 | margin | Stocks | 150 |  |  |  |  |  |  |  |  |  |  |  | -11,597 | 2,508 | -0.0614 | 0.707 |
| c500_dev | pre128 | margin | ETFs | 359 |  |  |  |  |  |  |  |  |  |  |  | -3,379 | 9,022 | -0.0093 | 0.727 |
| c500_dev | pre128 | cash_debit | All | 679 | -24,964 | 36.49 | -99.85 | 99.92 | 0.406 | -0.0396 | -0.0165 | -0.1113 | 8,530 | 65 | 41 | -24,964 | 45,756 | -0.0396 | 0.406 |
| c500_dev | pre128 | cash_debit | Stocks | 435 |  |  |  |  |  |  |  |  |  |  |  | -32,866 | 10,753 | -0.0819 | 0.4 |
| c500_dev | pre128 | cash_debit | ETFs | 244 |  |  |  |  |  |  |  |  |  |  |  | 7,902 | 35,003 | 0.0344 | 0.418 |
| c500_dev | pre128_noproxy | margin | All | 509 | -14,976 | 10,024 | -59.9 | 80.15 | 0.721 | -0.0271 | 0.0118 | -0.0277 | 13,282 | 14,623 | 8,633 | -14,976 | 11,530 | -0.0271 | 0.721 |
| c500_dev | pre128_noproxy | margin | Stocks | 150 |  |  |  |  |  |  |  |  |  |  |  | -11,597 | 2,508 | -0.0614 | 0.707 |
| c500_dev | pre128_noproxy | margin | ETFs | 359 |  |  |  |  |  |  |  |  |  |  |  | -3,379 | 9,022 | -0.0093 | 0.727 |
| c500_dev | pre128_noproxy | cash_debit | All | 772 | -24,959 | 40.68 | -99.84 | 99.95 | 0.449 | -0.0229 | 0.0181 | -0.0785 | 19,942 | 224 | 44 | -24,959 | 81,056 | -0.0229 | 0.449 |
| c500_dev | pre128_noproxy | cash_debit | Stocks | 515 |  |  |  |  |  |  |  |  |  |  |  | -24,013 | 49,744 | -0.0337 | 0.456 |
| c500_dev | pre128_noproxy | cash_debit | ETFs | 257 |  |  |  |  |  |  |  |  |  |  |  | -946 | 31,312 | -0.0025 | 0.436 |
| c500_dev | new | margin | All | 599 | -24,934 | 65.51 | -99.74 | 99.75 | 0.451 | -0.1231 | -0.149 | -0.1066 | 8,160 | 1,396 | 167 | -24,934 | -19,410 | -0.1231 | 0.451 |
| c500_dev | new | margin | Stocks | 370 |  |  |  |  |  |  |  |  |  |  |  | -22,727 | -19,275 | -0.2074 | 0.43 |
| c500_dev | new | margin | ETFs | 229 |  |  |  |  |  |  |  |  |  |  |  | -2,208 | -135 | -0.0237 | 0.485 |
| c500_dev | new | cash_debit | All | 947 | -24,376 | 623.5 | -97.51 | 99.5 | 0.422 | -0.0395 | -0.0582 | -0.0894 | 13,502 | 10,349 | 2,699 | -24,376 | -7,243 | -0.0395 | 0.422 |
| c500_dev | new | cash_debit | Stocks | 603 |  |  |  |  |  |  |  |  |  |  |  | -16,464 | -4,578 | -0.0415 | 0.418 |
| c500_dev | new | cash_debit | ETFs | 344 |  |  |  |  |  |  |  |  |  |  |  | -7,913 | -2,665 | -0.036 | 0.43 |
| c500_dev | new_noproxy | margin | All | 984 | -23,204 | 1,796 | -92.81 | 98.03 | 0.494 | -0.023 | 0.015 | -0.0183 | 20,310 | 32,368 | 7,251 | -23,204 | 1,112 | -0.023 | 0.494 |
| c500_dev | new_noproxy | margin | Stocks | 577 |  |  |  |  |  |  |  |  |  |  |  | -12,623 | 3,686 | -0.0213 | 0.487 |
| c500_dev | new_noproxy | margin | ETFs | 407 |  |  |  |  |  |  |  |  |  |  |  | -10,581 | -2,574 | -0.0254 | 0.504 |
| c500_dev | new_noproxy | cash_debit | All | 793 | -24,911 | 89.45 | -99.64 | 99.76 | 0.465 | -0.0668 | -0.073 | -0.0444 | 8,851 | 6,337 | 1,632 | -24,911 | -13,155 | -0.0668 | 0.465 |
| c500_dev | new_noproxy | cash_debit | Stocks | 469 |  |  |  |  |  |  |  |  |  |  |  | -19,489 | -12,894 | -0.0899 | 0.443 |
| c500_dev | new_noproxy | cash_debit | ETFs | 324 |  |  |  |  |  |  |  |  |  |  |  | -5,422 | -262 | -0.0346 | 0.497 |
| c125_dev | old | margin | All | 435 | -22,895 | 2,105 | -91.58 | 92.79 | 0.685 | -0.0526 | -0.0544 | -0.0808 | 20,243 | 16,242 | 6,059 | -22,895 | -6,980 | -0.0526 | 0.685 |
| c125_dev | old | margin | Stocks | 123 |  |  |  |  |  |  |  |  |  |  |  | -4,307 | 2,857 | -0.0297 | 0.691 |
| c125_dev | old | margin | ETFs | 312 |  |  |  |  |  |  |  |  |  |  |  | -18,588 | -9,837 | -0.064 | 0.683 |
| c125_dev | old | cash_debit | All | 485 | -24,892 | 107.9 | -99.57 | 99.76 | 0.507 | -0.0679 | -0.0177 | -0.0294 | 7,967 | 1,256 | 300 | -24,892 | -480 | -0.0679 | 0.507 |
| c125_dev | old | cash_debit | Stocks | 322 |  |  |  |  |  |  |  |  |  |  |  | -38,771 | -26,218 | -0.1802 | 0.484 |
| c125_dev | old | cash_debit | ETFs | 163 |  |  |  |  |  |  |  |  |  |  |  | 13,879 | 25,738 | 0.0916 | 0.552 |
| c125_dev | pre128 | margin | All | 529 | -16,079 | 8,921 | -64.32 | 81.24 | 0.724 | -0.0259 | -0.0086 | -0.0203 | 18,262 | 22,803 | 9,251 | -16,079 | 6,814 | -0.0259 | 0.724 |
| c125_dev | pre128 | margin | Stocks | 176 |  |  |  |  |  |  |  |  |  |  |  | -2,874 | 7,458 | -0.0124 | 0.773 |
| c125_dev | pre128 | margin | ETFs | 353 |  |  |  |  |  |  |  |  |  |  |  | -13,205 | -644 | -0.0339 | 0.7 |
| c125_dev | pre128 | cash_debit | All | 343 | -24,955 | 45.25 | -99.82 | 99.84 | 0.42 | -0.1355 | -0.1096 | -0.1142 | 2,750 | 72 | 50 | -24,955 | -4,686 | -0.1355 | 0.42 |
| c125_dev | pre128 | cash_debit | Stocks | 225 |  |  |  |  |  |  |  |  |  |  |  | -24,321 | -14,162 | -0.2399 | 0.391 |
| c125_dev | pre128 | cash_debit | ETFs | 118 |  |  |  |  |  |  |  |  |  |  |  | -634 | 9,476 | -0.0077 | 0.475 |
| c125_dev | pre128_noproxy | margin | All | 529 | -16,079 | 8,921 | -64.32 | 81.24 | 0.724 | -0.0259 | -0.0086 | -0.0203 | 18,262 | 22,803 | 9,251 | -16,079 | 6,814 | -0.0259 | 0.724 |
| c125_dev | pre128_noproxy | margin | Stocks | 176 |  |  |  |  |  |  |  |  |  |  |  | -2,874 | 7,458 | -0.0124 | 0.773 |
| c125_dev | pre128_noproxy | margin | ETFs | 353 |  |  |  |  |  |  |  |  |  |  |  | -13,205 | -644 | -0.0339 | 0.7 |
| c125_dev | pre128_noproxy | cash_debit | All | 567 | -24,942 | 57.7 | -99.77 | 99.87 | 0.466 | -0.0541 | -0.0214 | -0.0648 | 9,891 | 149 | 58 | -24,942 | 26,126 | -0.0541 | 0.466 |
| c125_dev | pre128_noproxy | cash_debit | Stocks | 397 |  |  |  |  |  |  |  |  |  |  |  | -23,722 | 9,929 | -0.0812 | 0.456 |
| c125_dev | pre128_noproxy | cash_debit | ETFs | 170 |  |  |  |  |  |  |  |  |  |  |  | -1,221 | 16,197 | -0.0072 | 0.488 |
| c125_dev | new | margin | All | 917 | -24,382 | 617.7 | -97.53 | 98.39 | 0.466 | -0.0459 | -0.0418 | -0.0682 | 11,728 | 17,600 | 4,116 | -24,382 | -10,940 | -0.0459 | 0.466 |
| c125_dev | new | margin | Stocks | 572 |  |  |  |  |  |  |  |  |  |  |  | -17,005 | -7,772 | -0.0571 | 0.442 |
| c125_dev | new | margin | ETFs | 345 |  |  |  |  |  |  |  |  |  |  |  | -7,378 | -3,167 | -0.0316 | 0.504 |
| c125_dev | new | cash_debit | All | 489 | -24,928 | 72.03 | -99.71 | 99.76 | 0.423 | -0.1403 | -0.1329 | -0.0902 | 9,382 | 940 | 127 | -24,928 | -19,625 | -0.1403 | 0.423 |
| c125_dev | new | cash_debit | Stocks | 304 |  |  |  |  |  |  |  |  |  |  |  | -13,495 | -10,169 | -0.1416 | 0.438 |
| c125_dev | new | cash_debit | ETFs | 185 |  |  |  |  |  |  |  |  |  |  |  | -11,433 | -9,456 | -0.1389 | 0.4 |
| c125_dev | new_noproxy | margin | All | 998 | -5,133 | 19,867 | -20.53 | 91.2 | 0.509 | -0.006 | -0.0091 | -0.0065 | 13,151 | 31,588 | 7,854 | -5,133 | 15,945 | -0.006 | 0.509 |
| c125_dev | new_noproxy | margin | Stocks | 659 |  |  |  |  |  |  |  |  |  |  |  | -17,246 | -2,315 | -0.0322 | 0.499 |
| c125_dev | new_noproxy | margin | ETFs | 339 |  |  |  |  |  |  |  |  |  |  |  | 12,113 | 18,259 | 0.0378 | 0.528 |
| c125_dev | new_noproxy | cash_debit | All | 696 | -24,892 | 107.7 | -99.57 | 99.66 | 0.444 | -0.0687 | -0.0676 | -0.0689 | 10,030 | 8,544 | 1,122 | -24,892 | -13,670 | -0.0687 | 0.444 |
| c125_dev | new_noproxy | cash_debit | Stocks | 447 |  |  |  |  |  |  |  |  |  |  |  | -3,463 | 3,517 | -0.0161 | 0.461 |
| c125_dev | new_noproxy | cash_debit | ETFs | 249 |  |  |  |  |  |  |  |  |  |  |  | -21,430 | -17,188 | -0.1453 | 0.414 |

## reconcile
`reconcile.csv`

| book | variant | profile | sum_pnl | delta_equity | diff | min_equity | min_equity_day | open_at_end |
|---|---|---|---|---|---|---|---|---|
| c500_dev | old | margin | -17,739 | -17,739 | -0 | 6,270 | 2023-04-13 | 0 |
| c500_dev | old | cash_debit | -24,926 | -24,926 | -0 | 73.86 | 2023-12-04 | 0 |
| c500_dev | pre128 | margin | -14,976 | -14,976 | -0 | 6,062 | 2023-04-13 | 0 |
| c500_dev | pre128 | cash_debit | -24,964 | -24,964 | -0 | 36.49 | 2023-08-31 | 0 |
| c500_dev | pre128_noproxy | margin | -14,976 | -14,976 | -0 | 6,062 | 2023-04-13 | 0 |
| c500_dev | pre128_noproxy | cash_debit | -24,959 | -24,959 | -0 | 40.68 | 2023-08-31 | 0 |
| c500_dev | new | margin | -24,934 | -24,934 | -0 | 63.67 | 2023-12-04 | 0 |
| c500_dev | new | cash_debit | -24,376 | -24,376 | -0 | 230 | 2023-11-08 | 0 |
| c500_dev | new_noproxy | margin | -23,204 | -23,204 | 0 | 1,303 | 2023-11-13 | 0 |
| c500_dev | new_noproxy | cash_debit | -24,911 | -24,911 | -0 | 87.61 | 2023-12-19 | 0 |
| c125_dev | old | margin | -22,895 | -22,895 | -0 | 2,011 | 2023-11-16 | 0 |
| c125_dev | old | cash_debit | -24,892 | -24,892 | -0 | 107.9 | 2023-10-02 | 0 |
| c125_dev | pre128 | margin | -16,079 | -16,079 | -0 | 5,749 | 2023-06-15 | 0 |
| c125_dev | pre128 | cash_debit | -24,955 | -24,955 | -0 | 45.25 | 2023-08-31 | 0 |
| c125_dev | pre128_noproxy | margin | -16,079 | -16,079 | -0 | 5,749 | 2023-06-15 | 0 |
| c125_dev | pre128_noproxy | cash_debit | -24,942 | -24,942 | -0 | 56.18 | 2023-04-14 | 0 |
| c125_dev | new | margin | -24,382 | -24,382 | 0 | 532.4 | 2023-10-26 | 0 |
| c125_dev | new | cash_debit | -24,928 | -24,928 | -0 | 70.19 | 2023-12-19 | 0 |
| c125_dev | new_noproxy | margin | -5,133 | -5,133 | -0 | 4,371 | 2023-05-15 | 0 |
| c125_dev | new_noproxy | cash_debit | -24,892 | -24,892 | -0 | 100.9 | 2023-11-28 | 0 |

## sleeves
`sleeves.csv`

| book | variant | profile | class | sleeve | n | net | per_risk | win | mean_dte_entry | mean_days_held | exit_mix |
|---|---|---|---|---|---|---|---|---|---|---|---|
| c500_dev | pre128 | margin | All | default | 509 | -14,976 | -0.0271 | 0.721 | 37.1 | 21 | take_profit:330 dte_exit:113 stop:57 expiry:9 |
| c500_dev | pre128 | margin | Stocks | default | 150 | -11,597 | -0.0614 | 0.707 | 36.4 | 21.7 | take_profit:92 dte_exit:36 stop:17 expiry:5 |
| c500_dev | pre128 | margin | ETFs | default | 359 | -3,379 | -0.0093 | 0.727 | 37.4 | 20.7 | take_profit:238 dte_exit:77 stop:40 expiry:4 |
| c500_dev | pre128 | cash_debit | All | short | 513 | -53,453 | -0.1196 | 0.359 | 19.9 | 4.4 | stop_intraday_proxy:246 take_profit:185 stop:40 lock_intraday_proxy:34 dte_exit:5 profit_lock:2 expiry:1 |
| c500_dev | pre128 | cash_debit | All | swing | 166 | 28,489 | 0.1548 | 0.554 | 46.4 | 15.8 | take_profit:90 stop:59 lock_intraday_proxy:10 dte_exit:4 profit_lock:3 |
| c500_dev | pre128 | cash_debit | Stocks | short | 361 | -34,163 | -0.1051 | 0.371 | 18.5 | 4.2 | stop_intraday_proxy:163 take_profit:135 stop:37 lock_intraday_proxy:20 dte_exit:5 profit_lock:1 |
| c500_dev | pre128 | cash_debit | Stocks | swing | 74 | 1,297 | 0.017 | 0.541 | 45.4 | 14.8 | take_profit:40 stop:28 lock_intraday_proxy:3 dte_exit:2 profit_lock:1 |
| c500_dev | pre128 | cash_debit | ETFs | short | 152 | -19,290 | -0.1583 | 0.329 | 23.3 | 5 | stop_intraday_proxy:83 take_profit:50 lock_intraday_proxy:14 stop:3 profit_lock:1 expiry:1 |
| c500_dev | pre128 | cash_debit | ETFs | swing | 92 | 27,192 | 0.2519 | 0.565 | 47.2 | 16.6 | take_profit:50 stop:31 lock_intraday_proxy:7 dte_exit:2 profit_lock:2 |
| c500_dev | pre128_noproxy | margin | All | default | 509 | -14,976 | -0.0271 | 0.721 | 37.1 | 21 | take_profit:330 dte_exit:113 stop:57 expiry:9 |
| c500_dev | pre128_noproxy | margin | Stocks | default | 150 | -11,597 | -0.0614 | 0.707 | 36.4 | 21.7 | take_profit:92 dte_exit:36 stop:17 expiry:5 |
| c500_dev | pre128_noproxy | margin | ETFs | default | 359 | -3,379 | -0.0093 | 0.727 | 37.4 | 20.7 | take_profit:238 dte_exit:77 stop:40 expiry:4 |
| c500_dev | pre128_noproxy | cash_debit | All | short | 570 | -40,246 | -0.0579 | 0.428 | 19.8 | 5.4 | stop:285 take_profit:240 profit_lock:34 dte_exit:8 expiry:3 |
| c500_dev | pre128_noproxy | cash_debit | All | swing | 202 | 15,287 | 0.0389 | 0.51 | 45.6 | 15.7 | take_profit:96 stop:82 profit_lock:18 dte_exit:6 |
| c500_dev | pre128_noproxy | cash_debit | Stocks | short | 412 | -22,964 | -0.0435 | 0.444 | 18.7 | 5.3 | stop:200 take_profit:181 profit_lock:20 dte_exit:8 expiry:3 |
| c500_dev | pre128_noproxy | cash_debit | Stocks | swing | 103 | -1,049 | -0.0057 | 0.505 | 44.5 | 15.5 | take_profit:47 stop:42 profit_lock:9 dte_exit:5 |
| c500_dev | pre128_noproxy | cash_debit | ETFs | short | 158 | -17,282 | -0.1029 | 0.386 | 22.9 | 5.7 | stop:85 take_profit:59 profit_lock:14 |
| c500_dev | pre128_noproxy | cash_debit | ETFs | swing | 99 | 16,336 | 0.0787 | 0.515 | 46.8 | 16 | take_profit:49 stop:40 profit_lock:9 dte_exit:1 |
| c500_dev | new | margin | All | short | 419 | -10,856 | -0.117 | 0.432 | 21.3 | 5.7 | stop_intraday_proxy:176 take_profit:172 lock_intraday_proxy:28 dte_exit:22 stop:18 expiry:2 profit_lock:1 |
| c500_dev | new | margin | All | swing | 180 | -14,079 | -0.1282 | 0.494 | 46.9 | 19.9 | take_profit:74 stop:72 lock_intraday_proxy:18 dte_exit:14 expiry:2 |
| c500_dev | new | margin | Stocks | short | 266 | -9,541 | -0.1743 | 0.421 | 19.7 | 4.9 | stop_intraday_proxy:114 take_profit:108 lock_intraday_proxy:16 stop:14 dte_exit:13 expiry:1 |
| c500_dev | new | margin | Stocks | swing | 104 | -13,186 | -0.2405 | 0.452 | 46.1 | 19.9 | stop:43 take_profit:41 dte_exit:9 lock_intraday_proxy:9 expiry:2 |
| c500_dev | new | margin | ETFs | short | 153 | -1,315 | -0.0346 | 0.451 | 24.1 | 7.1 | take_profit:64 stop_intraday_proxy:62 lock_intraday_proxy:12 dte_exit:9 stop:4 expiry:1 profit_lock:1 |
| c500_dev | new | margin | ETFs | swing | 76 | -893 | -0.0162 | 0.553 | 48 | 19.9 | take_profit:33 stop:29 lock_intraday_proxy:9 dte_exit:5 |
| c500_dev | new | cash_debit | All | short | 648 | -35,044 | -0.1118 | 0.372 | 21.8 | 5.2 | stop_intraday_proxy:330 take_profit:240 lock_intraday_proxy:43 stop:27 expiry:4 profit_lock:2 dte_exit:2 |
| c500_dev | new | cash_debit | All | swing | 299 | 10,667 | 0.0352 | 0.532 | 47.1 | 18.5 | take_profit:135 stop:114 lock_intraday_proxy:31 dte_exit:17 expiry:1 profit_lock:1 |
| c500_dev | new | cash_debit | Stocks | short | 418 | -21,295 | -0.1069 | 0.364 | 20.1 | 4.8 | stop_intraday_proxy:211 take_profit:153 lock_intraday_proxy:29 stop:20 expiry:3 dte_exit:2 |
| c500_dev | new | cash_debit | Stocks | swing | 185 | 4,831 | 0.0244 | 0.541 | 47.1 | 18.5 | take_profit:81 stop:70 lock_intraday_proxy:21 dte_exit:11 expiry:1 profit_lock:1 |
| c500_dev | new | cash_debit | ETFs | short | 230 | -13,749 | -0.1204 | 0.387 | 24.9 | 6 | stop_intraday_proxy:119 take_profit:87 lock_intraday_proxy:14 stop:7 profit_lock:2 expiry:1 |
| c500_dev | new | cash_debit | ETFs | swing | 114 | 5,836 | 0.0554 | 0.518 | 47 | 18.5 | take_profit:54 stop:44 lock_intraday_proxy:10 dte_exit:6 |
| c500_dev | new_noproxy | margin | All | short | 652 | 6,585 | 0.013 | 0.472 | 22.2 | 7.4 | take_profit:294 stop:269 dte_exit:48 profit_lock:36 expiry:5 |
| c500_dev | new_noproxy | margin | All | swing | 332 | -29,788 | -0.0593 | 0.536 | 46.5 | 18.8 | take_profit:152 stop:129 profit_lock:25 dte_exit:24 expiry:2 |
| c500_dev | new_noproxy | margin | Stocks | short | 377 | 19,803 | 0.0707 | 0.472 | 20.4 | 6.8 | take_profit:170 stop:160 dte_exit:24 profit_lock:20 expiry:3 |
| c500_dev | new_noproxy | margin | Stocks | swing | 200 | -32,426 | -0.1035 | 0.515 | 45.8 | 17.8 | stop:88 take_profit:86 profit_lock:14 dte_exit:11 expiry:1 |
| c500_dev | new_noproxy | margin | ETFs | short | 275 | -13,218 | -0.0581 | 0.473 | 24.7 | 8.3 | take_profit:124 stop:109 dte_exit:24 profit_lock:16 expiry:2 |
| c500_dev | new_noproxy | margin | ETFs | swing | 132 | 2,637 | 0.014 | 0.568 | 47.7 | 20.3 | take_profit:66 stop:41 dte_exit:13 profit_lock:11 expiry:1 |
| c500_dev | new_noproxy | cash_debit | All | short | 517 | -12,729 | -0.0681 | 0.441 | 21.5 | 6.2 | stop:249 take_profit:227 profit_lock:30 dte_exit:8 expiry:3 |
| c500_dev | new_noproxy | cash_debit | All | swing | 276 | -12,182 | -0.0654 | 0.511 | 46.8 | 18.2 | take_profit:118 stop:109 profit_lock:31 dte_exit:16 expiry:2 |
| c500_dev | new_noproxy | cash_debit | Stocks | short | 313 | -14,494 | -0.1279 | 0.399 | 19.9 | 5.8 | stop:161 take_profit:127 profit_lock:17 dte_exit:7 expiry:1 |
| c500_dev | new_noproxy | cash_debit | Stocks | swing | 156 | -4,995 | -0.0483 | 0.532 | 45.8 | 18.1 | take_profit:66 stop:63 profit_lock:16 dte_exit:9 expiry:2 |
| c500_dev | new_noproxy | cash_debit | ETFs | short | 204 | 1,765 | 0.024 | 0.505 | 23.8 | 6.9 | take_profit:100 stop:88 profit_lock:13 expiry:2 dte_exit:1 |
| c500_dev | new_noproxy | cash_debit | ETFs | swing | 120 | -7,187 | -0.0867 | 0.483 | 48.1 | 18.4 | take_profit:52 stop:46 profit_lock:15 dte_exit:7 |
| c125_dev | pre128 | margin | All | default | 529 | -16,079 | -0.0259 | 0.724 | 37.2 | 21.1 | take_profit:337 dte_exit:117 stop:65 expiry:10 |
| c125_dev | pre128 | margin | Stocks | default | 176 | -2,874 | -0.0124 | 0.773 | 36.9 | 21.3 | take_profit:117 dte_exit:34 stop:19 expiry:6 |
| c125_dev | pre128 | margin | ETFs | default | 353 | -13,205 | -0.0339 | 0.7 | 37.4 | 21 | take_profit:220 dte_exit:83 stop:46 expiry:4 |
| c125_dev | pre128 | cash_debit | All | short | 252 | -23,951 | -0.1993 | 0.377 | 19.1 | 4.2 | stop_intraday_proxy:128 take_profit:95 stop:20 lock_intraday_proxy:6 profit_lock:2 expiry:1 |
| c125_dev | pre128 | cash_debit | All | swing | 91 | -1,004 | -0.0157 | 0.538 | 46.3 | 13.5 | take_profit:50 stop:33 lock_intraday_proxy:6 dte_exit:2 |
| c125_dev | pre128 | cash_debit | Stocks | short | 181 | -18,069 | -0.2272 | 0.37 | 17.9 | 3.9 | stop_intraday_proxy:93 take_profit:68 stop:18 lock_intraday_proxy:2 |
| c125_dev | pre128 | cash_debit | Stocks | swing | 44 | -6,252 | -0.2862 | 0.477 | 44.4 | 14 | take_profit:22 stop:19 dte_exit:2 lock_intraday_proxy:1 |
| c125_dev | pre128 | cash_debit | ETFs | short | 71 | -5,882 | -0.1448 | 0.394 | 22.3 | 4.9 | stop_intraday_proxy:35 take_profit:27 lock_intraday_proxy:4 stop:2 profit_lock:2 expiry:1 |
| c125_dev | pre128 | cash_debit | ETFs | swing | 47 | 5,248 | 0.1244 | 0.596 | 48 | 13.1 | take_profit:28 stop:14 lock_intraday_proxy:5 |
| c125_dev | pre128_noproxy | margin | All | default | 529 | -16,079 | -0.0259 | 0.724 | 37.2 | 21.1 | take_profit:337 dte_exit:117 stop:65 expiry:10 |
| c125_dev | pre128_noproxy | margin | Stocks | default | 176 | -2,874 | -0.0124 | 0.773 | 36.9 | 21.3 | take_profit:117 dte_exit:34 stop:19 expiry:6 |
| c125_dev | pre128_noproxy | margin | ETFs | default | 353 | -13,205 | -0.0339 | 0.7 | 37.4 | 21 | take_profit:220 dte_exit:83 stop:46 expiry:4 |
| c125_dev | pre128_noproxy | cash_debit | All | short | 402 | -30,001 | -0.1076 | 0.44 | 19.9 | 5.6 | stop:206 take_profit:176 profit_lock:17 expiry:2 dte_exit:1 |
| c125_dev | pre128_noproxy | cash_debit | All | swing | 165 | 5,059 | 0.0277 | 0.527 | 45.8 | 14.2 | take_profit:85 stop:57 profit_lock:18 dte_exit:5 |
| c125_dev | pre128_noproxy | cash_debit | Stocks | short | 309 | -19,045 | -0.0934 | 0.437 | 19 | 5.5 | stop:159 take_profit:134 profit_lock:14 dte_exit:1 expiry:1 |
| c125_dev | pre128_noproxy | cash_debit | Stocks | swing | 88 | -4,677 | -0.0529 | 0.523 | 44.3 | 13.5 | take_profit:43 stop:31 profit_lock:10 dte_exit:4 |
| c125_dev | pre128_noproxy | cash_debit | ETFs | short | 93 | -10,957 | -0.1461 | 0.452 | 22.9 | 6.1 | stop:47 take_profit:42 profit_lock:3 expiry:1 |
| c125_dev | pre128_noproxy | cash_debit | ETFs | swing | 77 | 9,736 | 0.1033 | 0.532 | 47.6 | 15 | take_profit:42 stop:26 profit_lock:8 dte_exit:1 |
| c125_dev | new | margin | All | short | 610 | -18,790 | -0.0728 | 0.41 | 22.5 | 6.2 | stop_intraday_proxy:272 take_profit:236 lock_intraday_proxy:40 stop:29 dte_exit:28 profit_lock:3 expiry:2 |
| c125_dev | new | margin | All | swing | 307 | -5,592 | -0.0205 | 0.577 | 46.9 | 18.8 | take_profit:152 stop:105 lock_intraday_proxy:32 dte_exit:15 expiry:2 profit_lock:1 |
| c125_dev | new | margin | Stocks | short | 389 | -6,624 | -0.0429 | 0.401 | 21.2 | 5.6 | stop_intraday_proxy:170 take_profit:148 lock_intraday_proxy:30 stop:23 dte_exit:14 expiry:2 profit_lock:2 |
| c125_dev | new | margin | Stocks | swing | 183 | -10,381 | -0.0722 | 0.53 | 46.1 | 17.6 | take_profit:81 stop:69 lock_intraday_proxy:21 dte_exit:10 expiry:1 profit_lock:1 |
| c125_dev | new | margin | ETFs | short | 221 | -12,166 | -0.1172 | 0.425 | 24.8 | 7.2 | stop_intraday_proxy:102 take_profit:88 dte_exit:14 lock_intraday_proxy:10 stop:6 profit_lock:1 |
| c125_dev | new | margin | ETFs | swing | 124 | 4,788 | 0.037 | 0.645 | 48 | 20.5 | take_profit:71 stop:36 lock_intraday_proxy:11 dte_exit:5 expiry:1 |
| c125_dev | new | cash_debit | All | short | 327 | -14,975 | -0.1809 | 0.398 | 21.6 | 5 | stop_intraday_proxy:171 take_profit:130 lock_intraday_proxy:13 stop:9 profit_lock:4 |
| c125_dev | new | cash_debit | All | swing | 162 | -9,953 | -0.1049 | 0.475 | 45.8 | 19 | take_profit:68 stop:59 lock_intraday_proxy:16 dte_exit:10 profit_lock:5 expiry:4 |
| c125_dev | new | cash_debit | Stocks | short | 222 | -7,532 | -0.1568 | 0.41 | 20.6 | 5.1 | stop_intraday_proxy:109 take_profit:90 lock_intraday_proxy:12 stop:8 profit_lock:3 |
| c125_dev | new | cash_debit | Stocks | swing | 82 | -5,963 | -0.126 | 0.512 | 44.7 | 17.2 | take_profit:37 stop:29 lock_intraday_proxy:9 dte_exit:4 profit_lock:2 expiry:1 |
| c125_dev | new | cash_debit | ETFs | short | 105 | -7,443 | -0.2141 | 0.371 | 23.8 | 4.8 | stop_intraday_proxy:62 take_profit:40 lock_intraday_proxy:1 profit_lock:1 stop:1 |
| c125_dev | new | cash_debit | ETFs | swing | 80 | -3,990 | -0.0839 | 0.438 | 46.9 | 20.7 | take_profit:31 stop:30 lock_intraday_proxy:7 dte_exit:6 profit_lock:3 expiry:3 |
| c125_dev | new_noproxy | margin | All | short | 662 | -3,669 | -0.009 | 0.494 | 22 | 6.9 | take_profit:309 stop:282 profit_lock:34 dte_exit:32 expiry:5 |
| c125_dev | new_noproxy | margin | All | swing | 336 | -1,465 | -0.0033 | 0.539 | 47 | 19.4 | take_profit:156 stop:129 profit_lock:26 dte_exit:22 expiry:3 |
| c125_dev | new_noproxy | margin | Stocks | short | 457 | 632 | 0.0024 | 0.499 | 21.1 | 6.3 | take_profit:220 stop:191 profit_lock:28 dte_exit:14 expiry:4 |
| c125_dev | new_noproxy | margin | Stocks | swing | 202 | -17,878 | -0.0652 | 0.5 | 46 | 19.1 | stop:88 take_profit:82 profit_lock:15 dte_exit:15 expiry:2 |
| c125_dev | new_noproxy | margin | ETFs | short | 205 | -4,301 | -0.0298 | 0.483 | 24.1 | 8.3 | stop:91 take_profit:89 dte_exit:18 profit_lock:6 expiry:1 |
| c125_dev | new_noproxy | margin | ETFs | swing | 134 | 16,414 | 0.0934 | 0.597 | 48.5 | 20 | take_profit:74 stop:41 profit_lock:11 dte_exit:7 expiry:1 |
| c125_dev | new_noproxy | cash_debit | All | short | 458 | -18,375 | -0.1108 | 0.434 | 22.2 | 6.7 | stop:231 take_profit:194 profit_lock:27 dte_exit:3 expiry:3 |
| c125_dev | new_noproxy | cash_debit | All | swing | 238 | -6,518 | -0.0331 | 0.462 | 47.7 | 18.7 | stop:105 take_profit:97 profit_lock:22 dte_exit:13 expiry:1 |
| c125_dev | new_noproxy | cash_debit | Stocks | short | 298 | -8,586 | -0.0841 | 0.44 | 21.4 | 6.6 | stop:147 take_profit:129 profit_lock:18 dte_exit:2 expiry:2 |
| c125_dev | new_noproxy | cash_debit | Stocks | swing | 149 | 5,123 | 0.0453 | 0.503 | 47.3 | 18.5 | stop:64 take_profit:63 profit_lock:14 dte_exit:8 |
| c125_dev | new_noproxy | cash_debit | ETFs | short | 160 | -9,789 | -0.1537 | 0.425 | 23.6 | 6.7 | stop:84 take_profit:65 profit_lock:9 dte_exit:1 expiry:1 |
| c125_dev | new_noproxy | cash_debit | ETFs | swing | 89 | -11,641 | -0.1389 | 0.393 | 48.2 | 19.1 | stop:41 take_profit:34 profit_lock:8 dte_exit:5 expiry:1 |

## dte_buckets
`dte_buckets.csv`

| book | variant | profile | dte_bucket | n | net | per_risk |
|---|---|---|---|---|---|---|
| c500_dev | old | margin | [21, 36) | 82 | 1,278 | 0.0173 |
| c500_dev | old | margin | [36, 46) | 398 | -19,017 | -0.0476 |
| c500_dev | old | cash_debit | [21, 36) | 279 | -38,502 | -0.1464 |
| c500_dev | old | cash_debit | [36, 46) | 395 | 13,576 | 0.0364 |
| c500_dev | pre128 | margin | [21, 36) | 202 | -3,130 | -0.0154 |
| c500_dev | pre128 | margin | [36, 46) | 307 | -11,846 | -0.0339 |
| c500_dev | pre128 | cash_debit | [10, 21) | 248 | -30,606 | -0.1385 |
| c500_dev | pre128 | cash_debit | [21, 36) | 265 | -22,846 | -0.1012 |
| c500_dev | pre128 | cash_debit | [36, 46) | 91 | 6,834 | 0.0697 |
| c500_dev | pre128 | cash_debit | [46, 61) | 75 | 21,655 | 0.2518 |
| c500_dev | pre128_noproxy | margin | [21, 36) | 202 | -3,130 | -0.0154 |
| c500_dev | pre128_noproxy | margin | [36, 46) | 307 | -11,846 | -0.0339 |
| c500_dev | pre128_noproxy | cash_debit | [10, 21) | 291 | -11,666 | -0.0309 |
| c500_dev | pre128_noproxy | cash_debit | [21, 36) | 279 | -28,580 | -0.0898 |
| c500_dev | pre128_noproxy | cash_debit | [36, 46) | 119 | 21,869 | 0.1002 |
| c500_dev | pre128_noproxy | cash_debit | [46, 61) | 83 | -6,583 | -0.0377 |
| c500_dev | new | margin | [10, 21) | 169 | -4,177 | -0.191 |
| c500_dev | new | margin | [21, 36) | 250 | -6,679 | -0.0942 |
| c500_dev | new | margin | [36, 46) | 91 | -8,969 | -0.1887 |
| c500_dev | new | margin | [46, 61) | 89 | -5,110 | -0.082 |
| c500_dev | new | cash_debit | [10, 21) | 246 | -17,029 | -0.167 |
| c500_dev | new | cash_debit | [21, 36) | 402 | -18,014 | -0.0852 |
| c500_dev | new | cash_debit | [36, 46) | 155 | 5,615 | 0.038 |
| c500_dev | new | cash_debit | [46, 61) | 144 | 5,052 | 0.0325 |
| c500_dev | new_noproxy | margin | [10, 21) | 228 | 21,759 | 0.1498 |
| c500_dev | new_noproxy | margin | [21, 36) | 424 | -15,175 | -0.0419 |
| c500_dev | new_noproxy | margin | [36, 46) | 170 | -46,687 | -0.2009 |
| c500_dev | new_noproxy | margin | [46, 61) | 162 | 16,899 | 0.0626 |
| c500_dev | new_noproxy | cash_debit | [10, 21) | 197 | -13,935 | -0.2216 |
| c500_dev | new_noproxy | cash_debit | [21, 36) | 320 | 1,206 | 0.0097 |
| c500_dev | new_noproxy | cash_debit | [36, 46) | 142 | -7,875 | -0.0973 |
| c500_dev | new_noproxy | cash_debit | [46, 61) | 134 | -4,306 | -0.0409 |
| c125_dev | old | margin | [21, 36) | 70 | -3,854 | -0.0574 |
| c125_dev | old | margin | [36, 46) | 365 | -19,040 | -0.0517 |
| c125_dev | old | cash_debit | [21, 36) | 217 | -16,881 | -0.1001 |
| c125_dev | old | cash_debit | [36, 46) | 268 | -8,011 | -0.0404 |
| c125_dev | pre128 | margin | [21, 36) | 200 | 3,891 | 0.0193 |
| c125_dev | pre128 | margin | [36, 46) | 329 | -19,971 | -0.0477 |
| c125_dev | pre128 | cash_debit | [10, 21) | 135 | -22,904 | -0.3466 |
| c125_dev | pre128 | cash_debit | [21, 36) | 117 | -1,047 | -0.0194 |
| c125_dev | pre128 | cash_debit | [36, 46) | 52 | 1,050 | 0.0424 |
| c125_dev | pre128 | cash_debit | [46, 61) | 39 | -2,054 | -0.0523 |
| c125_dev | pre128_noproxy | margin | [21, 36) | 200 | 3,891 | 0.0193 |
| c125_dev | pre128_noproxy | margin | [36, 46) | 329 | -19,971 | -0.0477 |
| c125_dev | pre128_noproxy | cash_debit | [10, 21) | 202 | -27,083 | -0.1775 |
| c125_dev | pre128_noproxy | cash_debit | [21, 36) | 200 | -2,919 | -0.0231 |
| c125_dev | pre128_noproxy | cash_debit | [36, 46) | 97 | -9,394 | -0.1024 |
| c125_dev | pre128_noproxy | cash_debit | [46, 61) | 68 | 14,454 | 0.1592 |
| c125_dev | new | margin | [10, 21) | 202 | 522 | 0.0075 |
| c125_dev | new | margin | [21, 36) | 408 | -19,312 | -0.1026 |
| c125_dev | new | margin | [36, 46) | 160 | -11,431 | -0.0926 |
| c125_dev | new | margin | [46, 61) | 147 | 5,839 | 0.039 |
| c125_dev | new | cash_debit | [10, 21) | 129 | -8,758 | -0.3467 |
| c125_dev | new | cash_debit | [21, 36) | 198 | -6,217 | -0.1081 |
| c125_dev | new | cash_debit | [36, 46) | 94 | -5,070 | -0.1062 |
| c125_dev | new | cash_debit | [46, 61) | 68 | -4,883 | -0.1036 |
| c125_dev | new_noproxy | margin | [10, 21) | 231 | 7,626 | 0.071 |
| c125_dev | new_noproxy | margin | [21, 36) | 431 | -11,295 | -0.0378 |
| c125_dev | new_noproxy | margin | [36, 46) | 165 | -2,446 | -0.0116 |
| c125_dev | new_noproxy | margin | [46, 61) | 171 | 981 | 0.0041 |
| c125_dev | new_noproxy | cash_debit | [10, 21) | 152 | -10,930 | -0.2342 |
| c125_dev | new_noproxy | cash_debit | [21, 36) | 306 | -7,445 | -0.0625 |
| c125_dev | new_noproxy | cash_debit | [36, 46) | 121 | 344 | 0.0041 |
| c125_dev | new_noproxy | cash_debit | [46, 61) | 117 | -6,861 | -0.0612 |

## exit_mix
`exit_mix.csv`

| book | variant | profile | exit_reason | n | share | net |
|---|---|---|---|---|---|---|
| c500_dev | old | margin | dte_exit | 83 | 0.173 | -23,471 |
| c500_dev | old | margin | expiry | 6 | 0.013 | 1,032 |
| c500_dev | old | margin | stop | 71 | 0.148 | -56,963 |
| c500_dev | old | margin | take_profit | 320 | 0.667 | 61,663 |
| c500_dev | old | cash_debit | dte_exit | 32 | 0.047 | -10,778 |
| c500_dev | old | cash_debit | expiry | 2 | 0.003 | -1,104 |
| c500_dev | old | cash_debit | profit_lock | 38 | 0.056 | -2,240 |
| c500_dev | old | cash_debit | stop | 291 | 0.432 | -238,503 |
| c500_dev | old | cash_debit | take_profit | 311 | 0.461 | 227,699 |
| c500_dev | pre128 | margin | dte_exit | 113 | 0.222 | -27,471 |
| c500_dev | pre128 | margin | expiry | 9 | 0.018 | 2,589 |
| c500_dev | pre128 | margin | stop | 57 | 0.112 | -63,862 |
| c500_dev | pre128 | margin | take_profit | 330 | 0.648 | 73,768 |
| c500_dev | pre128 | cash_debit | dte_exit | 9 | 0.013 | -1,299 |
| c500_dev | pre128 | cash_debit | expiry | 1 | 0.001 | 585 |
| c500_dev | pre128 | cash_debit | lock_intraday_proxy | 44 | 0.065 | -7,609 |
| c500_dev | pre128 | cash_debit | profit_lock | 5 | 0.007 | -663 |
| c500_dev | pre128 | cash_debit | stop | 99 | 0.146 | -71,775 |
| c500_dev | pre128 | cash_debit | stop_intraday_proxy | 246 | 0.362 | -125,595 |
| c500_dev | pre128 | cash_debit | take_profit | 275 | 0.405 | 181,393 |
| c500_dev | pre128_noproxy | margin | dte_exit | 113 | 0.222 | -27,471 |
| c500_dev | pre128_noproxy | margin | expiry | 9 | 0.018 | 2,589 |
| c500_dev | pre128_noproxy | margin | stop | 57 | 0.112 | -63,862 |
| c500_dev | pre128_noproxy | margin | take_profit | 330 | 0.648 | 73,768 |
| c500_dev | pre128_noproxy | cash_debit | dte_exit | 14 | 0.018 | -5,053 |
| c500_dev | pre128_noproxy | cash_debit | expiry | 3 | 0.004 | -5,858 |
| c500_dev | pre128_noproxy | cash_debit | profit_lock | 52 | 0.067 | -6,865 |
| c500_dev | pre128_noproxy | cash_debit | stop | 367 | 0.475 | -350,366 |
| c500_dev | pre128_noproxy | cash_debit | take_profit | 336 | 0.435 | 343,182 |
| c500_dev | new | margin | dte_exit | 36 | 0.06 | -1,094 |
| c500_dev | new | margin | expiry | 4 | 0.007 | -3,326 |
| c500_dev | new | margin | lock_intraday_proxy | 46 | 0.077 | -1,474 |
| c500_dev | new | margin | profit_lock | 1 | 0.002 | -53 |
| c500_dev | new | margin | stop | 90 | 0.15 | -43,522 |
| c500_dev | new | margin | stop_intraday_proxy | 176 | 0.294 | -22,542 |
| c500_dev | new | margin | take_profit | 246 | 0.411 | 47,076 |
| c500_dev | new | cash_debit | dte_exit | 19 | 0.02 | -4,888 |
| c500_dev | new | cash_debit | expiry | 5 | 0.005 | -5,393 |
| c500_dev | new | cash_debit | lock_intraday_proxy | 74 | 0.078 | -594 |
| c500_dev | new | cash_debit | profit_lock | 3 | 0.003 | 402 |
| c500_dev | new | cash_debit | stop | 141 | 0.149 | -97,981 |
| c500_dev | new | cash_debit | stop_intraday_proxy | 330 | 0.348 | -94,082 |
| c500_dev | new | cash_debit | take_profit | 375 | 0.396 | 178,159 |
| c500_dev | new_noproxy | margin | dte_exit | 72 | 0.073 | -8,686 |
| c500_dev | new_noproxy | margin | expiry | 7 | 0.007 | 1,036 |
| c500_dev | new_noproxy | margin | profit_lock | 61 | 0.062 | -1,538 |
| c500_dev | new_noproxy | margin | stop | 398 | 0.404 | -282,169 |
| c500_dev | new_noproxy | margin | take_profit | 446 | 0.453 | 268,153 |
| c500_dev | new_noproxy | cash_debit | dte_exit | 24 | 0.03 | -1,639 |
| c500_dev | new_noproxy | cash_debit | expiry | 5 | 0.006 | -3,090 |
| c500_dev | new_noproxy | cash_debit | profit_lock | 61 | 0.077 | -2,390 |
| c500_dev | new_noproxy | cash_debit | stop | 358 | 0.451 | -120,507 |
| c500_dev | new_noproxy | cash_debit | take_profit | 345 | 0.435 | 102,715 |
| c125_dev | old | margin | dte_exit | 73 | 0.168 | -18,713 |
| c125_dev | old | margin | expiry | 7 | 0.016 | -1,835 |
| c125_dev | old | margin | stop | 77 | 0.177 | -59,464 |
| c125_dev | old | margin | take_profit | 278 | 0.639 | 57,117 |
| c125_dev | old | cash_debit | dte_exit | 20 | 0.041 | -480 |
| c125_dev | old | cash_debit | profit_lock | 22 | 0.045 | -1,520 |
| c125_dev | old | cash_debit | stop | 209 | 0.431 | -149,813 |
| c125_dev | old | cash_debit | take_profit | 234 | 0.482 | 126,921 |
| c125_dev | pre128 | margin | dte_exit | 117 | 0.221 | -28,228 |
| c125_dev | pre128 | margin | expiry | 10 | 0.019 | -30 |
| c125_dev | pre128 | margin | stop | 65 | 0.123 | -69,379 |
| c125_dev | pre128 | margin | take_profit | 337 | 0.637 | 81,558 |
| c125_dev | pre128 | cash_debit | dte_exit | 2 | 0.006 | 82 |
| c125_dev | pre128 | cash_debit | expiry | 1 | 0.003 | 901 |
| c125_dev | pre128 | cash_debit | lock_intraday_proxy | 12 | 0.035 | -1,005 |
| c125_dev | pre128 | cash_debit | profit_lock | 2 | 0.006 | -78 |
| c125_dev | pre128 | cash_debit | stop | 53 | 0.155 | -29,648 |
| c125_dev | pre128 | cash_debit | stop_intraday_proxy | 128 | 0.373 | -40,842 |
| c125_dev | pre128 | cash_debit | take_profit | 145 | 0.423 | 45,635 |
| c125_dev | pre128_noproxy | margin | dte_exit | 117 | 0.221 | -28,228 |
| c125_dev | pre128_noproxy | margin | expiry | 10 | 0.019 | -30 |
| c125_dev | pre128_noproxy | margin | stop | 65 | 0.123 | -69,379 |
| c125_dev | pre128_noproxy | margin | take_profit | 337 | 0.637 | 81,558 |
| c125_dev | pre128_noproxy | cash_debit | dte_exit | 6 | 0.011 | -1,664 |
| c125_dev | pre128_noproxy | cash_debit | expiry | 2 | 0.004 | 1,571 |
| c125_dev | pre128_noproxy | cash_debit | profit_lock | 35 | 0.062 | -4,573 |
| c125_dev | pre128_noproxy | cash_debit | stop | 263 | 0.464 | -150,413 |
| c125_dev | pre128_noproxy | cash_debit | take_profit | 261 | 0.46 | 130,137 |
| c125_dev | new | margin | dte_exit | 43 | 0.047 | -4,473 |
| c125_dev | new | margin | expiry | 4 | 0.004 | -3,138 |
| c125_dev | new | margin | lock_intraday_proxy | 72 | 0.079 | -1,039 |
| c125_dev | new | margin | profit_lock | 4 | 0.004 | -33 |
| c125_dev | new | margin | stop | 134 | 0.146 | -90,216 |
| c125_dev | new | margin | stop_intraday_proxy | 272 | 0.297 | -60,606 |
| c125_dev | new | margin | take_profit | 388 | 0.423 | 135,123 |
| c125_dev | new | cash_debit | dte_exit | 10 | 0.02 | -1,548 |
| c125_dev | new | cash_debit | expiry | 4 | 0.008 | -3,190 |
| c125_dev | new | cash_debit | lock_intraday_proxy | 29 | 0.059 | -743 |
| c125_dev | new | cash_debit | profit_lock | 9 | 0.018 | -1,546 |
| c125_dev | new | cash_debit | stop | 68 | 0.139 | -33,392 |
| c125_dev | new | cash_debit | stop_intraday_proxy | 171 | 0.35 | -27,275 |
| c125_dev | new | cash_debit | take_profit | 198 | 0.405 | 42,767 |
| c125_dev | new_noproxy | margin | dte_exit | 54 | 0.054 | 676 |
| c125_dev | new_noproxy | margin | expiry | 8 | 0.008 | -401 |
| c125_dev | new_noproxy | margin | profit_lock | 60 | 0.06 | -4,431 |
| c125_dev | new_noproxy | margin | stop | 411 | 0.412 | -244,141 |
| c125_dev | new_noproxy | margin | take_profit | 465 | 0.466 | 243,163 |
| c125_dev | new_noproxy | cash_debit | dte_exit | 16 | 0.023 | -3,164 |
| c125_dev | new_noproxy | cash_debit | expiry | 4 | 0.006 | -1,994 |
| c125_dev | new_noproxy | cash_debit | profit_lock | 49 | 0.07 | -2,789 |
| c125_dev | new_noproxy | cash_debit | stop | 336 | 0.483 | -114,329 |
| c125_dev | new_noproxy | cash_debit | take_profit | 291 | 0.418 | 97,384 |

## skips
`skips.csv`

| book | variant | profile | sleeve_full | gate_dte_window | gate_max_open_positions | gate_daily_loss | buying_power | cash | sizing | gate_open_risk_cap | gate_short_premium_regime |
|---|---|---|---|---|---|---|---|---|---|---|---|
| c500_dev | old | margin | 0 | 0 | 3561 | 5688 | 0 | 0 | 1089 | 0 | 0 |
| c500_dev | old | cash_debit | 0 | 0 | 4877 | 14416 | 0 | 44 | 5437 | 0 | 0 |
| c500_dev | pre128 | margin | 0 | 0 | 5695 | 6585 | 0 | 0 | 1592 | 0 | 0 |
| c500_dev | pre128 | cash_debit | 1149 | 0 | 6550 | 9181 | 0 | 117 | 29605 | 0 | 0 |
| c500_dev | pre128_noproxy | margin | 0 | 0 | 5695 | 6585 | 0 | 0 | 1592 | 0 | 0 |
| c500_dev | pre128_noproxy | cash_debit | 1735 | 0 | 8484 | 11648 | 0 | 68 | 24996 | 0 | 0 |
| c500_dev | new | margin | 6213 | 0 | 1739 | 10362 | 0 | 0 | 50345 | 0 | 120 |
| c500_dev | new | cash_debit | 6401 | 0 | 2447 | 14744 | 0 | 0 | 19362 | 0 | 0 |
| c500_dev | new_noproxy | margin | 11683 | 0 | 14566 | 12578 | 0 | 0 | 27646 | 0 | 187 |
| c500_dev | new_noproxy | cash_debit | 5343 | 0 | 2325 | 13405 | 0 | 0 | 21707 | 0 | 0 |
| c125_dev | old | margin | 0 | 0 | 2810 | 6987 | 0 | 0 | 2037 | 0 | 0 |
| c125_dev | old | cash_debit | 0 | 0 | 2743 | 11385 | 0 | 8 | 7942 | 0 | 0 |
| c125_dev | pre128 | margin | 0 | 0 | 6797 | 6730 | 0 | 0 | 1402 | 0 | 0 |
| c125_dev | pre128 | cash_debit | 1100 | 0 | 2289 | 5462 | 0 | 11 | 36231 | 0 | 0 |
| c125_dev | pre128_noproxy | margin | 0 | 0 | 6797 | 6730 | 0 | 0 | 1402 | 0 | 0 |
| c125_dev | pre128_noproxy | cash_debit | 1189 | 0 | 5024 | 9552 | 0 | 65 | 29171 | 0 | 0 |
| c125_dev | new | margin | 10347 | 0 | 8689 | 12687 | 0 | 0 | 36289 | 0 | 142 |
| c125_dev | new | cash_debit | 2910 | 0 | 618 | 9134 | 0 | 0 | 28735 | 0 | 0 |
| c125_dev | new_noproxy | margin | 13213 | 0 | 15574 | 11679 | 0 | 0 | 27295 | 0 | 197 |
| c125_dev | new_noproxy | cash_debit | 3975 | 0 | 2084 | 12234 | 0 | 0 | 23334 | 0 | 0 |

## runtime
`runtime.csv`

| book | variant | git | build_s | walk_s | total_s | peak_rss_mb | cache_hit_rate |
|---|---|---|---|---|---|---|---|
| c500_dev | old | ae3cf19 | 1,294 | 8.4 | 1,302 | 1,945 | 1 |
| c500_dev | pre128 | e1aa20e | 18,525 | 12.7 | 18,538 | 3,810 | 0.2776 |
| c500_dev | pre128_noproxy | e1aa20e | 1,858 | 12.6 | 1,870 | 3,818 | 1 |
| c500_dev | new | 4ae9e64 | 40,358 | 13 | 40,372 | 3,331 | 0.2776 |
| c500_dev | new_noproxy | 4ae9e64 | 2,522 | 11.8 | 2,534 | 4,619 | 1 |
| c125_dev | old | ae3cf19 | 534.7 | 7.1 | 541.8 | 1,945 | 1 |
| c125_dev | pre128 | f5f1da0 | 16,823 | 4.5 | 16,828 | 2,409 | 0.04188 |
| c125_dev | pre128_noproxy | f5f1da0 | 700.1 | 4.3 | 704.4 | 1,884 | 1 |
| c125_dev | new | 4ae9e64 | 29,778 | 8.1 | 29,786 | 3,587 | 0 |
| c125_dev | new_noproxy | 4ae9e64 | 951.5 | 7.6 | 959.2 | 3,663 | 1 |

## ablation
`ablation.csv`

| book | profile | entry_filters | ranker | credit_only | bp | d131 | trades | net_pnl | max_dd_pct | per_risk | net_at_mid | win_rate | eq_2021_12_31 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| c125_dev | cash_debit | live | debit_width | False | False | False | 426 | -24,955 | -99.82 | -0.1192 | -4,917 | 0.408 | 74 |
| c125_dev | cash_debit | live | debit_width | False | False | True | 543 | -24,942 | -99.79 | -0.1243 | -3,318 | 0.418 | 844 |
| c125_dev | cash_debit | live | managed_net_ev | False | False | False | 442 | -24,927 | -99.74 | -0.1002 | -12,364 | 0.441 | 135 |
| c125_dev | cash_debit | live | managed_net_ev | False | False | True | 489 | -24,928 | -99.76 | -0.1403 | -19,625 | 0.423 | 940 |
| c125_dev | cash_debit | none | debit_width | False | False | False | 343 | -24,955 | -99.84 | -0.1355 | -4,686 | 0.42 | 72 |
| c125_dev | cash_debit | none | debit_width | False | False | True | 550 | -24,942 | -99.77 | -0.1181 | 2,273 | 0.405 | 612 |
| c125_dev | cash_debit | none | managed_net_ev | False | False | False | 565 | -24,933 | -99.85 | -0.0506 | -7,197 | 0.416 | 197 |
| c125_dev | cash_debit | none | managed_net_ev | False | False | True | 608 | -24,926 | -99.74 | -0.1295 | -17,301 | 0.414 | 1,181 |
| c125_dev | margin | live | credit_width | False | False | False | 379 | -24,954 | -99.83 | -0.1833 | -17,863 | 0.456 | 100 |
| c125_dev | margin | live | credit_width | False | False | True | 826 | -24,901 | -99.69 | -0.0697 | -7,723 | 0.522 | 9,266 |
| c125_dev | margin | live | credit_width | False | True | False | 423 | -24,941 | -99.79 | -0.1568 | -15,972 | 0.475 | 106 |
| c125_dev | margin | live | credit_width | False | True | True | 826 | -24,901 | -99.69 | -0.0697 | -7,723 | 0.522 | 9,266 |
| c125_dev | margin | live | credit_width | True | False | False | 707 | -16,327 | -85.22 | -0.0169 | 12,928 | 0.69 | 25,506 |
| c125_dev | margin | live | credit_width | True | False | True | 486 | -16,893 | -75.74 | -0.0349 | -1,400 | 0.673 | 18,004 |
| c125_dev | margin | live | credit_width | True | True | False | 702 | -18,639 | -86.5 | -0.0198 | 9,693 | 0.689 | 25,506 |
| c125_dev | margin | live | credit_width | True | True | True | 486 | -16,893 | -75.74 | -0.0349 | -1,400 | 0.673 | 18,004 |
| c125_dev | margin | live | managed_net_ev | False | False | False | 518 | -24,929 | -99.82 | -0.0865 | -15,205 | 0.465 | 195 |
| c125_dev | margin | live | managed_net_ev | False | False | True | 917 | -24,382 | -98.39 | -0.0459 | -10,940 | 0.466 | 17,600 |
| c125_dev | margin | live | managed_net_ev | False | True | False | 469 | -24,935 | -99.78 | -0.0846 | -16,018 | 0.456 | 153 |
| c125_dev | margin | live | managed_net_ev | False | True | True | 917 | -24,382 | -98.39 | -0.0459 | -10,940 | 0.466 | 17,600 |
| c125_dev | margin | live | managed_net_ev | True | False | False | 641 | -23,406 | -95.84 | -0.0293 | -7,496 | 0.711 | 25,608 |
| c125_dev | margin | live | managed_net_ev | True | False | True | 551 | -6,223 | -56.32 | -0.01 | 3,774 | 0.733 | 27,495 |
| c125_dev | margin | live | managed_net_ev | True | True | False | 641 | -23,406 | -95.84 | -0.0293 | -7,496 | 0.711 | 25,608 |
| c125_dev | margin | live | managed_net_ev | True | True | True | 551 | -6,223 | -56.32 | -0.01 | 3,774 | 0.733 | 27,495 |
| c125_dev | margin | none | credit_width | False | False | False | 427 | -24,954 | -99.84 | -0.1281 | -14,954 | 0.471 | 121 |
| c125_dev | margin | none | credit_width | False | False | True | 960 | -24,258 | -98.74 | -0.0487 | 2,835 | 0.519 | 11,621 |
| c125_dev | margin | none | credit_width | False | True | False | 356 | -24,951 | -99.83 | -0.1925 | -17,065 | 0.43 | 127 |
| c125_dev | margin | none | credit_width | False | True | True | 960 | -24,258 | -98.74 | -0.0487 | 2,835 | 0.519 | 11,621 |
| c125_dev | margin | none | credit_width | True | False | False | 707 | -16,327 | -85.22 | -0.0169 | 12,928 | 0.69 | 25,506 |
| c125_dev | margin | none | credit_width | True | False | True | 486 | -16,893 | -75.74 | -0.0349 | -1,400 | 0.673 | 18,004 |
| c125_dev | margin | none | credit_width | True | True | False | 702 | -18,639 | -86.5 | -0.0198 | 9,693 | 0.689 | 25,506 |
| c125_dev | margin | none | credit_width | True | True | True | 486 | -16,893 | -75.74 | -0.0349 | -1,400 | 0.673 | 18,004 |
| c125_dev | margin | none | managed_net_ev | False | False | False | 469 | -24,932 | -99.77 | -0.1038 | -16,698 | 0.458 | 196 |
| c125_dev | margin | none | managed_net_ev | False | False | True | 764 | -24,519 | -99.34 | -0.0956 | -17,207 | 0.429 | 4,524 |
| c125_dev | margin | none | managed_net_ev | False | True | False | 470 | -24,934 | -99.77 | -0.1032 | -17,325 | 0.464 | 173 |
| c125_dev | margin | none | managed_net_ev | False | True | True | 764 | -24,519 | -99.34 | -0.0956 | -17,207 | 0.429 | 4,524 |
| c125_dev | margin | none | managed_net_ev | True | False | False | 680 | -22,412 | -91.62 | -0.028 | -7,004 | 0.701 | 25,150 |
| c125_dev | margin | none | managed_net_ev | True | False | True | 539 | -6,617 | -57.81 | -0.0107 | 2,877 | 0.716 | 28,887 |
| c125_dev | margin | none | managed_net_ev | True | True | False | 645 | -23,428 | -95.23 | -0.0306 | -8,635 | 0.696 | 25,150 |
| c125_dev | margin | none | managed_net_ev | True | True | True | 539 | -6,617 | -57.81 | -0.0107 | 2,877 | 0.716 | 28,887 |
| c500_dev | cash_debit | live | debit_width | False | False | False | 487 | -24,963 | -99.89 | -0.0802 | 8,505 | 0.4 | 65 |
| c500_dev | cash_debit | live | debit_width | False | False | True | 650 | -24,959 | -99.88 | -0.0599 | 22,427 | 0.395 | 492 |
| c500_dev | cash_debit | live | managed_net_ev | False | False | False | 348 | -24,955 | -99.85 | -0.1529 | -18,356 | 0.379 | 63 |
| c500_dev | cash_debit | live | managed_net_ev | False | False | True | 947 | -24,376 | -99.5 | -0.0395 | -7,243 | 0.422 | 10,349 |
| c500_dev | cash_debit | none | debit_width | False | False | False | 674 | -24,962 | -99.92 | -0.0396 | 45,560 | 0.404 | 66 |
| c500_dev | cash_debit | none | debit_width | False | False | True | 638 | -24,960 | -99.9 | -0.0725 | 23,381 | 0.387 | 388 |
| c500_dev | cash_debit | none | managed_net_ev | False | False | False | 544 | -24,956 | -99.88 | -0.0758 | -13,303 | 0.41 | 120 |
| c500_dev | cash_debit | none | managed_net_ev | False | False | True | 830 | -24,925 | -99.83 | -0.0538 | -13,675 | 0.441 | 4,704 |
| c500_dev | margin | live | credit_width | False | False | False | 778 | -24,951 | -99.85 | -0.0547 | 2,917 | 0.537 | 4,412 |
| c500_dev | margin | live | credit_width | False | False | True | 977 | -23,083 | -94.25 | -0.0435 | 7,849 | 0.539 | 20,954 |
| c500_dev | margin | live | credit_width | False | True | False | 794 | -24,954 | -99.86 | -0.0461 | 3,614 | 0.526 | 10,277 |
| c500_dev | margin | live | credit_width | False | True | True | 977 | -23,083 | -94.25 | -0.0435 | 7,849 | 0.539 | 20,954 |
| c500_dev | margin | live | credit_width | True | False | False | 630 | -22,660 | -94.4 | -0.0455 | -833 | 0.662 | 11,573 |
| c500_dev | margin | live | credit_width | True | False | True | 585 | 1,851 | -64.52 | 0.002 | 34,533 | 0.706 | 54,060 |
| c500_dev | margin | live | credit_width | True | True | False | 602 | -23,940 | -97.46 | -0.0496 | -2,600 | 0.661 | 10,074 |
| c500_dev | margin | live | credit_width | True | True | True | 585 | 1,851 | -64.52 | 0.002 | 34,533 | 0.706 | 54,060 |
| c500_dev | margin | live | managed_net_ev | False | False | False | 528 | -24,955 | -99.89 | -0.0958 | -15,947 | 0.451 | 115 |
| c500_dev | margin | live | managed_net_ev | False | False | True | 599 | -24,934 | -99.75 | -0.1231 | -19,410 | 0.451 | 1,396 |
| c500_dev | margin | live | managed_net_ev | False | True | False | 551 | -24,950 | -99.85 | -0.1077 | -15,538 | 0.434 | 170 |
| c500_dev | margin | live | managed_net_ev | False | True | True | 599 | -24,934 | -99.75 | -0.1231 | -19,410 | 0.451 | 1,396 |
| c500_dev | margin | live | managed_net_ev | True | False | False | 640 | -23,711 | -96.39 | -0.0432 | -7,145 | 0.686 | 16,373 |
| c500_dev | margin | live | managed_net_ev | True | False | True | 579 | -4,569 | -69.95 | -0.0048 | 12,049 | 0.765 | 47,726 |
| c500_dev | margin | live | managed_net_ev | True | True | False | 708 | -20,652 | -93.06 | -0.0252 | -729 | 0.732 | 31,294 |
| c500_dev | margin | live | managed_net_ev | True | True | True | 579 | -4,569 | -69.95 | -0.0048 | 12,049 | 0.765 | 47,726 |
| c500_dev | margin | none | credit_width | False | False | False | 743 | -24,955 | -99.87 | -0.0616 | -542 | 0.518 | 5,593 |
| c500_dev | margin | none | credit_width | False | False | True | 1032 | -20,311 | -90.49 | -0.0231 | 24,663 | 0.54 | 30,740 |
| c500_dev | margin | none | credit_width | False | True | False | 561 | -24,961 | -99.88 | -0.0951 | -8,187 | 0.494 | 204 |
| c500_dev | margin | none | credit_width | False | True | True | 1032 | -20,311 | -90.49 | -0.0231 | 24,663 | 0.54 | 30,740 |
| c500_dev | margin | none | credit_width | True | False | False | 630 | -22,660 | -94.4 | -0.0455 | -833 | 0.662 | 11,573 |
| c500_dev | margin | none | credit_width | True | False | True | 585 | 1,851 | -64.52 | 0.002 | 34,533 | 0.706 | 54,060 |
| c500_dev | margin | none | credit_width | True | True | False | 602 | -23,940 | -97.46 | -0.0496 | -2,600 | 0.661 | 10,074 |
| c500_dev | margin | none | credit_width | True | True | True | 585 | 1,851 | -64.52 | 0.002 | 34,533 | 0.706 | 54,060 |
| c500_dev | margin | none | managed_net_ev | False | False | False | 822 | -24,940 | -99.8 | -0.0476 | -9,214 | 0.478 | 2,703 |
| c500_dev | margin | none | managed_net_ev | False | False | True | 996 | -22,720 | -98.25 | -0.036 | -6,725 | 0.443 | 14,959 |
| c500_dev | margin | none | managed_net_ev | False | True | False | 967 | -24,934 | -99.87 | -0.024 | 2,626 | 0.496 | 16,891 |
| c500_dev | margin | none | managed_net_ev | False | True | True | 996 | -22,720 | -98.25 | -0.036 | -6,725 | 0.443 | 14,959 |
| c500_dev | margin | none | managed_net_ev | True | False | False | 688 | -22,859 | -95.97 | -0.0274 | -3,961 | 0.705 | 31,837 |
| c500_dev | margin | none | managed_net_ev | True | False | True | 571 | -8,234 | -72.93 | -0.0093 | 6,870 | 0.751 | 47,571 |
| c500_dev | margin | none | managed_net_ev | True | True | False | 705 | -21,439 | -93.78 | -0.0251 | -2,222 | 0.709 | 31,837 |
| c500_dev | margin | none | managed_net_ev | True | True | True | 571 | -8,234 | -72.93 | -0.0093 | 6,870 | 0.751 | 47,571 |

