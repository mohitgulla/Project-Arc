# ML studies (E27.7, PLAN D133)

One file per pre-registered model study, `ML-<n>.yaml` (model `arc.ml.study.MlStudySpec`,
`extra="forbid"`). The spec's canonical-JSON SHA-256 is its identity: editing any field
makes a new hash. Files land here only through `arc ml study adopt` (owner `approve
ML-<n>` → auto-merged spec PR on branch `ml/<n>`); a scratch spec uses id `ML-0` and is
never committed.

    arc ml study validate <spec.yaml>            # overlay, rule, incumbent, grid cap
    arc ml study run --spec <scratch.yaml>       # every rung of a scratch (ML-0) spec
    arc ml study run ML-<n> [--rung dev_2022]    # by hand (yields to the runner window)
    arc ml study adopt ML-<n> --spec <draft> --approval slack:<ts>
    arc ml study status                          # the shared queue, BT + ML, with ETA
    arc ml study verdict ML-<n>                  # verdict.json, or the last rung
    arc ml study stop ML-<n> "<reason>"          # a queued study
    arc ml study drop ML-<n> "<reason>"          # an unqueued draft (the number stays a gap)
    arc ml study show [ML-<n>] [--json]          # ledger lines

Spec fields: `id`, `hypothesis`, `owner`, `created`, `family` (`m1 | credit | debit`),
`incumbent` (a registered version, or `champion`, pinned at adopt), `change_kind`
(`challenger | tuning | features | calibration`), `overlay` (the family's training-config
overlay: M1 `params` / `rounds` / `features.add|drop` / `calibration.method|window_sessions`
/ `targets`; M2 the `M2Config` keys), `grid` (≤ `ml_studies.max_grid` overlay variants,
each laid on `overlay`), `decision_rule.kind` (the family's registered rule: `m1_keep`
for M1, `m2_promote` for credit/debit; a study can't change its bar) and `study_value`.

Protocol (enforced in code):

1. Rungs `dev_2022` → `dev_2023` → `verdict`, the harness's two test folds. The holdout
   (2024+) is never read by a study; champion promotion and its holdout read stay
   owner-only (`arc ml promote`).
2. Each dev rung fits every grid variant on that year's fold and logs every
   configuration with its inner selection metric (M1: calibration-block pinball; M2:
   inner-validation win log-loss). `dev_2022` failing the rule on every variant by more
   than `ml_studies.fail_margin` stops the study.
3. The verdict picks the variant with the best mean inner metric (never the test-year
   result), trains it end to end (M2: with the decision-value replays) and registers
   it: rule passed in both test years → `shadow`, else `candidate` with `verdict: fail`.
4. Studies share the nightly `backtest.studies` runner and queue with the BT studies
   (one job at a time, same window, `nice`, oldest first) and the 2-approvals-per-ISO-week
   (ET) cap. An interrupted rung leaves no ledger line and resumes the next night.
5. One line per rung in `data/ml/studies/ledger.jsonl`; runner log and `verdict.json`
   under `data/ml/studies/ML-<n>/`. Never `data/backtest/ledger.jsonl`.

`retired.yaml` lists retired `ML-<n>` ids (same shape as the BT list).
