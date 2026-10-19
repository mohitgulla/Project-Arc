# Live <-> sim reconciliation (E7.22, D125)

- generated 2026-10-19T04:48:14.672997-04:00 · git 5f58fb9940e3ef2dd419012202c14dcf6f2d8a85 · stores XP-1:control, XP-1:t1, XP-1:t2, XP-1:t3, control · since store start
- sim slippage assumption x = 0.25 (`backtest.sim.slippage_x`); report only: the owner flips `backtest.slippage_by_kind`
- **paper_bias:** Paper fills are friendlier than live: Alpaca's paper engine fills a marketable or at-mid limit without queue position or partial fills, so the measured x is a lower bound on live x.
- every estimate prints n and its 90% bootstrap CI (seed 0); `insufficient` = n < 2 or CI half-width > 0.25

## 1. Slippage (x = (fill - mid) / spread, pay convention; + = cost)

NBBO sources: opens `market_contexts.payload.legs[].bid/ask` (submit time), re-gated fills `decisions(order:band_moved).payload.mid/far_touch`, closes `proposals.structure_json.legs[].premium` + the D24 band `executions.band_hi - band_lo` (upper bound on x). `x_eod` = the same fill vs that session's ThetaData closing NBBO (what the sim would need).

### All (n=38)

| kind | x (submit NBBO) | x_eod (EOD close) |
|---|---|---|
| long_call | 0.173 [0.041, 0.304] (±0.132) n=6 | 0.574 [-1.778, 3.633] (±2.706) n=5 insufficient |
| long_put | 0.226 [0.159, 0.286] (±0.064) n=9 | 2.348 [0.739, 4.006] (±1.633) n=7 insufficient |
| vertical_debit | 0.129 [0.009, 0.239] (±0.115) n=23 | 1.460 [0.803, 2.134] (±0.666) n=20 insufficient |

### Stocks (n=38)

| kind | x (submit NBBO) | x_eod (EOD close) |
|---|---|---|
| long_call | 0.173 [0.041, 0.304] (±0.132) n=6 | 0.574 [-1.778, 3.633] (±2.706) n=5 insufficient |
| long_put | 0.226 [0.159, 0.286] (±0.064) n=9 | 2.348 [0.739, 4.006] (±1.633) n=7 insufficient |
| vertical_debit | 0.129 [0.009, 0.239] (±0.115) n=23 | 1.460 [0.803, 2.134] (±0.666) n=20 insufficient |

### ETFs (n=0)

_none_

By side: close 0.158 [0.072, 0.244] (±0.086) n=12; open 0.160 [0.052, 0.256] (±0.102) n=26

By sleeve: short 0.267 [0.199, 0.330] (±0.065) n=23; swing -0.006 [-0.150, 0.118] (±0.134) n=15

By tod: late 0.338 [0.282, 0.394] (±0.056) n=3; midday 0.089 [-0.022, 0.187] (±0.105) n=24; open 0.263 [0.173, 0.354] (±0.091) n=11

By step: 0 -0.060 [-0.195, 0.054] (±0.125) n=14; 1 0.107 [-0.032, 0.247] (±0.139) n=6; 2 0.259 [0.187, 0.324] (±0.069) n=7; 3 0.292 [0.250, 0.333] (±0.042) n=4; 4 0.466 [0.446, 0.486] (±0.020) n=7

By period: pre_switch 0.159 [0.084, 0.232] (±0.074) n=38

By arm: XP-1:control 0.043 [-0.127, 0.213] (±0.170) n=4; XP-1:t1 0.217 [0.036, 0.398] (±0.181) n=5; XP-1:t2 0.115 [-0.195, 0.362] (±0.279) n=7 insufficient; XP-1:t3 0.102 [-0.101, 0.340] (±0.220) n=5; control 0.204 [0.141, 0.268] (±0.064) n=17

By nbbo_source: band 0.173 [0.086, 0.272] (±0.093) n=11; market_contexts 0.190 [0.081, 0.285] (±0.102) n=24; regate -0.138 [-0.222, -0.053] (±0.085) n=3

