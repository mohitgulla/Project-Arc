# Forward A/B experiment specs (PLAN D44, card E10.1)

One YAML file per experiment, validated by `arc.experiments.models.ExperimentSpec`
(`extra="forbid"`). Not the same thing as the backtest overlays one level up in
`config/experiments/*.yaml`, but the `arms.treatment.overlay` block uses the same
format: each key is a config file stem (`ranking`, `exits`, `costs`,
`account_profiles`, `routines`, `universe` (E13.12)) and its value is a partial copy of that file,
deep-merged over it with `arc.utils.yamlpatch.deep_merge` (the function
`arc backtest rank --experiment` uses).

Lifecycle:

    arc experiment create --spec config/experiments/live/xp1_aa_k3.yaml --owner-approval P-<n> --db <db>
    arc experiment register XP-1 --owner-approval P-<n> --db <db>     # locks sha256(canonical spec)
    arc experiment show XP-1 --db <db>
    arc experiment verify XP-1 --db <db>

Unset `alpha`, `power`, `min_sessions` and `max_sessions` are
filled from `config/experiments.yaml` at `create`; the filled spec is what gets
hash-locked. After `register`, any edit needs a new experiment id.

Promotion (strategy-lane CI check, docs/OPS.md 5.20, D86): while a spec here has no
verdict file it is *open*, and every config leaf its treatment overlays set is locked;
no PR may change those leaves. The promotion PR cites `Experiment: XP-<n>` and commits
`verdicts/XP-<n>.yaml` with `experiment_id`, `verdict: win`, `winner: t<k>` (omit for a
single-treatment run = `t1`) and the stored report's `report_hash` (from
`arc experiment show XP-<n> --json`). The check only lets it change the values arm
`t<k>` tested. Any verdict file (win, futility, invalid) frees the leaves.

Who adds specs (D86): specs are added only by `arc experiment adopt` (E21.4) after the
owner approves an Analyst proposal (`approve P-<n>` in #arc-analyst). It commits
`xp<n>_<slug>.yaml` through an auto-merged PR on branch `xp/<n>`, and every concluded
experiment gets its `verdicts/XP-<n>.yaml` the same way (branch `xp/<n>-verdict`; docs/OPS.md
§5.18). Cards and PRs never
draft XP specs here; a strategy PR's only XP touchpoint is its `XP-advisory:` line.
`create` and `register` refuse without `--owner-approval <P-<n> | slack:<ts> | owner:<note>>`.

Registry reset (owner, 2026-10-13, PLAN D111): every earlier spec, verdict and history
file was removed and the live registry cleared; XP-1 is the K=3 calibration A/A
(`xp1_aa_k3.yaml`, card E15.9), and ids are never reused after this.

Fork at any persona (E13.12, D56): `arc experiment start` computes each arm's plan
(`arc.experiments.runner.arm_plan`) and stores it on the arm's identity and in the
`running` event: the loop step it forks at (any of `research`, `exits.mandatory`,
`quant.exit`, `risk.exit`, `quant.open`, `risk.open`, `quant.revise`,
`quant.propose`) and the non-loop personas it runs itself. An overlay touching
`universe` or `funnel.scout.*` gives the arm its own Scout; one touching
`personas.scalp*`, `funnel.scalp.*` or `categories.*` its own Scalp
(`experiments.runner.arm_personas` forces either for every arm). Preview without
writing anything:

    arc experiment start XP-<n> --dry-run --db <db>
    arc experiment arms-tick --dry-run --experiment XP-<n> --now 2026-10-06T06:00-04:00 --db <db>

The v1 A/A spec (`tests/fixtures/experiments/xp1_aa_baseline.yaml`) and the XP-5 v1 A/B
(`tests/fixtures/experiments/xp5_universe_screen.yaml`) are test fixtures only; they pin
the v1 -> v2 hash invariant.
