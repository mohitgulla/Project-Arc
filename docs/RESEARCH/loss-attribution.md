# E7.17 · Loss attribution: why the live-shaped backtest loses most of the $25K account

Report only (D84). No defaults change, no XP drafts (D86). Every candidate change below goes to the owner as a list to pick from.

Reproduce (C125, decisions 2020-01-01 → 2023-12-31, exits settle by 2024-03-30, both profiles, incumbent ranker, `--workers 4`):

```
nice -n 10 .venv/bin/python scripts/loss_attribution.py runs  --workers 4   # legacy, marks_only, fixed, est_spread (~25 min each)
nice -n 10 .venv/bin/python scripts/loss_attribution.py walks --workers 4   # 7 walk-only counterfactuals on one `fixed` build
.venv/bin/python scripts/loss_attribution.py report                          # this report's tables: docs/RESEARCH/loss-attribution/*.csv
```

Runs are written to `data/backtest/runs/attribution/<name>/`. Every table this report quotes is a CSV in `docs/RESEARCH/loss-attribution/`, and `tables.md` collects them all. The CSVs hold aggregates and our own simulated trades only, with no ThetaData rows.

## The answer in five lines

1. **Not a simulator bug at heart.** Two artefacts were real and are now fixed: wide legs made positions unmarkable, so losers rode to expiry (margin: 71 expiry exits worth −$31.5K → 7), and forward splits were mis-settled (2 trades). Both books still lose about the same after the fix: margin −$17.4K, cash_debit −$24.9K. The sanity re-pricing matches 60/60 sampled trades exactly.
2. **Margin has no edge before costs, so costs are the whole loss.** At mid, 469 trades make +$1.7K. Slippage ($18.5K) and fees ($0.5K) make it −$17.4K, i.e. costs are 110 % of the loss. The payoff is lopsided: the average win is 0.20 R and the average loss 0.66 R, which needs 77 % winners, and the book wins 72 %. Iron condors account for −$11.3K (65 %) and bull puts for −$7.5K (43 %). Nearly all of the loss (−$22.6K) lands in 2022–2023.
3. **Cash_debit loses before costs, and one tail cluster wipes the account.** Gross at mid is −$9.3K (37 % of the loss), and costs add another $15.6K (63 %). 19 long puts make −$26.6K, which is 107 % of the loss. Nine of them were deep (Δ55/Δ70) puts opened 11–20 Mar 2020 in a high-vol bear regime, with up to 135 % of equity at risk. Eight were stopped out on the rebound, and the nine net −$25.1K. Without long puts, the account still holds $19.0K at mid-2020 instead of $4.0K, but it ends at $123 anyway.
4. **Selection is badly calibrated.** Managed EV over-predicts by $66 per trade on margin and $108 on cash_debit. Its correlation with realised P&L is −0.04 on margin and −0.20 on cash_debit, and the top EV decile is the *worst* decile on cash_debit (−$200 per unit). PoP's Brier score is no better than the base rate.
5. **Sizing and the tail turn a thin or negative edge into ruin.** Open risk averages 46–48 % of equity (p90 71–92 %), 75–90 % of it in one direction, and the top 10 % of trades make 71–79 % of the gross loss. Halving size or capping new risk per day cuts the margin loss by $3.9K (max DD 83 % → 65–69 %) but leaves the per-trade return about unchanged (−2.1 % to −3.0 % vs −2.2 %). The real quoted spread is **not** worse than the `costs.yaml` 4 % model: at the estimated spread, margin loses more (−$22.0K).

So the loss is cost plus calibration on margin, and structure plus sizing/tail on cash_debit. The regime decides *where* each loss lands (2020 crash for debit, 2022–23 for credit), not *whether* it happens: neither book makes money per unit of risk in any year except 2021 for credit and 2020 for debit.

## 1. Sanity check (simulator correctness)

`scripts/loss_attribution_sanity.py` re-prices each sampled trade by hand from the raw ThetaData NBBO rows, independently of the simulator's chain and mark pipeline:

