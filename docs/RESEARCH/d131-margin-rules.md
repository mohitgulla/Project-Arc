# E28.6 · D131 margin rules in the backtest — knobs, baseline, BT proposal brief

Status: knobs + baseline done 2026-10-19 (card E28.6). Plan ref: PLAN D131 (owner answers
1–12, 2026-10-17). Cards do not draft BT specs (D121/D86): this file is the **brief** the
Analyst turns into `BT-<n>` proposals; the owner approves them.

## 1. What the sim can now vary (one overlay line per arm)

Every knob defaults to the shipped behaviour, so the base config is unchanged and an arm
is one `ranking` overlay line in a study spec (`arms.<arm>.overlay.ranking`):

| Rule (card) | Knob | Shipped | Arm example |
|---|---|---|---|
| Credit floor (E28.1) | `backtest.credit_wings.min_credit_width` | null = menu's 0.10 | `{backtest: {credit_wings: {min_credit_width: 0.20}}}` |
| Wing mode (E28.1) | `backtest.credit_wings.mode` `pct_spot` / `pct_nearest` / `dollars`, `pct`, `dollars`, `cap` | null = menu's widest ≤ 5 % fitting $3,750 | `{backtest: {credit_wings: {mode: dollars}}}` (live rollback `$5`) |
| Regime gate (E28.2) | `backtest.sim.gate.regime_gate_enabled` / `regime_gate_ma` 50·100·200 / `regime_gate_buffer_pct` / `regime_gate_blocked_kinds` | null = ArcSettings (on, 200, 0, bull_put+IC) | `{backtest: {sim: {gate: {regime_gate_ma: 50}}}}` |
| Regime VIX variant (sim only) | `backtest.sim.gate.regime_vix_pct_above`, `regime_vix_combine` `and`/`or` | null = the live rule | `{backtest: {sim: {gate: {regime_gate_ma: 50, regime_vix_pct_above: 0.8}}}}` |
| Open-risk cap (E28.3) | `backtest.sim.gate.max_open_risk_pct` 0.10–1.0 | null = 0.40 | `{backtest: {sim: {gate: {max_open_risk_pct: 0.30}}}}` |
| Cluster cap (E28.5) | `backtest.sim.gate.cluster_cap_enabled` / `_corr_threshold` / `_max_names` | null = on, 0.7, 2 | `{backtest: {sim: {gate: {cluster_cap_max_names: 3}}}}` |
| VRP cap (owner #12, sim only, **no live default**) | `backtest.sim.vrp_max` | null = off | `{backtest: {sim: {vrp_max: 0.10}}}` |
| Credit TP (existing) | `exits` overlay `take_profit_pct_of_max_gain` per credit kind | 0.50 | exits overlay, as in E7.18's TP candidates |

Definitions (code: `arc/backtest/sim_config.py`, `arc/backtest/ranking.py::CreditWings`,
`arc/backtest/sim.py`, `arc/backtest/market_index.py`):

- **Gate knobs** replace the `ArcSettings` field of the same name for the walk only
  (`SimGateOverrides.apply`); bounds equal the registry's. `build_sim` builds the SPY
  trend series for the arm's SMA length.
- **VRP** = IV30 − HV20 at the decision close, in vol points as a fraction (0.10 = 10 pts).
  IV30 = the E7.10 constant-maturity ATM IV the walk already reads; HV20 = 20-session
  close-to-close vol of split-adjusted closes (the E7.16 `rv20` definition, point in time).
  Above `vrp_max`, every credit candidate of that name-day leaves the pool (skip
  `vrp_cap`, once per name-day); debit candidates stay. Unknown IV30 or HV20 = not skipped.
- **VIX variant**: VIX's 1-year percentile = the live `market_health` `vix_pct_1y` rule
  (`percentile_prior`: share of the prior ≤ 252 closes strictly below today's, ≥ 120 obs).
  `and` blocks a blocked kind only when SPY < SMA **and** VIX percentile > x; `or` when
  either. A session without a percentile counts as VIX-high (fail closed). Data: Cboe's free
  `VIX_History.csv`, stored once offline by `scripts/fetch_vix_history.py` →
  `data/market_index/VIX.parquet` (9,296 closes, 1990-01-02 → 2026-10-16). A study with the
  knob set and no file refuses to build.
- **Credit wings** apply to every menu block that offers a credit kind; a set field changes
  the menu-cache key (the arm rebuilds its credit menus, ~8 h cold on C125 dev). `credit_wings`
  all-null and `sim.gate` all-null leave every menu-cache key and every walk byte-identical.

## 2. Baseline: margin book, all D131 rules on vs main before D131 (C125 dev)

`scripts/d131_baseline.py --book c125_dev` (report-only, relative A/B; E7.18 harness
caveats apply: EOD marks + range proxy, model fills at mid ± 0.25 × spread, no live
LLM selection). Margin profile, live incumbent ranker (`managed_net_ev`), $25K, 2020-01-01
→ 2023-12-31, every arm built from the E28.6 knobs:

- `menu-d131 / rules-on` = shipped main (5 % widest-fit wing, floor 0.10, regime 200-SMA,
  open risk 40 %, cluster cap 0.7/2);
- `menu-pre / rules-off` = main before D131 (nearest 2 % wing, no floor, every rule off);
- the two cross cells separate the menu effect from the gate effect.

Output: `data/backtest/runs/d131_baseline/c125_dev/` (`cells.csv`, `by_kind.csv`,
`by_sleeve.csv`, per-cell trades/equity/skips).

_Baseline running; results land here before the PR leaves draft._

## 3. Market-state frequencies the studies will act on (C125 dev window, 1,006 sessions)

| Condition | Sessions | 2020 | 2021 | 2022 | 2023 |
|---|---|---|---|---|---|
| SPY < SMA200 (shipped gate) | 355 | 116 | 0 | 213 | 26 |
| SPY < SMA50 | 322 | 60 | 23 | 165 | 74 |
| VIX 1y pct > 0.80 | 183 | 74 | 10 | 98 | 1 |
| SPY < SMA50 **and** VIX > p80 | 146 | 47 | 3 | 95 | 1 |
| SPY < SMA50 **or** VIX > p80 | 359 | 87 | 30 | 168 | 74 |
| SPY < SMA200 **and** VIX > p80 | 153 | 66 | 0 | 86 | 1 |

(SPY closes from the backtest underlying store; SMA warm-up from 2019.) The 50+VIX `and`
variant blocks short premium on fewer than half the sessions the shipped 200-SMA gate does,
mostly the same 2020/2022 stress windows; 2023's late-year 200-SMA dip is not blocked.

VRP (IV30 − RV20) over every name-day in `data/option_stats` (all 500 pulled names,
2020–2023, 439,150 name-days with both inputs, 95.3 %): p50 0.029, p75 0.085, p90 0.165,
p95 0.253, p99 0.62. Share above a cap: 0.05 → 39.1 %, 0.10 → 20.6 %, 0.15 → 11.6 %,
0.20 → 7.4 %. Per year the share > 0.10 is 24.2 / 19.6 / 18.4 / 20.4 % (2020–2023), so
the cap is not a 2020-only filter. (Descriptive only; the walk computes its own
HV20 from split-adjusted closes, which can differ slightly from the pull's `rv20`.)

## 4. BT proposal brief for the Analyst

All share the D25 / E7.18 decision rule verbatim: *beat the incumbent on net P&L **and**
max drawdown in ≥ 2 of 3 sub-periods (2020–21 / 2022 / 2023 on C125 dev, then C500 dev;
holdout only after a win), with a 90 % bootstrap CI excluding 0.* Incumbent = control =
the shipped config (all knobs null). Profiles: `margin` (the only profile with credit kinds).
Every arm below is checked by `tests/test_e286_backtest_knobs.py::test_brief_arms_validate_as_study_overlays`.

a. **Credit/width floor** 0.10 (control) vs 0.15 / 0.20 / 0.25:
   `{backtest: {credit_wings: {min_credit_width: x}}}`. Live probe (D131): 20Δ shorts with
   5 % wings run 0.04–0.25, median ≈ 0.12, so 0.25 removes almost every credit candidate;
   expect a debit-heavy book at 0.25. Each arm rebuilds the credit menus.
b. **VRP cap grid (descriptive first)**: `{backtest: {sim: {vrp_max: x}}}` for
   x ∈ {0.05, 0.10, 0.15, 0.20}. Report the share of credit opens removed (skip `vrp_cap`)
   and the P&L of the removed trades alongside the D25 verdict. Hypothesis direction is
   open: extreme IV − HV marks event risk (skip) *or* rich premium (keep). No live default
   regardless of the verdict without an owner decision (D131 #12). Walk-only arms (warm cache).
c. **Regime gate**: 200-SMA (control) vs 50-SMA + VIX percentile > 0.80
   (`{backtest: {sim: {gate: {regime_gate_ma: 50, regime_vix_pct_above: 0.8}}}}`, `and`);
   optional third arm `regime_vix_combine: or`. Needs `data/market_index/VIX.parquet`.
   Walk-only arms.
d. **Open-risk cap** 40 % (control) vs 30 % / 50 %:
   `{backtest: {sim: {gate: {max_open_risk_pct: x}}}}`. Walk-only arms. Read with the
   E7.18 finding that 34–75 % of equity was at risk at drawdown peaks.
e. **Credit take-profit** 50 % (control) vs 65 % of max gain: exits overlay on the credit
   kinds (`vertical_credit`, `iron_condor`) `take_profit_pct_of_max_gain: 0.65`, as in
   the E7.18 TP candidates. Changes exit outcomes, so the menus rebuild.

Cluster cap: no study proposed (owner revision 2026-10-18: a missing correlation is *not*
a peer; the PIT matrix needs ≥ 40 returns, so the dev window's first months cap nothing).
The knobs exist (`cluster_cap_max_names`, `cluster_cap_corr_threshold`) if the Analyst
wants one.

Posting this brief in the Analyst's thread is the owner's call; the card only writes it.
