# E7.19 · Sim alignment: both entry-DTE sleeves — re-baseline (D125)

Status: done 2026-10-17. Generated tables: [sim-alignment/tables.md](sim-alignment/tables.md)
(`scripts/sim_alignment_report.py`). The E7.18 report re-run on the new books is
[sim-alignment/loss-anatomy/tables.md](sim-alignment/loss-anatomy/tables.md), produced by
`scripts/loss_anatomy.py --runs-dir data/backtest/runs/baseline_d125_post128 {runs,features,report --out docs/RESEARCH/sim-alignment}`
(the same code path, pointed at the new runs dir).

Books: `data/backtest/runs/baseline_d125/<book>` (pre-D128 config) and
`data/backtest/runs/baseline_d125_post128/<book>` (**the incumbent for every later
study**); `<book>_noproxy` is the same sim with `intraday_proxy: none`. Same window
(dev 2020-01-01 → 2023-12-29), data end, universes, rankers (`credit_width` /
`debit_width`) and seeds as E7.18 (`data/backtest/runs/anatomy/`, the "old" book).
C500 dev is primary, C125 dev the check, as in E7.18.

**Config provenance.** The books ran on the E7.19 sim with the pre-D128 profiles and
menus (branch `843f0ee` + docs; manifest `git_sha` f5f1da0 / e1aa20e):

- margin was a 30–45 credit-only profile with 2 % wings;
- `menu_measure` was `control`.

D128/D131, merged on main while the books ran, changed two things:

- margin now has the cash_debit sleeves and debit kinds, and a 5 % widest-fit wing;
- the default menu measure is now `managed_net_ev_full`.

Both changes move the menu-cache keys, so a study run on current main rebuilds its
menus and does **not** reproduce these books. Read them (variant `pre128` in the
generated tables) as the sim-change effect on fixed config.

