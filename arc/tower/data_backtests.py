"""Backtests on the Experiments page (D124): ``/api/backtests`` and ``/api/backtests/{id}``.

Read-only. Every number is a stored row of the backtest research ledger
(``data/backtest/ledger.jsonl``, one line per study run), the study spec
(``config/backtest_studies/BT-<n>.yaml``) and the nightly runner's queue
(``data/backtest/queue.json``). The tower never runs or re-scores a backtest.

The files are parsed here with tolerant models (``extra="ignore"``) instead of
:mod:`arc.backtest.study`: that module pulls the simulator (pandas, the scanner) into
the tower process, and a ledger row written by a newer runner must not 503 the page.
A row that does not parse is skipped and counted in ``skipped_rows``.

Backtest results are dollars of net P&L over a simulated period on the backtest
account; live experiments are paired daily P&L in % of t0 equity. The two are never
pooled: the page shows them side by side.
"""

from __future__ import annotations

import datetime as _dt
import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

if TYPE_CHECKING:
    import sqlite3

__all__ = [
    "BacktestDetailResponse",
    "BacktestListItem",
    "BacktestPaths",
    "BacktestsResponse",
    "load_backtest",
    "load_backtests",
    "scrub_ids",
]

_STRICT = ConfigDict(extra="forbid", frozen=True)
_LOOSE = ConfigDict(extra="ignore")

#: The runner's ladder: rung -> (ledger phase, universe). The holdout runs on C500.
RUNGS: tuple[tuple[str, str, str], ...] = (
    ("c125_dev", "dev", "c125"),
    ("c500_dev", "dev", "c500"),
    ("holdout", "holdout", "c500"),
)
SPECS_DIR = "config/backtest_studies"
_ID_RE = re.compile(r"^BT-[1-9]\d*$")

Status = Literal["done", "stopped_at_dev", "running", "queued", "stopped", "failed", "not_run"]

# ---------------------------------------------------------------------------
# Owner-facing text: no plan (D#) or card (E#.#) ids (the Tower rule)
# ---------------------------------------------------------------------------

_ID = r"(?:D\d{1,3}|E\d{1,2}\.\d+[a-z]?)"
_IDS = re.compile(rf"\b{_ID}\b")
# "E7.2 / D4 re-run: x" -> "Re-run: x"
_LEAD = re.compile(rf"^\s*{_ID}(?:\s*/\s*{_ID})*\s+")
# "(E7.5a)" or "(D25, E7.5)" -> ""
_PAREN = re.compile(rf"\s*\(\s*{_ID}(?:\s*[,/]\s*{_ID})*\s*\)")
# "D4 (PLAN §0): " -> ""
_PLAN_REF = re.compile(rf"\b{_ID}\s*\(PLAN\s*§\s*\d+\)\s*:?\s*")
# "the D25 rule" -> "the rule", "E7.2 had" -> "had"
_INLINE = re.compile(rf"\b{_ID}(?:\s*/\s*{_ID})*\s+")


def scrub_ids(text: str) -> str:
    """*text* with plan/card ids removed and the sentence tidied (first letter upper)."""
    out = " ".join(text.split())
    out = _PLAN_REF.sub("", out)
    out = _PAREN.sub("", out)
    out = _LEAD.sub("", out)
    out = _INLINE.sub("", out)
    out = _IDS.sub("", out)  # an id at the end of a sentence / before punctuation
    out = re.sub(r"\s+([,.;:])", r"\1", re.sub(r"\s{2,}", " ", out)).strip()
    out = re.sub(r"(^|[.!?]\s+)([a-z])", lambda m: m.group(1) + m.group(2).upper(), out)
    return out


# ---------------------------------------------------------------------------
# Stored files (tolerant readers)
# ---------------------------------------------------------------------------


class _LedgerRow(BaseModel):
    model_config = _LOOSE

    at: _dt.datetime
    study: str
    spec_hash: str
    phase: Literal["dev", "holdout"]
    universe: str
    start: _dt.date
    end: _dt.date
    primary_metric: str = "net_pnl"
    profiles: list[str] = Field(default_factory=list)
    metrics: dict[str, dict[str, dict[str, Any]]] = Field(default_factory=dict)
    verdicts: dict[str, dict[str, dict[str, Any]]] = Field(default_factory=dict)
    overfit_warning: bool = False
    runtime_s: float | None = None


