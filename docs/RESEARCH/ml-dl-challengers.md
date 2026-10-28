# E27.6 · Deep-learning challengers vs M2 (D126)

Research only: nothing here is imported by the live scorer or read by the gate.

Run `dl-7a1b3f182df5` · code `33124649b008` · feature schema `45ba7c64e725` · M2 versions {'credit': 'm2-05fa82807a93', 'debit': 'm2-04e58916e118'} · created 2026-10-27T21:13:13.396258-04:00 · holdout reads: 0.

## Decision (`dl_promote`)

| family | model | years_passed | dl_promote | decision |
|---|---|---|---|---|
| credit | ft | 0 | no | negative result |
| credit | mlp | 0 | no | negative result |
| credit | stack | 0 | no | negative result |
| credit | surface | 0 | no | negative result |
| credit | temporal | 0 | no | negative result |
| debit | ft | 0 | no | negative result |
| debit | mlp | 0 | no | negative result |
| debit | stack | 1 | no | negative result |
| debit | surface | 0 | no | negative result |
| debit | temporal | 0 | no | negative result |


## Test-year metrics (All rows)

| family | fold | model | n | win_log_loss | win_auc | win_ece | stop_log_loss | er_ic_mean | er_ic_nw_t | best_dl |
|---|---|---|---|---|---|---|---|---|---|---|
| credit | test2022 | m2 | 150588 | 0.7238 | 0.6258 | 0.0987 | 0.5423 | 0.0696 | 1.4710 | – |
| credit | test2022 | mlp | 150588 | 0.9660 | 0.5719 | 0.2044 | 0.8251 | 0.0089 | 0.1533 | – |
| credit | test2022 | ft | 150588 | 0.9664 | 0.6351 | 0.1326 | 0.6336 | 0.1100 | 3.3490 | – |
| credit | test2022 | temporal | 150588 | 1.0738 | 0.5757 | 0.2384 | 0.8384 | 0.0147 | 0.2563 | – |
| credit | test2022 | surface | 150588 | 0.9947 | 0.5574 | 0.2273 | 0.9618 | -0.0001 | -0.0010 | – |
| credit | test2022 | stack | 150588 | 0.7034 | 0.6106 | 0.0609 | 0.5641 | 0.0891 | 1.9598 | surface |
| credit | test2023 | m2 | 149094 | 0.6741 | 0.6253 | 0.0694 | 0.5726 | 0.1372 | 13.2510 | – |
| credit | test2023 | mlp | 149094 | 0.7122 | 0.6034 | 0.0927 | 0.6392 | 0.1082 | 4.5440 | – |
| credit | test2023 | ft | 149094 | 0.6649 | 0.6320 | 0.0318 | 0.5923 | 0.1468 | 14.0469 | – |
| credit | test2023 | temporal | 149094 | 0.7843 | 0.5487 | 0.1858 | 0.6765 | 0.1106 | 4.4971 | – |
| credit | test2023 | surface | 149094 | 0.7405 | 0.5760 | 0.1266 | 0.6184 | 0.1052 | 4.3147 | – |
| credit | test2023 | stack | 149094 | 0.6742 | 0.6257 | 0.0677 | 0.5616 | 0.1459 | 13.8691 | ft |
| debit | test2022 | m2 | 150601 | 0.7353 | 0.5363 | 0.1204 | 0.6479 | 0.0864 | 1.7727 | – |
| debit | test2022 | mlp | 150601 | 1.0026 | 0.5139 | 0.2704 | 0.9233 | 0.0076 | 0.1242 | – |
| debit | test2022 | ft | 150601 | 0.8611 | 0.5275 | 0.1056 | 0.7309 | 0.0706 | 1.4504 | – |
| debit | test2022 | temporal | 150601 | 0.8924 | 0.5157 | 0.2573 | 1.0377 | 0.0155 | 0.2563 | – |
| debit | test2022 | surface | 150601 | 0.8139 | 0.5199 | 0.2054 | 0.7771 | 0.0178 | 0.2978 | – |
| debit | test2022 | stack | 150601 | 0.7148 | 0.5359 | 0.0587 | 0.6631 | 0.1363 | 2.9273 | mlp |
| debit | test2023 | m2 | 149055 | 0.7148 | 0.5322 | 0.1063 | 0.6890 | 0.1461 | 14.4676 | – |
| debit | test2023 | mlp | 149055 | 0.7992 | 0.5028 | 0.1913 | 0.7742 | 0.1123 | 5.4263 | – |
| debit | test2023 | ft | 149055 | 0.6899 | 0.5346 | 0.0425 | 0.8185 | 0.1571 | 17.2689 | – |
| debit | test2023 | temporal | 149055 | 0.7962 | 0.5031 | 0.2077 | 0.8113 | 0.1092 | 3.5995 | – |
| debit | test2023 | surface | 149055 | 0.7451 | 0.4990 | 0.1495 | 0.7398 | 0.1040 | 4.7561 | – |
| debit | test2023 | stack | 149055 | 0.6873 | 0.5057 | 0.0286 | 0.6765 | 0.0682 | 2.3245 | ft |


