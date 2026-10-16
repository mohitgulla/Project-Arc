# Backtest re-run on real quotes (E7.14, D84)

Status: **complete, 2026-10-16** (studies BT-2…BT-8 run through the E7.12 protocol on main 28afa22 + this branch; ledger `data/backtest/ledger.jsonl`). Dev = 2020-01-01 → 2023-12-31, holdout = 2024-01-01 → 2026-10-15, one holdout per spec.

Report only. Nothing in this document changes a default. Each recommendation is a
proposal for the owner (a draft `Experiment: XP-<n>` arm or a cost/config proposal).

## Verdicts that flipped

Each original verdict re-tested under its own rule, quoted verbatim in the spec (`config/backtest_studies/BT-<n>.yaml`). The **holdout** verdict is the one that counts. C500 holdouts can be cited as evidence by the Analyst (E7.15). A C125-only holdout is a hypothesis.

| Study | Original verdict | Holdout (rung) | New verdict | Flipped? |
|---|---|---|---|---|
| BT-2 · E7.2/D4 managed vs hold | management doesn't beat hold | C125 | keep hold. margin: managed +$9.7k, CI [−$25.6k, +$40.9k]. cash_debit: hold +$16.4k vs managed −$20.7k (CI spans 0) | no |
| BT-3 · E7.5 ranking | keep incumbents | **C500** | keep incumbents on both profiles. Closest: margin `rorc_day` +$4.7k, CI [−$18.8k, +$30.7k]. cash_debit `rorc_day` +$89.8k, CI [−$202k, +$386k] | no |
| BT-4 · E7.5b unified measure | keep incumbents | C125 | keep incumbents. Closest: margin `ev_proxy` +$2.5k | no |
| BT-5 · E16.3 anti-chase | do not recommend | **C500** | do not recommend (−$773) | no |
| BT-6 · E18.3 exit v2 vs v1/hold | keep v2 | C125 | keep v2. Dev (C125) said "recommend v1/hold" on a −$28 diff in an exhausted account; the holdout reverses that | no (dev only) |
| BT-7 · E17.3 regime v2 vs v1 | **roll back to v1** (owner kept v2) | **C500** | **keep v2, inconclusive.** margin: v1 +$35.0k, CI [+$3.5k, +$69.4k], but v2's $ drawdown is smaller, so the rollback leg ("v2 worse on both") fails. cash_debit: −$923, CI spans 0 | **yes** (rollback → keep, inconclusive) |

**Read this first.** On the dev period, every arm of both profiles loses most of the $25K account: margin −56 % to −81 %, cash_debit −99.5 % in every arm. Holdout margin is better, but still negative or flat for most arms. With equity near zero, the P&L differences between arms are mostly noise from a depleted account, not ranker, filter or exit effects. That is why almost every call is "keep". The unchanged verdicts therefore mean "not overturned", not "confirmed". E7.17 (loss attribution) investigates why the base book loses.

**One real signal:** BT-7 margin on the C500 holdout. Regime v1's stance labels beat v2 by +$35.0k, with the CI excluding 0 (v1 +$31.6k vs v2 −$3.4k). The pre-registered rule still says keep, because v2's dollar drawdown is smaller ($20.2k vs $24.5k). This is a candidate XP lead for the Analyst (`regime.model` v1 vs v2 on margin), not a flip.

**BT-8 (data only, same 6 tickers / 2024-03 → 2026-07):** switching from Alpaca smile marks to quoted marks moves margin by −$2.6k on the holdout, with CI [−$3,955, −$585] excluding 0. cash_debit moves by −$188. The data change alone shifts margin results by more than the D25 noise band. That is the opposite of BT-8's hypothesis, so older smile-mark verdicts on margin are not comparable.

## Cost model (study 6)

`scripts/cost_model_check.py --universe c125 --every 20` measures the closing NBBO spread
on every 20th session for all 125 names, from 2020-01 to 2026-10. That is 2.03 M contract
quotes, of which 1.36 M are tradeable (spread/mid ≤ 35 %). It compares the spreads with
the `config/costs.yaml` estimate, which is 4 % of mid with a $0.03 floor. The backtest
only uses that estimate where a contract has no quote. Live scanning and exit EV use it
wherever the quote is missing. The script outputs aggregates only.

| Bucket (tradeable) | contracts | median spread | median spread % of mid | quoted ÷ estimate (median) | estimate too low |
|---|---:|---:|---:|---:|---:|
| all tradeable | 1,364,179 | $0.16 | 10.4 % | 1.87 | 70 % |
| ≥ 1M daily $-vol | 229,706 | $0.04 | 3.1 % | 0.52 | 19 % |
| 100k–1M | 340,557 | $0.12 | 8.3 % | 1.34 | 64 % |
| 10k–100k | 573,811 | $0.20 | 12.3 % | 2.33 | 84 % |
| < 10k | 220,105 | $0.70 | 16.7 % | 3.89 | 97 % |

Across the moneyness buckets (0–20 % OTM) and the DTE buckets (7–75), the ratio stays
between 1.3 and 2.3. Liquidity is what drives the error, not strike or tenor. The flat
4 % is about right only for the most liquid tier. Everywhere else it understates the
spread 1.3–3.9×.

- Fit, minimising the median |log(quoted/estimate)| over all quotes: **spread_pct 0.14,
  spread_min 0.01**. The error drops from 0.91 to 0.59 log points.
