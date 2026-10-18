# E7.19 sim alignment: generated tables

Written by `scripts/sim_alignment_report.py`; the narrative is `../sim-alignment.md`.
`old` = E7.18 books (one expiry nearest the window middle); `new` = the D125
sleeve-aligned sim; `new_noproxy` = the same with `intraday_proxy: none`.

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
| c500_dev | new | margin | All | 509 | -14,976 | 10,024 | -59.9 | 80.15 | 0.721 | -0.0271 | 0.0118 | -0.0277 | 13,282 | 14,623 | 8,633 | -14,976 | 11,530 | -0.0271 | 0.721 |
| c500_dev | new | margin | Stocks | 150 |  |  |  |  |  |  |  |  |  |  |  | -11,597 | 2,508 | -0.0614 | 0.707 |
| c500_dev | new | margin | ETFs | 359 |  |  |  |  |  |  |  |  |  |  |  | -3,379 | 9,022 | -0.0093 | 0.727 |
| c500_dev | new | cash_debit | All | 679 | -24,964 | 36.49 | -99.85 | 99.92 | 0.406 | -0.0396 | -0.0165 | -0.1113 | 8,530 | 65 | 41 | -24,964 | 45,756 | -0.0396 | 0.406 |
| c500_dev | new | cash_debit | Stocks | 435 |  |  |  |  |  |  |  |  |  |  |  | -32,866 | 10,753 | -0.0819 | 0.4 |
| c500_dev | new | cash_debit | ETFs | 244 |  |  |  |  |  |  |  |  |  |  |  | 7,902 | 35,003 | 0.0344 | 0.418 |
| c500_dev | new_noproxy | margin | All | 509 | -14,976 | 10,024 | -59.9 | 80.15 | 0.721 | -0.0271 | 0.0118 | -0.0277 | 13,282 | 14,623 | 8,633 | -14,976 | 11,530 | -0.0271 | 0.721 |
| c500_dev | new_noproxy | margin | Stocks | 150 |  |  |  |  |  |  |  |  |  |  |  | -11,597 | 2,508 | -0.0614 | 0.707 |
| c500_dev | new_noproxy | margin | ETFs | 359 |  |  |  |  |  |  |  |  |  |  |  | -3,379 | 9,022 | -0.0093 | 0.727 |
| c500_dev | new_noproxy | cash_debit | All | 772 | -24,959 | 40.68 | -99.84 | 99.95 | 0.449 | -0.0229 | 0.0181 | -0.0785 | 19,942 | 224 | 44 | -24,959 | 81,056 | -0.0229 | 0.449 |
| c500_dev | new_noproxy | cash_debit | Stocks | 515 |  |  |  |  |  |  |  |  |  |  |  | -24,013 | 49,744 | -0.0337 | 0.456 |
| c500_dev | new_noproxy | cash_debit | ETFs | 257 |  |  |  |  |  |  |  |  |  |  |  | -946 | 31,312 | -0.0025 | 0.436 |
| c125_dev | old | margin | All | 435 | -22,895 | 2,105 | -91.58 | 92.79 | 0.685 | -0.0526 | -0.0544 | -0.0808 | 20,243 | 16,242 | 6,059 | -22,895 | -6,980 | -0.0526 | 0.685 |
| c125_dev | old | margin | Stocks | 123 |  |  |  |  |  |  |  |  |  |  |  | -4,307 | 2,857 | -0.0297 | 0.691 |
| c125_dev | old | margin | ETFs | 312 |  |  |  |  |  |  |  |  |  |  |  | -18,588 | -9,837 | -0.064 | 0.683 |
| c125_dev | old | cash_debit | All | 485 | -24,892 | 107.9 | -99.57 | 99.76 | 0.507 | -0.0679 | -0.0177 | -0.0294 | 7,967 | 1,256 | 300 | -24,892 | -480 | -0.0679 | 0.507 |
| c125_dev | old | cash_debit | Stocks | 322 |  |  |  |  |  |  |  |  |  |  |  | -38,771 | -26,218 | -0.1802 | 0.484 |
| c125_dev | old | cash_debit | ETFs | 163 |  |  |  |  |  |  |  |  |  |  |  | 13,879 | 25,738 | 0.0916 | 0.552 |
| c125_dev | new | margin | All | 529 | -16,079 | 8,921 | -64.32 | 81.24 | 0.724 | -0.0259 | -0.0086 | -0.0203 | 18,262 | 22,803 | 9,251 | -16,079 | 6,814 | -0.0259 | 0.724 |
| c125_dev | new | margin | Stocks | 176 |  |  |  |  |  |  |  |  |  |  |  | -2,874 | 7,458 | -0.0124 | 0.773 |
| c125_dev | new | margin | ETFs | 353 |  |  |  |  |  |  |  |  |  |  |  | -13,205 | -644 | -0.0339 | 0.7 |
| c125_dev | new | cash_debit | All | 343 | -24,955 | 45.25 | -99.82 | 99.84 | 0.42 | -0.1355 | -0.1096 | -0.1142 | 2,750 | 72 | 50 | -24,955 | -4,686 | -0.1355 | 0.42 |
| c125_dev | new | cash_debit | Stocks | 225 |  |  |  |  |  |  |  |  |  |  |  | -24,321 | -14,162 | -0.2399 | 0.391 |
| c125_dev | new | cash_debit | ETFs | 118 |  |  |  |  |  |  |  |  |  |  |  | -634 | 9,476 | -0.0077 | 0.475 |
| c125_dev | new_noproxy | margin | All | 529 | -16,079 | 8,921 | -64.32 | 81.24 | 0.724 | -0.0259 | -0.0086 | -0.0203 | 18,262 | 22,803 | 9,251 | -16,079 | 6,814 | -0.0259 | 0.724 |
| c125_dev | new_noproxy | margin | Stocks | 176 |  |  |  |  |  |  |  |  |  |  |  | -2,874 | 7,458 | -0.0124 | 0.773 |
| c125_dev | new_noproxy | margin | ETFs | 353 |  |  |  |  |  |  |  |  |  |  |  | -13,205 | -644 | -0.0339 | 0.7 |
| c125_dev | new_noproxy | cash_debit | All | 567 | -24,942 | 57.7 | -99.77 | 99.87 | 0.466 | -0.0541 | -0.0214 | -0.0648 | 9,891 | 149 | 58 | -24,942 | 26,126 | -0.0541 | 0.466 |
| c125_dev | new_noproxy | cash_debit | Stocks | 397 |  |  |  |  |  |  |  |  |  |  |  | -23,722 | 9,929 | -0.0812 | 0.456 |
| c125_dev | new_noproxy | cash_debit | ETFs | 170 |  |  |  |  |  |  |  |  |  |  |  | -1,221 | 16,197 | -0.0072 | 0.488 |

