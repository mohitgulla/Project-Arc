"""ML studies (card E27.7, PLAN D133): ``ML-<n>`` specs, their ledger, adopt, and the
rung runs the shared nightly runner (E26.3) launches.

A study trains a variant of an existing model family (``m1``, ``credit``, ``debit``) on
the E27.3 harness and judges it with that family's **pre-registered** rule on dev data:

* ``dev_2022`` / ``dev_2023``: each grid variant (the spec's ``overlay`` with one ``grid``
  item laid on it) is fit on that test year's walk-forward fold and judged with the
  family rule for that year. Every configuration tried is logged with its **inner**
  selection metric (M1: pinball of the raw cross-fitted LightGBM quantiles on the
  calibration block before the test cut; M2: the inner-validation win log-loss). A clear
  dev fail on ``dev_2022`` (every variant misses the rule by more than
  ``ml_studies.fail_margin``) stops the study.
* ``verdict``: the best variant on the inner selection metric (never the test-year
  result) is trained end to end (folds + final fit, M2 with its decision-value replays),
  judged by the family rule over both test years and registered: pass → ``shadow`` (the
  registry's candidate → shadow path), fail → ``candidate``. Champion stays owner-only
  (``arc ml promote``, the one place the holdout is read).

The holdout (2024 →) is never read here: the M1 loaders refuse it (``HoldoutReadError``)
and M2 reads the ``dev`` split only (D84, D133).

Files: the committed spec ``config/ml_studies/ML-<n>.yaml`` (hash-locked like BT),
``data/ml/studies/ledger.jsonl`` (one line per rung) and ``data/ml/studies/ML-<n>/``
(runner log, ``verdict.json``). ``ML-0`` is the scratch id: it runs with
``arc ml study run --spec`` but is never adopted or queued.

Heavy deps (pandas, LightGBM) are imported inside the functions that train.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import statistics
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import structlog
import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from arc.backtest.queue import (
    APPROVAL_RE,
    ML_RUNGS,
    RUNTIME_MARGIN,
    QueueEntry,
    QueueError,
    StudyQueue,
    check_weekly_cap,
    enqueue,
    eta_rows,
    load_queue,
    update_queue,
)
from arc.backtest.study import RETIRED_FILE, RetiredStudy, retired_from_text
from arc.backtest.study_adopt import (
    AdoptStudyError,
    AdoptStudyResult,
    check_not_retired,
    weekly_cap_or_raise,
)
from arc.experiments.models import document_hash
from arc.utils.yamlpatch import deep_merge

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from arc.backtest.study import LedgerEntry
    from arc.experiments.publish import MainView, Publisher

__all__ = [
    "FAMILY_RULE",
    "SCRATCH_ID",
    "SPECS_DIR",
    "M1Overlay",
    "MlLedgerEntry",
    "MlStudySpec",
    "RungOutcome",
    "adopt_ml_study",
    "best_variant",
    "clear_dev_fail",
    "drop_ml_study",
    "load_ml_ledger",
    "load_ml_retired",
    "load_ml_spec",
    "ml_estimate_s",
    "ml_rung_estimates",
    "ml_rung_line",
    "ml_study_hash",
    "resolve_spec",
    "run_ladder",
    "run_rung",
    "variants",
]

log = structlog.get_logger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SPECS_DIR = "config/ml_studies"
STUDIES_DIR = REPO_ROOT / SPECS_DIR
DEFAULT_LEDGER = Path("data/ml/studies/ledger.jsonl")
DEFAULT_RUNS_DIR = Path("data/ml/studies")
DEFAULT_MODELS = Path("data/ml/models")
#: never adopted or queued: the id of a scratch spec run with ``--spec``
SCRATCH_ID = "ML-0"
#: the family -> its registered decision rule (a new rule kind needs a card + a D# row)
FAMILY_RULE: dict[str, str] = {"m1": "m1_keep", "credit": "m2_promote", "debit": "m2_promote"}
#: the fixed inner selection metric per family (lower is better)
SELECTION_METRIC: dict[str, str] = {
    "m1": "inner_pinball",
    "credit": "inner_win_log_loss",
    "debit": "inner_win_log_loss",
}
RUNG_YEAR: dict[str, int] = {"dev_2022": 2022, "dev_2023": 2023}
HOLDOUT_START = dt.date(2024, 1, 1)
RECENT_ROWS = 5
_ID_RE = re.compile(r"^ML-(0|[1-9]\d*)$")
_FORBID = ConfigDict(extra="forbid", frozen=True)
#: M1 LightGBM params a study may tune; objective, seed, determinism and threads stay fixed
M1_TUNABLE: frozenset[str] = frozenset(
    {
        "learning_rate",
        "num_leaves",
        "min_data_in_leaf",
        "feature_fraction",
        "bagging_fraction",
        "bagging_freq",
        "lambda_l2",
        "max_bin",
    }
)
_M1_TARGET_RE = re.compile(r"^(log_ret|rv)(_h(5|10|21|42))?$")

ChangeKind = Literal["challenger", "tuning", "features", "calibration"]
Rung = Literal["dev_2022", "dev_2023", "verdict"]


class MlStudyError(RuntimeError):
    """A study run was refused (spec, window, holdout); nothing was written."""


# ---------------------------------------------------------------------------
# Spec
# ---------------------------------------------------------------------------


class FeatureDelta(BaseModel):
    model_config = _FORBID

    add: list[str] = Field(default_factory=list)
    drop: list[str] = Field(default_factory=list)


class M1Calibration(BaseModel):
    """``annual`` = the pre-registered CQR (offsets once, on the 252 sessions before each
    test year); ``rolling_monthly`` = offsets refit at each test month on the last
    *window_sessions* sessions of OOS predictions whose labels end before it (purge h + 5)."""

    model_config = _FORBID

    method: Literal["annual", "rolling_monthly"] = "annual"
    window_sessions: int = Field(default=252, ge=63, le=756)


class M1Overlay(BaseModel):
    """The typed M1 training-config overlay (on the incumbent's manifest config)."""

    model_config = _FORBID

    params: dict[str, float | int] = Field(default_factory=dict)
    rounds: int | None = Field(None, ge=10, le=5000)
    features: FeatureDelta = Field(default_factory=FeatureDelta)
    calibration: M1Calibration = Field(default_factory=M1Calibration)
    #: LightGBM serves only these targets (``rv``, ``log_ret``, ``rv_h21`` ...); the rest
    #: stay on their baseline. Empty = every target.
    targets: list[str] = Field(default_factory=list)

    @field_validator("params")
    @classmethod
    def _params(cls, v: dict[str, float | int]) -> dict[str, float | int]:
        bad = sorted(set(v) - M1_TUNABLE)
        if bad:
            msg = f"m1 params {bad} are not tunable (allowed: {sorted(M1_TUNABLE)})"
            raise ValueError(msg)
        return v

    @field_validator("targets")
    @classmethod
    def _targets(cls, v: list[str]) -> list[str]:
        bad = [t for t in v if not _M1_TARGET_RE.match(t)]
        if bad:
            msg = f"m1 targets {bad}: use log_ret|rv, optionally _h5|_h10|_h21|_h42"
            raise ValueError(msg)
        return v

    def target_set(self, horizons: Sequence[int]) -> list[tuple[str, int]] | None:
        """(kind, h) LightGBM may serve; None = all."""
        if not self.targets:
            return None
        out: list[tuple[str, int]] = []
        for t in self.targets:
            m = _M1_TARGET_RE.match(t)
            assert m is not None  # noqa: S101 - validated
            hs = [int(m.group(3))] if m.group(3) else list(horizons)
            out += [(m.group(1), h) for h in hs if (m.group(1), h) not in out]
        return out

    def extra(self) -> dict[str, Any] | None:
        """The overlay beyond params/rounds for the version id (None when default)."""
        doc = self.model_dump(mode="json", exclude_defaults=True)
        doc.pop("params", None)
        doc.pop("rounds", None)
        return doc or None


class StudyValue(BaseModel):
    """The Analyst's ranking inputs (the D86 value formula with compute nights)."""

    model_config = _FORBID

    expected_effect: dict[str, Any] | None = None
    affected_share: float | None = Field(None, gt=0, le=1)
    p_success: float | None = Field(None, gt=0, le=1)
    nights_needed: int | None = Field(None, ge=1)
    value_score: float | None = None
    score_override: dict[str, Any] | None = None


class DecisionRule(BaseModel):
    model_config = _FORBID

    kind: str


class MlStudySpec(BaseModel):
    """One ``ML-<n>`` study (E27.7). The canonical-JSON SHA-256 is its lock."""

    model_config = _FORBID

    id: str
    hypothesis: str = Field(..., min_length=1)
    owner: str = Field(..., min_length=1)
    created: dt.date
    family: Literal["m1", "credit", "debit"]
    #: a registered version, or ``champion`` (resolved and pinned at adopt)
    incumbent: str = Field(..., min_length=1)
    change_kind: ChangeKind
    overlay: dict[str, Any] = Field(default_factory=dict)
    grid: list[dict[str, Any]] = Field(default_factory=list, max_length=48)
    decision_rule: DecisionRule
    study_value: StudyValue | None = None

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        if not _ID_RE.match(v):
            msg = f"study id {v!r}: use ML-<n> (ML-0 = a scratch spec)"
            raise ValueError(msg)
        return v

    @model_validator(mode="after")
    def _rule_and_overlays(self) -> MlStudySpec:
        want = FAMILY_RULE[self.family]
        if self.decision_rule.kind != want:
            msg = (
                f"decision_rule {self.decision_rule.kind!r} is not registered for family "
                f"{self.family} (its rule is {want!r}; a new rule kind needs a card + D#)"
            )
            raise ValueError(msg)
        for i, ov in enumerate(variants(self)):
            where = "overlay" if not self.grid else f"grid[{i}]"
            check_overlay(self.family, ov, where)
        return self


def variants(spec: MlStudySpec) -> list[dict[str, Any]]:
    """Each grid item laid on the overlay (the overlay alone without a grid)."""
    if not spec.grid:
        return [dict(spec.overlay)]
    return [deep_merge(spec.overlay, g) for g in spec.grid]


def _feature_names() -> set[str]:
    from arc.ml.features.registry import FEATURES

    return {f.name for f in FEATURES}


def check_overlay(family: str, overlay: Mapping[str, Any], where: str = "overlay") -> None:
    """Validate *overlay* against the family's training-config model; features must be
    in the E27.1 registry. Raises ``ValueError``."""
    names = _feature_names()
    try:
        if family == "m1":
            ov = M1Overlay.model_validate(dict(overlay))
            feats = [*ov.features.add, *ov.features.drop]
        else:
            from arc.ml.m2.config import M2Config, apply_overlay

            cfg = apply_overlay(M2Config(), dict(overlay))
            feats = list(cfg.add_features)
    except ValueError as exc:
        msg = f"{where}: {exc}"
        raise ValueError(msg) from exc
    bad = sorted(set(feats) - names)
    if bad:
        msg = f"{where}: features {bad} are not in the feature registry (FEATURES)"
        raise ValueError(msg)


def ml_study_hash(spec: MlStudySpec) -> str:
    return document_hash(spec.model_dump(mode="json"))


def load_ml_spec(path: Path) -> MlStudySpec:
    """A spec file; its id must match the file name (``ML-<n>.yaml``)."""
    try:
        spec = MlStudySpec.model_validate(yaml.safe_load(path.read_text()) or {})
    except (OSError, yaml.YAMLError) as exc:
        msg = f"{path}: {exc}"
        raise ValueError(msg) from exc
    if spec.id != path.stem and spec.id != SCRATCH_ID:
        msg = f"{path}: id {spec.id} != file name {path.stem}"
        raise ValueError(msg)
    return spec


# ---------------------------------------------------------------------------
# Incumbent
# ---------------------------------------------------------------------------


def _manifest(models_root: Path, family: str, version: str) -> dict[str, Any]:
    p = models_root / family / version / "manifest.json"
    if not p.is_file():
        msg = f"{family}/{version}: no manifest.json under {models_root}"
        raise ValueError(msg)
    return json.loads(p.read_text())


def resolve_spec(spec: MlStudySpec, models_root: Path, *, max_grid: int) -> MlStudySpec:
    """The checks that need the models dir and config: the family has a model, the
    incumbent exists (``champion`` resolved and pinned), the grid is within *max_grid*."""
    from arc.ml.registry import load_index

    if len(spec.grid) > max_grid:
        msg = f"{spec.id}: grid of {len(spec.grid)} variants > ml_studies.max_grid {max_grid}"
        raise ValueError(msg)
    fam_dir = models_root / spec.family
    if not any(fam_dir.glob("*/manifest.json")):
        msg = f"{spec.id}: family {spec.family} has no model under {models_root}"
        raise ValueError(msg)
    inc = spec.incumbent
    if inc == "champion":
        champ = load_index(models_root, spec.family).champion
        if champ is None:
            msg = f"{spec.id}: {spec.family} has no champion; name the incumbent version"
            raise ValueError(msg)
        inc = champ
    _manifest(models_root, spec.family, inc)
    return spec.model_copy(update={"incumbent": inc}) if inc != spec.incumbent else spec


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------


class MlLedgerEntry(BaseModel):
    """One ``data/ml/studies/ledger.jsonl`` line: a finished rung (or a drop)."""

    model_config = ConfigDict(extra="forbid")

    study: str
    spec_hash: str
    family: str
    incumbent: str
    rung: Literal["dev_2022", "dev_2023", "verdict", "dropped"]
    at: dt.datetime
    rule: str
    #: per variant: index, overlay, selection metric, the rule result for the year
    variants: list[dict[str, Any]] = Field(default_factory=list)
    best_variant: int | None = None
    rung_pass: bool | None = None
    clear_fail: bool = False
    version: str | None = None
    rule_passed: bool | None = None
    verdict: Literal["pass", "fail", "dropped"] | None = None
    status: str | None = None
    rule_years: dict[str, Any] = Field(default_factory=dict)
    vs_incumbent: dict[str, Any] = Field(default_factory=dict)
    runtime_s: float | None = None
    git_sha: str = ""
    note: str = ""


def load_ml_ledger(path: Path) -> list[MlLedgerEntry]:
    if not path.is_file():
        return []
    return [
        MlLedgerEntry.model_validate_json(line)
        for line in path.read_text().splitlines()
        if line.strip()
    ]


def append_ml_ledger(path: Path, entry: MlLedgerEntry) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(entry.model_dump_json() + "\n")


def _rows(ledger: Sequence[MlLedgerEntry], spec_id: str, h: str) -> list[MlLedgerEntry]:
    return [e for e in ledger if e.study == spec_id and e.spec_hash == h]


# ---------------------------------------------------------------------------
# Retired
# ---------------------------------------------------------------------------


def load_ml_retired(studies_dir: Path | None = None) -> dict[str, RetiredStudy]:
    p = (studies_dir or STUDIES_DIR) / RETIRED_FILE
    return ml_retired_from_text(p.read_text() if p.is_file() else None, str(p))


def ml_retired_from_text(text: str | None, where: str = RETIRED_FILE) -> dict[str, RetiredStudy]:
    out = retired_from_text(text, where)
    bad = sorted(k for k in out if not k.startswith("ML-"))
    if bad:
        msg = f"{where}: {bad} are not ML-<n> ids"
        raise ValueError(msg)
    return out


def _retired(studies_dir: Path, main: MainView | None) -> dict[str, RetiredStudy]:
    out = load_ml_retired(studies_dir)
    if main is not None:
        path = f"{SPECS_DIR}/{RETIRED_FILE}"
        out = {**ml_retired_from_text(main.files(SPECS_DIR).get(path), f"main:{path}"), **out}
    return out


# ---------------------------------------------------------------------------
# Estimates (fit-or-defer, ETA)
# ---------------------------------------------------------------------------


def ml_estimate_s(
    ledger: Sequence[MlLedgerEntry],
    spec: MlStudySpec,
    rung: str,
    *,
    default_s: float,
    margin: float = RUNTIME_MARGIN,
) -> float:
    """Seconds a rung of *spec* should take: the median per-variant runtime of recent
    ML ledger lines of the same family and rung x variants x *margin*; the verdict is
    one variant end to end (two folds + the final fit, ~3 rungs' work)."""
    n = 1 if rung == "verdict" else len(variants(spec))
    rows = sorted(
        (e for e in ledger if e.family == spec.family and e.rung == rung and e.runtime_s),
        key=lambda e: e.at,
    )[-RECENT_ROWS:]
    per = [
        float(e.runtime_s or 0) / (1 if rung == "verdict" else max(1, len(e.variants)))
        for e in rows
    ]
    base = statistics.median(per) if per else default_s * (3 if rung == "verdict" else 1)
    return round(base * n * margin)


def ml_rung_estimates(
    ledger: Sequence[MlLedgerEntry], spec: MlStudySpec, *, default_s: float
) -> dict[str, dict[str, Any]]:
    return {r: {"seconds": ml_estimate_s(ledger, spec, r, default_s=default_s)} for r in ML_RUNGS}


def queue_ml_estimates(
    queue: StudyQueue,
    ledger: Sequence[MlLedgerEntry],
    specs: Mapping[str, MlStudySpec],
    *,
    default_s: float,
) -> dict[str, dict[str, Any]]:
    """``ML-<n> -> rung estimates`` for every queued ML entry whose spec is known."""
    return {
        e.id: ml_rung_estimates(ledger, specs[e.id], default_s=default_s)
        for e in queue.entries
        if e.kind == "ml" and e.id in specs
    }


# ---------------------------------------------------------------------------
# Family adapters
# ---------------------------------------------------------------------------


@dataclass
class StudyEnv:
    """Where a rung reads and writes."""

    data_dir: Path
    models_root: Path
    ledger: Path
    runs_dir: Path
    code_sha: str = ""
    workers: int = 6
    say: Callable[[str], None] = lambda _m: None
    #: the window check between variants (True = yield now; the rung leaves no line)
    stop: Callable[[], bool] = lambda: False


@dataclass
class RungOutcome:
    """A variant's result on one dev rung."""

    index: int
    overlay: dict[str, Any]
    selection: float
    passed: bool
    miss: float
    detail: dict[str, Any] = field(default_factory=dict)
    seconds: float = 0.0


def _check_dev(year: int) -> None:
    """E27.3's guard, applied before any study reads data: dev years only (D84/D133)."""
    from arc.ml.m1.dataset import HoldoutReadError

    if dt.date(year, 12, 31) >= HOLDOUT_START:
        msg = f"an ML study reads dev only: {year} reaches the holdout (≥ {HOLDOUT_START})"
        raise HoldoutReadError(msg)


class _M1:
    """M1 (``m1_keep``): LightGBM keeps a target when, in the year, its calibrated pinball
    beats the baseline with one-sided DM p < 0.05 and post-CQR coverage error ≤ 3 pts.
    The study passes a year when every target in its subset keeps."""

    def __init__(self, spec: MlStudySpec, env: StudyEnv) -> None:
        self.spec = spec
        self.env = env
        man = _manifest(env.models_root, "m1", spec.incumbent)
        self.base_params: dict[str, Any] = dict(man["lgb_params"])
        self.base_rounds = int(man["num_rounds"])
        self.base_features: list[str] = list(man["features"])
        self._df: Any = None

    def config(self, overlay: Mapping[str, Any]) -> dict[str, Any]:
        from arc.ml.features.compute import M1_HORIZONS

        ov = M1Overlay.model_validate(dict(overlay))
        feats = [f for f in self.base_features if f not in set(ov.features.drop)]
        feats += [f for f in ov.features.add if f not in feats]
        return {
            "params": {**self.base_params, **ov.params},
            "rounds": ov.rounds or self.base_rounds,
            "features": feats,
            "calibration": ov.calibration.method,
            "cal_sessions": ov.calibration.window_sessions,
            "lgbm_targets": ov.target_set(M1_HORIZONS),
            "extra": ov.extra(),
        }

    def df(self) -> Any:
        if self._df is None:
            from arc.ml.m1.train import load_dev_dataset

            self._df = load_dev_dataset(self.env.data_dir, self.env.data_dir / "ml" / "features")
        return self._df

    def _subset(self, cfg: dict[str, Any]) -> list[tuple[str, int]]:
        from arc.ml.features.compute import M1_HORIZONS
        from arc.ml.m1.predict import KINDS

        return cfg["lgbm_targets"] or [(k, h) for k in KINDS for h in M1_HORIZONS]

    def dev(self, i: int, overlay: dict[str, Any], year: int) -> RungOutcome:
        from arc.ml.m1.train import FOLDS, keep_decisions, metric_rows, run_fold

        _check_dev(year)
        t0 = time.monotonic()
        cfg = self.config(overlay)
        plan = next(p for p in FOLDS if p.name == f"test{year}")
        res, _ = run_fold(
            self.df(),
            plan,
            cfg["features"],
            params=cfg["params"],
            rounds=cfg["rounds"],
            calibration=cfg["calibration"],
            cal_sessions=cfg["cal_sessions"],
            workers=self.env.workers,
            lgbm_targets=cfg["lgbm_targets"],
            progress=self.env.say,
        )
        table = metric_rows(res)
        dec = keep_decisions(table)
        subset = self._subset(cfg)
        targets: dict[str, Any] = {}
        miss = -1.0
        for kind, h in subset:
            row = dec[(dec.kind == kind) & (dec.h == h)].iloc[0]
            t = table[(table.kind == kind) & (table.h == h) & (table.split == "All")]
            lg = t[t.model == "lgbm"]
            bs = t[t.model == "baseline"].iloc[0]
            pb = float(lg.pinball.iloc[0]) if len(lg) else float("nan")
            gap = pb / float(bs.pinball) - 1 if len(lg) else float("inf")
            miss = max(miss, gap)
            targets[f"{kind}_h{h}"] = {
                "pass": bool(row[f"{plan.name}_pass"]),
                "detail": str(row[f"{plan.name}_detail"]),
                "pinball": round(pb, 6),
                "pinball_baseline": round(float(bs.pinball), 6),
                "cov_err": round(float(lg.cov_err.iloc[0]), 5) if len(lg) else None,
                "inner_pinball": round(res.inner_pinball.get((kind, h), float("nan")), 6),
            }
        sel = [res.inner_pinball[t] for t in subset if t in res.inner_pinball]
        return RungOutcome(
            index=i,
            overlay=overlay,
            selection=float(statistics.fmean(sel)) if sel else float("inf"),
            passed=all(v["pass"] for v in targets.values()),
            miss=miss,
            detail={"targets": targets},
            seconds=time.monotonic() - t0,
        )

    def verdict(self, overlay: dict[str, Any], now: dt.datetime) -> dict[str, Any]:
        from arc.ml import registry as reg
        from arc.ml.m1.report import render_card
        from arc.ml.m1.train import train_m1

        cfg = self.config(overlay)
        out = self.env.models_root / "m1"
        run = train_m1(
            self.env.data_dir,
            self.env.data_dir / "ml" / "features",
            out,
            code_sha=self.env.code_sha,
            params=cfg["params"],
            rounds=cfg["rounds"],
            features=cfg["features"],
            calibration=cfg["calibration"],
            cal_sessions=cfg["cal_sessions"],
            lgbm_targets=cfg["lgbm_targets"],
            extra=cfg["extra"],
            study=self.spec.id,
            workers=self.env.workers,
            progress=self.env.say,
            df=self.df(),
        )
        (run.model_dir / "model_card.md").write_text(render_card(run.model_dir))
        dec = run.decisions
        years: dict[str, Any] = {}
        for kind, h in self._subset(cfg):
            row = dec[(dec.kind == kind) & (dec.h == h)].iloc[0]
            for y in RUNG_YEAR.values():
                d = years.setdefault(str(y), {"pass": True, "targets": {}})
                ok = bool(row[f"test{y}_pass"])
                d["pass"] &= ok
                d["targets"][f"{kind}_h{h}"] = {"pass": ok, "detail": str(row[f"test{y}_detail"])}
        passed = all(d["pass"] for d in years.values())
        entry = reg.register(
            self.env.models_root,
            "m1",
            run.manifest.version,
            now=now,
            rule="m1_keep",
            rule_passed=passed,
            feature_schema=run.manifest.feature_schema,
            summary={
                "study": self.spec.id,
                "rule_passed": passed,
                "kept": [f"{r.kind}_h{r.h}" for r in dec.itertuples() if r.source == "lgbm"],
            },
        )
        return {
            "version": run.manifest.version,
            "rule_passed": passed,
            "status": entry.status,
            "years": years,
        }

    def incumbent_metric(self, year: int, target: str) -> float | None:
        p = self.env.models_root / "m1" / self.spec.incumbent / "eval_metrics.csv"
        dec = self.env.models_root / "m1" / self.spec.incumbent / "eval_decisions.csv"
        if not (p.is_file() and dec.is_file()):
            return None
        import pandas as pd

        kind, h = target.rsplit("_h", 1)
        d = pd.read_csv(dec)
        src = d[(d.kind == kind) & (d.h == int(h))]
        model = str(src.source.iloc[0]) if len(src) else "baseline"
        t = pd.read_csv(p)
        r = t[
            (t.fold == f"test{year}")
            & (t.kind == kind)
            & (t.h == int(h))
            & (t.split == "All")
            & (t.model == model)
        ]
        return float(r.pinball.iloc[0]) if len(r) else None


class _M2:
    """M2 (``m2_promote``): per test year the win log-loss beats the logistic and the
    managed-PoP baselines, the e_r IC is > 0 with NW t > 2, and the ``ml_er`` replay beats
    the incumbent ranking on net P&L and max drawdown. Dev rungs judge the model legs
    (log-loss, IC); the replay leg needs the fold models' scores over the year's sims and
    runs once, in the verdict, on the selected variant."""

    def __init__(self, spec: MlStudySpec, env: StudyEnv) -> None:
        from arc.ml.m2.config import M2Config

        self.spec = spec
        self.env = env
        man = _manifest(env.models_root, spec.family, spec.incumbent)
        self.base = M2Config.model_validate(man["config"])

    def config(self, overlay: Mapping[str, Any]) -> Any:
        from arc.ml.m2.config import apply_overlay

        return apply_overlay(self.base, dict(overlay))

    def dev(self, i: int, overlay: dict[str, Any], year: int) -> RungOutcome:
        import tempfile

        from arc.ml import validate as v
        from arc.ml.m2.train import train_family

        _check_dev(year)
        t0 = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="ml-study-") as tmp:
            run = train_family(
                self.spec.family,
                self.config(overlay),
                ledger_root=self.env.data_dir / "ml" / "ledger",
                features_root=self.env.data_dir / "ml" / "features",
                out_root=Path(tmp),
                code_sha=self.env.code_sha,
                now=dt.datetime(year, 12, 31, tzinfo=dt.UTC),
                folds=v.walk_forward((year,)),
                monotone_check=False,
                progress=self.env.say,
                final_fit=False,
            )
        c = run.checks.iloc[0]
        inner = [
            float(t["value"])
            for t in run.trials
            if t.get("target") == "win" and t.get("metric") != "test_ic"
        ]
        bar = min(float(c.ll_logistic), float(c.ll_managed_pop))
        passed = bool(c.beats_logistic and c.beats_managed_pop and c.ic_pass)
        return RungOutcome(
            index=i,
            overlay=overlay,
            selection=min(inner) if inner else float("inf"),
            passed=passed,
            miss=float(c.ll_m2) / bar - 1,
            detail={
                "win_log_loss": round(float(c.ll_m2), 6),
                "ll_logistic": round(float(c.ll_logistic), 6),
                "ll_managed_pop": round(float(c.ll_managed_pop), 6),
                "er_ic": round(float(c.er_ic), 5),
                "er_ic_t": round(float(c.er_ic_t), 3),
                "legs": "log-loss + IC (replay in the verdict)",
            },
            seconds=time.monotonic() - t0,
        )

    def verdict(self, overlay: dict[str, Any], now: dt.datetime) -> dict[str, Any]:
        import pandas as pd

        from arc.ml import registry as reg
        from arc.ml.m2.pipeline import dev_run

        out = dev_run(
            [self.spec.family],
            self.config(overlay),
            data_dir=self.env.data_dir,
            out_root=self.env.models_root,
            code_sha=self.env.code_sha,
            now=now,
            workers=self.env.workers,
            say=self.env.say,
            manifest_extra={"study": self.spec.id, "overlay": overlay or None},
        )[self.spec.family]
        rule = pd.read_csv(Path(out["vdir"]) / "eval_rule.csv")
        years = {
            str(r.fold).removeprefix("test"): {
                "pass": bool(r.year_pass),
                "ll_m2": float(r.ll_m2),
                "er_ic": float(r.er_ic),
                "replay_pass": bool(r.replay_pass),
            }
            for r in rule.itertuples()
        }
        status = reg.load_index(self.env.models_root, self.spec.family).versions[out["version"]]
        return {
            "version": out["version"],
            "rule_passed": bool(out["rule_passed"]),
            "status": status.status,
            "years": years,
        }

    def incumbent_metric(self, year: int, _target: str) -> float | None:
        p = self.env.models_root / self.spec.family / self.spec.incumbent / "eval_metrics.csv"
        if not p.is_file():
            return None
        import pandas as pd

        t = pd.read_csv(p)
        r = t[(t.fold == f"test{year}") & (t.split == "All") & (t.group == "all")]
        return float(r.win_log_loss.iloc[0]) if len(r) else None


