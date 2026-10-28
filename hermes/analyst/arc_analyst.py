#!/usr/bin/env python3
"""Arc Analyst: independent weekly strategy review of Project Arc (E9.2).

Mirrors the Sentinel's pre-run gate (``arc_sentinel.py``) but reviews the trading
strategy, not the code. Stdlib only: Hermes runs this with its own Python.

Cron pre-run gate (no args):
  1. Copy the live store (``$ARC_REPO/data/arc.db``) to a private file with the
     sqlite backup API. The live file is opened ``mode=ro`` and nothing but
     ``backup()`` is called on it; every later read goes to the copy.
  2. Skip (last stdout line ``{"wakeAgent": false}``) unless the journal has at
     least one closed outcome AND (a closed outcome was recorded since the last
     run OR a halt was raised since the last run: a ``daily_loss`` halt is the
     drawdown event OR a forward experiment started/stopped/was promoted or
     rejected since the last run). The first rule is the E9.4 start condition: no
     review of an empty journal.
  3. Otherwise snapshot ``config/*.yaml``, run the read-only ``arc`` views on the
     copy (attribution, weekly scorecard, gaps, counterfactual, ``journal show``
     per trade closed this week, config diff/history/show), build the
     realised-vs-model table by structure kind x regime x DTE sleeve (E22.5: plus the
     D97 per-sleeve report, ``arc scorecard sleeves``), list this week's halts,
     the forward experiments (D44, E10.6: status, hash lock, latest stored
     ``ExperimentReport`` with arm / regime / structure breakdowns, queue per
     area), the newest ``docs/RESEARCH/*.md`` and the A-id ledger, and print the
     context (<= 30k chars; full text in ``RUN_DIR/context.md``).
     E21.3 (D86) adds: the XP backlog (``XP-<n>`` reserved when proposed, D121; statuses
     synced against the registry by ``proposal_ref``), ranked by value; arm capacity per area; the
     sessions-needed grid at K=3 (``arc experiment plan --k 3``, D108); the idea-seed leads
     not yet proposed or dropped; quick changes shipped since the last run (to
     review); and strategy PRs merged since the last run with their XP-advisory line.
     E7.15 (D84) adds, read-only: the backtest studies (``data/backtest/ledger.jsonl`` +
     ``docs/RESEARCH/studies/*.json``, each spec's hash from ``arc backtest study show
     --json``), the E7.9 checkpoints and the E7.14 backtest<->live drift. A holdout
     verdict not seen at the last ``mark`` also wakes the agent, and so does (E26.4) an XP
     verdict file ``config/experiments/live/verdicts/XP-<n>.yaml`` not seen at it.

Agent subcommands:
  record RUN_DIR              validate RUN_DIR/findings.json, reconcile into the ledger
                              (A-ids) and the XP backlog (XP-ids, xp_backlog.json)
  record RUN_DIR --mode verdict
                              E26.4 (D121) verdict run: the narrower contract (one new XP and
                              one new BT proposal at most, each citing a new verdict; no quick
                              changes, standing themes or full review)
  mark RUN_DIR                advance the watermark to this run (only after record); on a
                              verdict run it advances only the verdict run's watermark
  check-report FILE [--mode verdict]
                              the Slack report respects the size cap and sections
                              (verdict run: <= 1,400 chars)

Verdict gate (E26.4, D121; cron `arc-analyst-verdict-check`, daily 09:30 PT, through
``arc_analyst_verdict.py`` because Hermes runs a cron script without arguments):
  gate --mode verdict         wake only on a BT holdout verdict or an XP verdict file
                              (``config/experiments/live/verdicts/XP-<n>.yaml``) that neither
                              the weekly run nor a verdict run has read, at most once per ET day,
                              and never when the weekly run is due within 24 h
  check-seed [FILE]           validate the idea seed (default: the installed copy)
Owner subcommands (run by an interactive Hermes session on the owner's thread reply):
  triage A-ID STATUS [NOTE]   STATUS in accepted|wontfix|fixed
  ship A-ID [NOTE]            a quick change shipped: prints how to apply it; next run reviews
  approve XP-ID [NOTE] --approval slack:<ts>
                              proposal approved: freezes the spec and runs
                              `arc experiment adopt --id XP-ID` on the live store (E21.4):
                              spec PR (auto-merge), registered / queued
  drop XP-ID WHY              drop a proposal (its number is never reused)
  reprioritise XP-ID WHY [--score X]  pin to the top of the ranking (or override its score)
                              (P-<n> ids are retired, D121: these reply with the XP id)
  approve BT-ID [NOTE] --approval slack:<ts>
                              E26.2 (D121): weekly cap 2 per ISO week (ET), then
                              `arc backtest study adopt`: spec PR (auto-merge) + queued
  drop BT-ID WHY              drop a study draft that was never committed
  stop BT-ID WHY              stop a queued/running study (`arc backtest study stop`)
  backlog                     print the ranked backlog (XP and BT proposals)
  reset                       forget the watermark; next run reviews unconditionally

Environment (all optional): ARC_REPO (default ~/GitHub/Project-Arc),
ARC_ANALYST_HOME (default ~/.hermes/profiles/arc-analyst/analyst),
ARC_ANALYST_DB (default $ARC_REPO/data/arc.db), ARC_ANALYST_ARC (default
$ARC_REPO/.venv/bin/arc). No broker key, gate secret or Slack token is needed:
they are stripped from the environment of every command this script runs.
"""

from __future__ import annotations

import csv
import dataclasses
import datetime as dt
import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
DB_FMT = "%Y-%m-%dT%H:%M:%S.%fZ"  # arc.context.ttl.to_db / store _now_iso text
WINDOW_DAYS = 7
# D86 gate 1: 20 live closed trades per bucket (was 30). The attribution views are run with
# `--min-sample 20` so their `low_sample` flags agree (arc.journal.views keeps 30 elsewhere).
MIN_SAMPLE = 20
MAX_STDOUT = 30_000
REPORT_MAX = 4_000  # D141: emoji headers + blank lines between sections
MAX_SHOWN_TRADES = 5
SECRET_PREFIXES = (
    "ALPACA_",
    "ARC_GATE",
    "SLACK_",
    "ANTHROPIC_",
    "CLAUDE_",
    "OPENAI_",
    "OPENROUTER_",
    "GITHUB_",
    "GH_",
)

SEVERITIES = ("blocker", "high", "medium", "low", "info")
# D86 (E21.3): `quick-change` (ship now, no XP) and `xp-proposal` (an XP-<n> backlog entry)
# replace the old `recommendation` + `forward_spec`.
CATEGORIES = ("quick-change", "xp-proposal", "flaw", "theme", "data-gap")
ACTIONS = ("new-card", "comment-on-card", "owner-decision", "no-action")
ACTIVE = ("open", "regressed", "accepted")
# `shipped` is set only by `ship A-<n>` (stamps shipped_at; the next run must review it).
TRIAGE = ("accepted", "wontfix", "fixed")
REQUIRED = (
    "key",
    "title",
    "severity",
    "category",
    "evidence",
    "recommendation",
    "action",
    "bucket",
)
THEMES = {
    "cost-model": "cost model vs realised slippage (config/costs.yaml, D23)",
    "regime-menu": "regime-conditional structure menu (E7.5)",
    "ranker": "ranker choice (config/ranking.yaml, D25)",
    "exit-policy": "exit policy / early exits vs hold to expiry (config/exits.yaml, D19)",
    "sizing": "sizing = min(Risk suggestion, 5%-equity cap) (D18)",
    "auto-approve": "auto-approve gating (D34 + E7.5a scorecard gate)",
    # E27.8 (D133): model iteration is a standing responsibility (ML-<n> studies)
    "models": "ML models: registry, ML-<n> studies, live shadow monitoring (D126, D133)",
}
THEME_STATUSES = ("no-evidence", "watching", "action-proposed", "settled")
REPORT_SECTIONS = (
    "Performance vs model",
    "Standing themes",
    "Quick changes",
    "Quick-change review",
    "Experiments",
    "Next XP",
    "Backtest",
    "Next ML",
    "Obvious flaws",
    "create A-",
)

# Forward A/B experiments (PLAN D44, E10.6). Mirrors arc.experiments.models without importing
# it: this gate runs on the host python3 with the stdlib only (a test pins the mirror).
EXPERIMENT_AREAS = ("entries", "exits", "ranking", "sizing", "universe", "funnel", "other")
OVERLAY_TARGETS = ("ranking", "exits", "costs", "account_profiles", "routines", "universe")
SPEC_KEYS = (
    "spec_version",
    "id",
    "title",
    "hypothesis",
    "area",
    "kind",
    "arms",
    "primary_metric",
    "secondary_metric",
    "non_inferiority_margin",
    "alpha",
    "power",
    "mde",
    "min_sessions",
    "max_sessions",
    "proposed_by",
    "backtest_ref",
    "analysis",  # D119: pre-registered SPY adjustment + HAC (adopt fills the default)
)
EXPERIMENT_ID_RE = re.compile(r"^XP-[1-9]\d*$")
# Statuses the weekly report must cover (terminal promoted/rejected ones are history).
REPORTABLE = ("draft", "queued", "registered", "running", "stopped")

# ---- E21.3 (D86): XP proposals in the Analyst-owned backlog ----
# D121: a proposal reserves its XP-<n> when it is proposed (the next free number across the
# registry DB, config/experiments/live/, open xp/<n> spec PRs and the backlog itself) and
# registers under that same id. P-<n> (D86) is retired: kept only as `legacy_id` on
# migrated entries and read in old `proposal_ref` / `owner_approval` rows.
BACKLOG_FILE = "xp_backlog.json"
BACKLOG_VERSION = 2  # v2 (D121): XP-<n> ids, `next_id` = the next free XP number
PROPOSALS_DIR = "proposals"  # approved specs: the input of `arc experiment adopt` (E21.4)
PROPOSAL_ID_RE = re.compile(r"^XP-[1-9]\d*$")
LEGACY_PROPOSAL_ID_RE = re.compile(r"^P-[1-9]\d*$")
PROPOSAL_REF_RE = re.compile(r"^(?:XP|P)-[1-9]\d*$")  # what old and new events may carry
BACKTEST_ID_RE = re.compile(r"^BT-[1-9]\d*$")
SPEC_PR_BRANCH_RE = re.compile(r"^xp/([1-9]\d*)(?:-|$)")  # adopt's xp/<n>, verdicts xp/<n>-…
BACKLOG_STATUSES = ("proposed", "approved", "registered", "running", "concluded", "dropped")
BACKLOG_OPEN = ("proposed", "approved", "registered", "running")
BACKLOG_RANKED = ("proposed", "approved")  # not in the DB yet: what next_experiment picks from
BACKLOG_CAP = 12  # open entries; the Analyst drops the weakest with a reason
MAX_NEW_PROPOSALS = 2  # per run
# ---- E26.2 (D121): BT study proposals in the same backlog (`kind: bt`) ----
# A `study_spec` draft becomes a `kind: bt` entry keyed `bt:<finding key>` with its BT-<n>
# (kept across runs while the draft is re-listed). Value = pct_per_day x affected_share x
# p_success / nights_needed (cost = estimated compute nights instead of sessions). Owner:
# `approve BT-<n>` -> `arc backtest study adopt` (spec PR + data/backtest/queue.json),
# `drop BT-<n>` (draft not committed), `stop BT-<n>` (queued/running).
BT_KEY_PREFIX = "bt:"
BT_STATUSES = ("proposed", "approved", "queued", "running", "concluded", "stopped", "failed")
BT_QUEUED = ("queued", "running", "concluded", "stopped", "failed")  # in queue.json
BT_WEEKLY_CAP = 2  # D121: owner approvals per ISO week (America/New_York)
BT_QUEUE = Path("data") / "backtest" / "queue.json"
STUDY_VALUE_KEYS = ("pct_per_day", "source", "affected_share", "p_success", "nights_needed")
# ---- E27.8 (D133): ML-<n> model studies in the same backlog (`kind: ml`) ----
# An `ml_study_spec` draft becomes a `kind: ml` entry keyed `ml:<finding key>` with its ML-<n>
# (the next free number across config/ml_studies/, the shared queue, the ML ledger, the
# retired list, open ml/<n> spec PRs and the backlog; never reused). Same value formula as
# BT (cost = compute nights). Owner: `approve ML-<n>` -> `arc ml study adopt` (spec PR +
# a `kind: ml` queue entry; the 2-per-ISO-week approval cap is shared with BT),
# `drop ML-<n>` (`arc ml study drop`: the number stays a gap), `stop ML-<n>` (queued).
# Mirrors arc.ml.study (FAMILY_RULE, ChangeKind, MlStudySpec); a test pins the mirror.
ML_KEY_PREFIX = "ml:"
ML_STUDY_ID_RE = re.compile(r"^ML-[1-9]\d*$")
ML_SPEC_PR_BRANCH_RE = re.compile(r"^ml/([1-9]\d*)(?:-|$)")  # arc ml study adopt's ml/<n>
ML_FAMILY_RULE = {"m1": "m1_keep", "credit": "m2_promote", "debit": "m2_promote"}
ML_CHANGE_KINDS = ("challenger", "tuning", "features", "calibration")
ML_SPEC_KEYS = ("hypothesis", "family", "incumbent", "change_kind", "overlay", "grid",
                "decision_rule")  # fmt: skip
ML_SPEC_REQUIRED = ("hypothesis", "family", "incumbent", "change_kind", "decision_rule")
ML_MAX_GRID = 48  # MlStudySpec.grid max_length (ml_studies.max_grid may be lower: validate)
ML_MODELS = Path("data") / "ml" / "models"
ML_LEDGER = Path("data") / "ml" / "studies" / "ledger.jsonl"
ML_HEALTH = Path("data") / "ml" / "health"
ML_SPECS = Path("config") / "ml_studies"
ML_RETIRED = ML_SPECS / "retired.yaml"
ML_SPECS_DIR = "study-specs"  # RUN_DIR/study-specs/ML-<n>.yaml, next to the BT drafts
ML_MIN_N = 30  # live calibration / drift below this n is "insufficient" (E27.4)
ML_SECTION_MAX = 3_000
STUDY_KINDS = ("bt", "ml")
K_TREATMENTS = 3  # D108 (revises D86's 4): control + exactly 3 treatments
TREATMENT_NAMES = tuple(f"t{i}" for i in range(1, K_TREATMENTS + 1))
#: D108: an XP runs control as its own arm on the experiment account: C + K runner arms.
ARMS_PER_XP = K_TREATMENTS + 1
#: D108: the spec version an approved proposal is written in (control is a runnable arm).
PROPOSAL_SPEC_VERSION = 3
# A proposal's spec: the live v2 format minus what approval fills (id, proposed_by) and what
# `arc experiment create` fills from config/experiments.yaml (alpha, power, mde, sessions).
PROPOSAL_SPEC_FORBIDDEN = {
    "id": "the backlog reserves its XP-<n> when it records the proposal (D121); omit it",
    "proposed_by": "filled at adoption; omit it",
    "alpha": "filled from config/experiments.yaml; omit it",
    "power": "filled from config/experiments.yaml; omit it",
    "mde": "filled from config/experiments.yaml; omit it",
    "min_sessions": "filled from config/experiments.yaml; omit it",
    "max_sessions": "filled from config/experiments.yaml; omit it",
}
PROPOSAL_SPEC_REQUIRED = (
    "title",
    "hypothesis",
    "area",
    "arms",
    "non_inferiority_margin",
    "backtest_ref",
)
PROPOSAL_FIELDS = (
    "spec",
    "evidence",
    "expected_effect",
    "affected_share",
    "p_success",
    "sessions_needed",
    "value_score",
)
PROPOSAL_EVIDENCE = ("live", "backtest")  # backtest = a BT-<n> holdout study (E7.15, option A)
STOP_ADVICE = ("continue", "futility", "baseline-changed", "better-use-of-slot")
# Daily effects (fraction of t0 equity) the gate sizes at K=3 with `arc experiment plan`.
PLAN_EFFECTS = (0.0002, 0.0005, 0.001, 0.002)
PLAN_VIEW = f"plan-k{K_TREATMENTS}"  # the RUN_DIR log name of the sessions-needed grid
REGIME_VIEW = "regime-panel"  # E25.5 (D108): RUN_DIR log of `arc experiment regime-panel --json`
REGIME_PANEL_VERSION = 1  # the regime_panel JSON shape this gate renders
REGIME_SECTION_MAX = 3000  # chars of bucket lines inline; all lines in regime-buckets.log
MAX_PARALLEL_ARMS_DEFAULT = 10
MAX_ARMS_KEY = "experiments.runner.max_parallel_arms"
# No A/A sigma in the store (e.g. after the D80 fresh start): size the grid with the sigma the
# XP-1 A/A write-up documents, labelled as such.
AA_DOC = Path("docs") / "RESEARCH" / "experiments" / "XP-1-aa.md"
AA_DOC_SIGMA_RE = re.compile(r"sigma\(d_t\)\s*=\s*([0-9.]+)%\s*of t0 equity")
MERGED_PR_LINE_RE = re.compile(r"^\s*(XP-advisory|Experiment|Flag|Lane):.*$", re.MULTILINE)

# ---- E21.3 (D86): quick changes (ship now, no XP), reviewed by the next run ----
MAX_QUICK_CHANGES = 3
QC_KINDS = ("config", "code")
# a backtest alone is never enough for a quick change (D86 option A)
QC_EVIDENCE = ("live", "flaw", "cost-model", "xp-winner")
QC_FIELDS = ("change", "kind", "evidence_kind", "expected_effect", "risk", "rollback", "apply")
QC_REVIEW = ("keep", "roll-back", "too-early")
QC_CONFIG_PREFIXES = ("!arc config set ", "arc config set ")
QC_PR_LINE = "XP-advisory: none — Analyst quick change"
QC_REVIEW_STATUS = {"keep": "kept", "roll-back": "rolled-back"}
# E21.2 (D86): the idea seed. `#` comment lines, then JSON (valid YAML; stdlib has no YAML).
IDEA_SEED_FILE = "idea_seed.yaml"
SEED_KEYS = ("seed_version", "ideas")
IDEA_KEYS = (
    "id",
    "title",
    "area",
    "variables",
    "treatments",
    "hypothesis",
    "source",
    "evidence_refs",
    "notes",
    "retired_spec",
)
IDEA_REQUIRED = ("id", "title", "area", "variables", "treatments", "hypothesis", "source")
IDEA_ID_RE = re.compile(r"^S-[1-9]\d*$")
MAX_SEED_TREATMENTS = 4

# ---- E7.15 (D84): the backtest study framework (E7.12) as evidence ----
# Repo-relative inputs, all read-only. The ledger and checkpoints live under the gitignored
# data/; reports, the reconciliation (E7.14) and specs are committed.
BT_LEDGER = Path("data") / "backtest" / "ledger.jsonl"
BT_REPORTS = Path("docs") / "RESEARCH" / "studies"
BT_SPECS = Path("config") / "backtest_studies"
# E26.5 (D127): retired studies (history, never evidence). JSON body under `#` comments,
# read with the stdlib like idea_seed.yaml; arc.backtest.study.load_retired is the strict
# loader (a test pins that both read the committed file the same).
BT_RETIRED = BT_SPECS / "retired.yaml"
BT_CHECKPOINTS = Path("data") / "backtest_universe" / "checkpoints.json"  # E7.9
# E7.14's backtest<->live reconciliation, aggregates only:
# {"generated_at", "git_sha", "sim_slippage_frac",
#  "rows": [{"kind", "regime", "n", "entry_pred", "entry_actual", "exit_pred", "exit_actual",
#            "exit_reason_match", "pnl_tracking_error", "pnl_bias"}],
#  "summary": [str]}  ($ per unit / per trade). E7.22's `arc backtest recon` writes it
# (rows = closed live trades replayed through the sim, `exit_reason_match` = the share
# whose sim exit falls on live's exit day -- live's reasons (research_review) and the
# sim's (take_profit/stop/dte_exit) are different vocabularies; summary = slippage x,
# menu overlap, stance agreement).
BT_RECONCILIATION = BT_REPORTS / "reconciliation.json"
BT_SECTION_MAX = 4_000
BT_CHECKPOINT_NAMES = ("c125", "c250", "c500")
BT_CONFIRM_RUNG = "c500"  # D86 option A: only a C500 holdout backs an XP proposal
# A ranker arm (no overlay) tests the live menu ranking: these proposal leaves carry it.
BT_RANKER_LEAVES = ("exits.pipeline.rank_menu_by", "exits.pipeline.menu_measure")
# Mirrors arc.backtest.study.StudySpec (extra=forbid; a test pins the mirror). The Analyst
# drafts hypothesis / arms / profiles (+ optional primary_metric, max_variants, period);
# `record` fills id, owner, created and the ladder; the D25 rule is the model default.
STUDY_SPEC_KEYS = (
    "id",
    "hypothesis",
    "owner",
    "created",
    "arms",
    "profiles",
    "universe_ladder",
    "period",
    "decision_rule",
    "primary_metric",
    "max_variants",
    "robustness",
)
STUDY_SPEC_FILLED = {
    "id": "the id is assigned by `record` (BT-<next>); omit it",
    "owner": "filled by `record` (the A-id); omit it",
    "created": "filled by `record`; omit it",
    "universe_ladder": "fixed at [c125, c500] for Analyst drafts (E7.15); omit it",
    "decision_rule": "the D25 rule is copied verbatim (the model default); omit it",
}
STUDY_LADDER = ["c125", "c500"]
STUDY_TARGETS = ("ranking", "exits", "costs")
STUDY_PROFILES = ("margin", "cash_debit", "cash_long_only")
STUDY_RANKERS = (
    "credit_width",
    "debit_width",
    "ev_proxy",
    "managed_net_ev",
    "rorc_day",
    "rorc_day_vrp",
    "managed_net_ev_tilted",
    "rorc_day_tilted",
)
STUDY_PRIMARY = ("net_pnl", "sharpe", "sortino", "cagr", "return_pct", "profit_factor")
STUDY_PERIOD_KEYS = ("dev_start", "dev_end", "holdout_start", "holdout_end", "settle_days")
STUDY_SPECS_DIR = "study-specs"
STUDY_SPEC_CATEGORIES = ("theme", "data-gap", "xp-proposal")  # untested hypotheses only
# Legacy (pre-E7.15) backtest_ref values: kept only on backlog entries that already held them.
LEGACY_REF_PREFIXES = ("docs/RESEARCH", "data/backtest")

# ---- E26.4 (D121): the short verdict run ----
XP_VERDICTS = Path("config") / "experiments" / "live" / "verdicts"  # E21.4 verdict files
VERDICT_REPORT_MAX = 1_400  # D141 layout
VERDICT_REPORT_SECTIONS = ("verdict run", "*Read*", "*Follow-up*")
VERDICT_DOC_KEYS = ("mode", "verdicts", "findings", "backlog_drops")
VERDICT_CATEGORIES = ("xp-proposal", "theme", "data-gap")  # a study_spec rides on any of them
VERDICT_MAX_NEW = 1  # new proposals per kind (XP, BT/ML together) per verdict run
VERDICT_REF_RE = re.compile(r"(?<![A-Za-z0-9_-])((?:XP|BT|ML)-[1-9]\d*)(?![0-9])")
WEEKLY_TZ = ZoneInfo("America/Los_Angeles")  # arc-analyst-weekly-audit: `0 14 * * 0` host PT
WEEKLY_WEEKDAY = 6  # Sunday
WEEKLY_AT = dt.time(14, 0)
VERDICT_WEEKLY_GAP = dt.timedelta(hours=24)  # skip when the weekly run is due within this
VERDICT_RUN_SUFFIX = "-verdict"


@dataclasses.dataclass(frozen=True)
class Paths:
    """Every filesystem location the gate touches (tests build their own)."""

    home: Path  # analyst state: ledger.json, state.json, runs/
    repo: Path  # the checkout whose config/ and .venv the live tick uses
    live_db: Path
    arc: tuple[str, ...]  # argv prefix of the arc CLI
    gh: tuple[str, ...] = ()  # argv prefix of the GitHub CLI; () = skip the merged-PR section
    backtest_root: Path | None = None  # E7.15: where data/backtest + studies live; None = repo
    verdicts_dir: Path | None = None  # E26.4: XP verdict files; None = repo's XP_VERDICTS

    @property
    def bt_root(self) -> Path:
        return self.backtest_root or self.repo

    @property
    def xp_verdicts(self) -> Path:
        return self.verdicts_dir or self.repo / XP_VERDICTS

    @property
    def ledger(self) -> Path:
        return self.home / "ledger.json"

    @property
    def state(self) -> Path:
        return self.home / "state.json"

    @property
    def runs(self) -> Path:
        return self.home / "runs"

    @property
    def idea_seed(self) -> Path:
        """E21.2 (D86): the retired dev drafts as leads; install.sh copies it here."""
        return self.home / IDEA_SEED_FILE

    @property
    def backlog(self) -> Path:
        """E21.3 (D86): the Analyst-owned XP backlog (XP-<n>, D121); not the repo or DB."""
        return self.home / BACKLOG_FILE

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Paths:
        env = dict(os.environ if env is None else env)
        repo = Path(env.get("ARC_REPO") or Path.home() / "GitHub" / "Project-Arc").expanduser()
        home = Path(
            env.get("ARC_ANALYST_HOME")
            or Path.home() / ".hermes" / "profiles" / "arc-analyst" / "analyst"
        )
        db = Path(env.get("ARC_ANALYST_DB") or repo / "data" / "arc.db").expanduser()
        arc = env.get("ARC_ANALYST_ARC") or str(repo / ".venv" / "bin" / "arc")
        gh = env.get("ARC_ANALYST_GH", "gh")
        return cls(
            home=home.expanduser(),
            repo=repo,
            live_db=db,
            arc=(arc,),
            gh=(gh,) if gh else (),
        )


# ---------- pure helpers (unit-tested) ----------


def load_json(path: Path, default):
    return json.loads(path.read_text()) if path.exists() else default


def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
    tmp.replace(path)


def to_db(when: dt.datetime) -> str:
    return when.astimezone(dt.timezone.utc).strftime(DB_FMT)


def iso(when: dt.datetime) -> str:
    return when.astimezone(dt.timezone.utc).isoformat(timespec="seconds")


def clip(text: str, n: int) -> str:
    text = text.rstrip()
    return text if len(text) <= n else text[:n] + f"\n...[clipped {len(text) - n} chars]"


def closed_filter(alias: str = "") -> str:
    a = f"{alias}." if alias else ""
    return f"({a}exit_fill IS NOT NULL OR {a}status IN ('closed', 'expired_worthless'))"


def snapshot_db(live: Path, dest: Path) -> None:
    """Copy *live* to *dest* with the backup API; the live file is opened read-only.

    The live store runs in WAL mode, where even a read-only reader needs the ``-shm``
    index, so SQLite may leave empty ``-wal``/``-shm`` sidecars next to it (the tick
    leaves the same files); ``arc.db`` itself is never written. The copy is switched
    to ``journal_mode=DELETE`` so reading it later leaves nothing behind.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    for q in (dest, *(dest.with_name(dest.name + s) for s in ("-wal", "-shm", "-journal"))):
        q.unlink(missing_ok=True)
    src = sqlite3.connect(f"{live.resolve().as_uri()}?mode=ro", uri=True)
    dst = sqlite3.connect(dest)
    try:
        src.backup(dst)
        dst.execute("PRAGMA journal_mode = DELETE")
    finally:
        src.close()
        dst.close()


def wake_decision(
    conn: sqlite3.Connection,
    state: dict,
    now: dt.datetime,
    holdouts: list | None = None,
    xp_verdicts: list | None = None,
    ml_verdicts: list | None = None,
) -> dict:
    """Whether the agent should run, and why. Reads the COPY only. *holdouts* are the
    research ledger's holdout run keys (E7.15): one not seen at the last ``mark`` wakes it.
    *xp_verdicts* are the XP verdict files' keys (E26.4, :func:`xp_verdict_keys`): the same.
    *ml_verdicts* are the ML ledger's verdict keys (E27.8, :func:`ml_verdict_key`): the same."""
    closed_total, closed_max = conn.execute(
        f"SELECT COUNT(*), COALESCE(MAX(rowid), 0) FROM outcomes WHERE {closed_filter()}"
    ).fetchone()
    last_run = state.get("last_run_at")
    since = (
        dt.datetime.fromisoformat(last_run) if last_run else now - dt.timedelta(days=WINDOW_DAYS)
    )
    watermark = int(state.get("closed_watermark", 0))
    new_closed = conn.execute(
        f"SELECT COUNT(*) FROM outcomes WHERE {closed_filter()} AND rowid > ?", (watermark,)
    ).fetchone()[0]
    halts = [
        dict(zip(("at", "kind", "reason"), r))
        for r in conn.execute(
            "SELECT at, kind, reason FROM halts WHERE at > ? ORDER BY at", (to_db(since),)
        )
    ]
    reasons = []
    if new_closed:
        reasons.append(f"{new_closed} closed outcome(s) since the last run")
    if halts:
        kinds = sorted({h["kind"] for h in halts})
        reasons.append(f"{len(halts)} halt(s) since the last run ({', '.join(kinds)})")
    exp_events = []
    if _has_table(conn, "experiment_events"):
        exp_events = [
            f"{e} {s}" + (f" ({r})" if r else "")
            for e, s, r in conn.execute(
                "SELECT experiment_id, status, reason FROM experiment_events WHERE at > ? "
                "AND status IN ('running', 'stopped', 'promoted', 'rejected') ORDER BY id",
                (to_db(since),),
            )
        ]
    if exp_events:
        reasons.append(f"experiment status change(s) since the last run: {', '.join(exp_events)}")
    seen = set(state.get("bt_holdouts_seen") or [])
    new_holdouts = [h for h in holdouts or [] if h not in seen]
    if new_holdouts:
        ids = verdict_ids(new_holdouts)
        reasons.append(f"new backtest holdout verdict(s) since the last run: {ids}")
    new_xp = [k for k in xp_verdicts or [] if k not in set(state.get("xp_verdicts_seen") or [])]
    if new_xp:
        reasons.append(f"new XP verdict file(s) since the last run: {verdict_ids(new_xp)}")
    new_ml = [k for k in ml_verdicts or [] if k not in set(state.get("ml_verdicts_seen") or [])]
    if new_ml:
        reasons.append(f"new ML study verdict(s) since the last run: {verdict_ids(new_ml)}")
    wake = closed_total > 0 and bool(reasons)
    if closed_total == 0:
        reasons = ["no closed outcome in the journal yet (E9.4 start condition)"]
    elif not reasons:
        reasons = [
            "no new closed outcome, no halt, no experiment status change and no new backtest "
            "holdout verdict or XP verdict file since the last run"
        ]
    return {
        "wake": wake,
        "reasons": reasons,
        "closed_total": closed_total,
        "closed_max_rowid": closed_max,
        "new_closed": new_closed,
        "halts": halts,
        "since": iso(since),
        "new_holdouts": new_holdouts,
        "new_xp_verdicts": new_xp,
        "new_ml_verdicts": new_ml,
    }


def verdict_ids(keys: list) -> str:
    """``BT-3, XP-2`` from verdict keys ``<id>:<hash>[:<at>]``, numerically sorted per kind."""
    ids = {k.split(":", 1)[0] for k in keys}
    return ", ".join(sorted(ids, key=lambda s: (s[:2], int(s[3:]))))


def xp_verdict_keys(directory: Path) -> list[str]:
    """E26.4: one key per committed XP verdict file, ``XP-<n>:<sha256 of the text, 12>``.
    A rewritten file (a new verdict on the same id) is a new key; a missing dir is none."""
    if not directory.is_dir():
        return []
    out = []
    for f in sorted(directory.glob("XP-*.yaml")):
        if EXPERIMENT_ID_RE.match(f.stem):
            out.append(f"{f.stem}:{hashlib.sha256(f.read_bytes()).hexdigest()[:12]}")
    return sorted(out, key=lambda k: int(k.split(":", 1)[0][3:]))


def sleeve_case(sleeves: list | None) -> str:
    """E22.5 (D97): SQL for a closed trade's sleeve from its open proposal's entry DTE.

    *sleeves* is ``[(name, dte_min, dte_max), ...]`` (the ``arc scorecard sleeves`` view's
    windows); an entry DTE in no window is ``other``. Names are ``[a-z][a-z0-9_]*``
    (``DteSleeve``), ints are ints: nothing user-controlled is spliced in.
    """
    dte = "CAST(json_extract(p.structure_json, '$.dte') AS INTEGER)"
    whens = " ".join(
        f"WHEN {dte} BETWEEN {int(lo)} AND {int(hi)} THEN '{name}'"
        for name, lo, hi in sleeves or []
        if re.fullmatch(r"[a-z][a-z0-9_]*", str(name))
    )
    return f"CASE {whens} ELSE 'other' END" if whens else "'other'"


def realised_vs_model(
    conn: sqlite3.Connection, since: dt.datetime | None, sleeves: list | None = None
) -> list[dict]:
    """Latest outcome row per proposal, closed only, bucketed by structure kind x regime x
    DTE sleeve (E22.5, D97: derived from the open's entry DTE, so pre-D97 trades map too)."""
    where = "AND l.at >= ?" if since else ""
    args = (to_db(since),) if since else ()
    rows = conn.execute(
        f"""WITH latest AS (
                SELECT o.* FROM outcomes o
                WHERE o.rowid = (SELECT MAX(o2.rowid) FROM outcomes o2
                                 WHERE o2.proposal_hash = o.proposal_hash))
            SELECT COALESCE(json_extract(p.structure_json, '$.kind'), 'unknown') AS kind,
                   COALESCE(p.regime, 'unknown') AS regime,
                   {sleeve_case(sleeves)} AS sleeve,
                   COUNT(*) AS n,
                   SUM(CAST(l.realised_pnl AS REAL)) AS pnl,
                   SUM(CAST(l.ev_total AS REAL)) AS ev,
                   AVG(CAST(l.pnl_vs_ev AS REAL)) AS avg_vs_ev,
                   AVG(l.slippage_bps) AS slip_bps,
                   AVG(l.cost_bps) AS cost_bps,
                   SUM(CASE WHEN CAST(l.realised_pnl AS REAL) > 0 THEN 1 ELSE 0 END) AS wins
            FROM latest l LEFT JOIN proposals p ON p.proposal_hash = l.proposal_hash
            WHERE {closed_filter("l")} AND l.realised_pnl IS NOT NULL {where}
            GROUP BY 1, 2, 3 ORDER BY n DESC, kind, regime, sleeve""",
        args,
    ).fetchall()
    cols = (
        "kind", "regime", "sleeve", "n", "pnl", "ev", "avg_vs_ev", "slip_bps", "cost_bps", "wins"
    )  # fmt: skip
    return [dict(zip(cols, r)) for r in rows]