## reconcile
`reconcile.csv`

| book | variant | profile | sum_pnl | delta_equity | diff | min_equity | min_equity_day | open_at_end |
|---|---|---|---|---|---|---|---|---|
| c500_dev | old | margin | -17,739 | -17,739 | -0 | 6,270 | 2023-04-13 | 0 |
| c500_dev | old | cash_debit | -24,926 | -24,926 | -0 | 73.86 | 2023-12-04 | 0 |
| c500_dev | new | margin | -14,976 | -14,976 | -0 | 6,062 | 2023-04-13 | 0 |
| c500_dev | new | cash_debit | -24,964 | -24,964 | -0 | 36.49 | 2023-08-31 | 0 |
| c500_dev | new_noproxy | margin | -14,976 | -14,976 | -0 | 6,062 | 2023-04-13 | 0 |
| c500_dev | new_noproxy | cash_debit | -24,959 | -24,959 | -0 | 40.68 | 2023-08-31 | 0 |
| c125_dev | old | margin | -22,895 | -22,895 | -0 | 2,011 | 2023-11-16 | 0 |
| c125_dev | old | cash_debit | -24,892 | -24,892 | -0 | 107.9 | 2023-10-02 | 0 |
| c125_dev | new | margin | -16,079 | -16,079 | -0 | 5,749 | 2023-06-15 | 0 |
| c125_dev | new | cash_debit | -24,955 | -24,955 | -0 | 45.25 | 2023-08-31 | 0 |
| c125_dev | new_noproxy | margin | -16,079 | -16,079 | -0 | 5,749 | 2023-06-15 | 0 |
| c125_dev | new_noproxy | cash_debit | -24,942 | -24,942 | -0 | 56.18 | 2023-04-14 | 0 |

