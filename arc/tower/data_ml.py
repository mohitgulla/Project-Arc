"""Model registry and ``ML-<n>`` studies on the Experiments page's Model tab (D133).

Read-only, from files only, with the stdlib (``json`` / ``csv``) plus ``yaml`` for the
study specs: the registry (``data/ml/models/<family>/models.json``, ``promotions.jsonl``,
``holdout_reads.jsonl``, ``<version>/manifest.json`` and its ``eval_*.csv``), the daily
``ml_health`` record (``data/ml/health/<day>.json``), the ML study ledger
(``data/ml/studies/ledger.jsonl``), the committed specs (``config/ml_studies/ML-<n>.yaml``),
the shared study queue (``data/backtest/queue.json``, ``kind: ml``) and the Arc Analyst's
proposed drafts (``xp_backlog.json``, ``kind: ml``).

The tower never imports the training code (``arc.ml``), never loads a model and never
re-scores anything: every number is a stored aggregate. Model metrics are forecast quality
on the dev test years (loss, coverage, IC), never P&L.
"""

from __future__ import annotations

import csv
import datetime as _dt
import json
import re
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from arc.tower.data_backtests import scrub_ids

__all__ = [
    "MlModelDetail",
    "MlModelsResponse",
    "MlPaths",
    "MlStudiesResponse",
    "MlStudyDetailResponse",
    "load_ml_model",
    "load_ml_models",
    "load_ml_studies",
    "load_ml_study",
]

_STRICT = ConfigDict(extra="forbid", frozen=True)
_LOOSE = ConfigDict(extra="ignore")

#: The families the programme knows (a card adds one here when it adds a family).
FamilyKind = Literal["quantile", "outcome"]
FAMILIES: dict[str, tuple[str, FamilyKind, str]] = {
    "m1": (
        "M1 · Move & Vol",
        "quantile",
        "Calibrated quantiles of each name's log return and realised vol over the next "
        "5 to 42 sessions. Feeds the candidate model and the Monte Carlo path source.",
    ),
    "credit": (
        "M2 · Candidate Outcome (Credit)",
        "outcome",
        "P(win), E[R], P(stop), R quantiles and expected hold per credit candidate, "
        "calibrated out of fold.",
    ),
    "debit": (
        "M2 · Candidate Outcome (Debit)",
        "outcome",
        "The same targets for debit candidates.",
    ),
}
FAMILY_RULE = {"m1": "m1_keep", "credit": "m2_promote", "debit": "m2_promote"}
ModelStatus = Literal["champion", "shadow", "candidate", "retired", "planned"]
_STATUSES = ("champion", "shadow", "candidate", "retired")
_ML_ID_RE = re.compile(r"^ML-[1-9]\d*$")
_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
KIND_LABEL = {"log_ret": "Return", "rv": "Vol"}
#: M1's keep rule: DM one-sided p below this and coverage error at most COV_PTS points.
DM_P = 0.05
COV_PTS = 3.0
TOP_N = 10
WORST_N = 5

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


class MlPaths(BaseModel):
    """Where the tower reads the ML files (repo-relative paths resolved on *root*)."""

    model_config = ConfigDict(frozen=True)

    root: Path
    models: str = "data/ml/models"
    health: str = "data/ml/health"
    ledger: str = "data/ml/studies/ledger.jsonl"
    runs: str = "data/ml/studies"
    specs: str = "config/ml_studies"
    queue: str = "data/backtest/queue.json"
    #: the Arc Analyst's ``xp_backlog.json`` (proposed ``ML-<n>`` drafts); None = none
    backlog: Path | None = None

    def path(self, rel: str) -> Path:
        p = Path(rel)
        return p if p.is_absolute() else self.root / p


# ---------------------------------------------------------------------------
# Tolerant file readers
# ---------------------------------------------------------------------------


def _json(p: Path) -> Any:
    try:
        return json.loads(p.read_text()) if p.is_file() else None
    except (OSError, ValueError):
        return None


def _jsonl(p: Path) -> list[dict[str, Any]]:
    if not p.is_file():
        return []
    out = []
    for line in p.read_text().splitlines():
        try:
            row = json.loads(line) if line.strip() else None
        except ValueError:
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


def _csv(p: Path) -> list[dict[str, str]]:
    if not p.is_file():
        return []
    try:
        with p.open(newline="") as f:
            return list(csv.DictReader(f))
    except (OSError, csv.Error):
        return []


def _f(v: Any) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, int | float):
        return float(v) if v == v else None  # NaN -> None
    if isinstance(v, str) and v.strip():
        try:
            x = float(v)
        except ValueError:
            return None
        return x if x == x else None
    return None


def _b(v: Any) -> bool | None:
    if isinstance(v, bool):
        return v
    if isinstance(v, str) and v.strip().lower() in ("true", "false"):
        return v.strip().lower() == "true"
    return None


def _rel(a: float | None, b: float | None) -> float | None:
    """``a / b - 1`` (negative = *a* is the lower loss)."""
    if a is None or b is None or b == 0:
        return None
    return round(a / b - 1.0, 6)


def _r(v: float | None, nd: int = 6) -> float | None:
    return None if v is None else round(v, nd)


def _year(fold: str) -> str:
    return fold.removeprefix("test")


def _target(kind: str, h: Any) -> str:
    return f"{KIND_LABEL.get(kind, kind.title())} h{h}"


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------

Unit = Literal["pct", "pts", "ratio", "count", "usd", "text"]


class Stat(BaseModel):
    """One headline number; the UI formats it by *unit* (pct = fraction, pts = fraction
    shown in points, ratio = plain number, count = ``value / of``)."""

    model_config = _STRICT

    label: str
    value: float | None = None
    of: int | None = Field(None, description="Denominator for unit 'count' (e.g. 0 / 8)")
    unit: Unit = "ratio"
    text: str | None = Field(None, description="Pre-formatted value for unit 'text'")
    sub: str | None = None
    tone: Literal["pos", "neg", "neutral"] = "neutral"


class VersionRow(BaseModel):
    model_config = _STRICT

    version: str
    status: ModelStatus
    created: _dt.datetime | None = None
    rule_passed: bool | None = None
    source: str | None = Field(None, description="'card' or the ML-<n> study that made it")
    train_s: float | None = None
    note: str | None = None


class ModelRow(BaseModel):
    """One model on the Models card (a family's version, or a planned family)."""

    model_config = _STRICT

    family: str
    label: str
    family_kind: FamilyKind
    purpose: str
    version: str | None = None
    status: ModelStatus
    created: _dt.datetime | None = None
    source: str | None = None
    rule: str | None = None
    rule_passed: bool | None = None
    holdout_reads: int = 0
    stats: list[Stat] = Field(default_factory=list, description="The row's three numbers")


class FamilyCounts(BaseModel):
    model_config = _STRICT

    family: str
    label: str
    champion: str | None = None
    counts: dict[str, int] = Field(default_factory=dict)
    holdout_reads: int = 0


class MlSummary(BaseModel):
    model_config = _STRICT

    champions: int
    families: int
    champion_families: list[str] = Field(default_factory=list)
    in_shadow: int = 0
    studies_proposed: int = 0
    studies_queued: int = 0
    live_calibration: str | None = Field(
        None, description="'ok' | 'insufficient' | None (no live record yet)"
    )
    live_calibration_n: int | None = None
    worst_psi: float | None = None
    worst_psi_feature: str | None = None
    health_day: _dt.date | None = None
    running: str | None = Field(None, description="Study the shared nightly runner is on")
    queued: int = Field(0, description="Approved studies (BT and ML) waiting for a night")
    window_start: str = "21:30"


class MlModelsResponse(BaseModel):
    model_config = _STRICT

    as_of: _dt.datetime
    summary: MlSummary
    families: list[FamilyCounts]
    items: list[ModelRow]


class KV(BaseModel):
    model_config = _STRICT

    label: str
    value: str


class QuantileRuleCell(BaseModel):
    model_config = _STRICT

    year: str
    pinball_rel: float | None = Field(None, description="LightGBM pinball / baseline − 1")
    dm_p: float | None = None
    cov_err: float | None = Field(None, description="LightGBM coverage error after CQR")
    beats: bool = False
    cov_ok: bool = False
    passed: bool | None = None


