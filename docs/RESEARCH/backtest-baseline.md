# E7.2 — Cost-aware backtest: baseline and D4 structures

Status: first baseline, 2026-09-27. Engine: `arc/backtest/`. Reproduce:

    uv run arc backtest --tickers SPY --start 2024-02-01 --end 2026-08-07 \
        --data-dir ~/GitHub/Project-Arc/data --out data/backtest/spy \
        --train-months 6 --test-months 3

That run takes ~80 s. The full generated report is in
[backtest-baseline-run.md](backtest-baseline-run.md). Per-trade CSVs are written next to
`report.md` in `--out`; they are not committed because `data/` is gitignored.

## TL;DR

- **Nothing here is a strategy yet.** Across 2024-02 → 2026-08 on SPY, the unconditional
  results are dominated by one fact: SPY rose from ~489 to ~773, about +58%. Structures
  that are long delta made money (bull put, bull call, long call). Structures that are
  short delta lost money (bear call, bear put, long put). That is beta, not edge.
- **The baseline has no edge after costs.** Minimal filters, every expiry in 20–60 DTE,
  all seven structures: 22,746 trades, win rate 46.6%, **PF 0.84**, avg −$39 per trade.
  The frictionless (mid) P&L is also negative, so the problem is not only costs.
- **Delta-neutral premium selling is roughly break-even.** Iron condors, 30–45 DTE, after
  default costs:

  | Short Δ | PF | Avg $ | Win rate |
  |---|---|---|---|
  | 16 | 1.14 | +20 | 76% |
  | 30 | 0.97 | −8 | 54% |

  Costs take about $11–20 per condor, 4–5% of the credit collected. Under the harshest
  cost setting (fill at the touch, 8% spread) the 20Δ condor drops to PF 0.88.
- **Regime conditioning is the only strong signal, and it is in-sample.** The 20-day
  trend and realised-vol labels at entry split the results sharply:

  | Structure | Condition | PF |
  |---|---|---|
  | Iron condor | sideways trend | 1.21 |
  | Iron condor | bear trend | 0.23 |
  | Iron condor | low vol | 1.76 |
  | Iron condor | high vol | 0.36 |
  | Bear call | high vol | 0.17 |
  | Bull put | high vol | 48 (232 trades, mostly the April 2025 rebound) |

  This supports the Markov-regime gating idea (PLAN §7) as a hypothesis to test next.
  It is not yet a validated rule.
- **Naive walk-forward selection fails out-of-sample.** The method: pick the best D4 spec
  by average return on risk over the prior 6 months (using only trades already expired),
  then trade it for the next 3 months. Over 9 splits this gave 506 OOS trades, **PF 0.48**,
  avg RoR −35%, max DD $98.5k per 1-lot series. Past-6-month winners were mostly
  directional bets that had just worked. This is the expected failure mode, and it is why
  selectivity has to come from a pre-registered regime rule, not from ranking specs.
- **Tail months** (P&L summed over all 28 D4 specs, booked at expiry): 2026-07 −$73k,
  2025-12 −$63k, 2025-11 −$63k, 2026-01 −$60k, 2026-02 −$53k. For short-premium
  structures the worst single months were 2025-03 (bull puts, −$12k to −$18k per spec)
  and 2026-05 (iron condors and bear calls, −$16k to −$26k per spec).

## Engine choice: in-house (not optopsy)

| Criterion | optopsy (goldspanlabs, PyPI) | In-house (`arc/backtest`) |
|---|---|---|
| Licence | **AGPL-3.0**. This repo is proprietary, so a network-served derivative would trigger copyleft; needs a legal call | Ours |
| Data model | Quote-based: `csv_data(... bid, ask ...)`; its fills and slippage models assume bid/ask per row | Works with **trade closes only** (Alpaca history has no historical NBBO, see the E7.1 finding) and uses the NBBO when present (ThetaData) |
| Strategy coverage | 38 strategies, per-leg delta targeting, early exits | The D4 whitelist only (7 structures), delta-targeted anchor, hold-to-expiry |
| Cost model | Commissions plus mid / spread / liquidity slippage | Explicit `mid ± x·spread` plus per-contract fees on open and ITM close; spread quoted or estimated |
| Integration | Separate data layer (EODHD cache) | Reads the E7.1 `ParquetHistoryStore` directly; risk comes from E2.2 `max_gain_loss` |
| Walk-forward | Not built in | `walk_forward_splits` / `walk_forward_eval` with a no-look-ahead train filter |
| Python | 3.12–3.13 | 3.12 |