class _QueueRun(BaseModel):
    model_config = _LOOSE

    rung: str
    at: _dt.datetime
    outcome: str | None = None


class _QueueEntry(BaseModel):
    model_config = _LOOSE

    id: str
    status: str = "queued"
    approved_at: _dt.datetime
    ladder_state: str | None = None
    runs: list[_QueueRun] = Field(default_factory=list)
    stop_reason: str | None = None


class _Spec(BaseModel):
    model_config = _LOOSE

    id: str
    hypothesis: str
    created: _dt.date | None = None
    profiles: list[str] = Field(default_factory=list)
    arms: dict[str, Any] = Field(default_factory=dict)
    universe_ladder: list[str] = Field(default_factory=list)
    decision_rule: dict[str, Any] = Field(default_factory=dict)
    primary_metric: str = "net_pnl"


class BacktestPaths(BaseModel):
    """Where the tower reads the study files (repo-relative paths resolved on *root*)."""

    model_config = ConfigDict(frozen=True)

    root: Path
    ledger: str = "data/backtest/ledger.jsonl"
    queue: str = "data/backtest/queue.json"
    specs: str = SPECS_DIR

    def path(self, rel: str) -> Path:
        p = Path(rel)
        return p if p.is_absolute() else self.root / p


def _ledger(paths: BacktestPaths) -> tuple[list[_LedgerRow], int]:
    f = paths.path(paths.ledger)
    if not f.is_file():
        return [], 0
    rows: list[_LedgerRow] = []
    skipped = 0
    for line in f.read_text().splitlines():
        if not line.strip():
            continue
        try:
            rows.append(_LedgerRow.model_validate_json(line))
        except ValidationError:
            skipped += 1
    return rows, skipped


def _queue(paths: BacktestPaths) -> list[_QueueEntry]:
    f = paths.path(paths.queue)
    if not f.is_file():
        return []
    try:
        raw = json.loads(f.read_text())
    except (OSError, ValueError):
        return []
    out: list[_QueueEntry] = []
    for e in raw.get("entries", []) if isinstance(raw, dict) else []:
        try:
            out.append(_QueueEntry.model_validate(e))
        except ValidationError:
            continue
    return out


def _specs(paths: BacktestPaths) -> dict[str, _Spec]:
    d = paths.path(paths.specs)
    out: dict[str, _Spec] = {}
    if not d.is_dir():
        return out
    for f in sorted(d.glob("BT-*.yaml")):
        try:
            s = _Spec.model_validate(yaml.safe_load(f.read_text()) or {})
        except (OSError, yaml.YAMLError, ValidationError):
            continue
        if _ID_RE.match(s.id):
            out[s.id] = s
    return out


def _num(study_id: str) -> int:
    return int(study_id.split("-", 1)[1])


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class Headline(BaseModel):
    """One treatment's result on one profile in one run (the ledger's verdict row)."""

    model_config = _STRICT

    arm: str = Field(..., description="Treatment arm (t1..t3)")
    pnl_diff: float = Field(..., description="Treatment − control net P&L, dollars")
    ci_lo: float | None = Field(None, description="Lower end of the block-bootstrap range, $")
    ci_hi: float | None = Field(None, description="Upper end of the block-bootstrap range, $")
    switch: bool = Field(False, description="The pre-registered rule says switch to the arm")
    call: str = Field(..., description="The rule's call, plain words (e.g. 'keep control')")
    periods_won: int | None = Field(None, description="Market periods (bear/sideways/bull) won")
    periods_won_names: list[str] = Field(default_factory=list)


class RungState(BaseModel):
    model_config = _STRICT

    rung: Literal["c125_dev", "c500_dev", "holdout"]
    label: str = Field(..., description="'C125 Dev' | 'C500 Dev' | 'Holdout'")
    done: bool


class BacktestListItem(BaseModel):
    """One study row on the Experiments list."""

    model_config = _STRICT

    study_id: str
    hypothesis: str = Field(..., description="The spec's hypothesis, plan/card ids removed")
    status: Status
    variants: int = Field(..., description="Treatment arms in the spec")
    profiles: list[str] = Field(..., description="Account profiles the study ran on")
    profile: str | None = Field(
        None,
        description="Profile the headline is on: the live account's when the study ran on "
        "it, else the study's first profile",
    )
    ladder: list[RungState]
    stage: str | None = Field(None, description="Furthest stage run, e.g. 'C500 Holdout'")
    stage_phase: Literal["dev", "holdout"] | None = None
    runs: int = Field(0, description="Ledger rows (runs) for the study")
    last_run_at: _dt.datetime | None = None
    ci_level: float | None = Field(None, description="Block-bootstrap range coverage, e.g. 0.9")
    headline: Headline | None = Field(
        None, description="Best treatment on *profile* in the furthest stage's latest run"
    )