class QuantileRuleRow(BaseModel):
    model_config = _STRICT

    target: str
    kind: str
    h: int
    ships: str = Field(..., description="'baseline' | 'lgbm'")
    years: list[QuantileRuleCell]


class CoverageSeries(BaseModel):
    model_config = _STRICT

    target: str
    year: str
    model: str
    points: list[tuple[float, float]] = Field(..., description="(nominal, empirical)")


class LossBar(BaseModel):
    """One bar: the model's loss relative to a baseline (negative = better)."""

    model_config = _STRICT

    group: str = Field(..., description="'Vol' / 'Return' (M1) or the split (M2)")
    label: str = Field(..., description="'h21' or the baseline name")
    year: str
    rel: float


class Importance(BaseModel):
    model_config = _STRICT

    target: str
    kind: Literal["gain", "permutation"]
    rows: list[tuple[str, float]]


class RegimeRow(BaseModel):
    model_config = _STRICT

    regime: str
    target: str
    year: str
    n: int | None = None
    rel: float | None = Field(None, description="Loss vs baseline, relative (M1)")
    cov_err: float | None = None
    ece: float | None = Field(None, description="Win ECE in the regime (M2)")
    er_ic: float | None = None


class RollingCalibration(BaseModel):
    model_config = _STRICT

    year: str
    label: str
    annual: float | None
    rolling: float | None


class OutcomeRuleRow(BaseModel):
    model_config = _STRICT

    year: str
    ll_model: float | None = None
    ll_logistic: float | None = None
    ll_managed_pop: float | None = None
    beats_logistic: bool | None = None
    beats_managed_pop: bool | None = None
    er_ic: float | None = None
    er_ic_t: float | None = None
    ic_pass: bool | None = None
    replay_pass: bool | None = None
    net_incumbent: float | None = None
    net_model: float | None = None
    dd_incumbent: float | None = None
    dd_model: float | None = None
    passed: bool | None = None


class ReliabilityBin(BaseModel):
    model_config = _STRICT

    year: str
    target: str
    bin: str
    n: int | None = None
    mean_p: float
    observed: float


class ReplayRow(BaseModel):
    model_config = _STRICT

    year: str
    use: str
    trades: int | None = None
    net_pnl: float | None = None
    max_dd_pct: float | None = None
    win_rate: float | None = None


class Cost(BaseModel):
    model_config = _STRICT

    train_s: float | None = None
    folds_s: float | None = None
    final_s: float | None = None
    predict_s: float | None = None
    predict_rows: int | None = None
    predict_all_s: float | None = None
    size_bytes: int | None = None
    boosters: int | None = None
    peak_rss_mb: float | None = None


class LiveBlock(BaseModel):
    """The latest ``ml_health`` record, for the family it serves (``arc ml monitor``)."""

    model_config = _STRICT

    day: _dt.date
    serving: str | None = None
    status: str | None = Field(None, description="'unavailable' when the model did not load")
    pct_scored: float | None = None
    candidates: int | None = None
    latency_ms_p95: int | None = None
    calibration: str | None = None
    calibration_n: int | None = None
    brier: float | None = None
    brier_base: float | None = None
    drift_status: str | None = None
    worst_psi: float | None = None
    worst_psi_feature: str | None = None
    drift_warn: int | None = None
    score_psi: float | None = None
    parity: str | None = None
    last_retrain: str | None = None


class _DetailBase(BaseModel):
    model_config = _STRICT

    as_of: _dt.datetime
    model: ModelRow
    headline: list[Stat]
    spec: list[KV]
    feature_groups: dict[str, int] = Field(default_factory=dict)
    importance: list[Importance] = Field(default_factory=list)
    regimes: list[RegimeRow] = Field(default_factory=list)
    loss: list[LossBar] = Field(default_factory=list)
    versions: list[VersionRow] = Field(default_factory=list)
    cost: Cost | None = None
    live: LiveBlock | None = None


class QuantileDetail(_DetailBase):
    family_kind: Literal["quantile"] = "quantile"
    rule: list[QuantileRuleRow] = Field(default_factory=list)
    coverage: list[CoverageSeries] = Field(default_factory=list)
    rolling: list[RollingCalibration] = Field(default_factory=list)


class OutcomeDetail(_DetailBase):
    family_kind: Literal["outcome"] = "outcome"
    rule: list[OutcomeRuleRow] = Field(default_factory=list)
    reliability: list[ReliabilityBin] = Field(default_factory=list)
    replay: list[ReplayRow] = Field(default_factory=list)


MlModelDetail = Annotated[QuantileDetail | OutcomeDetail, Field(discriminator="family_kind")]


class MlModelDetailResponse(BaseModel):
    """``/api/ml/models/{family}/{version}``: the detail, discriminated on family_kind."""

    model_config = _STRICT

    detail: MlModelDetail


# -- studies ------------------------------------------------------------------

StudyStatus = Literal[
    "proposed", "queued", "running", "concluded", "stopped", "failed", "dropped", "approved"
]
#: The study ladder (D133): a verdict never reads the holdout; champion is owner-only.
STEPS: tuple[str, ...] = (
    "proposed",
    "approved",
    "dev_2022",
    "dev_2023",
    "verdict",
    "shadow",
    "champion",
)
STEP_LABEL = {
    "proposed": "Proposed",
    "approved": "Approved",
    "dev_2022": "Dev 2022",
    "dev_2023": "Dev 2023",
    "verdict": "Verdict",
    "shadow": "Shadow",
    "champion": "Champion",
}


class StudyStep(BaseModel):
    model_config = _STRICT

    step: str
    label: str
    state: Literal["done", "failed", "pending"]
    note: str | None = None


class MlStudyItem(BaseModel):
    model_config = _STRICT

    study_id: str
    title: str
    hypothesis: str
    family: str | None = None
    family_label: str | None = None
    change_kind: str | None = None
    status: StudyStatus
    retired: bool = False
    incumbent: str | None = None
    variants: int = 1
    steps: list[StudyStep]
    stage: str = Field(..., description="Label of the furthest step reached")
    verdict: Literal["pass", "fail", "dropped"] | None = None
    version: str | None = Field(None, description="The version its verdict registered")
    evidence: list[str] = Field(default_factory=list, description="One line per rung run")
    nights_needed: int | None = None
    runtime_s: float | None = None
    eta_s: float | None = Field(None, description="Estimated runner seconds left")
    queue_position: int | None = None
    created: _dt.date | None = None
    last_at: _dt.datetime | None = None


class MlStudiesResponse(BaseModel):
    model_config = _STRICT

    as_of: _dt.datetime
    items: list[MlStudyItem]
    proposed: int = 0
    queued: int = 0
    running: str | None = None
    window_start: str = "21:30"


class MlStudyDetailResponse(BaseModel):
    model_config = _STRICT

    as_of: _dt.datetime
    study: MlStudyItem
    spec: list[KV]
    overlay: dict[str, Any] = Field(default_factory=dict)
    grid: list[dict[str, Any]] = Field(default_factory=list)
    rule: str | None = None
    spec_file: str | None = None
    expected_effect: str | None = None


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class _VersionEntry(BaseModel):
    model_config = _LOOSE

    status: str = "candidate"
    created: str | None = None
    rule_passed: bool | None = None
    rule: str | None = None
    feature_schema: str | None = None
    summary: dict[str, Any] = Field(default_factory=dict)


class _Family(BaseModel):
    """One family dir: its index (or a synthesised one) and the version dirs."""

    name: str
    champion: str | None = None
    versions: dict[str, _VersionEntry]
    holdout_reads: int = 0
    indexed: bool = True


def _created(v: str | None) -> _dt.datetime | None:
    if not v:
        return None
    try:
        return _dt.datetime.fromisoformat(v)
    except ValueError:
        return None


