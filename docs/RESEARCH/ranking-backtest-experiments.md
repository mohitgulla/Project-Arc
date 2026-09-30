# E7.5a — Ranking backtest experiments (regime menu, short DTE, Net EV ÷ cost)

Status: run 2026-09-29. Card E7.5a. Plan ref: D4 (menu), D18/D19 (sizing, exits),
D23 (scorecard), D25 (switch rule), D34 (auto-approve).
Harness: E7.5 (`arc backtest rank`), unchanged apart from config knobs that are off by
default. Baseline = [ranking-backtest-run.md](ranking-backtest-run.md). The baseline
re-run for this card reproduced it exactly: same trades, same P&L, same verdicts.

## TL;DR

- **No experiment meets the pre-registered D25 rule for either incumbent.** Recommendation:
  **change no default**. The scorecard gate (below) keeps D34 auto-approve from opening
  positions until paper shows ≥ 30 closed trades, net EV ≥ 0 and slippage within tolerance.
- **margin (`credit_width`)**: every variant still loses after costs (−16.7k to −22.0k on
  $100k over 29 months, against −24.4k). Each variant is profitable at zero slippage
  (+46.7k for (a), +58.1k for (b)), so the edge on credit structures is smaller than the
  modelled spread cost. The next useful input is **measured** paper slippage, not another
  menu change.
- **cash_debit (`debit_width`)**: (b), a shorter entry DTE (21–35), is the strongest
  result in the whole grid. The incumbent goes from −27.6k to **+33.7k**, max DD drops from
  87.3k to 56.1k, and every cash_debit ranker improves by +9.5k to +99.7k. The 90% CI is
  still [−34.9k, +162.6k], and the gain comes from bull sessions only, so it fails the rule.
  Recommendation: a **confirmation run on held-out tickers** (AMZN, AMD) with the rule
  fixed first, before anyone flips `dte_min/dte_max` for the profile.
- **Bearish structures lose in every cash_debit variant.** Dropping `bear_put` (a) moves
  the bear-session loss onto `long_put` (−42.0k) rather than removing it. A "no bearish
  trades" menu is the obvious next one-variable experiment. It is not run here: it was
  chosen after seeing these results, so it would need its own held-out run.
- One grid cell passes the rule: (c) + `ev_proxy` on margin, +18.0k vs the (c) incumbent,
  CI [+0.8k, +40.4k]. Treat it as **a multiple-comparisons artefact until confirmed**:
  4 challengers × 3 experiments × 2 profiles gives 24 tests at a 90% CI, so about 2 false
  passes are expected. Its absolute result is +$298 over 29 months.

## Setup (same as E7.5)

Tickers SPY, QQQ, IWM, AAPL, NVDA, TSLA · entries 2024-03-01 → 2026-07-31 · $100k ·
5,000 MC paths · cost x = 0.25 of an estimated spread max($0.03, 4%·mid) · smile marks ·
trend stance proxy · D18 sizing · D19/D23 exits from `config/exits.yaml`. Alpaca history
has no historical quotes, so every cost is modelled (see the caveats in the baseline).

Each experiment is **one config file**, deep-merged over `config/ranking.yaml`. None of
them has a code path of its own:

| Experiment | File | The one variable |
|---|---|---|
| (a) regime-conditional menu | `config/experiments/e75a_a_regime_menu.yaml` | margin: iron condors in `sideways` only, no trade in bull/bear; cash_debit: `bear_put` dropped (bear → `long_put`) |
| (b) shorter DTE + D19 exits | `config/experiments/e75a_b_short_dte.yaml` | entry window margin 30–45 → **21–30**, cash_debit 30–60 → **21–35**; exits unchanged (50% TP, 75% EOD stop, close at 7 DTE) |
| (c) Net EV ÷ cost filter | `config/experiments/e75a_c_ev_cost.yaml` | keep a candidate only if managed Net EV ≥ **1.0 ×** its E2.4 expected round-trip cost (entry + exit spread/slippage + fees); set before the run |