class ArmMetrics(BaseModel):
    model_config = _STRICT

    arm: str = Field(..., description="control | t1..t3")
    trades: int | None = None
    net_pnl: float | None = None
    return_pct: float | None = None
    max_dd_pct: float | None = None
    sortino: float | None = None
    profit_factor: float | None = None
    win_rate: float | None = None
    avg_days_held: float | None = None
    cost_share_of_gross: float | None = None
    capacity_clipped_share: float | None = None
    exit_reasons: dict[str, float] = Field(default_factory=dict)
    skips: dict[str, int] = Field(default_factory=dict)


class ProfileResult(BaseModel):
    model_config = _STRICT

    profile: str
    arms: list[ArmMetrics]
    treatments: list[Headline]


class BacktestRun(BaseModel):
    """One ledger row: a rung of the ladder (or an extra size, e.g. C250)."""

    model_config = _STRICT

    at: _dt.datetime
    phase: Literal["dev", "holdout"]
    universe: str
    stage: str = Field(..., description="'C125 Dev', 'C500 Holdout', ...")
    start: _dt.date
    end: _dt.date
    overfit_warning: bool
    runtime_s: float | None = None
    results: list[ProfileResult]


class LinkedExperiment(BaseModel):
    model_config = _STRICT

    experiment_id: str
    title: str
    status: str


class BacktestsResponse(BaseModel):
    model_config = _STRICT

    as_of: _dt.datetime
    account_profile: str = Field(..., description="The live account's profile (the default)")
    items: list[BacktestListItem]
    queued: int = Field(0, description="Approved studies waiting for a night")
    running: str | None = Field(None, description="Study the nightly runner is on, if any")
    window_start: str = Field("21:30", description="Nightly runner start, ET (HH:MM)")
    skipped_rows: int = Field(0, description="Ledger lines that did not parse (not shown)")


class BacktestDetailResponse(BaseModel):
    model_config = _STRICT

    as_of: _dt.datetime
    account_profile: str
    study: BacktestListItem
    rule: str | None = Field(None, description="The pre-registered decision rule, plain text")
    rule_kind: str | None = None
    created: _dt.date | None = None
    arms: dict[str, Any] = Field(default_factory=dict, description="Treatment arm -> overlay")
    runs: list[BacktestRun] = Field(..., description="Every ledger row, oldest first")
    linked: list[LinkedExperiment] = Field(
        default_factory=list, description="Live experiments whose spec cites this study"
    )


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------

_PHASE = {"dev": "Dev", "holdout": "Holdout"}


def stage_label(phase: str, universe: str) -> str:
    return f"{universe.upper()} {_PHASE.get(phase, phase.title())}"


def _f(v: Any) -> float | None:
    return float(v) if isinstance(v, int | float) and not isinstance(v, bool) else None


def _headline(arm: str, v: dict[str, Any]) -> Headline:
    switch = bool(v.get("switch"))
    call = str(v.get("call") or ("switch" if switch else "keep control"))
    call = call.replace(" (future XP arm)", "").strip()
    won = str(v.get("won") or "")
    names = [w for w in won.split(",") if w and w != "\u2013"]
    pnl = _f(v.get("pnl_diff"))
    if pnl is None:
        pnl = _f(v.get("primary_diff")) or 0.0
    return Headline(
        arm=arm,
        pnl_diff=pnl,
        ci_lo=_f(v.get("ci_lo")),
        ci_hi=_f(v.get("ci_hi")),
        switch=switch,
        call=call,
        periods_won=int(v["subperiods_won"]) if isinstance(v.get("subperiods_won"), int) else None,
        periods_won_names=names,
    )


def _best(verdicts: dict[str, dict[str, Any]]) -> Headline | None:
    """The treatment with the largest net P&L difference (a switching arm first)."""
    rows = [_headline(arm, v) for arm, v in sorted(verdicts.items())]
    if not rows:
        return None
    return max(rows, key=lambda h: (h.switch, h.pnl_diff))