def adapter(spec: MlStudySpec, env: StudyEnv) -> _M1 | _M2:
    return _M1(spec, env) if spec.family == "m1" else _M2(spec, env)


# ---------------------------------------------------------------------------
# Rungs
# ---------------------------------------------------------------------------


def best_variant(lines: Sequence[MlLedgerEntry]) -> int:
    """The variant with the lowest mean inner selection metric over the dev rungs
    (ties: the lower index). Never the test-year result."""
    by: dict[int, list[float]] = {}
    for e in lines:
        for v in e.variants:
            by.setdefault(int(v["index"]), []).append(float(v["selection"]))
    if not by:
        msg = "no dev rung lines to select from"
        raise MlStudyError(msg)
    return min(by, key=lambda i: (statistics.fmean(by[i]), i))


def clear_dev_fail(line: MlLedgerEntry, fail_margin: float) -> bool:
    """``dev_2022`` fails the rule on every variant by more than *fail_margin*."""
    return (
        line.rung == "dev_2022"
        and bool(line.variants)
        and all(not v["passed"] and float(v["miss"]) > fail_margin for v in line.variants)
    )


def _vs_incumbent(ad: _M1 | _M2, spec: MlStudySpec, out: RungOutcome, year: int) -> dict[str, Any]:
    if spec.family == "m1":
        rel = []
        cov = []
        for t, d in out.detail["targets"].items():
            inc = ad.incumbent_metric(year, t)
            if inc and d["pinball"] == d["pinball"]:  # not NaN
                rel.append(d["pinball"] / inc - 1)
            if d["cov_err"] is not None:
                cov.append(d["cov_err"])
        return {
            "pinball_rel": round(statistics.fmean(rel), 4) if rel else None,
            "cov_err_max": round(max(cov), 4) if cov else None,
        }
    inc = ad.incumbent_metric(year, "")
    ll = out.detail["win_log_loss"]
    return {
        "log_loss_rel": round(ll / inc - 1, 4) if inc else None,
        "er_ic": out.detail["er_ic"],
    }