- **Entry:** each leg at mid ± 0.25 × spread from that session's bid/ask.
- **Exit rule and day:** the mid P&L path against the resolved `config/exits.yaml` rules (`arc.exits.policy`). The close then waits for the first session where every leg passes the live per-leg spread rule (`check_spread_tick`).
- **Exit price:** an early exit at that session's quotes. An expiry is settled at intrinsic on the underlying close, split-adjusted when a split falls inside the hold.
- **Fees and size:** 0.0403 $/contract/leg each way. Size is contracts × max loss as a % of the entry session's marked equity.

The sample is the 15 worst trades plus 15 random ones (seed 17) per profile.

| book | profile | n | entry exact | exit reason and day match | P&L/unit exact | max risk % equity |
|---|---|---:|---:|---:|---:|---:|
| BT-3 control (E7.14 sim) | margin | 30 | 30 | 22 | 22 | 15.0 % |
| BT-3 control (E7.14 sim) | cash_debit | 30 | 30 | 23 | 23 | 15.1 % |
| `fixed` (this card) | margin | 30 | 30 | **30** | **30** | 14.8 % |
| `fixed` (this card) | cash_debit | 30 | 30 | **30** | **30** | 14.9 % |

Risk % equity is against marked equity for `fixed` and against realised equity for the E7.14 walk, which sizes on realised equity. The 15.1 % is within rounding of fees on the 15 % `max_alloc_pct` cap. The per-trade rows are in `sanity_bt3.csv` and `sanity_fixed.csv`, and the PR body carries the `fixed` table.

**Equity reconciliation** (`reconcile.csv`): Σ pnl = Δequity to the cent on every book. No position is open at the data end. Cash_debit's minimum equity is $115–120 and never negative. Marked equity counts each open position's latest mark once.

### Bugs found and fixed (`arc/backtest/sim.py`, `tests/test_backtest_sim_e717.py`)

1. **Exit book = entry chain.** The simulator marked held positions only on the entry-filtered chain, where legs wider than `max_quoted_spread_pct` are dropped. A position with one wide leg became unmarkable, so its stop could not fire and it rode to expiry. Live marks a held position on its legs' quotes whatever their width, then applies the per-leg spread rule to the close order. The fix, `SimSettings.exit_marks: all_quotes` (default), marks on every two-sided closing quote and keeps the spread rule on the close. All 15 mismatches in the BT-3 sample are this bug. 11 are "expiry" exits where the hand re-price closes one to five weeks earlier (10 stops or a DTE exit, 1 delayed TP), and 4 are stops or TPs that fired 1–6 sessions late. Margin expiry exits fall from 71 (−$31.5K) to 7 (+$0.6K) and cash_debit's from 18 (−$14.8K) to 1.
2. **Forward splits inside the hold.** Option strikes in the raw rows are unadjusted, but the trade was valued on the old contract against the post-split stock. For example, NVDA's 4:1 split on 2021-07-20 left 700/715 puts against a ~$180 stock. The fix, `SimSettings.splits: adjust` (default), detects whole-number splits from raw vs adjusted closes, follows the re-struck contract and settles on the adjusted close. Trend labels and momentum now use split-adjusted closes. This affects 1 trade per profile: NVDA −$1,285 → +$256 and NEE −$2,438 → −$703.

Both fixes apply only to the live-shaped walk (`selection: cross_name`). The `per_ticker` E7.5 walk (BT-8) is unchanged, and `exit_marks: entry_chain` + `splits: ignore` reproduces BT-3 control byte for byte (440/585 trades, identical `trades.csv` and `equity.csv`; run `legacy`).

What the fix does to the base book (`counterfactuals.csv`):

| book | margin net | margin return on risk | cash_debit net | cash_debit return on risk |
|---|---:|---:|---:|---:|
| `legacy` (= BT-3 control) | −$17,294 | −3.2 % | −$24,880 | −0.4 % |
| `marks_only` (exit book fix) | −$20,435 | −4.7 % | −$24,905 | −19.3 % |
| **`fixed`** (both fixes, base book below) | **−$17,354** | **−2.2 %** | **−$24,876** | **−19.0 %** |

