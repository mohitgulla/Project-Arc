# ML in the backtest · `ml_er` / `ml_filter` / `ml_size` arms vs the incumbent (E27.5, D126)

Report-only comparison (D121: no BT registered, no spec committed). Runner:
`scripts/ml_backtest_arms.py run|tables` (own ledger and runs dir
`data/backtest/runs/e27_5/`, never `data/backtest/ledger.jsonl`). Aggregates only (D84).

## Setup

- **Window:** dev 2022-01-03 → 2023-12-29. These are E27.3's walk-forward test years,
  the only dev sessions on which a model can serve without a leak. Before 2022 no fold is
  trained, so `model_asof` returns nothing, and every arm would equal the control.
  There was no holdout read: `run_study` refuses `--phase holdout` for any ML arm or
  `m1_direction` cell.
- **Book:** E7.19 baseline (`config/ranking.yaml` as shipped), $25K, both profiles
  (`margin` = credit family, `cash_debit` = debit family). Live sleeves are read from
  config. One book walks both years.
- **Models:** credit `m2-05fa82807a93`, debit `m2-04e58916e118` (E27.3, both `candidate`).
  - Session d uses the fold whose labels ended before d. `test2022` serves 2022, and its
    labels end ≤ 10 sessions before 2022-01-03 (D126 purge + embargo). `test2023` serves
    2023.
  - Score coverage was **1.0** on every walk: every sim candidate joined its E7.21 ledger
    row on `(day, ticker, key)`.
- **Arms:**

  | arm | rule |
  |---|---|
  | control | incumbent ranker `managed_net_ev` (live `menu_measure: managed_net_ev_full`, D128) |
  | t1 | `ml_ranker: ml_er`: the cross-name pool sorted by M2 `e_r`, incumbent tie-breaks |
  | t2 | incumbent + `ml_filter: {min_e_r: 0.0}` (drop `e_r < 0`) |
  | t3 | `ml_er` + `ml_size: {mode: shrink}`: D18 contracts × `clip(e_r / median⁺ e_r, 0.5, 1.0)` |

- **Grid:** the E7.20 default, `trend_v2 / noisy_0.35 / lagged_5 / market_v2` × slippage
  x 0 / 0.25 / 0.5. All 12 cells ran on C125. C500 ran the control cell plus each arm's
  C125 worst cell (the E7.20 ladder).
- **Runtime:** C125 took 42 min and C500 107 min (6 workers, `nice 10`). The three ML arms
  share the control's menu build (`_build_overlay` drops the walk-only keys), so they add
  only walks.

## Parity check: the sim reproduces E27.3's replay

The control cell on C500 reproduces E27.3's 2022 replay exactly:

| | this study (2022 entries) | E27.3 replay 2022 |
|---|---|---|
| margin, control | 265 trades, −18,625 | `incumbent` 265, −18,625 |
| cash_debit, control | 249 trades, −19,803 | `incumbent` 249, −19,803 |
| cash_debit, t2 (`e_r ≥ 0`) | 125 trades, −22,635 | `ml_er_pos` 125, −22,635 |

The `ml_er` arm is close to E27.3's `ml_er`, but not identical:

| profile | this study | E27.3 `ml_er` |
|---|---|---|
| margin | 201, −21,943 | 210, −18,161 |
| debit | 215, −22,204 | 212, −22,857 |

E27.3 breaks ties by key. This arm keeps the incumbent's tie-breaks, which the card
specifies.

**2023 differs by construction.** E27.3 starts a fresh $25K book on 2023-01-03, whereas
this study carries the 2022 book forward. By end-2022 that book was down 75–90%, so
2023's sizing is a fraction of E27.3's. The sign checks below therefore compare the 2022
rows, where both start from the same state, as well as the totals.

## Results

All P&L is on the control cell: `trend_v2` stance, x = 0.25. "Diff" is treatment −
control net P&L. The grid column shows cells won out of cells run. The full tables
(All / Stocks / ETFs × sleeve, per entry year, every grid cell) are in the appendices.

| rung | profile | arm | trades | net | max DD | diff (90% CI) | grid won | E7.20 call |
|---|---|---|---|---|---|---|---|---|
| C500 | margin | control | 472 | −23,997 | 96.5% | | | |
| C500 | margin | t1 `ml_er` | 365 | −24,014 | 96.7% | −17 (−14.2k, +13.7k) | 4/5 | **keep** |
| C500 | margin | t2 filter | 118 | −22,725 | 93.6% | +1,272 (−16.2k, +17.8k) | 5/5 | **keep** |
| C500 | margin | t3 `ml_er`+size | 506 | −19,839 | 88.7% | +4,158 (−9.0k, +18.9k) | 4/5 | **keep** |
| C500 | cash_debit | control | 498 | −22,419 | 93.1% | | | |
| C500 | cash_debit | t1 `ml_er` | 352 | −24,500 | 98.8% | −2,081 (−17.2k, +11.6k) | 3/5 | **keep** |
| C500 | cash_debit | t2 filter | 125 | −22,635 | 92.1% | −216 (−14.0k, +13.9k) | 4/5 | **keep** |
| C500 | cash_debit | t3 `ml_er`+size | 333 | −24,922 | 99.7% | −2,503 (−15.9k, +9.5k) | 2/5 | **keep** |
| C125 | margin | t1 / t2 / t3 | 410 / 137 / 497 | | | −534 / +6,318 / +1,793 | 7 / 8 / 8 of 12 | keep ×3 |
| C125 | cash_debit | t1 / t2 / t3 | 498 / 129 / 567 | | | +4,437 / +7,882 / +6,966 | 8 / 9 / 10 of 12 | keep ×3 |