Proposed `backtest.slippage_by_kind` (from x_eod, clamped at 0):

```yaml
slippage_by_kind:
  long_call: insufficient
  long_put: insufficient
  vertical_debit: insufficient
```

Per fill:

| arm | day/side | ticker | kind | sleeve | tod | step | fill | x | x_eod |
|---|---|---|---|---|---|---|---|---|---|
| control | open | HOOD | long_put | short | midday | 0 | 5.0 | 0.167 | 0.000 |
| control | open | NVDA | long_call | short | midday | 0 | 5.35 | 0.000 | -3.167 |
| control | open | IREN | vertical_debit | short | midday | 2 | 0.96 | 0.318 | 0.409 |
| control | close | HOOD | long_put | short | open | 0 | -4.6 | 0.075 | -2.167 |
| control | close | NVDA | long_call | short | open | 0 | -5.75 | 0.450 | 8.167 |
| control | close | IREN | vertical_debit | short | open | 3 | -0.44 | 0.167 | -0.714 |
| control | open | AAPL | vertical_debit | short | open | 3 | 3.39 | 0.333 | -0.512 |
| control | open | INTC | vertical_debit | swing | midday | 0 | 5.61 | -0.034 | 2.889 |
| control | close | INTC | vertical_debit | swing | open | 0 | -3.75 | 0.000 | 2.342 |
| control | open | TSM | long_call | swing | midday | 2 | 12.3 | 0.365 | -0.794 |
| control | open | RKLB | long_put | short | midday | 1 | 3.05 | 0.312 | 4.909 |
| control | close | TSM | long_call | swing | open | 0 | -10.65 | 0.010 | no EOD two-sided quote that session |
| control | close | RKLB | long_put | short | open | 2 | -2.28 | 0.227 | no EOD two-sided quote that session |
| control | open | META | vertical_debit | short | open | 2 | 21.05 | 0.205 | no EOD two-sided quote that session |
| control | open | ASTS | long_put | short | midday | 3 | 2.98 | 0.333 | no EOD two-sided quote that session |
| control | close | AAPL | vertical_debit | short | midday | 2 | -3.14 | 0.047 | no EOD two-sided quote that session |
| control | open | COIN | vertical_debit | short | midday | 4 | 11.9 | 0.500 | no EOD two-sided quote that session |
| XP-1:control | open | INTC | vertical_debit | swing | midday | 0 | 5.62 | 0.002 | 2.944 |
| XP-1:control | open | AAPL | vertical_debit | short | midday | 1 | 6.25 | -0.254 | -0.500 |
| XP-1:control | open | RKLB | long_put | short | midday | 0 | 3.0 | 0.000 | 4.682 |
| XP-1:control | open | XOM | vertical_debit | swing | late | 4 | 3.69 | 0.424 | 0.144 |
| XP-1:t1 | open | AAPL | vertical_debit | short | open | 4 | 3.45 | 0.476 | -0.441 |
| XP-1:t1 | open | INTC | vertical_debit | swing | midday | 0 | 8.11 | -0.159 | 4.962 |
| XP-1:t1 | close | INTC | vertical_debit | swing | midday | 1 | -5.52 | 0.043 | 4.079 |
| XP-1:t1 | close | AAPL | vertical_debit | short | midday | 4 | -3.65 | 0.411 | 0.643 |
| XP-1:t1 | open | RKLB | long_put | short | midday | 1 | 3.05 | 0.312 | 4.909 |
| XP-1:t2 | open | AAPL | vertical_debit | short | open | 4 | 3.45 | 0.476 | -0.441 |
| XP-1:t2 | open | INTC | vertical_debit | swing | midday | 0 | 5.81 | -0.929 | 4.000 |
| XP-1:t2 | close | INTC | vertical_debit | swing | midday | 0 | -3.67 | -0.045 | 2.763 |
| XP-1:t2 | close | AAPL | vertical_debit | short | midday | 4 | -3.6 | 0.500 | 0.714 |
| XP-1:t2 | open | RKLB | long_put | short | midday | 1 | 3.6 | 0.214 | 2.014 |
| XP-1:t2 | open | BA | long_call | swing | late | 2 | 7.45 | 0.256 | -0.455 |
| XP-1:t2 | open | XOM | vertical_debit | swing | late | 3 | 6.44 | 0.333 | -0.131 |
| XP-1:t3 | open | AAPL | vertical_debit | short | open | 4 | 3.45 | 0.476 | -0.441 |
| XP-1:t3 | open | INTC | vertical_debit | swing | midday | 0 | 3.7 | -0.333 | 3.346 |
| XP-1:t3 | close | INTC | vertical_debit | swing | midday | 1 | -2.23 | 0.017 | 3.143 |
| XP-1:t3 | open | TSM | long_call | swing | midday | 0 | 12.15 | -0.041 | -0.882 |
| XP-1:t3 | open | RKLB | long_put | short | midday | 2 | 3.65 | 0.393 | 2.086 |