def _family(paths: MlPaths, name: str) -> _Family | None:
    fdir = paths.path(paths.models) / name
    if not fdir.is_dir():
        return None
    dirs = sorted(p.parent for p in fdir.glob("*/manifest.json"))
    raw = _json(fdir / "models.json")
    reads = sum(1 for r in _jsonl(fdir / "holdout_reads.jsonl") if r.get("kind") != "rolling")
    if isinstance(raw, dict) and isinstance(raw.get("versions"), dict):
        vers: dict[str, _VersionEntry] = {}
        for v, e in raw["versions"].items():
            try:
                vers[str(v)] = _VersionEntry.model_validate(e)
            except ValidationError:
                continue
        champ = raw.get("champion") if isinstance(raw.get("champion"), str) else None
        return _Family(name=name, champion=champ, versions=vers, holdout_reads=reads)
    if not dirs:
        return None
    # No index (M1 before the registry): the newest version dir ships, the rest are
    # superseded. "Newest" = the manifest file written last.
    dirs.sort(key=lambda d: ((d / "manifest.json").stat().st_mtime, d.name))
    vers = {}
    for i, d in enumerate(dirs):
        man = _json(d / "manifest.json") or {}
        created = man.get("created")
        if not created:
            ts = (d / "manifest.json").stat().st_mtime
            created = _dt.datetime.fromtimestamp(ts, tz=_dt.UTC).isoformat()
        vers[d.name] = _VersionEntry(
            status="champion" if i == len(dirs) - 1 else "retired",
            created=created,
            rule=FAMILY_RULE.get(name),
            feature_schema=man.get("feature_schema"),
        )
    return _Family(
        name=name, champion=dirs[-1].name, versions=vers, holdout_reads=reads, indexed=False
    )


def _families(paths: MlPaths) -> dict[str, _Family | None]:
    root = paths.path(paths.models)
    names = list(FAMILIES)
    if root.is_dir():
        names += sorted(p.name for p in root.iterdir() if p.is_dir() and p.name not in FAMILIES)
    return {n: _family(paths, n) for n in names}


def _meta(name: str) -> tuple[str, FamilyKind, str]:
    return FAMILIES.get(name, (name.upper(), "outcome", ""))


def _manifest(paths: MlPaths, family: str, version: str) -> dict[str, Any]:
    raw = _json(paths.path(paths.models) / family / version / "manifest.json")
    return raw if isinstance(raw, dict) else {}


def _source(man: dict[str, Any]) -> str:
    study = man.get("study")
    if isinstance(study, str) and _ML_ID_RE.match(study):
        return study
    if isinstance(study, dict) and _ML_ID_RE.match(str(study.get("id", ""))):
        return str(study["id"])
    return "card"


# ---------------------------------------------------------------------------
# Quantile family (M1)
# ---------------------------------------------------------------------------


class _QuantileEval:
    def __init__(self, vdir: Path, man: dict[str, Any]) -> None:
        self.vdir = vdir
        self.man = man
        self.metrics = [r for r in _csv(vdir / "eval_metrics.csv") if r.get("split") == "All"]
        self.decisions = _csv(vdir / "eval_decisions.csv")
        self.folds = sorted({r["fold"] for r in self.metrics if r.get("fold")})
        tg = man.get("targets") if isinstance(man.get("targets"), list) else []
        self.targets: list[tuple[str, int, str]] = [
            (str(t.get("kind")), int(t.get("h")), str(t.get("source") or "baseline"))
            for t in tg
            if isinstance(t, dict) and t.get("kind") and t.get("h") is not None
        ]
        if not self.targets:
            seen = sorted({(r["kind"], int(r["h"])) for r in self.decisions if r.get("h")})
            src = {(r["kind"], int(r["h"])): r.get("source", "baseline") for r in self.decisions}
            self.targets = [(k, h, src.get((k, h), "baseline")) for k, h in seen]

    def row(self, fold: str, kind: str, h: int, model: str) -> dict[str, str] | None:
        return next(
            (
                r
                for r in self.metrics
                if r.get("fold") == fold
                and r.get("kind") == kind
                and r.get("h") == str(h)
                and r.get("model") == model
            ),
            None,
        )

    def last(self) -> str | None:
        return self.folds[-1] if self.folds else None

    def passed(self, kind: str, h: int, fold: str) -> bool | None:
        d = next(
            (r for r in self.decisions if r.get("kind") == kind and r.get("h") == str(h)), None
        )
        return None if d is None else _b(d.get(f"{fold}_pass"))

    def rule(self) -> list[QuantileRuleRow]:
        out = []
        for kind, h, src in self.targets:
            cells = []
            for fold in self.folds:
                lg, bs = self.row(fold, kind, h, "lgbm"), self.row(fold, kind, h, "baseline")
                rel = _rel(_f((lg or {}).get("pinball")), _f((bs or {}).get("pinball")))
                p = _f((lg or {}).get("dm_p_one_sided"))
                cov = _f((lg or {}).get("cov_err"))
                cells.append(
                    QuantileRuleCell(
                        year=_year(fold),
                        pinball_rel=rel,
                        dm_p=p,
                        cov_err=_r(cov),
                        beats=rel is not None and rel < 0 and p is not None and p < DM_P,
                        cov_ok=cov is not None and cov * 100 <= COV_PTS,
                        passed=self.passed(kind, h, fold),
                    )
                )
            out.append(
                QuantileRuleRow(target=_target(kind, h), kind=kind, h=h, ships=src, years=cells)
            )
        return out

    def shipped_cov(self) -> tuple[float | None, str]:
        """The largest coverage error of the shipped source in the last test year."""
        fold = self.last()
        best: tuple[float | None, str] = (None, "")
        if fold is None:
            return best
        for kind, h, src in self.targets:
            r = self.row(fold, kind, h, "lgbm" if src == "lgbm" else "baseline")
            c = _f((r or {}).get("cov_err"))
            if c is not None and (best[0] is None or c > best[0]):
                best = (c, f"{_target(kind, h).lower()} · {_year(fold)}")
        return best

    def coverage(self) -> list[CoverageSeries]:
        levels = [float(x) for x in self.man.get("levels") or []]
        out = []
        for kind, h, _ in self.targets:
            for fold in self.folds:
                for model in ("lgbm", "baseline"):
                    r = self.row(fold, kind, h, model)
                    if r is None:
                        continue
                    pts = []
                    for lv in levels:
                        e = _f(r.get(f"cov_{round(lv * 100):02d}"))
                        if e is not None:
                            pts.append((lv, round(e, 4)))
                    if pts:
                        out.append(
                            CoverageSeries(
                                target=_target(kind, h), year=_year(fold), model=model, points=pts
                            )
                        )
        return out

    def loss(self) -> list[LossBar]:
        out = []
        for kind, h, _ in self.targets:
            for fold in self.folds:
                lg, bs = self.row(fold, kind, h, "lgbm"), self.row(fold, kind, h, "baseline")
                rel = _rel(_f((lg or {}).get("pinball")), _f((bs or {}).get("pinball")))
                if rel is not None:
                    out.append(
                        LossBar(
                            group=KIND_LABEL.get(kind, kind),
                            label=f"h{h}",
                            year=_year(fold),
                            rel=rel,
                        )
                    )
        return out

    def rolling(self) -> list[RollingCalibration]:
        out = []
        for kind, h, _ in self.targets:
            if kind != "rv":
                continue
            for fold in self.folds:
                r = self.row(fold, kind, h, "lgbm")
                if r is None or _f(r.get("cov_err_roll")) is None:
                    continue
                out.append(
                    RollingCalibration(
                        year=_year(fold),
                        label=f"h{h}",
                        annual=_r(_f(r.get("cov_err"))),
                        rolling=_r(_f(r.get("cov_err_roll"))),
                    )
                )
        return out

    def importance(self) -> list[Importance]:
        out = []
        gains: dict[tuple[str, str], list[tuple[str, float]]] = {}
        for r in _csv(self.vdir / "eval_gain.csv"):
            g = _f(r.get("gain"))
            if g is not None and r.get("feature"):
                gains.setdefault((r["kind"], r["h"]), []).append((r["feature"], g))
        for (kind, h), rows in sorted(gains.items(), key=lambda kv: (kv[0][0], int(kv[0][1]))):
            tot = sum(g for _, g in rows) or 1.0
            top = sorted(rows, key=lambda t: -t[1])[:TOP_N]
            out.append(
                Importance(
                    target=_target(kind, h),
                    kind="gain",
                    rows=[(f, round(g / tot, 4)) for f, g in top],
                )
            )
        perm: dict[tuple[str, str], dict[str, list[float]]] = {}
        for r in _csv(self.vdir / "eval_permutation.csv"):
            v = _f(r.get("pinball_rise"))
            if v is not None and r.get("feature"):
                perm.setdefault((r["kind"], r["h"]), {}).setdefault(r["feature"], []).append(v)
        for (kind, h), feats in sorted(perm.items(), key=lambda kv: (kv[0][0], int(kv[0][1]))):
            avg = sorted(((f, sum(v) / len(v)) for f, v in feats.items()), key=lambda t: -t[1])
            out.append(
                Importance(
                    target=_target(kind, h),
                    kind="permutation",
                    rows=[(f, round(v, 6)) for f, v in avg[:TOP_N]],
                )
            )
        return out

    def regimes(self) -> list[RegimeRow]:
        rows = []
        for r in _csv(self.vdir / "eval_regimes.csv"):
            pin, diff = _f(r.get("pinball")), _f(r.get("pinball_vs_base"))
            if pin is None or diff is None or pin - diff <= 0:
                continue
            rows.append(
                RegimeRow(
                    regime=f"{r.get('regime', '').replace('_', ' ').title()} · "
                    f"{r.get('value', '').replace('_', ' ').title()}",
                    target=_target(r.get("kind", ""), r.get("h", "")),
                    year=_year(r.get("fold", "")),
                    n=int(_f(r.get("n")) or 0) or None,
                    rel=round(diff / (pin - diff), 4),
                    cov_err=_r(_f(r.get("cov_err"))),
                )
            )
        rows.sort(key=lambda x: -(x.rel or 0))
        return rows[:WORST_N]

    def ic(self, kind: str, h: int) -> float | None:
        fold = self.last()
        src = next((s for k, hh, s in self.targets if k == kind and hh == h), "baseline")
        r = self.row(fold, kind, h, "lgbm" if src == "lgbm" else "baseline") if fold else None
        return _f((r or {}).get("rank_ic"))