Decision: **in-house.** Two reasons settled it. First, our only multi-year history
(Alpaca) has no quotes. Second, the AGPL licence is a real concern for a proprietary
repo. The engine is small (about 1,500 lines including docstrings), deterministic, and
covered by `tests/test_backtest.py`.
Revisit optopsy if we buy quote history (ThetaData Value) and want early-exit rules;
its simulator is richer there.

## Method

**Data.** E7.1 Alpaca daily option bars for SPY, 2024-02-01 → 2026-09-25 (665 sessions,
100% cached, 1.77M contract-days within 60 DTE). Underlying settlement and spot come from
Alpaca SIP daily bars, raw (unadjusted) closes, cached under
`data/underlying_daily/`. QQQ is cached only to 2024-10 and the other 18 tickers are not
cached yet, so this report is SPY-only. A partial SPY+QQQ run (2024-02 → 2025-03) is
qualitatively the same: QQQ bull put PF 1.52, long put PF 0.39.

**Chain.** For each session:

- mid = trade close, because Alpaca has no quote; the ThetaData NBBO mid is used when
  present.
- IV comes from a vectorised BSM bisection at a flat r = 4.5%, q = 0.
- Delta comes from BSM at that IV (European approximation).
- Contracts with no arbitrage-consistent IV are never selected.

**Selection.**

- Anchor strike = the contract with |Δ| closest to the target, within the tolerance.
- Wing = the strike nearest anchor ± 2% of spot, and it must land within 0.5–2× that
  width.
- For credit spreads, debit spreads and condors the anchor is the **short** strike (D4:
  16–30Δ short strikes). Long single options buy the anchor.
- D4 grid: 7 structures × Δ ∈ {0.16, 0.20, 0.25, 0.30}. One expiration per session,
  nearest the middle of 30–45 DTE. Every leg needs volume ≥ 1 that session.
- Baseline (Bawa, PLAN §7): the same 7 structures at Δ 0.25 ± 0.10, **every** expiry in
  20–60 DTE, no volume filter. One unit per spec per expiry per session.

**Costs** (`CostModel`, defaults):

- Each leg fills at `mid ± 0.25 × spread`. Buys pay up, sells give up.
- Spread = the quoted NBBO if present. Otherwise it is estimated as
  `max($0.03, 4% × mid)`. This estimate is the least-supported assumption in the study;
  see the sensitivity section.
- $0.65 per contract on open, and again on close for legs that finish in the money.
- Worst case (max loss) is measured **after** fills and fees.

**Exit.** Hold to expiry and settle at intrinsic against the underlying close on the
expiration date. D4 evidence says management rules don't beat hold-to-expiry. Early
assignment and pin risk are ignored.

**Metrics.**

- trades, win rate, PF (Σ wins / |Σ losses|), avg P&L, avg return on risk (P&L / max
  loss);
- avg cost (mid P&L − net P&L), and cost as a % of premium;
- max drawdown of cumulative 1-lot P&L booked at expiry;
- worst months by expiry month.

One unit per trade, and specs overlap in time. So totals and drawdowns describe a
1-lot-per-signal series, not a sized portfolio.

**Regimes** are labelled at the entry close only, so there is no look-ahead:

- Trend: 20-session return ≥ +5% bull, ≤ −5% bear, otherwise sideways. Same rule as
  E4.3 (PR #9); swap to `arc.features.regime` once that PR merges.
- Realised vol: 20-session annualised, <12% low, ≥20% high.

**Walk-forward.** Month-aligned rolling windows, 6-month train and 3-month test, with
test windows tiling the sample. Train trades count only if they had **expired** by the
train end. Selection score = average return on risk, with at least 10 train trades
required.

## Results (SPY, entries 2024-02-01 → 2026-08-07, default costs)

### Baseline (minimal filters)

| Structure | Trades | Win | PF | Avg $ | Avg RoR | Avg cost $ |
|---|---|---|---|---|---|---|
| bull_put | 3,233 | 88.4% | 1.57 | +53 | +4.8% | 10 |
| bull_call | 3,256 | 50.4% | 1.28 | +66 | +16.8% | 14 |
| long_call | 3,256 | 29.1% | 1.43 | +100 | +13.2% | 4 |
| iron_condor | 3,232 | 61.7% | 1.00 | −1 | −2.3% | 17 |
| bear_call | 3,237 | 68.3% | 0.73 | −54 | −7.0% | 6 |
| bear_put | 3,264 | 18.9% | 0.56 | −108 | −34.2% | 15 |
| long_put | 3,268 | 9.7% | 0.32 | −329 | −56.5% | 6 |
| **All** | **22,746** | **46.6%** | **0.84** | **−39** | **−9.4%** | **10** |

### D4 grid (30–45 DTE, 16–30Δ)

| Spec | Trades | Win | PF | Avg $ | Max DD $ | Worst month |
|---|---|---|---|---|---|---|
| bull_put 16Δ | 627 | 94.1% | 2.64 | +56 | 19,012 | 2025-03 (−11,971) |
| bull_put 30Δ | 628 | 86.0% | 1.63 | +69 | 32,097 | 2025-03 (−18,279) |
| iron_condor 16Δ | 627 | 76.1% | 1.14 | +20 | 30,547 | 2026-05 (−22,825) |
| iron_condor 20Δ | 625 | 68.6% | 1.05 | +9 | 36,826 | 2026-05 (−21,822) |
| iron_condor 30Δ | 622 | 53.7% | 0.97 | −8 | 44,803 | 2026-05 (−16,368) |
| bear_call 16Δ | 630 | 79.0% | 0.74 | −35 | 35,116 | 2026-05 (−25,638) |
| bull_call 20Δ | 628 | 45.7% | 1.35 | +76 | 44,604 | 2026-07 (−10,245) |
| bear_put 20Δ | 628 | 14.2% | 0.53 | −95 | 62,001 | 2026-07 (−6,609) |
| long_call 20Δ | 628 | 23.9% | 1.43 | +80 | 41,680 | 2026-07 (−7,456) |
| long_put 20Δ | 628 | 7.3% | 0.26 | −297 | 186,623 | 2025-05 (−14,770) |

For the credit structures (bull put, bear call, iron condor), a lower short Δ gave a
higher PF and a lower drawdown. Debit structures show no consistent Δ ordering. The full
28-spec table is in the run appendix.

### Regime-conditioned (D4, all deltas pooled)

| Structure | Bear | Sideways | Bull | Low vol | Mid vol | High vol |
|---|---|---|---|---|---|---|
| iron_condor PF | 0.23 | **1.21** | 0.72 | **1.76** | 0.83 | 0.36 |
| bull_put PF | 1.62 | 1.68 | 5.73 | 1.76 | 1.58 | 48 |
| bear_call PF | 0.19 | 0.91 | 0.36 | **1.43** | 0.60 | 0.17 |
| bull_call PF | 2.61 | 1.12 | 3.26 | 1.04 | 1.35 | 4.66 |
| long_call PF | 9.11 | 1.06 | 2.00 | 0.46 | 1.88 | 6.93 |

The trades split 83% sideways, 12% bull and 5% bear, so the bear cells rest on about 130
trades per structure. Most high-vol and bear-trend entries cluster around the
April 2025 drawdown and rebound, which makes those cells close to a single event.

### Cost sensitivity (D4 20Δ, avg $ per trade)

| x / est. spread | bull_put | iron_condor | bull_call | bear_call |
|---|---|---|---|---|
| 0 / 0% (mid, fees only) | +64 | +20 | +85 | −43 |
| 0.25 / 2% | +61 | +14 | +81 | −45 |
| **0.25 / 4% (default)** | **+57** | **+9** | **+76** | **−47** |
| 0.50 / 4% | +50 | −2 | +67 | −50 |
| 0.50 / 8% | +36 | −23 | +48 | −57 |

The condor is the most cost-sensitive structure, with four legs. Its edge at 20Δ
disappears at a touch fill. This is the QFX lesson (PLAN §7) in miniature.

## Caveats (read before using any number)

1. **No historical quotes.** Mid is a trade close that can be stale for illiquid strikes,
   and the spread is an estimate. Both bias fills. ThetaData EOD NBBO (E7.1 adapter,
   ~1 year free) should replace the estimate when a Theta Terminal is available; the
   engine already prefers quotes when present.
2. **One ticker, one bull market.** 2.5 years of SPY is a single regime path. There is no
   2022-style bear market in the sample.
3. **Overlapping 1-lots, no sizing.** Totals and drawdowns are not portfolio returns.
   The §5 gate limits (5% per underlying, max 8 positions) are not applied.
4. **European, cash-settled approximation.** SPY options are American and physically
   settled; early assignment on short ITM legs is ignored.
5. **Flat r, q = 0.** This slightly biases IV and delta, which matters most at 30Δ.
6. **Regime findings are in-sample and multiple-compared** (7 structures × 6 cells). Treat
   them as hypotheses.

## Recommended next steps (not done in this card)

- Finish the E7.1 backfill (`arc history download ...`) and rerun on the 20-ticker
  universe (§D9). The engine and CLI already take `--tickers`.
- Get ThetaData EOD quotes for the last year and rerun with quoted spreads, to check the
  4% estimate.
- Pre-register one regime rule, e.g. "iron condor only when trend = sideways and vol ≠
  high". Test it walk-forward with the rule fixed rather than selected. That is the fair
  follow-up to the in-sample regime split above.
- Wire `arc.features.regime` (E4.3) in once it merges, replacing
  `arc/backtest/regime.py`.