"Return on risk" is Σ P&L per contract / Σ max loss per contract over the trades a walk took. Comparing it across books needs care: a book that is ruined early keeps trading at whatever size is left, and the trades it takes depend on that path. On the 334 margin trades the two books share, the fix adds +$404 per unit (55 exit reasons changed, of which 50 were expiry → stop/DTE/TP). On cash_debit's 249 shared trades it changes −$25. Cash_debit's per-unit result turns negative because the `fixed` path opens the March 2020 long-put cluster at fuller size and the later book differs. The trades themselves did not get worse.

## 2. Decomposition of net P&L (`fixed` base book)

Every cut is in `decomp_fixed_<profile>.csv` for All / Stocks / ETFs, with N, net, mean, median, win rate, P&L per $ risk and share of net. The BT-3 book is in `decomp_bt3_*`. The headlines (All):

**margin, −$17,354, 469 trades**

| cut | worst bucket | N | net | share of net | best bucket | N | net |
|---|---|---:|---:|---:|---|---:|---:|
| kind | iron_condor | 213 | −$11,315 | 65 % | bear_call | 56 | +$1,473 |
| spec | iron_condor Δ25 | 48 | −$9,178 | 53 % | bull_put Δ25 | 33 | +$2,436 |
| spec | bull_put Δ16 | 80 | −$8,515 | 49 % | | | |
| period | 2022 bear | 86 | −$13,875 | 80 % | 2021 bull | 135 | +$6,698 |
| period | 2023 | 127 | −$8,677 | 50 % | 2020 rebound | 80 | +$3,042 |
| vol at entry | mid | 107 | −$20,228 | 117 % | high | 81 | +$12,247 |
| tier | rest (active fill, not core/momentum) | 118 | −$18,848 | 109 % | core | 339 | +$4,404 |
| liquidity | 50k–500k contracts | 155 | −$22,720 | 131 % | ≥ 500k | 270 | +$3,774 |
| class | ETFs | 357 | −$14,027 | 81 % | Stocks | 112 | −$3,327 |
| earnings in hold | no (stocks) | 85 | −$6,687 | 39 % | yes | 27 | +$3,360 |
| capacity clipped | yes | 210 | −$13,015 | 75 % | no | 259 | −$4,340 |
| exit reason | stop | 70 | −$74,838 | | take_profit | 315 | +$78,536 |

The trend cut equals the kind cut on margin, because the stance maps one-to-one (bull → bull put, bear → bear call, sideways → condor). GLD alone accounts for −$9,739 in 15 condors and −$2,494 in 24 bull puts, all in the `rest` tier.

**cash_debit, −$24,876, 431 trades**

| cut | worst bucket | N | net | share of net | best bucket | N | net |
|---|---|---:|---:|---:|---|---:|---:|
| kind | long_put | 19 | −$26,613 | 107 % | long_call | 172 | +$6,136 |
| spec | long_put Δ70 | 8 | −$18,938 | 76 % | long_call Δ40 | 124 | +$5,002 |
| period | 2020 crash | 73 | −$16,991 | 68 % | 2020 rebound | 104 | +$1,269 |
| period | 2021 bull | 170 | −$8,580 | 34 % | | | |
| trend | bear | 89 | −$31,177 | 125 % | bull | 342 | +$6,301 |
| vol at entry | high | 190 | −$40,382 | 162 % | low | 124 | +$7,926 |
| tier | core | 169 | −$17,152 | 69 % | | | |
| class | Stocks | 296 | −$30,391 | 122 % | ETFs | 135 | +$5,515 |
| earnings in hold | yes | 24 | −$3,581 | 14 % (−54 % per $ risk) | | | |
| exit reason | stop | 183 | −$85,306 | | take_profit | 211 | +$63,218 |

Stops dominate both books. A stop realises −0.88 R on margin and −0.82 R on cash_debit, against stop levels of 0.75 of max loss / debit. Stops fire on the close (`stop_eod_only`), so the overshoot is the gap between closes.

## 3. Cost and mark attribution

`costs_fixed_*.csv`: gross at mid, slippage (= `pnl_mid − pnl − fees`) and fees.