### Stocks

| family | fold | model | n | win_log_loss | er_ic_mean | er_ic_nw_t |
|---|---|---|---|---|---|---|
| credit | test2022 | m2 | 123939 | 0.7175 | 0.0658 | 1.4238 |
| credit | test2022 | mlp | 123939 | 0.9498 | 0.0088 | 0.1543 |
| credit | test2022 | ft | 123939 | 0.9551 | 0.1068 | 3.3069 |
| credit | test2022 | temporal | 123939 | 1.0692 | 0.0162 | 0.2882 |
| credit | test2022 | surface | 123939 | 0.9720 | 0.0013 | 0.0227 |
| credit | test2022 | stack | 123939 | 0.6975 | 0.0822 | 1.8057 |
| credit | test2023 | m2 | 122927 | 0.6746 | 0.1309 | 13.2484 |
| credit | test2023 | mlp | 122927 | 0.7129 | 0.0992 | 4.3538 |
| credit | test2023 | ft | 122927 | 0.6647 | 0.1417 | 14.1628 |
| credit | test2023 | temporal | 122927 | 0.7812 | 0.1027 | 4.2658 |
| credit | test2023 | surface | 122927 | 0.7398 | 0.0959 | 4.1166 |
| credit | test2023 | stack | 122927 | 0.6739 | 0.1410 | 13.8814 |
| debit | test2022 | m2 | 123995 | 0.7328 | 0.0819 | 1.7196 |
| debit | test2022 | mlp | 123995 | 0.9940 | 0.0107 | 0.1793 |
| debit | test2022 | ft | 123995 | 0.8608 | 0.0664 | 1.3632 |
| debit | test2022 | temporal | 123995 | 0.8932 | 0.0185 | 0.3165 |
| debit | test2022 | surface | 123995 | 0.8071 | 0.0180 | 0.3064 |
| debit | test2022 | stack | 123995 | 0.7130 | 0.1294 | 2.7481 |
| debit | test2023 | m2 | 122181 | 0.7149 | 0.1409 | 14.1984 |
| debit | test2023 | mlp | 122181 | 0.7958 | 0.1035 | 5.2186 |
| debit | test2023 | ft | 122181 | 0.6904 | 0.1532 | 18.2596 |
| debit | test2023 | temporal | 122181 | 0.7928 | 0.1012 | 3.4600 |
| debit | test2023 | surface | 122181 | 0.7435 | 0.0967 | 4.8018 |
| debit | test2023 | stack | 122181 | 0.6874 | 0.0659 | 2.3210 |


### ETFs

