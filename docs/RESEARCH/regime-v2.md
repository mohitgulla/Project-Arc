# E17.3 · Regime v2 vs v1 on the E7.5 harness (D77)

Status: run 2026-10-10 on main `b4110cf` + this card. Report only: nothing here changes a
live default. Plan ref: D77 (regime v2, transitional guard), E7.5 / E7.5a (harness, regime
menu), E7.2 (baseline conditioning table). Raw tables: [`regime-v2/`](regime-v2/).

## TL;DR

- **Verdict (pre-declared rule): roll back to v1.** v2 does better on `margin`: net
  P&L −$16.7k → −$0.3k, max DD $42.7k → $38.7k, CI of the daily P&L difference
  [−$2.5k, +$39.8k]. It does worse on `cash_debit` on **both** counts: net P&L −$11.3k →
  −$18.0k, max DD $61.2k → $68.7k, CI [−$65.2k, +$44.6k]. The rule says roll back if v2 is
  worse on both P&L and drawdown in either profile.
- **How strong the evidence is:** weak. Both CIs straddle 0 by a wide margin, so neither
  profile's difference is distinguishable from noise. The rule was fixed before the run,
  and on this data it gives *roll back*. Flipping back is the existing switch:
  `regime.model: v1` (registry, no PR). The owner decides.
- **Why cash_debit loses under v2:** v2 calls far more sessions `sideways` (cash_debit
  has no neutral structure, so it sits out). On NVDA and TSLA, v2 labels 451/606 and
  426/606 sessions sideways, against 223 and 198 for v1. cash_debit's incumbent trades
  fall from 147 to 116. It keeps most of the bull trades (93 vs 105; P&L +$15.0k vs
  +$17.1k) but loses more on fewer bear trades (23 trades −$33.0k vs 42 trades −$28.4k).
- **Vol-gated condor menu (candidate, not wired in):** on margin under v2, allowing the
  iron condor only in low/mid vol takes the incumbent from −$332 to +$842 (DD $38.7k →
  $36.1k), with CI [−$13.4k, +$14.6k]. It wins 1 of 3 sub-periods, so the D25 rule says no
  switch. `ev_proxy` gains the most (+$11.2k, CI [+$0.6k, +$24.4k]), but it also wins only
  1 sub-period.
- **Transitional guard (reference setting run 3 / margin z 0.10): keep off.** It would have
  blocked 169 of 606 sessions (27.9 %). On `margin`, trades opened on blocked days do
  slightly worse (mean −$50 vs +$18 per trade), but the CI of the difference
  [−$696, +$715] straddles 0. On `cash_debit`, blocked-day trades did **better** (mean
  +$170 vs −$459). Skipping those days would have forgone +$9.5k on cash_debit and saved
  $1.5k on margin.
- **Baseline conditioning (E7.2 table, SPY, all D4 specs):** v2's vol axis separates
  condors more sharply than v1's fixed buckets: iron-condor PF 2.56 in low vol vs 0.71 in
  high vol (v1 1.79 vs 0.37). v2's trend axis does **not**: the condor PF is 1.13 in
  sideways vs 1.40 in bull under v2, against 1.23 vs 0.73 under v1.

## Decision rules (fixed before the run, copied from the card)

**Regime model.** Use the incumbent ranker (margin `credit_width`, cash_debit
`debit_width`). Keep v2 if, in both profiles, v2 is not worse than v1 on net P&L **and**
max drawdown, **and** the upper bound of the 90 % block-bootstrap CI of the daily P&L
difference (v2 − v1) is > 0. Roll back to v1 (`regime.model: v1`) if v2 is worse on both
net P&L and drawdown in either profile. Otherwise keep v2, inconclusive.

**Transitional guard.** Turn it on (`regime.guard_min_run` 3 /
`regime.guard_min_margin_z` 0.10 via `arc config set`) only if, in both profiles,
blocked-day trades have a lower mean net P&L than the rest **and** the upper bound of
the CI of (blocked − rest) is < 0. Otherwise keep it off.

