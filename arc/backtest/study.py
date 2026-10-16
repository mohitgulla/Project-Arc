"""Backtest study framework (card E7.12, PLAN D84): pre-registered, hash-locked studies.

One standard way to ask the backtest a question. A study is a committed spec
(``config/backtest_studies/BT-<n>.yaml``) whose decision rule is written before any
run. :func:`run_study` runs it through the E7.11 live-shaped simulator
(:mod:`arc.backtest.sim_report`) and enforces the protocol:

1. **Dev** (``phase=dev``, the spec's dev period) on any rung of the checkpoint
   ladder (C125 → C250 → C500). Iterate freely; every run is appended to the
   **research ledger** (``data/backtest/ledger.jsonl``: spec hash, arm overlays,
   universe, period, metrics, git sha). The ledger counts distinct arm sets
   ("variants") per study.
2. **Confirm** on the larger rungs, same arms: the report flags a sign flip of the
   primary metric's treatment − control difference vs the first rung.
3. **Holdout** (``phase=holdout``): one run per spec hash, ever. Refused when the
   ledger already holds a holdout for that hash (or for the same arm set of the same
   study under an edited text), or when the study's variants exceed ``max_variants``
   without ``allow_overfit_warning`` (which is then stamped in the report and ledger).
4. **Verdict**: the D25 rule (:func:`arc.backtest.ranking.challenge`, unchanged) is
   applied to the holdout and printed verbatim with its bootstrap CI. A verdict only
   recommends; live changes go through D44 (``Experiment: XP-<n>``) or an owner waiver.

Hashing reuses the E10.1 pre-registration lock (:func:`arc.experiments.models.document_hash`,
the canonical-JSON SHA-256 of ``spec_hash``), and arm overlays are validated by the
same per-file validators as ``arc experiment`` (:func:`arc.experiments.overlay.validate_target`)
and merged with the same :func:`arc.utils.yamlpatch.deep_merge` as ``--experiment``.

Reports (``docs/RESEARCH/studies/<id>.md`` + ``.json``) carry aggregates only, never
raw quote rows (ThetaData retail terms). Read-only to trading: nothing here touches
the live loop, the gate or ``data/arc.db``.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import pandas as pd
import structlog
import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from arc.backtest.ranking import (
    BootstrapSpec,
    DecisionRule,
    RankRun,
    challenge,
    summarize,
)
from arc.experiments.models import document_hash
from arc.scanner.rank import Ranker, applicable, incumbent_for
from arc.utils.yamlpatch import deep_merge

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from arc.backtest.costs import CostModel
    from arc.backtest.ranking import RankingFile
    from arc.backtest.sim import SimRun
    from arc.config import ArcSettings
    from arc.exits.policy import ExitConfig

log = structlog.get_logger()

__all__ = [
    "DEFAULT_LEDGER",
    "DEFAULT_REPORT_DIR",
    "DEFAULT_RUNS_DIR",
    "LIQUIDITY_BUCKETS",
    "STUDIES_DIR",
    "SUBPERIODS",
    "ArmMetrics",
    "LedgerEntry",
    "StudyArm",
    "StudyArms",
    "StudyPeriod",
    "StudyRefusedError",
    "StudyRule",
    "StudySpec",
    "arm_set_hash",
    "check_holdout",
    "compare_runs",
    "ladder_flags",
    "load_ledger",
    "load_study",
    "render_report",
    "run_study",
    "study_hash",
    "study_path",
    "variants",
]

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
STUDIES_DIR = REPO_ROOT / "config" / "backtest_studies"
DEFAULT_LEDGER = Path("data/backtest/ledger.jsonl")
DEFAULT_RUNS_DIR = Path("data/backtest/studies")
DEFAULT_REPORT_DIR = Path("docs/RESEARCH/studies")

#: Config files an arm overlay may patch (the sim knobs live in ranking.yaml backtest.sim).
STUDY_TARGETS: tuple[str, ...] = ("ranking", "exits", "costs")
Universe = Literal["c125", "c250", "c500"]
_LADDER_ORDER: dict[str, int] = {"c125": 0, "c250": 1, "c500": 2}
_ID_RE = re.compile(r"^BT-[1-9]\d*$")
_TREATMENT_RE = re.compile(r"^t[1-3]$")
_FORBID = ConfigDict(extra="forbid", frozen=True)

#: Fixed report sub-periods (entry date, inclusive). The last runs to the data end.
SUBPERIODS: tuple[tuple[str, dt.date, dt.date | None], ...] = (
    ("2020 crash", dt.date(2020, 2, 1), dt.date(2020, 4, 30)),
    ("2020-21 bull", dt.date(2020, 5, 1), dt.date(2021, 12, 31)),
    ("2022 bear", dt.date(2022, 1, 1), dt.date(2022, 12, 31)),
    ("2023", dt.date(2023, 1, 1), dt.date(2023, 12, 31)),
    ("2024 -> latest", dt.date(2024, 1, 1), None),
)
#: Liquidity buckets on the name's option contract volume that session (D9 screen tuning).
LIQUIDITY_BUCKETS: tuple[tuple[str, float], ...] = (
    ("< 10k", 10_000.0),
    ("10k-100k", 100_000.0),
    ("100k-1M", 1_000_000.0),
    (">= 1M", math.inf),
)
NOT_REPLAYABLE = (
    "Not replayed: Discovery and Trending tiers (no historical LLM, Reddit or Stocktwits "
    "inputs), the LLM stance (a deterministic trend-label proxy stands in), intraday "
    "paths (EOD marks only; exits fire on closes)."
)
PrimaryMetric = Literal["net_pnl", "sharpe", "sortino", "cagr", "return_pct", "profit_factor"]


class StudyRefusedError(RuntimeError):
    """The protocol refuses this run (second holdout, too many variants, bad universe)."""


# ---------------------------------------------------------------------------
# Spec
# ---------------------------------------------------------------------------


class StudyArm(BaseModel):
    """One arm: an optional ranker plus config overlays (empty = the base config)."""

    model_config = _FORBID

    ranker: Ranker | None = Field(
        None, description="Ranker this arm walks with; None = the profile's incumbent"
    )
    overlay: dict[str, dict[str, Any]] = Field(
        default_factory=dict,
        description="config file stem (ranking | exits | costs) -> partial file, deep-merged",
    )

    @field_validator("overlay")
    @classmethod
    def _targets(cls, v: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        for target, body in v.items():
            if target not in STUDY_TARGETS:
                msg = f"overlay target {target!r} is not one of {', '.join(STUDY_TARGETS)}"
                raise ValueError(msg)
            if not body:
                msg = f"overlay target {target!r} is empty; drop it"
                raise ValueError(msg)
        return v

    def ranker_for(self, *, allows_credit: bool) -> Ranker:
        return self.ranker or incumbent_for(allows_credit=allows_credit)


class StudyArms(BaseModel):
    """A control plus 1-3 treatments (``t1`` .. ``t3``)."""

    model_config = _FORBID

    control: StudyArm = Field(default_factory=lambda: StudyArm())
    treatments: dict[str, StudyArm] = Field(..., min_length=1, max_length=3)

    @field_validator("treatments")
    @classmethod
    def _names(cls, v: dict[str, StudyArm]) -> dict[str, StudyArm]:
        for name in v:
            if not _TREATMENT_RE.match(name):
                msg = f"treatment arm {name!r}: name it t1..t3"
                raise ValueError(msg)
        return {k: v[k] for k in sorted(v)}

    @property
    def names(self) -> list[str]:
        return ["control", *self.treatments]

    def arm(self, name: str) -> StudyArm:
        return self.control if name == "control" else self.treatments[name]


class StudyPeriod(BaseModel):
    """Dev and holdout decision windows. The holdout is locked until ``--holdout``."""

    model_config = _FORBID

    dev_start: dt.date = dt.date(2020, 1, 1)
    dev_end: dt.date = dt.date(2023, 12, 31)
    holdout_start: dt.date = dt.date(2024, 1, 1)
    holdout_end: dt.date | None = Field(None, description="None = the latest data session")
    settle_days: int = Field(
        90, ge=0, description="Calendar days read past a window's last decision so exits settle"
    )

    @model_validator(mode="after")
    def _order(self) -> StudyPeriod:
        if not self.dev_start <= self.dev_end < self.holdout_start:
            msg = "period: need dev_start <= dev_end < holdout_start"
            raise ValueError(msg)
        if self.holdout_end is not None and self.holdout_end < self.holdout_start:
            msg = "period: holdout_end is before holdout_start"
            raise ValueError(msg)
        return self


#: Pre-registered rule kinds. Each one is an earlier card's rule, applied verbatim:
#:
#: * ``d25_challenge`` — E7.5 / D25 (:func:`arc.backtest.ranking.challenge`): treatment
#:   beats control on net P&L and max DD in >= ``min_subperiod_wins`` trend sub-periods
#:   **and** the CI lower bound of the daily P&L difference is > 0.
#: * ``entry_filter`` — E16.3 (:func:`arc.backtest.entry_filter.entry_filter_challenge`):
#:   treatment = filter on, control = off. Recommend if the treatment has a smaller max
#:   DD, net P&L not lower in >= ``min_subperiod_wins`` sub-periods and a CI lower bound
#:   > −``ci_floor_frac_of_base`` × |control net P&L|.
#: * ``keep_control`` — E18.3 (``scripts/exit_policy_report.keep_rule``): keep the
#:   control (the shipped policy) if its net P&L is not lower than the treatment's in
#:   >= ``min_subperiod_wins`` sub-periods **and** its max DD is not higher than the
#:   treatment's by more than ``dd_tolerance``; otherwise recommend the treatment.
#: * ``regime_keep`` — E17.3 (:func:`arc.backtest.regime_compare.regime_verdict`): keep
#:   the control if, in every profile, it is not worse than the treatment on net P&L and
#:   max DD and the CI upper bound of (control − treatment) is > 0; roll back if it is
#:   worse on both in any profile; otherwise keep, inconclusive.
RuleKind = Literal["d25_challenge", "entry_filter", "keep_control", "regime_keep"]


class StudyRule(BaseModel):
    """The pre-registered decision rule (see :data:`RuleKind`)."""

    model_config = _FORBID

    kind: RuleKind = "d25_challenge"
    min_subperiod_wins: int = Field(2, ge=1)
    subperiods: list[str] = Field(default_factory=lambda: ["bear", "sideways", "bull"])
    bootstrap: BootstrapSpec = Field(default_factory=lambda: BootstrapSpec())
    ci_floor_frac_of_base: float | None = Field(
        None, ge=0.0, le=1.0, description="entry_filter: CI floor as a share of |control P&L|"
    )
    dd_tolerance: float | None = Field(
        None, ge=0.0, le=1.0, description="keep_control: control's max DD may exceed by this"
    )
    verbatim: str | None = Field(
        None, description="The original card's rule text, quoted unchanged in the report"
    )

    @model_validator(mode="after")
    def _params(self) -> StudyRule:
        if self.kind == "entry_filter" and self.ci_floor_frac_of_base is None:
            msg = "decision_rule: entry_filter needs ci_floor_frac_of_base"
            raise ValueError(msg)
        if self.kind == "keep_control" and self.dd_tolerance is None:
            msg = "decision_rule: keep_control needs dd_tolerance"
            raise ValueError(msg)
        return self

    @property
    def decision(self) -> DecisionRule:
        return DecisionRule(
            min_subperiod_wins=self.min_subperiod_wins, subperiods=list(self.subperiods)
        )

    def text(self) -> str:
        """The rule as the deciding code states it (``verbatim`` is quoted separately)."""
        ci = self.bootstrap.ci
        subs = ", ".join(self.subperiods)
        n = len(self.subperiods)
        if self.kind == "entry_filter":
            from arc.backtest.entry_filter import EntryFilterRule

            return EntryFilterRule(
                min_subperiods_not_lower=self.min_subperiod_wins,
                ci_floor_frac_of_base=float(self.ci_floor_frac_of_base or 0.0),
            ).text(ci, self.subperiods)
        if self.kind == "keep_control":
            return (
                f"Keep the control if its net P&L is not lower than the treatment's in ≥ "
                f"{self.min_subperiod_wins} of {n} trend sub-periods ({subs}) **and** its "
                f"max drawdown is not higher than the treatment's by more than "
                f"{float(self.dd_tolerance or 0.0):.0%}. Otherwise recommend the treatment "
                f"(as a future XP arm, never a direct flip)."
            )
        if self.kind == "regime_keep":
            return (
                "Keep the control if, in every profile, it is not worse than the treatment "
                "on net P&L **and** max drawdown, **and** the upper bound of the "
                f"{ci:.0%} block-bootstrap CI of the daily P&L difference (control − "
                "treatment) is > 0. Roll back to the treatment if the control is worse on "
                "both net P&L and drawdown in any profile. Otherwise keep the control, "
                "inconclusive."
            )
        return self.decision.text(ci)


class StudySpec(BaseModel):
    """``config/backtest_studies/BT-<n>.yaml`` (hash-locked by :func:`study_hash`)."""

    model_config = _FORBID

    id: str
    hypothesis: str = Field(..., min_length=1)
    owner: str = Field(..., min_length=1)
    created: dt.date
    arms: StudyArms
    profiles: list[Literal["margin", "cash_debit", "cash_long_only"]] = Field(..., min_length=1)
    universe_ladder: list[Universe] = Field(default_factory=lambda: ["c125", "c500"])
    period: StudyPeriod = Field(default_factory=lambda: StudyPeriod())
    decision_rule: StudyRule = Field(default_factory=lambda: StudyRule())
    primary_metric: PrimaryMetric = "net_pnl"
    max_variants: int = Field(3, ge=1)

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        if not _ID_RE.match(v):
            msg = f"study id {v!r}: use BT-<n>"
            raise ValueError(msg)
        return v

    @field_validator("universe_ladder")
    @classmethod
    def _ladder(cls, v: list[str]) -> list[str]:
        if not v:
            msg = "universe_ladder needs at least one checkpoint"
            raise ValueError(msg)
        if len(set(v)) != len(v) or v != sorted(v, key=_LADDER_ORDER.__getitem__):
            msg = "universe_ladder must be unique and ascending (c125 -> c250 -> c500)"
            raise ValueError(msg)
        return v


#: Optional rule fields added by E7.14. Left out of the hash while unset, so a spec
#: written before them (BT-1) keeps its hash and its ledger lines / holdout lock.
_OPTIONAL_RULE_FIELDS: tuple[str, ...] = ("ci_floor_frac_of_base", "dd_tolerance", "verbatim")


def study_hash(spec: StudySpec) -> str:
    """The pre-registration lock: canonical-JSON SHA-256 of the whole spec (E10.1)."""
    doc = spec.model_dump(mode="json")
    rule = doc["decision_rule"]
    for k in _OPTIONAL_RULE_FIELDS:
        if rule.get(k) is None:
            rule.pop(k, None)
    return document_hash(doc)


def arm_set_hash(spec: StudySpec) -> str:
    """One variant = one distinct arm set (rankers + overlays + profiles)."""
    return document_hash(
        {"arms": spec.arms.model_dump(mode="json"), "profiles": list(spec.profiles)}
    )


def study_path(study_id: str, studies_dir: Path | None = None) -> Path:
    return (studies_dir or STUDIES_DIR) / f"{study_id}.yaml"


def _base(target: str, paths: Mapping[str, Path | None]) -> dict[str, Any]:
    p = paths.get(target) or REPO_ROOT / "config" / f"{target}.yaml"
    return yaml.safe_load(Path(p).read_text()) or {}


def arm_configs(
    spec: StudySpec, arm: str, *, paths: Mapping[str, Path | None] | None = None
) -> tuple[RankingFile, ExitConfig, CostModel]:
    """(ranking, exits, costs) for *arm*: each base file with the arm's overlay merged,
    validated by the same per-file validators as ``arc experiment``."""
    from arc.backtest.costs import CostModel
    from arc.backtest.ranking import RankingFile
    from arc.exits.policy import ExitConfig
    from arc.experiments.overlay import validate_target

    a = spec.arms.arm(arm)
    out: dict[str, object] = {}
    for target in STUDY_TARGETS:
        merged = deep_merge(_base(target, paths or {}), a.overlay.get(target, {}))
        validate_target(target, merged)
        out[target] = merged
    ranking = RankingFile.model_validate(out["ranking"])
    exits = ExitConfig.model_validate(out["exits"])
    c = out["costs"]
    assert isinstance(c, dict)
    cost = CostModel.model_validate(c.get("costs", c))
    return ranking, exits, cost


def load_study(path: Path | str, *, paths: Mapping[str, Path | None] | None = None) -> StudySpec:
    """Parse a spec and check every arm's overlay still validates on the base files."""
    p = Path(path)
    data = yaml.safe_load(p.read_text()) or {}
    if not isinstance(data, dict):
        msg = f"{p}: top level must be a mapping"
        raise ValueError(msg)
    spec = StudySpec.model_validate(data)
    if p.stem != spec.id:
        msg = f"{p}: file name must be {spec.id}.yaml"
        raise ValueError(msg)
    for arm in spec.arms.names:
        try:
            arm_configs(spec, arm, paths=paths)
        except ValueError as exc:
            why = " | ".join(str(exc).strip().splitlines()[:4])
            msg = f"{spec.id}: {arm} overlay does not validate: {why}"
            raise ValueError(msg) from exc
    return spec


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------