| profile | gross at mid | slippage | fees | net | costs / gross | median entry spread, % of premium |
|---|---:|---:|---:|---:|---:|---:|
| margin | +$1,685 | $18,522 | $517 | −$17,354 | 11.3× | 11.6 % |
| · iron_condor | +$633 | $11,622 | $326 | −$11,315 | 18.9× | 10.9 % |
| · bull_put | −$2,254 | $5,108 | $151 | −$7,512 | | 13.7 % |
| · bear_call | +$3,306 | $1,792 | $41 | +$1,473 | 0.55× | 8.9 % |
| cash_debit | −$9,314 | $15,310 | $252 | −$24,876 | | 10.8 % |
| · bull_call | +$6,119 | $5,775 | $180 | +$165 | 0.97× | 22.1 % |
| · bear_put | +$2,120 | $6,647 | $37 | −$4,564 | 3.1× | 28.4 % |
| · long_call | +$8,410 | $2,242 | $32 | +$6,136 | 0.27× | 6.2 % |
| · long_put | −$25,962 | $647 | $4 | −$26,613 | | 5.6 % |

Debit verticals pay 22–28 % of their premium in spread at entry, and their gross edge at mid is roughly the size of their round-trip cost. Condors pay four legs of spread on a small credit.

**Quoted spread vs the 4 % model** (run `est_spread`): every fill is re-priced at `costs.yaml`'s estimated spread (4 % of mid, minimum $0.03) instead of the quote, with the same menus.

| profile | slippage / contract, quoted | slippage / contract, 4 % model | net, quoted | net, 4 % model |
|---|---:|---:|---:|---:|
| margin | $9.17 | $13.78 | −$17,354 | −$21,976 |
| cash_debit | $8.55 | $9.52 | −$24,876 | −$24,897 |

The real quotes are *cheaper* than the 4 % model on these names, so the loss does not come from the quoted spread being unusually wide versus what the model assumed. The 4 % model's $0.03 floor and its 4 % of a deep-ITM mid outweigh the narrow quoted spreads of liquid ETFs.

## 4. Model calibration

`calib_fixed_*.csv` holds results by kind, and `calib_decile_fixed_*.csv` holds results by managed-EV decile (per contract).

| profile · kind | n | predicted EV / trade | realised / trade | predicted PoP | realised win | Brier | Brier (base rate) | corr(EV, P&L) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| margin · all | 469 | $29.0 | −$37.0 | 0.766 | 0.721 | 0.194 | 0.201 | −0.04 |
| · iron_condor | 213 | $37.2 | −$53.1 | 0.674 | 0.610 | 0.240 | 0.238 | 0.01 |
| · bull_put | 200 | $20.1 | −$37.6 | 0.863 | 0.835 | 0.144 | 0.138 | −0.11 |
| · bear_call | 56 | $29.2 | $26.3 | 0.770 | 0.732 | 0.197 | 0.196 | −0.22 |
| cash_debit · all | 431 | $50.8 | −$57.7 | 0.603 | 0.501 | 0.253 | 0.250 | −0.20 |
| · long_call | 172 | $74.5 | $35.7 | 0.590 | 0.459 | 0.263 | 0.248 | −0.15 |
| · bull_call | 170 | $35.4 | $1.0 | 0.622 | 0.588 | 0.235 | 0.242 | 0.18 |
| · bear_put | 70 | $9.5 | −$65.2 | 0.603 | 0.471 | 0.259 | 0.249 | 0.03 |
| · long_put | 19 | $127.2 | −$1,400.7 | 0.557 | 0.211 | 0.307 | 0.166 | −0.17 |

- **EV is too high everywhere except bear calls,** and it does not rank. On margin, deciles 1–10 realise between −$35 and +$17 per contract with no slope. On cash_debit, the top decile (predicted $151/contract) realises −$200, the worst of the ten.
- **PoP is over-stated** by 4–5 points on margin and 10 points on cash_debit (34 points on long puts). Its Brier score is at or above the "always predict the base rate" score, so it adds no information at the trade level.
- The structures where the ranker is most wrong are long puts and long calls, i.e. the long-premium legs the D80 cash profile leans on, plus condors, the margin book's largest bucket.