def sleeve_windows(view_json: str) -> list:
    """``[(name, dte_min, dte_max)]`` from ``arc scorecard sleeves --json`` (E22.5); [] if
    the view failed (the table then shows every trade as ``other``)."""
    try:
        doc = json.loads(view_json)
    except ValueError:
        return []
    return [
        (s["sleeve"], s["dte_min"], s["dte_max"])
        for s in doc.get("sleeves") or []
        if s.get("dte_min") is not None and s.get("dte_max") is not None
    ]


def sleeve_lines(view_json: str) -> list[str]:
    """The D97 per-sleeve report (verdict, n, CI) from ``arc scorecard sleeves --json``."""
    try:
        doc = json.loads(view_json)
    except ValueError:
        return [clip(view_json, 800)]
    weeks = doc.get("weeks")
    out = [
        f"- rule (D97, fixed in advance, report only): {doc.get('rule', '')}",
        f"- measurement start {str(doc.get('measurement_start') or 'n/a')[:10]}, "
        f"{_num(weeks, '.1f')} of {doc.get('max_weeks')} weeks",
    ]
    for s in doc.get("sleeves") or []:
        window = (
            f"{s['dte_min']}-{s['dte_max']} DTE" if s.get("dte_min") is not None else "no sleeve"
        )
        verdict = s.get("verdict") or "n/a"
        if s.get("verdict") and not s.get("ready"):
            verdict += " (not ready)"
        out.append(
            f"- {s['sleeve']} ({window}): opens {s['opens']}, closed n={s['closed']}, "
            f"win {_num(s.get('win_rate'), '.0%')}, mean {_num(s.get('mean_pnl'), '+,.2f')}, "
            f"median {_num(s.get('median_pnl'), '+,.2f')}, mean net "
            f"{_num(s.get('mean_net_pnl'), '+,.2f')} (CI {_num(s.get('ci_low'), '+,.2f')}.."
            f"{_num(s.get('ci_high'), '+,.2f')}), P&L/max loss "
            f"{_num(s.get('mean_pct_of_max_loss'), '+.0%')}, days held "
            f"{_num(s.get('mean_days_held'), '.1f')}, MAE {_num(s.get('mean_mae'), '+,.2f')}, "
            f"MFE {_num(s.get('mean_mfe'), '+,.2f')}, exits {s.get('exit_reasons') or {}}, "
            f"verdict {verdict}"
        )
    return out


def _num(v, fmt: str) -> str:
    return "n/a" if v is None else format(v, fmt)


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
        ).fetchone()
        is not None
    )


def _treatment_overlay(spec: dict) -> dict:
    """The treatment overlay of a v1 spec (``arms.treatment``) or a one-treatment v2
    spec (``arms.treatments.t1``, D69); a multi-treatment spec maps arm -> overlay."""
    arms = spec.get("arms") or {}
    if "treatment" in arms:
        return (arms.get("treatment") or {}).get("overlay") or {}
    ts = arms.get("treatments") or {}
    if len(ts) == 1:
        return (next(iter(ts.values())) or {}).get("overlay") or {}
    return {name: (arm or {}).get("overlay") or {} for name, arm in ts.items()}


def _loads(text, default):
    try:
        return json.loads(text) if text else default
    except ValueError:
        return default


def _treatment_count(spec: dict) -> int:
    """Runner arms of a spec: what it holds of max_parallel_arms. v1 ``treatment`` = 1; a
    multi-treatment spec its treatments, plus the control arm from spec v3 (D108)."""
    arms = spec.get("arms") or {}
    if "treatment" in arms:
        return 1
    control = 1 if int(spec.get("spec_version") or 2) >= PROPOSAL_SPEC_VERSION else 0
    return max(1, len(arms.get("treatments") or {})) + control


def _proposal_ref(detail) -> str | None:
    """The Analyst proposal an experiment event names (E21.4 ``proposal_ref``, or the D86
    ``owner_approval`` ref naming a proposal: ``XP-<n>``, or a legacy ``P-<n>``)."""
    if not isinstance(detail, dict):
        return None
    for key in ("proposal_ref", "owner_approval"):
        ref = str(detail.get(key) or "")
        if PROPOSAL_REF_RE.match(ref):
            return ref
    return None


def experiments_overview(conn: sqlite3.Connection) -> list[dict]:
    """Every forward experiment (D44) with its status, lock and latest stored report.

    Reads ``experiments`` / ``experiment_events`` / ``experiment_reports`` (E10.1, E10.3) on
    the COPY. A store from before E10 has none of them: returns ``[]``.
    """
    if not _has_table(conn, "experiments") or not _has_table(conn, "experiment_events"):
        return []
    has_reports = _has_table(conn, "experiment_reports")
    ids = [
        r[0]
        for r in conn.execute(
            "SELECT experiment_id FROM experiments GROUP BY experiment_id ORDER BY MIN(id)"
        )
    ]
    out = []
    for eid in ids:
        area, kind, spec_text, spec_hash, revision = conn.execute(
            "SELECT area, kind, spec, spec_hash, revision FROM experiments "
            "WHERE experiment_id = ? ORDER BY revision DESC LIMIT 1",
            (eid,),
        ).fetchone()
        spec = _loads(spec_text, {})
        events = [
            {"id": i, "status": s, "reason": r, "spec_hash": h, "detail": _loads(d, {}), "at": a}
            for i, s, r, h, d, a in conn.execute(
                "SELECT id, status, reason, spec_hash, detail, at FROM experiment_events "
                "WHERE experiment_id = ? ORDER BY id",
                (eid,),
            )
        ]
        last = events[-1] if events else {"status": "draft", "reason": None, "at": None}
        locked = [e for e in events if e["status"] != "draft"]
        running = next((e for e in events if e["status"] == "running"), None)
        stopped = next((e for e in reversed(events) if e["status"] == "stopped"), None)
        proposal_ref = next(
            (ref for e in events for ref in [_proposal_ref(e["detail"])] if ref), None
        )
        report = None
        if has_reports:
            row = conn.execute(
                "SELECT evaluated_at, verdict, sessions, as_of_day, payload "
                "FROM experiment_reports WHERE experiment_id = ? ORDER BY id DESC LIMIT 1",
                (eid,),
            ).fetchone()
            if row:
                report = {
                    "evaluated_at": row[0],
                    "verdict": row[1],
                    "sessions": row[2],
                    "as_of_day": row[3],
                    "payload": _loads(row[4], {}),
                }
        out.append(
            {
                "experiment_id": eid,
                "area": area,
                "kind": kind,
                "title": spec.get("title", ""),
                "hypothesis": spec.get("hypothesis", ""),
                "proposed_by": spec.get("proposed_by"),
                "backtest_ref": spec.get("backtest_ref"),
                "overlay": _treatment_overlay(spec),
                "arms": _treatment_count(spec),
                "proposal_ref": proposal_ref,
                "revision": revision,
                "spec_hash": spec_hash,
                "registered_hash": locked[0]["spec_hash"] if locked else None,
                "status": last["status"],
                "reason": last["reason"],
                "status_at": last["at"],
                "queued_event": max(
                    (e["id"] for e in events if e["status"] == "queued"), default=None
                ),
                "t0": (running or {}).get("detail", {}).get("t0"),
                "aa_override": bool((running or {}).get("detail", {}).get("aa_override")),
                "stop": (stopped or {}).get("detail") or None,
                "report": report,
            }
        )
    return out


def next_experiment_id(overview: list[dict]) -> str:
    nums = [
        int(e["experiment_id"].split("-", 1)[1])
        for e in overview
        if EXPERIMENT_ID_RE.match(e["experiment_id"])
    ]
    return f"XP-{max(nums, default=0) + 1}"


def area_queues(overview: list[dict]) -> dict[str, dict]:
    """Per area: the experiment holding it (registered/running) and the queue behind it."""
    out = {}
    for area in EXPERIMENT_AREAS:
        mine = [e for e in overview if e["area"] == area]
        holder = [e["experiment_id"] for e in mine if e["status"] in ("registered", "running")]
        queued = sorted(
            (e for e in mine if e["status"] == "queued"), key=lambda e: e["queued_event"] or 0
        )
        out[area] = {"holder": holder, "queued": [e["experiment_id"] for e in queued]}
    return out


def _pct(v) -> str:
    return "n/a" if v is None else f"{v * 100:+.3f}%"


def experiment_lines(overview: list[dict]) -> list[str]:
    """The pre-run context section for forward experiments (status, report, breakdowns, queue)."""
    if not overview:
        return ["- none registered yet (arc experiment list is empty)"]
    out = []
    for e in overview:
        status = e["status"] + (f" ({e['reason']})" if e["reason"] else "")
        lock = (
            "draft (not locked)"
            if e["registered_hash"] is None
            else f"locked sha256 {e['registered_hash'][:12]}"
        )
        out.append(f"### {e['experiment_id']} [{status}] {e['kind']}/{e['area']}: {e['title']}")
        out.append(
            f"- hypothesis: {clip(e['hypothesis'], 300)} | proposed_by {e['proposed_by']} | "
            f"backtest_ref {e['backtest_ref'] or '-'} | {lock} | since {e['status_at']}"
        )
        if e["overlay"]:
            out.append(f"- treatment overlay: {json.dumps(e['overlay'], sort_keys=True)}")
        if e["t0"]:
            out.append(
                f"- t0 {e['t0']}"
                + (" | ab started WITHOUT an A/A (owner override)" if e["aa_override"] else "")
            )
        if e["stop"]:
            s = e["stop"]
            out.append(
                f"- stop: sessions {s.get('sessions')} sigma {_pct(s.get('sigma'))} "
                f"note {s.get('note') or '-'}"
            )
        r = e["report"]
        if not r:
            out.append("- latest report: none stored yet")
            continue
        p = r["payload"]
        prim, sec = p.get("primary") or {}, p.get("secondary") or {}
        ci = prim.get("ci") or {}
        ci_txt = f"[{_pct(ci.get('lo'))}, {_pct(ci.get('hi'))}]" if ci else "n/a"
        out.append(
            f"- latest report {r['evaluated_at']} as of {r['as_of_day'] or '-'}: verdict "
            f"{r['verdict'].upper()} ({p.get('verdict_reason', '')}); sessions {r['sessions']} "
            f"(min {p.get('min_sessions')}, max {p.get('max_sessions')})"
        )
        out.append(
            "- primary (paired daily net P&L diff, % t0 equity): "
            f"mean {_pct(prim.get('mean'))}/day "
            f"always-valid CI {ci_txt} sigma {_pct(prim.get('sigma'))} "
            f"({prim.get('sigma_source') or '-'})"
        )
        margin = sec.get("margin")
        out.append(
            f"- secondary Sortino: control {_num(sec.get('sortino_control'), '.2f')} "
            f"treatment {_num(sec.get('sortino_treatment'), '.2f')} "
            + (
                f"margin {margin}: non_inferior={sec.get('non_inferior')}"
                if margin is not None
                else "(aa: no margin)"
            )
        )
        for a in p.get("arms") or []:
            out.append(
                f"- arm {a.get('arm')}: P&L {_num(a.get('total_pnl'), '+,.2f')} "
                f"max DD {_pct(a.get('max_drawdown'))} worst day {_pct(a.get('worst_day'))} "
                f"orders {a.get('orders')} fills {a.get('filled_executions')}/"
                f"{a.get('executions')} slippage {_num(a.get('mean_slippage_bps'), '.1f')} bps"
            )
        cal = p.get("calibration") or {}
        if cal.get("sigma") is not None:
            out.append(
                f"- calibration: sigma {_pct(cal.get('sigma'))} MDE "
                + ", ".join(f"{k}:{_pct(v)}" for k, v in (cal.get("mde_fixed") or {}).items())
                + f" | slippage gap {_num(cal.get('slippage_gap_bps'), '+.1f')} bps"
            )
        rows = p.get("breakdowns") or []
        out += _verdict_pair_lines(p) + _production_lines(p)
        if rows:
            out.append("- breakdowns (closed trades; per arm):")
            out += [
                f"  - {b.get('by')}={b.get('key')} {b.get('arm')}: n={b.get('trades')} "
                f"realised {_num(b.get('realised_pnl'), '+,.2f')}"
                for b in rows
            ]
        else:
            out.append("- breakdowns: no closed trade in either arm yet")
    out.append("")
    out.append("### Queue per area (one registered/running experiment per area)")
    for area, q in area_queues(overview).items():
        out.append(
            f"- {area}: holder {', '.join(q['holder']) or '-'} | queued "
            f"{', '.join(q['queued']) or '-'}"
        )
    return out


def _ci_text(ci) -> str:
    return f"[{_pct(ci.get('lo'))}, {_pct(ci.get('hi'))}]" if isinstance(ci, dict) else "n/a"


def _verdict_pair_lines(p: dict) -> list[str]:
    """The per-treatment verdicts of a multi-arm report: Ti vs C (D108: C = the control arm
    from report v3, production up to v2). These are the verdicts; nothing else is."""
    pairs = [pr for pr in p.get("pairwise") or [] if pr.get("kind") == "control_vs_treatment"]
    if not pairs:
        return []
    ref = "control arm" if p.get("reference") == "control" else "control (production, v2)"
    out = [f"- verdicts (Ti vs C, C = {ref}; Holm across K={len(pairs)}):"]
    for pr in pairs:
        out.append(
            f"  - {pr.get('b')} vs {pr.get('a')}: {str(pr.get('verdict') or '-').upper()} "
            f"n={pr.get('n')} mean {_pct(pr.get('mean'))}/day CI {_ci_text(pr.get('ci'))} "
            f"holm p {_num(pr.get('holm_p'), '.3f')}"
        )
    return out


def _production_lines(p: dict) -> list[str]:
    """D108 production panel (report v3): context only, never a verdict. Ti vs P, the
    P - C decomposition (start book + residual) and the ramp flag."""
    prod = p.get("production")
    if not isinstance(prod, dict):
        return []
    book = prod.get("start_book") or {}
    still = sum(1 for d in (book.get("close_dates") or {}).values() if d is None)
    out = [
        "- production panel (DESCRIPTIVE, never a winner: verdicts are Ti vs C above): "
        f"P {_num(prod.get('cum_p_pnl'), '+,.2f')} C {_num(prod.get('cum_c_pnl'), '+,.2f')} | "
        f"P - C {_pct(prod.get('cum_d_p_c'))} = start book {_pct(prod.get('cum_start_book'))} "
        f"+ residual {_pct(prod.get('cum_residual'))} | start book {len(book.get('ids') or [])} "
        f"structures, {still} still open | ramped from {prod.get('ramped_from') or 'not yet'}"
    ]
    for c in prod.get("compare") or []:
        if c.get("arm") == "control" and c.get("basis") != "post_ramp":
            continue
        out.append(
            f"  - {c.get('arm')} - P ({c.get('basis')}): n={c.get('n')} mean "
            f"{_pct(c.get('mean'))}/day CI {_ci_text(c.get('ci'))}"
        )
    return out


def table_lines(rows: list[dict], slippage_frac: str) -> list[str]:
    out = [
        "| kind | regime | sleeve | n | realised $ | EV $ | avg realised-EV $ | win | slippage "
        "bps (entry, realised) | cost bps (Quant, round trip) | sample |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        out.append(
            f"| {r['kind']} | {r['regime']} | {r.get('sleeve', 'other')} | {r['n']} | "
            f"{_num(r['pnl'], ',.2f')} | "
            f"{_num(r['ev'], ',.2f')} | {_num(r['avg_vs_ev'], '+,.2f')} | "
            f"{r['wins']}/{r['n']} | {_num(r['slip_bps'], '.0f')} | {_num(r['cost_bps'], '.0f')} | "
            f"{f'LOW (n<{MIN_SAMPLE})' if r['n'] < MIN_SAMPLE else 'ok'} |"
        )
    if not rows:
        out.append("| (none) | | | 0 | | | | | | | |")
    out.append(f"costs.yaml slippage_frac = {slippage_frac} (fill at mid +/- frac x spread)")
    return out


def slippage_frac(costs_yaml: Path) -> str:
    if not costs_yaml.exists():
        return "n/a (config/costs.yaml missing)"
    m = re.search(r"^\s*slippage_frac:\s*([0-9.]+)", costs_yaml.read_text(), re.MULTILINE)
    return m.group(1) if m else "n/a"


def _leaves(data, prefix: tuple = ()) -> list[tuple]:
    """Dotted leaf paths of a nested overlay (a list is one leaf, as in a config value)."""
    if isinstance(data, dict) and data:
        out = []
        for k, v in data.items():
            out += _leaves(v, (*prefix, str(k)))
        return out
    return [prefix]


def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def validate_proposal_spec(spec, where: str = "proposal.spec") -> list[str]:
    """An XP proposal's draft spec (D86, D108): the ``config/experiments/live/`` format, no ``id``.

    Mirrors ``arc.experiments.models.ExperimentSpec`` (extra=forbid, ab, control empty), plus
    D108's shape: control + exactly 3 treatments ``t1..t3``, every overlay on the SAME leaf set
    (one variable at 3 levels, or 3 alternatives of one mechanism), no two arms alike. The
    owner's ``approve XP-<n>`` writes it under its reserved id as spec v3 (control runs as its
    own flat arm); ``arc experiment adopt`` re-validates it in full.
    """
    if not isinstance(spec, dict):
        return [f"{where} must be an object in the config/experiments/live v2 format"]
    errs = []
    for key, why in PROPOSAL_SPEC_FORBIDDEN.items():
        if key in spec:
            errs.append(f"{where}.{key}: {why}")
    extra = sorted(set(spec) - set(SPEC_KEYS))
    if extra:
        errs.append(f"{where} has unknown keys {extra} (ExperimentSpec is extra=forbid)")
    missing = [k for k in PROPOSAL_SPEC_REQUIRED if spec.get(k) in (None, "", {})]
    if missing:
        errs.append(f"{where} missing {missing}")
    if spec.get("spec_version", PROPOSAL_SPEC_VERSION) not in (2, PROPOSAL_SPEC_VERSION):
        errs.append(
            f"{where}.spec_version must be {PROPOSAL_SPEC_VERSION} (D108: control runs as its "
            "own arm; a v2 draft is written as v3 at approval)"
        )
    if spec.get("kind", "ab") != "ab":
        errs.append(f"{where}.kind must be 'ab' (the owner runs A/A calibrations)")
    if spec.get("area") is not None and spec["area"] not in EXPERIMENT_AREAS:
        errs.append(f"{where}.area must be one of {EXPERIMENT_AREAS}")
    for key in ("title", "hypothesis", "backtest_ref"):
        if key in spec and not isinstance(spec[key], str):
            errs.append(f"{where}.{key} must be a string")
    margin = spec.get("non_inferiority_margin")
    if margin is not None and (not _is_num(margin) or margin <= 0):
        errs.append(f"{where}.non_inferiority_margin must be a number > 0")
    arms = spec.get("arms") or {}
    if not isinstance(arms, dict) or set(arms) - {"control", "treatments"}:
        errs.append(f"{where}.arms holds control and treatments only (v2; not `treatment`)")
        return errs
    control = arms.get("control") or {}
    if not isinstance(control, dict) or control.get("overlay") or set(control) - {"overlay"}:
        errs.append(f"{where}: the control arm is the production config (empty overlay)")
    ts = arms.get("treatments")
    if not isinstance(ts, dict) or sorted(ts) != sorted(TREATMENT_NAMES):
        got = sorted(ts) if isinstance(ts, dict) else ts
        errs.append(
            f"THREE-TREATMENTS: {where}.arms.treatments must be exactly "
            f"{', '.join(TREATMENT_NAMES)} (D108: control + {K_TREATMENTS}); got {got}"
        )
        return errs
    leafsets: dict[str, list[str]] = {}
    seen: dict[str, str] = {}
    for name in TREATMENT_NAMES:
        arm = ts[name]
        overlay = arm.get("overlay") if isinstance(arm, dict) else None
        if not isinstance(arm, dict) or set(arm) - {"overlay"}:
            errs.append(f"{where}.arms.treatments.{name} holds only an overlay")
        if not isinstance(overlay, dict) or not overlay:
            errs.append(f"{where}.arms.treatments.{name} needs an overlay (what changes)")
            continue
        bad = sorted(set(overlay) - set(OVERLAY_TARGETS))
        if bad:
            errs.append(f"{where}.arms.treatments.{name} targets {bad} not in {OVERLAY_TARGETS}")
        empty = sorted(k for k, v in overlay.items() if not v)
        if empty:
            errs.append(f"{where}.arms.treatments.{name} target(s) {empty} are empty; drop them")
        leafsets[name] = sorted(".".join(x) for x in _leaves(overlay))
        canon = json.dumps(overlay, sort_keys=True)
        if canon in seen:
            errs.append(f"{where}.arms.treatments.{name} is identical to {seen[canon]}")
        seen.setdefault(canon, name)
    if len(leafsets) == K_TREATMENTS and len({tuple(v) for v in leafsets.values()}) > 1:
        detail = "; ".join(f"{n}: {', '.join(v)}" for n, v in leafsets.items())
        errs.append(
            f"SAME-LEAVES: {where} treatments set different leaf sets ({detail}); every arm "
            "must set the same leaves (one variable at 3 levels, or 3 alternatives of one "
            "mechanism)"
        )
    return errs


def proposal_value(p: dict) -> float | None:
    """``expected_effect x affected_share x p_success / sessions_needed`` (D86), or None."""
    eff = (p.get("expected_effect") or {}).get("pct_per_day")
    share, prob, sess = p.get("affected_share"), p.get("p_success"), p.get("sessions_needed")
    if not all(_is_num(v) for v in (eff, share, prob, sess)) or sess <= 0:
        return None
    return eff * share * prob / sess


def effective_score(entry: dict) -> float:
    o = entry.get("score_override") or {}
    if _is_num(o.get("value")):
        return float(o["value"])
    v = entry.get("value_score")
    return float(v) if _is_num(v) else 0.0


def value_formula(entry: dict) -> str:
    """The inputs and the formula, printed for every proposal (D86)."""
    eff = (entry.get("expected_effect") or {}).get("pct_per_day")
    line = (
        f"value {_num(entry.get('value_score'), '.6f')} = {_num(eff, '+.3f')}%/day x share "
        f"{_num(entry.get('affected_share'), '.2f')} x p {_num(entry.get('p_success'), '.2f')} "
        + (
            f"/ {entry.get('nights_needed')} compute nights"
            if is_study(entry)
            else f"/ {entry.get('sessions_needed')} sessions"
        )
    )
    o = entry.get("score_override") or {}
    if _is_num(o.get("value")):
        line += f" | OVERRIDE {o['value']} ({o.get('reason', '')})"
    if entry.get("pinned"):
        line += f" | PINNED by owner ({entry['pinned'].get('reason', '')})"
    return line


def validate_proposal(
    f: dict,
    i: int,
    backlog: dict,
    seed_ids: frozenset = frozenset(),
    studies: list | None = None,
) -> list[str]:
    """An ``xp-proposal`` finding: the spec plus the D86 value inputs. *studies* is the
    E7.15 study snapshot (``metrics.json`` ``backtest.studies``) BT-<n> refs resolve in."""
    studies = studies or []
    where = f"findings[{i}] proposal"
    p = f.get("proposal")
    if not isinstance(p, dict):
        return [
            f"findings[{i}] xp-proposal needs a `proposal` object ({', '.join(PROPOSAL_FIELDS)})"
        ]
    errs = []
    extra = sorted(
        set(p)
        - {*PROPOSAL_FIELDS, "evidence_kind", "evidence_ref", "score_override", "seed_ref"}
        - {"evidence"}
    )
    if extra:
        errs.append(f"{where} has unknown keys {extra}")
    errs += validate_proposal_spec(p.get("spec"), f"{where}.spec")
    kind = p.get("evidence_kind")
    n = f.get("n")
    if kind not in PROPOSAL_EVIDENCE:
        errs.append(f"{where}.evidence_kind must be one of {PROPOSAL_EVIDENCE}")
    elif kind == "live" and isinstance(n, int) and n < MIN_SAMPLE:
        errs.append(
            f"findings[{i}] MIN-SAMPLE: a live-evidence proposal needs >= {MIN_SAMPLE} closed "
            f"trades in its bucket (n={n}); cite a BT-<n> holdout study (E7.15) or wait"
        )
    elif kind == "backtest":
        ref = str(p.get("evidence_ref") or "")
        if not BACKTEST_ID_RE.match(ref):
            errs.append(f"{where}.evidence_ref must be a BT-<n> study id for backtest evidence")
        elif (p.get("spec") or {}).get("backtest_ref") != ref:
            errs.append(f"{where}.spec.backtest_ref must equal evidence_ref ({ref})")
        else:  # option A (D86): only a passing C500 holdout of the same variable
            errs += [f"findings[{i}] {e}" for e in check_bt_evidence(ref, p.get("spec"), studies)]
    bref = (p.get("spec") or {}).get("backtest_ref")
    if kind != "backtest" and isinstance(bref, str) and bref.strip():
        errs += _check_spec_backtest_ref(bref.strip(), f, i, backlog, studies)
    if not str(p.get("evidence", "")).strip():
        errs.append(f"{where}.evidence must say what backs it (bucket + numbers, or BT-<n>)")
    eff = p.get("expected_effect")
    if (
        not isinstance(eff, dict)
        or not _is_num(eff.get("pct_per_day"))
        or eff["pct_per_day"] <= 0
        or not str(eff.get("source", "")).strip()
    ):
        errs.append(f"{where}.expected_effect needs pct_per_day > 0 (%/day) and its source")
    for key in ("affected_share", "p_success"):
        v = p.get(key)
        if not _is_num(v) or not 0 < v <= 1:
            errs.append(f"{where}.{key} must be a number in (0, 1]")
    s = p.get("sessions_needed")
    if not isinstance(s, int) or isinstance(s, bool) or s < 1:
        errs.append(f"{where}.sessions_needed must be an int >= 1 (read it off the K=3 plan grid)")
    want = proposal_value(p)
    got = p.get("value_score")
    if want is not None and (not _is_num(got) or abs(got - want) > max(1e-9, abs(want) * 0.01)):
        errs.append(
            f"{where}.value_score {got!r} != expected_effect x affected_share x p_success / "
            f"sessions_needed = {want:.6g}; set an override with score_override "
            "{value, reason} instead"
        )
    o = p.get("score_override")
    if o is not None and (
        not isinstance(o, dict)
        or not _is_num(o.get("value"))
        or not str(o.get("reason", "")).strip()
    ):
        errs.append(f"{where}.score_override needs value and reason")
    sref = p.get("seed_ref")
    if sref is not None and sref not in seed_ids:
        errs.append(f"{where}.seed_ref {sref!r} is not a lead in the idea seed")
    it = (backlog.get("items") or {}).get(f.get("key"))
    if (
        it
        and it["status"] not in ("proposed",)
        and not it.get("needs_rewrite")  # D108: a K=4 entry is rewritten, then re-approved
        and it.get("spec") != p.get("spec")
    ):
        errs.append(
            f"findings[{i}] {it['id']} is {it['status']}: its spec is frozen (the owner "
            "approved it); propose a new key for a changed design"
        )
    if it and it["status"] == "dropped":
        errs.append(f"findings[{i}] {it['id']} was dropped; propose under a new key")
    return errs


def validate_quick_change(f: dict, i: int) -> list[str]:
    """A ``quick-change`` finding: ship now, no XP; the next run reviews it (D86)."""
    where = f"findings[{i}] quick_change"
    q = f.get("quick_change")
    if not isinstance(q, dict):
        return [
            f"findings[{i}] quick-change needs a `quick_change` object "
            f"({', '.join(QC_FIELDS)}, watch)"
        ]
    errs = []
    missing = [k for k in QC_FIELDS if not str(q.get(k, "")).strip()]
    if missing:
        errs.append(f"{where} missing {missing}")
    if q.get("kind") not in QC_KINDS:
        errs.append(f"{where}.kind must be one of {QC_KINDS}")
    ev = q.get("evidence_kind")
    n = f.get("n")
    if ev not in QC_EVIDENCE:
        errs.append(
            f"{where}.evidence_kind must be one of {QC_EVIDENCE} (a backtest alone never "
            "backs a quick change, D86)"
        )
    elif ev == "live" and isinstance(n, int) and n < MIN_SAMPLE:
        errs.append(
            f"findings[{i}] MIN-SAMPLE: a live-evidence quick change needs >= {MIN_SAMPLE} "
            f"closed trades in its bucket (n={n}); report it as a theme status or data-gap"
        )
    elif ev == "xp-winner" and not EXPERIMENT_ID_RE.match(str(q.get("evidence_ref") or "")):
        errs.append(f"{where}.evidence_ref must name the winning XP-<n> for an xp-winner promotion")
    apply = str(q.get("apply", ""))
    if q.get("kind") == "config" and not apply.startswith(QC_CONFIG_PREFIXES):
        errs.append(
            f"{where}.apply for a runtime-tunable key is the Slack command "
            "`!arc config set <key> <value> -- <reason>`"
        )
    if q.get("kind") == "code":
        draft = f.get("draft_card") or {}
        if f.get("action") != "new-card" or not (draft.get("title") and draft.get("body")):
            errs.append(
                f"findings[{i}] a code/YAML quick change is action new-card with draft_card "
                "title and body (shipped with `create A-<n>`)"
            )
        elif QC_PR_LINE not in str(draft.get("body")):
            errs.append(
                f"findings[{i}] draft_card.body must ask for the PR line `{QC_PR_LINE} A-<n>`"
            )
    watch = q.get("watch")
    if (
        not isinstance(watch, dict)
        or not str(watch.get("metric", "")).strip()
        or not isinstance(watch.get("n"), int)
        or isinstance(watch.get("n"), bool)
        or watch["n"] < 1
    ):
        errs.append(f"{where}.watch needs metric and n (what the next run checks, and at what n)")
    return errs


def slot_check(area: str, overview: list[dict], max_arms: int) -> list[str]:
    """Why a K=3 experiment in *area* cannot start now (empty = a slot is free).

    One registered/running/queued experiment per area holds it (the registry's queue rule),
    and its control + 3 treatment arms (D108: control is its own arm on the experiment
    account) must fit under ``max_parallel_arms`` with the arms already registered/running
    (D69).
    """
    out = []
    holders = [
        e["experiment_id"]
        for e in overview
        if e["area"] == area and e["status"] in ("registered", "running", "queued")
    ]
    if holders:
        out.append(f"area {area} held by {', '.join(holders)}")
    busy = [e for e in overview if e["status"] in ("registered", "running")]
    used = sum(int(e.get("arms") or 1) for e in busy)
    if used + ARMS_PER_XP > max_arms:
        ids = ", ".join(e["experiment_id"] for e in busy)
        out.append(
            f"arms {used} + {ARMS_PER_XP} (control + {K_TREATMENTS}) > max_parallel_arms "
            f"{max_arms} ({ids})"
        )
    return out


def blockers_of(area: str, overview: list[dict], max_arms: int) -> list[str]:
    """The XP ids whose end frees the slot a K=3 experiment in *area* waits for."""
    ids = [
        e["experiment_id"]
        for e in overview
        if e["area"] == area and e["status"] in ("registered", "running", "queued")
    ]
    busy = [e for e in overview if e["status"] in ("registered", "running")]
    if sum(int(e.get("arms") or 1) for e in busy) + ARMS_PER_XP > max_arms:
        ids += [e["experiment_id"] for e in busy]
    return sorted(set(ids))


# ---------- idea seed (E21.2, D86) ----------


def parse_seed_text(text: str):
    """The seed document: drop full-line ``#`` comments, parse the rest as JSON."""
    body = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    return json.loads(body)


def validate_seed(doc) -> list[str]:
    """Schema of ``idea_seed.yaml`` (extra keys refused, like a pydantic ``extra=forbid``).

    Leads only: each names an area, its variable(s) as dotted ``<stem>.<path>`` leaves and
    1-4 treatment overlays in the ``config/experiments/live`` format (stems in
    ``OVERLAY_TARGETS``), each setting only leaves listed in ``variables``.
    """
    if not isinstance(doc, dict):
        return ["idea seed must be an object with seed_version and ideas"]
    errs = []
    extra = sorted(set(doc) - set(SEED_KEYS))
    if extra:
        errs.append(f"idea seed has unknown keys {extra}")
    if doc.get("seed_version") != 1:
        errs.append("idea seed seed_version must be 1")
    ideas = doc.get("ideas")
    if not isinstance(ideas, list):
        return [*errs, "idea seed ideas must be a list"]
    seen: set[str] = set()
    for i, idea in enumerate(ideas):
        where = f"ideas[{i}]"
        if not isinstance(idea, dict):
            errs.append(f"{where} must be an object")
            continue
        where = f"{where} ({idea.get('id', '?')})"
        extra = sorted(set(idea) - set(IDEA_KEYS))
        if extra:
            errs.append(f"{where} has unknown keys {extra}")
        missing = [k for k in IDEA_REQUIRED if idea.get(k) in (None, "", [], {})]
        if missing:
            errs.append(f"{where} missing {missing}")
        iid = str(idea.get("id", ""))
        if not IDEA_ID_RE.match(iid):
            errs.append(f"{where}.id must look like S-<n> (a lead, not an XP id)")
        elif iid in seen:
            errs.append(f"{where}.id is a duplicate")
        seen.add(iid)
        if idea.get("area") is not None and idea["area"] not in EXPERIMENT_AREAS:
            errs.append(f"{where}.area must be one of {EXPERIMENT_AREAS}")
        for key in ("title", "hypothesis", "source", "notes", "retired_spec"):
            if key in idea and not isinstance(idea[key], str):
                errs.append(f"{where}.{key} must be a string")
        refs = idea.get("evidence_refs", [])
        if not isinstance(refs, list) or not all(isinstance(r, str) and r for r in refs):
            errs.append(f"{where}.evidence_refs must be a list of non-empty strings")
        variables = idea.get("variables") or []
        if not isinstance(variables, list) or not all(isinstance(v, str) for v in variables):
            errs.append(f"{where}.variables must be a list of dotted leaves")
            variables = []
        for v in variables:
            if v.split(".", 1)[0] not in OVERLAY_TARGETS or "." not in v:
                errs.append(f"{where}.variables {v!r} must be <stem>.<path> ({OVERLAY_TARGETS})")
        treatments = idea.get("treatments") or {}
        if not isinstance(treatments, dict):
            errs.append(f"{where}.treatments must map a label to an overlay")
            continue
        if len(treatments) > MAX_SEED_TREATMENTS:
            errs.append(f"{where}: {len(treatments)} treatments, at most {MAX_SEED_TREATMENTS}")
        for label, overlay in treatments.items():
            if not isinstance(overlay, dict) or not overlay:
                errs.append(f"{where}.treatments.{label} must be a non-empty overlay")
                continue
            bad = sorted(set(overlay) - set(OVERLAY_TARGETS))
            if bad:
                errs.append(f"{where}.treatments.{label} targets {bad} not in {OVERLAY_TARGETS}")
            for leaf in _leaves(overlay):
                dotted = ".".join(leaf)
                if dotted not in variables:
                    errs.append(f"{where}.treatments.{label} sets {dotted}, not in variables")
    return errs