Reproduce (about 45 min for all four at 3 workers each):

    arc backtest rank --profile margin --profile cash_debit --from 2024-03-01 --to 2026-07-31 \
        --tickers SPY,QQQ,IWM,AAPL,NVDA,TSLA --workers 3 --offline --no-charts \
        --data-dir ~/GitHub/Project-Arc/data --out <out>/baseline
    # same command + --experiment config/experiments/e75a_a_regime_menu.yaml --out <out>/a …
    arc backtest rank-compare --baseline <out>/baseline \
        --experiment <out>/a --experiment <out>/b --experiment <out>/c --out <out>/compare

`rank-compare` compares each experiment's run of a ranker with the baseline run of the
**same** ranker. It uses the D25 rule unchanged: net P&L higher **and** max DD no larger in
≥ 2 of 3 trend sub-periods, **and** the 90% moving-block bootstrap CI of the daily P&L
difference above 0.

## Results: incumbents, experiment vs baseline

Net P&L and max DD in $, at the default cost x = 0.25.

| Profile / incumbent | Run | Trades | Net P&L | Max DD | Sharpe | Sub-periods won | P&L diff | 90% CI | Switch |
|---|---|---|---|---|---|---|---|---|---|
| margin / credit_width | baseline | 135 | −24,368 | 42,949 | −0.57 | | | | |
| | (a) regime menu | 92 | −16,654 | 42,715 | −0.33 | 2 (bear, bull)¹ | +7,714 | [−14,980, 30,756] | no |
| | (b) short DTE | 190 | −22,040 | 44,312 | −0.39 | 1 (bear) | +2,328 | [−29,336, 36,566] | no |
| | (c) EV ÷ cost ≥ 1 | 92 | −17,675 | 34,906 | −0.51 | 1 (bear) | +6,692 | [−21,967, 37,278] | no |
| cash_debit / debit_width | baseline | 118 | −27,608 | 87,340 | −0.08 | | | | |
| | (a) regime menu | 104 | −21,358 | 77,311 | −0.10 | 1 (bear) | +6,250 | [−27,502, 35,926] | no |
| | (b) short DTE | 166 | **+33,685** | **56,135** | **0.47** | 1 (bull) | +61,292 | [−34,862, 162,639] | no |
| | (c) EV ÷ cost ≥ 1 | 101 | −5,787 | 61,823 | 0.11 | 1 (bull) | +21,821 | [−32,591, 76,337] | no |

¹ (a) "wins" bear and bull for margin by not trading in them (a $0 loss beats a loss).
Its sideways condors alone lost −16,654.

The rows for every ranker are in
[ranking-backtest-experiments/compare.md](ranking-backtest-experiments/compare.md). The
full per-experiment reports (verdict by ranker within the experiment, sub-periods, cost
grid, by structure, worst trades) are `a_regime_menu.md`, `b_short_dte.md` and
`c_ev_cost.md` in the same directory.

### Sub-periods and structures (incumbents)

| Run | margin / credit_width by trend | cash_debit / debit_width by trend |
|---|---|---|
| baseline | bear −26,167 (23 t) · sideways +5,324 (74) · bull −3,524 (38) | bear −56,519 (41) · bull +28,912 (77) |
| (a) | sideways −16,654 (92 t, condors only) | bear −41,951 (27, all `long_put`) · bull +20,593 (77) |
| (b) | bear −22,572 (27) · sideways +2,778 (107) · bull −2,245 (56) | bear −50,151 (59) · bull **+83,836** (107) |
| (c) | bear −12,110 (7) · sideways −1,525 (64) · bull −4,040 (21) | bear −62,472 (31) · bull +56,685 (70) |

### Cost sensitivity (incumbents, net P&L by slippage x)

| Run | margin x = 0 / 0.25 / 0.5 | cash_debit x = 0 / 0.25 / 0.5 |
|---|---|---|
| baseline | +6,471 / −24,368 / −27,981 | −30,915 / −27,608 / −24,481 |
| (a) | +46,680 / −16,654 / −17,540 | +32,214 / −21,358 / −13,945 |
| (b) | +58,119 / −22,040 / −19,868 | +44,308 / +33,685 / +58,465 |
| (c) | +13,709 / −17,675 / −13,425 | −26,370 / −5,787 / +12,524 |

cash_debit P&L rising with x is not a cost benefit. A higher x makes the D19 targets
and stops fire at different marks and removes marginal trades (fewer trades per row), so
the path changes. It also means **the cash_debit results are path-sensitive**: one more
reason not to flip on a single run.