## sleeves
`sleeves.csv`

| book | variant | profile | class | sleeve | n | net | per_risk | win | mean_dte_entry | mean_days_held | exit_mix |
|---|---|---|---|---|---|---|---|---|---|---|---|
| c500_dev | new | margin | All | default | 509 | -14,976 | -0.0271 | 0.721 | 37.1 | 21 | take_profit:330 dte_exit:113 stop:57 expiry:9 |
| c500_dev | new | margin | Stocks | default | 150 | -11,597 | -0.0614 | 0.707 | 36.4 | 21.7 | take_profit:92 dte_exit:36 stop:17 expiry:5 |
| c500_dev | new | margin | ETFs | default | 359 | -3,379 | -0.0093 | 0.727 | 37.4 | 20.7 | take_profit:238 dte_exit:77 stop:40 expiry:4 |
| c500_dev | new | cash_debit | All | short | 513 | -53,453 | -0.1196 | 0.359 | 19.9 | 4.4 | stop_intraday_proxy:246 take_profit:185 stop:40 lock_intraday_proxy:34 dte_exit:5 profit_lock:2 expiry:1 |
| c500_dev | new | cash_debit | All | swing | 166 | 28,489 | 0.1548 | 0.554 | 46.4 | 15.8 | take_profit:90 stop:59 lock_intraday_proxy:10 dte_exit:4 profit_lock:3 |
| c500_dev | new | cash_debit | Stocks | short | 361 | -34,163 | -0.1051 | 0.371 | 18.5 | 4.2 | stop_intraday_proxy:163 take_profit:135 stop:37 lock_intraday_proxy:20 dte_exit:5 profit_lock:1 |
| c500_dev | new | cash_debit | Stocks | swing | 74 | 1,297 | 0.017 | 0.541 | 45.4 | 14.8 | take_profit:40 stop:28 lock_intraday_proxy:3 dte_exit:2 profit_lock:1 |
| c500_dev | new | cash_debit | ETFs | short | 152 | -19,290 | -0.1583 | 0.329 | 23.3 | 5 | stop_intraday_proxy:83 take_profit:50 lock_intraday_proxy:14 stop:3 profit_lock:1 expiry:1 |
| c500_dev | new | cash_debit | ETFs | swing | 92 | 27,192 | 0.2519 | 0.565 | 47.2 | 16.6 | take_profit:50 stop:31 lock_intraday_proxy:7 dte_exit:2 profit_lock:2 |
| c500_dev | new_noproxy | margin | All | default | 509 | -14,976 | -0.0271 | 0.721 | 37.1 | 21 | take_profit:330 dte_exit:113 stop:57 expiry:9 |
| c500_dev | new_noproxy | margin | Stocks | default | 150 | -11,597 | -0.0614 | 0.707 | 36.4 | 21.7 | take_profit:92 dte_exit:36 stop:17 expiry:5 |
| c500_dev | new_noproxy | margin | ETFs | default | 359 | -3,379 | -0.0093 | 0.727 | 37.4 | 20.7 | take_profit:238 dte_exit:77 stop:40 expiry:4 |
| c500_dev | new_noproxy | cash_debit | All | short | 570 | -40,246 | -0.0579 | 0.428 | 19.8 | 5.4 | stop:285 take_profit:240 profit_lock:34 dte_exit:8 expiry:3 |
| c500_dev | new_noproxy | cash_debit | All | swing | 202 | 15,287 | 0.0389 | 0.51 | 45.6 | 15.7 | take_profit:96 stop:82 profit_lock:18 dte_exit:6 |
| c500_dev | new_noproxy | cash_debit | Stocks | short | 412 | -22,964 | -0.0435 | 0.444 | 18.7 | 5.3 | stop:200 take_profit:181 profit_lock:20 dte_exit:8 expiry:3 |
| c500_dev | new_noproxy | cash_debit | Stocks | swing | 103 | -1,049 | -0.0057 | 0.505 | 44.5 | 15.5 | take_profit:47 stop:42 profit_lock:9 dte_exit:5 |
| c500_dev | new_noproxy | cash_debit | ETFs | short | 158 | -17,282 | -0.1029 | 0.386 | 22.9 | 5.7 | stop:85 take_profit:59 profit_lock:14 |
| c500_dev | new_noproxy | cash_debit | ETFs | swing | 99 | 16,336 | 0.0787 | 0.515 | 46.8 | 16 | take_profit:49 stop:40 profit_lock:9 dte_exit:1 |
| c125_dev | new | margin | All | default | 529 | -16,079 | -0.0259 | 0.724 | 37.2 | 21.1 | take_profit:337 dte_exit:117 stop:65 expiry:10 |
| c125_dev | new | margin | Stocks | default | 176 | -2,874 | -0.0124 | 0.773 | 36.9 | 21.3 | take_profit:117 dte_exit:34 stop:19 expiry:6 |
| c125_dev | new | margin | ETFs | default | 353 | -13,205 | -0.0339 | 0.7 | 37.4 | 21 | take_profit:220 dte_exit:83 stop:46 expiry:4 |
| c125_dev | new | cash_debit | All | short | 252 | -23,951 | -0.1993 | 0.377 | 19.1 | 4.2 | stop_intraday_proxy:128 take_profit:95 stop:20 lock_intraday_proxy:6 profit_lock:2 expiry:1 |
| c125_dev | new | cash_debit | All | swing | 91 | -1,004 | -0.0157 | 0.538 | 46.3 | 13.5 | take_profit:50 stop:33 lock_intraday_proxy:6 dte_exit:2 |
| c125_dev | new | cash_debit | Stocks | short | 181 | -18,069 | -0.2272 | 0.37 | 17.9 | 3.9 | stop_intraday_proxy:93 take_profit:68 stop:18 lock_intraday_proxy:2 |
| c125_dev | new | cash_debit | Stocks | swing | 44 | -6,252 | -0.2862 | 0.477 | 44.4 | 14 | take_profit:22 stop:19 dte_exit:2 lock_intraday_proxy:1 |
| c125_dev | new | cash_debit | ETFs | short | 71 | -5,882 | -0.1448 | 0.394 | 22.3 | 4.9 | stop_intraday_proxy:35 take_profit:27 lock_intraday_proxy:4 stop:2 profit_lock:2 expiry:1 |
| c125_dev | new | cash_debit | ETFs | swing | 47 | 5,248 | 0.1244 | 0.596 | 48 | 13.1 | take_profit:28 stop:14 lock_intraday_proxy:5 |
| c125_dev | new_noproxy | margin | All | default | 529 | -16,079 | -0.0259 | 0.724 | 37.2 | 21.1 | take_profit:337 dte_exit:117 stop:65 expiry:10 |
| c125_dev | new_noproxy | margin | Stocks | default | 176 | -2,874 | -0.0124 | 0.773 | 36.9 | 21.3 | take_profit:117 dte_exit:34 stop:19 expiry:6 |
| c125_dev | new_noproxy | margin | ETFs | default | 353 | -13,205 | -0.0339 | 0.7 | 37.4 | 21 | take_profit:220 dte_exit:83 stop:46 expiry:4 |
| c125_dev | new_noproxy | cash_debit | All | short | 402 | -30,001 | -0.1076 | 0.44 | 19.9 | 5.6 | stop:206 take_profit:176 profit_lock:17 expiry:2 dte_exit:1 |
| c125_dev | new_noproxy | cash_debit | All | swing | 165 | 5,059 | 0.0277 | 0.527 | 45.8 | 14.2 | take_profit:85 stop:57 profit_lock:18 dte_exit:5 |
| c125_dev | new_noproxy | cash_debit | Stocks | short | 309 | -19,045 | -0.0934 | 0.437 | 19 | 5.5 | stop:159 take_profit:134 profit_lock:14 dte_exit:1 expiry:1 |
| c125_dev | new_noproxy | cash_debit | Stocks | swing | 88 | -4,677 | -0.0529 | 0.523 | 44.3 | 13.5 | take_profit:43 stop:31 profit_lock:10 dte_exit:4 |
| c125_dev | new_noproxy | cash_debit | ETFs | short | 93 | -10,957 | -0.1461 | 0.452 | 22.9 | 6.1 | stop:47 take_profit:42 profit_lock:3 expiry:1 |
| c125_dev | new_noproxy | cash_debit | ETFs | swing | 77 | 9,736 | 0.1033 | 0.532 | 47.6 | 15 | take_profit:42 stop:26 profit_lock:8 dte_exit:1 |