def load_seed(path: Path) -> tuple[list[dict], list[str]]:
    """``(ideas, errors)``; a missing file is no seed (``[]``), never an error."""
    if not path.exists():
        return [], []
    try:
        doc = parse_seed_text(path.read_text())
    except ValueError as e:
        return [], [f"{path.name} is not valid JSON-after-comments: {e}"]
    errs = validate_seed(doc)
    return ([] if errs else list(doc["ideas"])), errs


def seed_lines(ideas: list[dict], errors: list[str], disposed: dict | None = None) -> list[str]:
    """The idea-seed leads still open: neither turned into a proposal nor dropped (D86)."""
    if errors:
        return [f"- idea seed INVALID (fix hermes/analyst/{IDEA_SEED_FILE}): {e}" for e in errors]
    disposed = disposed or {}
    open_ideas = [x for x in ideas if x["id"] not in disposed]
    out = []
    for x in open_ideas:
        arms = ", ".join(sorted(x["treatments"]))
        refs = "; ".join(x.get("evidence_refs") or []) or "none"
        out.append(
            f"- {x['id']} [{x['area']}] {x['title']}: vars {', '.join(x['variables'])}; "
            f"treatments {arms}; source {x['source']}; evidence {refs}"
        )
    done = [f"{sid} {d.get('status')}" for sid, d in sorted(disposed.items())]
    if done:
        out.append(f"- (disposed: {', '.join(done)})")
    return out or ["- none"]


# ---------- XP backlog (E21.3, D86) ----------


def empty_backlog() -> dict:
    return {"version": BACKLOG_VERSION, "next_id": 1, "items": {}, "seed": {}}


def _id_num(xid: str) -> int:
    """The number of an ``XP-<n>`` / ``P-<n>`` id (sort key; 0 for anything else)."""
    head, _, num = str(xid).partition("-")
    return int(num) if head in ("XP", "P") and num.isdigit() else 0


def backlog_numbers(backlog: dict) -> set[int]:
    """Every XP number the backlog holds or ever held: entry ids (dropped ones too) and
    ids an entry gave up on a re-reservation. A reserved number is never reused (D121)."""
    out = set()
    for e in backlog["items"].values():
        for xid in [e.get("id"), *(e.get("previous_ids") or [])]:
            if PROPOSAL_ID_RE.match(str(xid or "")):
                out.add(_id_num(xid))
    return out


def reserve_proposal_id(backlog: dict, used=()) -> str:
    """D121: the next free ``XP-<n>``: above every number in *used* (registry DB,
    ``config/experiments/live/``, open ``xp/<n>`` spec PRs), the backlog's own ids and its
    ``next_id`` high-water mark. Advances ``next_id``, so a dropped proposal leaves a gap."""
    n = max({*used, *backlog_numbers(backlog), int(backlog.get("next_id") or 1) - 1}) + 1
    backlog["next_id"] = n + 1
    return f"XP-{n}"


def migrate_backlog(backlog: dict, used=(), when: str = "") -> list[str]:
    """v1 -> v2 (D121): every ``P-<n>`` entry gets an ``XP-<n>`` and keeps ``legacy_id``.
    An entry already adopted keeps its registry id; the rest reserve the next free number
    in ``P-<n>`` order. Seed and ``next_experiment`` references follow. Idempotent: a v2
    backlog is left alone. Returns ``P-<n> -> XP-<m>`` lines."""
    if int(backlog.get("version") or 1) >= BACKLOG_VERSION:
        return []
    items = sorted(backlog["items"].values(), key=lambda e: _id_num(e.get("id", "")))
    mapping: dict[str, str] = {}
    for e in items:  # adopted entries first: their number is fixed by the registry
        old = str(e.get("id") or "")
        if LEGACY_PROPOSAL_ID_RE.match(old) and PROPOSAL_ID_RE.match(
            str(e.get("experiment_id") or "")
        ):
            mapping[old] = e["experiment_id"]
    taken = {*used, *(_id_num(x) for x in mapping.values())}
    backlog["next_id"] = max([*taken, 0]) + 1
    for e in items:
        old = str(e.get("id") or "")
        if LEGACY_PROPOSAL_ID_RE.match(old) and old not in mapping:
            mapping[old] = reserve_proposal_id(backlog, taken)
    out = []
    for e in items:
        old = str(e.get("id") or "")
        if old in mapping:
            e["legacy_id"], e["id"] = old, mapping[old]
            _hist(e, when, "migrated", legacy_id=old, id=e["id"], by="D121")
            out.append(f"{old} -> {e['id']}")
    for s in (backlog.get("seed") or {}).values():
        if isinstance(s, dict) and s.get("proposal") in mapping:
            s["proposal"] = mapping[s["proposal"]]
    nxt = backlog.get("next_experiment")
    if isinstance(nxt, dict) and str(nxt.get("value") or "") in mapping:
        nxt["value"] = mapping[nxt["value"]]
    backlog["version"] = BACKLOG_VERSION
    return out


def load_backlog(path: Path, used=(), when: str = "") -> dict:
    """The backlog, migrated to v2 (D121) in memory; *used* = the XP numbers taken outside
    it (:func:`used_xp_numbers`), so a migrated entry never takes a registered id."""
    data = load_json(path, None)
    if not isinstance(data, dict):
        return empty_backlog()
    for k, v in empty_backlog().items():
        if k != "version":
            data.setdefault(k, v)
    data.setdefault("version", 1)
    migrate_backlog(data, used, when)
    return data


def spec_numbers(repo: Path) -> set[int]:
    """XP numbers in ``config/experiments/live/`` (specs, verdicts, history): file names
    (``xp<n>_….yaml``, ``XP-<n>.yaml``) and their ``id:`` / ``experiment_id:`` lines."""
    out: set[int] = set()
    base = repo / "config" / "experiments" / "live"
    for d in (base, base / "verdicts", base / "history"):
        for f in sorted(d.glob("*")) if d.is_dir() else ():
            if not f.is_file() or f.suffix not in (".yaml", ".yml", ".json"):
                continue
            m = re.match(r"^(?:xp|XP-)([1-9]\d*)(?:[_.]|$)", f.name)
            if m:
                out.add(int(m.group(1)))
            for m in re.finditer(
                r'^\s*"?(?:id|experiment_id)"?\s*:\s*"?XP-([1-9]\d*)\b', f.read_text(), re.M
            ):
                out.add(int(m.group(1)))
    return out


def spec_pr_numbers(p: Paths) -> set[int]:
    """XP numbers of open spec PRs (branch ``xp/<n>``, adopt's publisher). Best effort:
    no ``gh``, no network or no JSON gives ``set()``."""
    return _open_pr_numbers(p, SPEC_PR_BRANCH_RE)


def _open_pr_numbers(p: Paths, branch_re: re.Pattern) -> set[int]:
    """Numbers in the head branches of open PRs matching *branch_re* (group 1)."""
    if not p.gh:
        return set()
    try:
        r = subprocess.run(
            [*p.gh, "pr", "list", "--state", "open", "--limit", "200", "--json", "headRefName"],
            cwd=p.repo,
            env=clean_env(),  # read-only `gh` (keychain auth), like merged_pr_lines
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        prs = json.loads(r.stdout) if r.returncode == 0 else []
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return set()
    out = set()
    for pr in prs if isinstance(prs, list) else []:
        m = branch_re.match(str((pr or {}).get("headRefName") or ""))
        if m:
            out.add(int(m.group(1)))
    return out


def registry_numbers(db: Path) -> set[int]:
    """XP numbers in the registry (``experiments``) of *db*, opened ``mode=ro``; ``set()``
    for a missing store or one from before E10."""
    if not db.is_file():
        return set()
    try:
        conn = sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True)
        try:
            if not _has_table(conn, "experiments"):
                return set()
            ids = [r[0] for r in conn.execute("SELECT DISTINCT experiment_id FROM experiments")]
        finally:
            conn.close()
    except sqlite3.Error:
        return set()
    return {_id_num(x) for x in ids if EXPERIMENT_ID_RE.match(str(x))}


def used_xp_numbers(p: Paths, registry_ids=None) -> set[int]:
    """D121: the XP numbers taken outside the backlog: the registry (*registry_ids*, the
    gate's snapshot; None = read the live store ``mode=ro``), the committed specs and the
    open ``xp/<n>`` spec PRs."""
    if registry_ids is None:
        reg = registry_numbers(p.live_db)
    else:
        reg = {_id_num(x) for x in registry_ids if EXPERIMENT_ID_RE.match(str(x))}
    return reg | spec_numbers(p.repo) | spec_pr_numbers(p)


def _hist(entry: dict, when: str, event: str, **extra) -> None:
    entry.setdefault("history", []).append({"at": when, "event": event, **extra})


def backlog_by_id(backlog: dict, pid: str) -> dict | None:
    """The XP entry holding *pid* (``kind: bt`` studies: :func:`bt_by_id`)."""
    return next(
        (e for e in backlog["items"].values() if e.get("id") == pid and not is_study(e)), None
    )


def bt_by_id(backlog: dict, sid: str) -> dict | None:
    """The ``kind: bt`` entry holding *sid* (E26.2)."""
    return next((e for e in backlog["items"].values() if is_bt(e) and e.get("id") == sid), None)


def backlog_by_ref(backlog: dict, ref: str) -> dict | None:
    """The entry a registry ``proposal_ref`` names: its ``XP-<n>``, or the ``legacy_id``
    ``P-<n>`` an old event row still carries (read alias, D121)."""
    return backlog_by_id(backlog, ref) or next(
        (e for e in backlog["items"].values() if ref and e.get("legacy_id") == ref), None
    )


def rereserve_collisions(backlog: dict, used, when: str) -> list[str]:
    """D121: a proposed/approved entry whose reserved number got taken outside the backlog
    (an owner-made spec, a hand ``create``) moves to the next free ``XP-<n>``; the old id
    goes to ``previous_ids`` (never reused). An approval does not carry over: the owner
    approved the old id. Returns ``XP-<a> -> XP-<b>`` lines."""
    out = []
    taken = set(used)
    for e in sorted(backlog["items"].values(), key=lambda e: _id_num(e["id"])):
        if e.get("status") not in BACKLOG_RANKED or e.get("experiment_id") == e["id"]:
            continue
        if is_study(e):
            continue
        if _id_num(e["id"]) not in taken:
            continue
        old = e["id"]
        e.setdefault("previous_ids", []).append(old)
        e["id"] = reserve_proposal_id(backlog, taken)
        if e["status"] == "approved":
            e["status"] = "proposed"
        _hist(e, when, "re-reserved", previous_id=old, id=e["id"], status=e["status"])
        out.append(f"{old} -> {e['id']} (its number was taken outside the backlog)")
    return out


def legacy_redirect(backlog: dict, pid: str) -> str | None:
    """``approve|drop|reprioritise P-<n>``: the reply naming the XP id to use (D121), or
    None when *pid* is not a retired ``P-<n>``."""
    if not LEGACY_PROPOSAL_ID_RE.match(pid or ""):
        return None
    entry = backlog_by_ref(backlog, pid)
    if entry is None:
        return f"P-ids are retired (D121): use XP-<n>; no proposal was {pid}"
    return f"P-ids are retired (D121): use {entry['id']} ({pid} is now {entry['id']})"


def ranked_backlog(
    backlog: dict, statuses: tuple = BACKLOG_RANKED, kind: str | None = "xp"
) -> list[dict]:
    """Open proposals in order: owner pins first (newest pin first), then effective score.
    An entry flagged ``needs_rewrite`` (D108: a K=4 spec) is not ranked until rewritten.
    *kind*: ``xp`` (default: what next_experiment / slots read), ``bt`` (E26.2 studies,
    what next_study reads) or None (the one mixed ranking, D121)."""
    items = [
        e
        for e in backlog["items"].values()
        if e.get("status") in statuses
        and not e.get("needs_rewrite")
        and (kind is None or entry_kind(e) == kind)
    ]
    return sorted(
        items,
        key=lambda e: (
            0 if e.get("pinned") else 1,
            "" if not e.get("pinned") else _neg_text(e["pinned"].get("at", "")),
            -effective_score(e),
            _id_num(e["id"]),
        ),
    )


def _neg_text(s: str) -> str:
    """Sort key that orders ISO timestamps newest first."""
    return "".join(chr(0x10FFFF - ord(c)) for c in s)


def _spec_treatments(spec) -> list[str]:
    arms = (spec or {}).get("arms") if isinstance(spec, dict) else None
    ts = arms.get("treatments") if isinstance(arms, dict) else None
    return sorted(ts) if isinstance(ts, dict) else []


def flag_needs_rewrite(backlog: dict, when: str) -> list[str]:
    """D108: a proposed/approved entry whose spec is not control + exactly ``t1..t3`` (e.g. a
    D86 four-treatment draft) is flagged ``needs_rewrite``, never silently trimmed: it leaves
    the ranking, cannot be ``next_experiment`` or approved, and the Analyst re-proposes the
    same key with three treatments (which clears the flag). Returns change lines."""
    out = []
    for e in sorted(backlog["items"].values(), key=lambda e: _id_num(e["id"])):
        if e.get("status") not in BACKLOG_RANKED or e.get("needs_rewrite") or is_study(e):
            continue
        got = _spec_treatments(e.get("spec"))
        if got == sorted(TREATMENT_NAMES):
            continue
        reason = (
            f"{len(got)} treatment(s) ({', '.join(got) or 'none'}); D108 proposals are "
            f"control + exactly {K_TREATMENTS} ({', '.join(TREATMENT_NAMES)}): re-propose "
            f"key {e.get('key')} with {K_TREATMENTS} treatments"
        )
        e["needs_rewrite"] = {"at": when, "reason": reason}
        _hist(e, when, "needs_rewrite", reason=reason)
        out.append(f"{e['id']}: needs_rewrite ({reason})")
    return out


def sync_backlog(backlog: dict, overview: list[dict], when: str) -> list[str]:
    """Statuses from the registry: the experiment whose events name ``proposal_ref`` (its
    ``XP-<n>``, or a legacy ``P-<n>`` alias, D121) moves its entry to registered / running /
    concluded (E21.4). Returns change lines."""
    status_map = {
        "draft": "approved",
        "queued": "registered",
        "registered": "registered",
        "running": "running",
        "stopped": "concluded",
        "promoted": "concluded",
        "rejected": "concluded",
    }
    out = []
    # several experiments can cite one proposal (a stopped run + its re-run): the most live wins
    live_rank = {"running": 0, "registered": 1, "queued": 2, "draft": 3}
    best: dict[str, tuple] = {}
    for i, e in enumerate(overview):
        ref = e.get("proposal_ref")
        if not ref:
            continue
        key = (live_rank.get(e["status"], 9), -i)
        if ref not in best or key < best[ref][0]:
            best[ref] = (key, e)
    for ref, (_, e) in sorted(best.items()):
        entry = backlog_by_ref(backlog, ref)
        if entry is None or entry.get("status") == "dropped":
            continue
        new = status_map.get(e["status"], entry["status"])
        changed = entry.get("experiment_id") != e["experiment_id"] or entry["status"] != new
        if changed:
            old = entry["status"]
            entry["status"] = new
            entry["experiment_id"] = e["experiment_id"]
            _hist(entry, when, "sync", status=new, experiment_id=e["experiment_id"])
            out.append(f"{entry['id']}: {old} -> {new} ({e['experiment_id']} {e['status']})")
    return out


def proposal_age(entry: dict, now: dt.datetime | None) -> str:
    """E26.4: ` | proposed <N>d ago (verdict run <run>)` for the weekly context; '' without
    *now* or a ``created`` stamp."""
    created = entry.get("created")
    if now is None or not created:
        return ""
    try:
        days = (now - dt.datetime.fromisoformat(created)).days
    except (TypeError, ValueError):
        return ""
    run = str(entry.get("created_run") or "")
    src = f" (verdict run {run})" if run.endswith(VERDICT_RUN_SUFFIX) else ""
    return f" | proposed {max(days, 0)}d ago{src}"


def backlog_lines(
    backlog: dict, overview: list[dict], max_arms: int, now: dt.datetime | None = None
) -> list[str]:
    """Context: every open proposal ranked, its formula and inputs, and slot readiness."""
    ranked = ranked_backlog(backlog)
    live = [e for e in backlog["items"].values() if e.get("status") in ("registered", "running")]
    rewrite = [
        e
        for e in backlog["items"].values()
        if e.get("status") in BACKLOG_RANKED and e.get("needs_rewrite")
    ]
    out = study_backlog_lines(backlog, now)
    if not ranked and not live and not rewrite:
        return [
            "- empty (no XP proposal yet: propose from the seed leads or live buckets n >= 20)",
            *out,
        ]
    out = [*mixed_rank_lines(backlog), *out]
    for rank, e in enumerate(ranked, 1):
        spec = e.get("spec") or {}
        why = slot_check(spec.get("area", "other"), overview, max_arms)
        slot = "slot free now" if not why else "waits: " + "; ".join(why)
        out.append(
            f"- #{rank} {e['id']} [{e['status']}] {spec.get('area')}: {spec.get('title')} "
            f"(key {e['key']}) | evidence {e.get('evidence_kind')}"
            f"{' ' + e['evidence_ref'] if e.get('evidence_ref') else ''} n={e.get('n')} | {slot}"
            f"{proposal_age(e, now)}"
        )
        out.append(f"  {value_formula(e)}")
        for name in TREATMENT_NAMES:
            ov = (((spec.get("arms") or {}).get("treatments") or {}).get(name) or {}).get("overlay")
            out.append(f"  {name}: {json.dumps(ov, sort_keys=True)}")
    for e in live:
        if is_bt(e):
            continue
        out.append(f"- {e['id']} [{e['status']}] -> {e.get('experiment_id')} (in the registry)")
    for e in sorted(rewrite, key=lambda e: _id_num(e["id"])):
        spec = e.get("spec") or {}
        out.append(
            f"- {e['id']} [{e['status']}, NEEDS REWRITE, not ranked] {spec.get('area')}: "
            f"{spec.get('title')} (key {e['key']}): {e['needs_rewrite'].get('reason')}"
        )
    return out


def mixed_rank_lines(backlog: dict) -> list[str]:
    """D121: XP and BT proposals in ONE ranking by value (cost = sessions for XP, compute
    nights for BT). next_experiment reads the XP order, next_study the BT order."""
    mixed = ranked_backlog(backlog, kind=None)
    if not any(is_bt(e) for e in mixed):
        return []
    return [
        "- one ranking, XP + BT (D121): "
        + ", ".join(f"#{i} {e['id']} ({effective_score(e):.6f})" for i, e in enumerate(mixed, 1))
    ]


def study_backlog_lines(backlog: dict, now: dt.datetime | None = None) -> list[str]:
    """The ``kind: bt`` entries (E26.2): ranked proposals with their formula, then the ones
    the owner approved (queue status), stopped or dropped this far."""
    ranked = ranked_backlog(backlog, kind="bt")
    out = []
    for rank, e in enumerate(ranked, 1):
        out.append(
            f"- BT #{rank} {e['id']} [{e['status']}] {e.get('title')} (key {e['key']}) | "
            f"draft {e.get('draft_path') or '-'}{proposal_age(e, now)}"
        )
        out.append(f"  {value_formula(e)}")
    done = sorted(
        (e for e in backlog["items"].values() if is_bt(e) and e["status"] not in BACKLOG_RANKED),
        key=lambda e: _study_num(e["id"]),
    )
    for e in done:
        out.append(
            f"- {e['id']} [{e['status']}] {e.get('title')}"
            + (f" (approved {e['approved_at'][:10]})" if e.get("approved_at") else "")
            + (f": {e['stop_reason']}" if e.get("stop_reason") else "")
        )
    return out


def capacity_lines(overview: list[dict], max_arms: int) -> list[str]:
    busy = [e for e in overview if e["status"] in ("registered", "running")]
    used = sum(int(e.get("arms") or 1) for e in busy)
    held = ", ".join("%s=%s" % (e["experiment_id"], e.get("arms") or 1) for e in busy)
    out = [
        f"- max_parallel_arms {max_arms}: {used} arm(s) registered/running "
        f"({held or 'none'}); a K={K_TREATMENTS} experiment needs {ARMS_PER_XP} "
        f"(control + {K_TREATMENTS} treatments, D108)"
    ]
    for area in EXPERIMENT_AREAS:
        why = slot_check(area, overview, max_arms)
        out.append(f"- {area}: {'free' if not why else '; '.join(why)}")
    return out


def quick_change_items(ledger: dict, statuses: tuple = ("shipped",)) -> list[dict]:
    return sorted(
        (
            it
            for it in ledger.get("items", {}).values()
            if it.get("category") == "quick-change" and it.get("status") in statuses
        ),
        key=lambda it: int(it["id"][2:]),
    )


def promotion_leads(overview: list[dict]) -> list[str]:
    """E21.4 (D86): every experiment that stopped with a ``win`` and is not promoted yet is a
    quick-change lead "promote XP-<n> winner t<k>" (the owner's PR citing ``Experiment:``)."""
    out = []
    for e in overview:
        if e["status"] != "stopped" or e["reason"] != "win":
            continue
        payload = (e.get("report") or {}).get("payload") or {}
        wins = [
            pr.get("b")
            for pr in payload.get("pairwise") or []
            if pr.get("kind") == "control_vs_treatment" and pr.get("verdict") == "win"
        ]
        head = payload.get("headline_arm")
        winner = head if head in wins else (wins[0] if wins else "t1")
        out.append(
            f"- promote {e['experiment_id']} winner {winner} ({e['area']}: {e['title']}): a "
            f"quick-change lead; the PR cites `Experiment: {e['experiment_id']}` and sets "
            f"{winner}'s overlay values (verdicts/{e['experiment_id']}.yaml)"
        )
    return out or ["- none (no experiment concluded with a win and awaiting promotion)"]


def review_lines(ledger: dict) -> list[str]:
    """Quick changes shipped and not yet judged keep / roll back: the run must review each."""
    items = quick_change_items(ledger)
    if not items:
        return ["- none shipped since the last review"]
    out = []
    for it in items:
        q = it.get("quick_change") or {}
        w = q.get("watch") or {}
        out.append(
            f"- {it['id']} shipped {it.get('shipped_at', '?')}: {q.get('change')} | watch "
            f"{w.get('metric')} at n={w.get('n')} | rollback {q.get('rollback')} | "
            f"baseline n={it.get('n')}: {clip(str(it.get('evidence', '')), 200)}"
        )
    return out


def _validate_experiment_lines(
    doc: dict, backlog_after: dict, experiments: list[dict], max_arms: int
) -> list[str]:
    """The weekly "Experiments" section: one line per live experiment (stop advice for the
    running ones) + the next proposal for the next free slot."""
    errs = []
    lines = doc.get("experiments", [])
    if not isinstance(lines, list):
        return ["experiments must be a list of {experiment_id, status, note}"]
    by_id = {}
    for j, ln in enumerate(lines):
        eid = ln.get("experiment_id") if isinstance(ln, dict) else None
        if not eid:
            errs.append(f"experiments[{j}] needs experiment_id")
            continue
        if eid in by_id:
            errs.append(f"experiments: {eid} listed twice")
        by_id[eid] = ln
        if not str(ln.get("note", "")).strip():
            errs.append(f"experiments {eid} needs a note (what the data says and why)")
    known = {e["id"]: e for e in experiments}
    for eid, ln in by_id.items():
        if eid not in known:
            errs.append(f"experiments: {eid} is not in the experiment registry")
            continue
        if ln.get("status") != known[eid]["status"]:
            errs.append(
                f"experiments {eid} status {ln.get('status')!r} != registry "
                f"{known[eid]['status']!r}"
            )
        if known[eid]["status"] == "running":
            adv = ln.get("stop_advice")
            if not isinstance(adv, dict) or adv.get("advice") not in STOP_ADVICE:
                errs.append(
                    f"experiments {eid} is running: stop_advice.advice must be one of "
                    f"{STOP_ADVICE} (the owner stops; you advise)"
                )
            elif adv["advice"] != "continue" and not str(adv.get("why", "")).strip():
                errs.append(f"experiments {eid} stop_advice {adv['advice']} needs a why")
    for e in experiments:
        if e["status"] in REPORTABLE and e["id"] not in by_id:
            errs.append(f"experiment {e['id']} ({e['status']}) has no line in experiments")
    nxt = str(doc.get("next_experiment", "")).strip()
    ranked = ranked_backlog(backlog_after)
    if not nxt:
        errs.append(
            "next_experiment is required: an XP-<n> from the ranked backlog, or 'none: <why>'"
        )
        return errs
    if nxt.lower().startswith("none"):
        if not nxt.partition(":")[2].strip():
            errs.append("next_experiment 'none' needs a reason ('none: <why>')")
        return errs
    entry = backlog_by_id(backlog_after, nxt)
    if entry is not None and entry.get("needs_rewrite"):
        errs.append(
            f"next_experiment {nxt} needs a rewrite (D108: control + exactly {K_TREATMENTS} "
            f"treatments); re-propose its key first, or name another XP-<n>"
        )
        return errs
    if entry is None or entry.get("status") not in BACKLOG_RANKED:
        errs.append(
            f"next_experiment {nxt!r} must be a proposed/approved XP-<n> in the backlog "
            "(after this run's proposals and drops), or 'none: <why>'"
        )
        return errs
    if ranked and ranked[0]["id"] != nxt and not str(doc.get("next_reason", "")).strip():
        errs.append(
            f"next_experiment {nxt} is not the top-ranked proposal ({ranked[0]['id']}); "
            "say why in next_reason"
        )
    area = (entry.get("spec") or {}).get("area", "other")
    overview = [{**e, "experiment_id": e["id"]} for e in experiments]
    blocked = blockers_of(area, overview, max_arms)
    slot = str(doc.get("next_slot", "")).strip()
    if not slot:
        errs.append("next_slot is required: 'free now' or which experiment frees the slot and when")
    else:
        missing = [b for b in blocked if b not in slot]
        if missing:
            errs.append(
                f"next_slot must name what frees the slot for {nxt} ({area}): "
                f"{', '.join(missing)} (one experiment per area, max_parallel_arms {max_arms})"
            )
    return errs


def _validate_quick_lists(doc: dict, findings: list, ledger: dict) -> list[str]:
    """Both tracks every run (D86): quick changes or ``quick_changes_none``, and a review of
    every quick change shipped since the last run."""
    errs = []
    if (
        not any(f.get("category") == "quick-change" for f in findings)
        and not str(doc.get("quick_changes_none", "")).strip()
    ):
        errs.append(
            "no quick-change findings: say why in quick_changes_none (the report's "
            "*Quick changes* section reads `none: <why>`)"
        )
    shipped = {it["id"]: it for it in quick_change_items(ledger)}
    reviews = doc.get("quick_change_reviews", [])
    if not isinstance(reviews, list):
        return [*errs, "quick_change_reviews must be a list"]
    seen = set()
    for j, r in enumerate(reviews):
        rid = r.get("id") if isinstance(r, dict) else None
        if rid not in shipped:
            errs.append(f"quick_change_reviews[{j}] {rid!r} is not a shipped quick change")
            continue
        if rid in seen:
            errs.append(f"quick_change_reviews: {rid} reviewed twice")
        seen.add(rid)
        if r.get("verdict") not in QC_REVIEW:
            errs.append(f"quick_change_reviews {rid} verdict must be one of {QC_REVIEW}")
        n = r.get("n")
        if not isinstance(n, int) or isinstance(n, bool) or n < 0:
            errs.append(f"quick_change_reviews {rid} needs n (closed trades since it shipped)")
        if not str(r.get("note", "")).strip():
            errs.append(f"quick_change_reviews {rid} needs a note")
        if r.get("verdict") in ("keep", "roll-back") and not (
            str(r.get("before", "")).strip() and str(r.get("after", "")).strip()
        ):
            errs.append(
                f"quick_change_reviews {rid} {r.get('verdict')} needs before and after "
                "(the watch metric with n), or verdict too-early"
            )
    for rid in sorted(set(shipped) - seen, key=lambda x: int(x[2:])):
        errs.append(
            f"quick change {rid} shipped since it was proposed: review it in "
            "quick_change_reviews (keep / roll-back / too-early)"
        )
    return errs


def _validate_backlog_moves(
    doc: dict, findings: list, backlog: dict, seed_ids: frozenset
) -> list[str]:
    errs = []
    prop_keys = {f.get("key") for f in findings if f.get("category") == "xp-proposal"}
    for j, d in enumerate(doc.get("backlog_drops", []) or []):
        pid = d.get("id") if isinstance(d, dict) else None
        entry = backlog_by_id(backlog, pid) if pid else None
        if entry is None:
            errs.append(f"backlog_drops[{j}] {pid!r} is not in the backlog")
            continue
        if entry["status"] not in BACKLOG_RANKED:
            errs.append(
                f"backlog_drops[{j}] {pid} is {entry['status']}: only proposed/approved "
                "entries can be dropped (the owner stops registered experiments)"
            )
        if not str(d.get("reason", "")).strip():
            errs.append(f"backlog_drops[{j}] {pid} needs a reason")
        if entry["key"] in prop_keys:
            errs.append(f"backlog_drops[{j}] {pid} is also re-proposed this run")
    disposed = backlog.get("seed") or {}
    for j, d in enumerate(doc.get("seed_drops", []) or []):
        sid = d.get("seed_id") if isinstance(d, dict) else None
        if sid not in seed_ids:
            errs.append(f"seed_drops[{j}] {sid!r} is not a lead in the idea seed")
        elif sid in disposed:
            errs.append(f"seed_drops[{j}] {sid} is already {disposed[sid].get('status')}")
        if not str((d or {}).get("reason", "")).strip():
            errs.append(f"seed_drops[{j}] {sid} needs a reason")
    refs = [
        (f.get("proposal") or {}).get("seed_ref")
        for f in findings
        if f.get("category") == "xp-proposal" and isinstance(f.get("proposal"), dict)
    ]
    dropped = {(d or {}).get("seed_id") for d in doc.get("seed_drops", []) or []}
    for sid in refs:
        if sid and sid in dropped:
            errs.append(f"seed lead {sid} is both proposed and dropped this run")
        if sid and (disposed.get(sid) or {}).get("status") == "dropped":
            errs.append(f"seed lead {sid} was dropped earlier; propose it under a fresh design")
    return errs


def apply_backlog(
    backlog: dict,
    doc: dict,
    run: str,
    when: str,
    finding_ids: dict | None = None,
    used=(),
) -> dict:
    """Append/update ``XP-<n>`` entries from this run's xp-proposals, then drops. Idempotent:
    re-applying the same doc adds no entry and no history line. A new entry reserves the
    next free XP number above *used* (registry, specs, open spec PRs) and the backlog's own
    ids (D121, :func:`reserve_proposal_id`)."""
    finding_ids = finding_ids or {}
    items = backlog["items"]
    out: dict[str, list] = {"new": [], "updated": [], "dropped": [], "seed_dropped": []}
    for f in doc.get("findings", []):
        if f.get("category") != "xp-proposal":
            continue
        p = f["proposal"]
        fields = {
            "spec": p["spec"],
            "evidence": p["evidence"],
            "evidence_kind": p["evidence_kind"],
            "evidence_ref": p.get("evidence_ref"),
            "expected_effect": p["expected_effect"],
            "affected_share": p["affected_share"],
            "p_success": p["p_success"],
            "sessions_needed": p["sessions_needed"],
            "value_score": p["value_score"],
            "score_override": p.get("score_override"),
            "seed_ref": p.get("seed_ref"),
            "n": f.get("n"),
            "title": f.get("title"),
        }
        if f["key"] in finding_ids:
            fields["finding_id"] = finding_ids[f["key"]]
        entry = items.get(f["key"])
        if entry is None:
            entry = items[f["key"]] = {
                "id": reserve_proposal_id(backlog, used),
                "key": f["key"],
                "status": "proposed",
                "created": when,
                "created_run": run,
                "history": [],
                **fields,
            }
            _hist(entry, when, "proposed", run=run)
            out["new"].append(entry["id"])
        else:
            changed = sorted(k for k, v in fields.items() if entry.get(k) != v)
            if changed:
                entry.update(fields)
                _hist(entry, when, "edit", run=run, fields=changed)
                out["updated"].append(entry["id"])
            if entry.get("needs_rewrite") and _spec_treatments(p["spec"]) == sorted(
                TREATMENT_NAMES
            ):
                # D108: rewritten with K=3; an approval of the old K=4 spec does not carry over
                entry.pop("needs_rewrite")
                if entry["status"] == "approved":
                    entry["status"] = "proposed"
                _hist(entry, when, "rewritten", run=run, status=entry["status"])
                if entry["id"] not in out["updated"]:
                    out["updated"].append(entry["id"])
        if p.get("seed_ref"):
            backlog["seed"][p["seed_ref"]] = {"status": "proposed", "proposal": entry["id"]}
    for d in doc.get("backlog_drops", []) or []:
        entry = backlog_by_id(backlog, d["id"])
        if entry is not None and entry["status"] != "dropped":
            entry["status"] = "dropped"
            _hist(entry, when, "dropped", run=run, by="analyst", reason=d["reason"])
            out["dropped"].append(entry["id"])
    for d in doc.get("seed_drops", []) or []:
        if d["seed_id"] not in backlog["seed"]:
            backlog["seed"][d["seed_id"]] = {
                "status": "dropped",
                "reason": d["reason"],
                "at": when,
            }
            out["seed_dropped"].append(d["seed_id"])
    return out