- **Live slippage x** comes from the E7.3 scorecard rule applied to every live fill, in
  the current store plus the pre-D80 archive (23 fills). It is **0.27** against the
  configured 0.25: long_call 0.12 (5 fills), long_put 0.30 (4), vertical_debit 0.31 (14).

**Proposal (owner, cost/config, not applied).**
1. Keep `slippage_frac: 0.25`. The measured 0.27 sits inside the noise of 23 fills.
2. Replace the single `spread_pct`/`spread_min` with a tiered estimate keyed on the
   D9 liquidity bucket. The medians above give about 3 %, 8 %, 12 % and 17 % of mid,
   with a $0.01 floor. If one constant is preferred, use 0.14/0.01.

The tiered estimate matters only where quotes are missing. In the backtest that is rare
(quoted marks, E7.10), so the change mainly affects the live fallback.

## Universe size (study 7)

BT-3 dev (2020–23), incumbent ranker, the same spec on each rung:

| Profile | C125 | C250 | C500 |
|---|---|---|---|
| margin net P&L / max DD | −$17.3k / 83.0 % | −$14.8k / 80.4 % | −$14.0k / 80.0 % |
| margin trades | 440 | 444 | 437 |
| cash_debit net P&L / max DD | −$24.9k / 99.7 % | −$24.9k / 99.9 % | −$24.9k / 99.9 % |

A wider pool slightly reduces the margin loss (+$3.3k from C125 to C500). The trade count barely changes, so the funnel picks about as many trades from a bigger pool. The challengers' gap to the incumbent stays inside the CI on every rung. cash_debit is exhausted on every rung, so this test can't say anything about it. Verdict: a wider universe doesn't hurt, and there's weak evidence that it helps margin. Not a reason to change the live universe on its own. Liquidity-bucket breakdowns are in the per-study `docs/RESEARCH/studies/BT-3.md` skip/segment tables.

## Backtest ↔ live reconciliation (study 8)

`scripts/backtest_reconcile.py` replays every closed live trade from the read-only stores
(`data/arc.db` plus the pre-D80 archive). That is 14 closed trades, all debit structures.
Each trade is replayed on its own legs, using the backtest's marked chains and fill model
(mid ± 0.25 × spread at the session close). Five trades can't be replayed: four closed
after the Theta pull ends (2026-10-09), and one leg (VST) is missing from the cached chain.

| | n | median | mean |
|---|---:|---:|---:|
| Live entry − model entry, $/share | 9 | +0.81 | +0.85 |
| Live entry vs EOD mid, in spreads | 9 | +1.5 | +2.2 |
| Live close − model close, $/share | 9 | +0.60 | +0.42 |
| P&L live − model, $ per trade | 9 | −37.5 | −57.0 |
| Same-session round-trip cost, live | 5 | $0.15 | $0.30 |
| Same-session round-trip cost, model (2 × 0.25 × spread) | 5 | $0.125 | $0.18 |

- **Fill-model bias.** On a same-session round trip the price move cancels out, so the
  round trip measures the fill model by itself. Live paid about $0.15 per share against
  the model's $0.125. That gap is small, and it agrees with the measured x of 0.27.
- **Timing gap.** Live entries sit 1.5 spreads above the closing mid. The trades open
  intraday, and the backtest only has the 16:00 close, so this is a price move, not a
  fill cost. It works against the live book: 8 of 9 live entries were above the model
  entry, and live P&L trails the model by about $38 per trade.
- **Exit reasons can't be compared yet.** Of the 14 live closes, 10 were Research reviews,
  3 were `remaining_ev_floor` and 1 was a stop. The backtest models neither of the first
  two. At the data end, the
  rule-based exits (stop, lock, TP, DTE) had not fired for any replayed trade.
- **Sample.** Nine trades over six sessions is too few for a tracking-error estimate.
  The script can be re-run once the free-tier EOD history extends past 2026-10-09.

## Caveats

- EOD only: entries and exits fill at the closing quote. Intraday entries (the timing gap
  above) and intraday stops aren't modelled.
- No Discovery/Trending/LLM replay. The pool is the point-in-time C125/C250/C500 list,
  and ranking is the deterministic ranker.
- Earnings dates are projected, not historical.
- Survivorship: names that are missing Alpaca bars drop out of the pool on those days.
- Stocks and ETFs compete in one pool. The All/Stocks/ETFs table in every BT report is a
  diagnostic view; the verdict is stated on All.

## Simulator speed (E7.14)

Wall clock per study run, from `queue.log`, M-series host, 10 workers, warm menu cache unless noted. The changes are bit-identical (ndtr + scalar-flag fast path, see the PR).

| Run | Wall clock |
|---|---|
| BT-3 / BT-5 / BT-7 dev, C125 | 13–20 min |
| BT-2 dev, C125 (cold exit-overlay rebuild) | 25 min |
| BT-6 dev, C125 (two cold exit overlays) | 54 min |
| BT-4 dev, C125 (unified-measure menus, cold) | 5 h 07 min |
| BT-3 / BT-5 / BT-7 dev, C500 | 28 / 72 / 46 min |
| BT-3 dev, C250 | 16 min |
| Holdouts (2024 → 2026-10), C125 / C500 | 21–42 min (BT-4: 2 h 51 min, cold) |
| Full queue (16 runs, after the C125 warm-up) | about 16 h |

Rule of thumb for planning (E26): a new study that reuses existing menus takes about 1–2 h for its C125 → C500 → holdout ladder. A study whose overlay changes the menus (exits, menu measure) rebuilds them cold, about 5–9 h.