def _rel_stat(label: str, rel: float | None, sub: str) -> Stat:
    tone: Literal["pos", "neg", "neutral"] = (
        "neutral" if rel is None else ("pos" if rel < 0 else "neg")
    )
    return Stat(label=label, value=rel, unit="pct", sub=sub, tone=tone)


def _quantile_stats(q: _QuantileEval, reads: int) -> tuple[list[Stat], list[Stat]]:
    """(row stats, detail headline)."""
    n = len(q.targets)
    on_lgbm = sum(1 for *_, s in q.targets if s == "lgbm")
    beats = sum(1 for r in q.rule() if r.years and all(c.beats for c in r.years))
    cov, cov_where = q.shipped_cov()
    fold = q.last()
    yr = _year(fold) if fold else ""
    ships = "LightGBM kept" if on_lgbm else "baseline kept"
    keep = Stat(label="Keep Rule", value=on_lgbm, of=n, unit="count", sub=ships)
    beat = Stat(label="Beats Baseline", value=beats, of=n, unit="count", sub="pinball, both years")
    cov_s = Stat(
        label="Coverage Error",
        value=cov,
        unit="pts",
        sub=f"shipped, {cov_where}" if cov_where else None,
        tone="neutral" if cov is None else ("pos" if cov * 100 <= COV_PTS else "neg"),
    )

    def rel21(kind: str) -> float | None:
        if fold is None:
            return None
        lg, bs = q.row(fold, kind, 21, "lgbm"), q.row(fold, kind, 21, "baseline")
        return _rel(_f((lg or {}).get("pinball")), _f((bs or {}).get("pinball")))

    ic = q.ic("rv", 21)
    head = [
        Stat(label="Keep Rule", value=on_lgbm, of=n, unit="count", sub="targets on LightGBM"),
        _rel_stat("Vol vs Baseline", rel21("rv"), f"pinball, h21 · {yr}"),
        _rel_stat("Return vs Baseline", rel21("log_ret"), f"pinball, h21 · {yr}"),
        cov_s,
        Stat(label="Rank IC", value=ic, unit="ratio", sub=f"vol h21 · {yr}"),
        Stat(
            label="Holdout Reads", value=reads, unit="count", sub="dev only" if not reads else None
        ),
    ]
    return [keep, beat, cov_s], head


# ---------------------------------------------------------------------------
# Outcome family (M2)
# ---------------------------------------------------------------------------


class _OutcomeEval:
    def __init__(self, vdir: Path, man: dict[str, Any]) -> None:
        self.vdir = vdir
        self.man = man
        self.rule_rows = _csv(vdir / "eval_rule.csv")
        self.metrics = _csv(vdir / "eval_metrics.csv")
        self.folds = sorted({r["fold"] for r in self.rule_rows if r.get("fold")}) or sorted(
            {r["fold"] for r in self.metrics if r.get("fold")}
        )

    def last(self) -> str | None:
        return self.folds[-1] if self.folds else None

    def rule(self) -> list[OutcomeRuleRow]:
        out = []
        for r in self.rule_rows:
            out.append(
                OutcomeRuleRow(
                    year=_year(r.get("fold", "")),
                    ll_model=_r(_f(r.get("ll_m2"))),
                    ll_logistic=_r(_f(r.get("ll_logistic"))),
                    ll_managed_pop=_r(_f(r.get("ll_managed_pop"))),
                    beats_logistic=_b(r.get("beats_logistic")),
                    beats_managed_pop=_b(r.get("beats_managed_pop")),
                    er_ic=_r(_f(r.get("er_ic"))),
                    er_ic_t=_r(_f(r.get("er_ic_t")), 3),
                    ic_pass=_b(r.get("ic_pass")),
                    replay_pass=_b(r.get("replay_pass")),
                    net_incumbent=_f(r.get("net_incumbent")),
                    net_model=_f(r.get("net_ml_er")),
                    dd_incumbent=_f(r.get("dd_incumbent")),
                    dd_model=_f(r.get("dd_ml_er")),
                    passed=_b(r.get("year_pass")),
                )
            )
        return out

    def overall(self, fold: str, split: str = "All") -> dict[str, str]:
        return next(
            (
                r
                for r in self.metrics
                if r.get("fold") == fold and r.get("split") == split and r.get("group") == "all"
            ),
            {},
        )

    def loss(self) -> list[LossBar]:
        out = []
        for fold in self.folds:
            for split in ("All", "Stocks", "ETFs"):
                m = self.overall(fold, split)
                ll = _f(m.get("win_log_loss"))
                for base, col in (
                    ("Logistic", "win_log_loss_logistic"),
                    ("Managed PoP", "win_log_loss_managed_pop"),
                ):
                    rel = _rel(ll, _f(m.get(col)))
                    if rel is not None:
                        out.append(LossBar(group=split, label=base, year=_year(fold), rel=rel))
        return out

    def reliability(self) -> list[ReliabilityBin]:
        out = []
        for r in _csv(self.vdir / "eval_reliability.csv"):
            if r.get("split", "All") != "All":
                continue
            mp, ob = _f(r.get("mean_p")), _f(r.get("observed"))
            if mp is None or ob is None:
                continue
            out.append(
                ReliabilityBin(
                    year=_year(r.get("fold", "")),
                    target=str(r.get("target", "win")),
                    bin=str(r.get("bin", "")),
                    n=int(_f(r.get("n")) or 0) or None,
                    mean_p=round(mp, 4),
                    observed=round(ob, 4),
                )
            )
        return out

    def importance(self) -> list[Importance]:
        out = []
        gains: dict[str, list[tuple[str, float]]] = {}
        for r in _csv(self.vdir / "eval_gain.csv"):
            g = _f(r.get("gain_share"))
            if g is not None and g > 0 and r.get("feature"):
                gains.setdefault(str(r.get("target")), []).append((r["feature"], g))
        for t in ("win", "stop", *sorted(set(gains) - {"win", "stop"})):
            if t in gains:
                top = sorted(gains[t], key=lambda x: -x[1])[:TOP_N]
                out.append(
                    Importance(target=t, kind="gain", rows=[(f, round(g, 4)) for f, g in top])
                )
        perm: dict[str, list[float]] = {}
        for r in _csv(self.vdir / "eval_permutation.csv"):
            v = _f(r.get("win_ll_rise"))
            if v is not None and r.get("feature"):
                perm.setdefault(r["feature"], []).append(v)
        if perm:
            avg = sorted(((f, sum(v) / len(v)) for f, v in perm.items()), key=lambda t: -t[1])
            out.append(
                Importance(
                    target="win",
                    kind="permutation",
                    rows=[(f, round(v, 6)) for f, v in avg[:TOP_N]],
                )
            )
        return out

    def regimes(self) -> list[RegimeRow]:
        rows = []
        for r in _csv(self.vdir / "eval_regimes.csv"):
            ece = _f(r.get("win_ece"))
            if ece is None:
                continue
            name, _, val = str(r.get("regime", "")).partition("=")
            rows.append(
                RegimeRow(
                    regime=f"{name.removeprefix('ctx_').replace('_', ' ').title()} · "
                    f"{val.replace('_', ' ').title()}",
                    target="Win",
                    year=_year(r.get("fold", "")),
                    n=int(_f(r.get("n")) or 0) or None,
                    ece=round(ece, 4),
                    er_ic=_r(_f(r.get("er_ic")), 4),
                )
            )
        rows.sort(key=lambda x: -(x.ece or 0))
        return rows[:WORST_N]

    def replay(self) -> list[ReplayRow]:
        out = []
        for r in _csv(self.vdir / "eval_replay.csv"):
            out.append(
                ReplayRow(
                    year=str(r.get("year", "")),
                    use=str(r.get("use", "")),
                    trades=int(_f(r.get("trades")) or 0),
                    net_pnl=_f(r.get("net_pnl")),
                    max_dd_pct=_f(r.get("max_dd_pct")),
                    win_rate=_f(r.get("win_rate")),
                )
            )
        return out