# ---------- BT study proposals in the backlog (E26.2, D121) ----------


def is_bt(entry: dict) -> bool:
    return (entry or {}).get("kind") == "bt"


def is_ml(entry: dict) -> bool:
    """A ``kind: ml`` model-study entry (E27.8, D133)."""
    return (entry or {}).get("kind") == "ml"


def is_study(entry: dict) -> bool:
    """A compute study (BT or ML): ranked by compute nights, approved under the shared cap."""
    return (entry or {}).get("kind") in STUDY_KINDS


def entry_kind(entry: dict) -> str:
    """``xp`` | ``bt`` | ``ml`` (an entry without ``kind`` is an XP proposal)."""
    k = (entry or {}).get("kind")
    return k if k in STUDY_KINDS else "xp"


def bt_numbers(backlog: dict) -> set[int]:
    """Every BT number a ``kind: bt`` entry holds (dropped ones too: never reused)."""
    return {
        _study_num(e["id"])
        for e in backlog["items"].values()
        if is_bt(e) and BACKTEST_ID_RE.match(str(e.get("id") or ""))
    }


def study_value(v: dict) -> float | None:
    """``pct_per_day x affected_share x p_success / nights_needed`` (D121), or None."""
    vals = [v.get(k) for k in ("pct_per_day", "affected_share", "p_success", "nights_needed")]
    if not all(_is_num(x) for x in vals) or vals[3] <= 0:
        return None
    return vals[0] * vals[1] * vals[2] / vals[3]


def validate_study_value(v, where: str) -> list[str]:
    """A ``study_spec`` finding's ``study_value``: the D121 ranking inputs of the study."""
    if not isinstance(v, dict):
        return [
            f"{where} is required with a study_spec: {{pct_per_day, source, affected_share, "
            "p_success, nights_needed}} (D121: BT studies rank with XPs; cost = compute nights "
            "from the ## Backtest queue estimates)"
        ]
    errs = []
    extra = sorted(set(v) - set(STUDY_VALUE_KEYS))
    if extra:
        errs.append(f"{where} has unknown keys {extra}")
    if not _is_num(v.get("pct_per_day")):
        errs.append(f"{where}.pct_per_day must be a number (%/day net if the study wins)")
    if not str(v.get("source", "")).strip():
        errs.append(f"{where}.source is required (where the effect size comes from)")
    for k in ("affected_share", "p_success"):
        x = v.get(k)
        if not _is_num(x) or not 0 < x <= 1:
            errs.append(f"{where}.{k} must be in (0, 1]")
    nights = v.get("nights_needed")
    if not _is_num(nights) or nights <= 0:
        errs.append(f"{where}.nights_needed must be > 0 (compute nights, the queue's estimate)")
    return errs


def reserve_study_id(backlog: dict, floor: int) -> str:
    """The next free ``BT-<n>``: above *floor* - 1 (the gate's ``next_study_id``: ledger,
    specs, reports) and every BT number the backlog holds."""
    return f"BT-{max([floor - 1, *bt_numbers(backlog)]) + 1}"


def apply_study_backlog(
    backlog: dict, doc: dict, run: str, when: str, floor: int, finding_ids: dict | None = None
) -> list[tuple[dict, dict]]:
    """Every ``study_spec`` finding as a ``kind: bt`` entry (key ``bt:<finding key>``).
    A re-listed draft keeps its BT id while proposed (an edit updates it). Validation
    refuses a study_spec on an approved / queued / dropped key (frozen), so only new or
    proposed entries arrive here. Returns ``(finding, entry)`` in finding order: the drafts
    ``record`` writes."""
    finding_ids = finding_ids or {}
    items = backlog["items"]
    out = []
    for f in doc.get("findings", []):
        if "study_spec" not in f:
            continue
        key = BT_KEY_PREFIX + f["key"]
        v = f.get("study_value") or {}
        fields = {
            "study_spec": f["study_spec"],
            "title": f.get("title"),
            "hypothesis": f["study_spec"].get("hypothesis"),
            "expected_effect": {"pct_per_day": v.get("pct_per_day"), "source": v.get("source")},
            "affected_share": v.get("affected_share"),
            "p_success": v.get("p_success"),
            "nights_needed": v.get("nights_needed"),
            "value_score": study_value(v),
            "n": f.get("n"),
        }
        if f["key"] in finding_ids:
            fields["finding_id"] = finding_ids[f["key"]]
        entry = items.get(key)
        if entry is None:
            entry = items[key] = {
                "id": reserve_study_id(backlog, floor),
                "kind": "bt",
                "key": key,
                "finding_key": f["key"],
                "status": "proposed",
                "created": when,
                "created_run": run,
                "history": [],
                **fields,
            }
            _hist(entry, when, "proposed", run=run)
        elif entry["status"] == "proposed":
            changed = sorted(k for k, val in fields.items() if entry.get(k) != val)
            if changed:
                entry.update(fields)
                _hist(entry, when, "edit", run=run, fields=changed)
        else:
            continue  # approved (an adopt to retry): frozen, no new draft
        out.append((f, entry))
    return out


def validate_study_backlog(f: dict, i: int, backlog: dict) -> list[str]:
    """A ``study_spec`` on a key whose BT entry the owner already acted on is frozen."""
    entry = backlog["items"].get(BT_KEY_PREFIX + str(f.get("key")))
    if entry is None or entry.get("status") == "proposed":
        return []
    sid, st = entry["id"], entry["status"]
    if st == "dropped":
        return [
            f"findings[{i}] study_spec: {sid} (key {f.get('key')}) was dropped by the owner; "
            "re-propose only a new design under a new finding key"
        ]
    if st == "approved" and f.get("study_spec") == entry.get("study_spec"):
        return []
    return [
        f"findings[{i}] study_spec: {sid} is {st} (owner-approved, the spec is frozen); "
        "remove the study_spec from this finding (its progress is in ## Backtest queue)"
    ]


def study_floor(studies: list, next_id: str | None = None) -> int:
    """The lowest BT number a new draft may take: above every study in the gate's snapshot
    (ledger, committed specs, reports) and its ``next_study_id``."""
    nums = [_study_num(s["id"]) for s in studies or [] if BACKTEST_ID_RE.match(str(s.get("id")))]
    nxt = _study_num(next_id) if next_id and BACKTEST_ID_RE.match(next_id) else 1
    return max([nxt, *(n + 1 for n in nums)])


def _validate_next_study(doc: dict, backlog_after: dict) -> list[str]:
    """E26.2 (D121): ``next_study`` names the BT study the owner should approve next (the
    top-ranked proposed ``kind: bt`` entry, or another with ``next_study_reason``), or
    ``none: <why>``. ``next_experiment`` stays XP-only."""
    nxt = str(doc.get("next_study", "")).strip()
    if not nxt:
        return ["next_study is required: a BT-<n> from the ranked backlog, or 'none: <why>' (D121)"]
    if nxt.lower().startswith("none"):
        if not nxt.partition(":")[2].strip():
            return ["next_study 'none' needs a reason ('none: <why>')"]
        return []
    ranked = ranked_backlog(backlog_after, kind="bt")
    entry = next((e for e in ranked if e["id"] == nxt), None)
    if entry is None:
        return [
            f"next_study {nxt!r} must be a proposed BT-<n> in the backlog (a study_spec draft "
            "after this run), or 'none: <why>'"
        ]
    if ranked[0]["id"] != nxt and not str(doc.get("next_study_reason", "")).strip():
        return [
            f"next_study {nxt} is not the top-ranked study ({ranked[0]['id']}); say why in "
            "next_study_reason"
        ]
    return []


def load_bt_queue(path: Path) -> dict:
    """``data/backtest/queue.json`` (E26.2), read-only; empty when missing or unreadable."""
    try:
        data = json.loads(path.read_text()) if path.exists() else None
    except (OSError, ValueError):
        data = None
    return data if isinstance(data, dict) else {"entries": []}


def sync_bt_queue(backlog: dict, queue: dict, when: str) -> list[str]:
    """``kind: bt`` and ``kind: ml`` statuses from the shared study queue (the runner
    moves queued -> running -> concluded / stopped / failed, E26.3/E27.7). Returns change
    lines."""
    out = []
    by_id = {
        e.get("id"): e
        for e in backlog["items"].values()
        if is_study(e) and e.get("status") != "dropped"
    }
    for q in queue.get("entries") or []:
        entry = by_id.get(q.get("id"))
        if entry is None or q.get("status") not in BT_QUEUED:
            continue
        if entry.get("spec_hash") and q.get("spec_hash") != entry["spec_hash"]:
            continue  # not the study this entry approved
        new = {"status": q["status"], "ladder_state": q.get("ladder_state")}
        if q.get("stop_reason"):
            new["stop_reason"] = q["stop_reason"]
        if q.get("pr_url"):
            new["pr_url"] = q["pr_url"]
        if all(entry.get(k) == v for k, v in new.items()):
            continue
        old = entry["status"]
        entry.update(new)
        _hist(entry, when, "queue-sync", **new)
        if old != new["status"]:
            out.append(f"{entry['id']}: {old} -> {new['status']}")
    return out


def iso_week(when: dt.datetime) -> tuple[int, int]:
    """The ISO (year, week) of *when* in America/New_York (the D121 cap's week)."""
    y, w, _ = when.astimezone(ET).isocalendar()
    return y, w


def week_reset(when: dt.datetime) -> dt.datetime:
    """Monday 00:00 ET after *when*: when the BT cap resets."""
    d = when.astimezone(ET).date()
    monday = d + dt.timedelta(days=7 - d.weekday())
    return dt.datetime.combine(monday, dt.time(0, 0), tzinfo=ET)


def bt_approvals_in_week(backlog: dict, now: dt.datetime, exclude: str = "") -> list[dict]:
    """``kind: bt`` entries the owner approved in *now*'s ISO week (ET), any status after
    (a stopped study still counts in its week; D121 counts approvals, not runs)."""
    week = iso_week(now)
    out = []
    for e in backlog["items"].values():
        at = e.get("approved_at")
        if not is_bt(e) or not at or e.get("id") == exclude:
            continue
        if iso_week(dt.datetime.fromisoformat(at)) == week:
            out.append(e)
    return sorted(out, key=lambda e: e["approved_at"])


def bt_queue_lines(queue_view: dict | None, errors: str = "") -> list[str]:
    """Context ``## Backtest queue`` (E26.2): every entry with status, next rung, nights so
    far and ETA, plus the per-rung compute estimates the Analyst reads nights_needed off."""
    if not isinstance(queue_view, dict):
        return [f"- unavailable: {errors or '`arc backtest study queue --json` failed'}"]
    out = []
    for r in queue_view.get("entries") or []:
        eta = r.get("eta_nights")
        eta_s = f"ETA {eta} night(s)" if eta is not None else "ETA ?"
        if r.get("status") not in ("queued", "running"):
            eta_s = "done"
        out.append(
            f"- {r['id']} [{r['status']}] next rung {r.get('ladder_state') or '-'} | nights so "
            f"far {r.get('nights', 0)} | {eta_s}"
            + (f" | queue #{r['queue_position']}" if r.get("queue_position") else "")
            + (f" | {r['stop_reason']}" if r.get("stop_reason") else "")
            + (f" | PR {r['pr_url']}" if r.get("pr_url") else "")
        )
    if not out:
        out.append("- empty (no approved BT study yet)")
    est = queue_view.get("rung_estimates") or {}
    parts = []
    for rung in ("c125_dev", "c500_dev", "holdout"):
        s = (est.get(rung) or {}).get("seconds")
        parts.append(f"{rung} {s / 3600:.1f} h" if _is_num(s) else f"{rung} ?")
    out.append(
        "- compute per rung (median of recent ledger runs x 1.2): "
        + ", ".join(parts)
        + "; one night = 11 h (21:30-08:30 ET). nights_needed = the ladder's sum / 11 h"
    )
    return out


# ---------- ML-<n> model studies (E27.8, D133) ----------


def ml_numbers(backlog: dict) -> set[int]:
    """Every ML number a ``kind: ml`` entry holds (dropped ones too: never reused)."""
    return {
        _study_num(e["id"])
        for e in backlog["items"].values()
        if is_ml(e) and ML_STUDY_ID_RE.match(str(e.get("id") or ""))
    }


def ml_by_id(backlog: dict, sid: str) -> dict | None:
    """The ``kind: ml`` entry holding *sid*."""
    return next((e for e in backlog["items"].values() if is_ml(e) and e.get("id") == sid), None)


def reserve_ml_id(backlog: dict, floor: int) -> str:
    """The next free ``ML-<n>``: above *floor* - 1 (specs, queue, ledger, retired, open
    ``ml/<n>`` PRs) and every ML number the backlog holds."""
    return f"ML-{max([floor - 1, *ml_numbers(backlog)]) + 1}"


def _ml_nums(ids) -> set[int]:
    return {_study_num(str(x)) for x in ids if ML_STUDY_ID_RE.match(str(x or ""))}


def load_ml_ledger_rows(path: Path) -> tuple[list[dict], list[str]]:
    """``data/ml/studies/ledger.jsonl`` (E27.7), read-only: ``(rows, errors)``."""
    if not path.exists():
        return [], []
    rows, errs = [], []
    for i, line in enumerate(path.read_text().splitlines(), 1):
        if not line.strip():
            continue
        d = _loads(line, None)
        if not isinstance(d, dict) or not d.get("study") or not d.get("rung"):
            errs.append(f"{path.name} line {i}: not an ML ledger row")
            continue
        rows.append(d)
    return rows, errs


def load_ml_retired(path: Path) -> tuple[dict[str, dict], list[str]]:
    """``config/ml_studies/retired.yaml`` (JSON body, ``#`` comments) -> ``{ML-<n>: row}``."""
    if not path.exists():
        return {}, []
    try:
        doc = parse_seed_text(path.read_text())
    except (OSError, ValueError) as e:
        return {}, [f"ml {path.name} is not readable JSON ({e}): retired ML studies unknown"]
    rows = doc.get("retired") if isinstance(doc, dict) else None
    if not isinstance(rows, list):
        return {}, [f"ml {path.name} has no `retired` list"]
    out, errs = {}, []
    for i, r in enumerate(rows):
        sid = str((r or {}).get("id", "")) if isinstance(r, dict) else ""
        if not ML_STUDY_ID_RE.match(sid) or sid in out:
            errs.append(f"ml {path.name} retired[{i}]: bad or duplicate id {sid!r}")
            continue
        out[sid] = {"at": str(r.get("at") or ""), "by": r.get("by"), "reason": r.get("reason")}
    return out, errs


def ml_verdict_key(row: dict) -> str:
    """One ML verdict's identity (the wake watermark), like :func:`holdout_key`."""
    return f"{row['study']}:{str(row.get('spec_hash', ''))[:12]}:{row.get('at', '')}"


def _read_csv(path: Path) -> list[dict]:
    try:
        with path.open(newline="") as fh:
            return list(csv.DictReader(fh))
    except (OSError, csv.Error):
        return []


def _truthy(v) -> bool:
    return str(v).strip().lower() in ("true", "1", "yes")


def ml_headline(vdir: Path, meta: dict) -> str:
    """A version's headline dev metrics from its eval files (aggregates only, D126)."""
    dec = _read_csv(vdir / "eval_decisions.csv")  # M1: the keep rule per (kind, h)
    if dec:
        cols = [c for c in dec[0] if c.endswith("_pass")]
        keep = [r for r in dec if cols and all(_truthy(r.get(c)) for c in cols)]
        folds = "/".join(c.removesuffix("_pass") for c in cols)
        worst = ""
        for r in dec[:1]:
            worst = f"; e.g. {r.get('kind')} h{r.get('h')}: {r.get(cols[-1][:-5] + '_detail')}"
        return f"LightGBM keeps {len(keep)}/{len(dec)} targets ({folds}, both years){worst}"
    rule = _read_csv(vdir / "eval_rule.csv")  # M2: the promote rule per test year
    if rule:
        return "; ".join(
            f"{r.get('fold')} {'pass' if _truthy(r.get('year_pass')) else 'fail'} "
            f"(log-loss {_num(_float(r.get('ll_m2')), '.4f')} vs logistic "
            f"{_num(_float(r.get('ll_logistic')), '.4f')}, e_r IC t "
            f"{_num(_float(r.get('er_ic_t')), '.2f')}, replay "
            f"{'pass' if _truthy(r.get('replay_pass')) else 'fail'})"
            for r in rule
        )
    summary = meta.get("summary")
    return f"summary {clip(json.dumps(summary, sort_keys=True), 200)}" if summary else "no eval"


def _float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def ml_models(root: Path) -> tuple[list[dict], list[str]]:
    """The model registry (E27.3) per family, read-only: champion and every version with
    its status, rule result, headline dev metrics and holdout reads. A version directory
    with a manifest but no ``models.json`` row is listed as ``unregistered``."""
    base = root / ML_MODELS
    if not base.is_dir():
        return [], []
    out, errs = [], []
    for fam in sorted(d for d in base.iterdir() if d.is_dir()):
        idx: dict = {}
        if (fam / "models.json").is_file():
            idx = _loads((fam / "models.json").read_text(), None)
            if not isinstance(idx, dict):
                errs.append(f"ml {fam.name}/models.json is not readable JSON")
                idx = {}
        versions = {str(k): dict(v or {}) for k, v in (idx.get("versions") or {}).items()}
        for m in sorted(fam.glob("*/manifest.json")):
            versions.setdefault(m.parent.name, {"status": "unregistered"})
        rows = []
        for v, meta in versions.items():
            man = _loads((fam / v / "manifest.json").read_text(), {}) if (
                fam / v / "manifest.json"
            ).is_file() else {}  # fmt: skip
            rows.append(
                {
                    "version": v,
                    "status": meta.get("status") or "unregistered",
                    "created": str(meta.get("created") or (man or {}).get("created") or ""),
                    "rule": meta.get("rule") or (man or {}).get("rule"),
                    "rule_passed": meta.get("rule_passed", (man or {}).get("rule_passed")),
                    "holdout_reads": (man or {}).get("holdout_reads"),
                    "headline": ml_headline(fam / v, meta),
                }
            )
        rows.sort(key=lambda r: (r["created"], r["version"]))
        if rows:
            out.append({"family": fam.name, "champion": idx.get("champion"), "versions": rows})
    return out, errs


def ml_snapshot(p: Paths) -> dict:
    """Registry + ML ledger + queue + retired list, read-only (the gate's ML snapshot:
    ``record`` validates families, incumbents, ``ML-<n>`` citations and the next id
    against it, and ``mark`` stores its verdict keys)."""
    root = p.bt_root
    models, errs = ml_models(root)
    rows, l_errs = load_ml_ledger_rows(root / ML_LEDGER)
    retired, r_errs = load_ml_retired(root / ML_RETIRED)
    errs += l_errs + r_errs
    queue = [
        e for e in load_bt_queue(root / BT_QUEUE).get("entries") or [] if e.get("kind") == "ml"
    ]
    spec_dir = root / ML_SPECS
    spec_ids = [f.stem for f in spec_dir.glob("ML-*.yaml")] if spec_dir.is_dir() else []
    studies: dict[str, dict] = {}
    for r in rows:
        s = studies.setdefault(r["study"], {"id": r["study"], "rungs": []})
        s["rungs"].append(r)
        for k in ("family", "incumbent", "spec_hash"):
            if r.get(k):
                s[k] = r[k]
        if r["rung"] in ("verdict", "dropped"):
            s["verdict"] = r
    for q in queue:
        studies.setdefault(q["id"], {"id": q["id"], "rungs": []})["queue"] = {
            k: q.get(k) for k in ("status", "ladder_state", "stop_reason", "pr_url", "approved_at")
        }
    for s in studies.values():
        s["retired"] = retired.get(s["id"])
    nums = _ml_nums([*spec_ids, *studies, *retired])
    keys = [
        ml_verdict_key(r)
        for r in rows
        if r["rung"] == "verdict" and r.get("verdict") in ("pass", "fail")
        and r["study"] not in retired
    ]  # fmt: skip
    return {
        "models": models,
        "studies": sorted(studies.values(), key=lambda s: _study_num(s["id"])),
        "retired": retired,
        "next_id": f"ML-{max(nums, default=0) + 1}",
        "verdict_keys": keys,
        "errors": errs,
    }


def ml_snapshot_meta(snap: dict) -> dict:
    """The compact copy ``metrics.json`` carries for ``record`` (no rung detail)."""
    return {
        "models": [
            {
                "family": m["family"],
                "champion": m.get("champion"),
                "versions": [
                    {"version": v["version"], "status": v["status"]} for v in m["versions"]
                ],
            }
            for m in snap.get("models") or []
        ],  # fmt: skip
        "verdicts": {
            s["id"]: {k: (s.get("verdict") or {}).get(k) for k in ("verdict", "status", "version")}
            for s in snap.get("studies") or []
            if (s.get("verdict") or {}).get("rung") == "verdict" and not s.get("retired")
        },
        "retired": sorted(snap.get("retired") or {}, key=_study_num),
        "next_id": snap.get("next_id") or "ML-1",
    }


def ml_floor(ml_meta: dict | None, extra=()) -> int:
    """The lowest ML number a new draft may take: the gate's ``next_id`` and *extra* (open
    ``ml/<n>`` PR numbers, read at record time)."""
    nxt = str((ml_meta or {}).get("next_id") or "ML-1")
    base = _study_num(nxt) if ML_STUDY_ID_RE.match(nxt) else 1
    return max([base, *(n + 1 for n in extra)])


def _rel(x) -> str:
    return f"{100 * x:+.0f}%" if _is_num(x) else "?"


def ml_rung_text(r: dict) -> str:
    """One ML ledger row as text (mirrors arc.ml.study.ml_rung_line)."""
    if r.get("rung") == "verdict":
        return (
            f"verdict {r.get('verdict')} -> {r.get('version')} "
            f"({(r.get('status') or 'candidate')}, variant {r.get('best_variant')}); dev only"
        )
    if r.get("rung") == "dropped":
        return f"dropped ({r.get('note') or '-'})"
    vs = r.get("vs_incumbent") or {}
    if r.get("family") == "m1":
        cov = vs.get("cov_err_max")
        body = f"pinball {_rel(vs.get('pinball_rel'))} vs incumbent" + (
            f", cov err {100 * cov:.1f} pts" if _is_num(cov) else ""
        )
    else:
        body = f"log-loss {_rel(vs.get('log_loss_rel'))} vs incumbent, e_r IC {vs.get('er_ic')}"
    n = len(r.get("variants") or [])
    pick = f", best of {n}" if n > 1 else ""
    ok = "pass" if r.get("rung_pass") else "fail"
    return f"{str(r.get('rung')).replace('_', ' ')} {ok}: {body}{pick}"


def ml_models_lines(models: list[dict], errors=()) -> list[str]:
    """Context ``## ML models``: per family champion / shadow / candidate, then every
    version with its headline dev metrics and holdout reads."""
    out = [f"- WARNING: {e}" for e in errors]
    if not models:
        return [*out, "- no model under data/ml/models yet (E27.2/E27.3)"]
    for m in models:
        by: dict[str, list[str]] = {}
        for v in m["versions"]:
            by.setdefault(v["status"], []).append(v["version"])
        out.append(
            f"- {m['family']}: champion {m.get('champion') or 'none'} · shadow "
            f"{', '.join(by.get('shadow', [])) or 'none'} · candidate "
            f"{', '.join(by.get('candidate', [])) or 'none'}"
            + (f" · unregistered {', '.join(by['unregistered'])}" if by.get("unregistered") else "")
        )
        for v in m["versions"]:
            rp = v.get("rule_passed")
            rule = f"{v.get('rule') or 'rule ?'} {'pass' if rp else 'fail' if rp is False else '?'}"
            reads = v.get("holdout_reads")
            out.append(
                f"  - {v['version']} [{v['status']}] {v.get('created', '')[:10] or '-'} · {rule} · "
                f"holdout reads {reads if reads is not None else '-'} · {v['headline']}"
            )
    return out


def ml_studies_lines(snap: dict, eta: dict | None = None) -> list[str]:
    """Context ``## ML studies``: each study's queue status, rungs and verdict (a verdict
    is dev only: a pass moves the version to shadow, never to champion)."""
    eta = eta or {}
    out = [f"- WARNING: {e}" for e in snap.get("errors") or [] if not e.startswith("ml ")]
    for s in snap.get("studies") or []:
        if s.get("retired"):
            continue
        q = s.get("queue") or {}
        status = q.get("status") or ("dropped" if (s.get("verdict") or {}).get("rung") == (
            "dropped") else "ledger only")  # fmt: skip
        e = eta.get(s["id"]) or {}
        when = ""
        if status in ("queued", "running"):
            n = e.get("eta_nights")
            eta_n = n if n is not None else "?"
            when = f" | next rung {q.get('ladder_state') or '-'} | ETA {eta_n} night(s)"
        out.append(
            f"- {s['id']} [{status}] {s.get('family') or '-'} vs {s.get('incumbent') or '-'}{when}"
            + (f" | {q['stop_reason']}" if q.get("stop_reason") else "")
            + (f" | PR {q['pr_url']}" if q.get("pr_url") else "")
        )
        for r in s.get("rungs") or []:
            out.append(f"  {ml_rung_text(r)}")
    if len(out) == len([x for x in out if x.startswith("- WARNING")]):
        out.append("- none yet (no ML-<n> study approved or run)")
    by: dict[str, list[str]] = {}
    for sid, r in (snap.get("retired") or {}).items():
        by.setdefault(f"{r.get('reason')} ({r.get('at')})", []).append(sid)
    out += [
        f"- RETIRED (history only: never cite, re-run or build on these): "
        f"{', '.join(sorted(ids, key=_study_num))}: {why}"
        for why, ids in by.items()
    ]
    return out


def _n_text(n) -> str:
    return f"n={n}" + (" insufficient" if not _is_num(n) or n < ML_MIN_N else "")


def ml_live_lines(rec: dict | None, built: bool = True) -> list[str]:
    """Context ``## ML live``: the nightly ``ml_health`` record (E27.4) as text: coverage,
    live calibration, PSI drift, score drift and parity, n everywhere and ``insufficient``
    below 30 (never a basis for a claim)."""
    if not built:
        return ["- live monitoring not built yet (E27.4)"]
    if not isinstance(rec, dict) or not rec:
        return ["- no ml_health record yet (the nightly ml refresh has not written one)"]
    out = [f"- record {rec.get('day')} (schema {rec.get('schema')}, nightly "
           f"{rec.get('nightly_day') or rec.get('day')}); models "
           f"{', '.join(rec.get('model_versions') or []) or '-'}"]  # fmt: skip
    c = rec.get("coverage") or {}
    if c:
        out.append(
            f"- coverage: {c.get('scored')}/{c.get('candidates')} candidates scored "
            f"({c.get('pct_scored') if c.get('pct_scored') is not None else '-'}%) over "
            f"{c.get('ticks')} ticks; latency p95 {c.get('latency_ms_p95')} ms"
            + (
                "; unavailable: "
                + ", ".join(f"{k} x{v}" for k, v in (c.get("unavailable_reasons") or {}).items())
                if c.get("unavailable_reasons")
                else ""
            )
        )
    for sp, cal in (rec.get("calibration") or {}).items():
        parts = []
        for k, v in (cal or {}).items():
            if not isinstance(v, dict):
                continue
            n = v.get("n")
            if not _is_num(n) or n < ML_MIN_N:
                parts.append(f"{k} {_n_text(n)}")
            elif k == "e_r":
                parts.append(
                    f"e_r {_n_text(n)} mean e_r {v.get('mean_e_r')} vs mean R {v.get('mean_r')}"
                )
            else:
                parts.append(
                    f"{k} {_n_text(n)} brier {v.get('brier')} vs base {v.get('brier_base')}"
                )
        out.append(f"- calibration [{sp}]: " + ("; ".join(parts) or "none"))
    d = rec.get("feature_drift") or {}
    if d.get("status") in (None, "not_run"):
        out.append("- feature drift (PSI): not run")
    else:
        top = ", ".join(f"{t.get('feature')} {t.get('psi')}" for t in d.get("top") or [])
        warn = list(d.get("warn") or [])
        out.append(
            f"- feature drift (PSI, live {_n_text(d.get('n_live'))} vs train "
            f"n={d.get('n_train')}): "
            f"top {top or '-'}; warn > {d.get('warn_above')}: {len(warn)}"
            + (f" ({', '.join(warn[:5])})" if warn else "")
        )
    sd = (rec.get("score_drift") or {}).get("all") or {}
    if sd:
        out.append(
            "- score drift [all]: "
            + "; ".join(
                f"{k} live {_n_text(v.get('n_live'))} mean {v.get('live_mean')} vs backtest "
                f"n={v.get('n_ref')} mean {v.get('ref_mean')} PSI {v.get('psi')}"
                for k, v in sd.items()
                if isinstance(v, dict)
            )
        )
    par = rec.get("parity") or {}
    out.append(f"- parity: {par.get('status', 'not_run')}" + (
        f" ({par['detail']})" if par.get("detail") else ""))  # fmt: skip
    lr = rec.get("last_retrain")
    if isinstance(lr, dict):
        out.append(
            f"- last retrain: {lr.get('version')} {'pass' if lr.get('passed') else 'fail'} "
            f"(n={lr.get('n')}) · candidate only"
        )
    return out


def latest_ml_health(root: Path, day: dt.date) -> dict | None:
    """The newest ``data/ml/health/<YYYY-MM-DD>.json`` dated on or before *day* (stdlib
    mirror of arc.ml.refresh.latest_health)."""
    hdir = root / ML_HEALTH
    if not hdir.is_dir():
        return None
    files = sorted(f for f in hdir.glob("*.json") if f.stem <= day.isoformat())
    for f in reversed(files):
        d = _loads(f.read_text(), None)
        if isinstance(d, dict):
            return d
    return None


def ml_live_record(p: Paths, run_dir: Path, copy: Path | None, day: dt.date) -> dict | None:
    """``arc ml monitor --json`` on the store copy (coverage/calibration recomputed), else
    the newest nightly ``ml_health`` file."""
    if copy is not None:
        text = run_arc(
            p,
            ["ml", "monitor", "--json", "--db", str(copy), "--day", day.isoformat(),
             "--data", str(p.bt_root / "data")],
            run_dir,
            "ml-monitor",
            timeout=120,
        )  # fmt: skip
        for s in ("-wal", "-shm"):  # a mode=ro reader of a WAL copy leaves its index behind
            side = copy.with_name(copy.name + s)
            if side.exists() and (s == "-shm" or side.stat().st_size == 0):
                side.unlink(missing_ok=True)
        rec = _loads(text, None)
        if isinstance(rec, dict) and rec.get("coverage") is not None:
            return rec
    return latest_ml_health(p.bt_root, day)


def ml_status_view(p: Paths, run_dir: Path) -> dict:
    """``arc ml study status --json`` (the shared queue, ML priced from the ML ledger):
    ``{ML-<n>: row}``; empty when it fails (the context shows ETA ?)."""
    text = run_arc(
        p,
        ["ml", "study", "status", "--json", "--queue", str(p.bt_root / BT_QUEUE),
         "--ledger", str(p.bt_root / ML_LEDGER), "--studies-dir", str(p.bt_root / ML_SPECS),
         "--data", str(p.bt_root / "data")],
        run_dir,
        "ml-study-status",
        timeout=60,
    )  # fmt: skip
    lines = text.strip().splitlines()
    view = _loads(lines[-1] if lines else "", None)
    rows = view.get("entries") if isinstance(view, dict) else None
    return {r["id"]: r for r in rows or [] if isinstance(r, dict) and r.get("kind") == "ml"}


def ml_refs(text) -> set[str]:
    return set(re.findall(r"(?<![\w-])ML-[1-9]\d*(?!\d)", str(text or "")))


def validate_ml_study_spec(spec, where: str = "ml_study_spec", ml: dict | None = None) -> list[str]:
    """An ``ml_study_spec`` draft (E27.8, D133): a challenger, grid, feature-set or
    calibration change within an EXISTING family, under that family's registered rule.
    Mirrors arc.ml.study.MlStudySpec; ``record`` also runs ``arc ml study validate``.
    *ml* is the gate's snapshot meta (families, versions); None skips those checks."""
    if not isinstance(spec, dict):
        return [f"{where} must be an object"]
    errs = []
    filled = sorted({"id", "owner", "created", "study_value"} & set(spec))
    if filled:
        errs.append(f"{where}: {filled} are filled by `record` (the ML-<n> is reserved there)")
    extra = sorted(set(spec) - set(ML_SPEC_KEYS) - {"id", "owner", "created", "study_value"})
    if extra:
        errs.append(f"{where} has unknown keys {extra} (allowed: {list(ML_SPEC_KEYS)})")
    missing = [k for k in ML_SPEC_REQUIRED if not spec.get(k)]
    if missing:
        errs.append(f"{where} missing {missing}")
    fam = spec.get("family")
    if fam and fam not in ML_FAMILY_RULE:
        errs.append(
            f"{where}.family {fam!r}: new model families are cards, not ML studies (D133); "
            f"an ML-<n> works within {sorted(ML_FAMILY_RULE)}"
        )
        return errs
    rule = spec.get("decision_rule")
    kind = rule.get("kind") if isinstance(rule, dict) else rule
    if fam and kind != ML_FAMILY_RULE[fam]:
        errs.append(
            f"{where}.decision_rule {kind!r} is not the registered rule of {fam} "
            f"({{'kind': {ML_FAMILY_RULE[fam]!r}}}; a new rule kind needs a card + D#)"
        )
    if spec.get("change_kind") and spec["change_kind"] not in ML_CHANGE_KINDS:
        errs.append(f"{where}.change_kind must be one of {list(ML_CHANGE_KINDS)}")
    if not isinstance(spec.get("overlay", {}), dict):
        errs.append(f"{where}.overlay must be an object")
    grid = spec.get("grid", [])
    if not isinstance(grid, list) or not all(isinstance(g, dict) for g in grid):
        errs.append(f"{where}.grid must be a list of overlay objects")
    elif len(grid) > ML_MAX_GRID:
        errs.append(f"{where}.grid has {len(grid)} variants; at most {ML_MAX_GRID}")
    body = json.dumps([spec.get("overlay"), spec.get("grid"), spec.get("incumbent")]).lower()
    if "holdout" in body:
        errs.append(
            f"{where}: an ML study never reads the holdout (D133: dev test years only; the "
            "holdout is read only at `arc ml promote` with the owner's re-read ok)"
        )
    if ml is not None and fam:
        fams = {m["family"]: m for m in ml.get("models") or []}
        if fam not in fams:
            errs.append(f"{where}.family {fam} has no model under data/ml/models yet")
        else:
            inc = spec.get("incumbent")
            versions = {v["version"] for v in fams[fam].get("versions") or []}
            if inc == "champion" and not fams[fam].get("champion"):
                errs.append(f"{where}.incumbent champion: {fam} has no champion; name a version")
            elif inc and inc != "champion" and inc not in versions:
                errs.append(
                    f"{where}.incumbent {inc!r} is not a {fam} version ({sorted(versions)})"
                )
    return errs