def _lead_profile(profiles: list[str], account_profile: str) -> str | None:
    if account_profile in profiles:
        return account_profile
    return profiles[0] if profiles else None


def _furthest(rows: list[_LedgerRow]) -> _LedgerRow | None:
    """Latest holdout row, else the latest dev row on the largest universe."""
    if not rows:
        return None
    hold = [r for r in rows if r.phase == "holdout"]
    if hold:
        return max(hold, key=lambda r: r.at)
    return max(rows, key=lambda r: (_univ_size(r.universe), r.at))


def _univ_size(u: str) -> int:
    m = re.search(r"\d+", u)
    return int(m.group()) if m else 0


def _status(rows: list[_LedgerRow], q: _QueueEntry | None) -> Status:
    if q is not None and q.status in ("running", "queued"):
        return q.status  # type: ignore[return-value]
    if any(r.phase == "holdout" for r in rows):
        return "done"
    if q is not None and q.status in ("stopped", "failed"):
        return q.status  # type: ignore[return-value]
    return "stopped_at_dev" if rows else "not_run"


def _ladder(rows: list[_LedgerRow]) -> list[RungState]:
    seen = {(r.phase, r.universe) for r in rows}
    has_hold = any(r.phase == "holdout" for r in rows)
    out = []
    for rung, phase, univ in RUNGS:
        done = has_hold if rung == "holdout" else (phase, univ) in seen
        label = "Holdout" if rung == "holdout" else stage_label(phase, univ)
        out.append(RungState(rung=rung, label=label, done=done))  # type: ignore[arg-type]
    return out


def _ci_level(spec: _Spec | None) -> float | None:
    if spec is None:
        return None
    boot = spec.decision_rule.get("bootstrap") or {}
    return _f(boot.get("ci")) if isinstance(boot, dict) else None


def _item(
    study_id: str,
    spec: _Spec | None,
    rows: list[_LedgerRow],
    q: _QueueEntry | None,
    account_profile: str,
) -> BacktestListItem:
    rows = sorted(rows, key=lambda r: r.at)
    profiles = (
        list(spec.profiles)
        if spec and spec.profiles
        else sorted({p for r in rows for p in r.profiles})
    )
    profile = _lead_profile(profiles, account_profile)
    fur = _furthest(rows)
    head = _best(fur.verdicts.get(profile, {})) if fur is not None and profile else None
    treatments = (spec.arms.get("treatments") or {}) if spec else {}
    variants = (
        len(treatments)
        if isinstance(treatments, dict) and treatments
        else (max((len(v) for r in rows for v in r.verdicts.values()), default=0))
    )
    return BacktestListItem(
        study_id=study_id,
        hypothesis=scrub_ids(spec.hypothesis) if spec else "",
        status=_status(rows, q),
        variants=variants,
        profiles=profiles,
        profile=profile,
        ladder=_ladder(rows),
        stage=stage_label(fur.phase, fur.universe) if fur else None,
        stage_phase=fur.phase if fur else None,
        runs=len(rows),
        last_run_at=rows[-1].at if rows else None,
        ci_level=_ci_level(spec),
        headline=head,
    )


def _account_profile(settings: Any) -> str:
    return str(getattr(settings, "account_profile", None) or "cash_debit")


def _collect(
    paths: BacktestPaths,
) -> tuple[dict[str, _Spec], dict[str, list[_LedgerRow]], dict[str, _QueueEntry], int]:
    specs = _specs(paths)
    rows, skipped = _ledger(paths)
    by: dict[str, list[_LedgerRow]] = {}
    for r in rows:
        by.setdefault(r.study, []).append(r)
    queue = {e.id: e for e in _queue(paths)}
    return specs, by, queue, skipped


def load_backtests(
    paths: BacktestPaths, *, settings: Any, now: _dt.datetime, window_start: str = "21:30"
) -> BacktestsResponse:
    """Every study with a spec, a ledger row or a queue entry; latest run first."""
    specs, by, queue, skipped = _collect(paths)
    ap = _account_profile(settings)
    ids = sorted(set(specs) | set(by) | set(queue), key=_num)
    items = [
        _item(i, specs.get(i), by.get(i, []), queue.get(i), ap) for i in ids if _ID_RE.match(i)
    ]
    epoch = _dt.datetime.min.replace(tzinfo=_dt.UTC)
    items.sort(key=lambda it: (it.last_run_at or epoch, _num(it.study_id)), reverse=True)
    running = next((e.id for e in queue.values() if e.status == "running"), None)
    return BacktestsResponse(
        as_of=now,
        account_profile=ap,
        items=items,
        queued=sum(1 for e in queue.values() if e.status == "queued"),
        running=running,
        window_start=window_start,
        skipped_rows=skipped,
    )