def _outcome_stats(
    o: _OutcomeEval, reads: int, summary: dict[str, Any]
) -> tuple[list[Stat], list[Stat]]:
    rule = o.rule()
    fold = o.last()
    yr = _year(fold) if fold else ""
    m = o.overall(fold) if fold else {}
    ll = _f(m.get("win_log_loss"))
    if ll is None:  # a registry summary is enough for the row
        ll = _f((summary.get("win_log_loss") or {}).get(fold or "", None))
    rel = _rel(ll, _f(m.get("win_log_loss_logistic")))
    ic = _f(m.get("er_ic_mean"))
    if ic is None:
        ic = _f((summary.get("er_ic") or {}).get(fold or "", None))
    years_ok = sum(1 for r in rule if r.passed)
    promote = Stat(
        label="Promote Rule",
        value=years_ok,
        of=len(rule) or None,
        unit="count",
        sub="test years passed" if rule else "pre-registered",
    )
    ll_s = _rel_stat("Log-Loss vs Logistic", rel, f"win, {yr}" if yr else "win")
    ic_s = Stat(
        label="E[R] IC",
        value=ic,
        unit="ratio",
        sub=f"per day, {yr}" if yr else None,
        tone="neutral" if ic is None else ("pos" if ic > 0 else "neg"),
    )
    last = next((r for r in rule if r.year == yr), None)
    net = (
        None
        if last is None or last.net_model is None or last.net_incumbent is None
        else last.net_model - last.net_incumbent
    )
    head = [
        promote,
        ll_s,
        ic_s,
        Stat(label="Win ECE", value=_f(m.get("win_ece")), unit="pts", sub=f"calibrated, {yr}"),
        Stat(
            label="Replay vs Incumbent",
            value=net,
            unit="usd",
            sub=f"net, ranked by E[R], {yr}",
            tone="neutral" if net is None else ("pos" if net > 0 else "neg"),
        ),
        Stat(
            label="Holdout Reads", value=reads, unit="count", sub="dev only" if not reads else None
        ),
    ]
    return [promote, ll_s, ic_s], head


# ---------------------------------------------------------------------------
# Spec, groups, cost, live
# ---------------------------------------------------------------------------


def _groups(paths: MlPaths, vdir: Path, man: dict[str, Any]) -> dict[str, int]:
    feats = _json(vdir / "features.json")
    if not isinstance(feats, list):
        feats = man.get("features") if isinstance(man.get("features"), list) else []
    gmap = _json(vdir / "feature_groups.json")
    if not isinstance(gmap, dict):
        # A version written before the group export: any version of the same feature
        # schema carries the registry's map (the groups are a property of the schema).
        schema = man.get("feature_schema")
        gmap = {}
        for p in sorted(paths.path(paths.models).glob("*/*/feature_groups.json")):
            om = _json(p.parent / "manifest.json") or {}
            if schema and om.get("feature_schema") == schema:
                got = _json(p)
                if isinstance(got, dict):
                    gmap = got
                    break
    out: dict[str, int] = {}
    for f in feats:
        g = str(gmap.get(str(f), "other"))
        out[g] = out.get(g, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: (-kv[1], kv[0])))


def _group_label(g: str) -> str:
    return g.replace("_", " ").title()


def _feature_kv(groups: dict[str, int]) -> KV:
    n = sum(groups.values())
    parts = " · ".join(f"{_group_label(g)} {c}" for g, c in groups.items())
    return KV(label="Features", value=f"{n}" + (f" ({parts})" if parts else ""))


def _window(w: Any) -> str | None:
    if not isinstance(w, dict) or not w.get("start"):
        return None
    return f"{w.get('start')} → {w.get('end')}"


def _short(h: Any, n: int = 12) -> str:
    return str(h)[:n] if h else "—"


def _model_card_rows(vdir: Path) -> str | None:
    p = vdir / "model_card.md"
    try:
        m = (
            re.search(r"Rows:\s*([\d,]+);\s*tickers\s*(\d+)", p.read_text())
            if p.is_file()
            else None
        )
    except OSError:
        return None
    return f"{m.group(2)} names · {m.group(1)} rows" if m else None


def _quantile_spec(
    man: dict[str, Any], q: _QuantileEval, vdir: Path, groups: dict[str, int]
) -> list[KV]:
    kinds = sorted(
        {k for k, _, _ in q.targets},
        key=lambda k: list(KIND_LABEL).index(k) if k in KIND_LABEL else 9,
    )
    hs = man.get("horizons") or sorted({h for _, h, _ in q.targets})
    lv = man.get("levels") or []
    p = man.get("lgb_params") if isinstance(man.get("lgb_params"), dict) else {}
    out = [
        KV(label="Targets", value=" · ".join(KIND_LABEL.get(k, k).lower() for k in kinds) or "—"),
        KV(label="Horizons", value=" · ".join(str(h) for h in hs) + " sessions"),
        KV(label="Quantiles", value=" · ".join(f"{round(float(x) * 100):g}" for x in lv) or "—"),
    ]
    rows = _model_card_rows(vdir)
    if rows:
        out.append(KV(label="Universe", value=rows))
    if w := _window(man.get("train_window")):
        out.append(KV(label="Train Window", value=w))
    folds = ", ".join(_year(f) for f in q.folds)
    purge = (
        (man.get("train_window") or {}).get("purge")
        if isinstance(man.get("train_window"), dict)
        else None
    )
    out.append(
        KV(
            label="Validation",
            value=f"walk-forward · test {folds or '—'}" + (f" · purge {purge}" if purge else ""),
        )
    )
    out.append(KV(label="Baselines", value="historical quantiles (return) · HAR-IV (vol)"))
    if w := _window(man.get("calibration_window")):
        out.append(KV(label="Calibration", value=f"conformal (CQR) per level, {w}"))
    if p:
        parts = [
            f"{man.get('num_rounds')} rounds" if man.get("num_rounds") else None,
            f"lr {p.get('learning_rate')}" if "learning_rate" in p else None,
            f"{p.get('num_leaves')} leaves" if "num_leaves" in p else None,
            f"min leaf {p.get('min_data_in_leaf')}" if "min_data_in_leaf" in p else None,
        ]
        out.append(KV(label="Params", value=" · ".join(x for x in parts if x)))
        if "seed" in p:
            det = " · deterministic" if p.get("deterministic") else ""
            out.append(KV(label="Seed", value=f"{p['seed']}{det}"))
    out.append(_feature_kv(groups))
    out.append(
        KV(
            label="Keep Rule",
            value=f"beats baseline (DM p < {DM_P:g}) and coverage error ≤ {COV_PTS:g} pts, "
            "both years",
        )
    )
    out.append(_lineage(man))
    return out


