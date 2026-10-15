# Backtest re-run on real quotes (E7.14, D84)

Status: **draft. The study runs are in progress; the verdict table is filled in from the
ledger as each study finishes.**

Report only. Nothing in this document changes a default. Each recommendation is a
proposal for the owner (a draft `Experiment: XP-<n>` arm or a cost/config proposal).

## Verdicts that flipped

<!-- VERDICTS -->

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

<!-- UNIVERSE -->

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

<!-- PERF -->