def _sub(f: dict, key: str, field: str):
    """``f[key][field]`` when ``f[key]`` is an object (drafts are validated elsewhere)."""
    v = f.get(key)
    return v.get(field) if isinstance(v, dict) else None


def check_ml_citations(findings: list, ml: dict | None) -> list[str]:
    """Every ``ML-<n>`` a finding cites must resolve to an ML ledger verdict (a dev rung
    or a queued study is a hypothesis, not evidence); a retired one is never evidence."""
    verdicts = (ml or {}).get("verdicts") or {}
    retired = set((ml or {}).get("retired") or [])
    errs = []
    for i, f in enumerate(findings):
        if not isinstance(f, dict):
            continue
        texts = [
            f.get("evidence"),
            _sub(f, "proposal", "evidence_ref"),
            _sub(f, "study_spec", "hypothesis"),
            _sub(f, "ml_study_spec", "hypothesis"),
        ]
        for sid in sorted(set().union(*(ml_refs(t) for t in texts)), key=_study_num):
            if sid in retired:
                errs.append(f"findings[{i}] cites {sid}: it is retired (history only)")
            elif sid not in verdicts:
                errs.append(
                    f"findings[{i}] cites {sid}: no verdict in the ML ledger (cite only an "
                    "ML-<n> verdict; a dev rung or a queued study is a hypothesis)"
                )
    return errs


def apply_ml_backlog(
    backlog: dict, doc: dict, run: str, when: str, floor: int, finding_ids: dict | None = None
) -> list[tuple[dict, dict]]:
    """Every ``ml_study_spec`` finding as a ``kind: ml`` entry (key ``ml:<finding key>``),
    like :func:`apply_study_backlog`: a re-listed draft keeps its ML id while proposed."""
    finding_ids = finding_ids or {}
    items = backlog["items"]
    out = []
    for f in doc.get("findings", []):
        if "ml_study_spec" not in f:
            continue
        key = ML_KEY_PREFIX + f["key"]
        v = f.get("study_value") or {}
        spec = f["ml_study_spec"]
        fields = {
            "ml_study_spec": spec,
            "title": f.get("title"),
            "hypothesis": spec.get("hypothesis"),
            "family": spec.get("family"),
            "change_kind": spec.get("change_kind"),
            "expected_effect": {"pct_per_day": v.get("pct_per_day"), "source": v.get("source")},
            "affected_share": v.get("affected_share"),
            "p_success": v.get("p_success"),
            "nights_needed": v.get("nights_needed"),
            "value_score": study_value(v),
            "n": f.get("n"),
        }
        if f["key"] in finding_ids:
            fields["finding_id"] = finding_ids[f["key"]]
        entry = items.get(key)
        if entry is None:
            entry = items[key] = {
                "id": reserve_ml_id(backlog, floor),
                "kind": "ml",
                "key": key,
                "finding_key": f["key"],
                "status": "proposed",
                "created": when,
                "created_run": run,
                "history": [],
                **fields,
            }
            _hist(entry, when, "proposed", run=run)
        elif entry["status"] == "proposed":
            changed = sorted(k for k, val in fields.items() if entry.get(k) != val)
            if changed:
                entry.update(fields)
                _hist(entry, when, "edit", run=run, fields=changed)
        else:
            continue  # approved (an adopt to retry): frozen
        out.append((f, entry))
    return out


def validate_ml_backlog(f: dict, i: int, backlog: dict) -> list[str]:
    """An ``ml_study_spec`` on a key whose ML entry the owner already acted on is frozen."""
    entry = backlog["items"].get(ML_KEY_PREFIX + str(f.get("key")))
    if entry is None or entry.get("status") == "proposed":
        return []
    sid, st = entry["id"], entry["status"]
    if st == "dropped":
        return [
            f"findings[{i}] ml_study_spec: {sid} (key {f.get('key')}) was dropped by the owner; "
            "re-propose only a new design under a new finding key"
        ]
    if st == "approved" and f.get("ml_study_spec") == entry.get("ml_study_spec"):
        return []
    return [
        f"findings[{i}] ml_study_spec: {sid} is {st} (owner-approved, the spec is frozen); "
        "remove the ml_study_spec from this finding (its progress is in ## ML studies)"
    ]


def validate_ml_finding(f: dict, i: int, backlog: dict, ml: dict | None) -> list[str]:
    """A finding carrying an ``ml_study_spec`` (one study per finding: not with a BT)."""
    errs = []
    if f.get("category") not in STUDY_SPEC_CATEGORIES:
        errs.append(
            f"findings[{i}] `ml_study_spec` drafts a model study: only on {STUDY_SPEC_CATEGORIES}"
        )
    if "study_spec" in f:
        errs.append(f"findings[{i}] carries both study_spec and ml_study_spec: one study each")
    errs += validate_ml_study_spec(f["ml_study_spec"], f"findings[{i}] ml_study_spec", ml)
    errs += validate_study_value(f.get("study_value"), f"findings[{i}] study_value")
    errs += validate_ml_backlog(f, i, backlog)
    return errs


def _validate_next_ml_study(doc: dict, backlog_after: dict) -> list[str]:
    """E27.8 (D133): ``next_ml_study`` names the ML study the owner should approve next
    (the top-ranked proposed ``kind: ml`` entry, or another with ``next_ml_study_reason``),
    or ``none: <why>``."""
    nxt = str(doc.get("next_ml_study", "")).strip()
    if not nxt:
        return ["next_ml_study is required: an ML-<n> from the ranked backlog, or 'none: <why>'"]
    if nxt.lower().startswith("none"):
        if not nxt.partition(":")[2].strip():
            return ["next_ml_study 'none' needs a reason ('none: <why>')"]
        return []
    ranked = ranked_backlog(backlog_after, kind="ml")
    if not any(e["id"] == nxt for e in ranked):
        return [
            f"next_ml_study {nxt!r} must be a proposed ML-<n> in the backlog (an ml_study_spec "
            "draft after this run), or 'none: <why>'"
        ]
    if ranked[0]["id"] != nxt and not str(doc.get("next_ml_study_reason", "")).strip():
        return [
            f"next_ml_study {nxt} is not the top-ranked ML study ({ranked[0]['id']}); say why "
            "in next_ml_study_reason"
        ]
    return []


def write_ml_study_spec(path: Path, sid: str, spec: dict, f: dict, aid: str, now) -> Path:
    """The draft for the owner: ``RUN_DIR/study-specs/ML-<n>.yaml`` (JSON is valid YAML),
    an ``MlStudySpec`` (extra=forbid) that ``arc ml study adopt`` commits on approval."""
    v = f.get("study_value") or {}
    nights = v.get("nights_needed")
    doc = {
        "id": sid,
        "hypothesis": spec["hypothesis"],
        "owner": f"Arc Analyst {aid}",
        "created": now.astimezone(ET).date().isoformat(),
        "family": spec["family"],
        "incumbent": spec["incumbent"],
        "change_kind": spec["change_kind"],
        "overlay": spec.get("overlay") or {},
        "grid": spec.get("grid") or [],
        "decision_rule": spec["decision_rule"]
        if isinstance(spec["decision_rule"], dict)
        else {"kind": spec["decision_rule"]},
        "study_value": {
            "expected_effect": {"pct_per_day": v.get("pct_per_day"), "source": v.get("source")},
            "affected_share": v.get("affected_share"),
            "p_success": v.get("p_success"),
            "nights_needed": max(1, math.ceil(nights)) if _is_num(nights) else None,
            "value_score": study_value(v),
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"# DRAFT ML study spec by the Arc Analyst ({aid}, {f['key']}). Not committed, not run:\n"
        f"# the Analyst never trains a model. The owner's `approve {sid}` in #arc-analyst runs\n"
        f"# `arc ml study adopt` (config/ml_studies/{sid}.yaml via an auto-merged PR + a queue\n"
        "# entry): dev 2022 -> dev 2023 -> verdict on the shared nightly runner. Dev only; a\n"
        "# pass moves the version to shadow (D133).\n"
        + json.dumps(doc, indent=2, sort_keys=True)
        + "\n"
    )
    return path


def ml_cli_validate(p: Paths, run_dir: Path, drafts: list[tuple[str, dict, dict]]) -> list[str]:
    """``arc ml study validate`` on each draft (overlay vs the family's training config,
    features vs the registry, incumbent manifest, grid cap): the pydantic check the
    stdlib mirror cannot do. Fails closed: a validator that does not answer rejects."""
    errs = []
    tmp = run_dir / ML_SPECS_DIR / ".validate"
    for sid, spec, f in drafts:
        path = write_ml_study_spec(tmp / f"{sid}.yaml", sid, spec, f, "A-check", NOW_FOR_CHECK)
        text = run_arc(
            p,
            ["ml", "study", "validate", str(path), "--json",
             "--models", str(p.bt_root / ML_MODELS), "--studies-dir", str(p.bt_root / ML_SPECS),
             "--data", str(p.bt_root / "data")],
            run_dir,
            f"ml-validate-{sid}",
            timeout=120,
        )  # fmt: skip
        lines = text.strip().splitlines()
        d = _loads(lines[-1] if lines else "", None)
        if not isinstance(d, dict) or not d.get("spec_hash"):
            errs.append(
                f"{sid} (key {f['key']}): `arc ml study validate` refused it: "
                f"{clip(text.strip(), 400)}"
            )
    shutil.rmtree(tmp, ignore_errors=True)
    return errs


NOW_FOR_CHECK = dt.datetime(2000, 1, 1, tzinfo=dt.timezone.utc)  # validation drafts only


def write_ml_drafts(
    run_dir: Path, backlog: dict, doc: dict, items: dict, finding_ids: dict, floor: int, now
) -> list[str]:
    """Each ml_study_spec as a ``kind: ml`` entry that keeps its ML-<n> while proposed;
    its draft is written for ``approve ML-<n>``."""
    out = []
    for f, entry in apply_ml_backlog(backlog, doc, run_dir.name, iso(now), floor, finding_ids):
        sid = entry["id"]
        it = items[f["key"]]
        path = write_ml_study_spec(
            run_dir / ML_SPECS_DIR / f"{sid}.yaml", sid, f["ml_study_spec"], f, it["id"], now
        )
        entry["draft_path"] = str(path)
        it["ml_study_spec"] = {"id": sid, "path": str(path), "run": run_dir.name}
        out.append(f"{it['id']} -> {sid} ({path})")
    return out


def ml_pr_numbers(p: Paths) -> set[int]:
    """ML numbers of open ``ml/<n>`` spec PRs (best effort, like :func:`spec_pr_numbers`)."""
    return _open_pr_numbers(p, ML_SPEC_PR_BRANCH_RE)


def pending_ml_drafts(backlog: dict, doc: dict, floor: int) -> list[tuple[str, dict, dict]]:
    """``(ML-<n>, spec, finding)`` the drafts ``record`` will write (on a copy)."""
    after = json.loads(json.dumps(backlog))
    pairs = apply_ml_backlog(after, doc, "check", "", floor)
    return [(e["id"], f["ml_study_spec"], f) for f, e in pairs]


def study_approvals_in_week(backlog: dict, now: dt.datetime, exclude: str = "") -> list[dict]:
    """BT and ML entries approved in *now*'s ISO week (ET): the shared cap (D121 + D133)."""
    week = iso_week(now)
    out = []
    for e in backlog["items"].values():
        at = e.get("approved_at")
        if not is_study(e) or not at or e.get("id") == exclude:
            continue
        if iso_week(dt.datetime.fromisoformat(at)) == week:
            out.append(e)
    return sorted(out, key=lambda e: e["approved_at"])


def apply_reviews(ledger: dict, doc: dict, run: str, when: str) -> list[str]:
    out = []
    for r in doc.get("quick_change_reviews", []) or []:
        it = next((x for x in ledger["items"].values() if x.get("id") == r["id"]), None)
        if it is None:
            continue
        it.setdefault("history", []).append(
            {
                "at": when,
                "run": run,
                "event": f"review:{r['verdict']}",
                "n": r.get("n"),
                "before": r.get("before"),
                "after": r.get("after"),
                "note": r.get("note"),
            }
        )
        new = QC_REVIEW_STATUS.get(r["verdict"])
        if new:
            it["status"] = new
        out.append(f"{r['id']} {r['verdict']}")
    return out


def _finding_shape_errs(f: dict, i: int) -> list[str]:
    """The per-finding fields both contracts (weekly and E26.4 verdict run) require."""
    errs = []
    missing = [k for k in REQUIRED if not str(f.get(k, "")).strip()]
    if missing:
        errs.append(f"findings[{i}] missing {missing}")
    if f.get("severity") not in SEVERITIES:
        errs.append(f"findings[{i}] bad severity {f.get('severity')!r}")
    if f.get("category") not in CATEGORIES:
        errs.append(f"findings[{i}] bad category {f.get('category')!r}")
    if f.get("action") not in ACTIONS:
        errs.append(f"findings[{i}] bad action {f.get('action')!r}")
    n = f.get("n")
    if not isinstance(n, int) or isinstance(n, bool) or n < 0:
        errs.append(f"findings[{i}] n (closed trades in the bucket) must be an int >= 0")
    if f.get("theme") is not None and f.get("theme") not in THEMES:
        errs.append(f"findings[{i}] unknown theme {f.get('theme')!r}")
    draft = f.get("draft_card") or {}
    if f.get("action") == "new-card" and not (draft.get("title") and draft.get("body")):
        errs.append(f"findings[{i}] action new-card needs draft_card.title and draft_card.body")
    for gone in ("forward_spec", "experiment"):
        if gone in f:
            errs.append(
                f"findings[{i}] `{gone}` is retired (D86): an experiment is an "
                "xp-proposal with a `proposal`"
            )
    return errs


def validate_findings(
    doc: dict,
    ledger: dict,
    prev_themes: dict | None = None,
    experiments: list[dict] | None = None,
    backlog: dict | None = None,
    seed_ids: frozenset = frozenset(),
    max_arms: int = MAX_PARALLEL_ARMS_DEFAULT,
    studies: list | None = None,
    used=(),
    ml: dict | None = None,
) -> list[str]:
    """*ml* is the gate's ML snapshot meta (E27.8: families/versions, ML verdicts, retired
    ids, next id); None (an old metrics.json) checks ML drafts by shape only."""
    errs: list[str] = []
    studies = studies or []
    prev_themes = prev_themes or {}
    experiments = experiments or []
    backlog = backlog if backlog is not None else empty_backlog()
    # D121: registry ids are taken; a new proposal reserves above them (as `record` does)
    used = {*used, *(_id_num(str(e.get("id") or "")) for e in experiments)} - {0}
    if doc.get("verdict") not in ("quiet", "findings"):
        errs.append("verdict must be 'quiet' or 'findings'")
    findings, resolved = doc.get("findings", []), doc.get("resolved", [])
    keys = [f.get("key") for f in findings]
    if len(keys) != len(set(keys)):
        errs.append("duplicate keys in findings")
    qcs = 0
    new_props = [
        f
        for f in findings
        if f.get("category") == "xp-proposal" and f.get("key") not in backlog["items"]
    ]
    if len(new_props) > MAX_NEW_PROPOSALS:
        errs.append(f"{len(new_props)} new xp-proposals: at most {MAX_NEW_PROPOSALS} per run (D86)")
    for i, f in enumerate(findings):
        errs += _finding_shape_errs(f, i)
        cat = f.get("category")
        if cat == "xp-proposal":
            errs += validate_proposal(f, i, backlog, seed_ids, studies)
        elif "proposal" in f:
            errs.append(f"findings[{i}] `proposal` is only allowed on an xp-proposal")
        if "study_spec" in f:  # E7.15: a draft BT-<n> for an untested hypothesis
            if cat not in STUDY_SPEC_CATEGORIES:
                errs.append(
                    f"findings[{i}] `study_spec` drafts a study for an untested hypothesis: "
                    f"only on {STUDY_SPEC_CATEGORIES}"
                )
            if cat == "xp-proposal" and (f.get("proposal") or {}).get("evidence_kind") == (
                "backtest"
            ):
                errs.append(f"findings[{i}] `study_spec` with backtest evidence: it is tested")
            errs += validate_study_spec(f["study_spec"], f"findings[{i}] study_spec")
            errs += validate_study_value(f.get("study_value"), f"findings[{i}] study_value")
            errs += validate_study_backlog(f, i, backlog)
        elif "study_value" in f and "ml_study_spec" not in f:
            errs.append(f"findings[{i}] `study_value` is only allowed with a study_spec")
        if "ml_study_spec" in f:  # E27.8 (D133): a draft ML-<n> model study
            errs += validate_ml_finding(f, i, backlog, ml)
        if cat == "quick-change":
            qcs += 1
            errs += validate_quick_change(f, i)
        elif "quick_change" in f:
            errs.append(f"findings[{i}] `quick_change` is only allowed on a quick-change")
    if qcs > MAX_QUICK_CHANGES:
        errs.append(f"{qcs} quick changes: at most {MAX_QUICK_CHANGES} per run")
    errs += _validate_quick_lists(doc, findings, ledger)
    errs += _validate_backlog_moves(doc, findings, backlog, seed_ids)
    after = None
    if not errs:
        after = json.loads(json.dumps(backlog))
        apply_backlog(after, doc, "check", "", used=used)  # the XP ids `record` reserves
        apply_study_backlog(after, doc, "check", "", study_floor(studies))  # E26.2 BT ids
        apply_ml_backlog(after, doc, "check", "", ml_floor(ml))  # E27.8 ML ids
        n_open = sum(
            1
            for e in after["items"].values()
            if e.get("status") in BACKLOG_OPEN and not is_study(e)
        )
        if n_open > BACKLOG_CAP:
            errs.append(
                f"backlog would hold {n_open} open proposals; cap is {BACKLOG_CAP}: drop the "
                "weakest in backlog_drops with a reason"
            )
    errs += _validate_experiment_lines(doc, after or backlog, experiments, max_arms)
    errs += _validate_next_study(doc, after or backlog)
    errs += _validate_next_ml_study(doc, after or backlog)  # E27.8 (D133)
    errs += check_ml_citations(findings, ml)
    errs += _validate_retired_mentions(doc, studies)  # E26.5 (D127)
    themes = {t.get("theme"): t for t in doc.get("themes", [])}
    for tid in THEMES:
        t = themes.get(tid)
        if t is None:
            errs.append(f"standing theme {tid!r} has no status line")
            continue
        if t.get("status") not in THEME_STATUSES:
            errs.append(f"theme {tid!r} bad status {t.get('status')!r}")
        if not str(t.get("note", "")).strip():
            errs.append(f"theme {tid!r} needs a note (N and the evidence)")
        was = (prev_themes.get(tid) or {}).get("status")
        if (
            was == "settled"
            and t.get("status") != "settled"
            and not str(t.get("new_evidence", "")).strip()
        ):
            errs.append(f"theme {tid!r} was settled: re-open it only with new_evidence")
    for extra in sorted(set(themes) - set(THEMES)):
        errs.append(f"unknown theme {extra!r}")
    items = ledger.get("items", {})
    for r in resolved:
        k = r.get("key")
        if k not in items or items[k].get("status") not in ACTIVE:
            errs.append(f"resolved key {k!r} is not an active ledger finding")
        if not str(r.get("evidence", "")).strip():
            errs.append(f"resolved {k!r} needs evidence")
    accounted = set(keys) | {r.get("key") for r in resolved}
    for k, it in items.items():
        if it.get("status") in ACTIVE and k not in accounted:
            errs.append(
                f"active finding {it['id']} ({k}) not accounted for: "
                "list it in findings (still present) or resolved (with evidence)"
            )
    if doc.get("verdict") == "quiet" and any(
        f.get("severity") in ("blocker", "high", "medium") for f in findings
    ):
        errs.append("verdict 'quiet' but findings include medium+ severity")
    return errs


def reconcile(ledger: dict, doc: dict, run: str, when: str) -> dict:
    items = ledger.setdefault("items", {})
    report: dict[str, list] = {
        "new": [],
        "regressed": [],
        "still_open": [],
        "resolved": [],
        "suppressed": [],
    }
    for f in doc.get("findings", []):
        k = f["key"]
        it = items.get(k)
        if it is None:
            n = ledger.get("next_id", 1)
            ledger["next_id"] = n + 1
            it = items[k] = {
                "id": f"A-{n}",
                "key": k,
                "status": "open",
                "first_seen_run": run,
                "first_seen": when,
                "history": [],
            }
            bucket = "new"
        elif it["status"] == "wontfix":
            bucket = "suppressed"
        elif it["status"] in ("resolved", "fixed"):
            it["status"] = "regressed"
            bucket = "regressed"
        else:
            bucket = "still_open"
        it.update(
            title=f["title"],
            severity=f["severity"],
            category=f["category"],
            n=f.get("n"),
            last_seen_run=run,
            last_seen=when,
        )
        it.setdefault("history", []).append({"at": when, "run": run, "event": bucket})
        report[bucket].append({"id": it["id"], **f})
    for r in doc.get("resolved", []):
        it = items[r["key"]]
        it["status"] = "resolved"
        it.setdefault("history", []).append(
            {"at": when, "run": run, "event": "resolved", "evidence": r["evidence"]}
        )
        report["resolved"].append(
            {
                "id": it["id"],
                "key": r["key"],
                "title": it.get("title", ""),
                "evidence": r["evidence"],
            }
        )
    return report


def triage(ledger: dict, aid: str, status: str, note: str, when: str) -> bool:
    for it in ledger.get("items", {}).values():
        if it.get("id") == aid:
            it["status"] = status
            it.setdefault("history", []).append(
                {"at": when, "event": f"owner:{status}", "note": note}
            )
            return True
    return False


def validate_report(text: str) -> list[str]:
    errs = []
    if len(text) > REPORT_MAX:
        errs.append(f"report is {len(text)} chars; cap is {REPORT_MAX}")
    for s in REPORT_SECTIONS:
        if s not in text:
            errs.append(f"report lacks {s!r}")
    # D86: both tracks every run. Each section's body holds items or `none: <why>`.
    for title in ("Quick changes", "Experiments"):
        # D141: a section ends at the next blank line or the next (emoji +) bold header line.
        m = re.search(
            rf"\*{re.escape(title)}\*[^\n]*\n(.*?)(?=\n[ \t]*\n|\n[^\n•]*\*[A-Z][^\n]*\*|\Z)",
            text,
            re.S,
        )
        body = m.group(1).strip() if m else ""
        if m and not body:
            errs.append(f"section *{title}* is empty: list items or `none: <why>`")
    return errs


# ---------- backtest studies (E7.15, D84) ----------


def _study_num(sid: str) -> int:
    return int(sid.split("-", 1)[1])


def load_bt_ledger(path: Path) -> tuple[list[dict], list[str]]:
    """The E7.12 research ledger (``data/backtest/ledger.jsonl``), read-only: ``(rows, errors)``.

    A missing file is no study yet (``[]``), never an error; a bad line is skipped and named.
    """
    if not path.exists():
        return [], []
    rows, errs = [], []
    for i, line in enumerate(path.read_text().splitlines(), 1):
        if not line.strip():
            continue
        try:
            d = json.loads(line)
        except ValueError as e:
            errs.append(f"{path.name}:{i} is not JSON ({e})")
            continue
        if (
            not isinstance(d, dict)
            or not BACKTEST_ID_RE.match(str(d.get("study", "")))
            or d.get("phase") not in ("dev", "holdout")
        ):
            errs.append(f"{path.name}:{i} is not a study ledger line")
            continue
        rows.append(d)
    return rows, errs


def load_study_reports(directory: Path) -> dict[str, dict]:
    """``BT-<n>`` -> the header of its committed standard report (``docs/RESEARCH/studies``)."""
    out = {}
    for f in sorted(directory.glob("BT-*.json")) if directory.is_dir() else []:
        if not BACKTEST_ID_RE.match(f.stem):
            continue
        doc = _loads(f.read_text(), {})
        if isinstance(doc, dict) and isinstance(doc.get("header"), dict):
            out[f.stem] = doc["header"]
    return out


def holdout_key(e: dict) -> str:
    """One holdout run's identity (the wake rule's watermark)."""
    return f"{e['study']}:{str(e.get('spec_hash', ''))[:12]}:{e.get('at', '')}"


def load_retired_studies(path: Path) -> tuple[dict[str, dict], list[str]]:
    """E26.5 (D127): ``retired.yaml`` -> ``({BT-<n>: {at, by, reason}}, errors)``. Missing is
    nothing retired; a malformed file or row is named in *errors* (the strict pydantic loader
    and CI keep the committed file valid)."""
    if not path.exists():
        return {}, []
    try:
        doc = parse_seed_text(path.read_text())
    except (OSError, ValueError) as e:
        return {}, [f"{path.name} is not readable JSON ({e}): retired studies unknown"]
    rows = doc.get("retired") if isinstance(doc, dict) else None
    if not isinstance(rows, list):
        return {}, [f"{path.name} has no `retired` list"]
    out, errs = {}, []
    for i, r in enumerate(rows):
        sid = str((r or {}).get("id", "")) if isinstance(r, dict) else ""
        if not BACKTEST_ID_RE.match(sid) or sid in out:
            errs.append(f"{path.name} retired[{i}]: bad or duplicate id {sid!r}")
            continue
        out[sid] = {"at": str(r.get("at") or ""), "by": r.get("by"), "reason": r.get("reason")}
    return out, errs


def retired_ids(p: Paths) -> set[str]:
    return set(load_retired_studies(p.bt_root / BT_RETIRED)[0])


def live_holdouts(entries: list[dict], retired: set[str]) -> list[str]:
    """The holdout keys that wake a run: a retired study's holdout never does (D127)."""
    return [
        holdout_key(e) for e in entries if e["phase"] == "holdout" and e["study"] not in retired
    ]


def current_studies(studies: list[dict]) -> list[dict]:
    """The studies that are evidence or work in progress: every one not retired."""
    return [s for s in studies if not s.get("retired")]


def retired_lines(studies: list[dict]) -> list[str]:
    """One context line per retirement reason: the ids, so the model neither cites nor
    re-proposes them."""
    by: dict[str, list[str]] = {}
    for s in studies:
        r = s.get("retired")
        if r:
            by.setdefault(f"{r.get('reason')} ({r.get('at')})", []).append(s["id"])
    return [
        f"- RETIRED (D127, history only: never cite, re-run, extend or build on these; draft "
        f"fresh studies from current main): {', '.join(sorted(ids, key=_study_num))}: {why}"
        for why, ids in by.items()
    ]


def retired_refs(text, studies: list[dict]) -> list[str]:
    """Retired ``BT-<n>`` ids named in *text*."""
    gone = {s["id"] for s in studies if s.get("retired")}
    found = set(re.findall(r"(?<![\w-])BT-[1-9]\d*(?!\d)", str(text or "")))
    return sorted(found & gone, key=_study_num)


def _validate_retired_mentions(doc: dict, studies: list[dict]) -> list[str]:
    """``next_*`` text must not lean on a retired study as evidence (D127). Naming it as
    retired ("BT-1..8 are retired") is fine; citing its result is not."""
    errs = []
    for k in ("next_experiment", "next_reason", "next_study", "next_study_reason"):
        text = str(doc.get(k) or "")
        if "retired" in text.lower():
            continue
        for sid in retired_refs(text, studies):
            errs.append(
                f"{k} cites {sid}: it is retired (D127, history only); leave it out or say "
                "it is retired"
            )
    return errs


def arm_variables(arms: dict) -> list[str]:
    """Dotted leaves the treatments change: overlay leaves (``exits.default.take_profit_pct``),
    plus ``ranker`` when a treatment walks a different ranker than the control."""
    arms = arms if isinstance(arms, dict) else {}
    ctrl = arms.get("control") or {}
    out: set[str] = set()
    for arm in (arms.get("treatments") or {}).values():
        arm = arm if isinstance(arm, dict) else {}
        out.update(".".join(x) for x in _leaves(arm.get("overlay") or {}) if x)
        if arm.get("ranker") != ctrl.get("ranker"):
            out.add("ranker")
    return sorted(out)


def _run_summary(e: dict) -> dict:
    rows = [
        {"profile": p, "treatment": t, **(v if isinstance(v, dict) else {})}
        for p, tr in sorted((e.get("verdicts") or {}).items())
        for t, v in sorted((tr or {}).items())
    ]
    return {
        "universe": e.get("universe"),
        "at": e.get("at"),
        "start": e.get("start"),
        "end": e.get("end"),
        "spec_hash": e.get("spec_hash"),
        "overfit_warning": bool(e.get("overfit_warning")),
        "verdicts": rows,
        # E7.20 (D125): a robust spec's `switch` is already its robust call, so a
        # `fragile` arm (wins the control cell, fails the robustness grid) never passes
        "passes": any(bool(r.get("switch")) for r in rows),
        "fragile": any(r.get("robust_call") == "fragile" for r in rows),
    }


def study_overview(entries: list[dict], current: dict[str, dict]) -> list[dict]:
    """One summary per study: ledger runs (E7.12) joined with the current spec (``arc backtest
    study show --json``: its hash now) or, without a spec file, its committed report header."""
    ids = sorted({e["study"] for e in entries} | set(current), key=_study_num)
    out = []
    for sid in ids:
        mine = sorted((e for e in entries if e["study"] == sid), key=lambda e: str(e.get("at")))
        cur = current.get(sid) or {}
        dev = [e for e in mine if e["phase"] == "dev"]
        hold = [e for e in mine if e["phase"] == "holdout"]
        last = mine[-1] if mine else {}
        h = hold[-1] if hold else None
        ref = h or last
        arms = ref.get("arms") or cur.get("arms") or {}
        cur_hash = cur.get("spec_hash")
        variants = [int(e.get("variants") or 0) for e in mine] + [
            int(cur.get("variants_tried") or 0)
        ]
        out.append(
            {
                "id": sid,
                "hypothesis": cur.get("hypothesis") or "",
                "arms": arms,
                "variables": arm_variables(arms),
                "profiles": ref.get("profiles") or cur.get("profiles") or [],
                "ladder": cur.get("universe_ladder") or [],
                "rungs": sorted(
                    {e.get("universe") for e in mine if e.get("universe") in BT_CHECKPOINT_NAMES},
                    key=BT_CHECKPOINT_NAMES.index,
                ),
                "runs": len(mine),
                "dev": _run_summary(dev[-1]) if dev else None,
                "holdout": _run_summary(h) if h else None,
                "variants": max(variants),
                "max_variants": cur.get("max_variants"),
                "current_hash": cur_hash,
                "spec_file": bool(cur_hash),
                "hash_changed": bool(h and cur_hash and cur_hash != h.get("spec_hash")),
                "git_sha": last.get("git_sha"),
                "at": last.get("at"),
            }
        )
    return out


def _verdict_text(r: dict) -> str:
    if r.get("robust_call") == "fragile":
        g = r.get("grid") or {}
        call = (
            f"FRAGILE (wins the control cell, fails the robustness grid: won "
            f"{g.get('cells_won')}/{g.get('cells')} cells)"
        )
    elif r.get("switch"):
        call = "SWITCH candidate (robust)" if r.get("robust_call") else "SWITCH candidate"
    else:
        call = "keep control"
    return (
        f"{r.get('profile')}/{r.get('treatment')} {call} diff {_num(r.get('pnl_diff'), '+,.0f')} "
        f"CI [{_num(r.get('ci_lo'), ',.0f')}, {_num(r.get('ci_hi'), ',.0f')}]"
    )


def study_evidence_class(s: dict) -> str:
    """What a study may back (D86 option A): only a passing C500 holdout is evidence."""
    h = s.get("holdout")
    if h is None:
        return "hypothesis only (dev, no holdout)"
    if h.get("universe") != BT_CONFIRM_RUNG:
        return f"hypothesis only ({h.get('universe')} holdout, not C500)"
    if s.get("hash_changed"):
        return "NOT citable (spec edited since the holdout)"
    if not h.get("passes") and h.get("fragile"):
        return "C500 holdout: FRAGILE (fails the robustness grid; not evidence)"
    if not h.get("passes"):
        return "C500 holdout: keep control (evidence against)"
    return "C500 holdout PASS (may back an XP proposal)"


def _treatments(arms: dict) -> dict:
    return arms.get("treatments") or {}


def _arm_label(name: str, arm) -> str:
    arm = arm or {}
    ov = arm.get("overlay")
    leaves = ",".join(".".join(x) for x in _leaves(ov)) if ov else ""
    return f"{name}={arm.get('ranker') or 'incumbent'}" + (f" +{leaves}" if leaves else "")


def study_lines(studies: list[dict]) -> list[str]:
    """One line per study for the context (the cap is applied by the caller)."""
    if not studies:
        return ["- none yet (no BT-<n> in data/backtest/ledger.jsonl or config/backtest_studies)"]
    out = []
    for s in studies:
        arms = s.get("arms") or {}
        named = [("control", arms.get("control")), *sorted(_treatments(arms).items())]
        arm_txt = " | ".join(_arm_label(name, a) for name, a in named)
        dev, h = s.get("dev"), s.get("holdout")
        dev_txt = (
            f"dev {dev['universe']} {dev.get('start')}..{dev.get('end')}: "
            + "; ".join(_verdict_text(r) for r in dev["verdicts"])
            if dev
            else "dev: not run"
        )
        hold_txt = (
            f"HOLDOUT {h['universe']} {h.get('start')}..{h.get('end')}: "
            + "; ".join(_verdict_text(r) for r in h["verdicts"])
            if h
            else "holdout pending"
        )
        mv = s.get("max_variants")
        overfit = (isinstance(mv, int) and s["variants"] > mv) or bool(h and h["overfit_warning"])
        planned = "->".join(s.get("ladder") or []) or "?"
        out.append(
            f"- {s['id']} {clip(s.get('hypothesis') or '(no spec/report text)', 160)} · arms "
            f"{arm_txt} · ladder reached {','.join(s['rungs']) or '-'} (planned {planned}) · "
            f"{dev_txt} · {hold_txt} · variants {s['variants']}/{mv if mv is not None else '?'}"
            f"{' OVERFIT-WARNING' if overfit else ''} · git {s.get('git_sha') or '-'} · "
            f"{str(s.get('at') or '-')[:10]} · {study_evidence_class(s)}"
            + ("" if s.get("spec_file") else " · (no loadable spec: hash unverifiable)")
        )
    return out


def checkpoint_lines(path: Path) -> list[str]:
    """E7.9's ``checkpoints.json`` (``{c125: {complete_at, sessions, gb}, ...}``)."""
    if not path.exists():
        return [
            f"- {path.name} absent (the E7.9 Theta pull has not completed a checkpoint): "
            "studies so far ran on the partitions their report header names"
        ]
    d = _loads(path.read_text(), None)
    if not isinstance(d, dict):
        return [f"- {path.name} is not a JSON object"]
    out = []
    for c in BT_CHECKPOINT_NAMES:
        v = d.get(c)
        if isinstance(v, dict) and v.get("complete_at"):
            out.append(
                f"- {c}: complete {v.get('complete_at')} · sessions {v.get('sessions', '?')} · "
                f"{v.get('gb', '?')} GB"
            )
        else:
            out.append(f"- {c}: incomplete")
    return out


def _diff(a, b) -> str:
    return f"{b - a:+.2f}" if _is_num(a) and _is_num(b) else "n/a"


def drift_lines(path: Path) -> list[str]:
    """E7.14's backtest<->live reconciliation, per structure kind x regime, with n."""
    if not path.exists():
        return [f"- no reconciliation yet (E7.14 writes {BT_RECONCILIATION})"]
    d = _loads(path.read_text(), None)
    if not isinstance(d, dict) or not isinstance(d.get("rows"), list):
        return [f"- {path.name} has no `rows` list"]
    out = [
        f"- generated {d.get('generated_at', '?')} · git {d.get('git_sha', '?')} · sim "
        f"slippage_frac {d.get('sim_slippage_frac', '?')} (predicted = simulator, actual = fills)"
    ]
    for r in d["rows"]:
        if not isinstance(r, dict):
            continue
        n = r.get("n")
        low = isinstance(n, int) and n < MIN_SAMPLE
        m = r.get("exit_reason_match")
        match = f"{float(m) * 100:.0f}%" if _is_num(m) else "n/a"
        e0, e1 = r.get("entry_pred"), r.get("entry_actual")
        x0, x1 = r.get("exit_pred"), r.get("exit_actual")
        out.append(
            f"- {r.get('kind')} · {r.get('regime')} n={n}"
            f"{f' LOW (n<{MIN_SAMPLE})' if low else ''}: "
            f"entry pred {_num(e0, '.2f')} vs actual {_num(e1, '.2f')} ({_diff(e0, e1)})"
            f" · exit pred {_num(x0, '.2f')} vs actual {_num(x1, '.2f')} ({_diff(x0, x1)})"
            f" · exit-reason match {match}"
            f" · P&L tracking error {_num(r.get('pnl_tracking_error'), ',.2f')}"
            f" (bias {_num(r.get('pnl_bias'), '+,.2f')})"
        )
    # E7.22 (D125): the live<->sim recon adds its aggregate summary to the same slot
    out += [f"- {s}" for s in d.get("summary") or [] if isinstance(s, str)]
    return out


def backtest_snapshot(p: Paths, run_dir: Path) -> dict:
    """Ledger + current specs + reports, read-only. Each spec's hash comes from
    ``arc backtest study show BT-<n> --json`` (the gate has no YAML/pydantic). A retired
    study (E26.5, D127) keeps its row with ``retired: {at, by, reason}`` (so ids stay taken)
    but is no evidence, no context finding and wakes nothing."""
    entries, errs = load_bt_ledger(p.bt_root / BT_LEDGER)
    retired, r_errs = load_retired_studies(p.bt_root / BT_RETIRED)
    errs += r_errs
    reports = load_study_reports(p.bt_root / BT_REPORTS)
    spec_dir = p.bt_root / BT_SPECS
    spec_ids = (
        sorted(
            (f.stem for f in spec_dir.glob("BT-*.yaml") if BACKTEST_ID_RE.match(f.stem)),
            key=_study_num,
        )
        if spec_dir.is_dir()
        else []
    )
    current: dict[str, dict] = {
        sid: {
            "hypothesis": h.get("hypothesis"),
            "max_variants": h.get("max_variants"),
            "universe_ladder": h.get("universe_ladder"),
        }
        for sid, h in reports.items()
    }
    for sid in retired:
        current.setdefault(sid, {})
    for sid in spec_ids:
        if sid in retired:  # history: its hash is never checked as evidence again
            continue
        text = run_arc(
            p,
            [
                "backtest",
                "study",
                "show",
                sid,
                "--json",
                "--studies-dir",
                str(spec_dir),
                "--ledger",  # spec only: the gate reads the ledger itself (a bad line in
                str(run_dir / "no-ledger.jsonl"),  # it must not hide the hash)
            ],
            run_dir,
            f"study-show-{sid}",
            timeout=60,
        )
        lines = text.strip().splitlines()
        d = _loads(lines[-1] if lines else "", None)
        if not isinstance(d, dict) or not d.get("spec_hash"):
            errs.append(f"{sid}: `arc backtest study show --json` failed (study-show-{sid}.log)")
            continue
        current[sid] = {**current.get(sid, {}), **d}
    studies = study_overview(entries, current)
    for s in studies:
        s["retired"] = retired.get(s["id"])
    nums = [_study_num(s["id"]) for s in studies]
    return {
        "studies": studies,
        "errors": errs,
        "next_study_id": f"BT-{max(nums, default=0) + 1}",
        "holdouts": live_holdouts(entries, set(retired)),
    }


def bt_queue_view(p: Paths, run_dir: Path) -> tuple[dict | None, str]:
    """``arc backtest study queue --json`` (read-only) for the context: ``(view, error)``."""
    text = run_arc(
        p,
        ["backtest", "study", "queue", "--json", "--queue", str(p.bt_root / BT_QUEUE),
         "--ledger", str(p.bt_root / BT_LEDGER)],
        run_dir,
        "backtest-queue",
        timeout=60,
    )  # fmt: skip
    lines = text.strip().splitlines()
    view = _loads(lines[-1] if lines else "", None)
    if not isinstance(view, dict):
        return None, f"`arc backtest study queue --json` failed ({run_dir}/backtest-queue.log)"
    return view, ""


def capped_section(lines: list[str], cap: int, full: Path) -> list[str]:
    """*lines* up to about *cap* chars; the full list is written to *full*."""
    full.write_text("\n".join(lines) + "\n")
    out, size = [], 0
    for i, ln in enumerate(lines):
        if size + len(ln) + 1 > cap:
            out.append(f"- ...[{len(lines) - i} more line(s); full: {full}]")
            break
        out.append(ln)
        size += len(ln) + 1
    return out


def _check_spec_backtest_ref(ref: str, f: dict, i: int, backlog: dict, studies: list) -> list[str]:
    """``spec.backtest_ref`` on a live-evidence proposal: ``none``, a known ``BT-<n>``, or a
    legacy pre-E7.15 path kept only by a backlog entry that already carried it (grandfathered)."""
    if ref.lower() == "none":
        return []
    if BACKTEST_ID_RE.match(ref):
        s = next((x for x in studies if x["id"] == ref), None)
        if s is None:
            return [f"findings[{i}] BT-REF: {ref} is not in the study ledger"]
        if s.get("retired"):
            return [f"findings[{i}] {_retired_ref_err(ref, s)}"]
        return []
    entry = (backlog.get("items") or {}).get(f.get("key")) or {}
    if ref.startswith(LEGACY_REF_PREFIXES) and (entry.get("spec") or {}).get("backtest_ref") == ref:
        return []  # grandfathered: the entry held this ref before E7.15
    return [
        f"findings[{i}] proposal.spec.backtest_ref {ref!r} must be a BT-<n> study id or 'none' "
        "(legacy docs/RESEARCH refs stay only on backlog entries that already held them)"
    ]


def _retired_ref_err(ref: str, s: dict) -> str:
    return (
        f"BT-REF: {ref} is retired (D127): {(s.get('retired') or {}).get('reason')}; "
        "cite a current study or 'none'"
    )


def check_bt_evidence(ref: str, spec, studies: list[dict]) -> list[str]:
    """Option A (D86): a ``BT-<n>`` backing an XP proposal must resolve to a study whose C500
    holdout passed under the spec hash still committed, testing the proposal's variable.
    A retired study (D127) is never evidence."""
    s = next((x for x in studies if x["id"] == ref), None)
    if s is None:
        return [f"BT-REF: {ref} is not in the study ledger (data/backtest/ledger.jsonl)"]
    if s.get("retired"):
        return [_retired_ref_err(ref, s)]
    h = s.get("holdout")
    if h is None:
        return [
            f"BT-REF: {ref} has no holdout run (dev-only results are a hypothesis, not evidence)"
        ]
    errs = []
    if h.get("universe") != BT_CONFIRM_RUNG:
        errs.append(
            f"BT-REF: {ref}'s holdout ran on {h.get('universe')}, not {BT_CONFIRM_RUNG} "
            "(a C125/C250 result is a hypothesis, not evidence)"
        )
    if not s.get("current_hash"):
        errs.append(f"BT-REF: {ref} has no loadable spec file: its hash cannot be checked")
    elif s.get("hash_changed"):
        errs.append(
            f"BT-REF: {ref}'s spec hash changed since its holdout "
            f"({str(h.get('spec_hash'))[:12]} -> {s['current_hash'][:12]})"
        )
    if not h.get("passes") and h.get("fragile"):
        errs.append(
            f"BT-REF: {ref}'s holdout verdict is fragile (wins the control cell, fails the "
            "D125 robustness grid): not evidence"
        )
    elif not h.get("passes"):
        errs.append(f"BT-REF: {ref}'s holdout verdict is keep-control (no treatment passed D25)")
    want = set()
    for v in s.get("variables") or []:
        want.update(BT_RANKER_LEAVES if v == "ranker" else (v,))
    arms = ((spec or {}).get("arms") or {}).get("treatments") or {}
    got = {
        ".".join(x)
        for a in arms.values()
        if isinstance(a, dict)
        for x in _leaves(a.get("overlay") or {})
        if x
    }
    if got and not got <= want:
        errs.append(
            f"BT-REF: the proposal varies {sorted(got)} but {ref} tests "
            f"{s.get('variables') or 'nothing'} (its arm overlay leaves)"
        )
    return errs


def validate_study_spec(spec, where: str = "study_spec") -> list[str]:
    """A draft ``config/backtest_studies/BT-<next>.yaml`` (mirror of ``StudySpec``, extra=forbid):
    hypothesis, arms (control + t1..t3), profiles; ONE variable (the same leaf set in every
    treatment, or the ranker alone). id/owner/created/ladder/rule are filled by ``record``."""
    if not isinstance(spec, dict):
        return [f"{where} must be an object (the E7.12 study spec format)"]
    errs = [f"{where}.{k}: {why}" for k, why in STUDY_SPEC_FILLED.items() if k in spec]
    extra = sorted(set(spec) - set(STUDY_SPEC_KEYS))
    if extra:
        errs.append(f"{where} has unknown keys {extra} (StudySpec is extra=forbid)")
    if not str(spec.get("hypothesis", "")).strip():
        errs.append(f"{where}.hypothesis is required")
    prof = spec.get("profiles")
    if (
        not isinstance(prof, list)
        or not prof
        or len(set(map(str, prof))) != len(prof)
        or any(x not in STUDY_PROFILES for x in prof)
    ):
        errs.append(f"{where}.profiles must be a non-empty unique list of {STUDY_PROFILES}")
    if "primary_metric" in spec and spec["primary_metric"] not in STUDY_PRIMARY:
        errs.append(f"{where}.primary_metric must be one of {STUDY_PRIMARY}")
    mv = spec.get("max_variants")
    if "max_variants" in spec and (not isinstance(mv, int) or isinstance(mv, bool) or mv < 1):
        errs.append(f"{where}.max_variants must be an int >= 1")
    errs += _validate_study_period(spec.get("period"), f"{where}.period")
    errs += _validate_robustness(spec.get("robustness"), f"{where}.robustness")
    arms = spec.get("arms")
    if not isinstance(arms, dict) or set(arms) - {"control", "treatments"}:
        return [*errs, f"{where}.arms holds control and treatments only"]
    ts = arms.get("treatments")
    if not isinstance(ts, dict) or not 1 <= len(ts) <= 3 or set(ts) - {"t1", "t2", "t3"}:
        return [*errs, f"{where}.arms.treatments must be 1-3 arms named t1..t3"]
    named = [("control", arms.get("control") or {}), *sorted(ts.items())]
    for name, arm in named:
        if not isinstance(arm, dict) or set(arm) - {"ranker", "overlay"}:
            errs.append(f"{where}.arms.{name} holds only ranker and overlay")
            continue
        if arm.get("ranker") is not None and arm["ranker"] not in STUDY_RANKERS:
            errs.append(f"{where}.arms.{name}.ranker must be one of {STUDY_RANKERS}")
        ov = arm.get("overlay")
        ov = {} if ov is None else ov
        if not isinstance(ov, dict):
            errs.append(f"{where}.arms.{name}.overlay must be a mapping")
            continue
        bad = sorted(set(ov) - set(STUDY_TARGETS))
        if bad:
            errs.append(f"{where}.arms.{name} overlay targets {bad} not in {STUDY_TARGETS}")
        if any(not v for v in ov.values()):
            errs.append(f"{where}.arms.{name} has an empty overlay target; drop it")
    if errs:
        return errs
    if (arms.get("control") or {}).get("overlay"):
        errs.append(f"{where}.arms.control is the base config: no overlay (a ranker is fine)")
    ctrl_ranker = (arms.get("control") or {}).get("ranker")
    sets, seen = {}, {}
    for name, arm in sorted(ts.items()):
        leaves = sorted(".".join(x) for x in _leaves(arm.get("overlay") or {}) if x)
        if arm.get("ranker") not in (None, ctrl_ranker):
            leaves.append("ranker")
        if not leaves:
            errs.append(f"{where}.arms.{name} is identical to control")
        sets[name] = tuple(leaves)
        canon = json.dumps(arm, sort_keys=True)
        if canon in seen:
            errs.append(f"{where}.arms.{name} is identical to {seen[canon]}")
        seen.setdefault(canon, name)
    if len(set(sets.values())) > 1 or any("ranker" in v and len(v) > 1 for v in sets.values()):
        errs.append(
            f"ONE-VARIABLE: {where} treatments must vary one thing (the same leaf set in every "
            f"arm, or the ranker alone); got {dict(sets)}"
        )
    return errs


#: Mirrors arc.backtest.stance.StanceProxy minus the reserved E7.22 slot (a test pins it).
#: ``m1_direction`` (E27.5) reads the point-in-time M1 model: dev-phase studies only.
STUDY_STANCE_PROXIES = (
    "trend_v2",
    "noisy_0.2",
    "noisy_0.35",
    "lagged_5",
    "market_v2",
    "both",
    "m1_direction",
)
STUDY_ROBUSTNESS_KEYS = ("stance", "slippage_x", "min_cell_win_frac", "max_cell_loss_ci")


def _validate_robustness(rob, where: str) -> list[str]:
    """E7.20 (D125): an optional robustness grid (``{}`` = the default 4 × 3 grid)."""
    if rob is None:
        return []
    if not isinstance(rob, dict):
        return [f"{where} must be a mapping ({{}} = the default D125 grid)"]
    errs = []
    extra = sorted(set(rob) - set(STUDY_ROBUSTNESS_KEYS))
    if extra:
        errs.append(f"{where} has unknown keys {extra}")
    st = rob.get("stance")
    if st is not None and (
        not isinstance(st, list)
        or "trend_v2" not in st
        or len(set(st)) != len(st)
        or any(s not in STUDY_STANCE_PROXIES for s in st)
    ):
        errs.append(f"{where}.stance must be a unique list of {STUDY_STANCE_PROXIES} with trend_v2")
    xs = rob.get("slippage_x")
    if xs is not None and (
        not isinstance(xs, list)
        or not xs
        or len(set(xs)) != len(xs)
        or any(isinstance(x, bool) or not isinstance(x, int | float) or not 0 <= x <= 1 for x in xs)
    ):
        errs.append(f"{where}.slippage_x must be distinct numbers in [0, 1]")
    f = rob.get("min_cell_win_frac")
    if f is not None and (isinstance(f, bool) or not isinstance(f, int | float) or not 0 < f <= 1):
        errs.append(f"{where}.min_cell_win_frac must be in (0, 1]")
    return errs


def _validate_study_period(period, where: str) -> list[str]:
    if period is None:
        return []
    if not isinstance(period, dict) or set(period) - set(STUDY_PERIOD_KEYS):
        return [f"{where} holds only {STUDY_PERIOD_KEYS}"]
    days = {}
    for k in ("dev_start", "dev_end", "holdout_start", "holdout_end"):
        v = period.get(k)
        if v is None:
            continue
        try:
            days[k] = dt.date.fromisoformat(str(v))
        except ValueError:
            return [f"{where}.{k} must be an ISO date"]
    sd = period.get("settle_days")
    if sd is not None and (not isinstance(sd, int) or isinstance(sd, bool) or sd < 0):
        return [f"{where}.settle_days must be an int >= 0"]
    d0 = days.get("dev_start", dt.date(2020, 1, 1))
    d1 = days.get("dev_end", dt.date(2023, 12, 31))
    h0 = days.get("holdout_start", dt.date(2024, 1, 1))
    if not d0 <= d1 < h0:
        return [f"{where}: need dev_start <= dev_end < holdout_start"]
    if "holdout_end" in days and days["holdout_end"] < h0:
        return [f"{where}: holdout_end is before holdout_start"]
    return []


def write_study_spec(run_dir: Path, sid: str, spec: dict, f: dict, aid: str, now) -> Path:
    """The draft for the owner: ``RUN_DIR/study-specs/BT-<n>.yaml`` (JSON is valid YAML)."""
    doc = {
        "id": sid,
        "hypothesis": spec["hypothesis"],
        "owner": f"Arc Analyst {aid}",
        "created": now.astimezone(ET).date().isoformat(),
        "arms": spec["arms"],
        "profiles": spec["profiles"],
        "universe_ladder": list(STUDY_LADDER),
        **{
            k: spec[k]
            for k in ("period", "primary_metric", "max_variants", "robustness")
            if k in spec
        },
    }
    path = run_dir / STUDY_SPECS_DIR / f"{sid}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"# DRAFT study spec by the Arc Analyst ({aid}, {f['key']}). Not committed, not run:\n"
        "# the Analyst never runs a backtest. The owner's `approve "
        f"{sid}` in #arc-analyst commits it to\n"
        f"# config/backtest_studies/{sid}.yaml (auto-merged PR) and queues it for the nightly\n"
        "# study runner: c125 dev -> c500 dev -> one c500 holdout (E26.2/E26.3, D121). The D25\n"
        "# rule is the default.\n" + json.dumps(doc, indent=2, sort_keys=True) + "\n"
    )
    return path