| family | fold | model | n | win_log_loss | er_ic_mean | er_ic_nw_t |
|---|---|---|---|---|---|---|
| credit | test2022 | m2 | 26649 | 0.7522 | 0.0878 | 1.6166 |
| credit | test2022 | mlp | 26649 | 1.0384 | 0.0134 | 0.2113 |
| credit | test2022 | ft | 26649 | 1.0167 | 0.1264 | 3.4448 |
| credit | test2022 | temporal | 26649 | 1.0943 | 0.0103 | 0.1609 |
| credit | test2022 | surface | 26649 | 1.0960 | -0.0036 | -0.0553 |
| credit | test2022 | stack | 26649 | 0.7300 | 0.1231 | 2.6676 |
| credit | test2023 | m2 | 26167 | 0.6721 | 0.1661 | 11.3922 |
| credit | test2023 | mlp | 26167 | 0.7093 | 0.1517 | 4.8728 |
| credit | test2023 | ft | 26167 | 0.6658 | 0.1749 | 11.6283 |
| credit | test2023 | temporal | 26167 | 0.7984 | 0.1499 | 5.2710 |
| credit | test2023 | surface | 26167 | 0.7438 | 0.1498 | 4.7726 |
| credit | test2023 | stack | 26167 | 0.6754 | 0.1735 | 11.5682 |
| debit | test2022 | m2 | 26606 | 0.7463 | 0.1053 | 1.8932 |
| debit | test2022 | mlp | 26606 | 1.0410 | -0.0019 | -0.0268 |
| debit | test2022 | ft | 26606 | 0.8628 | 0.0890 | 1.7917 |
| debit | test2022 | temporal | 26606 | 0.8886 | 0.0052 | 0.0746 |
| debit | test2022 | surface | 26606 | 0.8446 | 0.0230 | 0.3546 |
| debit | test2022 | stack | 26606 | 0.7231 | 0.1712 | 3.7586 |
| debit | test2023 | m2 | 26874 | 0.7146 | 0.1662 | 11.9603 |
| debit | test2023 | mlp | 26874 | 0.8147 | 0.1542 | 5.7398 |
| debit | test2023 | ft | 26874 | 0.6876 | 0.1751 | 11.7527 |
| debit | test2023 | temporal | 26874 | 0.8118 | 0.1472 | 4.0826 |
| debit | test2023 | surface | 26874 | 0.7525 | 0.1416 | 4.4168 |
| debit | test2023 | stack | 26874 | 0.6867 | 0.0792 | 2.2534 |


## Rule legs per fold

| family | model | fold | ll | ll_m2 | ll_managed_pop | er_ic | er_ic_t | er_ic_m2 | beats_m2_ll | beats_m2_ic | net | net_m2 | dd | dd_m2 | net_incumbent | beats_incumbent | beats_m2_value | year_pass |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| credit | mlp | test2022 | 0.966 | 0.724 | 0.762 | 0.009 | 0.153 | 0.070 | no | no | -21534.150 | -21943.310 | -87.750 | -89.530 | -18625.240 | no | yes | no |
| credit | ft | test2022 | 0.966 | 0.724 | 0.762 | 0.110 | 3.349 | 0.070 | no | yes | -22807.010 | -21943.310 | -91.460 | -89.530 | -18625.240 | no | no | no |
| credit | temporal | test2022 | 1.074 | 0.724 | 0.762 | 0.015 | 0.256 | 0.070 | no | no | -19041.780 | -21943.310 | -82.790 | -89.530 | -18625.240 | no | yes | no |
| credit | surface | test2022 | 0.995 | 0.724 | 0.762 | -0.000 | -0.001 | 0.070 | no | no | -21893.290 | -21943.310 | -87.790 | -89.530 | -18625.240 | no | yes | no |
| credit | stack | test2022 | 0.703 | 0.724 | 0.762 | 0.089 | 1.960 | 0.070 | yes | no | -14265.750 | -21943.310 | -61.450 | -89.530 | -18625.240 | yes | yes | no |
| credit | mlp | test2023 | 0.712 | 0.674 | 0.699 | 0.108 | 4.544 | 0.137 | no | no | -16313.380 | -11871.010 | -79.260 | -61.780 | 21879.110 | no | no | no |
| credit | ft | test2023 | 0.665 | 0.674 | 0.699 | 0.147 | 14.047 | 0.137 | yes | yes | -8905.750 | -11871.010 | -69.130 | -61.780 | 21879.110 | no | no | no |
| credit | temporal | test2023 | 0.784 | 0.674 | 0.699 | 0.111 | 4.497 | 0.137 | no | no | -19569.990 | -11871.010 | -80.940 | -61.780 | 21879.110 | no | no | no |
| credit | surface | test2023 | 0.741 | 0.674 | 0.699 | 0.105 | 4.315 | 0.137 | no | no | -19391.160 | -11871.010 | -78.900 | -61.780 | 21879.110 | no | no | no |
| credit | stack | test2023 | 0.674 | 0.674 | 0.699 | 0.146 | 13.869 | 0.137 | no | yes | -1858.480 | -11871.010 | -62.470 | -61.780 | 21879.110 | no | no | no |
| debit | mlp | test2022 | 1.003 | 0.735 | 0.707 | 0.008 | 0.124 | 0.086 | no | no | -21025.880 | -22203.800 | -86.520 | -92.250 | -19803.240 | no | yes | no |
| debit | ft | test2022 | 0.861 | 0.735 | 0.707 | 0.071 | 1.450 | 0.086 | no | no | -20795.700 | -22203.800 | -86.020 | -92.250 | -19803.240 | no | yes | no |
| debit | temporal | test2022 | 0.892 | 0.735 | 0.707 | 0.015 | 0.256 | 0.086 | no | no | -20047.860 | -22203.800 | -80.650 | -92.250 | -19803.240 | no | yes | no |
| debit | surface | test2022 | 0.814 | 0.735 | 0.707 | 0.018 | 0.298 | 0.086 | no | no | -21373.510 | -22203.800 | -89.950 | -92.250 | -19803.240 | no | yes | no |
| debit | stack | test2022 | 0.715 | 0.735 | 0.707 | 0.136 | 2.927 | 0.086 | yes | yes | -21525.180 | -22203.800 | -88.530 | -92.250 | -19803.240 | no | yes | yes |
| debit | mlp | test2023 | 0.799 | 0.715 | 0.699 | 0.112 | 5.426 | 0.146 | no | no | -22539.970 | -12371.240 | -90.600 | -73.250 | -1050.370 | no | no | no |
| debit | ft | test2023 | 0.690 | 0.715 | 0.699 | 0.157 | 17.269 | 0.146 | yes | yes | -17727.510 | -12371.240 | -81.790 | -73.250 | -1050.370 | no | no | no |
| debit | temporal | test2023 | 0.796 | 0.715 | 0.699 | 0.109 | 3.600 | 0.146 | no | no | -21334.560 | -12371.240 | -85.340 | -73.250 | -1050.370 | no | no | no |
| debit | surface | test2023 | 0.745 | 0.715 | 0.699 | 0.104 | 4.756 | 0.146 | no | no | -20165.870 | -12371.240 | -83.970 | -73.250 | -1050.370 | no | no | no |
| debit | stack | test2023 | 0.687 | 0.715 | 0.699 | 0.068 | 2.324 | 0.146 | yes | no | -21306.430 | -12371.240 | -88.680 | -73.250 | -1050.370 | no | no | no |