**Vol gate.** Reported as a candidate, using the E7.5a D25 rule against the v2 run (it is
not part of the keep/rollback decision).

## Method

- **Harness:** E7.5 `arc backtest rank` with the E7.5a (a) regime-conditional menu
  (`config/experiments/e75a_a_regime_menu.yaml`: margin opens iron condors only in
  `sideways`; cash_debit opens bull → `bull_call`/`long_call`, bear → `long_put`, sideways
  → nothing). The tickers, dates, costs (x = 0.25), 5,000 MC paths, D18 sizing, exits and
  bootstrap (2,000 resamples, 20-day blocks, seed 7) are the same as
  [ranking-backtest-run.md](ranking-backtest-run.md) and
  [ranking-backtest-experiments.md](ranking-backtest-experiments.md).
- **The one variable:** `backtest.regime_model: v1 | v2`
  (`config/experiments/e173_regime_v2.yaml`). It decides the trend label behind the stance
  menu, the sub-period split and the `trend`/`vol` columns on each trade.
  - v1 = the E4.3 rule (20-session return ±5 %) plus fixed 12 %/20 % rv20 buckets.
  - v2 = `arc.features.regime`'s live labeller: the vol-scaled 20-day z (r20 / (σ60·√20),
    bull ≥ +1, bear ≤ −1), plus each ticker's rv20 percentile within its own 252 sessions
    (fixed buckets during warm-up).
  - Both use only closes ≤ the decision day (`tests/test_backtest_regime.py` pins that).
- **Label history:** both arms label on **split-adjusted** daily closes from 490 days
  before the first entry (`--label-history-days 490 --label-adjusted`). That gives v2 a
  full 252-session vol-percentile window from day one, as the live `regime` step does.
  - It also keeps NVDA's 10:1 split (2024-06-10) from reading as a −90 % crash: on raw
    closes, v1 labels NVDA `bear` for 20 sessions (2024-06-10 → 07-09) where adjusted
    closes say `bull`.
  - Strikes, marks and settlement still use the raw closes. Apart from those 20 NVDA days,
    the v1 labels are identical to the E7.5 run's.
- **Vol gate:** `config/experiments/e173_vol_gate.yaml` (`backtest.vol_gate`). On top of
  the v2 run, margin's `iron_condor` stays on the menu only when the v2 vol state is `low`
  or `mid`.
- **Guard:** E17.2's `transitional_reason` (`arc.pipeline.market_guard`) runs per decision
  day on SPY v2 labels (run length of equal consecutive labels, margin z to the nearest
  ±1 threshold), built the same way as the 2-year replay fixture test, at the named
  reference setting run 3 / margin z 0.10. The incumbent's v2-run trades are split by
  whether their entry day would have been blocked. The CI of (blocked mean − rest mean)
  is a moving block bootstrap over **entry days** (20-day blocks, 2,000 resamples, seed 7),
  carrying every trade opened that day, so same-day trades stay correlated.
- **Baseline table:** `arc backtest --tickers SPY --start 2024-02-01 --end 2026-08-07`
  (the E7.2 run), with `--regime-model v1|v2` and the same label history.

### Reproduce

    D=<data dir with options_eod/, underlying_daily/, underlying_ohlc/>
    A="backtest rank --profile margin --profile cash_debit --from 2024-03-01 --to 2026-07-31 \
       --tickers SPY,QQQ,IWM,AAPL,NVDA,TSLA --workers 4 --no-charts --data-dir $D \
       --label-history-days 490 --label-adjusted \
       --experiment config/experiments/e75a_a_regime_menu.yaml"
    uv run arc $A --regime-model v1 --out <out>/run_v1
    uv run arc $A --experiment config/experiments/e173_regime_v2.yaml --out <out>/run_v2
    uv run arc $A --experiment config/experiments/e173_regime_v2.yaml \
        --experiment config/experiments/e173_vol_gate.yaml --out <out>/run_v2_gate
    uv run python scripts/e173_regime_report.py --v1 <out>/run_v1 --v2 <out>/run_v2 \
        --gate <out>/run_v2_gate --data-dir $D --out <out>/compare
    # baseline conditioning table
    uv run arc backtest --tickers SPY --start 2024-02-01 --end 2026-08-07 --data-dir $D \
        --train-months 6 --test-months 3 --no-sensitivity \
        --label-history-days 490 --label-adjusted --regime-model v2 --out <out>/base_v2