## 5. Tail and sizing

`tail_fixed_*.csv` and `worst_days_fixed_*.csv`:

| | margin | cash_debit |
|---|---:|---:|
| gross loss / gross win | $100.1K / $82.7K | $89.0K / $64.1K |
| top 1 % / 5 % / 10 % of trades' share of gross loss | 18 % / 52 % / 79 % | 21 % / 57 % / 71 % |
| peak → trough | $34.1K (2021-10-13) → $5.7K (2023-11-20) | $43.2K (2020-03-11) → $115 (2023-05-10) |
| first below half the start | 2023-03-15 | 2020-04-08 |
| mean / p90 open max loss, % of equity | 46 % / 71 % | 48 % / 92 % |
| mean share of open positions in one direction | 0.75 | 0.90 |

- **The account broke:**
  - **cash_debit** on 2020-03-24 → 04-06. Nine to ten bearish positions were open, eight or nine of them long puts or bear puts, carrying 82–135 % of equity at risk. Five sessions each lost 17–36 % of equity.
  - **margin** on 2021-11 → 2023. The worst days hold 7–10 positions with 48–91 % of equity at risk, mostly short-put-side (bull puts and condors on SPY/QQQ/IWM/DIA/TQQQ), losing 6–26 % of equity on a single day.
- Per-day risk is bounded per trade (`max_alloc_pct` 15 %, every sampled `fixed` trade ≤ 14.9 %), but nothing bounds the *sum* across ten correlated positions opened in the same week.

**One-variable counterfactuals** (each walk differs from `fixed` in one setting; same menus; `counterfactuals.csv`):

| counterfactual | profile | trades | net | max DD | return on risk | equity 2020-06-30 | equity 2021-12-31 | equity 2022-12-31 |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| `fixed` (base) | margin | 469 | −$17,354 | 83.3 % | −2.2 % | $20,459 | $25,933 | $18,492 |
| half size | margin | 508 | −$13,487 | 64.7 % | −3.0 % | $20,627 | $20,737 | $15,556 |
| max open positions 10 → 5 | margin | 273 | −$21,285 | 87.9 % | −6.7 % | $22,435 | $16,302 | $11,067 |
| new risk ≤ 15 % of equity per day | margin | 445 | −$13,483 | 68.6 % | −2.1 % | $18,699 | $18,190 | $13,129 |
| `fixed` (base) | cash_debit | 431 | −$24,876 | 99.7 % | −19.0 % | $4,033 | $651 | $300 |
| half size | cash_debit | 676 | −$24,721 | 99.1 % | −7.0 % | $10,810 | $3,529 | $1,450 |
| max open positions 10 → 5 | cash_debit | 362 | −$24,903 | 99.7 % | −5.0 % | $11,647 | $1,328 | $552 |
| new risk ≤ 15 % of equity per day | cash_debit | 610 | −$24,795 | 99.7 % | −7.2 % | $8,003 | **$20,870** | $1,133 |

Smaller or slower risk buys time: cash_debit with the day cap is still at $20.9K at the end of 2021. It does not buy edge, though. Every sizing variant still ends at −54 % to −85 % (margin) or −99 % (cash_debit), because the per-trade return stays negative. Halving the slots *hurts* margin: the walk then fills its five slots with the same top-ranked, over-predicted names.

## 6. Structure counterfactuals (descriptive, not tuned)

The base book minus one kind at a time, plus BT-2's hold-to-expiry vs managed. BT-2 used the E7.14 simulator, so it is compared against BT-3 control.

