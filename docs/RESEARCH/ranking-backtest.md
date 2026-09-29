# E7.5 — Ranking backtest: which candidate should the scanner pick?

Status: first run, 2026-09-28. Code: `arc/scanner/rank.py` (rankers),
`arc/backtest/ranking.py` + `arc/backtest/rank_report.py` (harness), config
`config/ranking.yaml`. Plan ref: D25 (switch rule), D23, D18.

Reproduce the primary run (~15 min on 6 workers):

    uv run arc backtest rank --profile margin --profile cash_debit \
        --from 2024-03-01 --to 2026-07-31 \
        --tickers SPY,QQQ,IWM,AAPL,NVDA,TSLA --workers 6 \
        --data-dir ~/GitHub/Project-Arc/data --out data/backtest/rank

The full generated report (every table, worst trades with root causes, cost grid,
equity charts) is [ranking-backtest-run.md](ranking-backtest-run.md). CSVs are
written next to `report.md` in `--out` (not committed, `data/` is gitignored).

## TL;DR

- **Recommendation: keep both incumbents.** `credit_width` stays the default for
  `margin` and `debit_width` for `cash_debit`. No challenger met the pre-registered
  D25 rule in the primary run or in any of the three variants below.
- **Closest challengers:**
  - `margin`: `rorc_day_vrp` won 2 of 3 sub-periods (bear, sideways), but its P&L
    edge is +$1.0k with a 90% CI of [−$21k, +$27k].
  - `cash_debit`: `managed_net_ev` won 2 of 3 (bear, bull), +$33k, CI [−$15k, +$86k].
    Its drawdown was also smaller: $53k against $87k for `debit_width`.
- **Ranking is not where the edge is.** Under the primary settings every
  `margin` ranker lost money (−$23k to −$35k on $100k over 29 months), and every
  `cash_debit` ranker was near flat or negative, apart from the two small positives
  (`managed_net_ev` +$5.7k, `rorc_day_vrp` +$12k on only 38 trades).
  - The losses come from **direction**: bear-trend bear calls and long puts.
  - They do not come from which strike or width was picked.
  - The spread between the best and worst ranker is smaller than the bootstrap
    noise.
- **The raw Alpaca EOD closes are unusable for marking debit spreads.** Closes are
  last trades printed at different times, and 13–65% of near-the-money strikes sat
  in a non-monotone pair on sampled sessions.
  - Marked on raw closes, `cash_debit` "earns" +$0.8M to +$2.0M on $100k (run D below).
  - That is the artifact, not a strategy. Example: a QQQ 428/420 bear put entered on
    stale prints showed +555% of its risk in one day.
  - The harness therefore re-marks every leg from a same-session fitted IV smile by
    default (`backtest.marks: smile`).

## Method (what was held fixed)

The only variable between runs is the ranker. Everything else is shared:

| | |
|---|---|
| Universe | SPY, QQQ, IWM (ETFs) + AAPL, NVDA, TSLA. AMZN and AMD were still downloading; the D9 names are not run yet. |
| Decisions | Daily, at the close, 2024-03-01 → 2026-07-31. 606 sessions per ticker, top-1 per ticker per day. |
| Menu | For each profile: every structure kind × a delta grid (`config/ranking.yaml` → `backtest.menus`), on the one expiration nearest the middle of the profile's entry DTE window (margin 30–45, cash_debit 30–60), with 2%-of-spot verticals. |
| Stance | Director proxy: the 20-session trend label at the decision close selects the profile's `stance_strategies` (bull → bullish, bear → bearish, sideways → neutral). `cash_debit` has no neutral structure, so it does not trade sideways sessions. |
| Hard filters | Managed Net EV > 0 (E2.4 Monte Carlo, 5,000 paths, fixed seed) and PoP ≥ 0. The same filters apply to every ranker. |
| No look-ahead | IV, delta and smile come from that session's chain; the realised-vol forecast is mean(HV20, HV60) of closes ≤ t; the trend label uses closes ≤ t. The future is only read after the pick, to play the trade out. A test changes every future close ×3 and asserts the menu is identical. |
| Sizing | D18: floor(5% × current equity / max loss). There is no Risk persona, so the cap binds. |
| Portfolio caps | The pure gate rules `check_per_underlying` and `check_max_open_positions` from `arc/gate/rules.py`; for cash profiles, settled cash must cover the debit + fees. |
| Costs | `config/costs.yaml` (E6.1a): fill at mid ± 0.25 × est. spread, spread = max($0.03, 4% of mid), plus fees. Grid: 0 / 0.25 / 0.5. |
| Exits | `config/exits.yaml` (E2.4, relaxed stops) checked on EOD marks; expiry settles on the underlying close. |
| Decision rule | Fixed before the run: switch only if the challenger beats the incumbent on net P&L **and** max drawdown in ≥ 2 of 3 trend sub-periods at entry, **and** the 90% moving-block (20-day) bootstrap CI of the daily P&L difference is > 0. |