**Every arm gets `keep`.** None passes the D25 rule on the control cell: every 90% CI
straddles 0 by roughly ±$10–17K, so the grid cannot promote any of them to `switch` or
`fragile`. No cell's CI upper bound falls below 0 either, so no arm is shown to be harmful.

**Stocks / ETFs split (C500 net, control cell):**

| profile | split | control | t1 | t2 | t3 |
|---|---|---|---|---|---|
| margin | Stocks | −18,336 | −13,130 | −11,598 | −10,704 |
| margin | ETFs | −5,661 | −10,884 | −11,127 | −9,135 |
| cash_debit | Stocks | −18,179 | −17,847 | −17,905 | −20,164 |
| cash_debit | ETFs | −4,240 | −6,653 | −4,730 | −4,758 |

On margin, every ML arm helps on stocks and hurts on ETFs. The `e_r` ranking moves risk
toward ETF credit spreads, which lose more per $ risked (t2 −0.52, t1 −0.31 vs −0.09
control).

### Sign check vs E27.3's decision-value replay

**C500, cash_debit:** all six checks agree. In both 2022 and in total, all three ML uses
lose to the incumbent, in the sim and in the replay.

**C500, margin:**

| arm | 2022 | total |
|---|---|---|
| t1 | **disagree**: sim −3,318, replay +464 (both small) | agree |
| t2 | agree | **disagree**: sim +1,272, replay −28,402 |
| t3 | agree | **disagree**: sim +4,158, replay −30,944 |

- The two total disagreements are the 2023 book-state effect described above. The
  replay's incumbent made +$21.9K in a fresh 2023 book. Here the 2022 book is ~96% down,
  so 2023 is too small to repeat that, and the smaller treatment drawdown (t3 88.7% vs
  96.5%) is what shows up as "better".
- The 2022 t1 disagreement is within the tie-break difference noted in the parity check.

**C125:** most signs disagree (ML better in the sim, worse in the replay). C125 is not
the replay's universe (C500), so this is expected, but it also means the C125 positives
do not carry over to C500.

**Flag for the Analyst:** where the two start from the same state (2022, C500), the sim
and the replay agree on every arm except the t1 margin coin flip. The only places they
part are artefacts of the book-state carry-over.

## Read: which use is most promising

1. **`ml_size: shrink` on credit (t3, margin): most promising, still `keep`.**
   - It is the only arm that beats the control on C500 net (+4.2K), on max DD (88.7% vs
     96.5%) and on net-per-risk (−0.114 vs −0.120), and it wins 4 of 5 C500 cells.
   - In 2022, where the comparison with the replay is clean, it made −16.2K vs the
     control's −18.6K and the replay's `ml_er` −18.2K.
   - The gain comes from sizing down weak-`e_r` trades (547 resized), not from picking
     different ones.
   - It fails on debit (2/5 cells, −2.5K), and its CI is ±$14K. This is a hypothesis,
     not evidence.