def run_rung(
    spec: MlStudySpec,
    rung: str,
    env: StudyEnv,
    *,
    now: dt.datetime,
    fail_margin: float,
) -> MlLedgerEntry | None:
    """Run one rung and append its ledger line; None when *env.stop* asked to yield
    (no line: the rung resumes next time)."""
    if rung not in ML_RUNGS:
        msg = f"{rung!r}: ML rungs are {ML_RUNGS} (no holdout rung: D133)"
        raise MlStudyError(msg)
    h = ml_study_hash(spec)
    t0 = time.monotonic()
    ad = adapter(spec, env)
    base = {
        "study": spec.id,
        "spec_hash": h,
        "family": spec.family,
        "incumbent": spec.incumbent,
        "rung": rung,
        "at": now,
        "rule": spec.decision_rule.kind,
        "git_sha": env.code_sha,
    }
    if rung in RUNG_YEAR:
        year = RUNG_YEAR[rung]
        outs: list[RungOutcome] = []
        for i, ov in enumerate(variants(spec)):
            if env.stop():
                env.say(f"{spec.id} {rung}: yielding to the runner window; no ledger line")
                return None
            env.say(f"{spec.id} {rung}: variant {i + 1}/{len(variants(spec))}")
            outs.append(ad.dev(i, ov, year))
        best = min(outs, key=lambda o: (o.selection, o.index))
        line = MlLedgerEntry(
            **base,
            variants=[
                {
                    "index": o.index,
                    "overlay": o.overlay,
                    "selection_metric": SELECTION_METRIC[spec.family],
                    "selection": round(o.selection, 8),
                    "passed": o.passed,
                    "miss": round(o.miss, 6),
                    "seconds": round(o.seconds, 1),
                    **o.detail,
                }
                for o in outs
            ],
            best_variant=best.index,
            rung_pass=best.passed,
            vs_incumbent=_vs_incumbent(ad, spec, best, year),
            runtime_s=round(time.monotonic() - t0, 1),
        )
        line.clear_fail = clear_dev_fail(line, fail_margin)
    else:
        dev = [e for e in _rows(load_ml_ledger(env.ledger), spec.id, h) if e.rung in RUNG_YEAR]
        if {e.rung for e in dev} != set(RUNG_YEAR):
            msg = f"{spec.id}: the verdict needs both dev rungs on the ledger first"
            raise MlStudyError(msg)
        if env.stop():
            env.say(f"{spec.id} verdict: yielding to the runner window; no ledger line")
            return None
        i = best_variant(dev)
        ov = variants(spec)[i]
        res = ad.verdict(ov, now)
        line = MlLedgerEntry(
            **base,
            variants=[{"index": i, "overlay": ov}],
            best_variant=i,
            version=res["version"],
            rule_passed=res["rule_passed"],
            verdict="pass" if res["rule_passed"] else "fail",
            status=res["status"],
            rule_years=res["years"],
            runtime_s=round(time.monotonic() - t0, 1),
        )
        sdir = env.runs_dir / spec.id
        sdir.mkdir(parents=True, exist_ok=True)
        (sdir / "verdict.json").write_text(
            json.dumps(line.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
        )
    append_ml_ledger(env.ledger, line)
    log.info("ml.study.rung", study=spec.id, rung=rung, line=ml_rung_line(line))
    return line


def run_ladder(
    spec: MlStudySpec,
    env: StudyEnv,
    *,
    clock: Callable[[], dt.datetime],
    fail_margin: float,
) -> list[MlLedgerEntry]:
    """Every rung not yet on the ledger, in order (the scratch / manual path); stops on
    a clear dev fail or when *env.stop* asks to yield."""
    h = ml_study_hash(spec)
    done = {e.rung for e in _rows(load_ml_ledger(env.ledger), spec.id, h)}
    out: list[MlLedgerEntry] = []
    for rung in ML_RUNGS:
        if rung in done:
            continue
        line = run_rung(spec, rung, env, now=clock(), fail_margin=fail_margin)
        if line is None:
            break
        out.append(line)
        env.say(ml_rung_line(line))
        if line.clear_fail:
            break
    return out


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{100 * x:+.0f}%".replace("-", "−")


def ml_rung_line(e: MlLedgerEntry) -> str:
    """``ML-3 · dev 2023 · pass · pinball −18% vs incumbent, cov err 2.4 pts`` (+ status)."""
    if e.rung == "verdict":
        status = (e.status or "candidate").title()
        return (
            f"{e.study} · verdict · {e.verdict} · {e.version} (variant {e.best_variant}) → {status}"
        )
    if e.rung == "dropped":
        return f"{e.study} · dropped · {e.note}"
    year = e.rung.removeprefix("dev_")
    ok = "pass" if e.rung_pass else "fail"
    vs = e.vs_incumbent
    if e.family == "m1":
        cov = vs.get("cov_err_max")
        body = f"pinball {_pct(vs.get('pinball_rel'))} vs incumbent" + (
            f", cov err {100 * cov:.1f} pts" if cov is not None else ""
        )
    else:
        body = f"log-loss {_pct(vs.get('log_loss_rel'))} vs incumbent, e_r IC {vs.get('er_ic')}"
    n = len(e.variants)
    pick = f" · best of {n} (inner)" if n > 1 else ""
    return f"{e.study} · dev {year} · {ok} · {body}{pick}"


# ---------------------------------------------------------------------------
# Adopt / drop
# ---------------------------------------------------------------------------


def spec_file_text(spec: MlStudySpec, *, approval: str) -> str:
    h = ml_study_hash(spec)
    head = (
        f"# {spec.id}: Arc Analyst draft ML study, committed by `arc ml study adopt` on the\n"
        f"# owner's approval {approval} (E27.7, PLAN D133). Hash-locked: an edit is a new\n"
        f"# spec hash. spec sha256 {h}\n"
    )
    doc = spec.model_dump(mode="json", exclude_defaults=True)
    text = head + yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, width=88)
    again = MlStudySpec.model_validate(yaml.safe_load(text))
    if ml_study_hash(again) != h:  # pragma: no cover - guards a serialisation change
        msg = f"{spec.id}: the spec file does not re-parse to the locked hash"
        raise AdoptStudyError(msg)
    return text