class LedgerEntry(BaseModel):
    """One study run (``data/backtest/ledger.jsonl``, append-only)."""

    model_config = ConfigDict(extra="forbid")

    at: dt.datetime
    study: str
    spec_hash: str
    arm_set_hash: str
    arms: dict[str, Any] = Field(..., description="The arm set (rankers + overlays)")
    profiles: list[str]
    phase: Literal["dev", "holdout"]
    universe: str
    universe_version: str
    partitions: dict[str, int]
    start: dt.date
    end: dt.date
    data_end: dt.date
    git_sha: str
    primary_metric: str
    #: profile -> arm -> metric -> value (rounded aggregates only)
    metrics: dict[str, dict[str, dict[str, Any]]]
    #: profile -> treatment -> D25 challenge row (+ the primary metric's difference)
    verdicts: dict[str, dict[str, dict[str, Any]]]
    variants: int = Field(..., description="Distinct arm sets of this study, this run included")
    overfit_warning: bool = False
    run_dir: str


def load_ledger(path: Path) -> list[LedgerEntry]:
    if not path.exists():
        return []
    return [LedgerEntry.model_validate_json(line) for line in path.read_text().splitlines() if line]


def _append(path: Path, entry: LedgerEntry) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as fh:
        fh.write(entry.model_dump_json() + "\n")