Add `--offline` once `underlying_ohlc/` covers 490 days before 2024-03-01. Without it, the
first run fetches split-adjusted bars from Alpaca. The three ranking runs took ~70 min in
parallel on 12 CPUs.

## Data caveats (from the E7.5 run)

- Alpaca options history has **no historical quotes**: `mid` is the session's last trade
  close, and the bid/ask spread is *estimated* as max(0.03, 0.04·mid). Costs, slippage and
  the cost-sensitivity grid are therefore modelled, not observed.
- A trade close can be hours stale, so every leg is **re-marked from a same-session fitted
  IV smile** (volume-weighted quadratic in log-moneyness, 3-MAD trim, no extrapolation)
  for entries, daily marks and early exits. Expiry settles on the underlying close. This
  removes stale-close noise but also any real skew kinks.
- **Stance** is a deterministic Research proxy (the trend label at the decision close
  picks the profile's structures). The live Research persona uses more information, and
  its stance quality is outside this test.
- Daily EOD decisions and marks only: stops and take-profits are checked on closes.
- Menus hold one expiration and a fixed delta grid, not the full live scanner menu.
- At most one new position per ticker per session, sized by D18 with no Risk persona.
- **Specific to this card:**
  - 6 tickers over 2.4 years is a small sample: margin trades ~93 and cash_debit ~130
    times per arm, and every CI below is wide.
  - The cash_debit v1 baseline here (147 trades, −$11.3k) differs from the E7.5a (a) row
    (104 trades, −$21.4k). Margin reproduces that row exactly (92 trades, −$16,654). The
    likeliest cause is E18.1's exit policy v2 (debit take profit 0.60, D78), which merged
    after E7.5a and changes only debit exits. Both arms here share today's code, so the
    v1 vs v2 comparison is like for like.

## Results: v1 vs v2 labels (incumbent rankers)

Net P&L, max DD and the P&L difference in $ at the default cost x = 0.25. Difference =
v2 − v1, with a 90 % block-bootstrap CI of the summed daily equity-change difference.

| Profile / incumbent | Trades v1 → v2 | Net P&L v1 → v2 | Max DD v1 → v2 | Sharpe v1 → v2 | P&L diff | 90 % CI | P&L not worse | DD not worse |
|---|---|---|---|---|---|---|---|---|
| margin / credit_width | 92 → 93 | −16,654 → **−332** | 42,715 → **38,675** | −0.33 → 0.08 | +16,321 | [−2,543, +39,807] | yes | yes |
| cash_debit / debit_width | 147 → 116 | −11,274 → −17,986 | 61,205 → 68,697 | 0.04 → −0.09 | −6,712 | [−65,175, +44,575] | **no** | **no** |

**Verdict: roll back to v1.** v2 is worse on both net P&L and max drawdown for
`cash_debit`. On `margin`, v2 passes every keep condition.

### Every ranker (context; the verdict uses the incumbents)

| Profile | Ranker | Trades v1 → v2 | Net P&L v1 → v2 | Max DD v1 → v2 | P&L diff | 90 % CI |
|---|---|---|---|---|---|---|
| margin | credit_width | 92 → 93 | −16,654 → −332 | 42,715 → 38,675 | +16,321 | [−2,543, +39,807] |
| margin | ev_proxy | 103 → 102 | −9,562 → −10,496 | 33,295 → 31,964 | −934 | [−12,579, +10,324] |
| margin | managed_net_ev | 97 → 98 | −6,988 → +1,558 | 32,420 → 33,046 | +8,546 | [−7,033, +25,245] |
| margin | rorc_day | 95 → 98 | −2,595 → +6,305 | 34,550 → 33,837 | +8,900 | [−7,465, +26,423] |
| margin | rorc_day_vrp | 95 → 98 | −2,595 → +6,305 | 34,550 → 33,837 | +8,900 | [−7,465, +26,423] |
| cash_debit | debit_width | 147 → 116 | −11,274 → −17,986 | 61,205 → 68,697 | −6,712 | [−65,175, +44,575] |
| cash_debit | ev_proxy | 148 → 115 | +7,461 → −18,216 | 70,151 → 73,623 | −25,677 | [−90,420, +34,701] |
| cash_debit | managed_net_ev | 138 → 110 | −6,128 → −19,969 | 48,025 → 59,871 | −13,841 | [−71,519, +38,847] |
| cash_debit | rorc_day | 156 → 117 | −6,341 → −12,869 | 66,683 → 66,604 | −6,528 | [−63,098, +48,637] |
| cash_debit | rorc_day_vrp | 47 → 50 | +24,286 → +7,351 | 39,409 → 42,459 | −16,935 | [−53,328, +14,318] |

The pattern holds across rankers. v2 helps every margin ranker but one (`ev_proxy`, −$0.9k)
and hurts every cash_debit ranker. Margin trades only in `sideways` under this menu, so v2
helps margin by **choosing better sideways days** (trade count is about unchanged). It
hurts cash_debit by **taking away directional days**: it labels more sessions sideways,
and cash_debit sits those out.

### Incumbent trades by trend label at entry

| Profile | Arm | Bear trades / P&L | Sideways trades / P&L | Bull trades / P&L |
|---|---|---|---|---|
| margin | v1 | – | 92 / −16,654 | – |
| margin | v2 | – | 93 / −332 | – |
| cash_debit | v1 | 42 / −28,367 | – | 105 / +17,093 |
| cash_debit | v2 | 23 / −33,004 | – | 93 / +15,019 |

## Label flips: how often v2 disagrees with v1

Over the 606 decision sessions per ticker (2024-03-01 → 2026-07-31), counting days where
both labels are known (all of them, with the 490-day label history).

| Ticker | Trend flips | Trend flip % | Vol flips | Vol flip % | v1 bear / side / bull | v2 bear / side / bull |
|---|---|---|---|---|---|---|
| SPY | 119 | 19.6 % | 273 | 45.0 % | 33 / 506 / 67 | 45 / 433 / 128 |
| QQQ | 68 | 11.2 % | 182 | 30.0 % | 47 / 428 / 131 | 35 / 442 / 129 |
| IWM | 55 | 9.1 % | 283 | 46.7 % | 74 / 388 / 144 | 60 / 439 / 107 |
| AAPL | 104 | 17.2 % | 296 | 48.8 % | 116 / 266 / 224 | 68 / 366 / 172 |
| NVDA | 228 | 37.6 % | 347 | 57.3 % | 132 / 223 / 251 | 18 / 451 / 137 |
| TSLA | 228 | 37.6 % | 397 | 65.5 % | 190 / 198 / 218 | 62 / 426 / 118 |

The vol-scaled threshold works as designed. On high-vol names (NVDA, TSLA) a ±5 % 20-day
move is ordinary, so v2 calls most of those days sideways. On low-vol SPY, v2 calls more
days directional than v1 (bull 128 vs 67). The vol state disagrees on 30–66 % of days,
because a per-ticker percentile puts TSLA's ordinary vol in `mid`, where fixed buckets put
it in `high`.

## Vol-gated condor menu (candidate; not wired in)

Margin under v2 labels: the iron condor only when the trend is `sideways` **and** the
v2 vol state is `low` or `mid`. Compared with the v2 run using the E7.5a D25 rule
(≥ 2 of 3 sub-periods won on P&L and DD, and CI lower bound > 0).

| Ranker | Trades v2 → gated | Net P&L v2 → gated | Max DD v2 → gated | Sharpe v2 → gated | Sub-periods won | P&L diff | 90 % CI | Switch |
|---|---|---|---|---|---|---|---|---|
| credit_width (incumbent) | 93 → 86 | −332 → +842 | 38,675 → 36,096 | 0.08 → 0.10 | 1 (sideways) | +1,174 | [−13,383, +14,557] | no |
| ev_proxy | 102 → 92 | −10,496 → +657 | 31,964 → 27,532 | −0.30 → 0.08 | 1 (sideways) | +11,153 | [+640, +24,380] | no |
| managed_net_ev | 98 → 89 | +1,558 → +591 | 33,046 → 30,431 | 0.12 → 0.09 | 0 | −967 | [−16,269, +14,410] | no |
| rorc_day | 98 → 90 | +6,305 → +4,078 | 33,837 → 29,751 | 0.24 → 0.18 | 0 | −2,227 | [−17,278, +14,084] | no |
| rorc_day_vrp | 98 → 90 | +6,305 → +4,078 | 33,837 → 29,751 | 0.24 → 0.18 | 0 | −2,227 | [−17,278, +14,084] | no |

The gate lowers max drawdown for every ranker ($2.6k–$4.4k). The P&L effect is mixed and
small, apart from `ev_proxy`. Under the menu, margin trades only in sideways, so it can
win at most 1 of 3 sub-periods, and the D25 rule's "≥ 2 sub-periods" can never pass here.
That is a limit of the rule for single-regime menus, not evidence against the gate. Still a
candidate for a later change; it needs a forward experiment before anything goes live.

## Transitional guard (E17.2 rule at the reference setting run 3 / margin z 0.10)

SPY v2 labels per decision day: **169 of 606 sessions (27.9 %) would have been blocked.**
The 2-year replay fixture gives 23.4 % at the same setting; this window includes the
April 2025 drawdown and its whipsaw. Trades are the incumbent's in the **v2 run**.

| Profile / incumbent | Group | Trades | Entry days | Win rate | Mean net P&L | Net P&L | PF |
|---|---|---|---|---|---|---|---|
| margin / credit_width | blocked | 29 | 25 | 69.0 % | −50 | −1,456 | 0.95 |
| | rest | 64 | 59 | 70.3 % | +18 | +1,124 | 1.02 |
| cash_debit / debit_width | blocked | 56 | 40 | 58.9 % | **+170** | +9,535 | 1.12 |
| | rest | 60 | 53 | 45.0 % | −459 | −27,520 | 0.73 |

| Profile | Mean diff (blocked − rest) | 90 % CI | P&L forgone if blocked days were skipped |
|---|---|---|---|
| margin | −68 | [−696, +715] | −1,456 (skipping would have **saved** $1.5k) |
| cash_debit | +629 | [−509, +1,997] | +9,535 (skipping would have **cost** $9.5k) |

**Verdict: keep off.** The turn-on condition fails in both profiles:
- margin: blocked-day trades are lower on the mean, but the CI upper bound is +$715,
  above 0.
- cash_debit: blocked-day trades are higher on the mean.

Low-conviction days, near a threshold or just after a flip, are where cash_debit found
its better entries in this sample.

## Baseline conditioning table under v2 (E7.2: SPY, all D4 specs pooled)

PF = Σ wins / |Σ losses|. The v1 column reproduces
[backtest-baseline.md](backtest-baseline.md) within 0.03 PF in every cell except bull_put
high vol (49 vs 48, which rests on a near-zero loss sum). The small differences come
from the longer split-adjusted label history: rows before the first 20 sessions that were
`unknown` now carry a label.

| Structure | Bear v1 → v2 | Sideways v1 → v2 | Bull v1 → v2 | Low vol v1 → v2 | Mid vol v1 → v2 | High vol v1 → v2 |
|---|---|---|---|---|---|---|
| iron_condor PF | 0.23 → 0.23 | 1.23 → 1.13 | 0.73 → **1.40** | 1.79 → **2.56** | 0.84 → 0.85 | 0.37 → 0.71 |
| bull_put PF | 1.64 → 2.29 | 1.70 → 1.68 | 5.79 → 2.56 | 1.78 → 2.82 | 1.60 → 0.87 | 49 → 3.89 |
| bear_call PF | 0.19 → 0.16 | 0.92 → 0.83 | 0.36 → 0.89 | 1.45 → 1.79 | 0.60 → 0.91 | 0.17 → 0.39 |
| bull_call PF | 2.63 → 3.68 | 1.13 → 1.20 | 3.28 → 1.36 | 1.05 → 0.93 | 1.35 → 1.10 | 4.69 → 2.05 |
| long_call PF | 9.13 → 9.63 | 1.06 → 1.13 | 2.00 → 0.85 | 0.46 → 0.38 | 1.88 → 0.83 | 6.95 → 3.15 |
| bear_put PF | 0.93 → 0.61 | 0.57 → 0.57 | 0.18 → 0.42 | 0.59 → 0.42 | 0.57 → 0.97 | 0.11 → 0.33 |
| long_put PF | 0.10 → 0.08 | 0.34 → 0.35 | 0.05 → 0.14 | 0.41 → 0.24 | 0.25 → 0.64 | 0.00 → 0.07 |
| iron_condor trades | 132 → 180 | 2,072 → 1,743 | 296 → 577 | 1,126 → 770 | 1,143 → 752 | 231 → 978 |

- **Vol:** v2's percentile state puts far more SPY days in `high` (978 vs 231) and splits
  condors more cleanly at the low end (PF 2.56). Its `high` bucket is no longer a few days
  around April 2025, so the high-vol PF is less extreme on both sides. This is the
  strongest support for v2 in the report, and it is what the vol-gate candidate builds on.
- **Trend:** v2 labels SPY bull on 577 condor entries (v1 296), and condors make money
  there (PF 1.40). v2's `bull` on SPY is mostly a slow grind (a high z from low vol), and
  condors like a slow grind. So under v2, "sideways only" is not obviously the right
  condor menu either.

## What this means for the owner

- **Regime model:** the fixed rule says **roll back to v1** (`arc config set regime.model
  v1`), driven by cash_debit, on differences the bootstrap cannot tell apart from noise.
  Keeping v2 would be a judgement call against the pre-declared rule; this report doesn't
  make it.
- **Guard:** keep it off (the rule's answer, and the cash_debit evidence points the other
  way).
- **Vol gate:** don't wire it in now. It is the most promising lead (lower drawdown on all
  margin rankers, condor PF 2.56 in v2 low vol) for a forward experiment if wanted.

## Code

- `arc/backtest/regime.py`: `regime_labels(closes, model)`. v1 returns exactly
  `label_trend`/`label_vol` (the E7.5 labels); v2 wraps `arc.features.regime`'s
  `label_regimes_v2` and `label_vol_v2`.
- `arc/backtest/ranking.py`:
  - `BacktestSettings.regime_model` (default `v1`) and `vol_gate` (default empty).
  - `apply_stance(..., vol=, vol_gate=)`.
  - `labels_for(closes, model)`.
- `arc/backtest/rank_report.py`, `engine.py`, `report.py`, `cli.py`: thread the model and
  an optional longer label-only close history (`--regime-model`, `--label-history-days`,
  `--label-adjusted`) through `arc backtest rank` and `arc backtest`. All defaults
  reproduce the previous output byte for byte. `tests/test_backtest_regime.py` checks this
  on a synthetic run, and it was checked on real data for SPY/NVDA May–Jul 2024: all 10
  output files identical to main.
- `arc/backtest/regime_compare.py`: the verdict rule, label flips, guard replay and split
  (tests in `tests/test_backtest_regime_compare.py`).
- `scripts/e173_regime_report.py`: builds `regime-v2/*.csv` from the run directories.