def study_pr_body(spec: MlStudySpec, h: str, approval: str) -> str:
    return (
        f"XP-advisory: none — {spec.id} is an offline ML study spec (E27.7, PLAN D133)\n\n"
        f"`arc ml study adopt` commits **{spec.id}** ({spec.hypothesis}), drafted by "
        f"{spec.owner}, on the owner's approval `{approval}` in #arc-analyst.\n\n"
        f"- family {spec.family}; incumbent `{spec.incumbent}`; {spec.change_kind}; "
        f"{len(variants(spec))} variant(s); rule {spec.decision_rule.kind}\n"
        f"- spec sha256 `{h}` (the pre-registration lock)\n\n"
        "Auto-merged once CI is green. The nightly study runner runs it after the merge: "
        "dev 2022 -> dev 2023 -> verdict (dev only; a pass moves the model to shadow). "
        "Nothing here changes a live decision.\n"
    )


def _committed(study_id: str, studies_dir: Path, main: MainView | None) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    local = studies_dir / f"{study_id}.yaml"
    if local.is_file():
        out.append((str(local), ml_study_hash(load_ml_spec(local))))
    if main is not None:
        path = f"{SPECS_DIR}/{study_id}.yaml"
        text = main.files(SPECS_DIR).get(path)
        if text is not None:
            spec = MlStudySpec.model_validate(yaml.safe_load(text) or {})
            out.append((f"origin/main:{path}", ml_study_hash(spec)))
    return out


