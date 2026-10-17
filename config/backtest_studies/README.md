# Backtest studies (E7.12, PLAN D84)

One file per pre-registered study, `BT-<n>.yaml` (model `arc.backtest.study.StudySpec`,
`extra="forbid"`). The spec's canonical-JSON SHA-256 (the E10.1 lock) is its identity:
editing any field makes a new hash.

    arc backtest study show BT-<n>              # hash, variants tried, holdout state, ladder
    arc backtest study run BT-<n>               # dev on the first ladder rung
    arc backtest study run BT-<n> --universe c500   # confirm on a larger rung (same arms)
    arc backtest study run BT-<n> --holdout     # once per spec hash, on the last rung
    arc backtest study ledger BT-<n>|all [--json]

Protocol (enforced in code):

1. Dev runs (default 2020-01-01 -> 2023-12-31) iterate freely; every run appends one
   line to `data/backtest/ledger.jsonl` (spec hash, arm set, universe, period, metrics,
   git sha). Each distinct arm set (rankers + overlays + profiles) is one variant.
2. Larger rungs reuse the same arms; the report's ladder table flags a sign flip of the
   primary metric's treatment - control difference vs the first rung.
3. The holdout (default 2024-01-01 -> latest) runs once per spec hash, and once per arm
   set of a study (rewording the text does not buy a second holdout). More variants
   than `max_variants` need `--allow-overfit-warning`, stamped in the report and ledger.
4. The D25 rule (`arc.backtest.ranking.challenge`) is applied verbatim and printed with
   its bootstrap CI. A verdict only recommends; live changes go through D44.

Arms: a `control` plus `t1`..`t3`, each an optional `ranker` (default: the profile's
incumbent) and an `overlay` on `ranking` / `exits` / `costs` (sim knobs live in
`ranking.yaml` `backtest.sim`), deep-merged like `arc backtest rank --experiment`.

Reports go to `docs/RESEARCH/studies/<id>.md` + `.json` (aggregates only, never raw
quote rows). Per-run CSVs stay under `data/backtest/studies/` (gitignored).

Retired studies (E26.5, PLAN D127): `retired.yaml` here lists studies whose results no
longer describe the current algo (BT-1..BT-8). It is not a spec (the `BT-*.yaml` globs skip
it) and adding a row changes no study hash. A retired study is history only: the Analyst
leaves it out of evidence and its context, `arc backtest study adopt|run` and the nightly
runner refuse it, and the Tower lists it under Retired. Nothing is deleted, and ids are never
reused (the next study is above every retired id). Add one with
`arc backtest study retire BT-<n> --reason "..."` and commit the file via a PR.