## Decision-value replays

| family | year | model | use | trades | net_pnl | max_dd_pct |
|---|---|---|---|---|---|---|
| credit | 2022 | incumbent | incumbent | 265 | -18625.24 | -77.83 |
| credit | 2022 | ft | ml_er | 211 | -22807.01 | -91.46 |
| credit | 2022 | ft | ml_pstop | 118 | -12483.80 | -63.51 |
| credit | 2022 | m2 | ml_er | 201 | -21943.31 | -89.53 |
| credit | 2022 | m2 | ml_pstop | 149 | -15542.67 | -74.52 |
| credit | 2022 | mlp | ml_er | 221 | -21534.15 | -87.75 |
| credit | 2022 | mlp | ml_pstop | 140 | -21214.20 | -87.51 |
| credit | 2022 | stack | ml_er | 269 | -14265.75 | -61.45 |
| credit | 2022 | stack | ml_pstop | 189 | -21693.72 | -88.56 |
| credit | 2022 | surface | ml_er | 186 | -21893.29 | -87.79 |
| credit | 2022 | surface | ml_pstop | 151 | -21972.57 | -90.85 |
| credit | 2022 | temporal | ml_er | 236 | -19041.78 | -82.79 |
| credit | 2022 | temporal | ml_pstop | 186 | -20989.09 | -88.84 |
| debit | 2022 | incumbent | incumbent | 249 | -19803.24 | -85.46 |
| debit | 2022 | ft | ml_er | 219 | -20795.70 | -86.02 |
| debit | 2022 | ft | ml_pstop | 124 | -22298.33 | -90.41 |
| debit | 2022 | m2 | ml_er | 215 | -22203.80 | -92.25 |
| debit | 2022 | m2 | ml_pstop | 126 | -20942.46 | -89.32 |
| debit | 2022 | mlp | ml_er | 238 | -21025.88 | -86.52 |
| debit | 2022 | mlp | ml_pstop | 201 | -20224.86 | -86.31 |
| debit | 2022 | stack | ml_er | 221 | -21525.18 | -88.53 |
| debit | 2022 | stack | ml_pstop | 209 | -22579.85 | -90.57 |
| debit | 2022 | surface | ml_er | 229 | -21373.51 | -89.95 |
| debit | 2022 | surface | ml_pstop | 179 | -21794.65 | -90.32 |
| debit | 2022 | temporal | ml_er | 254 | -20047.86 | -80.65 |
| debit | 2022 | temporal | ml_pstop | 193 | -19802.53 | -85.69 |
| credit | 2023 | incumbent | incumbent | 278 | 21879.11 | -60.79 |
| credit | 2023 | ft | ml_er | 227 | -8905.75 | -69.13 |
| credit | 2023 | ft | ml_pstop | 190 | -15229.87 | -72.80 |
| credit | 2023 | m2 | ml_er | 277 | -11871.01 | -61.78 |
| credit | 2023 | m2 | ml_pstop | 205 | -6519.51 | -59.15 |
| credit | 2023 | mlp | ml_er | 227 | -16313.38 | -79.26 |
| credit | 2023 | mlp | ml_pstop | 197 | -21886.61 | -89.17 |
| credit | 2023 | stack | ml_er | 254 | -1858.48 | -62.47 |
| credit | 2023 | stack | ml_pstop | 224 | -2518.77 | -53.69 |
| credit | 2023 | surface | ml_er | 258 | -19391.16 | -78.90 |
| credit | 2023 | surface | ml_pstop | 229 | -17945.47 | -72.21 |
| credit | 2023 | temporal | ml_er | 237 | -19569.99 | -80.94 |
| credit | 2023 | temporal | ml_pstop | 186 | -20869.68 | -87.10 |
| debit | 2023 | incumbent | incumbent | 282 | -1050.37 | -59.89 |
| debit | 2023 | ft | ml_er | 276 | -17727.51 | -81.79 |
| debit | 2023 | ft | ml_pstop | 159 | -19553.61 | -81.67 |
| debit | 2023 | m2 | ml_er | 257 | -12371.24 | -73.25 |
| debit | 2023 | m2 | ml_pstop | 175 | -19211.18 | -81.07 |
| debit | 2023 | mlp | ml_er | 233 | -22539.97 | -90.60 |
| debit | 2023 | mlp | ml_pstop | 210 | -17185.18 | -80.48 |
| debit | 2023 | stack | ml_er | 268 | -21306.43 | -88.68 |
| debit | 2023 | stack | ml_pstop | 177 | -19167.10 | -85.55 |
| debit | 2023 | surface | ml_er | 249 | -20165.87 | -83.97 |
| debit | 2023 | surface | ml_pstop | 183 | -17302.09 | -75.91 |
| debit | 2023 | temporal | ml_er | 212 | -21334.56 | -85.34 |
| debit | 2023 | temporal | ml_pstop | 156 | -18571.38 | -80.75 |


