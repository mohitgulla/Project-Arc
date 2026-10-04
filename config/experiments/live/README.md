# Forward A/B experiment specs (PLAN D44, card E10.1)

One YAML file per experiment, validated by `arc.experiments.models.ExperimentSpec`
(`extra="forbid"`). Not the same thing as the backtest overlays one level up in
`config/experiments/*.yaml`, but the `arms.treatment.overlay` block uses the same
format: each key is a config file stem (`ranking`, `exits`, `costs`,
`account_profiles`, `routines`) and its value is a partial copy of that file,
deep-merged over it with `arc.utils.yamlpatch.deep_merge` (the function
`arc backtest rank --experiment` uses).

Lifecycle:

    arc experiment create --spec config/experiments/live/x1_aa_baseline.yaml --db <db>
    arc experiment register X-1 --db <db>     # locks sha256(canonical spec)
    arc experiment show X-1 --db <db>
    arc experiment verify X-1 --db <db>

Unset `alpha`, `power`, `min_sessions`, `max_sessions` and `guardrails` are
filled from `config/experiments.yaml` at `create`; the filled spec is what gets
hash-locked. After `register`, any edit needs a new experiment id.

Promotion (E10.7 strategy-lane CI check, docs/OPS.md 5.19): a PR that flips a
strategy default cites `Experiment: X-<n>` and commits
`verdicts/X-<n>.yaml` with `experiment_id`, `verdict: win` and the stored report's
`report_hash` (from `arc experiment show X-<n> --json`). The check only lets the PR
change the values that experiment's treatment overlay tested.