## 2. Candidate overlap (live pick vs the E7.19 sim menu on the EOD book)

- All: n=49 (skipped 13): exact 0.122, same expiry or exact 0.531, kind present 1.000, sim top = live kind 0.041, sim top = live pick 0.000
- Stocks: n=43 (skipped 13): exact 0.140, same expiry or exact 0.605, kind present 1.000, sim top = live kind 0.047, sim top = live pick 0.000
- ETFs: n=6 (skipped 0): exact 0.000, same expiry or exact 0.000, kind present 1.000, sim top = live kind 0.000, sim top = live pick 0.000
- pre_switch: n=49, exact 0.122
- post_switch: n=0, exact n/a

| arm | day | ticker | profile | measure | live | match | Δexp d | Δstrike/spot | rank debit_width | rank ev_proxy | rank managed_net_ev | rank managed_net_ev_tilted | rank rorc_day | rank rorc_day_tilted | rank rorc_day_vrp | sim top kind | live passes filters | live managed EV | menu |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| control | 2026-10-12 | AMD | cash_debit | control | bull_call | same_expiry | 0 | 0.084 |  |  |  |  |  |  |  | bull_call | False | -32.11 | 70 |
| control | 2026-10-12 | AMZN | cash_debit | control | long_call | exact | 0 | 0.000 | 3 | 3 | 2 | 2 | 1 | 1 |  |  | True | 15.96 | 64 |
| control | 2026-10-12 | ASTS | cash_debit | control | bull_call | nearest | 7 | 0.253 |  |  |  |  |  |  |  |  | False | -73.73 | 40 |
| control | 2026-10-12 | BA | cash_debit | control | long_call | same_expiry | 0 | 0.013 |  |  |  |  |  |  |  |  | False | -4.02 | 43 |
| control | 2026-10-12 | CMG | cash_debit | control | long_put | same_expiry | 0 | 0.016 |  |  |  |  |  |  |  | long_put | False | -11.15 | 31 |
| control | 2026-10-12 | DELL | cash_debit | control | long_call | same_expiry | 0 | 0.004 | 1 | 1 | 2 | 2 | 2 | 2 |  |  | True | 0.44 | 59 |
| control | 2026-10-12 | HOOD | cash_debit | control | long_put | exact | 0 | 0.000 |  |  |  |  |  |  |  |  | False | -3.01 | 58 |
| control | 2026-10-12 | IREN | cash_debit | control | bull_call | nearest | 7 | 0.058 |  |  |  |  |  |  |  |  | False | -15.00 | 67 |
| control | 2026-10-12 | MSFT | cash_debit | control | bear_put | same_expiry | 0 | 0.046 |  |  |  |  |  |  |  |  | False | -43.03 | 69 |
| control | 2026-10-12 | MU | cash_debit | control | bull_call | same_expiry | 0 | 0.020 |  |  |  |  |  |  |  |  | False | -217.13 | 70 |
| control | 2026-10-12 | NVDA | cash_debit | control | long_call | same_expiry | 0 | 0.022 |  |  |  |  |  |  |  |  | False | -21.31 | 61 |
| control | 2026-10-12 | TSLA | cash_debit | control | bear_put | same_expiry | 0 | 0.040 |  |  |  |  |  |  |  |  | False | -15.59 | 70 |
| control | 2026-10-13 | JNJ | cash_debit | control | bear_put | nearest | 7 | 0.020 |  |  |  |  |  |  |  |  | False | -46.71 | 44 |
| control | 2026-10-13 | MU | cash_debit | control | bear_put | same_expiry | 0 | 0.032 |  |  |  |  |  |  |  |  | False | -23.51 | 70 |
| control | 2026-10-14 | AAPL | cash_debit | control | bear_put | nearest | 4 | 0.007 |  |  |  |  |  |  |  |  | False | -54.90 | 60 |
| control | 2026-10-14 | INTC | cash_debit | control | bull_call | nearest | 7 | 0.183 |  |  |  |  |  |  |  |  | False | -15.37 | 54 |
| control | 2026-10-14 | UNH | cash_debit | control | bear_put | same_expiry | 0 | 0.051 |  |  |  |  |  |  |  |  | False | -66.40 | 61 |
| control | 2026-10-15 | RKLB | cash_debit | control | long_put | same_expiry | 0 | 0.018 |  |  |  |  |  |  |  |  | False | -20.19 | 43 |
| control | 2026-10-15 | TLT | cash_debit | control | bear_put | nearest | 4 | 0.006 |  |  |  |  |  |  |  |  | False | -2.27 | 71 |
| control | 2026-10-15 | TLT | cash_debit | control | bear_put | nearest | 4 | 0.006 |  |  |  |  |  |  |  |  | False | -2.27 | 71 |
| control | 2026-10-15 | TSLA | cash_debit | control | bull_call | nearest | 4 | 0.055 |  |  |  |  |  |  |  |  | False | -32.54 | 70 |
| control | 2026-10-15 | TSM | cash_debit | control | long_call | same_expiry | 0 | 0.024 |  |  |  |  |  |  |  |  | False | -44.22 | 67 |
| control | 2026-10-16 | ASTS | cash_debit | control | long_put | skip |  | n/a |  |  |  |  |  |  |  |  |  | n/a | 0 |
| control | 2026-10-16 | COIN | cash_debit | control | bull_call | skip |  | n/a |  |  |  |  |  |  |  |  |  | n/a | 0 |
| control | 2026-10-16 | META | cash_debit | control | bull_call | skip |  | n/a |  |  |  |  |  |  |  |  |  | n/a | 0 |
| control | 2026-10-16 | MU | cash_debit | control | bull_call | skip |  | n/a |  |  |  |  |  |  |  |  |  | n/a | 0 |
| XP-1:control | 2026-10-14 | AAPL | cash_debit | control | bear_put | nearest | 4 | 0.007 |  |  |  |  |  |  |  |  | False | -54.90 | 60 |
| XP-1:control | 2026-10-14 | AAPL | cash_debit | control | bear_put | nearest | 4 | 0.022 |  |  |  |  |  |  |  |  | False | -54.90 | 60 |
| XP-1:control | 2026-10-14 | INTC | cash_debit | control | bull_call | nearest | 7 | 0.183 |  |  |  |  |  |  |  |  | False | -15.37 | 54 |
| XP-1:control | 2026-10-15 | RKLB | cash_debit | control | long_put | same_expiry | 0 | 0.018 |  |  |  |  |  |  |  |  | False | -20.19 | 43 |
| XP-1:control | 2026-10-15 | TLT | cash_debit | control | bear_put | nearest | 4 | 0.006 |  |  |  |  |  |  |  |  | False | -2.27 | 71 |
| XP-1:control | 2026-10-15 | TSLA | cash_debit | control | bull_call | nearest | 4 | 0.055 |  |  |  |  |  |  |  |  | False | -32.54 | 70 |
| XP-1:control | 2026-10-15 | XOM | cash_debit | control | bull_call | same_expiry | 0 | 0.029 |  |  |  |  |  |  |  |  | False | -21.83 | 43 |
| XP-1:control | 2026-10-16 | COIN | cash_debit | control | bull_call | skip |  | n/a |  |  |  |  |  |  |  |  |  | n/a | 0 |
| XP-1:control | 2026-10-16 | NVDA | cash_debit | control | long_call | skip |  | n/a |  |  |  |  |  |  |  |  |  | n/a | 0 |
| XP-1:t1 | 2026-10-14 | AAPL | cash_debit | control | bear_put | nearest | 4 | 0.007 |  |  |  |  |  |  |  |  | False | -54.90 | 60 |
| XP-1:t1 | 2026-10-14 | INTC | cash_debit | control | bull_call | nearest | 7 | 0.234 |  |  |  |  |  |  |  |  | False | -15.37 | 54 |
| XP-1:t1 | 2026-10-15 | RKLB | cash_debit | control | long_put | same_expiry | 0 | 0.072 |  |  |  |  |  |  |  |  | False | -20.19 | 43 |
| XP-1:t1 | 2026-10-15 | RKLB | cash_debit | control | long_put | same_expiry | 0 | 0.018 |  |  |  |  |  |  |  |  | False | -20.19 | 43 |
| XP-1:t1 | 2026-10-15 | TLT | cash_debit | control | bear_put | nearest | 4 | 0.006 |  |  |  |  |  |  |  |  | False | -2.27 | 71 |
| XP-1:t1 | 2026-10-15 | TSLA | cash_debit | control | bull_call | same_expiry | 0 | 0.069 |  |  |  |  |  |  |  |  | False | -22.15 | 70 |
| XP-1:t1 | 2026-10-15 | TSM | cash_debit | control | long_call | same_expiry | 0 | 0.024 |  |  |  |  |  |  |  |  | False | -44.22 | 67 |
| XP-1:t1 | 2026-10-16 | COIN | cash_debit | control | bull_call | skip |  | n/a |  |  |  |  |  |  |  |  |  | n/a | 0 |
| XP-1:t1 | 2026-10-16 | MU | cash_debit | control | bull_call | skip |  | n/a |  |  |  |  |  |  |  |  |  | n/a | 0 |
| XP-1:t2 | 2026-10-14 | AAPL | cash_debit | control | bear_put | nearest | 4 | 0.007 |  |  |  |  |  |  |  |  | False | -54.90 | 60 |
| XP-1:t2 | 2026-10-14 | INTC | cash_debit | control | bull_call | nearest | 7 | 0.183 |  |  |  |  |  |  |  |  | False | -15.37 | 54 |
| XP-1:t2 | 2026-10-15 | BA | cash_debit | control | long_call | exact | 0 | 0.000 |  |  |  |  |  |  |  |  | False | -50.07 | 42 |
| XP-1:t2 | 2026-10-15 | RKLB | cash_debit | control | long_put | exact | 0 | 0.000 |  |  |  |  |  |  |  |  | False | -20.19 | 43 |
| XP-1:t2 | 2026-10-15 | TLT | cash_debit | control | bear_put | nearest | 4 | 0.006 |  |  |  |  |  |  |  |  | False | -2.27 | 71 |
| XP-1:t2 | 2026-10-15 | TSLA | cash_debit | control | bull_call | nearest | 4 | 0.055 |  |  |  |  |  |  |  |  | False | -32.54 | 70 |
| XP-1:t2 | 2026-10-15 | XOM | cash_debit | control | bull_call | same_expiry | 0 | 0.057 |  |  |  |  |  |  |  |  | False | -21.83 | 43 |
| XP-1:t2 | 2026-10-16 | COIN | cash_debit | control | long_call | skip |  | n/a |  |  |  |  |  |  |  |  |  | n/a | 0 |
| XP-1:t2 | 2026-10-16 | MU | cash_debit | control | long_call | skip |  | n/a |  |  |  |  |  |  |  |  |  | n/a | 0 |
| XP-1:t2 | 2026-10-16 | NVDA | cash_debit | control | long_call | skip |  | n/a |  |  |  |  |  |  |  |  |  | n/a | 0 |
| XP-1:t3 | 2026-10-14 | AAPL | cash_debit | control | bear_put | nearest | 4 | 0.007 |  |  |  |  |  |  |  |  | False | -54.90 | 60 |
| XP-1:t3 | 2026-10-14 | INTC | cash_debit | control | bull_call | nearest | 7 | 0.133 |  |  |  |  |  |  |  |  | False | -15.37 | 54 |
| XP-1:t3 | 2026-10-15 | NVDA | cash_debit | control | long_call | exact | 0 | 0.000 | 2 | 1 | 2 | 2 | 2 | 2 | 2 |  | True | 1.50 | 63 |
| XP-1:t3 | 2026-10-15 | RKLB | cash_debit | control | long_put | exact | 0 | 0.000 |  |  |  |  |  |  |  |  | False | -20.19 | 43 |
| XP-1:t3 | 2026-10-15 | TLT | cash_debit | control | bear_put | nearest | 4 | 0.006 |  |  |  |  |  |  |  |  | False | -2.27 | 71 |
| XP-1:t3 | 2026-10-15 | TSM | cash_debit | control | long_call | same_expiry | 0 | 0.024 |  |  |  |  |  |  |  |  | False | -44.22 | 67 |
| XP-1:t3 | 2026-10-16 | COIN | cash_debit | control | bull_call | skip |  | n/a |  |  |  |  |  |  |  |  |  | n/a | 0 |
| XP-1:t3 | 2026-10-16 | MU | cash_debit | control | bull_call | skip |  | n/a |  |  |  |  |  |  |  |  |  | n/a | 0 |