def adopt_ml_study(  # noqa: PLR0913
    study_id: str,
    spec_path: Path,
    *,
    approval: str,
    now: dt.datetime,
    queue_path: Path,
    ledger: Path,
    models_root: Path,
    max_grid: int = 12,
    default_rung_s: float = 2400,
    studies_dir: Path | None = None,
    main: MainView | None = None,
    publisher: Publisher | None = None,
    expect_hash: str | None = None,
    dry_run: bool = False,
    dry_run_pr: bool = False,
    bt_ledger: Sequence[LedgerEntry] = (),
) -> AdoptStudyResult:
    """``approve ML-<n>``: validate the draft (incumbent pinned), commit it through an
    auto-merged PR and queue it (``kind: ml``) under the shared weekly cap. Mirrors
    :func:`arc.backtest.study_adopt.adopt_study` and reuses its retired / cap checks."""
    approval = (approval or "").strip()
    if not APPROVAL_RE.match(approval):
        msg = (
            f"invalid approval {approval!r}: an ML study adoption needs the owner's Slack "
            "approval, --approval slack:<ts> (the `approve ML-<n>` message, D133)"
        )
        raise AdoptStudyError(msg)
    if study_id == SCRATCH_ID:
        msg = f"{SCRATCH_ID} is the scratch id: run it with `arc ml study run --spec`"
        raise AdoptStudyError(msg)
    if not spec_path.is_file():
        msg = f"no draft spec {spec_path}"
        raise AdoptStudyError(msg)
    if spec_path.stem != study_id:
        msg = f"{spec_path}: the draft's file name must be {study_id}.yaml"
        raise AdoptStudyError(msg)
    try:
        spec = resolve_spec(load_ml_spec(spec_path), models_root, max_grid=max_grid)
    except ValueError as exc:
        msg = f"{study_id}: the draft does not validate as an MlStudySpec: {exc}"
        raise AdoptStudyError(msg) from exc
    h = ml_study_hash(spec)
    if expect_hash and expect_hash != h:
        msg = (
            f"{study_id}: hash mismatch: the draft is {h[:12]} but the approval was recorded "
            f"for {expect_hash[:12]}; a changed design needs a new ML id"
        )
        raise AdoptStudyError(msg)
    queued = load_queue(queue_path).get(study_id)
    sdir = studies_dir or STUDIES_DIR
    try:
        retired = _retired(sdir, main)
    except ValueError as exc:
        msg = f"cannot read the retired ML study list: {exc}"
        raise AdoptStudyError(msg) from exc
    check_not_retired(study_id, retired, queued=queued is not None)
    if queued is not None and queued.spec_hash != h:
        msg = (
            f"{study_id}: hash mismatch: queued with spec {queued.spec_hash[:12]} "
            f"({queued.status}), this draft is {h[:12]}; a changed design needs a new ML id"
        )
        raise AdoptStudyError(msg)
    if queued is None:
        rows = [e for e in load_ml_ledger(ledger) if e.study == study_id]
        if rows:
            msg = (
                f"{study_id} is taken: the ML study ledger already has {len(rows)} line(s) "
                "of it; the Analyst re-drafts under the next free ML id"
            )
            raise AdoptStudyError(msg)
    for where, other in _committed(study_id, sdir, main):
        if other != h:
            msg = (
                f"{study_id} is taken: {where} holds spec {other[:12]}, not this draft's "
                f"{h[:12]}; the Analyst re-drafts under the next free ML id"
            )
            raise AdoptStudyError(msg)
    weekly_cap_or_raise(queue_path, study_id, now)

    text = spec_file_text(spec, approval=approval)
    path = f"{SPECS_DIR}/{study_id}.yaml"
    branch = f"ml/{study_id.split('-')[1]}"
    title = f"{study_id}: commit ML study spec (owner approved, D133)"
    body = study_pr_body(spec, h, approval)
    est = ml_rung_estimates(load_ml_ledger(ledger), spec, default_s=default_rung_s)
    res = AdoptStudyResult(
        id=study_id,
        spec_hash=h,
        status="dry-run" if dry_run else "queued",
        created=False,
        path=path,
        branch=branch,
        pr_title=title,
        pr_body=body,
        file_text=text,
        dry_run=dry_run,
        dry_run_pr=dry_run_pr,
        rung_estimates=est,
    )
    if dry_run:
        res.pr_status = "dry-run"
        return res
    pr_url = queued.pr_url if queued is not None else None
    if dry_run_pr or publisher is None:
        res.pr_status = "dry-run" if dry_run_pr else "skipped"
    else:
        pr = publisher.publish(branch=branch, files={path: text}, title=title, body=body)
        res.pr_url, res.pr_status, res.auto_merge = pr.url, pr.status, pr.auto_merge
        pr_url = pr.url or pr_url
    entry = QueueEntry(
        id=study_id, kind="ml", spec_hash=h, approved_at=now, approval=approval, pr_url=pr_url
    )

    def _add(q: StudyQueue) -> tuple[QueueEntry, bool]:
        try:
            check_weekly_cap(q, study_id, now)  # re-checked under the lock
            return enqueue(q, entry)
        except QueueError as exc:  # pragma: no cover - checked above, re-checked under lock
            raise AdoptStudyError(str(exc)) from exc

    got, created = update_queue(queue_path, _add)
    res.created = created
    res.status = got.status
    res.pr_url = res.pr_url or got.pr_url
    res.entry = got.model_dump(mode="json")
    rows = {r["id"]: r for r in eta_rows(load_queue(queue_path), bt_ledger, {study_id: est})}
    row = rows.get(study_id) or {}
    res.queue_position = row.get("queue_position")
    res.eta_nights = row.get("eta_nights")
    res.compute_s = row.get("compute_s")
    log.info("ml.study.adopted", study=study_id, spec_hash=h[:12], created=created)
    return res