## dte_buckets
`dte_buckets.csv`

| book | variant | profile | dte_bucket | n | net | per_risk |
|---|---|---|---|---|---|---|
| c500_dev | old | margin | [21, 36) | 82 | 1,278 | 0.0173 |
| c500_dev | old | margin | [36, 46) | 398 | -19,017 | -0.0476 |
| c500_dev | old | cash_debit | [21, 36) | 279 | -38,502 | -0.1464 |
| c500_dev | old | cash_debit | [36, 46) | 395 | 13,576 | 0.0364 |
| c500_dev | new | margin | [21, 36) | 202 | -3,130 | -0.0154 |
| c500_dev | new | margin | [36, 46) | 307 | -11,846 | -0.0339 |
| c500_dev | new | cash_debit | [10, 21) | 248 | -30,606 | -0.1385 |
| c500_dev | new | cash_debit | [21, 36) | 265 | -22,846 | -0.1012 |
| c500_dev | new | cash_debit | [36, 46) | 91 | 6,834 | 0.0697 |
| c500_dev | new | cash_debit | [46, 61) | 75 | 21,655 | 0.2518 |
| c500_dev | new_noproxy | margin | [21, 36) | 202 | -3,130 | -0.0154 |
| c500_dev | new_noproxy | margin | [36, 46) | 307 | -11,846 | -0.0339 |
| c500_dev | new_noproxy | cash_debit | [10, 21) | 291 | -11,666 | -0.0309 |
| c500_dev | new_noproxy | cash_debit | [21, 36) | 279 | -28,580 | -0.0898 |
| c500_dev | new_noproxy | cash_debit | [36, 46) | 119 | 21,869 | 0.1002 |
| c500_dev | new_noproxy | cash_debit | [46, 61) | 83 | -6,583 | -0.0377 |
| c125_dev | old | margin | [21, 36) | 70 | -3,854 | -0.0574 |
| c125_dev | old | margin | [36, 46) | 365 | -19,040 | -0.0517 |
| c125_dev | old | cash_debit | [21, 36) | 217 | -16,881 | -0.1001 |
| c125_dev | old | cash_debit | [36, 46) | 268 | -8,011 | -0.0404 |
| c125_dev | new | margin | [21, 36) | 200 | 3,891 | 0.0193 |
| c125_dev | new | margin | [36, 46) | 329 | -19,971 | -0.0477 |
| c125_dev | new | cash_debit | [10, 21) | 135 | -22,904 | -0.3466 |
| c125_dev | new | cash_debit | [21, 36) | 117 | -1,047 | -0.0194 |
| c125_dev | new | cash_debit | [36, 46) | 52 | 1,050 | 0.0424 |
| c125_dev | new | cash_debit | [46, 61) | 39 | -2,054 | -0.0523 |
| c125_dev | new_noproxy | margin | [21, 36) | 200 | 3,891 | 0.0193 |
| c125_dev | new_noproxy | margin | [36, 46) | 329 | -19,971 | -0.0477 |
| c125_dev | new_noproxy | cash_debit | [10, 21) | 202 | -27,083 | -0.1775 |
| c125_dev | new_noproxy | cash_debit | [21, 36) | 200 | -2,919 | -0.0231 |
| c125_dev | new_noproxy | cash_debit | [36, 46) | 97 | -9,394 | -0.1024 |
| c125_dev | new_noproxy | cash_debit | [46, 61) | 68 | 14,454 | 0.1592 |

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
| c500_dev | new | margin | dte_exit | 113 | 0.222 | -27,471 |
| c500_dev | new | margin | expiry | 9 | 0.018 | 2,589 |
| c500_dev | new | margin | stop | 57 | 0.112 | -63,862 |
| c500_dev | new | margin | take_profit | 330 | 0.648 | 73,768 |
| c500_dev | new | cash_debit | dte_exit | 9 | 0.013 | -1,299 |
| c500_dev | new | cash_debit | expiry | 1 | 0.001 | 585 |
| c500_dev | new | cash_debit | lock_intraday_proxy | 44 | 0.065 | -7,609 |
| c500_dev | new | cash_debit | profit_lock | 5 | 0.007 | -663 |
| c500_dev | new | cash_debit | stop | 99 | 0.146 | -71,775 |
| c500_dev | new | cash_debit | stop_intraday_proxy | 246 | 0.362 | -125,595 |
| c500_dev | new | cash_debit | take_profit | 275 | 0.405 | 181,393 |
| c500_dev | new_noproxy | margin | dte_exit | 113 | 0.222 | -27,471 |
| c500_dev | new_noproxy | margin | expiry | 9 | 0.018 | 2,589 |
| c500_dev | new_noproxy | margin | stop | 57 | 0.112 | -63,862 |
| c500_dev | new_noproxy | margin | take_profit | 330 | 0.648 | 73,768 |
| c500_dev | new_noproxy | cash_debit | dte_exit | 14 | 0.018 | -5,053 |
| c500_dev | new_noproxy | cash_debit | expiry | 3 | 0.004 | -5,858 |
| c500_dev | new_noproxy | cash_debit | profit_lock | 52 | 0.067 | -6,865 |
| c500_dev | new_noproxy | cash_debit | stop | 367 | 0.475 | -350,366 |
| c500_dev | new_noproxy | cash_debit | take_profit | 336 | 0.435 | 343,182 |
| c125_dev | old | margin | dte_exit | 73 | 0.168 | -18,713 |
| c125_dev | old | margin | expiry | 7 | 0.016 | -1,835 |
| c125_dev | old | margin | stop | 77 | 0.177 | -59,464 |
| c125_dev | old | margin | take_profit | 278 | 0.639 | 57,117 |
| c125_dev | old | cash_debit | dte_exit | 20 | 0.041 | -480 |
| c125_dev | old | cash_debit | profit_lock | 22 | 0.045 | -1,520 |
| c125_dev | old | cash_debit | stop | 209 | 0.431 | -149,813 |
| c125_dev | old | cash_debit | take_profit | 234 | 0.482 | 126,921 |
| c125_dev | new | margin | dte_exit | 117 | 0.221 | -28,228 |
| c125_dev | new | margin | expiry | 10 | 0.019 | -30 |
| c125_dev | new | margin | stop | 65 | 0.123 | -69,379 |
| c125_dev | new | margin | take_profit | 337 | 0.637 | 81,558 |
| c125_dev | new | cash_debit | dte_exit | 2 | 0.006 | 82 |
| c125_dev | new | cash_debit | expiry | 1 | 0.003 | 901 |
| c125_dev | new | cash_debit | lock_intraday_proxy | 12 | 0.035 | -1,005 |
| c125_dev | new | cash_debit | profit_lock | 2 | 0.006 | -78 |
| c125_dev | new | cash_debit | stop | 53 | 0.155 | -29,648 |
| c125_dev | new | cash_debit | stop_intraday_proxy | 128 | 0.373 | -40,842 |
| c125_dev | new | cash_debit | take_profit | 145 | 0.423 | 45,635 |
| c125_dev | new_noproxy | margin | dte_exit | 117 | 0.221 | -28,228 |
| c125_dev | new_noproxy | margin | expiry | 10 | 0.019 | -30 |
| c125_dev | new_noproxy | margin | stop | 65 | 0.123 | -69,379 |
| c125_dev | new_noproxy | margin | take_profit | 337 | 0.637 | 81,558 |
| c125_dev | new_noproxy | cash_debit | dte_exit | 6 | 0.011 | -1,664 |
| c125_dev | new_noproxy | cash_debit | expiry | 2 | 0.004 | 1,571 |
| c125_dev | new_noproxy | cash_debit | profit_lock | 35 | 0.062 | -4,573 |
| c125_dev | new_noproxy | cash_debit | stop | 263 | 0.464 | -150,413 |
| c125_dev | new_noproxy | cash_debit | take_profit | 261 | 0.46 | 130,137 |