| counterfactual | profile | trades | net | Δ net vs base | return on risk |
|---|---|---:|---:|---:|---:|
| minus bull_put | margin | 314 | −$13,966 | +$3,389 | −0.7 % |
| minus bear_call | margin | 426 | −$15,582 | +$1,773 | −2.4 % |
| minus iron_condor | margin | 404 | −$20,501 | −$3,147 | −4.8 % |
| minus bull_call | cash_debit | 340 | −$24,858 | +$19 | −13.6 % |
| minus bear_put | cash_debit | 380 | −$24,846 | +$31 | −23.6 % |
| minus long_call | cash_debit | 390 | −$24,880 | −$3 | −6.8 % |
| minus long_put | cash_debit | 461 | −$24,877 | −$0 | −1.4 % (equity $19.0K at 2020-06-30 vs $4.0K) |
| BT-2 hold to expiry (E7.14 sim) | margin | 319 | −$23,997 | −$6,702 vs managed | −5.5 % |
| BT-2 managed (= BT-3 control) | margin | 440 | −$17,294 | | −3.2 % |
| BT-2 hold to expiry (E7.14 sim) | cash_debit | 142 | −$24,907 | −$27 vs managed | −36.0 % |
| BT-2 managed (= BT-3 control) | cash_debit | 585 | −$24,880 | | −0.4 % |

Removing a kind frees its slots for the next-ranked kind. So "minus iron_condor" makes margin *worse*, because the freed slots go to more bull puts. No single kind removal turns either book positive. Managed exits beat hold-to-expiry per unit of risk on both profiles (the D4 statement does not hold on real quotes). The E7.14 BT-2 verdict still says "keep control" under the D25 bootstrap CI, which this report does not overrule.

**BT-8** (E7.5 six-ticker `per_ticker` walk, 2024-03 → 2026-07, existing results; `bt8.csv`) is also stop-dominated:

| profile · arm | net | stop P&L | take-profit P&L |
|---|---:|---:|---:|
| margin control | −$17.9K | −$26.2K | +$19.2K |
| margin t1 | −$16.0K | −$40.2K | +$49.1K |
| cash_debit control | −$15.7K | −$82.0K | +$74.2K |
| cash_debit t1 | −$21.5K | −$88.6K | +$73.8K |

So the pattern is not limited to 2020/2022. This card leaves the `per_ticker` walk unchanged, so BT-8 carries no E7.17 fix to re-run.

## 7. Backtest ↔ live

The live stores (`data/arc.db` plus the pre-D80 archive, read through a backup copy) hold **14 closed trades, all debit**: 10 `vertical_debit`, 3 `long_call`, 1 `long_put`. Net is −$2,827 (mean −$202), with 2 winners out of 14. Ten exits were Research reviews, 3 were `remaining_ev_floor` and 1 was a stop (`live_summary.csv`). E7.14's study 8 could replay 9 of them. Live P&L trails the model by a median $38 per trade, mostly because live enters intraday about 1.5 spreads above the closing mid. The same-session round-trip cost is $0.15/share live against $0.125 in the model.

- **Buckets.** All 14 sit in the cash_debit book's worst cost buckets (debit verticals pay 22–28 % of premium in spread) and in its weakest calibration (long premium). The live 14 % win rate is far below the backtest's 50 %.
- **Exit reasons can't be compared.** The backtest does not model the Research-review exit that closed 10 of the 14.
- **n = 14, and 9 replayable, over about two weeks.** Too few to say live shows "the same pattern". It is consistent with it and does not contradict it.

## Candidate changes (ranked; nothing applied)

Each lead is for the Analyst or owner to pick up as a BT study (D121) or an XP lead (D86). The drafted rule is D25's, verbatim: *Switch the default to a challenger only if it beats the incumbent on net P&L (higher) **and** max drawdown (not larger) in ≥ 2 of 3 sub-periods (bear, sideways, bull trend regime at entry), **and** the 90 % block-bootstrap CI of its daily P&L difference vs the incumbent excludes 0 (lower bound > 0).* The exit-book and split fixes should be in the simulator before any of these run (this PR).

1. **Portfolio-level risk budget (sizing).**
   - *Evidence:* open max loss averages 46–48 % of equity (p90 71–92 %) with 75–90 % in one direction. The day-cap counterfactual cuts margin's loss by $3.9K and DD from 83 % to 69 %, and keeps cash_debit at $20.9K to the end of 2021 vs $0.65K. N = 445/610 trades.
   - *Draft BT:* "A cap on total open max loss (30 % / 45 % of equity) and on new max loss per day (15 %) lowers max DD without lowering net P&L." Arms: control; day cap 15 %; open-risk cap 30 %; open-risk cap 45 %.
   - Needs one small sim knob (open-risk cap); the day cap already exists (`max_new_risk_pct_day`).