def drop_ml_study(
    study_id: str, reason: str, *, now: dt.datetime, queue_path: Path, ledger: Path
) -> MlLedgerEntry:
    """Owner ``drop ML-<n> <why>`` of a draft that is not queued: a ``dropped`` ledger
    line reserves the number (a gap, never reused, D121). A queued study is stopped."""
    if not reason.strip():
        msg = "drop needs a reason"
        raise AdoptStudyError(msg)
    if not _ID_RE.match(study_id) or study_id == SCRATCH_ID:
        msg = f"{study_id!r}: use ML-<n>"
        raise AdoptStudyError(msg)
    q = load_queue(queue_path).get(study_id)
    if q is not None:
        msg = f"{study_id} is {q.status} (committed and queued): use `stop {study_id} <why>`"
        raise AdoptStudyError(msg)
    if any(e.study == study_id for e in load_ml_ledger(ledger)):
        msg = f"{study_id} already has ML ledger lines (ran or dropped)"
        raise AdoptStudyError(msg)
    line = MlLedgerEntry(
        study=study_id,
        spec_hash="",
        family="",
        incumbent="",
        rung="dropped",
        at=now,
        rule="",
        verdict="dropped",
        note=f"owner: {reason.strip()}",
    )
    append_ml_ledger(ledger, line)
    return line