# ---------- agent / owner subcommands ----------


def write_proposal_spec(p: Paths, entry: dict) -> Path:
    """An approved ``XP-<n>``'s spec, the input of ``arc experiment adopt`` (E21.4). JSON is
    valid YAML (stdlib only: no YAML writer). It carries the reserved id (D121): adopt
    registers it under that same ``XP-<n>`` (``--id``). D108: written as spec v3, control
    an empty overlay (its own arm)."""
    spec = {
        "id": entry["id"],
        "kind": "ab",
        **entry["spec"],
        "spec_version": PROPOSAL_SPEC_VERSION,
        "proposed_by": entry.get("finding_id") or "owner",
    }
    arms = dict(spec.get("arms") or {})
    arms["control"] = arms.get("control") or {"overlay": {}}
    spec["arms"] = arms
    path = p.home / PROPOSALS_DIR / f"{entry['id']}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"# APPROVED Arc Analyst proposal {entry['id']} ({entry['key']}).\n"
        "# `arc experiment adopt` (E21.4) registers it under its reserved id (D121) and\n"
        f"# commits the spec: arc experiment adopt {entry['id']} --id {entry['id']} "
        "--proposal <this file> --owner-approval slack:<ts>  (D86)\n"
        + json.dumps(spec, indent=2, sort_keys=True)
        + "\n"
    )
    return path


def _seed_ids(p: Paths) -> frozenset:
    ideas, _ = load_seed(p.idea_seed)
    return frozenset(x["id"] for x in ideas)


def write_study_drafts(
    run_dir: Path,
    backlog: dict,
    doc: dict,
    items: dict,
    finding_ids: dict,
    floor: int,
    now: dt.datetime,
) -> list[str]:
    """E7.15 + E26.2 (D121): each study_spec is a `kind: bt` backlog entry that keeps its
    BT-<n> across runs while proposed; its draft is written for `approve BT-<n>`."""
    out = []
    for f, entry in apply_study_backlog(backlog, doc, run_dir.name, iso(now), floor, finding_ids):
        sid = entry["id"]
        it = items[f["key"]]
        path = write_study_spec(run_dir, sid, f["study_spec"], f, it["id"], now)
        entry["draft_path"] = str(path)
        it["study_spec"] = {"id": sid, "path": str(path), "run": run_dir.name}
        out.append(f"{it['id']} -> {sid} ({path})")
    return out


def cmd_record(p: Paths, run_dir: Path, now: dt.datetime) -> int:
    doc = json.loads((run_dir / "findings.json").read_text())
    if load_json(run_dir / "metrics.json", {}).get("mode") == "verdict":
        print("refusing: this RUN_DIR is a verdict run; use `record RUN_DIR --mode verdict`")
        return 2
    ledger = load_json(p.ledger, {"next_id": 1, "items": {}})
    state = load_json(p.state, {})
    meta = load_json(run_dir / "metrics.json", {})
    experiments = meta.get("experiments", [])
    # D121: the numbers taken outside the backlog (the gate's registry snapshot, the specs,
    # open xp/<n> PRs): a new proposal reserves above them
    used = used_xp_numbers(p, [e["id"] for e in experiments])
    backlog = load_backlog(p.backlog, used, iso(now))
    max_arms = int(meta.get("max_parallel_arms") or MAX_PARALLEL_ARMS_DEFAULT)
    bt_meta = meta.get("backtest") or {}
    studies = bt_meta.get("studies") or []
    sync_backlog(
        backlog,
        [{**e, "experiment_id": e["id"]} for e in experiments],
        iso(now),
    )
    rereserve_collisions(backlog, used, iso(now))
    flag_needs_rewrite(backlog, iso(now))  # D108: K=4 drafts leave the ranking
    errs = validate_findings(
        doc,
        ledger,
        state.get("themes"),
        experiments,
        backlog=backlog,
        seed_ids=_seed_ids(p),
        max_arms=max_arms,
        studies=studies,
        used=used,
        ml=meta.get("ml"),
    )
    ml_floor_n = ml_floor(meta.get("ml"), ml_pr_numbers(p))  # E27.8: + open ml/<n> PRs
    if not errs:  # E27.8: the pydantic check (overlay keys, incumbent manifest): fail closed
        errs = ml_cli_validate(p, run_dir, pending_ml_drafts(backlog, doc, ml_floor_n))
    if errs:
        print("findings.json rejected (ledger and backlog unchanged):\n- " + "\n- ".join(errs))
        return 2
    report = reconcile(ledger, doc, run_dir.name, iso(now))
    items = ledger["items"]
    for f in doc.get("findings", []):
        if f.get("category") == "quick-change":
            items[f["key"]]["quick_change"] = f["quick_change"]
            items[f["key"]]["evidence"] = f.get("evidence")
    finding_ids = {k: it["id"] for k, it in items.items()}
    floor = study_floor(studies, bt_meta.get("next_study_id"))
    study_specs = write_study_drafts(run_dir, backlog, doc, items, finding_ids, floor, now)
    study_specs += write_ml_drafts(run_dir, backlog, doc, items, finding_ids, ml_floor_n, now)
    reviews = apply_reviews(ledger, doc, run_dir.name, iso(now))
    save_json(p.ledger, ledger)
    moves = apply_backlog(backlog, doc, run_dir.name, iso(now), finding_ids, used)
    backlog["next_experiment"] = {
        "value": doc.get("next_experiment"),
        "slot": doc.get("next_slot"),
        "reason": doc.get("next_reason"),
        "run": run_dir.name,
    }
    backlog["next_study"] = {
        "value": doc.get("next_study"),
        "reason": doc.get("next_study_reason"),
        "run": run_dir.name,
    }
    backlog["next_ml_study"] = {
        "value": doc.get("next_ml_study"),
        "reason": doc.get("next_ml_study_reason"),
        "run": run_dir.name,
    }
    sync_bt_queue(backlog, load_bt_queue(p.bt_root / BT_QUEUE), iso(now))
    save_json(p.backlog, backlog)
    state["themes"] = {
        t["theme"]: {k: v for k, v in t.items() if k != "theme"} for t in doc.get("themes", [])
    }
    save_json(p.state, state)
    save_json(
        run_dir / "reconciled.json",
        {**report, "backlog": moves, "reviews": reviews, "study_specs": study_specs},
    )
    out: dict = {k: [x["id"] for x in v] for k, v in report.items()}
    out["backlog"] = moves
    out["reviews"] = reviews
    out["study_specs"] = study_specs
    print(json.dumps(out, indent=2))
    return 0


def cmd_mark(p: Paths, run_dir: Path, now: dt.datetime) -> int:
    if not (run_dir / "reconciled.json").exists():
        print("refusing to mark: run `arc_analyst.py record RUN_DIR` first")
        return 2
    meta = load_json(run_dir / "metrics.json", {})
    if meta.get("mode") == "verdict":
        return mark_verdict_run(p, run_dir, meta, now)
    if "closed_max_rowid" not in meta:
        print(f"refusing to mark: {run_dir}/metrics.json is not a gate run")
        return 2
    state = load_json(p.state, {})
    state.update(
        closed_watermark=meta["closed_max_rowid"],
        last_run_at=meta["now"],
        last_run_dir=str(run_dir),
        last_marked_at=iso(now),
    )
    if "bt_holdouts" in meta:  # E7.15: the holdout verdicts this run has read
        state["bt_holdouts_seen"] = sorted(
            set(state.get("bt_holdouts_seen") or []) | set(meta["bt_holdouts"])
        )
    if "xp_verdicts" in meta:  # E26.4: the XP verdict files this run has read
        state["xp_verdicts_seen"] = sorted(
            set(state.get("xp_verdicts_seen") or []) | set(meta["xp_verdicts"])
        )
    if "ml_verdicts" in meta:  # E27.8: the ML study verdicts this run has read
        state["ml_verdicts_seen"] = sorted(
            set(state.get("ml_verdicts_seen") or []) | set(meta["ml_verdicts"])
        )
    save_json(p.state, state)
    print(f"watermark -> outcome rowid {meta['closed_max_rowid']}, last run {meta['now']}")
    return 0


def cmd_triage(p: Paths, aid: str, status: str, note: str, now: dt.datetime) -> int:
    if status not in TRIAGE:
        print(f"status must be one of {TRIAGE}")
        return 2
    ledger = load_json(p.ledger, {"next_id": 1, "items": {}})
    if not triage(ledger, aid, status, note, iso(now)):
        print(f"no finding {aid}")
        return 2
    save_json(p.ledger, ledger)
    print(f"{aid} -> {status}")
    return 0


def cmd_ship(p: Paths, aid: str, note: str, now: dt.datetime) -> int:
    """Owner ``ship A-<n>``: mark a quick change shipped (the next run must review it) and
    print how to apply it: the Slack ``config set`` command, or the ``create A-<n>`` card."""
    ledger = load_json(p.ledger, {"next_id": 1, "items": {}})
    it = next((x for x in ledger.get("items", {}).values() if x.get("id") == aid), None)
    if it is None:
        print(f"no finding {aid}")
        return 2
    q = it.get("quick_change")
    if it.get("category") != "quick-change" or not isinstance(q, dict):
        print(f"{aid} is a {it.get('category')}, not a quick-change; use `create {aid}`")
        return 2
    if it.get("status") == "shipped":
        print(f"{aid} is already shipped ({it.get('shipped_at')})")
        return 0
    it["status"] = "shipped"
    it["shipped_at"] = iso(now)
    it.setdefault("history", []).append({"at": iso(now), "event": "owner:shipped", "note": note})
    save_json(p.ledger, ledger)
    print(f"{aid} -> shipped; the next weekly run reviews it ({q.get('watch')})")
    if q.get("kind") == "config":
        print(f"apply (owner, in Slack): {q['apply']}")
    else:
        print(
            f"apply: `create {aid}` (draft card; PR body `{QC_PR_LINE} {aid}`)\n"
            f"plan: {q.get('apply')}"
        )
    print(f"rollback: {q.get('rollback')}")
    return 0


def owner_id_kind(oid: str) -> str | None:
    """D121: the owner commands route on the id prefix: ``xp`` (``XP-<n>``), ``bt``
    (``BT-<n>``, E26.2), ``ml`` (``ML-<n>``, E27.8), ``legacy`` (a retired ``P-<n>``), or
    None."""
    if PROPOSAL_ID_RE.match(oid or ""):
        return "xp"
    if BACKTEST_ID_RE.match(oid or ""):
        return "bt"
    if ML_STUDY_ID_RE.match(oid or ""):
        return "ml"  # E27.8: an ML-<n> model study
    if LEGACY_PROPOSAL_ID_RE.match(oid or ""):
        return "legacy"
    return None


def registry_status(db: Path, xid: str) -> str | None:
    """The latest registry status of *xid* in *db* (``mode=ro``), None if not registered."""
    return registry_info(db, xid)[0]