Ranks are within the sim menu of live's own direction (the stance menu holding live's kind); blank = the matched candidate fails `ranking.filters` or that ranker's own gate on the EOD book. Live's anti-chase / range-edge filters, Research's choice and the gate are not replayed. The sim's top pick uses the trend_v2 proxy stance and the day's menu measure.

## 3. Exit timing (closed live positions through the sim's exit walk)

- All: n=12, replayed 2: tracking error (live - sim) sum $-1,136.53, mean |err| 619.07; with the intraday proxy sum $-1,136.53; same exit day 0.500; sim exits later by 0.50 d on average
- Stocks: n=12, replayed 2: tracking error (live - sim) sum $-1,136.53, mean |err| 619.07; with the intraday proxy sum $-1,136.53; same exit day 0.500; sim exits later by 0.50 d on average
- ETFs: n=0, replayed 0: tracking error (live - sim) sum $0.00, mean |err| n/a; with the intraday proxy sum $0.00; same exit day n/a; sim exits later by n/a d on average

| arm | ticker | kind | entry | live exit | live reason | live P&L | sim exit (EOD) | sim reason | sim P&L | proxy exit | proxy reason | err | sim @ live exit | pricing gap | timing gap | note |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| control | HOOD | long_put | 2026-10-12 | 2026-10-13 | research_review | -80.00 | 2026-10-14 | take_profit | 1,107.34 | 2026-10-14 | take_profit | -1,187.34 | -162.66 | 82.66 | -1,270.00 |  |
| control | NVDA | long_call | 2026-10-12 | 2026-10-13 | research_review | 40.00 |  | no exit by data end 2026-10-15 | n/a |  | no exit by data end 2026-10-15 | n/a | 107.42 | -67.42 | n/a |  |
| control | IREN | vertical_debit | 2026-10-12 | 2026-10-14 | stop | -260.00 | 2026-10-14 | stop | -310.81 | 2026-10-14 | stop | 50.81 | -310.81 | 50.81 | 0.00 |  |
| control | AAPL | vertical_debit | 2026-10-14 | 2026-10-16 | research_review | -50.00 |  | no exit by data end 2026-10-15 | n/a |  | no exit by data end 2026-10-15 | n/a | n/a | n/a | n/a |  |
| control | INTC | vertical_debit | 2026-10-14 | 2026-10-15 | research_review | -372.00 |  | no exit by data end 2026-10-15 | n/a |  | no exit by data end 2026-10-15 | n/a | -197.82 | -174.18 | n/a |  |
| control | TSM | long_call | 2026-10-15 | 2026-10-16 | research_review | -165.00 |  | no exit by data end 2026-10-15 | n/a |  | no exit by data end 2026-10-15 | n/a | n/a | n/a | n/a |  |
| control | RKLB | long_put | 2026-10-15 | 2026-10-16 | research_review | -154.00 |  | no exit by data end 2026-10-15 | n/a |  | no exit by data end 2026-10-15 | n/a | n/a | n/a | n/a |  |
| XP-1:t1 | AAPL | vertical_debit | 2026-10-14 | 2026-10-15 | research_review | 40.00 |  | no exit by data end 2026-10-15 | n/a |  | no exit by data end 2026-10-15 | n/a | -22.82 | 62.82 | n/a |  |
| XP-1:t1 | INTC | vertical_debit | 2026-10-14 | 2026-10-15 | research_review | -518.00 |  | no exit by data end 2026-10-15 | n/a |  | no exit by data end 2026-10-15 | n/a | -250.32 | -267.68 | n/a |  |
| XP-1:t2 | AAPL | vertical_debit | 2026-10-14 | 2026-10-15 | research_review | 30.00 |  | no exit by data end 2026-10-15 | n/a |  | no exit by data end 2026-10-15 | n/a | -22.82 | 52.82 | n/a |  |
| XP-1:t2 | INTC | vertical_debit | 2026-10-14 | 2026-10-15 | research_review | -214.00 |  | no exit by data end 2026-10-15 | n/a |  | no exit by data end 2026-10-15 | n/a | -98.91 | -115.09 | n/a |  |
| XP-1:t3 | INTC | vertical_debit | 2026-10-14 | 2026-10-15 | research_review | -294.00 |  | no exit by data end 2026-10-15 | n/a |  | no exit by data end 2026-10-15 | n/a | -132.82 | -161.18 | n/a |  |