## Seed variance

| family | model | fold | seeds | ll_mean | ll_sd | ic_mean | ic_sd |
|---|---|---|---|---|---|---|---|
| credit | ft | test2022 | 3 | 0.9392 | 0.1261 | 0.1074 | 0.0313 |
| credit | ft | test2023 | 3 | 0.6696 | 0.0033 | 0.1427 | 0.0074 |
| credit | mlp | test2022 | 5 | 0.9192 | 0.1218 | 0.0120 | 0.0173 |
| credit | mlp | test2023 | 5 | 0.7210 | 0.0313 | 0.1006 | 0.0287 |
| credit | surface | test2022 | 3 | 0.9675 | 0.1706 | 0.0011 | 0.0024 |
| credit | surface | test2023 | 3 | 0.7342 | 0.0265 | 0.0952 | 0.0295 |
| credit | temporal | test2022 | 3 | 1.0006 | 0.1286 | 0.0151 | 0.0142 |
| credit | temporal | test2023 | 3 | 0.7370 | 0.0116 | 0.1066 | 0.0034 |
| debit | ft | test2022 | 3 | 0.8865 | 0.2436 | 0.0920 | 0.0730 |
| debit | ft | test2023 | 3 | 0.7144 | 0.0458 | 0.1516 | 0.0048 |
| debit | mlp | test2022 | 5 | 0.9434 | 0.1596 | 0.0061 | 0.0161 |
| debit | mlp | test2023 | 5 | 0.7727 | 0.0256 | 0.1061 | 0.0197 |
| debit | surface | test2022 | 3 | 0.8432 | 0.0815 | 0.0167 | 0.0191 |
| debit | surface | test2023 | 3 | 0.7396 | 0.0168 | 0.0998 | 0.0356 |
| debit | temporal | test2022 | 3 | 0.9256 | 0.0560 | 0.0122 | 0.0211 |
| debit | temporal | test2023 | 3 | 0.7769 | 0.0266 | 0.1024 | 0.0078 |