def variants(entries: Sequence[LedgerEntry], study: str, current: str | None = None) -> int:
    """Distinct arm sets tried for *study* (plus *current*, if it is new)."""
    seen = {e.arm_set_hash for e in entries if e.study == study}
    if current is not None:
        seen.add(current)
    return len(seen)


def check_holdout(
    spec: StudySpec,
    entries: Sequence[LedgerEntry],
    *,
    allow_overfit_warning: bool,
) -> bool:
    """Raise :class:`StudyRefusedError` unless a holdout run is allowed; returns whether
    the run carries the overfit warning."""
    h, a = study_hash(spec), arm_set_hash(spec)
    for e in entries:
        if e.phase != "holdout":
            continue
        if e.spec_hash == h:
            msg = (
                f"{spec.id}: the holdout already ran for spec {h[:12]} on {e.at:%Y-%m-%d}; "
                "one holdout per spec hash"
            )
            raise StudyRefusedError(msg)
        if e.study == spec.id and e.arm_set_hash == a:
            msg = (
                f"{spec.id}: the holdout already ran for this arm set under spec "
                f"{e.spec_hash[:12]}; editing the text does not buy a second holdout"
            )
            raise StudyRefusedError(msg)
    n = variants(entries, spec.id, a)
    if n > spec.max_variants:
        if not allow_overfit_warning:
            msg = (
                f"{spec.id}: {n} variants tried > max_variants {spec.max_variants}; "
                "pass --allow-overfit-warning to run the holdout anyway (stamped in the report)"
            )
            raise StudyRefusedError(msg)
        return True
    return False


