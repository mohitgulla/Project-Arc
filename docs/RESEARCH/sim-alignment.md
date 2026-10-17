# E7.19 · Sim alignment: both entry-DTE sleeves — re-baseline (D125)

Status: C125 dev books done 2026-10-17; C500 dev books running (this file is updated
when they land). Generated tables: [sim-alignment/tables.md](sim-alignment/tables.md)
(`scripts/sim_alignment_report.py`); the E7.18 report re-run on the new books:
`sim-alignment/loss-anatomy/` (`scripts/loss_anatomy.py --runs-dir
data/backtest/runs/baseline_d125 report --out docs/RESEARCH/sim-alignment`).

Books: `data/backtest/runs/baseline_d125/<book>` (the incumbent for every later study)
and `<book>_noproxy` (same sim, `intraday_proxy: none`). Same window, data end,
universes, rankers and seeds as E7.18 (`data/backtest/runs/anatomy/`, the "old" book:
one expiration nearest the middle of the profile's DTE envelope).

## What changed in the sim

| | old (pre-D125) | new (D125) |
|---|---|---|
| expirations | one, nearest the envelope middle (cash_debit: every trade 31–45 DTE) | every expiry in each sleeve window, capped at 3 (low end, middle, high end) |
| cash_debit sleeves | none | short 10–35 and swing 36–60, each its own menu, slot cap and exit block |
| short-sleeve exits | the kind policy (TP 0.60, stop 0.75 EOD) | the `short` block (TP 0.40, stop 0.50 **intraday**, close 3 DTE, lock 0.30/0.10 intraday) |
| intraday-flagged rules | EOD marks only | also checked at session d's underlying high/low (sticky-strike reprice), stop-first |
| margin | one window 21–45 | one implicit `default` sleeve 21–45, every expiry (cap 3) |

## C125 dev: old vs new (both profiles, All)

| profile | book | trades | net | max DD | per-$-risk | win | net at mid |
|---|---|---|---|---|---|---|---|
| margin | old | 435 | −$22,895 | 92.8 % | −0.053 | 0.685 | −$6,980 |
| margin | **new** | 529 | −$16,079 | 81.2 % | −0.026 | 0.724 | +$6,814 |
| cash_debit | old | 485 | −$24,892 | 99.8 % | −0.068 | 0.507 | −$480 |
| cash_debit | **new** | 343 | −$24,955 | 99.8 % | −0.136 | 0.420 | −$4,686 |
| cash_debit | new, proxy off | 567 | −$24,942 | 99.9 % | −0.054 | 0.466 | +$26,126 |

Σ pnl = Δequity on every book (diff 0.00, 0 open at the end); equity never below $0
(cash_debit floors at $45 — it is ruined in every variant, so its $ net is censored at
−$25K and per-$-risk is the comparable measure). Stocks/ETFs splits: tables.md `books`.

## Per-sleeve split (C125, cash_debit, new)

| sleeve | n | net | per-$-risk | win | mean DTE at entry | mean days held | exits |
|---|---|---|---|---|---|---|---|
| short (10–35) | 252 | −$23,951 | −0.199 | 0.377 | 19.1 | 4.2 | proxy stop 128, TP 95, EOD stop 20, proxy lock 6 |
| swing (36–60) | 91 | −$1,004 | −0.016 | 0.538 | 46.3 | 13.5 | TP 50, stop 33, proxy lock 6 |

Proxy off: short 402 trades −$30,001 (−0.108), swing 165 trades +$5,059 (+0.028).
`sleeve_full` skipped 1,100 sleeve-full opens (1,189 proxy off).

## DTE-bucket distribution (C125, cash_debit)

| entry DTE | old n | old per-risk | new n | new net | new per-risk |
|---|---|---|---|---|---|
| 10–20 | 0 | | 135 | −$22,904 | −0.347 |
| 21–35 | 217 | −0.100 | 117 | −$1,047 | −0.019 |
| 36–45 | 268 | −0.040 | 52 | +$1,050 | +0.042 |
| 46–60 | 0 | | 39 | −$2,054 | −0.052 |

Both sleeves are populated (acceptance 2). Margin: 200 trades at 21–35 (+$3,891,
+0.019) and 329 at 36–45 (−$19,971, −0.048).

## Exit mix (C125, cash_debit)

old: stop 43 %, TP 48 %, lock 4.5 %, DTE exit 4 %. new: TP 42 %, **proxy stop 37 %**,
EOD stop 15.5 %, proxy lock 3.5 %. Proxy off: stop 46 %, TP 46 %, lock 6 %.

## Intraday proxy on/off (C125, cash_debit)

The proxy changes the short sleeve most: on, 128 of 252 short trades stop out on the
day's range (median hold 4 sessions); off, the same rules wait for an EOD breach and
the book takes 567 trades instead of 343, because with the proxy on it is nearly ruined
by mid-2020 (equity $2,750 on 2020-06-30 vs $9,891 off) and sizing then blocks most
opens — per-$-risk −0.136 on vs −0.054 off. Net at mid moves from
+$26.1K (off) to −$4.7K (on): the proxy books the stop at the extreme with the spread
paid, where the EOD book often sees the price come back. The proxy is stop-first and so
over-states stops on whipsaw days (a day that touched both TP and stop books the stop);
read the on/off pair as a bracket on the live short sleeve, not a point estimate.
Margin is identical on/off (its rules are all EOD-only).

## Run times and peak RSS

| book | build | walk | peak RSS | cache hit |
|---|---|---|---|---|
| C125 new (cold menus, 6 workers, host shared with another card's tests) | 16,823 s | 4.5 s | 2,409 MB | 4 % |
| C125 new, proxy off (warm menus) | 700 s | 4.3 s | 1,884 MB | 100 % |
| C125 old (E7.18, warm) | 535 s | 7.1 s | 1,945 MB | 100 % |

Size check (menu build, C125, 20 sessions from 2021-03-01): margin nearest 133 s → all
285 s (2.1×) → cap 3 281 s; cash_debit nearest 154 s → all 783 s (5.1×) → cap 3 694 s
(4.5×). Recorded in D125.

## E7.18 headline findings on the new books

_Filled from `sim-alignment/loss-anatomy/` when the C500 books land (C500 is E7.18's
primary book)._

## Caveats

- Same as E7.18 (dev period only, C500 survivorship, stance proxy, inferred earnings).
- The proxy reprices at the EOD IV (sticky strike) and ignores the intraday IV move,
  path order within the day and quote width at the extreme.
- cash_debit is ruined in every variant, so $ comparisons stop at the floor; compare
  per-$-risk and the sleeve split.