## Rankers

| Ranker | Orders by | Profiles |
|---|---|---|
| `credit_width` | credit ÷ widest wing, `ev_proxy` tie-break (live scanner default) | margin (incumbent) |
| `debit_width` | payoff width ÷ debit; single legs after verticals | cash_debit (incumbent) |
| `ev_proxy` | static hold-to-expiry EV after entry costs, $ | both |
| `managed_net_ev` | E2.4 managed-exit Net EV after all costs, $ | both |
| `rorc_day` | managed Net EV ÷ (max loss × expected days held) | both |
| `rorc_day_vrp` | `rorc_day`, but skip if ATM IV − RV forecast ≤ `vrp_threshold` (0) | both |

## Primary results (run A: 6 tickers, smile marks, trend stance, x = 0.25)

| Profile | Ranker | Trades | Net P&L | Max DD | Sharpe | Win % | Mean PoP |
|---|---|---|---|---|---|---|---|
| margin | **credit_width** | 135 | −24,368 | 42,949 | −0.57 | 73.3 | 76.2 |
| margin | ev_proxy | 140 | −34,558 | 40,107 | −1.19 | 75.0 | 81.1 |
| margin | managed_net_ev | 133 | −31,352 | 39,729 | −0.90 | 72.2 | 79.4 |
| margin | rorc_day | 135 | −29,115 | 41,364 | −0.85 | 73.3 | 79.3 |
| margin | rorc_day_vrp | 133 | −23,371 | 37,015 | −0.67 | 74.4 | 79.3 |
| cash_debit | **debit_width** | 118 | −27,608 | 87,340 | −0.08 | 42.4 | 47.6 |
| cash_debit | ev_proxy | 113 | −9,881 | 68,902 | 0.08 | 41.6 | 46.7 |
| cash_debit | managed_net_ev | 109 | 5,679 | 53,207 | 0.24 | 43.1 | 45.8 |
| cash_debit | rorc_day | 121 | −9,167 | 87,249 | 0.13 | 43.0 | 46.9 |
| cash_debit | rorc_day_vrp | 38 | 11,988 | 29,118 | 0.30 | 47.4 | 45.1 |

**Calibration.** Win rates sit a few points below the modelled managed PoP for
both profiles, so the PoP model is roughly calibrated, slightly optimistic. The
modelled Net EV is too optimistic for credit structures: `margin` modelled
+$13–16 per unit against realised −$8 to −$28.

**Challengers against the incumbent:**

| Profile | Challenger | Sub-periods won | P&L diff | 90% CI | Switch |
|---|---|---|---|---|---|
| margin | ev_proxy | 0 | −10,190 | [−38,205, 20,014] | no |
| margin | managed_net_ev | 1 (bear) | −6,984 | [−31,767, 18,704] | no |
| margin | rorc_day | 1 (bear) | −4,747 | [−27,996, 20,107] | no |
| margin | rorc_day_vrp | 2 (bear, sideways) | +996 | [−21,222, 26,717] | no (CI) |
| cash_debit | ev_proxy | 2 (bear, bull) | +17,726 | [−17,138, 53,527] | no (CI) |
| cash_debit | managed_net_ev | 2 (bear, bull) | +33,287 | [−14,708, 85,997] | no (CI) |
| cash_debit | rorc_day | 0 | +18,440 | [−23,581, 61,711] | no |
| cash_debit | rorc_day_vrp | 1 (bear) | +39,595 | [−51,780, 137,003] | no |

**Where the money went.** The by-structure breakdown in the run report shows the
same pattern for every ranker:

- `margin`: bear-trend bear calls lost about $24–28k whichever ranker picked them.
  Sideways condors were about flat, and bull puts lost a few $k.