## Surface → M1 `move_vs_implied_h21`

| fold | arm | split | n | ic_mean | ic_nw_t | mse | surface_coverage |
|---|---|---|---|---|---|---|---|
| test2022 | panel | All | 111552 | 0.0492 | 2.9753 | 0.4658 | 1.0000 |
| test2022 | panel | Stocks | 89258 | 0.0395 | 2.0229 | 0.4852 | 1.0000 |
| test2022 | panel | ETFs | 22294 | 0.1402 | 3.8647 | 0.3883 | 1.0000 |
| test2022 | panel+surface | All | 111552 | 0.0484 | 2.9026 | 0.4370 | 1.0000 |
| test2022 | panel+surface | Stocks | 89258 | 0.0394 | 2.0498 | 0.4533 | 1.0000 |
| test2022 | panel+surface | ETFs | 22294 | 0.1347 | 3.8313 | 0.3717 | 1.0000 |
| test2022 | m1 m1-53833784f276 | All | 111552 | 0.0432 | 2.1928 | 0.4451 | – |
| test2022 | m1 m1-53833784f276 | Stocks | 89258 | 0.0190 | 0.9037 | 0.4706 | – |
| test2022 | m1 m1-53833784f276 | ETFs | 22294 | 0.1854 | 5.0246 | 0.3429 | – |
| test2023 | panel | All | 103702 | 0.1077 | 5.2678 | 0.3608 | 1.0000 |
| test2023 | panel | Stocks | 83068 | 0.0649 | 3.4422 | 0.3732 | 1.0000 |
| test2023 | panel | ETFs | 20634 | 0.2681 | 9.0508 | 0.3106 | 1.0000 |
| test2023 | panel+surface | All | 103702 | 0.0963 | 5.0913 | 0.3687 | 1.0000 |
| test2023 | panel+surface | Stocks | 83068 | 0.0570 | 3.0013 | 0.3824 | 1.0000 |
| test2023 | panel+surface | ETFs | 20634 | 0.2464 | 8.2184 | 0.3137 | 1.0000 |
| test2023 | m1 m1-53833784f276 | All | 103702 | 0.0639 | 4.4990 | 0.5781 | – |
| test2023 | m1 m1-53833784f276 | Stocks | 83068 | 0.0345 | 2.3804 | 0.6138 | – |
| test2023 | m1 m1-53833784f276 | ETFs | 20634 | 0.2002 | 5.9427 | 0.4343 | – |


## Compute