`sim @ live exit` = the sim's own fills (EOD book, `slippage_x`) on live's entry and exit sessions; `pricing gap` = live - that (intraday fills vs EOD closes), `timing gap` = that - sim P&L (live's exit day vs the sim's exit rules). They sum to `err`.

## 4. Stance gap (Research `decisions.payload.stance`, persona research, stage shortlist, last per day)

m1_direction: unavailable: no live m1_direction series (E27.2 publishes dev OOS only; the live shadow is not wired), so the column is `unknown`

- All (n=134): trend_v2 agree 0.194 (n=98) {'bearish->bearish': 3, 'bearish->neutral': 30, 'bullish->bearish': 1, 'bullish->bullish': 16, 'bullish->neutral': 48}; market_v2 agree 0.000 (n=103) {'bearish->neutral': 33, 'bullish->neutral': 70}; m1_direction agree n/a (n=0) {}
- Stocks (n=129): trend_v2 agree 0.204 (n=93) {'bearish->bearish': 3, 'bearish->neutral': 25, 'bullish->bearish': 1, 'bullish->bullish': 16, 'bullish->neutral': 48}; market_v2 agree 0.000 (n=98) {'bearish->neutral': 28, 'bullish->neutral': 70}; m1_direction agree n/a (n=0) {}
- ETFs (n=5): trend_v2 agree 0.000 (n=5) {'bearish->neutral': 5}; market_v2 agree 0.000 (n=5) {'bearish->neutral': 5}; m1_direction agree n/a (n=0) {}