2. **`ml_filter e_r ≥ 0` (t2): robust in direction, but it starves the book.**
   - It won 5/5 C500 cells on margin and 4/5 on debit, so it is rarely much worse.
   - But it keeps only 118–125 trades, all in 2022. In 2023 `e_r < 0` holds for ~100% of
     candidates (E27.3's finding), so the arm never trades in 2023. That shows up as a
     smaller loss, not an edge.
   - Win rate falls (0.38 vs 0.44 margin, 0.38 vs 0.45 debit), and C500 ETFs lose
     −0.52 per $ risked on margin.
   - The filter as a hard skip is not usable while `e_r`'s level sits below 0. A
     re-centred threshold (a per-day quantile of `e_r`) is the testable form.
3. **`ml_er` as the ranker (t1): no edge.**
   - Flat on margin (−17) and worse on debit (−2.1K). Its DD is higher than the control's
     in both profiles on C500.
   - This matches E27.3's conclusion that the incumbent key out-ranks `e_r` over the menu.
4. **Debit family overall:** no ML use beats the incumbent on C500. Debit ideas should
   wait for a retrained debit M2.
5. **M1 uses (`m1_direction` stance proxy, `exits.model.path_vol: m1`)** are wired and
   tested but were **not run** in this comparison, which the card defines as M2's four
   arms.
   - Both read the point-in-time M1 outputs (`oos_m1_features.parquet` of the served
     version, 2022–2023 only; `m1_direction` → `unknown` before 2022). They are ready
     for a study.
   - An `m1` path arm needs its own menu build, so it costs a full build per rung
     (~40 min C125, ~105 min C500).

**Caveat on every number above:** the baseline loses 90–98% of the $25K book on both
profiles in 2022–2023. These are relative A/B comparisons inside a losing strategy, not
evidence of a profitable one (D125).

## Candidate BT proposals for the Analyst's backlog

These are candidates only: no spec files were written (D121), and the Analyst proposes
while the owner approves. They are ranked by expected information per compute hour.

1. **`ml_size: shrink` on margin with a fresh book per year.**
   - Arms: control vs `ml_size {mode: shrink}` with the incumbent ranker. That isolates
     sizing, which t3 confounded with `ml_er`.
   - Grid: the E7.20 default. Window: 2022 and 2023 as separate dev studies, so 2023
     doesn't inherit 2022's drawdown.
   - Expected: confirms or kills the one positive signal above. Cost: one shared build
     per rung.
2. **`ml_size` floor/cap sweep (≤ 3 variants: floor 0.25 / 0.5 / 0.75),** margin only,
   if (1) is at least `fragile`.
3. **`ml_filter: {max_p_stop: τ}` on margin.** E27.3's best single use was `ml_pstop` on
   credit 2022 (the stop classifier, AUC 0.74), and this comparison did not test it.
   Arms: control vs `max_p_stop` at the E27.3 fold τ.
4. **A re-centred `ml_filter`:** drop the bottom k% of each day's `e_r`, not `e_r < 0`.
   This needs a new `ml_filter` leg (`max_rank_pct`). File it as a small code card
   before the study.
5. **`m1_direction` as the stance proxy:** a grid-only study (the stance axis gains
   `m1_direction`) that measures whether M1's direction is a better analyst stand-in
   than `trend_v2`. Dev 2022–2023 only.
6. **`exits.model.path_vol: m1` on margin:** the M1 Monte Carlo paths in the managed-exit
   model. Each rung needs its own build, so run C125 first.

Not proposed: `ml_er` as the ranker (no edge on either rung, consistent with E27.3), and
any debit-family ML use until the debit M2 is retrained.

## Reproduce

```
ARC_ENV=paper nice -n 10 .venv/bin/python scripts/ml_backtest_arms.py run --universe c125 --workers 6
ARC_ENV=paper nice -n 10 .venv/bin/python scripts/ml_backtest_arms.py run --universe c500 --workers 6
.venv/bin/python scripts/ml_backtest_arms.py tables --universe c500
```

The ML extra must be installed (`uv sync --extra ml`). The spec it writes, BT-9000, lives
only under `data/backtest/runs/e27_5/specs/`.

## Appendix A · C500 tables (headline rung)

Run dir: `20261027T141223-dev-c500-9b6923a4` (under `data/backtest/runs/e27_5/runs/BT-9000/`)

### C500 · margin

| arm | trades | net | max_dd_pct | diff_vs_control | ci_lo | ci_hi | robust | cell_win_frac | coverage |
|---|---|---|---|---|---|---|---|---|---|
| control: control (incumbent managed_net_ev) | 472 | -23996.68 | 96.51 |  |  |  |  |  |  |
| t1: ml_er ranker | 365 | -24014.05 | 96.67 | -17.38 | -14207.23 | 13677.01 | keep | 0.8 | 1.0 |
| t2: incumbent + ml_filter e_r ≥ 0 | 118 | -22725.08 | 93.57 | 1271.59 | -16219.69 | 17799.3 | keep | 1.0 | 1.0 |
| t3: ml_er + ml_size shrink | 506 | -19838.65 | 88.73 | 4158.03 | -9049.43 | 18914.86 | keep | 0.8 | 1.0 |

**Grid: treatment − control net P&L per cell**

| cell | t1 | t2 | t3 |
|---|---|---|---|
| market_v2@0 | 7078.15 | 465.96 | -1040.15 |
| noisy_0.35@0.25 | 594.21 | 1405.05 | 1149.5 |
| noisy_0.35@0.5 | 1021.39 | 3578.97 | 63.06 |
| trend_v2@0 | 2392.15 | 6863.38 | 608.41 |
| trend_v2@0.25 | -17.38 | 1271.59 | 4158.03 |

**All / Stocks / ETFs × sleeve (control cell)**

| arm | split | sleeve | trades | net | per_risk | win |
|---|---|---|---|---|---|---|
| control | All | all | 472 | -23997.0 | -0.1198 | 0.441 |
| control | All | short | 313 | -16123.0 | -0.1607 | 0.396 |
| control | All | swing | 159 | -7874.0 | -0.0787 | 0.528 |
| control | Stocks | all | 353 | -18336.0 | -0.1313 | 0.425 |
| control | Stocks | short | 233 | -14690.0 | -0.2132 | 0.373 |
| control | Stocks | swing | 120 | -3645.0 | -0.0515 | 0.525 |
| control | ETFs | all | 119 | -5661.0 | -0.0932 | 0.487 |
| control | ETFs | short | 80 | -1433.0 | -0.0456 | 0.463 |
| control | ETFs | swing | 39 | -4228.0 | -0.1444 | 0.538 |
| t1 | All | all | 365 | -24014.0 | -0.2184 | 0.482 |
| t1 | All | short | 206 | -8683.0 | -0.1952 | 0.466 |
| t1 | All | swing | 159 | -15331.0 | -0.2342 | 0.503 |
| t1 | Stocks | all | 260 | -13130.0 | -0.1751 | 0.465 |
| t1 | Stocks | short | 153 | -5994.0 | -0.1817 | 0.438 |
| t1 | Stocks | swing | 107 | -7136.0 | -0.17 | 0.505 |
| t1 | ETFs | all | 105 | -10884.0 | -0.3111 | 0.524 |
| t1 | ETFs | short | 53 | -2689.0 | -0.234 | 0.547 |
| t1 | ETFs | swing | 52 | -8194.0 | -0.3489 | 0.5 |
| t2 | All | all | 118 | -22725.0 | -0.2741 | 0.381 |
| t2 | All | short | 42 | -6805.0 | -0.274 | 0.405 |
| t2 | All | swing | 76 | -15920.0 | -0.2741 | 0.368 |
| t2 | Stocks | all | 92 | -11598.0 | -0.1888 | 0.391 |
| t2 | Stocks | short | 31 | -3994.0 | -0.2091 | 0.387 |
| t2 | Stocks | swing | 61 | -7604.0 | -0.1796 | 0.393 |
| t2 | ETFs | all | 26 | -11127.0 | -0.5181 | 0.346 |
| t2 | ETFs | short | 11 | -2811.0 | -0.4902 | 0.455 |
| t2 | ETFs | swing | 15 | -8316.0 | -0.5283 | 0.267 |
| t3 | All | all | 506 | -19839.0 | -0.1136 | 0.498 |
| t3 | All | short | 317 | -6134.0 | -0.1001 | 0.473 |
| t3 | All | swing | 189 | -13705.0 | -0.1209 | 0.54 |
| t3 | Stocks | all | 359 | -10704.0 | -0.0874 | 0.487 |
| t3 | Stocks | short | 229 | -5775.0 | -0.1305 | 0.463 |
| t3 | Stocks | swing | 130 | -4929.0 | -0.0631 | 0.531 |
| t3 | ETFs | all | 147 | -9135.0 | -0.1751 | 0.524 |
| t3 | ETFs | short | 88 | -359.0 | -0.0211 | 0.5 |
| t3 | ETFs | swing | 59 | -8775.0 | -0.2497 | 0.559 |

**Per entry year (control cell; one book across both years)**

| arm | year | trades | net | per_risk | win | net_stocks | net_etfs |
|---|---|---|---|---|---|---|---|
| control | 2022 | 265 | -18625.0 | -0.117 | 0.468 | -13229.0 | -5397.0 |
| control | 2023 | 207 | -5371.0 | -0.1306 | 0.406 | -5107.0 | -264.0 |
| t1 | 2022 | 201 | -21943.0 | -0.2318 | 0.463 | -11609.0 | -10335.0 |
| t1 | 2023 | 164 | -2071.0 | -0.1354 | 0.506 | -1522.0 | -549.0 |
| t2 | 2022 | 118 | -22725.0 | -0.2741 | 0.381 | -11598.0 | -11127.0 |
| t3 | 2022 | 283 | -16199.0 | -0.1286 | 0.498 | -8736.0 | -7463.0 |
| t3 | 2023 | 223 | -3639.0 | -0.0749 | 0.498 | -1968.0 | -1672.0 |

**E27.3 decision-value replay (fresh C500 book per year)**

| version | year | use | trades | net |
|---|---|---|---|---|
| m2-05fa82807a93 | 2022 | incumbent | 265 | -18625.0 |
| m2-05fa82807a93 | 2022 | ml_er | 210 | -18161.0 |
| m2-05fa82807a93 | 2022 | ml_er_pos | 112 | -22951.0 |
| m2-05fa82807a93 | 2022 | ml_pstop | 158 | -16205.0 |
| m2-05fa82807a93 | 2022 | credit_width | 277 | -21020.0 |
| m2-05fa82807a93 | 2023 | incumbent | 278 | 21879.0 |
| m2-05fa82807a93 | 2023 | ml_er | 261 | -9528.0 |
| m2-05fa82807a93 | 2023 | ml_er_pos | 1 | -2198.0 |
| m2-05fa82807a93 | 2023 | ml_pstop | 217 | -11148.0 |
| m2-05fa82807a93 | 2023 | credit_width | 261 | -7833.0 |
| m2-05fa82807a93 | 2022+2023 | credit_width | 538 | -28853.0 |
| m2-05fa82807a93 | 2022+2023 | incumbent | 543 | 3254.0 |
| m2-05fa82807a93 | 2022+2023 | ml_er | 471 | -27690.0 |
| m2-05fa82807a93 | 2022+2023 | ml_er_pos | 113 | -25148.0 |
| m2-05fa82807a93 | 2022+2023 | ml_pstop | 375 | -27353.0 |

**Sign check vs E27.3**

- 2022 · t1 vs replay `ml_er`: sim diff -3318, replay diff +464 → **DISAGREE**
- 2022+2023 · t1 vs replay `ml_er`: sim diff -17, replay diff -30944 → agree
- 2022 · t2 vs replay `ml_er_pos`: sim diff -4100, replay diff -4326 → agree
- 2022+2023 · t2 vs replay `ml_er_pos`: sim diff +1272, replay diff -28402 → **DISAGREE**
- 2022 · t3 vs replay `ml_er`: sim diff +2426, replay diff +464 → agree
- 2022+2023 · t3 vs replay `ml_er`: sim diff +4158, replay diff -30944 → **DISAGREE**

### C500 · cash_debit

| arm | trades | net | max_dd_pct | diff_vs_control | ci_lo | ci_hi | robust | cell_win_frac | coverage |
|---|---|---|---|---|---|---|---|---|---|
| control: control (incumbent managed_net_ev) | 498 | -22419.23 | 93.09 |  |  |  |  |  |  |
| t1: ml_er ranker | 352 | -24499.86 | 98.76 | -2080.63 | -17221.21 | 11584.29 | keep | 0.6 | 1.0 |
| t2: incumbent + ml_filter e_r ≥ 0 | 125 | -22635.39 | 92.09 | -216.17 | -13983.51 | 13913.94 | keep | 0.8 | 1.0 |
| t3: ml_er + ml_size shrink | 333 | -24921.83 | 99.69 | -2502.6 | -15872.82 | 9527.45 | keep | 0.4 | 1.0 |

**Grid: treatment − control net P&L per cell**

| cell | t1 | t2 | t3 |
|---|---|---|---|
| market_v2@0 | 1496.62 | 4355.83 | 3121.85 |
| noisy_0.35@0.25 | -55.81 | 3150.7 | 625.23 |
| noisy_0.35@0.5 | 465.65 | 2133.42 | -2.33 |
| trend_v2@0 | 4489.5 | 427.34 | -1570.9 |
| trend_v2@0.25 | -2080.63 | -216.17 | -2502.6 |

**All / Stocks / ETFs × sleeve (control cell)**

| arm | split | sleeve | trades | net | per_risk | win |
|---|---|---|---|---|---|---|
| control | All | all | 498 | -22419.0 | -0.1331 | 0.45 |
| control | All | short | 328 | -5406.0 | -0.0598 | 0.433 |
| control | All | swing | 170 | -17013.0 | -0.218 | 0.482 |
| control | Stocks | all | 353 | -18179.0 | -0.1618 | 0.419 |
| control | Stocks | short | 239 | -7610.0 | -0.1216 | 0.393 |
| control | Stocks | swing | 114 | -10569.0 | -0.2121 | 0.474 |
| control | ETFs | all | 145 | -4240.0 | -0.0757 | 0.524 |
| control | ETFs | short | 89 | 2204.0 | 0.0792 | 0.539 |
| control | ETFs | swing | 56 | -6444.0 | -0.2284 | 0.5 |
| t1 | All | all | 352 | -24500.0 | -0.2274 | 0.429 |
| t1 | All | short | 207 | -10177.0 | -0.1979 | 0.44 |
| t1 | All | swing | 145 | -14323.0 | -0.2543 | 0.414 |
| t1 | Stocks | all | 252 | -17847.0 | -0.2377 | 0.425 |
| t1 | Stocks | short | 152 | -8594.0 | -0.2406 | 0.421 |
| t1 | Stocks | swing | 100 | -9254.0 | -0.2351 | 0.43 |
| t1 | ETFs | all | 100 | -6653.0 | -0.2036 | 0.44 |
| t1 | ETFs | short | 55 | -1583.0 | -0.1007 | 0.491 |
| t1 | ETFs | swing | 45 | -5069.0 | -0.2989 | 0.378 |
| t2 | All | all | 125 | -22635.0 | -0.2671 | 0.376 |
| t2 | All | short | 40 | -6152.0 | -0.2984 | 0.275 |
| t2 | All | swing | 85 | -16483.0 | -0.2571 | 0.424 |
| t2 | Stocks | all | 103 | -17905.0 | -0.2741 | 0.379 |
| t2 | Stocks | short | 35 | -4867.0 | -0.2748 | 0.257 |
| t2 | Stocks | swing | 68 | -13038.0 | -0.2739 | 0.441 |
| t2 | ETFs | all | 22 | -4730.0 | -0.2436 | 0.364 |
| t2 | ETFs | short | 5 | -1285.0 | -0.4426 | 0.4 |
| t2 | ETFs | swing | 17 | -3445.0 | -0.2086 | 0.353 |
| t3 | All | all | 333 | -24922.0 | -0.2803 | 0.369 |
| t3 | All | short | 216 | -8769.0 | -0.2483 | 0.375 |
| t3 | All | swing | 117 | -16153.0 | -0.3014 | 0.359 |
| t3 | Stocks | all | 244 | -20164.0 | -0.2905 | 0.381 |
| t3 | Stocks | short | 157 | -6445.0 | -0.2483 | 0.382 |
| t3 | Stocks | swing | 87 | -13718.0 | -0.3157 | 0.379 |
| t3 | ETFs | all | 89 | -4758.0 | -0.244 | 0.337 |
| t3 | ETFs | short | 59 | -2324.0 | -0.2484 | 0.356 |
| t3 | ETFs | swing | 30 | -2434.0 | -0.24 | 0.3 |

**Per entry year (control cell; one book across both years)**

| arm | year | trades | net | per_risk | win | net_stocks | net_etfs |
|---|---|---|---|---|---|---|---|
| control | 2022 | 249 | -19803.0 | -0.1697 | 0.45 | -14659.0 | -5144.0 |
| control | 2023 | 249 | -2616.0 | -0.0505 | 0.45 | -3520.0 | 904.0 |
| t1 | 2022 | 215 | -22204.0 | -0.2292 | 0.428 | -16826.0 | -5378.0 |
| t1 | 2023 | 137 | -2296.0 | -0.211 | 0.431 | -1021.0 | -1275.0 |
| t2 | 2022 | 125 | -22635.0 | -0.2671 | 0.376 | -17905.0 | -4730.0 |
| t3 | 2022 | 249 | -22965.0 | -0.2727 | 0.398 | -18718.0 | -4247.0 |
| t3 | 2023 | 84 | -1957.0 | -0.4177 | 0.286 | -1446.0 | -511.0 |

**E27.3 decision-value replay (fresh C500 book per year)**

| version | year | use | trades | net |
|---|---|---|---|---|
| m2-04e58916e118 | 2022 | incumbent | 249 | -19803.0 |
| m2-04e58916e118 | 2022 | ml_er | 212 | -22857.0 |
| m2-04e58916e118 | 2022 | ml_er_pos | 125 | -22635.0 |
| m2-04e58916e118 | 2022 | ml_pstop | 141 | -21615.0 |
| m2-04e58916e118 | 2022 | debit_width | 276 | -23485.0 |
| m2-04e58916e118 | 2023 | incumbent | 282 | -1050.0 |
| m2-04e58916e118 | 2023 | ml_er | 264 | -6315.0 |
| m2-04e58916e118 | 2023 | ml_er_pos | 0 | 0.0 |
| m2-04e58916e118 | 2023 | ml_pstop | 178 | -15432.0 |
| m2-04e58916e118 | 2023 | debit_width | 257 | -23679.0 |
| m2-04e58916e118 | 2022+2023 | debit_width | 533 | -47163.0 |
| m2-04e58916e118 | 2022+2023 | incumbent | 531 | -20854.0 |
| m2-04e58916e118 | 2022+2023 | ml_er | 476 | -29172.0 |
| m2-04e58916e118 | 2022+2023 | ml_er_pos | 125 | -22635.0 |
| m2-04e58916e118 | 2022+2023 | ml_pstop | 319 | -37047.0 |

**Sign check vs E27.3**

- 2022 · t1 vs replay `ml_er`: sim diff -2401, replay diff -3054 → agree
- 2022+2023 · t1 vs replay `ml_er`: sim diff -2081, replay diff -8318 → agree
- 2022 · t2 vs replay `ml_er_pos`: sim diff -2832, replay diff -2832 → agree
- 2022+2023 · t2 vs replay `ml_er_pos`: sim diff -216, replay diff -1781 → agree
- 2022 · t3 vs replay `ml_er`: sim diff -3162, replay diff -3054 → agree
- 2022+2023 · t3 vs replay `ml_er`: sim diff -2503, replay diff -8318 → agree

## Appendix B · C125 tables (first rung)

Run dir: `20261027T132934-dev-c125-9b6923a4` (under `data/backtest/runs/e27_5/runs/BT-9000/`)

### C125 · margin

| arm | trades | net | max_dd_pct | diff_vs_control | ci_lo | ci_hi | robust | cell_win_frac | coverage |
|---|---|---|---|---|---|---|---|---|---|
| control: control (incumbent managed_net_ev) | 418 | -23460.65 | 97.88 |  |  |  |  |  |  |
| t1: ml_er ranker | 410 | -23994.94 | 97.32 | -534.3 | -22625.14 | 26372.45 | keep | 0.5833 | 1.0 |
| t2: incumbent + ml_filter e_r ≥ 0 | 137 | -17142.25 | 78.08 | 6318.39 | -11928.03 | 28571.04 | keep | 0.6667 | 1.0 |
| t3: ml_er + ml_size shrink | 497 | -21668.11 | 91.09 | 1792.54 | -14886.83 | 20036.47 | keep | 0.6667 | 1.0 |

**Grid: treatment − control net P&L per cell**

| cell | t1 | t2 | t3 |
|---|---|---|---|
| lagged_5@0 | 1253.45 | 5249.8 | 548.9 |
| lagged_5@0.25 | 246.54 | 3215.77 | -17.22 |
| lagged_5@0.5 | 1262.15 | 4636.41 | 788.93 |
| market_v2@0 | 14173.6 | -650.46 | 5075.03 |
| market_v2@0.25 | 11642.15 | 6558.63 | 9373.38 |
| market_v2@0.5 | 7588.91 | 5718.03 | 3862.93 |
| noisy_0.35@0 | -2237.31 | -2184.7 | -2516.51 |
| noisy_0.35@0.25 | -1340.67 | -2260.94 | -3304.56 |
| noisy_0.35@0.5 | -2277.6 | -2335.96 | -2600.91 |
| trend_v2@0 | -958.53 | 1413.67 | 5073.89 |
| trend_v2@0.25 | -534.3 | 6318.39 | 1792.54 |
| trend_v2@0.5 | 371.71 | 8709.32 | 2001.98 |

**All / Stocks / ETFs × sleeve (control cell)**

| arm | split | sleeve | trades | net | per_risk | win |
|---|---|---|---|---|---|---|
| control | All | all | 418 | -23461.0 | -0.1503 | 0.457 |
| control | All | short | 271 | -6408.0 | -0.0783 | 0.432 |
| control | All | swing | 147 | -17053.0 | -0.2296 | 0.503 |
| control | Stocks | all | 330 | -13266.0 | -0.1182 | 0.461 |
| control | Stocks | short | 216 | -4387.0 | -0.0715 | 0.435 |
| control | Stocks | swing | 114 | -8879.0 | -0.1743 | 0.509 |
| control | ETFs | all | 88 | -10195.0 | -0.2325 | 0.443 |
| control | ETFs | short | 55 | -2020.0 | -0.0985 | 0.418 |
| control | ETFs | swing | 33 | -8174.0 | -0.3505 | 0.485 |
| t1 | All | all | 410 | -23995.0 | -0.1233 | 0.466 |
| t1 | All | short | 250 | -1560.0 | -0.0187 | 0.476 |
| t1 | All | swing | 160 | -22435.0 | -0.2022 | 0.45 |
| t1 | Stocks | all | 322 | -14029.0 | -0.0949 | 0.466 |
| t1 | Stocks | short | 201 | -1230.0 | -0.019 | 0.483 |
| t1 | Stocks | swing | 121 | -12799.0 | -0.1541 | 0.438 |
| t1 | ETFs | all | 88 | -9966.0 | -0.2131 | 0.466 |
| t1 | ETFs | short | 49 | -330.0 | -0.0175 | 0.449 |
| t1 | ETFs | swing | 39 | -9636.0 | -0.3456 | 0.487 |
| t2 | All | all | 137 | -17142.0 | -0.1207 | 0.467 |
| t2 | All | short | 46 | -1151.0 | -0.0377 | 0.435 |
| t2 | All | swing | 91 | -15992.0 | -0.1434 | 0.484 |
| t2 | Stocks | all | 108 | -4952.0 | -0.0461 | 0.463 |
| t2 | Stocks | short | 37 | 290.0 | 0.0124 | 0.432 |
| t2 | Stocks | swing | 71 | -5242.0 | -0.0624 | 0.479 |
| t2 | ETFs | all | 29 | -12190.0 | -0.3524 | 0.483 |
| t2 | ETFs | short | 9 | -1441.0 | -0.2017 | 0.444 |
| t2 | ETFs | swing | 20 | -10749.0 | -0.3917 | 0.5 |
| t3 | All | all | 497 | -21668.0 | -0.1134 | 0.433 |
| t3 | All | short | 309 | -5470.0 | -0.0717 | 0.395 |
| t3 | All | swing | 188 | -16198.0 | -0.1411 | 0.495 |
| t3 | Stocks | all | 381 | -11764.0 | -0.0797 | 0.441 |
| t3 | Stocks | short | 230 | -4905.0 | -0.0875 | 0.404 |
| t3 | Stocks | swing | 151 | -6859.0 | -0.0749 | 0.497 |
| t3 | ETFs | all | 116 | -9904.0 | -0.2281 | 0.405 |
| t3 | ETFs | short | 79 | -565.0 | -0.028 | 0.367 |
| t3 | ETFs | swing | 37 | -9339.0 | -0.4022 | 0.486 |

**Per entry year (control cell; one book across both years)**

| arm | year | trades | net | per_risk | win | net_stocks | net_etfs |
|---|---|---|---|---|---|---|---|
| control | 2022 | 229 | -19814.0 | -0.1534 | 0.467 | -10542.0 | -9273.0 |
| control | 2023 | 189 | -3646.0 | -0.1353 | 0.444 | -2724.0 | -922.0 |
| t1 | 2022 | 253 | -15416.0 | -0.0944 | 0.514 | -8757.0 | -6659.0 |
| t1 | 2023 | 157 | -8579.0 | -0.2749 | 0.389 | -5272.0 | -3307.0 |
| t2 | 2022 | 137 | -17142.0 | -0.1207 | 0.467 | -4952.0 | -12190.0 |
| t3 | 2022 | 301 | -14795.0 | -0.0985 | 0.458 | -7106.0 | -7689.0 |
| t3 | 2023 | 196 | -6873.0 | -0.168 | 0.393 | -4658.0 | -2215.0 |

**E27.3 decision-value replay (fresh C500 book per year)**

| version | year | use | trades | net |
|---|---|---|---|---|
| m2-05fa82807a93 | 2022 | incumbent | 265 | -18625.0 |
| m2-05fa82807a93 | 2022 | ml_er | 210 | -18161.0 |
| m2-05fa82807a93 | 2022 | ml_er_pos | 112 | -22951.0 |
| m2-05fa82807a93 | 2022 | ml_pstop | 158 | -16205.0 |
| m2-05fa82807a93 | 2022 | credit_width | 277 | -21020.0 |
| m2-05fa82807a93 | 2023 | incumbent | 278 | 21879.0 |
| m2-05fa82807a93 | 2023 | ml_er | 261 | -9528.0 |
| m2-05fa82807a93 | 2023 | ml_er_pos | 1 | -2198.0 |
| m2-05fa82807a93 | 2023 | ml_pstop | 217 | -11148.0 |
| m2-05fa82807a93 | 2023 | credit_width | 261 | -7833.0 |
| m2-05fa82807a93 | 2022+2023 | credit_width | 538 | -28853.0 |
| m2-05fa82807a93 | 2022+2023 | incumbent | 543 | 3254.0 |
| m2-05fa82807a93 | 2022+2023 | ml_er | 471 | -27690.0 |
| m2-05fa82807a93 | 2022+2023 | ml_er_pos | 113 | -25148.0 |
| m2-05fa82807a93 | 2022+2023 | ml_pstop | 375 | -27353.0 |

**Sign check vs E27.3**

- 2022 · t1 vs replay `ml_er`: sim diff +4398, replay diff +464 → agree
- 2022+2023 · t1 vs replay `ml_er`: sim diff -534, replay diff -30944 → agree
- 2022 · t2 vs replay `ml_er_pos`: sim diff +2672, replay diff -4326 → **DISAGREE**
- 2022+2023 · t2 vs replay `ml_er_pos`: sim diff +6318, replay diff -28402 → **DISAGREE**
- 2022 · t3 vs replay `ml_er`: sim diff +5019, replay diff +464 → agree
- 2022+2023 · t3 vs replay `ml_er`: sim diff +1793, replay diff -30944 → **DISAGREE**

### C125 · cash_debit

| arm | trades | net | max_dd_pct | diff_vs_control | ci_lo | ci_hi | robust | cell_win_frac | coverage |
|---|---|---|---|---|---|---|---|---|---|
| control: control (incumbent managed_net_ev) | 382 | -24338.18 | 98.73 |  |  |  |  |  |  |
| t1: ml_er ranker | 498 | -19900.82 | 89.31 | 4437.36 | -21279.69 | 29008.13 | keep | 0.6667 | 1.0 |
| t2: incumbent + ml_filter e_r ≥ 0 | 129 | -16455.78 | 79.79 | 7882.4 | -14092.94 | 33047.88 | keep | 0.75 | 1.0 |
| t3: ml_er + ml_size shrink | 567 | -17372.36 | 80.23 | 6965.83 | -19544.48 | 33533.89 | keep | 0.8333 | 1.0 |

**Grid: treatment − control net P&L per cell**

| cell | t1 | t2 | t3 |
|---|---|---|---|
| lagged_5@0 | 1521.15 | 8748.79 | 1769.14 |
| lagged_5@0.25 | -677.91 | 5047.69 | -551.46 |
| lagged_5@0.5 | 1.81 | 6934.87 | 3.6 |
| market_v2@0 | -4906.13 | -2879.44 | -3993.65 |
| market_v2@0.25 | 3024.38 | 5914.0 | 2600.38 |
| market_v2@0.5 | 702.25 | 6135.45 | 2321.47 |
| noisy_0.35@0 | -2368.33 | -2784.67 | 2693.89 |
| noisy_0.35@0.25 | 1026.17 | 417.72 | 1045.06 |
| noisy_0.35@0.5 | 1066.8 | 4940.57 | 1440.63 |
| trend_v2@0 | -9800.94 | -4034.11 | 1437.6 |
| trend_v2@0.25 | 4437.36 | 7882.4 | 6965.83 |
| trend_v2@0.5 | 1128.67 | 7605.75 | 938.61 |

**All / Stocks / ETFs × sleeve (control cell)**

| arm | split | sleeve | trades | net | per_risk | win |
|---|---|---|---|---|---|---|
| control | All | all | 382 | -24338.0 | -0.1802 | 0.429 |
| control | All | short | 253 | -8185.0 | -0.1231 | 0.411 |
| control | All | swing | 129 | -16153.0 | -0.2356 | 0.465 |
| control | Stocks | all | 299 | -15536.0 | -0.1652 | 0.415 |
| control | Stocks | short | 202 | -8555.0 | -0.1795 | 0.386 |
| control | Stocks | swing | 97 | -6981.0 | -0.1505 | 0.474 |
| control | ETFs | all | 83 | -8802.0 | -0.2145 | 0.482 |
| control | ETFs | short | 51 | 371.0 | 0.0197 | 0.51 |
| control | ETFs | swing | 32 | -9173.0 | -0.4135 | 0.438 |
| t1 | All | all | 498 | -19901.0 | -0.0679 | 0.444 |
| t1 | All | short | 311 | -6180.0 | -0.0426 | 0.437 |
| t1 | All | swing | 187 | -13721.0 | -0.0928 | 0.455 |
| t1 | Stocks | all | 373 | -15947.0 | -0.0745 | 0.434 |
| t1 | Stocks | short | 242 | -716.0 | -0.0064 | 0.455 |
| t1 | Stocks | swing | 131 | -15231.0 | -0.1491 | 0.397 |
| t1 | ETFs | all | 125 | -3954.0 | -0.0502 | 0.472 |
| t1 | ETFs | short | 69 | -5464.0 | -0.165 | 0.377 |
| t1 | ETFs | swing | 56 | 1510.0 | 0.033 | 0.589 |
| t2 | All | all | 129 | -16456.0 | -0.1122 | 0.473 |
| t2 | All | short | 43 | -4971.0 | -0.1571 | 0.395 |
| t2 | All | swing | 86 | -11485.0 | -0.0998 | 0.512 |
| t2 | Stocks | all | 101 | -2760.0 | -0.0249 | 0.475 |
| t2 | Stocks | short | 33 | -2842.0 | -0.1189 | 0.333 |
| t2 | Stocks | swing | 68 | 81.0 | 0.0009 | 0.544 |
| t2 | ETFs | all | 28 | -13696.0 | -0.3828 | 0.464 |
| t2 | ETFs | short | 10 | -2129.0 | -0.2748 | 0.6 |
| t2 | ETFs | swing | 18 | -11566.0 | -0.4127 | 0.389 |
| t3 | All | all | 567 | -17372.0 | -0.0556 | 0.474 |
| t3 | All | short | 350 | -1454.0 | -0.0122 | 0.446 |
| t3 | All | swing | 217 | -15919.0 | -0.0826 | 0.521 |
| t3 | Stocks | all | 435 | -15837.0 | -0.0648 | 0.471 |
| t3 | Stocks | short | 276 | 600.0 | 0.0064 | 0.453 |
| t3 | Stocks | swing | 159 | -16437.0 | -0.1088 | 0.503 |
| t3 | ETFs | all | 132 | -1536.0 | -0.0226 | 0.485 |
| t3 | ETFs | short | 74 | -2053.0 | -0.0781 | 0.419 |
| t3 | ETFs | swing | 58 | 518.0 | 0.0124 | 0.569 |

**Per entry year (control cell; one book across both years)**

| arm | year | trades | net | per_risk | win | net_stocks | net_etfs |
|---|---|---|---|---|---|---|---|
| control | 2022 | 229 | -21752.0 | -0.1801 | 0.441 | -14124.0 | -7628.0 |
| control | 2023 | 153 | -2586.0 | -0.1811 | 0.412 | -1412.0 | -1174.0 |
| t1 | 2022 | 278 | -12123.0 | -0.0537 | 0.486 | -7265.0 | -4858.0 |
| t1 | 2023 | 220 | -7777.0 | -0.1158 | 0.391 | -8682.0 | 904.0 |
| t2 | 2022 | 129 | -16456.0 | -0.1122 | 0.473 | -2760.0 | -13696.0 |
| t3 | 2022 | 319 | -2528.0 | -0.0119 | 0.505 | -669.0 | -1859.0 |
| t3 | 2023 | 248 | -14845.0 | -0.1496 | 0.435 | -15168.0 | 323.0 |

**E27.3 decision-value replay (fresh C500 book per year)**

| version | year | use | trades | net |
|---|---|---|---|---|
| m2-04e58916e118 | 2022 | incumbent | 249 | -19803.0 |
| m2-04e58916e118 | 2022 | ml_er | 212 | -22857.0 |
| m2-04e58916e118 | 2022 | ml_er_pos | 125 | -22635.0 |
| m2-04e58916e118 | 2022 | ml_pstop | 141 | -21615.0 |
| m2-04e58916e118 | 2022 | debit_width | 276 | -23485.0 |
| m2-04e58916e118 | 2023 | incumbent | 282 | -1050.0 |
| m2-04e58916e118 | 2023 | ml_er | 264 | -6315.0 |
| m2-04e58916e118 | 2023 | ml_er_pos | 0 | 0.0 |
| m2-04e58916e118 | 2023 | ml_pstop | 178 | -15432.0 |
| m2-04e58916e118 | 2023 | debit_width | 257 | -23679.0 |
| m2-04e58916e118 | 2022+2023 | debit_width | 533 | -47163.0 |
| m2-04e58916e118 | 2022+2023 | incumbent | 531 | -20854.0 |
| m2-04e58916e118 | 2022+2023 | ml_er | 476 | -29172.0 |
| m2-04e58916e118 | 2022+2023 | ml_er_pos | 125 | -22635.0 |
| m2-04e58916e118 | 2022+2023 | ml_pstop | 319 | -37047.0 |

**Sign check vs E27.3**

- 2022 · t1 vs replay `ml_er`: sim diff +9629, replay diff -3054 → **DISAGREE**
- 2022+2023 · t1 vs replay `ml_er`: sim diff +4437, replay diff -8318 → **DISAGREE**
- 2022 · t2 vs replay `ml_er_pos`: sim diff +5296, replay diff -2832 → **DISAGREE**
- 2022+2023 · t2 vs replay `ml_er_pos`: sim diff +7882, replay diff -1781 → **DISAGREE**
- 2022 · t3 vs replay `ml_er`: sim diff +19224, replay diff -3054 → **DISAGREE**
- 2022+2023 · t3 vs replay `ml_er`: sim diff +6966, replay diff -8318 → **DISAGREE**