def ladder_flags(
    entries: Sequence[LedgerEntry], spec: StudySpec, current: LedgerEntry | None = None
) -> list[dict[str, Any]]:
    """Dev results per rung for this arm set, with a sign-flip flag vs the first rung.

    The flag compares the sign of the primary metric's treatment − control difference
    per profile × treatment with the earliest dev run on the lowest rung.
    """
    a = arm_set_hash(spec)
    rows = [e for e in entries if e.study == spec.id and e.arm_set_hash == a]
    if current is not None:
        rows.append(current)
    first: dict[tuple[str, str], float] = {}
    out: list[dict[str, Any]] = []
    for e in sorted(rows, key=lambda e: (e.phase != "dev", _LADDER_ORDER.get(e.universe, 9), e.at)):
        for p, tr in sorted(e.verdicts.items()):
            for t, v in sorted(tr.items()):
                d = float(v.get("primary_diff", 0.0))
                key = (p, t)
                ref = first.setdefault(key, d) if e.phase == "dev" else first.get(key)
                flip = ref is not None and d != 0 and ref != 0 and (d > 0) != (ref > 0)
                out.append(
                    {
                        "at": e.at.isoformat(timespec="seconds"),
                        "phase": e.phase,
                        "universe": e.universe,
                        "profile": p,
                        "treatment": t,
                        "primary_diff": round(d, 4),
                        "ci_lo": v.get("ci_lo"),
                        "ci_hi": v.get("ci_hi"),
                        "switch": v.get("switch"),
                        "sign_flip_vs_first_rung": bool(flip),
                    }
                )
    return out


# ---------------------------------------------------------------------------
# Metrics (aggregates only)
# ---------------------------------------------------------------------------


class ArmMetrics(BaseModel):
    """The standard per arm × profile metrics."""

    model_config = ConfigDict(extra="forbid")

    trades: int
    net_pnl: float
    return_pct: float = Field(..., description="On the sim equity (D80 $25K)")
    cagr: float | None
    max_dd_pct: float
    sharpe: float | None
    sortino: float | None
    profit_factor: float | None
    win_rate: float | None
    avg_days_held: float | None
    turnover_per_month: float
    cost_share_of_gross: float | None
    capacity_clipped_share: float | None
    exit_reasons: dict[str, float]
    skips: dict[str, int]


def _f(x: object, nd: int = 4) -> float | None:
    if x is None:
        return None
    v = float(x)  # type: ignore[arg-type]
    return round(v, nd) if math.isfinite(v) else None


def _as_rank_run(run: SimRun) -> RankRun:
    return RankRun(
        ranker=run.ranker,
        profile=run.profile,
        slippage=0.0,
        trades=run.trades,
        equity=run.equity,
        skipped=run.skip_counts,
    )


def arm_metrics(run: SimRun, equity0: float) -> ArmMetrics:
    s = summarize(_as_rank_run(run), equity0)
    t = run.trades
    n = len(t)
    eq = run.equity
    final = float(eq.iloc[-1]) if len(eq) else equity0
    pf: float | None = None
    clipped: float | None = None
    mix: dict[str, float] = {}
    if n:
        wins = float(t.loc[t["pnl"] > 0, "pnl"].sum())
        losses = -float(t.loc[t["pnl"] < 0, "pnl"].sum())
        pf = wins / losses if losses > 0 else None
        if "capacity_clipped" in t:
            clipped = float(t["capacity_clipped"].astype(bool).mean())
        vc = t["exit_reason"].astype(str).value_counts(normalize=True)
        mix = {str(k): round(float(v), 4) for k, v in sorted(vc.items())}
    return ArmMetrics(
        trades=n,
        net_pnl=round(float(s["net_pnl"]), 2),  # type: ignore[arg-type]
        return_pct=round((final / equity0 - 1.0) * 100, 2),
        cagr=_f(s["cagr"]),
        max_dd_pct=round(float(s["max_dd_pct"]) * 100, 2),  # type: ignore[arg-type]
        sharpe=_f(s["sharpe"], 3),
        sortino=_f(s["sortino"], 3),
        profit_factor=_f(pf, 3),
        win_rate=_f(s["win_rate"], 3),
        avg_days_held=_f(s["avg_days_held"], 1),
        turnover_per_month=round(float(s["turnover_per_month"]), 2),  # type: ignore[arg-type]
        cost_share_of_gross=_f(s["cost_share_of_gross"], 3),
        capacity_clipped_share=_f(clipped, 3),
        exit_reasons=mix,
        skips={k: int(v) for k, v in run.skip_counts.items()},
    )


def _primary(m: ArmMetrics, metric: str) -> float:
    v = getattr(m, metric)
    return float(v) if v is not None else 0.0


def compare_runs(
    control: SimRun, treatment: SimRun, rule: StudyRule, *, primary: float = 0.0
) -> dict[str, Any]:
    """The spec's rule kind, verbatim (treatment vs control), plus the primary metric's
    difference for the ladder's sign-flip check.

    Every kind returns ``switch`` = "the rule recommends the treatment" (for
    ``regime_keep`` that is the per-profile "control worse on both" leg; the study-level
    verdict across profiles is :func:`regime_call`), ``call`` (plain words) and the
    bootstrap CI of the daily P&L difference treatment − control.
    """
    from arc.backtest.ranking import _max_dd, block_bootstrap_ci, subperiod_stats

    c, t = _as_rank_run(control), _as_rank_run(treatment)
    boot = rule.bootstrap
    if rule.kind == "entry_filter":
        from arc.backtest.entry_filter import EntryFilterRule, entry_filter_challenge

        row = entry_filter_challenge(
            c,
            t,
            rule=EntryFilterRule(
                min_subperiods_not_lower=rule.min_subperiod_wins,
                ci_floor_frac_of_base=float(rule.ci_floor_frac_of_base or 0.0),
            ),
            subperiods=rule.decision,
            boot=boot,
        )
        rec = bool(row["recommend"])
        return {
            "subperiods_won": int(row["subperiods_not_lower"]),  # type: ignore[call-overload]
            "won": str(row["not_lower"]),
            "pnl_diff": round(float(row["pnl_diff"]), 2),  # type: ignore[arg-type]
            "ci_lo": round(float(row["ci_lo"]), 2),  # type: ignore[arg-type]
            "ci_hi": round(float(row["ci_hi"]), 2),  # type: ignore[arg-type]
            "ci_floor": round(float(row["ci_floor"]), 2),  # type: ignore[arg-type]
            "smaller_dd": bool(row["smaller_dd"]),
            "switch": rec,
            "call": "recommend the forward experiment" if rec else "do not recommend",
            "primary_diff": round(primary, 4),
        }
    if rule.kind in ("keep_control", "regime_keep"):
        idx = c.equity.index.union(t.equity.index)
        a = t.equity.reindex(idx).ffill().bfill()
        b = c.equity.reindex(idx).ffill().bfill()
        diff = (a.diff().fillna(0.0) - b.diff().fillna(0.0)).to_numpy(dtype=float)
        point, lo, hi = block_bootstrap_ci(
            diff, resamples=boot.resamples, block=boot.block_days, ci=boot.ci, seed=boot.seed
        )
        dd_c, dd_t = _max_dd(c.equity), _max_dd(t.equity)
        pnl_c = float(c.trades["pnl"].sum()) if len(c.trades) else 0.0
        pnl_t = float(t.trades["pnl"].sum()) if len(t.trades) else 0.0
        out: dict[str, Any] = {
            "pnl_diff": round(point, 2),
            "ci_lo": round(lo, 2),
            "ci_hi": round(hi, 2),
            "max_dd_control": round(dd_c, 2),
            "max_dd_treatment": round(dd_t, 2),
            "primary_diff": round(primary, 4),
        }
        if rule.kind == "keep_control":
            sc, st = subperiod_stats(c, rule.subperiods), subperiod_stats(t, rule.subperiods)
            not_lower = [lab for lab in rule.subperiods if sc[lab][0] >= st[lab][0]]
            tol = float(rule.dd_tolerance or 0.0)
            dd_ok = dd_c <= dd_t * (1 + tol) if dd_t > 0 else dd_c <= 0
            keep = len(not_lower) >= rule.min_subperiod_wins and dd_ok
            return out | {
                "subperiods_won": len(not_lower),
                "won": ",".join(not_lower) or "–",
                "dd_ok": bool(dd_ok),
                "switch": not keep,
                "call": "keep control" if keep else "recommend the treatment (future XP arm)",
            }
        # regime_keep: control vs treatment, per profile (the CI here is control − treatment)
        pnl_ok, dd_ok = pnl_c >= pnl_t, dd_c <= dd_t
        return out | {
            "subperiods_won": 0,
            "won": "–",
            "pnl_not_worse": bool(pnl_ok),
            "dd_not_worse": bool(dd_ok),
            "ci_hi_control_minus_treatment": round(-lo, 2),
            "switch": bool(not pnl_ok and not dd_ok),
            "call": "roll back to the treatment"
            if not pnl_ok and not dd_ok
            else ("keep control" if pnl_ok and dd_ok and -lo > 0 else "keep, inconclusive"),
        }
    row = challenge(c, t, rule=rule.decision, boot=boot)
    sw = bool(row["switch"])
    return {
        "subperiods_won": int(row["subperiods_won"]),  # type: ignore[call-overload]
        "won": str(row["won"]),
        "pnl_diff": round(float(row["pnl_diff"]), 2),  # type: ignore[arg-type]
        "ci_lo": round(float(row["ci_lo"]), 2),  # type: ignore[arg-type]
        "ci_hi": round(float(row["ci_hi"]), 2),  # type: ignore[arg-type]
        "switch": sw,
        "call": "switch candidate" if sw else "keep control",
        "primary_diff": round(primary, 4),
    }