## What each experiment says

**(a) Regime-conditional menu.** The E7.5 sideways iron-condor bucket (+5,324 over 74
trades) does not survive as a stand-alone policy. Once condors are the only margin
structure, the capacity the verticals used to hold goes to 18 more condors, and the set
loses −16,654. The bucket was selected on the same data, so this is the expected
regression to the mean. For cash_debit, removing `bear_put` sends bear sessions to
`long_put`, which loses about as much. The loser is **bearish exposure in this
2024–2026 sample** (a rising market), not the debit-spread structure.

**(b) Shorter DTE with D19 exits.** Margin barely moves: it trades more (190 vs 135) and
the extra premium goes to costs. cash_debit improves the most of any variant: shorter
holds (13.0 vs 19.6 days), more turnover and a +83.8k bull bucket. The managed exits
leave money on the table here: hold-to-expiry would have made +143k against +33.7k
managed. That points to the debit take-profit (100% of debit) and 75% stop as the next
thing to test, again one variable at a time.

**(c) Net EV ÷ cost ≥ 1.** It cuts about a third of trades, lowers drawdown for both
incumbents (margin 42.9k → 34.9k, cash_debit 87.3k → 61.8k) and moves P&L toward 0. It
is the cheapest risk reduction in the grid, and it is live-ready as a scanner/pipeline
filter (`ranking.filters.min_net_ev_to_cost`, default off). Recommendation: **owner
decision**. On its own it does not clear the switch rule.

## Recommendation per account profile

| Profile | Change now | Next step |
|---|---|---|
| margin | none (incumbent stays; all variants lose after costs) | collect ≥ 30 paper fills, re-run with `--slippage-from-scorecard` (below); confirm (c)+`ev_proxy` on held-out tickers before considering it |
| cash_debit | none (b is promising, not significant) | held-out confirmation run of (b) (AMZN, AMD, same dates, rule fixed before); then a bull-only menu and a debit take-profit variant, one at a time |

These are recommendations only. The owner flips defaults (D25).

## Measured slippage feeds the next backtest

The E7.3 weekly scorecard now reports realised **entry slippage per structure kind as a
fraction x of the modelled bid-ask spread**. The modelled spread is the one the proposal
was priced with: the quote when valid, else the cost model's estimate. x is the same
number as `slippage_frac` in the backtest fill model `mid ± x·spread`. The rendered
scorecard has a table `Entry slippage by structure kind` (fills, realised $, modelled
spread $, x).

Two ways to use it in the next ranking backtest:

1. **From the audit DB**, opened read-only:

       arc backtest rank … --slippage-from-scorecard data/arc.db \
           [--scorecard-days 90] [--scorecard-min-fills 5]

   prints `measured slippage x: iron_condor 0.31, vertical_credit 0.22, …` and prices the
   base run's menus and fills per structure kind at that x. Kinds with fewer fills keep
   `config/costs.yaml`'s x. Negative x (price improvement) clamps to 0. The `slippage_grid`
   sensitivity rows keep their own x.
2. **Pinned in config**, for a reproducible run: an experiment overlay with

       backtest:
         slippage_by_kind: {iron_condor: 0.31, vertical_credit: 0.22}

The report header lists the measured x used. Today the paper store has **0 closed trades
and no fills**, so the CLI prints `none (too few fills)` and the run uses costs.yaml. That
was checked on a copy of `data/arc.db`.

## Scorecard gate in front of D34 auto-approve

Implemented in the same card (`arc/journal/scorecard.py` `auto_approve_readiness`,
`arc/approvals/service.py`; keys and runbook in OPS §5.10). An auto-approve of an **open**
needs, over the latest `auto_approve.min_closed_trades` (30) closed positions: that many
closed trades, realised net EV ≥ 0 after fees, and realised entry slippage ≤ modelled
half-spread × `auto_approve.slippage_tolerance` (1.5). Otherwise the card keeps its
buttons, the journal records `auto_approve_gated` with the failing criteria, and the
request waits for a manual approval. `auto_approve.scorecard_gate: off` is an explicit,
confirm-gated opt-out that logs a warning on every auto-approval. On the current paper
store the gate reports `0 closed trades < 30 required`.