**The incumbent is the post-D128 rebuild** `data/backtest/runs/baseline_d125_post128/<book>`
(+ `_noproxy`), variant `new` in the tables: the margin superset menu, the live
incumbent ranker (`menu_measure` → `managed_net_ev`), the debit-scoped anti-chase on the
walk (`sim.entry_filters: live`), margin buying power (D129) and the D131 rules. Why it
is worse than `pre128`, cell by cell, is in [Post-D128 attribution](#post-d128-attribution).

## What changed in the sim

| | old (pre-D125) | new (D125) |
|---|---|---|
| expirations | one, nearest the envelope middle (every cash_debit trade 21–45 DTE) | every expiry in each sleeve window, capped at 3 (low end, middle, high end) |
| cash_debit sleeves | none | short 10–35 and swing 36–60: own menu, slot cap (`sleeve_max_positions`) and exit block |
| short-sleeve exits | the kind policy (TP 0.60, stop 0.75 EOD) | the `short` block (TP 0.40, stop 0.50 **intraday**, close 3 DTE, lock 0.30/0.10 intraday) |
| intraday-flagged rules | EOD marks only | also checked at session d's underlying high/low (sticky-strike reprice, spread paid), stop-first |
| margin | one window 21–45, nearest expiry | one implicit `default` sleeve 21–45, every expiry (cap 3) |

## Post-D128 attribution

The incumbent ruins margin on both books (C500 −$24,934, 599 trades, −0.123 per-$-risk,
net at mid −$19,410; C125 −$24,382, −0.046, at mid −$10,940). The `pre128` margin books
lost −$14,976 / −$16,079 and were positive at mid. `scripts/sim_alignment_ablation.py`
walks a grid on the same cached menus to find which change does it:
- ranker: `managed_net_ev` (the live incumbent) or the width ranker;
- debit kinds on the margin menu or credit only;
- anti-chase (`entry_filters`) live or none;
- margin buying power on or off;
- D131 rules (`short_premium_regime` + `open_risk_cap`) on or off.

That is 32 margin cells and 8 cash_debit cells per book, all on the proxy-on sim. The
output is `baseline_d125_post128/ablation/<book>/`, and the 80 cells are in
[sim-alignment/ablation.csv](sim-alignment/ablation.csv). Every cell keeps the
post-D128 margin menu shape (sleeves, 5 % widest-fit wing), so the first row below is
not the `pre128` book.

**Margin, one change at a time (C500 / C125):**

| step | C500 trades | C500 net | C500 per-$-risk | C500 at mid | C125 net | C125 per-$-risk | C125 at mid |
|---|---|---|---|---|---|---|---|
| credit only, width ranker, nothing else | 630 | −$22,660 | −0.046 | −$833 | −$16,327 | −0.017 | +$12,928 |
| + ranker `managed_net_ev` | 688 | −$22,859 | −0.027 | −$3,961 | −$22,412 | −0.028 | −$7,004 |
| + debit kinds (D128 superset) | 822 | −$24,940 | −0.048 | −$9,214 | −$24,932 | −0.104 | −$16,698 |
| + debit-scoped anti-chase | 528 | −$24,955 | −0.096 | −$15,947 | −$24,929 | −0.087 | −$15,205 |
| + margin buying power | 551 | −$24,950 | −0.108 | −$15,538 | −$24,935 | −0.085 | −$16,018 |
| + D131 rules = **incumbent** | 599 | −$24,934 | −0.123 | −$19,410 | −$24,382 | −0.046 | −$10,940 |

**Margin, incumbent minus one change:**

| removed | C500 net | C500 per-$-risk | C500 at mid | C500 max DD | C125 net | C125 per-$-risk | C125 at mid | C125 max DD |
|---|---|---|---|---|---|---|---|---|
| — (incumbent) | −$24,934 | −0.123 | −$19,410 | 99.8 % | −$24,382 | −0.046 | −$10,940 | 98.4 % |
| debit kinds (credit only) | **−$4,569** | **−0.005** | **+$12,049** | **70.0 %** | **−$6,223** | **−0.010** | **+$3,774** | **56.3 %** |
| ranker → `credit_width` | −$23,083 | −0.044 | +$7,849 | 94.2 % | −$24,901 | −0.070 | −$7,723 | 99.7 % |
| anti-chase | −$22,720 | −0.036 | −$6,725 | 98.2 % | −$24,519 | −0.096 | −$17,207 | 99.3 % |
| buying power | identical | | | | identical | | | |
| D131 rules | −$24,950 | −0.108 | −$15,538 | 99.8 % | −$24,935 | −0.085 | −$16,018 | 99.8 % |

What the grid shows:

1. **The debit kinds on the margin menu are the loss.** Removing them is the only
   single change that keeps margin out of ruin on both books, and it gives the best
   margin book in the grid: C500 −$4,569 and +$12,049 at mid, C125 −$6,223 and +$3,774
   at mid. The credit-only width-ranker cell with D131 rules is +$1,851 on C500 (+$34,533
   at mid, 64.5 % max DD).
   - Under `managed_net_ev`, 84–88 % of the incumbent's C500 margin trades are debit.
   - Long calls alone are −$17,453 (n 298, −0.119), long puts −0.448 and bear puts
     −0.178, while the credit kinds are flat to positive
     (`sim-alignment/loss-anatomy/margin_by_kind.csv`).
   - E7.5b's evidence for `managed_net_ev_full` was a debit-only and a credit-only book
     (D128: "untested on a mixed menu"). On a mixed menu the ranker's managed Net EV
     prefers debit structures, and those lose after cost.
2. **The ranker is second.** With debit kinds on the menu, `credit_width` beats
   `managed_net_ev` on C500 (−0.044 vs −0.123, +$7,849 vs −$19,410 at mid) but not on
   C125 (−0.070 vs −0.046). Credit only, the two rankers are close on per-$-risk.
3. **Anti-chase is not consistent.** It costs money on C500 (off: −0.036, on: −0.123)
   and saves money on C125 (off: −0.096, on: −0.046). It cuts only debit ideas, so its
   effect rides on the debit kinds in item 1.
4. **Margin buying power never binds once the D131 rules are on.** The bp-on and bp-off
   cells match in every pair (same trades, same net): `open_risk_cap` refuses first.
   Without D131 it changes up to 182 opens and at most ±$3.1K net, mostly on cells
   that are already ruined.
5. **D131 rules mostly help.** Per-$-risk on minus off:
   - C125 mixed menu: +0.008 to +0.144;
   - C500 credit only: +0.016 to +0.052;
   - C500 mixed menu: −0.027 to +0.072;
   - C125 credit only: −0.018 to +0.020.
6. **cash_debit:** the grid reproduces the `pre128` book (debit_width, no filter, no
   D131: 674 trades, −0.040, +$45,560 at mid, against 679 / −0.040 / +$45,756). The
   incumbent ranker `managed_net_ev` is worse at mid on both books (C500 −$7,243 vs
   `debit_width` +$22,427; C125 −$19,625 vs −$3,318). Every cash_debit cell is ruined.
7. **The proxy is a large share of the incumbent's margin loss**, now that margin
   carries the short-sleeve debit book. With the proxy off, margin is C500 −$23,204 at
   −0.023 (+$1,112 at mid) and C125 −$5,133 at −0.006 (+$15,945 at mid).

These are dev-period walks of one sim on fixed menus. They attribute the loss, but none
of them is a study verdict. Under D86/D44 any change to the live menu, ranker or filter
goes through a BT study and an XP. The candidate studies this points to are:
- margin credit only, or the debit kinds on a separate measure;
- `credit_width` vs `managed_net_ev` on the mixed margin menu;
- anti-chase on/off on the mixed menu.

## Old vs pre128 books (All)

This section and the next five measure the sim change on fixed pre-D128 config
(`new` below = variant `pre128` in the generated tables). The incumbent's numbers are in
[Post-D128 attribution](#post-d128-attribution); the E7.18 loss-anatomy re-run under
`sim-alignment/loss-anatomy/` is on the incumbent.

| book | profile | variant | trades | net | max DD | per-$-risk | win | net at mid |
|---|---|---|---|---|---|---|---|---|
| C500 | margin | old | 480 | −$17,739 | 78.7 % | −0.038 | 0.706 | −$547 |
| C500 | margin | **new** | 509 | −$14,976 | 80.2 % | −0.027 | 0.721 | +$11,530 |
| C500 | cash_debit | old | 674 | −$24,926 | 99.9 % | −0.039 | 0.493 | +$9,956 |
| C500 | cash_debit | **new** | 679 | −$24,964 | 99.9 % | −0.040 | 0.406 | +$45,756 |
| C500 | cash_debit | new, proxy off | 772 | −$24,959 | 99.95 % | −0.023 | 0.449 | +$81,056 |
| C125 | margin | old | 435 | −$22,895 | 92.8 % | −0.053 | 0.685 | −$6,980 |
| C125 | margin | **new** | 529 | −$16,079 | 81.2 % | −0.026 | 0.724 | +$6,814 |
| C125 | cash_debit | old | 485 | −$24,892 | 99.8 % | −0.068 | 0.507 | −$480 |
| C125 | cash_debit | **new** | 343 | −$24,955 | 99.8 % | −0.136 | 0.420 | −$4,686 |
| C125 | cash_debit | new, proxy off | 567 | −$24,942 | 99.9 % | −0.054 | 0.466 | +$26,126 |

- Σ pnl = Δequity on every book (diff $0.00, 0 open at the end), and equity never goes
  below $0 ([reconcile.csv](sim-alignment/reconcile.csv)).
- Cash is never negative. Every open is refused (`cash` skip) when its cash use exceeds
  equity minus the cash already held by open positions.
- Every cash_debit variant is ruined (equity < $110). Its $ net is censored at −$25K, so
  per-$-risk and the sleeve split are the comparable measures. The new C500 cash_debit
  book is ruined sooner: $65 at 2021-12-31 against $13,900 in the old book.
- Margin is identical with the proxy on and off: all of its rules are EOD-only.
- Stocks/ETFs splits are in tables.md `books`.

## Per-sleeve split (cash_debit, new)

| book | sleeve | n | net | per-$-risk | win | mean DTE at entry | mean days held | exits |
|---|---|---|---|---|---|---|---|---|
| C500 | short 10–35 | 513 | −$53,453 | −0.120 | 0.359 | 19.9 | 4.4 | proxy stop 246, TP 185, EOD stop 40, proxy lock 34 |
| C500 | swing 36–60 | 166 | +$28,489 | +0.155 | 0.554 | 46.4 | 15.8 | TP 90, stop 59, proxy lock 10 |
| C125 | short 10–35 | 252 | −$23,951 | −0.199 | 0.377 | 19.1 | 4.2 | proxy stop 128, TP 95, EOD stop 20, proxy lock 6 |
| C125 | swing 36–60 | 91 | −$1,004 | −0.016 | 0.538 | 46.3 | 13.5 | TP 50, stop 33, proxy lock 6 |

With the proxy off:

| book | sleeve | n | net | per-$-risk |
|---|---|---|---|---|
| C500 | short | 570 | −$40,246 | −0.058 |
| C500 | swing | 202 | +$15,287 | +0.039 |
| C125 | short | 402 | −$30,001 | −0.108 |
| C125 | swing | 165 | +$5,059 | +0.028 |

**The short sleeve is the loss on both universes, with and without the proxy; the swing
sleeve is flat to positive.**

`sleeve_full` skips: C500 1,149 (1,735 proxy off), C125 1,100 (1,189 proxy off).

## DTE-bucket distribution (cash_debit)

| entry DTE | C500 old n / per-risk | C500 new n / net / per-risk | C125 new n / per-risk |
|---|---|---|---|
| 10–20 | 0 | 248 / −$30,606 / −0.139 | 135 / −0.347 |
| 21–35 | 279 / −0.146 | 265 / −$22,846 / −0.101 | 117 / −0.019 |
| 36–45 | 395 / +0.036 | 91 / +$6,834 / +0.070 | 52 / +0.042 |
| 46–60 | 0 | 75 / +$21,655 / +0.252 | 39 / −0.052 |

- Both sleeves are populated (acceptance 2;
  `sim-alignment/loss-anatomy/cash_debit_by_dte_bucket.csv`).
- The old one-expiry book already showed the same split: its 21–35 entries were the
  loss and its 36–45 entries the gain.
- Margin (C500 new) has 202 trades at 21–35 (−0.015) and 307 at 36–45 (−0.034).

## Exit mix (cash_debit, C500)

| exit | old | new | new, proxy off |
|---|---|---|---|
| take profit | 46 % | 40.5 % | 43.5 % |
| stop (EOD) | 43 % | 14.6 % | 47.5 % |
| stop (intraday proxy) | — | 36.2 % | — |
| profit lock (EOD / proxy) | 5.6 % | 0.7 % / 6.5 % | 6.7 % |
| DTE exit / expiry | 5 % | 1.5 % | 2.2 % |

## Intraday proxy on/off

- The proxy changes the short sleeve the most. With it on, 246 of the 513 C500 short
  trades stop out on the day's range. The sleeve's mean hold is 4.4 sessions.
- With the proxy off, the same rules wait for an EOD breach. Per-$-risk on C500 goes from
  −0.040 (on) to −0.023 (off), and net at mid from +$45.8K (on) to +$81.1K (off).
- The proxy books the stop at the day's extreme and pays the spread there, while the EOD
  book often sees the price come back by the close.
- The proxy is stop-first, so it overstates stops on whipsaw days: a day that touched
  both TP and stop books the stop. Read the on/off pair as a bracket on the live short
  sleeve, not as a point estimate.

## Run times and peak RSS

| book | build | walk | peak RSS | menu-cache hit |
|---|---|---|---|---|
| C500 new (cold menus, 10 workers) | 18,525 s (5.1 h) | 12.7 s | 3,810 MB | 28 % |
| C500 new, proxy off (warm) | 1,858 s | 12.6 s | 3,819 MB | 100 % |
| C500 old (E7.18, warm) | 1,294 s | 8.4 s | 1,945 MB | 100 % |
| C125 new (cold menus, 6 workers, host shared with another card's tests) | 16,823 s (4.7 h) | 4.5 s | 2,409 MB | 4 % |
| C125 new, proxy off (warm) | 700 s | 4.3 s | 1,884 MB | 100 % |
| C125 old (E7.18, warm) | 535 s | 7.1 s | 1,945 MB | 100 % |

The menus are now cached, so a study that only changes ranking, portfolio or exit
settings re-runs C500 in about 30 minutes.

Size check (menu build, C125, 20 sessions from 2021-03-01):

| profile | nearest | all expiries | cap 3 |
|---|---|---|---|
| margin | 133 s | 285 s (2.1×) | 281 s |
| cash_debit | 154 s | 783 s (5.1×) | 694 s (4.5×) |

These numbers are recorded in D125.

## E7.18 headline findings on the pre128 books (C500 primary, C125 check)

| # | E7.18 finding | on the new books | verdict |
|---|---|---|---|
| 1 | Cost, not direction: margin flat at mid (−$547), cash_debit positive at mid (+$9,956) | margin at mid **+$11,530** (slippage $25,734); cash_debit at mid **+$45,756** (slippage $69,012) | **holds, stronger**: both books are positive at mid and lose on cost; slippage rose 55 % (margin) and doubled (cash_debit) |
| 2 | Margin loses on ETFs, cash_debit on stocks | margin: broad-equity ETFs flip to +$3,088 and **stocks now lose −$11,597** (−0.061); commodity/international ETFs still lose. cash_debit stocks −$32,866 (−0.082), ETFs positive | **changed for margin** (the loss moved from ETFs to stocks); holds for cash_debit |
| 3 | Losses cluster at regime turns | margin flips ≤10s −$19,355 (−0.116) vs +0.011 rest, C125 −0.075; cash_debit flips −$29,997 (**−0.216**) vs +0.010 rest, C125 −0.335 | **holds**, stronger for cash_debit |
| 4 | Bleed is short-dated debit (long puts, bear puts) and margin condors / bull puts | cash_debit long put −0.470, bear put −0.262, **bull call now −$22,807 (−0.075)**, long call +$38,362 (+0.185). Margin condors −$17,464 (−0.070); **bull puts now flat** (−$140) | holds for puts and condors; **changed** for margin bull puts (no longer a bleed) and cash_debit bull calls (a larger loss) |
| 5 | Losers declare early; 2021–22 is where the equity went; margin long holds lose | margin ≥29-session holds −$23,648 (−0.162); cash_debit 2021 −$22,059 (−0.187), and the book is ruined by the end of 2021 (2022–23 have 12 trades) | **holds**; the cash_debit ruin is earlier |
| 6 | Correlated stacking: same-cluster q5−q1 −0.166 R margin, −0.198 R cash_debit | margin +0.002 (C125 −0.036); cash_debit −0.065 (C125 −0.087) | **weaker**: the sign survives for cash_debit, but the effect is a third of its size; margin's effect is gone (it already flipped on C125 in E7.18) |
| 7 | Features barely predict losers (AUC 0.586 margin / 0.494 cash_debit) | AUC 0.575 margin / 0.527 cash_debit (only one fold: the book is ruined by 2022) | **holds** |

The missing-information rows also moved:

- **EOD stop overshoot** fell from $24,967 to $6,981 on cash_debit. The proxy now books
  most short-sleeve stops intraday, which is the point of the proxy.
- **Earnings held through** by cash_debit stocks fell from −$22,318 (n 49) to −$2,027
  (n 16). Shorter holds cross fewer earnings dates.
- **FOMC/CPI** for margin flipped from −$13,408 to +$1,615 (C500). C125 still shows
  −0.099, so the finding is unsettled.

Ranked candidates from E7.18 that these results change: the macro-event blackout
(margin) no longer holds on C500, and the debit earnings blackout loses most of its
evidence. The spread cap and the cluster cap still rest on findings 1 and 6 (the cluster
cap now on weaker evidence) and need re-testing on this incumbent book.

The table above was measured on the `pre128` books (commit 4fa18a45).
`sim-alignment/loss-anatomy/` is now regenerated on the incumbent. These are the
incumbent's C500 values that differ:
- **#1** no longer holds for margin: it is negative at mid (−$19,410). Only credit-only
  margin is positive at mid (attribution, item 1).
- **#3 holds**, stronger:
  - margin regime-flip trades ≤10 sessions −$22,160 (−0.140);
  - cash_debit −$14,704 (−0.268).
- **#4:** margin's bleed is now its debit kinds: long call −$17,453, long put −0.448,
  bear put −0.178. Margin condors and bull puts are flat.
- **#6** comes back on margin as `same_group_open` (q5−q1 −0.153 R, CI excludes 0).
  cash_debit `same_cluster_open` is −0.115 R with a CI that includes 0.
- **#7 holds:** fold AUCs ≤ 0.55.

## Caveats

- The E7.18 caveats still apply: dev period only, C500 survivorship, the stance proxy,
  and inferred earnings dates.
- The proxy reprices at the EOD IV (sticky strike). It ignores the intraday IV move, the
  order of moves within the day, and the quote width at the extreme. It is stop-first.
- cash_debit is ruined in every variant, so $ comparisons stop at the floor. Compare
  per-$-risk and the sleeve split instead.
- Every study verdict recorded before D125 ran on the one-expiry sim
  (`docs/RESEARCH/backtest-baseline.md`).