def registry_info(db: Path, xid: str) -> tuple[str | None, str | None]:
    """``(latest status, proposal_ref)`` of *xid* in *db* (``mode=ro``); ``(None, None)``
    when it is not registered."""
    if not db.is_file():
        return None, None
    try:
        conn = sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True)
        try:
            if not _has_table(conn, "experiment_events"):
                return None, None
            rows = conn.execute(
                "SELECT status, detail FROM experiment_events WHERE experiment_id = ? ORDER BY id",
                (xid,),
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return None, None
    if not rows:
        return None, None
    ref = next((r for _, d in rows for r in [_proposal_ref(_loads(d, {}))] if r), None)
    return rows[-1][0], ref


def _owner_entry(
    p: Paths, pid: str, verb: str, now: dt.datetime | None = None
) -> tuple[dict, dict | None, str | None]:
    """``(backlog, entry, refusal)`` for an owner command on *pid*. *refusal* is the reply
    when there is nothing to act on: a retired ``P-<n>`` (names its XP id, D121), a
    ``BT-<n>`` (E26.2), an id that is not a proposal, or an ``XP-<n>`` the registry holds
    but the backlog does not (an owner-made spec: its status)."""
    when = iso(now) if now else ""
    kind = owner_id_kind(pid)
    if kind is None:
        backlog = load_backlog(p.backlog)
        return backlog, None, f"{pid!r} is not a proposal id: use XP-<n> (D121)"
    used = used_xp_numbers(p) if _needs_migration(p.backlog) else ()
    backlog = load_backlog(p.backlog, used, when)
    if used:
        save_json(p.backlog, backlog)
    if kind == "legacy":
        return backlog, None, legacy_redirect(backlog, pid)
    if kind == "bt":  # E26.2: a `kind: bt` study proposal
        entry = bt_by_id(backlog, pid)
        if entry is None:
            return backlog, None, f"no study proposal {pid} in the backlog"
        return backlog, entry, None
    if kind == "ml":  # E27.8: a `kind: ml` model-study proposal
        entry = ml_by_id(backlog, pid)
        if entry is None:
            return backlog, None, f"no ML study proposal {pid} in the backlog"
        return backlog, entry, None
    entry = backlog_by_id(backlog, pid)
    if entry is None:
        status = registry_status(p.live_db, pid)
        if status:
            return backlog, None, f"{pid} is {status} in the registry (not a backlog proposal)"
        return backlog, None, f"no proposal {pid}"
    return backlog, entry, None


def _needs_migration(path: Path) -> bool:
    data = load_json(path, None)
    return isinstance(data, dict) and int(data.get("version") or 1) < BACKLOG_VERSION


APPROVAL_RE = re.compile(r"^slack:\d{9,11}\.\d{1,6}$")
GH_PREFIXES = ("GH_", "GITHUB_")


def adopt_proposal(p: Paths, pid: str, spec_path: Path, approval: str) -> tuple[int, dict | str]:
    """``arc experiment adopt`` on the LIVE store (E21.4): the one write the Analyst profile
    makes, on the owner's ``approve``. Returns ``(exit code, adopt JSON | error text)``."""
    argv = [
        *p.arc,
        "experiment",
        "adopt",
        pid,
        "--id",
        pid,  # D121: register under the reserved id; adopt refuses if it is taken
        "--proposal",
        str(spec_path),
        "--owner-approval",
        approval,
        "--db",
        str(p.live_db),
        "--json",
    ]
    # the GitHub CLI's token stays (adopt opens the spec PR); broker / LLM keys never do
    env = {**clean_env(), **{k: v for k, v in os.environ.items() if k.startswith(GH_PREFIXES)}}
    try:
        r = subprocess.run(
            argv, cwd=p.repo, env=env, capture_output=True, text=True, timeout=600, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return 127, str(e)
    if r.returncode:
        return r.returncode, (r.stderr or r.stdout).strip()
    try:
        return 0, json.loads(r.stdout)
    except ValueError:
        return 1, f"adopt printed no JSON: {r.stdout[:500]}"


def cmd_approve(p: Paths, pid: str, note: str, approval: str, now: dt.datetime) -> int:
    """Owner ``approve XP-<n>``: freeze the spec, write it, and adopt it under that same id
    (E21.4, D121 ``--id``): spec PR (auto-merge), ``registered`` (or ``queued``); the backlog
    entry moves ``approved`` -> ``registered``. Re-running retries a failed adopt
    (idempotent per XP-<n>). ``approve P-<n>`` replies with the XP id to use."""
    if not APPROVAL_RE.match(approval or ""):
        print(
            "approve needs --approval slack:<ts> (the owner's message ts in #arc-analyst, "
            "D86 owner-confirmed registry rule)"
        )
        return 2
    if owner_id_kind(pid) == "bt":
        return cmd_approve_bt(p, pid, note, approval, now)
    if owner_id_kind(pid) == "ml":
        return cmd_approve_ml(p, pid, note, approval, now)
    backlog, entry, refusal = _owner_entry(p, pid, "approve", now)
    if entry is None:
        print(refusal)
        return 2
    if entry["status"] not in ("proposed", "approved"):
        print(f"{pid} is {entry['status']} ({entry.get('experiment_id') or '-'}); nothing to do")
        return 2
    # a retry after a failed adopt finds its own registry row under the reserved id: keep it
    _, own_ref = registry_info(p.live_db, pid)
    used = used_xp_numbers(p) - ({_id_num(pid)} if own_ref == pid else set())
    moved = rereserve_collisions(backlog, used, iso(now))
    if entry["id"] != pid:
        save_json(p.backlog, backlog)
        print(
            f"{pid}'s number was taken outside the backlog since it was proposed; it is now "
            f"{entry['id']}. Re-send `approve {entry['id']}` (D121)."
        )
        return 2
    if moved:
        save_json(p.backlog, backlog)
    flag_needs_rewrite(backlog, iso(now))
    if entry.get("needs_rewrite"):
        save_json(p.backlog, backlog)
        print(
            f"{pid} needs a rewrite before approval: {entry['needs_rewrite'].get('reason')}. "
            "The Analyst re-proposes it on its next run (or `drop` it)."
        )
        return 2
    if entry["status"] == "proposed":
        entry["status"] = "approved"
        _hist(entry, iso(now), "approved", by="owner", note=note, approval=approval)
    path = write_proposal_spec(p, entry)
    entry["spec_path"] = str(path)
    save_json(p.backlog, backlog)
    code, res = adopt_proposal(p, pid, path, approval)
    if code or not isinstance(res, dict):
        print(
            f"{pid} approved, but `arc experiment adopt` failed (exit {code}): {res}\n"
            f"Spec: {path}. Re-send `approve {pid}` to retry (idempotent)."
        )
        return 1
    entry["status"] = "registered"
    entry["experiment_id"] = res["experiment_id"]
    entry["pr_url"] = res.get("pr_url")
    _hist(
        entry, iso(now), "adopted", experiment_id=res["experiment_id"],
        registry_status=res["status"], pr=res.get("pr_url"),
    )  # fmt: skip
    save_json(p.backlog, backlog)
    if res["status"] == "queued":
        when = f"queued: {res.get('queue_reason') or 'behind ' + ', '.join(res['queued_behind'])}"
    else:
        when = f"owner to start: `{res.get('start_cmd')}` (D112, manual)"
    xid = res["experiment_id"]
    print(
        (f"{pid} [{res['status']}]" if xid == pid else f"{pid} -> {xid} [{res['status']}]")
        + f", area {res['area']}, {res['arms']} arms\n"
        f"Spec PR: {res.get('pr_url') or res.get('pr_status') or '-'} "
        "(auto-merges when CI is green)\n"
        f"{when}. Start it once the spec PR is merged and the A/A calibration is committed."
    )
    return 0


def adopt_study(
    p: Paths, sid: str, spec_path: Path, approval: str, now: dt.datetime, expect_hash: str = ""
) -> tuple[int, dict | str]:
    """``arc backtest study adopt`` (E26.2): commit the draft (auto-merged PR on ``bt/<n>``)
    and queue it in ``data/backtest/queue.json``. ``ARC_ANALYST_BT_DRY_RUN_PR=1`` skips the
    PR step only (the live dry run of the card). Returns ``(exit code, JSON | error)``."""
    argv = [
        *p.arc,
        "backtest",
        "study",
        "adopt",
        sid,
        "--spec",
        str(spec_path),
        "--approval",
        approval,
        "--queue",
        str(p.bt_root / BT_QUEUE),
        "--ledger",
        str(p.bt_root / BT_LEDGER),
        "--studies-dir",
        str(p.bt_root / BT_SPECS),
        "--now",
        now.isoformat(),
        "--json",
        *(["--expect-hash", expect_hash] if expect_hash else []),
        *(["--dry-run-pr"] if os.environ.get("ARC_ANALYST_BT_DRY_RUN_PR") == "1" else []),
    ]
    env = {**clean_env(), **{k: v for k, v in os.environ.items() if k.startswith(GH_PREFIXES)}}
    try:
        r = subprocess.run(
            argv, cwd=p.repo, env=env, capture_output=True, text=True, timeout=600, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return 127, str(e)
    if r.returncode:
        return r.returncode, (r.stderr or r.stdout).strip()
    lines = r.stdout.strip().splitlines()
    try:
        return 0, json.loads(lines[-1] if lines else "")
    except ValueError:
        return 1, f"adopt printed no JSON: {r.stdout[:500]}"


def cmd_approve_bt(p: Paths, sid: str, note: str, approval: str, now: dt.datetime) -> int:
    """Owner ``approve BT-<n>`` (E26.2, D121): the weekly cap (2 per ISO week, ET), then
    ``arc backtest study adopt``: spec PR (auto-merge) + a ``queued`` entry. The entry moves
    ``proposed`` -> ``approved`` -> ``queued``; re-sending retries a failed PR or queue step
    (idempotent per BT id, the approval keeps its week)."""
    if not APPROVAL_RE.match(approval or ""):
        print(
            "approve needs --approval slack:<ts> (the owner's message ts in #arc-analyst, "
            "D86/D121 owner-confirmed rule)"
        )
        return 2
    backlog, entry, refusal = _owner_entry(p, sid, "approve", now)
    if entry is None:
        print(refusal)
        return 2
    if entry["status"] not in ("proposed", "approved"):
        print(f"{sid} is {entry['status']} (already committed and queued); nothing to do")
        return 2
    draft = Path(str(entry.get("draft_path") or ""))
    if not entry.get("draft_path") or not draft.is_file():
        print(f"{sid}: its draft {draft} is gone; the Analyst re-drafts it on its next run")
        return 2
    if entry["status"] == "proposed":
        week = study_approvals_in_week(backlog, now, exclude=sid)
        if len(week) >= BT_WEEKLY_CAP:
            got = ", ".join(f"{e['id']} ({e['approved_at'][:10]}, {e['status']})" for e in week)
            y, w = iso_week(now)
            print(
                f"{sid} refused: the weekly cap is {BT_WEEKLY_CAP} study approvals per ISO week, "
                f"BT and ML together (D121, D133); week {y}-W{w:02d} already has {got}. It resets "
                f"{week_reset(now).strftime('%a %Y-%m-%d %H:%M')} ET; re-send "
                f"`approve {sid}` then."
            )
            return 2
        entry["status"] = "approved"
        entry["approved_at"] = iso(now)
        entry["approval"] = approval
        _hist(entry, iso(now), "approved", by="owner", note=note, approval=approval)
        save_json(p.backlog, backlog)
    code, res = adopt_study(p, sid, draft, entry.get("approval") or approval, now,
                            entry.get("spec_hash") or "")  # fmt: skip
    if code or not isinstance(res, dict):
        print(
            f"{sid} approved, but `arc backtest study adopt` failed (exit {code}): {res}\n"
            f"Draft: {draft}. Re-send `approve {sid}` to retry (idempotent; the approval "
            "keeps its week)."
        )
        return 1
    entry["status"] = res.get("status") or "queued"
    entry["spec_hash"] = res["spec_hash"]
    entry["pr_url"] = res.get("pr_url")
    entry["ladder_state"] = (res.get("entry") or {}).get("ladder_state")
    _hist(
        entry, iso(now), "adopted", spec_hash=res["spec_hash"][:12], pr=res.get("pr_url"),
        pr_status=res.get("pr_status"), queue_position=res.get("queue_position"),
    )  # fmt: skip
    save_json(p.backlog, backlog)
    eta = res.get("eta_nights")
    print(
        f"{sid} [{entry['status']}] {entry.get('hypothesis')}\n"
        f"Spec PR: {res.get('pr_url') or res.get('pr_status') or '-'} "
        f"(config/backtest_studies/{sid}.yaml; auto-merges when CI is green)\n"
        f"Queue #{res.get('queue_position') or '-'}, ETA "
        f"{eta if eta is not None else '?'} night(s) (21:30-08:30 ET; c125 dev -> c500 dev -> "
        "c500 holdout). The runner starts it once the spec PR is merged."
    )
    return 0


def ml_study_cli(p: Paths, args: list[str], timeout: int = 600) -> tuple[int, dict | str]:
    """``arc ml study <args>`` (E27.7) with the analyst's paths: ``(exit, JSON | text)``.
    The ``gh`` env passes through for adopt's spec PR, like :func:`adopt_study`."""
    env = {**clean_env(), **{k: v for k, v in os.environ.items() if k.startswith(GH_PREFIXES)}}
    try:
        r = subprocess.run(
            [*p.arc, "ml", "study", *args], cwd=p.repo, env=env, capture_output=True,
            text=True, timeout=timeout, check=False,
        )  # fmt: skip
    except (OSError, subprocess.TimeoutExpired) as e:
        return 127, str(e)
    if r.returncode:
        return r.returncode, (r.stderr or r.stdout).strip()
    lines = r.stdout.strip().splitlines()
    doc = _loads(lines[-1] if lines else "", None)
    return 0, doc if isinstance(doc, dict) else r.stdout.strip()


def _ml_paths(p: Paths) -> list[str]:
    root = p.bt_root
    return ["--data", str(root / "data"), "--models", str(root / ML_MODELS),
            "--ledger", str(root / ML_LEDGER), "--studies-dir", str(root / ML_SPECS)]  # fmt: skip


def cmd_approve_ml(p: Paths, sid: str, note: str, approval: str, now: dt.datetime) -> int:
    """Owner ``approve ML-<n>`` (E27.8, D133): the shared weekly cap (2 BT+ML approvals
    per ISO week, ET), then ``arc ml study adopt``: spec PR (auto-merge) adding
    ``config/ml_studies/ML-<n>.yaml`` + a ``kind: ml`` queue entry. Re-sending retries a
    failed PR or queue step (idempotent per ML id; the approval keeps its week)."""
    backlog, entry, refusal = _owner_entry(p, sid, "approve", now)
    if entry is None:
        print(refusal)
        return 2
    if entry["status"] not in ("proposed", "approved"):
        print(f"{sid} is {entry['status']} (already committed and queued); nothing to do")
        return 2
    draft = Path(str(entry.get("draft_path") or ""))
    if not entry.get("draft_path") or not draft.is_file():
        print(f"{sid}: its draft {draft} is gone; the Analyst re-drafts it on its next run")
        return 2
    if entry["status"] == "proposed":
        week = study_approvals_in_week(backlog, now, exclude=sid)
        if len(week) >= BT_WEEKLY_CAP:
            got = ", ".join(f"{e['id']} ({e['approved_at'][:10]}, {e['status']})" for e in week)
            y, w = iso_week(now)
            print(
                f"{sid} refused: the weekly cap is {BT_WEEKLY_CAP} study approvals per ISO week, "
                f"BT and ML together (D121, D133); week {y}-W{w:02d} already has {got}. It "
                f"resets {week_reset(now).strftime('%a %Y-%m-%d %H:%M')} ET; re-send "
                f"`approve {sid}` then."
            )
            return 2
        entry["status"] = "approved"
        entry["approved_at"] = iso(now)
        entry["approval"] = approval
        _hist(entry, iso(now), "approved", by="owner", note=note, approval=approval)
        save_json(p.backlog, backlog)
    args = [
        "adopt", sid, "--spec", str(draft), "--approval", entry.get("approval") or approval,
        "--queue", str(p.bt_root / BT_QUEUE), "--now", now.isoformat(), "--json", *_ml_paths(p),
        *(["--expect-hash", entry["spec_hash"]] if entry.get("spec_hash") else []),
        *(["--dry-run-pr"] if os.environ.get("ARC_ANALYST_BT_DRY_RUN_PR") == "1" else []),
    ]  # fmt: skip
    code, res = ml_study_cli(p, args)
    if code or not isinstance(res, dict) or not res.get("spec_hash"):
        print(
            f"{sid} approved, but `arc ml study adopt` failed (exit {code}): {res}\n"
            f"Draft: {draft}. Re-send `approve {sid}` to retry (idempotent; the approval "
            "keeps its week)."
        )
        return 1
    entry["status"] = res.get("status") or "queued"
    entry["spec_hash"] = res["spec_hash"]
    entry["pr_url"] = res.get("pr_url")
    entry["ladder_state"] = (res.get("entry") or {}).get("ladder_state")
    _hist(
        entry, iso(now), "adopted", spec_hash=res["spec_hash"][:12], pr=res.get("pr_url"),
        pr_status=res.get("pr_status"), queue_position=res.get("queue_position"),
    )  # fmt: skip
    save_json(p.backlog, backlog)
    eta = res.get("eta_nights")
    print(
        f"{sid} [{entry['status']}] {entry.get('hypothesis')}\n"
        f"Spec PR: {res.get('pr_url') or res.get('pr_status') or '-'} "
        f"(config/ml_studies/{sid}.yaml; auto-merges when CI is green)\n"
        f"Queue #{res.get('queue_position') or '-'}, ETA "
        f"{eta if eta is not None else '?'} night(s) (21:30-08:30 ET, shared with BT; dev 2022 "
        "-> dev 2023 -> verdict, dev only: a pass moves the version to shadow, never to "
        "champion). The runner starts it once the spec PR is merged."
    )
    return 0


def stop_study(p: Paths, sid: str, why: str) -> tuple[int, dict | str]:
    """``arc backtest study stop`` on the queue (E26.2)."""
    argv = [*p.arc, "backtest", "study", "stop", sid, "--reason", why,
            "--queue", str(p.bt_root / BT_QUEUE), "--json"]  # fmt: skip
    try:
        r = subprocess.run(
            argv, cwd=p.repo, env=clean_env(), capture_output=True, text=True, timeout=120,
            check=False,
        )  # fmt: skip
    except (OSError, subprocess.TimeoutExpired) as e:
        return 127, str(e)
    if r.returncode:
        return r.returncode, (r.stderr or r.stdout).strip()
    try:
        return 0, json.loads(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return 1, f"stop printed no JSON: {r.stdout[:500]}"


def cmd_stop(p: Paths, sid: str, why: str, now: dt.datetime) -> int:
    """Owner ``stop BT-<n> <why>`` (E26.2): a queued/running study becomes ``stopped``
    (the runner obeys at its next check). A draft that was never committed is dropped
    with ``drop BT-<n>`` instead."""
    if owner_id_kind(sid) == "ml":
        return cmd_stop_ml(p, sid, why, now)
    if owner_id_kind(sid) != "bt":
        print(
            "`stop` is for backtest and ML studies (BT-<n>, ML-<n>); an XP is stopped with "
            "`arc experiment stop`"
        )
        return 2
    if not why.strip():
        print("stop needs a reason")
        return 2
    backlog, entry, refusal = _owner_entry(p, sid, "stop", now)
    if entry is not None and entry["status"] in BACKLOG_RANKED:
        print(f"{sid} is {entry['status']} (not committed): use `drop {sid} <why>`")
        return 2
    code, res = stop_study(p, sid, why)
    if code or not isinstance(res, dict):
        print(f"{sid}: `arc backtest study stop` refused (exit {code}): {res}")
        return 2
    if entry is not None:
        entry["status"] = "stopped"
        entry["stop_reason"] = res.get("stop_reason")
        _hist(entry, iso(now), "stopped", by="owner", reason=why)
        save_json(p.backlog, backlog)
    print(f"{sid} -> stopped ({why}); the runner stops it at its next check")
    return 0


def cmd_stop_ml(p: Paths, sid: str, why: str, now: dt.datetime) -> int:
    """Owner ``stop ML-<n> <why>`` (E27.8): ``arc ml study stop`` on the shared queue."""
    if not why.strip():
        print("stop needs a reason")
        return 2
    backlog, entry, _ = _owner_entry(p, sid, "stop", now)
    if entry is not None and entry["status"] in BACKLOG_RANKED:
        print(f"{sid} is {entry['status']} (not committed): use `drop {sid} <why>`")
        return 2
    code, res = ml_study_cli(
        p, ["stop", sid, why, "--queue", str(p.bt_root / BT_QUEUE), "--json"], 120
    )
    if code:
        print(f"{sid}: `arc ml study stop` refused (exit {code}): {res}")
        return 2
    if entry is not None:
        entry["status"] = "stopped"
        entry["stop_reason"] = res.get("stop_reason") if isinstance(res, dict) else why
        _hist(entry, iso(now), "stopped", by="owner", reason=why)
        save_json(p.backlog, backlog)
    print(f"{sid} -> stopped ({why}); the runner stops it at its next check")
    return 0


def cmd_drop(p: Paths, pid: str, why: str, now: dt.datetime) -> int:
    """Owner ``drop XP-<n> <why>``. The number stays reserved: a gap, never reused (D121)."""
    backlog, entry, refusal = _owner_entry(p, pid, "drop", now)
    if entry is None:
        print(refusal)
        return 2
    if not why.strip():
        print("drop needs a reason")
        return 2
    if entry["status"] not in BACKLOG_RANKED:
        if is_study(entry):
            print(f"{pid} is {entry['status']} (committed and queued): use `stop {pid} <why>`")
        else:
            print(f"{pid} is {entry['status']}; stop the experiment with `arc experiment stop`")
        return 2
    if is_ml(entry):  # E27.8: the ML ledger records the gap (the number is never reused)
        code, res = ml_study_cli(
            p,
            ["drop", pid, why, "--queue", str(p.bt_root / BT_QUEUE), "--now", now.isoformat(),
             *_ml_paths(p)],
            120,
        )  # fmt: skip
        if code:
            print(f"{pid}: `arc ml study drop` refused (exit {code}): {res}")
            return 2
    entry["status"] = "dropped"
    _hist(entry, iso(now), "dropped", by="owner", reason=why)
    save_json(p.backlog, backlog)
    print(f"{pid} -> dropped ({why})")
    return 0


def cmd_reprioritise(p: Paths, pid: str, why: str, score: float | None, now: dt.datetime) -> int:
    """Owner ``reprioritise XP-<n> <why> [--score X]``: pin it to the top, or override its
    score. The Analyst keeps the pin until the owner reprioritises again."""
    backlog, entry, refusal = _owner_entry(p, pid, "reprioritise", now)
    if entry is None:
        print(refusal)
        return 2
    if not why.strip():
        print("reprioritise needs a reason")
        return 2
    if entry["status"] not in BACKLOG_RANKED:
        print(f"{pid} is {entry['status']}; only proposed/approved entries are ranked")
        return 2
    if score is None:
        entry["pinned"] = {"at": iso(now), "reason": why}
        _hist(entry, iso(now), "pinned", by="owner", reason=why)
    else:
        entry["score_override"] = {"value": score, "reason": f"owner: {why}"}
        _hist(entry, iso(now), "score_override", by="owner", value=score, reason=why)
    save_json(p.backlog, backlog)
    rank = [e["id"] for e in ranked_backlog(backlog, kind=None)].index(pid) + 1
    print(f"{pid} -> rank #{rank} ({why})")
    return 0


def cmd_backlog(p: Paths) -> int:
    used = used_xp_numbers(p)
    migrate = _needs_migration(p.backlog)
    backlog = load_backlog(p.backlog, used)
    if migrate:
        save_json(p.backlog, backlog)  # D121: the XP ids shown are the ones kept
    flag_needs_rewrite(backlog, "")  # in memory: show a K=4 draft as needing a rewrite
    ranked = ranked_backlog(backlog, kind=None)  # D121: XP and BT in one ranking
    if not ranked:
        print("backlog: no open proposals")
    for i, e in enumerate(ranked, 1):
        if is_study(e):
            what = "backtest study" if is_bt(e) else "ML study"
            print(f"#{i} {e['id']} [{e['status']}] {what}: {e.get('title')}")
        else:
            spec = e.get("spec") or {}
            print(f"#{i} {e['id']} [{e['status']}] {spec.get('area')}: {spec.get('title')}")
        print(f"   {value_formula(e)}")
    others = [
        e
        for e in backlog["items"].values()
        if e.get("status") not in BACKLOG_RANKED or e.get("needs_rewrite")
    ]
    for e in sorted(others, key=lambda e: (entry_kind(e), _id_num(e["id"]), e["id"])):
        flag = " NEEDS REWRITE (D108: 3 treatments)" if e.get("needs_rewrite") else ""
        title = e.get("title") if is_study(e) else (e.get("spec") or {}).get("title")
        print(f"   {e['id']} [{e['status']}]{flag} {title}")
    # D121: what the next xp-proposal reserves (registry, specs, open xp/<n> PRs, backlog)
    print(f"next proposal id: {reserve_proposal_id(dict(backlog), used)} (reserved at record)")
    return 0


def cmd_reset(p: Paths) -> int:
    state = load_json(p.state, {})
    for k in ("closed_watermark", "last_run_at", "bt_holdouts_seen", "xp_verdicts_seen",
              "verdict_run"):  # fmt: skip
        state.pop(k, None)
    save_json(p.state, state)
    print("next run reviews unconditionally (if the journal has a closed outcome)")
    return 0


def cmd_check_report(path: Path, mode: str = "weekly") -> int:
    text = path.read_text()
    errs = validate_verdict_report(text) if mode == "verdict" else validate_report(text)
    if errs:
        print("report rejected:\n- " + "\n- ".join(errs))
        return 2
    print(f"report ok ({len(text)} chars)")
    return 0


def cmd_check_seed(path: Path) -> int:
    if not path.exists():
        print(f"idea seed missing: {path}")
        return 2
    ideas, errs = load_seed(path)
    if errs:
        print("idea seed rejected:\n- " + "\n- ".join(errs))
        return 2
    print(f"idea seed ok ({len(ideas)} leads: {', '.join(x['id'] for x in ideas)})")
    return 0


# ---------- gate (cron pre-run) ----------


def clean_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(SECRET_PREFIXES)}
    env.pop("VIRTUAL_ENV", None)
    env["ARC_ENV"] = "paper"
    return env


def run_arc(p: Paths, args: list[str], run_dir: Path, name: str, timeout: int = 180) -> str:
    """One read-only ``arc`` view against the COPY; full output to RUN_DIR/<name>.log."""
    try:
        r = subprocess.run(
            [*p.arc, *args],
            cwd=p.repo,
            env=clean_env(),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        out, code = r.stdout, r.returncode
        if code:
            out += "\n" + r.stderr
    except subprocess.TimeoutExpired:
        out, code = f"TIMEOUT after {timeout}s", 124
    except FileNotFoundError as e:
        out, code = str(e), 127
    (run_dir / f"{name}.log").write_text(out)
    return out if code == 0 else f"(exit {code}; see {run_dir}/{name}.log)\n{clip(out, 800)}"


def closed_this_week(conn: sqlite3.Connection, since: dt.datetime) -> list[str]:
    rows = conn.execute(
        f"""SELECT proposal_hash FROM outcomes WHERE {closed_filter()} AND at >= ?
            UNION SELECT open_proposal_hash FROM open_structures
            WHERE status = 'closed' AND closed_at >= ?""",
        (to_db(since), to_db(since)),
    ).fetchall()
    return sorted({r[0] for r in rows})


def newest_research(repo: Path) -> tuple[str, str]:
    docs = sorted((repo / "docs" / "RESEARCH").glob("*.md"), key=lambda q: q.stat().st_mtime)
    if not docs:
        return "(none)", ""
    names = ", ".join(d.name for d in reversed(docs[-6:]))
    return names, clip(docs[-1].read_text(), 2500)


def max_parallel_arms(conn: sqlite3.Connection, repo: Path) -> int:
    """Effective ``experiments.runner.max_parallel_arms``: the latest applied D26 override,
    else the YAML default, else 10 (D69)."""
    try:
        row = conn.execute(
            "SELECT new FROM config_changes WHERE key = ? AND status = 'applied' "
            "ORDER BY id DESC LIMIT 1",
            (MAX_ARMS_KEY,),
        ).fetchone()
    except sqlite3.Error:
        row = None
    if row and row[0] is not None:
        try:
            return int(json.loads(row[0]))
        except (ValueError, TypeError):
            pass
    path = repo / "config" / "experiments.yaml"
    if path.exists():
        m = re.search(r"^\s*max_parallel_arms:\s*(\d+)", path.read_text(), re.MULTILINE)
        if m:
            return int(m.group(1))
    return MAX_PARALLEL_ARMS_DEFAULT


def aa_doc_sigma(repo: Path) -> float | None:
    """The documented A/A sigma (fraction of t0 equity) from ``AA_DOC``, or None."""
    path = repo / AA_DOC
    if not path.exists():
        return None
    m = AA_DOC_SIGMA_RE.search(path.read_text())
    return float(m.group(1)) / 100 if m else None


def run_plan(p: Paths, db: list[str], run_dir: Path) -> str:
    """``arc experiment plan --k 3 --effects … --json`` (K_TREATMENTS, D108) at the stored
    A/A sigma; with none stored, at the sigma ``AA_DOC`` documents (source labelled)."""
    argv = [
        "experiment",
        "plan",
        "--k",
        str(K_TREATMENTS),
        "--effects",
        ",".join(str(e) for e in PLAN_EFFECTS),
        "--json",
    ]
    text = run_arc(p, [*argv, *db], run_dir, PLAN_VIEW)
    if "no A/A sigma" not in text:
        return text
    sigma = aa_doc_sigma(p.repo)
    if sigma is None:
        return text
    out = run_arc(p, [*argv, "--sigma", repr(sigma), *db], run_dir, PLAN_VIEW)
    try:
        d = json.loads(out)
    except ValueError:
        return out
    d["sigma_source"] = f"{AA_DOC} (no A/A sigma in the store)"
    return json.dumps(d)


def plan_lines(text: str) -> list[str]:
    """The K=3 sessions-needed grid from ``arc experiment plan --k 3 --effects … --json``."""
    try:
        d = json.loads(text)
    except ValueError:
        return [f"- unavailable: {clip(text, 400)}"]
    out = [
        f"- sigma {_pct(d.get('sigma'))}/day ({d.get('sigma_source')}), alpha {d.get('alpha')} "
        f"Holm, power {d.get('power')}, window {d.get('min_sessions')}-{d.get('max_sessions')} "
        f"sessions, K={d.get('k')}"
    ]
    lo = d.get("min_sessions") or 0
    for g in d.get("effects") or []:
        need = g.get("sessions_needed")
        if not g.get("fits_window"):
            note = " (> max_sessions: too small to detect)"
        elif isinstance(need, int) and need < lo:
            note = f" (runs at least min_sessions: use {lo})"
        else:
            note = ""
        out.append(f"- effect {_pct(g.get('effect'))}/day -> {need} sessions{note}")
    return out


def regime_panel_lines(text: str, run_dir: Path) -> list[str]:
    """E25.5 (D108 (c)): the pooled P - C / Ti - C by regime across v3 XPs, compact.

    Descriptive context only: never a verdict, stop or promotion input, and a ``thin``
    bucket (n < 10) is not evidence for a proposal on its own."""
    try:
        d = json.loads(text)
    except ValueError:
        return [f"- unavailable: {clip(text, 400)}"]
    if not isinstance(d, dict) or d.get("panel_version") != REGIME_PANEL_VERSION:
        return [f"- unsupported regime panel shape (want v{REGIME_PANEL_VERSION})"]
    if not d.get("sessions"):
        return ["- no v3 sessions yet"]
    out = [
        f"- XPs {', '.join(d.get('xps') or [])}; sessions {d['sessions']}; "
        f"untagged (unknown) {d.get('unknown_sessions', 0)}; thin = n < {d.get('thin_n')}",
    ]

    def mc(m: dict | None) -> str:
        if not m or m.get("mean") is None:
            return "n/a"
        ci = m.get("ci") or {}
        return _pct(m["mean"]) + (f" [{_pct(ci.get('lo'))}, {_pct(ci.get('hi'))}]" if ci else "")

    rows = [
        f"- {b['grouping']}={b['key']}: n {b['n']}{' THIN' if b.get('thin') else ''} "
        f"P-C {mc(b.get('d_p_c'))} residual {mc(b.get('residual'))} "
        f"Ti-C(all) {mc((b.get('ti_minus_c') or {}).get('all'))}"
        for b in d.get("buckets") or []
    ]
    out += capped_section(rows, REGIME_SECTION_MAX, run_dir / "regime-buckets.log")
    for s in d.get("starts") or []:
        out.append(
            f"- start {s['experiment_id']} t0 {str(s.get('t0'))[:10]} regime "
            f"{s.get('start_regime')}: {s.get('sessions')} sessions, cum P-C "
            f"{_pct(s.get('cum_d_p_c'))}, residual {_pct(s.get('cum_residual'))}, ramped "
            f"{s.get('ramped_from') or 'not yet'} [{s.get('status_verdict')}]"
        )
    return out


def merged_pr_lines(p: Paths, since: str | None, run_dir: Path) -> list[str]:
    """Strategy PRs merged since the last run with their XP-advisory / lane lines (D86): an
    advisory-flagged PR is a lead for a proposal. Read-only ``gh``; skipped without it."""
    if not p.gh or not since:
        return ["- skipped (no gh, or first run)"]
    day = since[:10]
    try:
        r = subprocess.run(
            [
                *p.gh,
                "pr",
                "list",
                "--state",
                "merged",
                "--search",
                f"merged:>={day}",
                "--limit",
                "50",
                "--json",
                "number,title,body,mergedAt",
            ],
            cwd=p.repo,
            env=clean_env(),
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        text, code = r.stdout, r.returncode
        if code:
            text += "\n" + r.stderr
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        text, code = str(e), 1
    (run_dir / "merged-prs.log").write_text(text)
    if code:
        return [f"- gh failed (exit {code}; see {run_dir}/merged-prs.log)"]
    try:
        prs = json.loads(text)
    except ValueError:
        return ["- gh returned no JSON"]
    out = []
    for pr in sorted(prs, key=lambda x: x.get("mergedAt") or ""):
        lines = [m.group(0).strip() for m in MERGED_PR_LINE_RE.finditer(pr.get("body") or "")]
        if not lines:
            continue
        out.append(
            f"- #{pr['number']} {clip(pr['title'], 90)} (merged {pr.get('mergedAt', '')[:10]}): "
            + " | ".join(clip(x, 160) for x in lines[:3])
        )
    return out or ["- none with an XP-advisory / lane line"]


def ledger_lines(ledger: dict) -> list[str]:
    items = ledger.get("items", {})
    out = []
    for it in sorted(items.values(), key=lambda x: int(x["id"][2:])):
        if it["status"] in ACTIVE or it["status"] == "wontfix":
            out.append(
                f"- {it['id']} [{it['status']}] key={it['key']} sev={it.get('severity')} "
                f"n={it.get('n')} {it.get('title', '')}"
            )
    return out or ["- (empty: first run)"]


def theme_lines(state: dict) -> list[str]:
    prev = state.get("themes") or {}
    return [
        f"- {tid}: {desc} :: last status "
        f"{(prev.get(tid) or {}).get('status', 'none')} "
        f"{(prev.get(tid) or {}).get('note', '')}".rstrip()
        for tid, desc in THEMES.items()
    ]


def build_context(
    p: Paths,
    run_dir: Path,
    copy: Path,
    decision: dict,
    now: dt.datetime,
    bt: dict | None = None,
    ml: dict | None = None,
) -> str:
    bt = bt if bt is not None else backtest_snapshot(p, run_dir)
    ml = ml if ml is not None else ml_snapshot(p)  # E27.8 (D133)
    since = now - dt.timedelta(days=WINDOW_DAYS)
    since_day = since.astimezone(ET).date().isoformat()
    week_day = now.astimezone(ET).date().isoformat()
    # --until is exclusive at the ET day start: pin it to the gate's own clock, not the views'.
    until_day = (now.astimezone(ET).date() + dt.timedelta(days=1)).isoformat()
    db = ["--db", str(copy)]

    cfg_dir = run_dir / "config"
    cfg_dir.mkdir(exist_ok=True)
    for y in sorted((p.repo / "config").glob("*.yaml")):
        shutil.copy2(y, cfg_dir / y.name)

    # E22.5 (D97): the sleeve windows of the effective profile, from the arc view
    sleeves_view = run_arc(p, ["scorecard", "sleeves", "--json", *db], run_dir, "sleeves-all")
    windows = sleeve_windows(sleeves_view)
    conn = sqlite3.connect(f"{copy.resolve().as_uri()}?mode=ro", uri=True)
    try:
        week_rows = realised_vs_model(conn, since, windows)
        all_rows = realised_vs_model(conn, None, windows)
        closed = closed_this_week(conn, since)
        halts = conn.execute(
            "SELECT at, kind, reason, cleared_at FROM halts WHERE at >= ? OR cleared_at IS NULL "
            "ORDER BY at",
            (to_db(since),),
        ).fetchall()
        closed_week_n = sum(r["n"] for r in week_rows)
        overview = experiments_overview(conn)
        max_arms = max_parallel_arms(conn, p.repo)
    finally:
        conn.close()

    frac = slippage_frac(p.repo / "config" / "costs.yaml")
    views = {
        "attribution-7d": run_arc(
            p,
            [
                "scorecard",
                "attribution",
                "--since",
                since_day,
                "--until",
                until_day,
                "--by",
                "kind,regime",
                "--min-sample",
                str(MIN_SAMPLE),
                *db,
            ],
            run_dir,
            "attribution-7d",
        ),
        "attribution-all": run_arc(
            p,
            [
                "scorecard",
                "attribution",
                "--until",
                until_day,
                "--by",
                "kind,regime,persona_model",
                "--min-sample",
                str(MIN_SAMPLE),
                *db,
            ],
            run_dir,
            "attribution-all",
        ),
        PLAN_VIEW: run_plan(p, db, run_dir),
        REGIME_VIEW: run_arc(
            p, ["experiment", "regime-panel", "--json", *db], run_dir, REGIME_VIEW
        ),
        "scorecard-week": run_arc(
            p, ["journal", "scorecard", "--week", week_day, *db], run_dir, "scorecard-week"
        ),
        "gaps-7d": run_arc(
            p,
            ["journal", "gaps", "--since", since_day, "--data-dir", str(p.repo / "data"), *db],
            run_dir,
            "gaps-7d",
        ),
        "counterfactual-7d": run_arc(
            p,
            [
                "journal",
                "counterfactual",
                "--since",
                since_day,
                "--until",
                until_day,
                "--data-dir",
                str(p.repo / "data"),
                *db,
            ],
            run_dir,
            "counterfactual-7d",
        ),
        "config-diff": run_arc(p, ["config", "diff", *db], run_dir, "config-diff"),
        "config-history": run_arc(
            p, ["config", "history", "--limit", "30", *db], run_dir, "config-history"
        ),
        "config-show": run_arc(p, ["config", "show", *db], run_dir, "config-show"),
    }
    shows = [
        (h, run_arc(p, ["journal", "show", h, *db], run_dir, f"show-{h[:12]}")) for h in closed
    ]
    research_names, research_head = newest_research(p.repo)
    ledger = load_json(p.ledger, {"next_id": 1, "items": {}})
    state = load_json(p.state, {})
    seed, seed_errs = load_seed(p.idea_seed)
    used = used_xp_numbers(p, [e["experiment_id"] for e in overview])
    backlog = load_backlog(p.backlog, used, iso(now))  # D121: v1 P-<n> entries get XP ids
    migrated = int((load_json(p.backlog, None) or {}).get("version") or 1) < BACKLOG_VERSION
    synced = sync_backlog(backlog, overview, iso(now))
    synced += rereserve_collisions(backlog, used, iso(now))
    synced += flag_needs_rewrite(backlog, iso(now))  # D108: K=4 drafts are flagged, not trimmed
    synced += sync_bt_queue(backlog, load_bt_queue(p.bt_root / BT_QUEUE), iso(now))  # E26.2
    if synced or (migrated and p.backlog.exists()):
        save_json(p.backlog, backlog)  # registry is the truth for registered/running/concluded
    merged = merged_pr_lines(p, state.get("last_run_at"), run_dir)

    out = [
        f"RUN_DIR={run_dir}",
        f"DB_COPY={copy}",
        f"NOW={iso(now)}",
        f"WINDOW={since_day}..{week_day} (ET, {WINDOW_DAYS}d)",
        f"CLOSED_TRADES: week={closed_week_n} all_time={sum(r['n'] for r in all_rows)} "
        f"(MIN-SAMPLE n>={MIN_SAMPLE} per bucket)",
        f"WAKE: {'; '.join(decision['reasons'])}",
        "",
        "## Realised vs model, this week (kind x regime x DTE sleeve)",
        *table_lines(week_rows, frac),
        "",
        "## Realised vs model, all time (kind x regime x DTE sleeve)",
        *table_lines(all_rows, frac),
        "",
        "## DTE sleeves, all time (E22.5, D97: short vs swing; sleeve derived from entry DTE, "
        f"full: {run_dir}/sleeves-all.log)",
        *sleeve_lines(sleeves_view),
        "",
        "## Backtest <-> live drift (E7.14 reconciliation: simulator vs real fills; a persistent "
        "drift is a cost-model theme line or a -> Sentinel flaw, not a strategy change)",
        *drift_lines(p.bt_root / BT_RECONCILIATION),
        "",
        f"## Backtest studies (D84; E7.12 ledger {BT_LEDGER} + {BT_REPORTS}; next free id "
        f"{reserve_study_id(backlog, study_floor(bt['studies'], bt['next_study_id']))} (a new "
        "study_spec reserves it; a re-listed draft keeps its BT id); cite only a C500 holdout verdict, a C125-only or dev-only "
        "result is a hypothesis)",
        *[f"- WARNING: {e}" for e in bt.get("errors") or []],
        *retired_lines(bt["studies"]),
        *capped_section(
            study_lines(current_studies(bt["studies"])),
            BT_SECTION_MAX,
            run_dir / "backtest-studies.log",
        ),
        f"## Backtest queue (E26.2, D121: {BT_QUEUE}; approved studies run one at a time "
        "nightly 21:30-08:30 ET, c125 dev -> c500 dev -> one c500 holdout; at most "
        f"{BT_WEEKLY_CAP} owner approvals per ISO week)",
        *bt_queue_lines(*bt_queue_view(p, run_dir)),
        "### Data checkpoints (E7.9 checkpoints.json)",
        *checkpoint_lines(p.bt_root / BT_CHECKPOINTS),
        "",
        f"## ML models (E27.3 registry {ML_MODELS}; D126: dev metrics are model quality, never "
        "P&L evidence; champion -> shadow -> candidate)",
        *capped_section(
            ml_models_lines(ml["models"], [e for e in ml["errors"] if e.startswith("ml ")]),
            ML_SECTION_MAX,
            run_dir / "ml-models.log",
        ),
        f"## ML studies (E27.7, D133: ledger {ML_LEDGER}, shared nightly queue with BT; next "
        f"free id {reserve_ml_id(backlog, ml_floor(ml_snapshot_meta(ml)))} (an ml_study_spec "
        "reserves it; a re-listed draft keeps its ML id); a verdict reads DEV only: a pass "
        "moves the version to shadow, never to champion; cite only an ML-<n> verdict)",
        *capped_section(
            ml_studies_lines(ml, ml_status_view(p, run_dir)),
            ML_SECTION_MAX,
            run_dir / "ml-studies.log",
        ),
        "## ML live (E27.4 nightly ml_health / `arc ml monitor`; shadow scoring is the "
        f"out-of-sample check; n < {ML_MIN_N} is insufficient: never a basis for a claim)",
        *ml_live_lines(ml_live_record(p, run_dir, copy, now.astimezone(ET).date())),
        "",
        "## Halts this week (and any still active)",
    ]
    out += [f"- {a} {k}: {r} (cleared {c or 'NO, still active'})" for a, k, r, c in halts] or [
        "- none"
    ]
    out += [
        "",
        f"## Forward experiments (D44; next free XP id {reserve_proposal_id(dict(backlog), used)} "
        "(D121: a new xp-proposal reserves it when `record` runs); "
        f"full: sqlite3 'file:{copy}?mode=ro' or arc experiment report <id> --stored --db {copy})",
        *experiment_lines(overview),
        "",
        f"## Experiment capacity (D69/D108: one experiment per area; control + K={K_TREATMENTS} "
        f"treatments = {ARMS_PER_XP} arms each)",
        *capacity_lines(overview, max_arms),
        "",
        f"## Sessions needed at K={K_TREATMENTS} (arc experiment plan; read sessions_needed "
        "off this grid)",
        *plan_lines(views[PLAN_VIEW]),
        "",
        "## Regime panel (E25.5, D108: P - C and Ti - C pooled across v3 XPs by SPY regime "
        "and start date; descriptive context, never a verdict, stop or promotion input; "
        f"full JSON: {run_dir}/{REGIME_VIEW}.log)",
        *regime_panel_lines(views[REGIME_VIEW], run_dir),
        "",
        f"## XP backlog (yours; {p.backlog}; ranked by value, owner pins first; "
        f"cap {BACKLOG_CAP} open, <= {MAX_NEW_PROPOSALS} new per run)",
        *([f"- registry sync: {s}" for s in synced] or []),
        *backlog_lines(backlog, overview, max_arms, now),
        "",
        f"## Experiment idea seed (D86: leads from retired dev drafts, not proposals; "
        f"open until proposed (seed_ref) or dropped (seed_drops); full: {p.idea_seed})",
        *seed_lines(seed, seed_errs, backlog.get("seed")),
        "",
        "## Promotion leads (E21.4: winning experiments not promoted yet; propose each as a "
        "quick change)",
        *promotion_leads(overview),
        "",
        "## Quick changes shipped since the last review (review each: keep / roll-back / "
        "too-early)",
        *review_lines(ledger),
        "",
        "## Strategy PRs merged since the last run (XP-advisory lines are proposal leads)",
        *merged,
        "",
        "## Scorecard attribution, this week",
        clip(views["attribution-7d"], 2500),
        "",
        "## Scorecard attribution, all time (kind, regime, persona models)",
        clip(views["attribution-all"], 3000),
        "",
        "## Weekly scorecard (arc journal scorecard)",
        clip(views["scorecard-week"], 4000),
        "",
        "## Journal gaps, this week",
        clip(views["gaps-7d"], 3000),
        "",
        "## Counterfactual, this week",
        clip(views["counterfactual-7d"], 2000),
        "",
        "## Config overrides (D26) and change log",
        clip(views["config-diff"], 1200),
        clip(views["config-history"], 1500),
        "",
        f"## Effective config (full: {run_dir}/config-show.log; YAML: {cfg_dir})",
        clip(views["config-show"], 2500),
        "",
        f"## Trades closed this week ({len(closed)}; journal show for the first "
        f"{MAX_SHOWN_TRADES}, all in {run_dir}/show-*.log)",
    ]
    for h, text in shows[:MAX_SHOWN_TRADES]:
        out += [f"### {h[:12]}", clip(text, 1500)]
    out += [
        "",
        "## Standing themes (status from the last run)",
        *theme_lines(state),
        "",
        "## Ledger (you must account for every ACTIVE item: re-list or resolve)",
        *ledger_lines(ledger),
        "",
        f"## Latest research docs: {research_names}",
        research_head,
    ]
    return "\n".join(out)


def cmd_gate(p: Paths, now: dt.datetime) -> int:
    p.home.mkdir(parents=True, exist_ok=True)
    if not p.live_db.is_file():
        print(f"(no audit store at {p.live_db})")
        print(json.dumps({"wakeAgent": False}))
        return 0
    staging = p.home / "staging-copy.db"
    snapshot_db(p.live_db, staging)
    state = load_json(p.state, {})
    bt_entries, _ = load_bt_ledger(p.bt_root / BT_LEDGER)
    holdouts = live_holdouts(bt_entries, retired_ids(p))  # D127: retired never wakes
    xp_keys = xp_verdict_keys(p.xp_verdicts)
    ml = ml_snapshot(p)  # E27.8 (D133): registry + ML ledger + queue, read-only
    conn = sqlite3.connect(f"{staging.resolve().as_uri()}?mode=ro", uri=True)
    try:
        decision = wake_decision(conn, state, now, holdouts, xp_keys, ml["verdict_keys"])
        experiments = [
            {
                "id": e["experiment_id"],
                "status": e["status"],
                "area": e["area"],
                "arms": e["arms"],
                "proposal_ref": e["proposal_ref"],
            }
            for e in experiments_overview(conn)
        ]
        max_arms = max_parallel_arms(conn, p.repo)
    finally:
        conn.close()
    if not decision["wake"]:
        staging.unlink(missing_ok=True)
        print(f"skip: {'; '.join(decision['reasons'])}")
        print(json.dumps({"wakeAgent": False}))
        return 0
    run_dir = p.runs / now.astimezone(ET).strftime("%Y-%m-%d-%H%M")
    run_dir.mkdir(parents=True, exist_ok=True)
    copy = run_dir / "arc-copy.db"
    staging.replace(copy)
    bt = backtest_snapshot(p, run_dir)
    save_json(
        run_dir / "metrics.json",
        {
            "now": iso(now),
            **{k: v for k, v in decision.items() if k != "wake"},
            "experiments": experiments,  # registry snapshot `record` validates against
            "max_parallel_arms": max_arms,
            # E7.15: the study snapshot `record` resolves BT-<n> refs against, and the
            # holdout keys `mark` stores so the same verdict does not wake the next run
            "backtest": {"studies": bt["studies"], "next_study_id": bt["next_study_id"]},
            "bt_holdouts": holdouts,
            "xp_verdicts": xp_keys,  # E26.4: the XP verdict files `mark` stores as seen
            "ml": ml_snapshot_meta(ml),  # E27.8: what `record` validates ML drafts against
            "ml_verdicts": ml["verdict_keys"],  # E27.8: the ML verdicts `mark` stores
        },
    )
    ctx = build_context(p, run_dir, copy, decision, now, bt, ml)
    (run_dir / "context.md").write_text(ctx)
    tail = f"\n...[truncated; full: {run_dir}/context.md]"
    print(ctx if len(ctx) <= MAX_STDOUT else ctx[: MAX_STDOUT - len(tail)] + tail)
    return 0


# ---------- E26.4 (D121): the short verdict run ----------


def unseen_verdicts(
    state: dict, holdouts: list, xp_keys: list, ml_keys: list | None = None
) -> tuple[list, list, list]:
    """The BT holdout keys, XP verdict-file keys and ML verdict keys (E27.8) neither the
    weekly run nor a verdict run has marked as read (both ``mark`` paths add to the same
    seen lists)."""
    bt_seen = set(state.get("bt_holdouts_seen") or [])
    xp_seen = set(state.get("xp_verdicts_seen") or [])
    ml_seen = set(state.get("ml_verdicts_seen") or [])
    return (
        [h for h in holdouts if h not in bt_seen],
        [k for k in xp_keys if k not in xp_seen],
        [k for k in ml_keys or [] if k not in ml_seen],
    )


def next_weekly_run(now: dt.datetime) -> dt.datetime:
    """The next ``arc-analyst-weekly-audit`` slot at or after *now* (Sunday 14:00 PT)."""
    local = now.astimezone(WEEKLY_TZ)
    day = local.date() + dt.timedelta(days=(WEEKLY_WEEKDAY - local.weekday()) % 7)
    slot = dt.datetime.combine(day, WEEKLY_AT, tzinfo=WEEKLY_TZ)
    return slot if slot >= local else slot + dt.timedelta(days=7)


def verdict_gate_decision(
    state: dict, now: dt.datetime, holdouts: list, xp_keys: list, ml_keys: list | None = None
) -> dict:
    """Wake the short run only on an unseen verdict, at most once per ET day, and never when
    the weekly run (which reads every verdict anyway) is due within ``VERDICT_WEEKLY_GAP``."""
    new_bt, new_xp, new_ml = unseen_verdicts(state, holdouts, xp_keys, ml_keys)
    today = now.astimezone(ET).date().isoformat()
    weekly = next_weekly_run(now)
    out = {"wake": False, "new_holdouts": new_bt, "new_xp_verdicts": new_xp,
           "new_ml_verdicts": new_ml, "day": today, "next_weekly": iso(weekly)}  # fmt: skip
    if not new_bt and not new_xp and not new_ml:
        out["reason"] = "no new backtest holdout verdict or XP verdict file since the last run"
    elif (state.get("verdict_run") or {}).get("day") == today:
        out["reason"] = f"a verdict run already happened today ({today} ET); next one tomorrow"
    elif weekly - now <= VERDICT_WEEKLY_GAP:
        out["reason"] = f"the weekly run is due within 24 h ({iso(weekly)}); it reads them"
    else:
        out["wake"] = True
        out["reason"] = f"new verdict(s): {verdict_ids(new_bt + new_xp + new_ml)}"
    return out


def verdict_refs(text) -> set[str]:
    return set(VERDICT_REF_RE.findall(str(text or "")))


def related_backlog(backlog: dict, ids: set[str]) -> list[dict]:
    """Backlog entries a verdict touches: the entry itself (``BT-<n>``/``XP-<n>``) or one
    that cites it (evidence_ref, the spec's backtest_ref, evidence text)."""
    out = []
    for e in backlog["items"].values():
        spec = e.get("spec") or {}
        cites = {e.get("id"), e.get("evidence_ref"), spec.get("backtest_ref")}
        cites |= verdict_refs(e.get("evidence"))
        if ids & {c for c in cites if c}:
            out.append(e)
    return sorted(out, key=lambda e: (is_bt(e), _id_num(e["id"]), e["id"]))


def xp_verdict_lines(directory: Path, keys: list) -> list[str]:
    out = []
    for k in keys:
        xid = k.split(":", 1)[0]
        path = directory / f"{xid}.yaml"
        text = path.read_text() if path.is_file() else "(file gone since the gate)"
        out += [f"### {xid} ({path})", "```yaml", clip(text.strip(), 2_500), "```"]
    return out


def build_verdict_context(
    p: Paths,
    run_dir: Path,
    decision: dict,
    bt: dict,
    backlog: dict,
    now: dt.datetime,
    ml: dict | None = None,
) -> str:
    ml = ml or {"studies": [], "retired": {}, "next_id": "ML-1"}
    new_ml = decision.get("new_ml_verdicts") or []
    ids = {
        k.split(":", 1)[0] for k in decision["new_holdouts"] + decision["new_xp_verdicts"] + new_ml
    }
    ml_ids = {k.split(":", 1)[0] for k in new_ml}
    bt_ids = {k.split(":", 1)[0] for k in decision["new_holdouts"]}
    studies = [s for s in bt["studies"] if s["id"] in bt_ids]
    related = related_backlog(backlog, ids)
    open_titles = [
        f"- {e['id']} [{e['status']}] "
        f"{e.get('title') if is_study(e) else (e.get('spec') or {}).get('title')}"
        for e in ranked_backlog(backlog, kind=None)
    ]
    lines = [
        f"RUN_DIR={run_dir}",
        "MODE: verdict run (E26.4, D121): read the new verdict(s) below and propose at most "
        "one follow-up XP and one follow-up study (a BT or an ML, D133), each citing a verdict "
        "id. No quick changes, no standing themes, no full review: the weekly run does those.",
        f"VERDICTS: {verdict_ids(decision['new_holdouts'] + decision['new_xp_verdicts'] + new_ml)}",
        f"NEXT WEEKLY RUN: {decision['next_weekly']}",
        "DELIVERY: top-level post in #arc-analyst (the runner's message ts is not stored, so "
        "the reply cannot be threaded under it)",
        "",
        "## New backtest holdout verdicts (D84 ledger; C500 holdout = citable evidence)",
        *(study_lines(studies) or ["- none"]),
        "",
        f"## New XP verdict files ({p.xp_verdicts})",
        *(xp_verdict_lines(p.xp_verdicts, decision["new_xp_verdicts"]) or ["- none"]),
        "",
        f"## New ML study verdicts (E27.7 ledger {ML_LEDGER}; dev only: a pass moves the "
        "version to shadow, never to champion)",
        *(
            ml_studies_lines({"studies": [s for s in ml["studies"] if s["id"] in ml_ids]})
            if ml_ids
            else ["- none"]
        ),
        "",
        "## Backlog entries these verdicts touch (status changes go in backlog_drops)",
        *(
            [f"- {e['id']} [{e['status']}] key={e['key']} | {value_formula(e)}" for e in related]
            or ["- none"]
        ),  # fmt: skip
        "",
        f"## Open backlog (ids only; do not re-propose these; cap {BACKLOG_CAP} XP)",
        *(open_titles or ["- empty"]),
        f"- next free study id {bt['next_study_id']}",
        f"- next free ML study id {reserve_ml_id(backlog, ml_floor(ml_snapshot_meta(ml)))}",
        *retired_lines(bt["studies"]),
    ]
    return "\n".join(lines) + "\n"


def cmd_gate_verdict(p: Paths, now: dt.datetime) -> int:
    """``gate --mode verdict`` (cron ``arc-analyst-verdict-check``). Reads no audit store:
    the verdict run works from the research ledger, the verdict files and the backlog."""
    p.home.mkdir(parents=True, exist_ok=True)
    state = load_json(p.state, {})
    bt_entries, _ = load_bt_ledger(p.bt_root / BT_LEDGER)
    holdouts = live_holdouts(bt_entries, retired_ids(p))  # D127: retired never wakes
    xp_keys = xp_verdict_keys(p.xp_verdicts)
    ml = ml_snapshot(p)  # E27.8: an unseen ML study verdict also wakes the short run
    decision = verdict_gate_decision(state, now, holdouts, xp_keys, ml["verdict_keys"])
    if not decision["wake"]:
        print(f"skip: {decision['reason']}")
        print(json.dumps({"wakeAgent": False}))
        return 0
    run_dir = p.runs / (now.astimezone(ET).strftime("%Y-%m-%d-%H%M") + VERDICT_RUN_SUFFIX)
    run_dir.mkdir(parents=True, exist_ok=True)
    bt = backtest_snapshot(p, run_dir)
    used = used_xp_numbers(p)
    backlog = load_backlog(p.backlog, used, iso(now))
    save_json(
        run_dir / "metrics.json",
        {
            "mode": "verdict",
            "now": iso(now),
            "reason": decision["reason"],
            "next_weekly": decision["next_weekly"],
            "verdict_ids": sorted(
                {
                    k.split(":", 1)[0]
                    for k in decision["new_holdouts"]
                    + decision["new_xp_verdicts"]
                    + decision["new_ml_verdicts"]
                }
            ),  # fmt: skip
            "bt_holdouts": decision["new_holdouts"],
            "xp_verdicts": decision["new_xp_verdicts"],
            "ml_verdicts": decision["new_ml_verdicts"],
            "backtest": {"studies": bt["studies"], "next_study_id": bt["next_study_id"]},
            "ml": ml_snapshot_meta(ml),
        },
    )
    state["verdict_run"] = {"day": decision["day"], "run_dir": str(run_dir), "at": iso(now)}
    save_json(p.state, state)  # once per ET day, even if the agent fails to mark
    ctx = build_verdict_context(p, run_dir, decision, bt, backlog, now, ml)
    (run_dir / "context.md").write_text(ctx)
    print(f"WAKE: {decision['reason']}")
    print(clip(ctx, MAX_STDOUT))
    return 0


def validate_verdict_findings(
    doc: dict,
    backlog: dict,
    verdict_id_list: list,
    studies: list | None = None,
    used=(),
    ml: dict | None = None,
) -> list[str]:
    """The narrower E26.4 contract: ``mode: verdict``, every verdict read, at most one new
    XP and one new study proposal (a BT or an ML, D133; each citing a verdict of this run),
    drops of related entries only. Anything weekly-only (quick changes, themes, review
    lists) is refused."""
    errs: list[str] = []
    studies = studies or []
    vids = set(verdict_id_list)
    for k in sorted(set(doc) - set(VERDICT_DOC_KEYS)):
        errs.append(
            f"`{k}` is not allowed in a verdict run (no quick changes, standing themes or "
            "full review; the weekly run does those)"
        )
    if doc.get("mode") != "verdict":
        errs.append("mode must be 'verdict'")
    reads = {
        r.get("id"): r for r in doc.get("verdicts") or [] if isinstance(r, dict) and r.get("id")
    }
    for vid in sorted(vids - set(reads), key=lambda s: (s[:2], int(s[3:]))):
        errs.append(f"verdict {vid} has no `verdicts` entry (id + read)")
    for vid, r in reads.items():
        if vid not in vids:
            errs.append(f"verdicts: {vid} is not a new verdict of this run ({sorted(vids)})")
        if not str(r.get("read", "")).strip():
            errs.append(f"verdicts: {vid} needs a `read` (what the verdict says, with N)")
    findings = doc.get("findings") or []
    keys = [f.get("key") for f in findings]
    if len(keys) != len(set(keys)):
        errs.append("duplicate keys in findings")
    n_xp = n_bt = 0
    for i, f in enumerate(findings):
        cat = f.get("category")
        if cat == "quick-change" or "quick_change" in f:
            errs.append(f"findings[{i}] quick changes are not allowed in a verdict run")
            continue
        if cat not in VERDICT_CATEGORIES:
            errs.append(
                f"findings[{i}] category {cat!r}: a verdict run only proposes "
                f"({', '.join(VERDICT_CATEGORIES)}); the weekly run reviews the rest"
            )
            continue
        errs += _finding_shape_errs(f, i)
        cited = (
            verdict_refs(f.get("evidence"))
            | verdict_refs((f.get("proposal") or {}).get("evidence_ref"))
            | verdict_refs((f.get("ml_study_spec") or {}).get("hypothesis"))
        )
        if not cited & vids:
            errs.append(f"findings[{i}] must cite a verdict of this run ({sorted(vids)}) in "
                        "its evidence")  # fmt: skip
        if cat == "xp-proposal":
            if f.get("key") in backlog["items"]:
                errs.append(f"findings[{i}] {f['key']} is already in the backlog: edits wait "
                            "for the weekly run (backlog_drops for a status change)")  # fmt: skip
            n_xp += 1
            errs += validate_proposal(f, i, backlog, frozenset(), studies)
        elif "proposal" in f:
            errs.append(f"findings[{i}] `proposal` is only allowed on an xp-proposal")
        if "study_spec" in f:
            if BT_KEY_PREFIX + str(f.get("key")) in backlog["items"]:
                errs.append(f"findings[{i}] study_spec on {f['key']}: already in the backlog")
            n_bt += 1
            errs += validate_study_spec(f["study_spec"], f"findings[{i}] study_spec")
            errs += validate_study_value(f.get("study_value"), f"findings[{i}] study_value")
        elif "ml_study_spec" in f:  # E27.8: the follow-up study may be an ML-<n> (D133)
            if ML_KEY_PREFIX + str(f.get("key")) in backlog["items"]:
                errs.append(f"findings[{i}] ml_study_spec on {f['key']}: already in the backlog")
            n_bt += 1
            errs += validate_ml_finding(f, i, backlog, ml)
        elif "study_value" in f:
            errs.append(f"findings[{i}] `study_value` is only allowed with a study_spec")
        elif cat != "xp-proposal":
            errs.append(f"findings[{i}] a {cat} finding in a verdict run carries a study_spec "
                        "or ml_study_spec (the follow-up study); anything else waits for the "
                        "weekly run")  # fmt: skip
    if n_xp > VERDICT_MAX_NEW:
        errs.append(f"{n_xp} new XP proposals: at most {VERDICT_MAX_NEW} per verdict run")
    if n_bt > VERDICT_MAX_NEW:
        errs.append(
            f"{n_bt} new study proposals (BT/ML): at most {VERDICT_MAX_NEW} per verdict run"
        )
    errs += check_ml_citations(findings, ml)
    errs += _validate_backlog_moves(doc, findings, backlog, frozenset())
    related_ids = {e["id"] for e in related_backlog(backlog, vids)}
    for j, d in enumerate(doc.get("backlog_drops") or []):
        pid = str((d or {}).get("id") or "") if isinstance(d, dict) else ""
        entry = backlog_by_id(backlog, pid) if pid else None
        cites = verdict_refs(d.get("reason")) & vids if isinstance(d, dict) else set()
        if entry is not None and entry["id"] not in related_ids and not cites:
            errs.append(
                f"backlog_drops[{j}] {entry['id']} is not related to this run's verdicts: "
                "cite the verdict in the reason or leave it to the weekly run"
            )
    if not errs:
        after = json.loads(json.dumps(backlog))
        apply_backlog(after, doc, "check", "", used=used)
        n_open = sum(
            1
            for e in after["items"].values()
            if e.get("status") in BACKLOG_OPEN and not is_study(e)
        )
        if n_open > BACKLOG_CAP:
            errs.append(f"backlog would hold {n_open} open proposals; cap is {BACKLOG_CAP}: "
                        "drop the weakest related entry or leave it to the weekly run")  # fmt: skip
    return errs


def cmd_record_verdict(p: Paths, run_dir: Path, now: dt.datetime) -> int:
    meta = load_json(run_dir / "metrics.json", {})
    if meta.get("mode") != "verdict":
        print(f"refusing: {run_dir} is not a verdict run (`gate --mode verdict` makes one)")
        return 2
    doc = json.loads((run_dir / "findings.json").read_text())
    ledger = load_json(p.ledger, {"next_id": 1, "items": {}})
    used = used_xp_numbers(p)
    backlog = load_backlog(p.backlog, used, iso(now))
    bt_meta = meta.get("backtest") or {}
    studies = bt_meta.get("studies") or []
    errs = validate_verdict_findings(
        doc, backlog, meta.get("verdict_ids") or [], studies, used, meta.get("ml")
    )
    ml_floor_n = ml_floor(meta.get("ml"), ml_pr_numbers(p))
    if not errs:
        errs = ml_cli_validate(p, run_dir, pending_ml_drafts(backlog, doc, ml_floor_n))
    if errs:
        print("findings.json rejected (ledger and backlog unchanged):\n- " + "\n- ".join(errs))
        return 2
    report = reconcile(ledger, doc, run_dir.name, iso(now))
    items = ledger["items"]
    finding_ids = {k: it["id"] for k, it in items.items()}
    floor = study_floor(studies, bt_meta.get("next_study_id"))
    study_specs = write_study_drafts(run_dir, backlog, doc, items, finding_ids, floor, now)
    study_specs += write_ml_drafts(run_dir, backlog, doc, items, finding_ids, ml_floor_n, now)
    save_json(p.ledger, ledger)
    moves = apply_backlog(backlog, doc, run_dir.name, iso(now), finding_ids, used)
    sync_bt_queue(backlog, load_bt_queue(p.bt_root / BT_QUEUE), iso(now))
    save_json(p.backlog, backlog)
    save_json(
        run_dir / "reconciled.json",
        {**report, "backlog": moves, "study_specs": study_specs, "mode": "verdict"},
    )
    out: dict = {k: [x["id"] for x in v] for k, v in report.items()}
    out["backlog"] = moves
    out["study_specs"] = study_specs
    print(json.dumps(out, indent=2))
    return 0


def mark_verdict_run(p: Paths, run_dir: Path, meta: dict, now: dt.datetime) -> int:
    """Mark the verdicts this run read as seen; the weekly watermark (closed outcomes,
    last_run_at) is untouched, so the weekly run still reviews the whole week."""
    state = load_json(p.state, {})
    state["bt_holdouts_seen"] = sorted(
        set(state.get("bt_holdouts_seen") or []) | set(meta.get("bt_holdouts") or [])
    )
    state["xp_verdicts_seen"] = sorted(
        set(state.get("xp_verdicts_seen") or []) | set(meta.get("xp_verdicts") or [])
    )
    state["ml_verdicts_seen"] = sorted(
        set(state.get("ml_verdicts_seen") or []) | set(meta.get("ml_verdicts") or [])
    )
    run = state.get("verdict_run") or {}
    run.update(marked_run_dir=str(run_dir), marked_at=iso(now))
    state["verdict_run"] = run
    save_json(p.state, state)
    read = ", ".join(meta.get("verdict_ids") or []) or "-"
    print(f"verdicts read: {read} (weekly watermark kept)")
    return 0


def validate_verdict_report(text: str) -> list[str]:
    errs = []
    if len(text) > VERDICT_REPORT_MAX:
        errs.append(f"report is {len(text)} chars; a verdict run's cap is {VERDICT_REPORT_MAX}")
    for s in VERDICT_REPORT_SECTIONS:
        if s not in text:
            errs.append(f"report lacks {s!r}")
    for weekly in ("*Quick changes*", "Standing themes"):
        if weekly in text:
            errs.append(f"{weekly!r} belongs to the weekly report, not a verdict run")
    return errs


def main(argv: list[str], p: Paths | None = None, now: dt.datetime | None = None) -> int:
    p = p or Paths.from_env()
    now = now or dt.datetime.now(dt.timezone.utc)
    if not argv:
        return cmd_gate(p, now)
    cmd, *rest = argv
    verdict = rest[-2:] == ["--mode", "verdict"]
    if verdict:
        rest = rest[:-2]
    if cmd == "gate" and not rest:
        return cmd_gate_verdict(p, now) if verdict else cmd_gate(p, now)
    if cmd == "record" and len(rest) == 1:
        if verdict:
            return cmd_record_verdict(p, Path(rest[0]), now)
        return cmd_record(p, Path(rest[0]), now)
    if cmd == "check-report" and len(rest) == 1:
        return cmd_check_report(Path(rest[0]), "verdict" if verdict else "weekly")
    if verdict:
        print(__doc__)
        return 64
    if cmd == "mark" and len(rest) == 1:
        return cmd_mark(p, Path(rest[0]), now)
    if cmd == "triage" and len(rest) in (2, 3):
        return cmd_triage(p, rest[0], rest[1], rest[2] if len(rest) == 3 else "", now)
    if cmd == "ship" and len(rest) in (1, 2):
        return cmd_ship(p, rest[0], rest[1] if len(rest) == 2 else "", now)
    if cmd == "approve" and "--approval" in rest[:-1]:
        i = rest.index("--approval")
        approval, args = rest[i + 1], rest[:i] + rest[i + 2 :]
        if len(args) in (1, 2):
            return cmd_approve(p, args[0], args[1] if len(args) == 2 else "", approval, now)
    if cmd == "approve" and len(rest) in (1, 2):
        return cmd_approve(p, rest[0], rest[1] if len(rest) == 2 else "", "", now)
    if cmd == "drop" and len(rest) == 2:
        return cmd_drop(p, rest[0], rest[1], now)
    if cmd == "stop" and len(rest) == 2:
        return cmd_stop(p, rest[0], rest[1], now)
    if cmd == "reprioritise" and len(rest) in (2, 4) and (len(rest) == 2 or rest[2] == "--score"):
        try:
            score = float(rest[3]) if len(rest) == 4 else None
        except ValueError:
            print("--score must be a number")
            return 2
        return cmd_reprioritise(p, rest[0], rest[1], score, now)
    if cmd == "backlog" and not rest:
        return cmd_backlog(p)
    if cmd == "reset" and not rest:
        return cmd_reset(p)
    if cmd == "check-seed" and len(rest) <= 1:
        return cmd_check_seed(Path(rest[0]) if rest else p.idea_seed)
    print(__doc__)
    return 64


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