# ---------------------------------------------------------------------------
# Runner hooks (arc.backtest.runner, kind: ml)
# ---------------------------------------------------------------------------


def rung_argv(
    study_id: str,
    *,
    rung: str,
    spec_dir: Path,
    ledger: Path,
    runs_dir: Path,
    models_root: Path,
    data_dir: Path,
    workers: int,
    now: dt.datetime,
    python: str,
) -> list[str]:
    """``python -c 'arc main' ml study run ML-<n> --rung <r> ...`` (the runner owns the
    window, so the child runs with ``--ignore-window``)."""
    argv = [python, "-c", "from arc.cli import main; raise SystemExit(main())"]
    argv += ["ml", "study", "run", study_id, "--rung", rung, "--studies-dir", str(spec_dir)]
    argv += ["--ledger", str(ledger), "--runs-dir", str(runs_dir), "--models", str(models_root)]
    argv += ["--data", str(data_dir), "--workers", str(workers), "--now", now.isoformat()]
    argv.append("--ignore-window")
    return argv


def find_line(
    ledger: Path, study_id: str, spec_hash: str, rung: str, at: dt.datetime
) -> MlLedgerEntry | None:
    return next(
        (
            e
            for e in load_ml_ledger(ledger)
            if e.study == study_id and e.spec_hash == spec_hash and e.rung == rung and e.at == at
        ),
        None,
    )
