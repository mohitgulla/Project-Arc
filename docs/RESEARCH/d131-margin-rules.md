# E28.6 · D131 margin rules in the backtest — knobs, baseline, BT proposal brief

Status: knobs + brief done 2026-10-19, baseline 2026-10-26 (card E28.6). Plan ref: PLAN D131 (owner answers
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
- the two cross cells separate the menu effect from the gate effect;
- each of the four again with the debit kinds excluded (`sim.exclude_kinds`, suffix
  `__credit`). This is a diagnostic, not a study arm (see "Why every full cell is ruined").

Output: `data/backtest/runs/d131_baseline/c125_dev/` (`cells.csv`, `by_kind.csv`,
`by_sleeve.csv`, per-cell trades/equity/skips). Run 2026-10-26 on the warm menu cache
(d131 build 817 s); the four full cells reproduce the 2026-10-19 run to the cent.
`net_mid` = the same trades filled at mid (the cost-free bound). `max_dd_pct` is negative,
so a positive delta = a shallower drawdown.

| cell | trades | net | net at mid | max DD % | win | per-$-risk |
|---|---|---|---|---|---|---|
| **menu-d131 / rules-on** (shipped) | 913 | −$24,350 | −$12,069 | −99.0 | 0.468 | −0.056 |
| menu-d131 / rules-off | 469 | −$24,935 | −$16,018 | −99.8 | 0.456 | −0.085 |
| **menu-pre / rules-off** (pre-D131) | 511 | −$24,927 | −$15,981 | −99.7 | 0.434 | −0.099 |
| menu-pre / rules-on | 848 | −$24,686 | −$9,750 | −99.6 | 0.459 | −0.046 |
| menu-d131 / rules-on / credit | 510 | −$9,811 | +$1,643 | −59.1 | 0.725 | −0.016 |
| menu-d131 / rules-off / credit | 623 | −$22,147 | −$6,918 | −90.8 | 0.685 | −0.034 |
| menu-pre / rules-off / credit | 564 | −$21,175 | −$3,085 | −88.9 | 0.690 | −0.040 |
| menu-pre / rules-on / credit | 476 | +$3,406 | +$22,878 | −55.8 | 0.708 | +0.005 |

Where the loss sits (net by entry year; D25 sub-periods):

| cell | debit share | debit net | credit net | 2020–21 | 2022 | 2023 | equity end-2021 | equity < $2.5K from |
|---|---|---|---|---|---|---|---|---|
| d131 / on | 0.91 | −$20,163 | −$4,187 | −$15,765 | −$4,506 | −$4,080 | $7,557 | 2023-02-21 |
| d131 / off | 0.85 | −$20,309 | −$4,626 | −$24,815 | −$104 | −$16 | $153 | 2021-03-22 |
| pre / off | 0.86 | −$21,499 | −$3,428 | −$24,767 | −$127 | −$34 | $202 | 2020-10-12 |
| pre / on | 0.91 | −$25,393 | +$707 | −$12,751 | −$8,717 | −$3,218 | $15,087 | 2023-01-24 |
| d131 / on / credit | 0 | — | −$9,811 | +$4,138 | −$8,644 | −$5,305 | $28,441 | never |
| d131 / off / credit | 0 | — | −$22,147 | −$5,834 | −$13,785 | −$2,527 | $18,889 | never |
| pre / off / credit | 0 | — | −$21,175 | −$4,406 | −$15,072 | −$1,697 | $19,564 | never |
| pre / on / credit | 0 | — | +$3,406 | +$12,055 | −$6,532 | −$2,117 | $33,859 | never |

Per-kind, per-sleeve and skip tables: `data/backtest/runs/d131_baseline/c125_dev/`
(`by_kind.csv`, `by_sleeve.csv`, `cells.csv`) and the PR body.

### Why every full cell is ruined (−97 % to −99.7 %)

It is not a harness or capital-base bug. Checked on all four full cells:

- Σ trade P&L = Δequity to the cent, and the book starts at $25,000 (the live D80 size).
- No open's max loss exceeds 15 % of the equity at entry (the D116 per-underlying cap);
  the median is 3–9 %.
- The shipped cell reproduces E7.19's attribution: E7.19's incumbent C125 margin book
  (same menu shape, D131 rules on) is −$24,382 / −0.046 there
  (`docs/RESEARCH/sim-alignment.md` § Post-D128 attribution); `menu-d131 / rules-on`
  here is −$24,350 / −0.056.

The cause is the D128 margin menu, as E7.19 found: the margin profile carries the
cash_debit sleeves and debit kinds, and the live incumbent ranker (`managed_net_ev`)
picks them. Debit kinds are 85–91 % of every full cell's trades and −$20K to −$25K of
its net; long calls alone are −$11K to −$23K. A small book compounds the loss: 90–93 % of
the trades in the rules-off cells open in 2020–21, the book is below $250 by end-2021,
and the 2022–23 trades are a few dollars each (median |P&L| $8–12). The D131 rules slow that (end-2021
equity $7.6K / $15.1K with rules on) but don't stop it, because they don't touch
debit kinds (the regime gate blocks bull puts and iron condors only).

With the debit kinds excluded no cell is ruined (max DD 56–91 %), and the D131 rules
are worth +$12.3K net (d131 menu) and +$24.6K (pre menu) on the credit book they were
written for. Whether margin keeps the debit kinds is a strategy question (D128) that
E7.19 already listed as a candidate study ("margin credit only, or the debit kinds on a
separate measure"); this card does not change it.

### Read (relative A/B only)

- **Shipped vs pre-D131, full menu:** +$577 net, +$3,913 at mid, per-$-risk +0.043,
  drawdown 0.7 pts shallower. Both are ruined, so the $ net is censored at −$25K;
  per-$-risk and time-to-ruin (2023-02 vs 2020-10) are the comparable measures.
- **Gate effect** (rules on − off, same menu): per-$-risk +0.029 (d131 menu) / +0.053
  (pre menu) on the full menu; +$12.3K / +$24.6K net and 32–33 pts shallower drawdown
  credit only. The rules help in every pair.
- **Menu effect** (d131 menu − pre menu): ≈ 0 on the full menu with rules off (both
  ruined). Credit only it is negative: rules off −$972, rules on −$13.2K (the 5 %
  widest-fit wing and 0.10 floor vs the 2 % nearest wing, both under the D131 rules).
  That is one book and one window, not a verdict; brief (a) and a wing-width arm
  (`credit_wings.mode: pct_nearest, pct: 0.02`) are how the Analyst would test it.
- Every credit-only cell loses in 2022 and in 2023; 2020–21 is the only sub-period with
  a positive cell. This baseline is not a study verdict.

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

Two more questions the baseline raises (listed for the Analyst; not part of the card's a–e):
- **Wing width**: shipped 5 % widest-fit vs the pre-D131 2 % nearest wing
  (`{backtest: {credit_wings: {mode: pct_nearest, pct: 0.02}}}`); credit only, under the
  D131 rules, the 2 % wing was +$13.2K better on this one book.
- **Debit kinds on the margin menu** (E7.19's first candidate study, D128): they are the
  ruin in every full cell here. A credit-only arm is `{backtest: {sim: {exclude_kinds:
  [bull_call, bear_put, long_call, long_put]}}}`.

Cluster cap: no study proposed (owner revision 2026-10-18: a missing correlation is *not*
a peer; the PIT matrix needs ≥ 40 returns, so the dev window's first months cap nothing).
The knobs exist (`cluster_cap_max_names`, `cluster_cap_corr_threshold`) if the Analyst
wants one.

Posting this brief in the Analyst's thread is the owner's call; the card only writes it.