def _lineage(man: dict[str, Any]) -> KV:
    schema = _short(man.get("feature_schema"))
    data = _short(man.get("data_digest"), 16)
    code = _short(man.get("code_sha"), 10)
    return KV(label="Lineage", value=f"schema {schema} · data {data} · code {code}")


def _outcome_spec(man: dict[str, Any], o: _OutcomeEval, groups: dict[str, int]) -> list[KV]:
    cfg = man.get("config") if isinstance(man.get("config"), dict) else {}
    cal = cfg.get("calibration") if isinstance(cfg.get("calibration"), dict) else {}
    targets = list((man.get("rounds") or {}).keys()) if isinstance(man.get("rounds"), dict) else []
    out = [
        KV(label="Targets", value=" · ".join(targets) or "—"),
    ]
    if man.get("profile"):
        out.append(KV(label="Profile", value=str(man["profile"]).replace("_", " ").title()))
    n, raw = man.get("n_rows"), man.get("n_raw_ledger_rows")
    if n:
        out.append(
            KV(
                label="Rows",
                value=f"{int(n):,} train rows"
                + (f" of {int(raw):,} ledger rows" if raw else "")
                + (
                    f" · cap {man.get('n_raw_rows_per_day_cap')}/day"
                    if man.get("n_raw_rows_per_day_cap")
                    else ""
                ),
            )
        )
    if w := _window(man.get("train_window")):
        out.append(KV(label="Train Window", value=w))
    folds = ", ".join(_year(f) for f in o.folds)
    out.append(KV(label="Validation", value=f"walk-forward · test {folds or '—'}"))
    out.append(KV(label="Baselines", value="logistic · managed PoP"))
    if cal.get("method"):
        out.append(KV(label="Calibration", value=f"{cal['method']}, out of fold"))
    if cfg.get("learning_rate") is not None:
        out.append(
            KV(
                label="Params",
                value=f"lr {cfg.get('learning_rate')} · {len(cfg.get('grid') or [])} grid points"
                + (
                    f" · {man.get('configs_tried')} configs tried"
                    if man.get("configs_tried")
                    else ""
                ),
            )
        )
    if man.get("label_slippage"):
        out.append(KV(label="Label Slippage", value=str(man["label_slippage"])))
    out.append(_feature_kv(groups))
    out.append(
        KV(
            label="Promote Rule",
            value="beats logistic and managed PoP on log-loss, per-day E[R] IC > 0 (NW t > 2), "
            "replay beats the incumbent on net and max DD, both years",
        )
    )
    out.append(_lineage(man))
    return out


def _cost(vdir: Path, man: dict[str, Any]) -> Cost | None:
    t = _json(vdir / "timings.json")
    if not isinstance(t, dict):
        t = man.get("timings") if isinstance(man.get("timings"), dict) else None
    if not t:
        return None
    sizes = t.get("sizes") if isinstance(t.get("sizes"), dict) else {}
    size = sum(int(v) for v in sizes.values() if isinstance(v, int | float)) or None
    rows = _f(t.get("predict_one_day_rows"))
    return Cost(
        train_s=_f(t.get("total_s")),
        folds_s=_f(t.get("fold_fits_s") if "fold_fits_s" in t else t.get("fold_fit_s")),
        final_s=_f(t.get("final_fit_s")),
        predict_s=_f(t.get("predict_one_day_s")),
        predict_rows=int(rows) if rows is not None else None,
        predict_all_s=_f(t.get("predict_one_day_all_lgbm_s")),
        size_bytes=size,
        boosters=len(sizes) or None,
        peak_rss_mb=_f(t.get("peak_rss_mb")),
    )


def _health(paths: MlPaths) -> dict[str, Any] | None:
    d = paths.path(paths.health)
    if not d.is_dir():
        return None
    for p in sorted(d.glob("*.json"), reverse=True):
        rec = _json(p)
        if isinstance(rec, dict) and rec.get("day"):
            return rec
    return None


def _d(v: Any) -> dict[str, Any]:
    return v if isinstance(v, dict) else {}


def _live(rec: dict[str, Any] | None, family: str | None = None) -> LiveBlock | None:
    if rec is None:
        return None
    serving = _d(rec.get("serving"))
    if family is not None and serving.get("family") not in (family, None):
        return None
    try:
        day = _dt.date.fromisoformat(str(rec["day"]))
    except ValueError:
        return None
    cov = _d(rec.get("coverage"))
    cal = _d(_d(_d(rec.get("calibration")).get("all")).get("p_win"))
    drift = _d(rec.get("feature_drift"))
    top = drift.get("top") if isinstance(drift.get("top"), list) else []
    first = _d(top[0]) if top else {}
    sd = _d(_d(_d(rec.get("score_drift")).get("all")).get("e_r"))
    ret = _d(rec.get("last_retrain"))
    n = cal.get("n")
    return LiveBlock(
        day=day,
        serving=serving.get("version"),
        status=serving.get("status"),
        pct_scored=_f(cov.get("pct_scored")),
        candidates=int(cov["candidates"]) if isinstance(cov.get("candidates"), int) else None,
        latency_ms_p95=int(cov["latency_ms_p95"])
        if isinstance(cov.get("latency_ms_p95"), int)
        else None,
        calibration=cal.get("status"),
        calibration_n=int(n) if isinstance(n, int) else None,
        brier=_f(cal.get("brier")),
        brier_base=_f(cal.get("brier_base")),
        drift_status=drift.get("status"),
        worst_psi=_f(first.get("psi")),
        worst_psi_feature=first.get("feature"),
        drift_warn=len(drift["warn"]) if isinstance(drift.get("warn"), list) else None,
        score_psi=_f(sd.get("psi")),
        parity=_d(rec.get("parity")).get("status"),
        last_retrain=str(ret.get("at"))[:10] if ret.get("at") else None,
    )


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


def _row(paths: MlPaths, fam: _Family, version: str, e: _VersionEntry) -> ModelRow:
    label, kind, purpose = _meta(fam.name)
    vdir = paths.path(paths.models) / fam.name / version
    man = _manifest(paths, fam.name, version)
    status = e.status if e.status in _STATUSES else "candidate"
    if kind == "quantile":
        stats, _ = _quantile_stats(_QuantileEval(vdir, man), fam.holdout_reads)
    else:
        stats, _ = _outcome_stats(_OutcomeEval(vdir, man), fam.holdout_reads, e.summary)
    return ModelRow(
        family=fam.name,
        label=label,
        family_kind=kind,
        purpose=purpose,
        version=version,
        status=status,  # type: ignore[arg-type]
        created=_created(e.created),
        source=_source(man),
        rule=e.rule or FAMILY_RULE.get(fam.name),
        rule_passed=e.rule_passed,
        holdout_reads=fam.holdout_reads,
        stats=stats,
    )


def _planned(name: str) -> ModelRow:
    label, kind, purpose = _meta(name)
    return ModelRow(
        family=name,
        label=label,
        family_kind=kind,
        purpose=purpose,
        status="planned",
        rule=FAMILY_RULE.get(name),
    )


_ORDER = {"champion": 0, "shadow": 1, "candidate": 2, "planned": 3, "retired": 4}