def regime_call(verdicts: Mapping[str, Mapping[str, Mapping[str, Any]]], treatment: str) -> str:
    """E17.3's study-level verdict across every profile (``regime_keep`` rows)."""
    rows = [tr[treatment] for tr in verdicts.values() if treatment in tr]
    if not rows:
        return "keep, inconclusive"
    if any(r["switch"] for r in rows):
        return "roll back to the treatment"
    if all(
        r["pnl_not_worse"] and r["dd_not_worse"] and r["ci_hi_control_minus_treatment"] > 0
        for r in rows
    ):
        return "keep control"
    return "keep, inconclusive"


def load_sectors(path: Path | None = None) -> dict[str, str]:
    """``ticker -> sector`` from ``config/sectors.yaml`` (the E5.9 map; unknown = unmapped)."""
    p = path or REPO_ROOT / "config" / "sectors.yaml"
    data = yaml.safe_load(p.read_text()) or {}
    return {
        str(t).upper(): str(sector)
        for sector, names in (data.get("sectors") or {}).items()
        for t in names or []
    }


def _group(t: pd.DataFrame, key: pd.Series | str) -> list[dict[str, Any]]:
    if not len(t):
        return []
    g = t.groupby(key, sort=True)["pnl"]
    out = []
    for k, s in g:
        out.append(
            {
                "key": str(k),
                "trades": len(s),
                "net_pnl": round(float(s.sum()), 2),
                "win_rate": round(float((s > 0).mean()), 3),
            }
        )
    return out


def _bucket(liq: float) -> str:
    for name, hi in LIQUIDITY_BUCKETS:
        if liq < hi:
            return name
    return LIQUIDITY_BUCKETS[-1][0]  # pragma: no cover - inf bound


def subperiod_rows(run: SimRun, end: dt.date) -> list[dict[str, Any]]:
    """Fixed sub-periods by entry date: trades, net P&L, win rate, max DD of equity there."""
    t = run.trades
    out = []
    for name, a, b in SUBPERIODS:
        hi = b or end
        sel = t[(t["entry_date"] >= a) & (t["entry_date"] <= hi)] if len(t) else t
        eq = run.equity[(run.equity.index >= a) & (run.equity.index <= hi)]
        dd = float(((eq.cummax() - eq) / eq.cummax()).max()) * 100 if len(eq) else 0.0
        out.append(
            {
                "period": name,
                "trades": len(sel),
                "net_pnl": round(float(sel["pnl"].sum()), 2) if len(sel) else 0.0,
                "win_rate": round(float((sel["pnl"] > 0).mean()), 3) if len(sel) else None,
                "max_dd_pct": round(dd, 2),
            }
        )
    return out


#: The All / Stocks / ETFs split every report carries (owner 2026-10-11, E7.14).
SEGMENTS: tuple[str, ...] = ("All", "Stocks", "ETFs")


def _segment_frame(t: pd.DataFrame, segment: str) -> pd.DataFrame:
    if segment == "All" or not len(t):
        return t
    etf = (t["etf"] if "etf" in t else pd.Series(data=False, index=t.index)).astype(bool)
    return t[etf] if segment == "ETFs" else t[~etf]


def _dd_window(equity: pd.Series) -> tuple[dt.date, dt.date] | None:
    """(peak day, trough day) of the run's maximum drawdown, or None if it never drew down."""
    if len(equity) < 2:  # noqa: PLR2004
        return None
    peak = equity.cummax()
    dd = (peak - equity) / peak
    if not float(dd.max()) > 0:
        return None
    trough = dd.idxmax()
    start = equity[:trough].idxmax()
    return start, trough