| family | model | fold | seconds | device | epochs | val_ic | aux_coverage |
|---|---|---|---|---|---|---|---|
| credit | mlp | test2022 | 77.300 | cpu | 2,1,3,1,2 | 0.224 | – |
| credit | ft | test2022 | 2138.100 | mps | 7,5,3 | 0.180 | – |
| credit | temporal | test2022 | 53.400 | mps | 1,1,1 | 0.205 | 1.000 |
| credit | surface | test2022 | 45.200 | mps | 2,1,3 | 0.226 | 1.000 |
| credit | mlp | test2023 | 161.500 | cpu | 7,2,1,1,2 | 0.013 | – |
| credit | ft | test2023 | 2017 | mps | 2,2,2 | 0.145 | – |
| credit | temporal | test2023 | 82.900 | mps | 1,1,1 | 0.008 | 1.000 |
| credit | surface | test2023 | 86.700 | mps | 1,6,1 | 0.023 | 1.000 |
| debit | mlp | test2022 | 74.000 | cpu | 1,1,3,2,1 | 0.229 | – |
| debit | ft | test2022 | 1623.100 | mps | 4,2,4 | 0.184 | – |
| debit | temporal | test2022 | 53.500 | mps | 1,1,1 | 0.218 | 1.000 |
| debit | surface | test2022 | 35.500 | mps | 1,1,1 | 0.219 | 1.000 |
| debit | mlp | test2023 | 123.900 | cpu | 1,2,1,1,2 | 0.006 | – |
| debit | ft | test2023 | 1703.200 | mps | 1,1,2 | 0.181 | – |
| debit | temporal | test2023 | 85.100 | mps | 1,1,1 | 0.008 | 1.000 |
| debit | surface | test2023 | 79.600 | mps | 4,2,1 | 0.026 | 1.000 |


Total wall time 6846 s.

## Reading the result

**Verdict:** a negative result for every challenger, against the pre-registered rule. M2 (LightGBM) stays the champion, and no DL model goes to shadow. None of the settings below were tuned after seeing these numbers. A follow-up would need a new `ML-<n>` study (D133), not a re-run with new settings.

- **FT-Transformer** comes closest. In 2023 it beats M2 on both statistical legs for both families:
  - credit: log-loss 0.665 vs 0.674, `e_r` IC 0.147 vs 0.137 (t 14);
  - debit: log-loss 0.690 vs 0.715, `e_r` IC 0.157 vs 0.146.

  It fails 2022 on both. 2022 calibration is also the leg where its 3 seeds disagree most (log-loss sd 0.13–0.24). Its `ml_er` replay lost to M2's in 2022 (credit) and in 2023 (debit). It never beat the incumbent.
- **ResNet-MLP, temporal, surface** never beat M2 on any statistical leg in either year. Their 2022 `e_r` IC is about 0, against M2's 0.07–0.09. They early-stop at 1–3 epochs on the 2021 inner block, which is the regime the 2022 drawdown left behind. That suggests these nets overfit the inner year rather than lack capacity. The 60-session sequence and the IV surface add nothing over the M2 features. They also make 2022 calibration worse, not better.
- **Stack** (M2 + the fold's best DL by inner-validation IC, blended ex ante) is the only challenger that passes any family-year:
  - **debit 2022:** log-loss 0.715 vs 0.735, IC 0.136 vs 0.086 (t 2.9), `ml_er` net and DD both better than M2.
  - **credit:** it gives the best `ml_er` replay in both years (2022 −$14.3k vs M2 −$21.9k; 2023 −$1.9k vs −$11.9k). It still fails the rule: 2022 IC t is 1.96 (needs > 2), and 2023 log-loss is a tie (0.6742 vs 0.6741).
  - **debit 2023:** it degrades (IC 0.068 vs 0.146), so it fails "both years".
- **Replays vs the incumbent:** every ML score, M2 included, loses to the incumbent ranker in credit 2023 (+$21.9k) and debit 2023 (−$1.1k). This matches E27.3/E27.5: the `ml_er` ranking on its own is not yet a better decision rule than `managed_net_ev_full`.
- **Surface → M1:** adding the IV-surface embedding does not improve `move_vs_implied_h21` on IC in either year:
  - 2022: All 0.048 vs 0.049 without it;
  - 2023: 0.096 vs 0.108.

  MSE improves a little in 2022 and is worse in 2023, so the surface is **not** recommended as an M1 feature. Both net arms beat the published LightGBM M1's stock IC, but the M1 keeps the better ETF IC in 2022. That comparison is informative only; it is not part of the rule.
- **Cost:** 1 h 54 min wall time for both families on the host:
  - FT took 27–36 min per fold on MPS; every other net took under 3 min.
  - The replay simulation builds took about 19–20 min per year.
  - The run fits well inside the 6 h budget.

**Caveats:**
- MPS runs (FT, temporal, surface) are not bit-reproducible, so the seed table gives the spread. CPU runs (MLP) are deterministic, and this is tested.
- Only dev years were read (D84); no holdout reads.
- Replays use the E27.3 single-year `ml_er` / `ml_pstop` walk with each fold's own τ_stop.
