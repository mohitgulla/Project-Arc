# XP-1 A/A calibration

Status: **pending live run (E10.8)**. The live A/A (about 10 sessions on the production and experiment paper accounts) has not run yet. The section below is the **fixture dry run** (synthetic sessions on scratch stores, no orders) that shows the layout; E10.8 replaces this file with the live report:

    arc experiment report XP-1 --db data/arc.db --stored --format md --out docs/RESEARCH/experiments/XP-1-aa.md

Regenerate the example with `.venv/bin/python scripts/xp1_aa_dry_run.py --dir <empty scratch dir> --write-doc`.

## Example: fixture dry run (synthetic data)

### XP-1 A/A calibration report

- Evaluated: 2026-10-09 16:40 -04:00; t0 2026-09-25 15:00 -04:00 at $100,000.00 (both arms); legacy book 0 structure(s), excluded
- Sessions: 10 paired (window 10–10); as of 2026-10-09; none missing
- Harness check (verdict): **futility**: A/A complete after 10 sessions (no difference, as expected)
- Mean d_t −0.017%/day, always-valid 95% CI [−0.07, +0.04] (% of t0 equity; p 1.00). An A/A whose CI excludes 0 is `invalid`: the harness, not the strategy, differs between the arms.

#### Noise: sigma of the paired daily difference

- sigma(d_t) = 0.036% of t0 equity per session ($35.70 at t0 equity)

#### Minimum detectable effect (daily mean of d_t)

| Sessions | Fixed horizon (2.8·sigma/√n) | Always-valid (power 80%) |
|---:|---:|---:|
| 10 | 0.032% | 0.044% |
| 20 | 0.022% | 0.031% |
| 40 | 0.016% | 0.022% |
| 60 | 0.013% | 0.019% |

The always-valid MDE is the effect the daily-peeking mSPRT detects by that session with the stated power; it is larger than the fixed-horizon MDE, which is the price of peeking every day.

#### Execution gap between the two paper accounts

- Slippage gap (treatment − control mean): +2.6 bps
- Fill-rate gap (treatment − control): +3.3%
- LLM divergence on identical inputs: 0% (0/1 paired chains)

#### Per arm (informational; no guardrails)

| Arm | Sessions | P&L | Max DD | Worst day | Orders | Fills | Mean slippage |
|---|---:|---:|---:|---:|---:|---:|---:|
| control | 10 | −$607.02 | 0.78% | −0.30% | 66 | 27/30 | 5.3 bps |
| treatment | 10 | −$779.21 | 0.90% | −0.30% | 64 | 28/30 | 7.9 bps |

#### Policy (fixed, not set from this A/A)

- alpha 0.05, power 0.8, min 20 / max 60 sessions for A/B experiments (owner, D44). This A/A informs only sigma, the MDE and the execution gaps above.

#### Provenance

- Spec sha256 `179f25fb9bbccc772d2f57484cb7d872fb348b031260ff65047bb817fc236e6b` (registered `179f25fb9bbccc772d2f57484cb7d872fb348b031260ff65047bb817fc236e6b`)
- Config sha256 `7f1071bcda8bbb4226616044fbb932aa97606cf124d47bcfccb67b64a7a9c817`
- Shas: control `d6e54ca66c97`, treatment `d6e54ca66c97`, evaluator `d6e54ca66c97`
- Report sha256 `284a7149f301adeb523f674ad45fc3f08a6aaffc24e9e83a66aa9be872dcbeb7`

#### Series

| Session | Control P&L | Legacy P&L | Treatment P&L | d_t |
|---|---:|---:|---:|---:|
| 2026-09-28 | $0.37 | $0.00 | $19.96 | +0.020% |
| 2026-09-29 | $89.62 | $0.00 | $103.90 | +0.014% |
| 2026-09-30 | −$82.24 | $0.00 | −$78.02 | +0.004% |
| 2026-10-01 | −$267.18 | $0.00 | −$304.40 | −0.037% |
| 2026-10-02 | −$136.40 | $0.00 | −$137.57 | −0.001% |
| 2026-10-05 | −$297.49 | $0.00 | −$269.68 | +0.028% |
| 2026-10-06 | $18.04 | $0.00 | −$35.73 | −0.054% |
| 2026-10-07 | $402.07 | $0.00 | $383.76 | −0.018% |
| 2026-10-08 | −$147.67 | $0.00 | −$223.71 | −0.076% |
| 2026-10-09 | −$186.14 | $0.00 | −$237.72 | −0.052% |