_METRIC_KEYS = (
    "trades",
    "net_pnl",
    "return_pct",
    "max_dd_pct",
    "sortino",
    "profit_factor",
    "win_rate",
    "avg_days_held",
    "cost_share_of_gross",
    "capacity_clipped_share",
)


def _arm_metrics(arm: str, m: dict[str, Any]) -> ArmMetrics:
    vals: dict[str, Any] = {}
    for k in _METRIC_KEYS:
        v = m.get(k)
        if k == "trades":
            vals[k] = int(v) if isinstance(v, int | float) and not isinstance(v, bool) else None
        else:
            vals[k] = _f(v)
    exits = {
        str(k): float(v) for k, v in (m.get("exit_reasons") or {}).items() if _f(v) is not None
    }
    skips = {str(k): int(v) for k, v in (m.get("skips") or {}).items() if isinstance(v, int)}
    return ArmMetrics(arm=arm, exit_reasons=exits, skips=skips, **vals)


def _arm_order(arm: str) -> tuple[int, str]:
    return (0 if arm == "control" else 1, arm)


def _run(r: _LedgerRow) -> BacktestRun:
    results = []
    for prof in sorted(set(r.metrics) | set(r.verdicts)):
        arms = r.metrics.get(prof, {})
        results.append(
            ProfileResult(
                profile=prof,
                arms=[_arm_metrics(a, arms[a]) for a in sorted(arms, key=_arm_order)],
                treatments=[_headline(a, v) for a, v in sorted(r.verdicts.get(prof, {}).items())],
            )
        )
    return BacktestRun(
        at=r.at,
        phase=r.phase,
        universe=r.universe,
        stage=stage_label(r.phase, r.universe),
        start=r.start,
        end=r.end,
        overfit_warning=r.overfit_warning,
        runtime_s=r.runtime_s,
        results=results,
    )


def _linked(conn: sqlite3.Connection | None, study_id: str) -> list[LinkedExperiment]:
    if conn is None:
        return []
    from arc.experiments.store import ExperimentStore

    pat = re.compile(rf"(?<![\w-]){re.escape(study_id)}(?!\d)")
    out = []
    try:
        states = ExperimentStore(conn).all()
    except Exception:  # noqa: BLE001 - a store without the experiment tables links nothing
        return []
    for st in states:
        ref = getattr(st.spec, "backtest_ref", None)
        if ref and pat.search(ref):
            out.append(
                LinkedExperiment(
                    experiment_id=st.spec.id, title=st.spec.title, status=str(st.status)
                )
            )
    return out


def _clean_overlay(v: Any) -> Any:
    if isinstance(v, dict):
        return {str(k): _clean_overlay(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_clean_overlay(x) for x in v]
    return v


def load_backtest(
    paths: BacktestPaths,
    study_id: str,
    *,
    settings: Any,
    now: _dt.datetime,
    conn: sqlite3.Connection | None = None,
) -> BacktestDetailResponse | None:
    """One study: its list row, rule, arms and every run with full metrics; None if unknown."""
    if not _ID_RE.match(study_id):
        return None
    specs, by, queue, _ = _collect(paths)
    spec = specs.get(study_id)
    rows = sorted(by.get(study_id, []), key=lambda r: r.at)
    if spec is None and not rows and study_id not in queue:
        return None
    ap = _account_profile(settings)
    treatments = (spec.arms.get("treatments") or {}) if spec else {}
    arms = {
        str(k): _clean_overlay((v or {}).get("overlay") or {})
        for k, v in (treatments.items() if isinstance(treatments, dict) else [])
    }
    verbatim = spec.decision_rule.get("verbatim") if spec else None
    return BacktestDetailResponse(
        as_of=now,
        account_profile=ap,
        study=_item(study_id, spec, rows, queue.get(study_id), ap),
        rule=scrub_ids(str(verbatim)) if verbatim else None,
        rule_kind=str(spec.decision_rule.get("kind"))
        if spec and spec.decision_rule.get("kind")
        else None,
        created=spec.created if spec else None,
        arms=arms,
        runs=[_run(r) for r in rows],
        linked=_linked(conn, study_id),
    )