def segment_rows(run: SimRun) -> list[dict[str, Any]]:
    """All / Stocks / ETFs (keyed on the trade's ``etf`` flag, i.e. ``HistoryView.is_etf``).

    Per segment: trades, win rate, mean / median / net P&L, P&L per $ risked (net P&L /
    sum of max loss × contracts), its contribution to the run's max drawdown (net P&L of
    the trades closed between the drawdown's peak and trough), the exit-reason mix and the
    spread + fee cost (``pnl_mid − pnl``). All = Stocks + ETFs (trade counts and sums).
    """
    t = run.trades
    win = _dd_window(run.equity)
    out: list[dict[str, Any]] = []
    for seg in SEGMENTS:
        s = _segment_frame(t, seg)
        n = len(s)
        risked = float((s["max_loss_unit"] * s["contracts"]).sum()) if n else 0.0
        pnl = float(s["pnl"].sum()) if n else 0.0
        dd = 0.0
        if n and win is not None:
            closed = pd.to_datetime(s["exit_date"]).dt.date
            dd = float(s.loc[(closed > win[0]) & (closed <= win[1]), "pnl"].sum())
        mix = s["exit_reason"].value_counts().sort_index() if n else pd.Series(dtype=int)
        out.append(
            {
                "segment": seg,
                "trades": n,
                "win_rate": round(float((s["pnl"] > 0).mean()), 3) if n else None,
                "net_pnl": round(pnl, 2),
                "mean_pnl": round(float(s["pnl"].mean()), 2) if n else None,
                "median_pnl": round(float(s["pnl"].median()), 2) if n else None,
                "pnl_per_risk": round(pnl / risked, 4) if risked > 0 else None,
                "dd_contribution": round(dd, 2),
                "spread_cost": round(float((s["pnl_mid"] - s["pnl"]).sum()), 2) if n else 0.0,
                "exit_reasons": {str(k): int(v) for k, v in mix.items()},
            }
        )
    return out


def segment_verdicts(
    segments: Mapping[str, Sequence[Mapping[str, Any]]], treatments: Sequence[str]
) -> dict[str, dict[str, Any]]:
    """Per treatment: net P&L difference vs control for each segment, and a flag when the
    sign differs between Stocks and ETFs (the verdict itself is stated on All)."""
    by = {arm: {r["segment"]: r for r in rows} for arm, rows in segments.items()}
    out: dict[str, dict[str, Any]] = {}
    for t in treatments:
        diff = {
            seg: round(by[t][seg]["net_pnl"] - by["control"][seg]["net_pnl"], 2) for seg in SEGMENTS
        }
        s, e = diff["Stocks"], diff["ETFs"]
        out[t] = {**diff, "sign_differs": bool(s * e < 0)}
    return out


def breakdowns(run: SimRun, sectors: Mapping[str, str]) -> dict[str, list[dict[str, Any]]]:
    t = run.trades
    if not len(t):
        return {k: [] for k in ("regime", "structure", "tier", "sector", "etf", "liquidity")}
    return {
        "regime": _group(t, "trend"),
        "structure": _group(t, t["kind"] if "kind" in t else pd.Series("?", index=t.index)),
        "tier": _group(t, t["tier"] if "tier" in t else pd.Series("rest", index=t.index)),
        "sector": _group(t, t["underlying"].map(lambda u: sectors.get(str(u), "unknown"))),
        "etf": _group(
            t,
            (t["etf"] if "etf" in t else pd.Series(data=False, index=t.index)).map(
                lambda e: "ETF" if bool(e) else "single stock"
            ),
        ),
        "liquidity": _group(t, t["liquidity"].astype(float).map(_bucket)),
    }


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------