## skips
`skips.csv`

| book | variant | profile | sleeve_full | gate_dte_window | gate_max_open_positions | gate_daily_loss |
|---|---|---|---|---|---|---|
| c500_dev | old | margin | 0 | 0 | 3561 | 5688 |
| c500_dev | old | cash_debit | 0 | 0 | 4877 | 14416 |
| c500_dev | new | margin | 0 | 0 | 5695 | 6585 |
| c500_dev | new | cash_debit | 1149 | 0 | 6550 | 9181 |
| c500_dev | new_noproxy | margin | 0 | 0 | 5695 | 6585 |
| c500_dev | new_noproxy | cash_debit | 1735 | 0 | 8484 | 11648 |
| c125_dev | old | margin | 0 | 0 | 2810 | 6987 |
| c125_dev | old | cash_debit | 0 | 0 | 2743 | 11385 |
| c125_dev | new | margin | 0 | 0 | 6797 | 6730 |
| c125_dev | new | cash_debit | 1100 | 0 | 2289 | 5462 |
| c125_dev | new_noproxy | margin | 0 | 0 | 6797 | 6730 |
| c125_dev | new_noproxy | cash_debit | 1189 | 0 | 5024 | 9552 |

## runtime
`runtime.csv`

| book | variant | git | build_s | walk_s | total_s | peak_rss_mb | cache_hit_rate |
|---|---|---|---|---|---|---|---|
| c500_dev | old | ae3cf19 | 1,294 | 8.4 | 1,302 | 1,945 | 1 |
| c500_dev | new | e1aa20e | 18,525 | 12.7 | 18,538 | 3,810 | 0.2776 |
| c500_dev | new_noproxy | e1aa20e | 1,858 | 12.6 | 1,870 | 3,818 | 1 |
| c125_dev | old | ae3cf19 | 534.7 | 7.1 | 541.8 | 1,945 | 1 |
| c125_dev | new | f5f1da0 | 16,823 | 4.5 | 16,828 | 2,409 | 0.04188 |
| c125_dev | new_noproxy | f5f1da0 | 700.1 | 4.3 | 704.4 | 1,884 | 1 |