def load_ml_models(
    paths: MlPaths, *, now: _dt.datetime, window_start: str = "21:30"
) -> MlModelsResponse:
    """Every family: its champion / shadow / candidate versions (retired ones only on the
    detail page), a planned row for a family with no version, and the strip numbers."""
    fams = _families(paths)
    items: list[ModelRow] = []
    counts: list[FamilyCounts] = []
    for name, fam in fams.items():
        label = _meta(name)[0]
        if fam is None or not fam.versions:
            items.append(_planned(name))
            counts.append(FamilyCounts(family=name, label=label))
            continue
        c: dict[str, int] = {}
        for e in fam.versions.values():
            c[e.status] = c.get(e.status, 0) + 1
        counts.append(
            FamilyCounts(
                family=name,
                label=label,
                champion=fam.champion,
                counts=dict(sorted(c.items())),
                holdout_reads=fam.holdout_reads,
            )
        )
        live = [(v, e) for v, e in fam.versions.items() if e.status != "retired"]
        for v, e in live:
            items.append(_row(paths, fam, v, e))
    epoch = _dt.datetime.min.replace(tzinfo=_dt.UTC)
    items.sort(
        key=lambda r: (
            _ORDER[r.status],
            list(fams).index(r.family),
            -(r.created or epoch).timestamp(),
        )
    )
    st = load_ml_studies(paths, now=now, window_start=window_start)
    rec = _health(paths)
    lv = _live(rec)
    champs = [r.family for r in items if r.status == "champion"]
    summary = MlSummary(
        champions=len(champs),
        families=len(fams),
        champion_families=champs,
        in_shadow=sum(1 for r in items if r.status == "shadow"),
        studies_proposed=st.proposed,
        studies_queued=st.queued,
        live_calibration=lv.calibration if lv else None,
        live_calibration_n=lv.calibration_n if lv else None,
        worst_psi=lv.worst_psi if lv else None,
        worst_psi_feature=lv.worst_psi_feature if lv else None,
        health_day=lv.day if lv else None,
        running=_queue_running(paths),
        queued=sum(1 for e in _queue(paths) if e.get("status") == "queued"),
        window_start=window_start,
    )
    return MlModelsResponse(as_of=now, summary=summary, families=counts, items=items)


def load_ml_model(
    paths: MlPaths, family: str, version: str, *, now: _dt.datetime
) -> MlModelDetailResponse | None:
    """One version's detail page, or None when the family or version is unknown."""
    if not _VERSION_RE.match(family) or not _VERSION_RE.match(version):
        return None
    fam = _families(paths).get(family)
    if fam is None or version not in fam.versions:
        return None
    e = fam.versions[version]
    vdir = paths.path(paths.models) / family / version
    man = _manifest(paths, family, version)
    row = _row(paths, fam, version, e)
    groups = _groups(paths, vdir, man)
    timings = {
        v: (
            _cost(paths.path(paths.models) / family / v, _manifest(paths, family, v)) or Cost()
        ).train_s
        for v in fam.versions
    }
    versions = sorted(
        (
            VersionRow(
                version=v,
                status=ve.status if ve.status in _STATUSES else "candidate",  # type: ignore[arg-type]
                created=_created(ve.created),
                rule_passed=ve.rule_passed,
                source=_source(_manifest(paths, family, v)),
                train_s=timings.get(v),
            )
            for v, ve in fam.versions.items()
        ),
        key=lambda r: r.created or _dt.datetime.min.replace(tzinfo=_dt.UTC),
        reverse=True,
    )
    live = _live(_health(paths), family) if row.status in ("champion", "shadow") else None
    common: dict[str, Any] = {
        "as_of": now,
        "model": row,
        "feature_groups": groups,
        "versions": versions,
        "cost": _cost(vdir, man),
        "live": live,
    }
    if row.family_kind == "quantile":
        q = _QuantileEval(vdir, man)
        _, head = _quantile_stats(q, fam.holdout_reads)
        detail: QuantileDetail | OutcomeDetail = QuantileDetail(
            **common,
            headline=head,
            spec=_quantile_spec(man, q, vdir, groups),
            rule=q.rule(),
            coverage=q.coverage(),
            loss=q.loss(),
            importance=q.importance(),
            regimes=q.regimes(),
            rolling=q.rolling(),
        )
    else:
        o = _OutcomeEval(vdir, man)
        _, head = _outcome_stats(o, fam.holdout_reads, e.summary)
        detail = OutcomeDetail(
            **common,
            headline=head,
            spec=_outcome_spec(man, o, groups),
            rule=o.rule(),
            reliability=o.reliability(),
            loss=o.loss(),
            importance=o.importance(),
            regimes=o.regimes(),
            replay=o.replay(),
        )
    return MlModelDetailResponse(detail=detail)


# ---------------------------------------------------------------------------
# Studies
# ---------------------------------------------------------------------------


def _queue(paths: MlPaths) -> list[dict[str, Any]]:
    raw = _json(paths.path(paths.queue))
    entries = raw.get("entries") if isinstance(raw, dict) else None
    return [e for e in entries or [] if isinstance(e, dict) and isinstance(e.get("id"), str)]


def _queue_running(paths: MlPaths) -> str | None:
    return next((e["id"] for e in _queue(paths) if e.get("status") == "running"), None)


def _specs(paths: MlPaths) -> dict[str, dict[str, Any]]:
    d = paths.path(paths.specs)
    out: dict[str, dict[str, Any]] = {}
    if not d.is_dir():
        return out
    for f in sorted(d.glob("ML-*.yaml")):
        try:
            raw = yaml.safe_load(f.read_text()) or {}
        except (OSError, yaml.YAMLError):
            continue
        if isinstance(raw, dict) and _ML_ID_RE.match(str(raw.get("id", ""))):
            out[str(raw["id"])] = {**raw, "_file": f"{paths.specs}/{f.name}"}
    return out


def _retired(paths: MlPaths) -> set[str]:
    f = paths.path(paths.specs) / "retired.yaml"
    try:
        raw = yaml.safe_load(f.read_text()) if f.is_file() else None
    except (OSError, yaml.YAMLError):
        return set()
    rows = raw.get("retired") if isinstance(raw, dict) else None
    return {str(r.get("id")) for r in rows or [] if isinstance(r, dict)}


def _backlog(paths: MlPaths) -> dict[str, dict[str, Any]]:
    if paths.backlog is None:
        return {}
    raw = _json(paths.backlog)
    items = raw.get("items") if isinstance(raw, dict) else None
    out: dict[str, dict[str, Any]] = {}
    for e in (items or {}).values() if isinstance(items, dict) else []:
        if isinstance(e, dict) and e.get("kind") == "ml" and _ML_ID_RE.match(str(e.get("id", ""))):
            out[str(e["id"])] = e
    return out


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{100 * x:+.0f}%".replace("-", "−")


def _rung_line(e: dict[str, Any]) -> str:
    """One ledger line in words (the CLI's ``ml_rung_line``, plan ids removed)."""
    rung = str(e.get("rung", ""))
    if rung == "verdict":
        st = str(e.get("status") or "candidate").title()
        return f"Verdict · {e.get('verdict')} · {e.get('version')} → {st}"
    if rung == "dropped":
        return f"Dropped · {scrub_ids(str(e.get('note') or ''))}".rstrip(" ·")
    year = rung.removeprefix("dev_")
    ok = "pass" if e.get("rung_pass") else "fail"
    vs = _d(e.get("vs_incumbent"))
    if e.get("family") == "m1":
        cov = _f(vs.get("cov_err_max"))
        body = f"pinball {_pct(_f(vs.get('pinball_rel')))} vs incumbent" + (
            f", cov err {100 * cov:.1f} pts" if cov is not None else ""
        )
    else:
        body = (
            f"log-loss {_pct(_f(vs.get('log_loss_rel')))} vs incumbent, E[R] IC {vs.get('er_ic')}"
        )
    n = len(e.get("variants") or [])
    pick = f" · best of {n}" if n > 1 else ""
    return f"Dev {year} · {ok} · {body}{pick}"


def _steps(
    status: str, ledger: list[dict[str, Any]], family_status: str | None
) -> tuple[list[StudyStep], str]:
    done: set[str] = {"proposed"}
    failed: str | None = None
    notes: dict[str, str] = {}
    if status not in ("proposed", "dropped"):
        done.add("approved")
    for e in ledger:
        r = str(e.get("rung", ""))
        if r in ("dev_2022", "dev_2023"):
            if e.get("rung_pass") is False and e.get("clear_fail"):
                failed = r
            else:
                done.add(r)
            notes[r] = "pass" if e.get("rung_pass") else "fail"
        if r == "verdict":
            done.add("verdict")
            notes["verdict"] = str(e.get("verdict") or "")
            if e.get("verdict") == "fail":
                failed = "verdict"
                done.discard("verdict")
    if family_status in ("shadow", "champion", "retired"):
        done.add("shadow")
    if family_status == "champion":
        done.add("champion")
    if status == "dropped":
        failed = "approved"
    out = []
    reached = "proposed"
    for s in STEPS:
        state: Literal["done", "failed", "pending"] = (
            "failed" if s == failed else "done" if s in done else "pending"
        )
        if state == "done":
            reached = s
        out.append(StudyStep(step=s, label=STEP_LABEL[s], state=state, note=notes.get(s)))
    return out, STEP_LABEL[failed or reached]