class _ArmPlan(BaseModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    arm: str
    cfg: Any
    exits: Any
    cost: Any
    overlay_key: str


def _window(spec: StudySpec, phase: str, latest: dt.date) -> tuple[dt.date, dt.date, dt.date]:
    """(first decision, last decision, last session read) for *phase*."""
    p = spec.period
    if phase == "dev":
        end = min(p.dev_end, latest)
        return p.dev_start, end, min(latest, end + dt.timedelta(days=p.settle_days))
    end = min(p.holdout_end or latest, latest)
    return p.holdout_start, end, latest


def run_study(  # noqa: PLR0913
    spec: StudySpec,
    *,
    phase: Literal["dev", "holdout"],
    universe: str | None,
    now: dt.datetime,
    latest: dt.date,
    data_dir: Path,
    ledger: Path,
    runs_dir: Path,
    report_dir: Path | None,
    settings: ArcSettings,
    paths: Mapping[str, Path | None] | None = None,
    allow_overfit_warning: bool = False,
    tickers: Sequence[str] | None = None,
    workers: int = 1,
    cache: bool = True,
    cache_dir: Path | None = None,
    git_sha: str | None = None,
) -> LedgerEntry:
    """Run *spec* once under the protocol, append the ledger line, write the report.

    *now* stamps the ledger line (tests inject it); *latest* is the last data session
    available (the holdout's open end).
    """
    from arc.backtest.ranking import profile_allows_credit
    from arc.backtest.sim_report import _git_sha, _write_run, build_sim, walk

    uni = universe or (spec.universe_ladder[0] if phase == "dev" else spec.universe_ladder[-1])
    if uni not in spec.universe_ladder:
        msg = f"{spec.id}: universe {uni} is not on the ladder {spec.universe_ladder}"
        raise StudyRefusedError(msg)
    entries = load_ledger(ledger)
    overfit = False
    if phase == "holdout":
        overfit = check_holdout(spec, entries, allow_overfit_warning=allow_overfit_warning)
    h, a = study_hash(spec), arm_set_hash(spec)
    n_var = variants(entries, spec.id, a)
    start, end, data_end = _window(spec, phase, latest)
    if end < start:
        msg = f"{spec.id}: no {phase} sessions (window {start} -> {end}, latest data {latest})"
        raise StudyRefusedError(msg)

    plans: list[_ArmPlan] = []
    for arm in spec.arms.names:
        cfg, exits, cost = arm_configs(spec, arm, paths=paths)
        key = document_hash(spec.arms.arm(arm).model_dump(mode="json")["overlay"])
        plans.append(_ArmPlan(arm=arm, cfg=cfg, exits=exits, cost=cost, overlay_key=key))
    allows = {p: profile_allows_credit(p, settings.account_profiles_file) for p in spec.profiles}
    for arm in spec.arms.names:
        r = spec.arms.arm(arm).ranker
        for p in spec.profiles:
            if r is not None and not applicable(r, allows_credit=allows[p]):
                msg = f"{spec.id}: arm {arm} ranker {r.value} does not apply to profile {p}"
                raise StudyRefusedError(msg)

    run_dir = runs_dir / spec.id / f"{now:%Y%m%dT%H%M%S}-{phase}-{uni}-{h[:8]}"
    runs: dict[str, dict[str, SimRun]] = {p: {} for p in spec.profiles}
    equity0 = 0.0
    version = ""
    partitions: dict[str, int] = {}
    built: dict[str, Any] = {}  # overlay key -> SimBuild (arms differing by ranker share it)
    for plan in plans:
        want = sorted({r for x in spec.arms.names if (r := spec.arms.arm(x).ranker) is not None})
        if plan.overlay_key not in built:
            built[plan.overlay_key] = build_sim(
                data_dir=data_dir,
                universe=uni,
                start=start,
                end=end,
                data_end=data_end,
                profiles=spec.profiles,
                rankers=want,
                cfg=plan.cfg,
                cost=plan.cost,
                exits=plan.exits,
                settings=settings,
                tickers=tickers,
                workers=workers,
                cache=cache,
                cache_dir=cache_dir,
            )
        b = built[plan.overlay_key]
        version, partitions = b.universe_version, b.partitions
        equity0 = plan.cfg.backtest.sim.equity
        for p in spec.profiles:
            r = spec.arms.arm(plan.arm).ranker_for(allows_credit=allows[p])
            run = walk(b, profile=p, ranker=r, cfg=plan.cfg, end=end)
            _write_run(run, run_dir / p / plan.arm)
            runs[p][plan.arm] = run

    metrics: dict[str, dict[str, ArmMetrics]] = {
        p: {arm: arm_metrics(r, equity0) for arm, r in by.items()} for p, by in runs.items()
    }
    verdicts: dict[str, dict[str, dict[str, Any]]] = {}
    for p, by in runs.items():
        verdicts[p] = {}
        for t in spec.arms.treatments:
            d = _primary(metrics[p][t], spec.primary_metric) - _primary(
                metrics[p]["control"], spec.primary_metric
            )
            verdicts[p][t] = compare_runs(by["control"], by[t], spec.decision_rule, primary=d)
    entry = LedgerEntry(
        at=now,
        study=spec.id,
        spec_hash=h,
        arm_set_hash=a,
        arms=spec.arms.model_dump(mode="json"),
        profiles=list(spec.profiles),
        phase=phase,
        universe=uni,
        universe_version=version,
        partitions=partitions,
        start=start,
        end=end,
        data_end=data_end,
        git_sha=git_sha or _git_sha(),
        primary_metric=spec.primary_metric,
        metrics={
            p: {k: m.model_dump(mode="json") for k, m in by.items()} for p, by in metrics.items()
        },
        verdicts=verdicts,
        variants=n_var,
        overfit_warning=overfit,
        run_dir=str(run_dir),
    )
    _append(ledger, entry)
    payload = build_report(
        spec,
        entry,
        runs,
        entries=entries,
        sectors=load_sectors(),
        latest=latest,
    )
    run_dir.mkdir(parents=True, exist_ok=True)
    md = render_report(payload)
    (run_dir / "report.json").write_text(json.dumps(payload, indent=1, default=str) + "\n")
    (run_dir / "report.md").write_text(md)
    if report_dir is not None:
        report_dir.mkdir(parents=True, exist_ok=True)
        (report_dir / f"{spec.id}.json").write_text(
            json.dumps(payload, indent=1, default=str) + "\n"
        )
        (report_dir / f"{spec.id}.md").write_text(md)
    log.info(
        "backtest.study.done",
        study=spec.id,
        phase=phase,
        universe=uni,
        spec_hash=h[:12],
        variants=n_var,
        overfit_warning=overfit,
    )
    return entry


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def build_report(
    spec: StudySpec,
    entry: LedgerEntry,
    runs: Mapping[str, Mapping[str, SimRun]],
    *,
    entries: Sequence[LedgerEntry],
    sectors: Mapping[str, str],
    latest: dt.date,
) -> dict[str, Any]:
    """The standard report payload (JSON-able aggregates)."""
    holdouts = [e for e in entries if e.study == spec.id and e.phase == "holdout"]
    per_period_wins: dict[str, dict[str, list[str]]] = {}
    sub: dict[str, dict[str, list[dict[str, Any]]]] = {}
    brk: dict[str, dict[str, dict[str, list[dict[str, Any]]]]] = {}
    segs: dict[str, dict[str, list[dict[str, Any]]]] = {}
    seg_verdicts: dict[str, dict[str, dict[str, Any]]] = {}
    for p, by in runs.items():
        sub[p] = {arm: subperiod_rows(r, latest) for arm, r in by.items()}
        brk[p] = {arm: breakdowns(r, sectors) for arm, r in by.items()}
        segs[p] = {arm: segment_rows(r) for arm, r in by.items()}
        seg_verdicts[p] = segment_verdicts(segs[p], list(spec.arms.treatments))
        per_period_wins[p] = {}
        for t in spec.arms.treatments:
            ctrl = {row["period"]: row["net_pnl"] for row in sub[p]["control"]}
            per_period_wins[p][t] = [
                row["period"]
                for row in sub[p][t]
                if row["trades"] and row["net_pnl"] > ctrl.get(row["period"], 0.0)
            ]
    return {
        "header": {
            "study": spec.id,
            "hypothesis": spec.hypothesis,
            "owner": spec.owner,
            "created": spec.created.isoformat(),
            "spec_hash": entry.spec_hash,
            "arm_set_hash": entry.arm_set_hash,
            "phase": entry.phase,
            "rule": spec.decision_rule.text(),
            "rule_kind": spec.decision_rule.kind,
            "rule_verbatim": spec.decision_rule.verbatim,
            "primary_metric": spec.primary_metric,
            "universe": entry.universe,
            "universe_ladder": list(spec.universe_ladder),
            "universe_version": entry.universe_version,
            "partitions": entry.partitions,
            "decisions": f"{entry.start} -> {entry.end}",
            "data_end": entry.data_end.isoformat(),
            "dev_period": f"{spec.period.dev_start} -> {spec.period.dev_end}",
            "holdout_period": f"{spec.period.holdout_start} -> "
            f"{spec.period.holdout_end or 'latest'}",
            "not_replayable": NOT_REPLAYABLE,
            "git_sha": entry.git_sha,
            "variants_tried": entry.variants,
            "max_variants": spec.max_variants,
            "overfit_warning": entry.overfit_warning,
            "holdout_runs_before": len(holdouts),
            "arms": {
                arm: {
                    "ranker": (spec.arms.arm(arm).ranker or "incumbent"),
                    "overlay": spec.arms.arm(arm).overlay,
                }
                for arm in spec.arms.names
            },
        },
        "metrics": entry.metrics,
        "verdicts": entry.verdicts,
        "subperiods": sub,
        "per_period_wins": per_period_wins,
        "breakdowns": brk,
        "segments": segs,
        "segment_verdicts": seg_verdicts,
        "study_calls": (
            {t: regime_call(entry.verdicts, t) for t in spec.arms.treatments}
            if spec.decision_rule.kind == "regime_keep"
            else {}
        ),
        "ladder": ladder_flags(entries, spec, entry),
    }


def _md_table(rows: Sequence[Mapping[str, Any]], cols: Sequence[str] | None = None) -> str:
    if not rows:
        return "_none_\n"
    cs = list(cols or rows[0].keys())
    lines = ["| " + " | ".join(cs) + " |", "|" + "---|" * len(cs)]
    for r in rows:
        lines.append("| " + " | ".join(_cell(r.get(c)) for c in cs) + " |")
    return "\n".join(lines) + "\n"


def _cell(v: object) -> str:
    if v is None:
        return "–"
    if isinstance(v, float):
        return f"{v:,.2f}" if abs(v) >= 100 else f"{v:.4g}"
    if isinstance(v, dict):
        return ", ".join(f"{k} {_cell(x)}" for k, x in v.items()) or "–"
    return str(v)


def verdict_lines(payload: Mapping[str, Any]) -> list[str]:
    """One plain line per profile × treatment: what the rule says on this run."""
    h = payload["header"]
    word = "VERDICT" if h["phase"] == "holdout" else "dev result (not a verdict)"
    out = []
    for p, tr in payload["verdicts"].items():
        for t, v in tr.items():
            call = v.get("call") or ("switch candidate" if v["switch"] else "keep control")
            out.append(
                f"{word} · {p} · {t} vs control: {call} (sub-periods {v['subperiods_won']} "
                f"[{v['won']}], P&L diff {v['pnl_diff']:,.0f}, "
                f"CI [{v['ci_lo']:,.0f}, {v['ci_hi']:,.0f}])"
            )
    if payload["header"].get("rule_kind") == "regime_keep":
        for t, call in payload.get("study_calls", {}).items():
            out.append(f"{word} · all profiles · {t} vs control: {call}")
    return out


def render_report(payload: Mapping[str, Any]) -> str:
    h = payload["header"]
    lines = [f"# {h['study']} · {h['hypothesis']}", ""]
    if h["overfit_warning"]:
        lines += [
            f"> **OVERFIT WARNING:** {h['variants_tried']} variants tried > max_variants "
            f"{h['max_variants']}; the holdout ran under `--allow-overfit-warning`.",
            "",
        ]
    lines += [
        "## Header",
        "",
        f"- phase **{h['phase']}**; spec hash `{h['spec_hash'][:12]}`; arm set "
        f"`{h['arm_set_hash'][:12]}`; owner {h['owner']}; created {h['created']}",
        f"- rule: {h['rule']}",
        f"- primary metric: `{h['primary_metric']}`",
        f"- universe **{h['universe']}** (ladder {', '.join(h['universe_ladder'])}), version "
        f"`{h['universe_version']}`; data partitions {h['partitions']}",
        f"- decisions {h['decisions']} (exits settle by {h['data_end']}); dev "
        f"{h['dev_period']}; holdout {h['holdout_period']}",
        f"- {h['not_replayable']}",
        f"- git `{h['git_sha']}`; variants tried {h['variants_tried']} of max "
        f"{h['max_variants']}; earlier holdout runs {h['holdout_runs_before']}",
        "- arms: "
        + "; ".join(
            f"{arm} = ranker {a['ranker']}"
            + (f", overlay `{json.dumps(a['overlay'], sort_keys=True)}`" if a["overlay"] else "")
            for arm, a in h["arms"].items()
        ),
        "",
        "## Verdict",
        "",
        "The pre-registered rule, verbatim:",
        "",
        f"> {h['rule']}",
        "",
        *(
            [f"Original card's wording: _{h['rule_verbatim']}_", ""]
            if h.get("rule_verbatim")
            else []
        ),
        *(f"- {s}" for s in verdict_lines(payload)),
        "",
        "A verdict only recommends: a live change still goes through D44 "
        "(`Experiment: XP-<n>` with arms copied from this spec) or an owner waiver.",
        "",
        "## Per arm × profile",
        "",
    ]
    metric_cols = [
        "profile",
        "arm",
        "trades",
        "net_pnl",
        "return_pct",
        "cagr",
        "max_dd_pct",
        "sharpe",
        "sortino",
        "profit_factor",
        "win_rate",
        "avg_days_held",
        "turnover_per_month",
        "cost_share_of_gross",
        "capacity_clipped_share",
    ]
    rows = [
        {"profile": p, "arm": arm, **m}
        for p, by in payload["metrics"].items()
        for arm, m in by.items()
    ]
    lines += [_md_table(rows, metric_cols)]
    if "segments" in payload:
        lines += [
            "## All / Stocks / ETFs",
            "",
            "One pool per run; the verdict is stated on All, Stocks and ETFs are diagnostic. "
            "`dd_contribution` = net P&L of the trades closed inside the run's max-drawdown "
            "window; `spread_cost` = P&L at mid − net P&L (spread + fees).",
            "",
        ]
        seg_cols = [
            "profile",
            "arm",
            "segment",
            "trades",
            "win_rate",
            "net_pnl",
            "mean_pnl",
            "median_pnl",
            "pnl_per_risk",
            "dd_contribution",
            "spread_cost",
            "exit_reasons",
        ]
        lines += [
            _md_table(
                [
                    {"profile": p, "arm": arm, **row}
                    for p, by in payload["segments"].items()
                    for arm, rs in by.items()
                    for row in rs
                ],
                seg_cols,
            )
        ]
        sv = [
            {"profile": p, "treatment": t, **v}
            for p, tr in payload.get("segment_verdicts", {}).items()
            for t, v in tr.items()
        ]
        flagged = [r for r in sv if r["sign_differs"]]
        lines += [
            "Treatment − control net P&L by segment"
            + (
                f" (**sign differs between Stocks and ETFs for {len(flagged)} treatment(s)**):"
                if flagged
                else ":"
            ),
            "",
            _md_table(sv),
        ]
    lines += ["## Exit-reason mix", ""]
    lines += [
        _md_table(
            [{"profile": r["profile"], "arm": r["arm"], "mix": r["exit_reasons"]} for r in rows]
        )
    ]
    lines += ["## Skip reasons", ""]
    lines += [
        _md_table(
            [
                {"profile": r["profile"], "arm": r["arm"], **{k: v for k, v in r["skips"].items()}}
                for r in rows
            ]
        )
    ]
    lines += ["## Sub-periods", "", "Fixed windows by entry date.", ""]
    sp = [
        {"profile": p, "arm": arm, **row}
        for p, by in payload["subperiods"].items()
        for arm, rs in by.items()
        for row in rs
    ]
    lines += [_md_table(sp)]
    for title, key in (
        ("By regime", "regime"),
        ("By structure", "structure"),
        ("By universe tier", "tier"),
        ("By sector", "sector"),
        ("ETF vs single stock", "etf"),
        ("By liquidity bucket", "liquidity"),
    ):
        b = [
            {"profile": p, "arm": arm, **row}
            for p, by in payload["breakdowns"].items()
            for arm, d in by.items()
            for row in d.get(key, [])
        ]
        lines += [f"## {title}", "", _md_table(b)]
    lines += [
        "## Uncertainty",
        "",
        "Moving-block bootstrap CI of the daily equity-change difference vs control "
        "(seeded), and the fixed sub-periods each treatment won on net P&L.",
        "",
    ]
    unc = [
        {
            "profile": p,
            "treatment": t,
            "pnl_diff": v["pnl_diff"],
            "ci_lo": v["ci_lo"],
            "ci_hi": v["ci_hi"],
            "periods_won": ", ".join(payload["per_period_wins"][p][t]) or "–",
        }
        for p, tr in payload["verdicts"].items()
        for t, v in tr.items()
    ]
    lines += [_md_table(unc)]
    lines += [
        "## Checkpoint ladder",
        "",
        "Every ledger run of this arm set; `sign_flip_vs_first_rung` compares the primary "
        "metric's treatment − control sign with the first dev rung.",
        "",
        _md_table(payload["ladder"]),
    ]
    return "\n".join(lines).rstrip() + "\n"
