# E7.5b · Unified menu measure + stance-tilted ranking: walk-forward backtest

Status: run 2026-10-10 on main 034073c + this card (commit 6ac56ce). Code:
`arc/scanner/menu.py` (live ranker), `arc/scanner/rank.py` (`*_tilted` rankers,
`tilted_drift`), `arc/exits/model.py` (`drift=`), `arc/backtest/ranking.py`
(`expiry_mode: all`, `direction_tilt`, trend-proxy hit rate). Overlay:
`config/experiments/e75b_unified_measure.yaml`. Plan ref: D79 (D25, D41, D44, D76).

Reproduce (~53 min on 6 workers, 12 CPUs; mean menu 8.5 / 8.9 candidates vs E7.5's 3.7 / 2.9):

    uv run arc backtest rank --profile margin --profile cash_debit \
        --from 2024-03-01 --to 2026-07-31 --tickers SPY,QQQ,IWM,AAPL,NVDA,TSLA \
        --workers 6 --data-dir ~/GitHub/Project-Arc/data --offline \
        --experiment config/experiments/e75b_unified_measure.yaml \
        --out data/backtest/e75b/tilt025

The descriptive tilt-0.10 point adds a second overlay with `backtest.direction_tilt:
0.10` and `ranking.rankers: [managed_net_ev_tilted, rorc_day_tilted]`.

## TL;DR

- **Verdict (pre-declared D25 rule, tilt 0.25 fixed before the run): keep both
  incumbents.** No challenger wins ≥ 2 of 3 sub-periods on net P&L **and** max
  drawdown, and no 90 % bootstrap CI of the daily P&L difference excludes 0.
  - `cash_debit`: keep `debit_width`. Closest: **`managed_net_ev`**, +$33.4k net P&L
    (−$22.9k → +$10.4k), max DD $56.8k → $47.2k, CI **[−$1.1k, +$82.1k]**. It won the
    bear sub-period. In the bull sub-period P&L nearly doubled (+$32.1k → +$60.1k)
    but its drawdown was $2.7k larger, so the rule scores it as a loss there. This
    repeats E7.5 (+$33k, CI [−14.7k, +86k]) on the live-shaped menu, with a CI now only
    $1.1k short of excluding 0.
  - `margin`: keep `credit_width`. Closest: `rorc_day`, +$2.9k, CI [−$12.9k, +$17.6k].
- **The tilt adds nothing measurable.** `managed_net_ev_tilted` ≈ `managed_net_ev`
  (+$33.9k vs +$33.4k, wider CI [−$7.6k, +$88.6k]). `rorc_day_tilted` beats
  `rorc_day` by $5.2k but wins 0 sub-periods. On `margin` the tilt does slightly
  worse than untilted (−$3.0k vs −$0.7k for `managed_net_ev`).
- **Why the tilt cannot help much here:** the backtest's stance is the 20-session
  trend proxy, and it was right only **51.6 %** of the time over the next 15 sessions
  (SPY 68 %, QQQ 69 %, IWM 51 %, AAPL 48 %, NVDA 46 %, TSLA 49 %). A drift in the
  stance's direction helps only when the stance is right more than half the time.
  Research's live stance quality is not tested here. XP-14's t2 arm is the only way to
  measure the tilt under the real Research stance.
- **The unified measure changes which kinds get picked.** On `cash_debit`,
  `managed_net_ev` drops `bull_call` entirely (23 → 0 trades, −$13.1k avoided) and
  halves `bear_put` (50 → 24, −$42.6k → −$20.5k). It moves the book to `long_call`
  (90 → 97, +$45.2k → +$60.1k) and `long_put`. That is the cross-kind comparison
  the split credit/width vs EV/max-loss sort never makes.
- **No default changes.** Ship flag-off (`exits.pipeline.menu_measure: control`). The
  draft **XP-14** (t1 `rorc_day_full`, t2 `rorc_day_tilted` @ 0.25, t3
  `managed_net_ev_full`) is the next step if the owner wants a forward test. Per this
  report, t3 is the arm to watch.

## Decision rule (fixed before the run, D25)

A challenger replaces the incumbent (`debit_width` for cash_debit, `credit_width` for
margin) only if it beats it on net P&L (higher) **and** max drawdown (not larger) in
≥ 2 of 3 sub-periods (bear / sideways / bull trend at entry), **and** the 90 %
block-bootstrap CI of the daily P&L difference excludes 0. The tilt verdict uses
`direction_tilt = 0.25` (a quarter of a 1σ move over the expected hold), fixed in the
card before the run. The 0.10 grid point is descriptive only.

## Setup

- 6 tickers (SPY, QQQ, IWM, AAPL, NVDA, TSLA), 606 decision sessions each,
  2024-03-01 → 2026-07-31, $100k, MC 5,000 paths, cost x = 0.25 of the spread, exit
  policy v2 (E18.1).
- **Live-shaped menu** (`expiry_mode: all`): every expiration in the profile's DTE
  window (margin 30–45, cash_debit 30–60) at the scanner's delta bands (credit short
  16–30 Δ, debit short 20–35 Δ, long 40–70 Δ). Mean menu: margin 8.5, cash_debit 8.9
  candidates per session after the stance filter and liquidity. The E7.5 run had one
  expiration nearest the window middle; that remains the default
  (`config/ranking.yaml`), so the E7.5 numbers reproduce without this overlay.