def _variants(spec: dict[str, Any]) -> int:
    g = spec.get("grid")
    return len(g) if isinstance(g, list) and g else 1


def _study(
    sid: str,
    spec: dict[str, Any] | None,
    draft: dict[str, Any] | None,
    q: dict[str, Any] | None,
    ledger: list[dict[str, Any]],
    retired: bool,
    paths: MlPaths,
    position: int | None,
    default_rung_s: float,
) -> MlStudyItem:
    dspec = _d((draft or {}).get("ml_study_spec"))
    s = spec or dspec
    family = str(s.get("family")) if s.get("family") else (draft or {}).get("family")
    verdict_row = next((e for e in reversed(ledger) if e.get("rung") == "verdict"), None)
    dropped_row = next((e for e in reversed(ledger) if e.get("rung") == "dropped"), None)
    if q is not None and q.get("status") in ("queued", "running", "concluded", "stopped", "failed"):
        status = str(q["status"])
    elif dropped_row is not None or (draft or {}).get("status") == "dropped":
        status = "dropped"
    elif spec is not None:
        status = "concluded" if verdict_row else "approved"
    else:
        status = "proposed" if (draft or {}).get("status", "proposed") == "proposed" else "approved"
    fam_status = None
    version = verdict_row.get("version") if verdict_row else None
    if family and version:
        fam = _family(paths, str(family))
        if fam is not None and version in fam.versions:
            fam_status = fam.versions[version].status
    steps, stage = _steps(status, ledger, fam_status)
    hyp = scrub_ids(str(s.get("hypothesis") or (draft or {}).get("hypothesis") or ""))
    title = scrub_ids(str((draft or {}).get("title") or "")) or hyp
    title = re.sub(r"^ML draft:\s*", "", title)
    runtime = sum(_f(e.get("runtime_s")) or 0.0 for e in ledger) or None
    eta = None
    if status in ("queued", "running"):
        left = [
            r
            for r in ("dev_2022", "dev_2023", "verdict")
            if r not in {e.get("rung") for e in ledger}
        ]
        n = _variants(s)
        eta = float(sum(default_rung_s * (3 if r == "verdict" else n) for r in left))
    created = s.get("created") or (draft or {}).get("created")
    try:
        cdate = _dt.date.fromisoformat(str(created)[:10]) if created else None
    except ValueError:
        cdate = None
    last = None
    for e in ledger:
        at = _created(str(e.get("at"))) if e.get("at") else None
        if at and (last is None or at > last):
            last = at
    value = _d(s.get("study_value"))
    nights = (draft or {}).get("nights_needed") or value.get("nights_needed")
    return MlStudyItem(
        study_id=sid,
        title=title,
        hypothesis=hyp,
        family=str(family) if family else None,
        family_label=_meta(str(family))[0] if family else None,
        change_kind=str(s.get("change_kind"))
        if s.get("change_kind")
        else (draft or {}).get("change_kind"),
        status=status,  # type: ignore[arg-type]
        retired=retired,
        incumbent=str(s.get("incumbent")) if s.get("incumbent") else None,
        variants=_variants(s),
        steps=steps,
        stage=stage,
        verdict=verdict_row.get("verdict")
        if verdict_row
        else ("dropped" if status == "dropped" else None),
        version=str(version) if version else None,
        evidence=[_rung_line(e) for e in ledger],
        nights_needed=int(nights) if isinstance(nights, int) else None,
        runtime_s=runtime,
        eta_s=eta,
        queue_position=position,
        created=cdate,
        last_at=last,
    )


def _collect(paths: MlPaths, default_rung_s: float) -> dict[str, MlStudyItem]:
    specs = _specs(paths)
    drafts = _backlog(paths)
    queue = {
        e["id"]: e for e in _queue(paths) if e.get("kind") == "ml" and _ML_ID_RE.match(e["id"])
    }
    active = sorted(
        (e for e in _queue(paths) if e.get("status") in ("queued", "running")),
        key=lambda e: (str(e.get("approved_at", "")), e["id"]),
    )
    pos = {e["id"]: i + 1 for i, e in enumerate(active)}
    ledger: dict[str, list[dict[str, Any]]] = {}
    for e in _jsonl(paths.path(paths.ledger)):
        sid = str(e.get("study", ""))
        if _ML_ID_RE.match(sid):
            ledger.setdefault(sid, []).append(e)
    retired = _retired(paths)
    ids = set(specs) | set(drafts) | set(queue) | set(ledger)
    return {
        i: _study(
            i,
            specs.get(i),
            drafts.get(i),
            queue.get(i),
            sorted(ledger.get(i, []), key=lambda e: str(e.get("at", ""))),
            i in retired,
            paths,
            pos.get(i),
            default_rung_s,
        )
        for i in ids
    }


_STUDY_ORDER = {
    "running": 0,
    "queued": 1,
    "approved": 2,
    "proposed": 3,
    "concluded": 4,
    "stopped": 5,
    "failed": 6,
    "dropped": 7,
}


def load_ml_studies(
    paths: MlPaths,
    *,
    now: _dt.datetime,
    window_start: str = "21:30",
    default_rung_s: float = 2400.0,
) -> MlStudiesResponse:
    """Every ``ML-<n>``: proposed drafts, approved/queued/running ones, verdicts."""
    items = list(_collect(paths, default_rung_s).values())
    items.sort(
        key=lambda it: (it.retired, _STUDY_ORDER.get(it.status, 9), -int(it.study_id.split("-")[1]))
    )
    return MlStudiesResponse(
        as_of=now,
        items=items,
        proposed=sum(1 for i in items if i.status == "proposed" and not i.retired),
        queued=sum(1 for i in items if i.status in ("queued", "running")),
        running=next((i.study_id for i in items if i.status == "running"), None),
        window_start=window_start,
    )


def _fmt_overlay(v: Any, prefix: str = "") -> list[str]:
    if isinstance(v, dict):
        out: list[str] = []
        for k, x in v.items():
            out += _fmt_overlay(x, f"{prefix}{k}.")
        return out
    if isinstance(v, list) and not v:
        return []
    return [f"{prefix.rstrip('.')}: {v}"]


def load_ml_study(
    paths: MlPaths, study_id: str, *, now: _dt.datetime, default_rung_s: float = 2400.0
) -> MlStudyDetailResponse | None:
    if not _ML_ID_RE.match(study_id):
        return None
    items = _collect(paths, default_rung_s)
    it = items.get(study_id)
    if it is None:
        return None
    spec = _specs(paths).get(study_id)
    draft = _backlog(paths).get(study_id)
    s = spec or _d((draft or {}).get("ml_study_spec"))
    overlay = _d(s.get("overlay"))
    grid = [g for g in s.get("grid") or [] if isinstance(g, dict)]
    rule = _d(s.get("decision_rule")).get("kind")
    kv = [
        KV(label="Hypothesis", value=it.hypothesis or "—"),
        KV(label="Family", value=it.family_label or "—"),
        KV(label="Change", value=(it.change_kind or "—").replace("_", " ").title()),
        KV(label="Incumbent", value=it.incumbent or "—"),
        KV(
            label="Challenger",
            value="; ".join(_fmt_overlay(overlay)) or "the incumbent's config",
        ),
        KV(
            label="Grid",
            value=f"{len(grid)} configs" if grid else "1 config · no hyper-parameter search",
        ),
        KV(
            label="Decision Rule",
            value=f"{rule} · pre-registered, unchanged" if rule else "—",
        ),
        KV(label="Holdout", value="never read by a study; promotion to champion is owner-only"),
        KV(label="If It Passes", value="registers as shadow (live scoring, no decision change)"),
    ]
    if spec is not None:
        kv.append(KV(label="Spec File", value=str(spec.get("_file"))))
    eff = _d((draft or {}).get("expected_effect"))
    expected = scrub_ids(str(eff.get("source"))) if eff.get("source") else None
    return MlStudyDetailResponse(
        as_of=now,
        study=it,
        spec=kv,
        overlay=overlay,
        grid=grid,
        rule=rule,
        spec_file=str(spec.get("_file")) if spec else None,
        expected_effect=expected,
    )