- `cash_debit`: bull-trend long calls made +$12k to +$41k, and bear-trend long puts
  and bear puts gave most of it back.
- The trend proxy's bear calls are wrong more often than its bull calls. That is a
  **stance** problem, which sits upstream of ranking.
- Worst-10 root causes (E7.4 vocabulary, heuristic):
  - `margin`: mostly `regime_misread` (a move of ≥ 1.5 implied σ over the hold).
  - `cash_debit`: mostly `exit_management`, meaning the stop fired on a smaller move.
    That is consistent with debit spreads losing most of their value before a
    rebound.

## Robustness

Each variant changes one thing. The verdicts come from each run's own decision
table.

| Run | Change | margin verdict (closest) | cash_debit verdict (closest) |
|---|---|---|---|
| A | primary | keep; `rorc_day_vrp` 2/3, CI [−21k, 27k] | keep; `managed_net_ev` 2/3, CI [−15k, 86k] |
| B | 4 tickers (drop NVDA, TSLA) | keep; `rorc_day_vrp` 2/3, CI [−19k, 26k] | keep; `managed_net_ev` 1/3, CI [−22k, 27k] |
| C | 4 tickers, **no stance filter** (ranker also picks direction) | keep; `managed_net_ev` 2/3, CI [−1k, 65k] | keep; `rorc_day_vrp` 2/3, CI [−29k, 65k] |
| D | 6 tickers, **raw closes** (IV-outlier filter only) | keep; `rorc_day_vrp` 2/3, CI [−13k, 112k] | keep; `rorc_day` 1/3, CI [−373k, 495k]. P&L is an artifact: +$0.8M to +$2.0M |

**Cost grid, run A.** The ranking order changes between x = 0, 0.25 and 0.5.
For example, `margin` `rorc_day_vrp` is +$21.9k at x = 0 but −$23.4k at x = 0.25,
and `cash_debit` `ev_proxy` is −$17.6k at x = 0 but +$9.4k at x = 0.5. A real edge
would survive the cost grid in order, and this doesn't. It is consistent with the
bootstrap: the differences are noise.

For `cash_debit`, `managed_net_ev` and `rorc_day_vrp` had a smaller max drawdown
than `debit_width` in all 3 cost settings. That is the most consistent signal in
the data, but it does not pass the rule.

## Data caveats (read before trusting any number)

1. **No historical quotes.** Alpaca options history is trade bars only.
   - The spread is *estimated* as max($0.03, 4% × mid) (E7.1 finding).
   - Costs and the whole cost grid are therefore modelled, not observed.
   - ThetaData EOD is not in the store, so it was not used.
2. **Stale closes.** Legs are re-marked from a same-session smile: a
   volume-weighted quadratic in log-moneyness over OTM IVs, with a 3-MAD trim and
   no extrapolation.
   - This removes the parity violations, as test
     `test_smile_marks_repair_a_stale_close_and_stay_monotone` shows.
   - It also removes any real skew kink a quadratic cannot follow.
   - It prices every leg at the fitted IV, so ranking on "cheap vs the smile" is
     not possible in this data.
3. **EOD only.** Stops and take-profits fire on closes (the relaxed, end-of-day
   evaluation the owner prefers). Intraday paths are not seen.
4. **Simplified menu.** One expiry and a delta grid, not the full live scanner
   menu. Contracts with no trade that session are absent.
5. **Director proxy.** The trend label stands in for the live Director's stance.
   The losses above say more about that proxy than about ranking.
6. **Universe.** Six names over about 29 months, with the three trend sub-periods
   unevenly populated. `cash_debit` has 0 sideways trades by construction, so its
   rule is effectively "win both bear and bull".

## What would change the answer

- Real quotes (ThetaData or OPRA NBBO EOD) instead of estimated spreads and
  smile-fitted marks. This is the largest uncertainty.
- Better stance. Rerun once the Director is backtestable (E7.x) with
  `backtest.stance` fed by it.
- The full D9 universe after AMZN, AMD and the rest are downloaded:
  `--tickers` defaults to `config/ranking.yaml` → `backtest.tickers`.

The defaults are not changed by this card: the scanner's `rank_by` and exits.yaml's
`pipeline.rank_menu_by` are untouched. The owner flips them if a later run passes
the rule.