- Stance = the 20-session trend label at the decision close (bull → bullish, bear →
  bearish, sideways → neutral), which picks the profile's `stance_strategies`. The
  `*_tilted` rankers read the same stance to sign their drift.
- Tilt mechanics (same code as live, `arc.scanner.rank.tilted_drift`): `mu = r +
  sign(stance) × tilt × σ_path / √T_hold`, on directional kinds only (verticals, long
  call/put). Condors and neutral stances stay at `r`. Only the MC paths move; marks
  are still priced at `r`. The tilted Net EV / rorc_day is a ranking key only. Every
  stored number, the D41 floor and the gate see the untilted model.

## Results (default costs, x = 0.25)

| profile | ranker | trades | net P&L | max DD | Sharpe | P&L diff vs incumbent | 90 % CI | sub-periods won |
|---|---|---|---|---|---|---|---|---|
| margin | **credit_width** (incumbent) | 148 | −11,256 | 42,748 | −0.18 | – | – | – |
| margin | managed_net_ev | 145 | −11,978 | 29,455 | −0.25 | −722 | [−29,190, 25,984] | 1 (bear) |
| margin | rorc_day | 148 | −8,313 | 36,147 | −0.14 | +2,942 | [−12,921, 17,597] | 1 (bear) |
| margin | managed_net_ev_tilted | 147 | −14,261 | 29,353 | −0.32 | −3,006 | [−28,975, 22,576] | 1 (bear) |
| margin | rorc_day_tilted | 152 | −11,974 | 39,594 | −0.23 | −718 | [−16,965, 13,587] | 1 (sideways) |
| cash_debit | **debit_width** (incumbent) | 181 | −22,937 | 56,821 | −0.01 | – | – | – |
| cash_debit | managed_net_ev | 154 | 10,442 | 47,246 | 0.29 | +33,378 | [−1,108, 82,087] | 1 (bear) |
| cash_debit | rorc_day | 196 | −2,661 | 55,916 | 0.21 | +20,276 | [−22,801, 61,113] | 0 |
| cash_debit | managed_net_ev_tilted | 142 | 10,928 | 50,708 | 0.29 | +33,865 | [−7,611, 88,554] | 1 (bear) |
| cash_debit | rorc_day_tilted | 216 | 2,552 | 52,094 | 0.27 | +25,488 | [−17,909, 66,368] | 0 |

cash_debit sub-periods (cash_debit has no neutral structure, so sideways = 0 trades):

| ranker | bear P&L / DD | bull P&L / DD |
|---|---|---|
| debit_width | −55,006 / 55,606 | +32,070 / 27,568 |
| managed_net_ev | −49,630 / 55,510 | +60,072 / 30,237 |
| managed_net_ev_tilted | −51,503 / 55,005 | +62,431 / 34,879 |
| rorc_day_tilted | −54,484 / 62,210 | +57,036 / 29,094 |

Cost sensitivity: at x = 0.5, `managed_net_ev` stays ahead of `debit_width` (+$4.4k vs
−$13.4k), but `managed_net_ev_tilted` turns negative (−$8.8k). At x = 0 every cash_debit
ranker loses except `rorc_day` (+$23.7k), whose drawdown is the largest ($102k). The
tilt has no ranking that is robust across costs.

TILT010_PLACEHOLDER

## Live wiring: what the measures do to a real menu

Recorded NVDA chain (`arc/data/fixtures/nvda_chain.json`, as of 2026-09-27),
`--stance bullish --top 5`, profile `cash_debit`:

```
--rank-by scanner
  1  long_call       2026-11-06  38d  225     ev/L -0.193  PoP 0.30  EV -237.84  maxL 1234.00
  2  long_call       2026-11-06  38d  235     ev/L -0.198  PoP 0.24  EV -142.91  maxL  721.50
  3  long_call       2026-11-06  38d  230     ev/L -0.203  PoP 0.27  EV -194.58  maxL  960.00
  4  long_call       2026-10-30  31d  230     ev/L -0.205  PoP 0.27  EV -170.55  maxL  831.50
  5  long_call       2026-10-30  31d  225     ev/L -0.207  PoP 0.30  EV -230.10  maxL 1113.50
--rank-by rorc_day_full                                   (key = rorc_day)
  1  long_call       2026-11-06  38d  225                                              key -0.012687
  2  bull_call_debit 2026-10-30  31d  225/240  PoP 0.37  EV -145.96  maxL 680.50      key -0.014409
  3  long_call       2026-11-06  38d  230                                              key -0.015321
  4  long_call       2026-10-30  31d  225                                              key -0.015901
  5  bull_call_debit 2026-10-30  31d  230/240  PoP 0.34  EV  -86.41  maxL 398.50      key -0.016489
--rank-by rorc_day_tilted --tilt 0.25                     (key = tilted rorc_day)
  1  long_call 2026-11-06 225 · 2 long_call 2026-10-30 230 · 3 long_call 2026-11-06 235
  4  long_call 2026-10-30 225 · 5 long_call 2026-11-06 230   keys -0.00145 … -0.00237
```

**2 of the `rorc_day_full` top 5 were outside the scanner's top 5**: both bull call
debit spreads, which the scanner's EV/max-loss key ranks below every long call.
`rorc_day_tilted` keeps the scanner's five long calls in a different order. The up
drift favours the higher-delta, uncapped calls over the capped spreads. On `margin`
(bullish → bull puts) the pool holds only 2 candidates, so all three measures keep
the same two, and the tilt swaps their order.

A live run on Alpaca's NVDA chain was attempted on 2026-10-10 (a Saturday). Off-RTH
every quote is flagged stale (`liquid 0`, rejected `stale_quote` 278, `zero_bid` 80,
`missing_greeks` 130), so the comparison was run on the recorded chain above. The
same three commands on a live RTH chain are listed in the PR as owed.

## Cost of the full-pool measure (perf budget)

The managed model runs on up to `menu_pool_max` = 20 candidates per ticker instead of
the `pipeline_scan_top` = 5 survivors (`exits.model.n_paths` = 20,000, unchanged), on
12 CPUs (estimate below is labelled as such):

| profile / ticker / stance | pool | untilted models | + tilted re-run | per ticker |
|---|---|---|---|---|
| cash_debit NVDA bullish | 9 | 164 ms | +163 ms | 0.33 s |
| cash_debit SPY bearish | 9 | 171 ms | +163 ms | 0.33 s |
| margin SPY neutral (condors) | 9 | 513 ms | +0 (never tilted) | 0.51 s |

Recorded chains hold ≤ 9 candidates per stance. A 20-candidate live pool is (estimate, linear in pool size) about
2.2x that: ≤ 0.75 s per ticker for debit structures with the tilt, ≤ 1.15 s for condors.
A 10-ticker Research chain therefore grows by ≤ ~12 s, below the card's 30 s budget,
so the ranking keeps `exits.model.n_paths` (no separate ranking path count). Each
pipeline run logs `pipeline.menu_measure … rank_ms=` per ticker so the live cost can
be checked once a treatment arm runs.

## Caveats

- Same data caveats as E7.5: Alpaca options history has no historical quotes, legs
  are re-marked from a fitted same-session smile, the spread is modelled as max(0.03,
  0.04·mid), decisions and marks are EOD only.
- The backtest stance is a trend proxy with a ~52 % forward hit rate. The tilted
  rankers' value depends on stance accuracy, so this run is close to a null test of
  the tilt itself. It shows the tilt does not *hurt* much at 0.25. It cannot show
  whether it helps under the live Research stance.
- The live-shaped menu uses each band's anchor deltas (4 for credit and debit
  shorts, 3 for longs), not every strike the live scanner tries, and contracts that
  did not trade that session are missing. The live pool is larger, so live
  differences between measures should be at least as large as here.
- `managed_net_ev` on `cash_debit` is now the same near-miss twice (E7.5 one-expiry
  menu, E7.5b live-shaped menu). The forward test (XP-14 t3) is the natural next
  step; this report does not change any default.