2. **No deep long puts in high-vol bear regimes (structure).**
   - *Evidence:* long_put Δ55/Δ70, N = 12, −$25.8K, 9 of them opened in one week of March 2020 at vol = high. Long-put PoP is predicted at 0.56 and realised at 0.21.
   - *Draft BT:* "Excluding long puts with |Δ| ≥ 0.55 when vol = high does not lower cash_debit net P&L." Arms: control; exclude long_put Δ55/Δ70 at high vol; exclude all long puts.
   - N is small and one episode drives it, so the D25 sub-period test will likely be inconclusive. Read it as a tail guard, not an edge.
3. **Liquidity floor for credit structures (cost).**
   - *Evidence:* margin trades on names with 50k–500k contracts/day lose −$22.7K (N = 155), while ≥ 500k make +$3.8K (N = 270). The `rest` tier (active fill, not core/momentum) loses −$18.8K (N = 118), GLD alone −$12.2K. Condors pay 19× their gross edge in costs.
   - *Draft BT:* "Restricting margin structures to names with ≥ 500k option contracts/day raises net P&L." Arms: control; floor 500k; floor 500k + no condors on `rest`.
4. **Re-calibrate or shrink the managed EV / PoP before ranking (selection).**
   - *Evidence:* predicted EV exceeds realised by $66–108 per trade, corr(EV, P&L) is −0.04 / −0.20, and the top EV decile is the worst on cash_debit (N = 43). PoP Brier is no better than the base rate.
   - *Lead:* an Analyst investigation rather than a grid. Candidates are (a) PoP from a realised-vol path model, (b) an EV haircut for the realised stop overshoot (−0.82 to −0.88 R vs 0.75), (c) ranking debit structures by `rorc_day` instead. BT-3's ranker arms already test (c) on the old simulator. Re-run BT-3 on the fixed simulator first.
5. **Debit verticals cost about their whole edge (cost/structure).**
   - *Evidence:* bull_call +$6.1K at mid vs $6.0K costs (N = 170). bear_put pays a median 28 % of premium in spread (N = 70).
   - *Draft BT:* "Requiring entry spread ≤ 15 % of the vertical's premium raises cash_debit net P&L." Arms: control; ≤ 15 %; ≤ 10 %.
6. **Stops overshoot on the close.** This lead is not testable on EOD data. Realised stop losses average 0.82–0.88 R against a 0.75 trigger, because stops fire at the close (`stop_eod_only`). An intraday stop needs intraday data, which the D84 dataset lacks. Note it for the live/XP side only.

## Caveats

- **EOD only.** Entries and exits are simulated at the 16:00 close; intraday stops, Research-review exits and `remaining_ev_floor` exits are not modelled.
- **Dev period only.** 2020-01-01 → 2023-12-31. The holdout (2024 onward) is read only as the settlement tail for trades opened by 2023-12-31, as in E7.14, plus BT-8's existing results and the live trades.
- **Earnings are detected, not announced.** The "earnings in hold" cut uses the E7.12 detector (`option_stats/earnings_events.parquet`, IV-crush based), not an announcement calendar.
- **Survivorship.** C125 is the 2026 universe list replayed backwards, so names that were delisted or fell out of liquidity since 2020 are missing.
- **Path dependence.** Once an account is below a few thousand dollars, the walk can only size what is left. Late-period trade counts and $ P&L then say little, which is why the report also gives per-unit (R) figures. Counterfactual differences are one draw of a path, not a distribution; no bootstrap is attached here.
- **Not replayed** (as in E7.14): the Discovery/Trending tiers, the LLM stance (a trend-label proxy stands in) and the sleeves (the sim ignores them).
- **Comparing variants.** `est_spread` and every counterfactual re-run the same menus, so they isolate fills and walk rules, not selection.
