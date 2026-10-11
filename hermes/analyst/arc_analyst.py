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
     realised-vs-model table by structure kind x regime, list this week's halts,
     the forward experiments (D44, E10.6: status, hash lock, latest stored
     ``ExperimentReport`` with arm / regime / structure breakdowns, queue per
     area), the newest ``docs/RESEARCH/*.md`` and the A-id ledger, and print the
     context (<= 30k chars; full text in ``RUN_DIR/context.md``).
     E21.3 (D86) adds: the XP backlog (``P-<n>``, statuses synced against the
     registry by ``proposal_ref``), ranked by value; arm capacity per area; the
     sessions-needed grid at K=4 (``arc experiment plan --k 4``); the idea-seed leads
     not yet proposed or dropped; quick changes shipped since the last run (to
     review); and strategy PRs merged since the last run with their XP-advisory line.
     E7.15 (D84) adds, read-only: the backtest studies (``data/backtest/ledger.jsonl`` +
     ``docs/RESEARCH/studies/*.json``, each spec's hash from ``arc backtest study show
     --json``), the E7.9 checkpoints and the E7.14 backtest<->live drift. A holdout
     verdict not seen at the last ``mark`` also wakes the agent.

Agent subcommands:
  record RUN_DIR              validate RUN_DIR/findings.json, reconcile into the ledger
                              (A-ids) and the XP backlog (P-ids, xp_backlog.json)
  mark RUN_DIR                advance the watermark to this run (only after record)
  check-report FILE           the Slack report respects the size cap and sections
  check-seed [FILE]           validate the idea seed (default: the installed copy)
Owner subcommands (run by an interactive Hermes session on the owner's thread reply):
  triage A-ID STATUS [NOTE]   STATUS in accepted|wontfix|fixed
  ship A-ID [NOTE]            a quick change shipped: prints how to apply it; next run reviews
  approve P-ID [NOTE]         proposal approved: writes the spec for adoption (E21.4)
  drop P-ID WHY               drop a proposal
  reprioritise P-ID WHY [--score X]   pin to the top of the ranking (or override its score)
  backlog                     print the ranked backlog
  reset                       forget the watermark; next run reviews unconditionally

Environment (all optional): ARC_REPO (default ~/GitHub/Project-Arc),
ARC_ANALYST_HOME (default ~/.hermes/profiles/arc-analyst/analyst),
ARC_ANALYST_DB (default $ARC_REPO/data/arc.db), ARC_ANALYST_ARC (default
$ARC_REPO/.venv/bin/arc). No broker key, gate secret or Slack token is needed:
they are stripped from the environment of every command this script runs.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
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
REPORT_MAX = 3_500
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
# D86 (E21.3): `quick-change` (ship now, no XP) and `xp-proposal` (a P-<n> backlog entry)
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
)
EXPERIMENT_ID_RE = re.compile(r"^XP-[1-9]\d*$")
# Statuses the weekly report must cover (terminal promoted/rejected ones are history).
REPORTABLE = ("draft", "queued", "registered", "running", "stopped")

# ---- E21.3 (D86): XP proposals (P-<n>) in the Analyst-owned backlog ----
BACKLOG_FILE = "xp_backlog.json"
PROPOSALS_DIR = "proposals"  # approved specs for the owner's manual steps (until E21.4)
PROPOSAL_ID_RE = re.compile(r"^P-[1-9]\d*$")
BACKTEST_ID_RE = re.compile(r"^BT-[1-9]\d*$")
BACKLOG_STATUSES = ("proposed", "approved", "registered", "running", "concluded", "dropped")
BACKLOG_OPEN = ("proposed", "approved", "registered", "running")
BACKLOG_RANKED = ("proposed", "approved")  # not in the DB yet: what next_experiment picks from
BACKLOG_CAP = 12  # open entries; the Analyst drops the weakest with a reason
MAX_NEW_PROPOSALS = 2  # per run
K_TREATMENTS = 4  # D86: control + exactly 4 treatments
TREATMENT_NAMES = tuple(f"t{i}" for i in range(1, K_TREATMENTS + 1))
# A proposal's spec: the live v2 format minus what approval fills (id, proposed_by) and what
# `arc experiment create` fills from config/experiments.yaml (alpha, power, mde, sessions).
PROPOSAL_SPEC_FORBIDDEN = {
    "id": "the id is assigned when the owner approves (P-<n> -> XP-<n>); omit it",
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
# Daily effects (fraction of t0 equity) the gate sizes at K=4 with `arc experiment plan`.
PLAN_EFFECTS = (0.0002, 0.0005, 0.001, 0.002)
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
BT_CHECKPOINTS = Path("data") / "backtest_universe" / "checkpoints.json"  # E7.9
# E7.14's backtest<->live reconciliation, aggregates only:
# {"generated_at", "git_sha", "sim_slippage_frac",
#  "rows": [{"kind", "regime", "n", "entry_pred", "entry_actual", "exit_pred", "exit_actual",
#            "exit_reason_match", "pnl_tracking_error", "pnl_bias"}]}  ($ per unit / per trade)
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


@dataclasses.dataclass(frozen=True)
class Paths:
    """Every filesystem location the gate touches (tests build their own)."""

    home: Path  # analyst state: ledger.json, state.json, runs/
    repo: Path  # the checkout whose config/ and .venv the live tick uses
    live_db: Path
    arc: tuple[str, ...]  # argv prefix of the arc CLI
    gh: tuple[str, ...] = ()  # argv prefix of the GitHub CLI; () = skip the merged-PR section
    backtest_root: Path | None = None  # E7.15: where data/backtest + studies live; None = repo

    @property
    def bt_root(self) -> Path:
        return self.backtest_root or self.repo

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
        """E21.3 (D86): the Analyst-owned XP backlog (P-<n>); not the repo, not the DB."""
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
    conn: sqlite3.Connection, state: dict, now: dt.datetime, holdouts: list | None = None
) -> dict:
    """Whether the agent should run, and why. Reads the COPY only. *holdouts* are the
    research ledger's holdout run keys (E7.15): one not seen at the last ``mark`` wakes it."""
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
        ids = sorted({h.split(":", 1)[0] for h in new_holdouts}, key=lambda s: int(s[3:]))
        reasons.append(f"new backtest holdout verdict(s) since the last run: {', '.join(ids)}")
    wake = closed_total > 0 and bool(reasons)
    if closed_total == 0:
        reasons = ["no closed outcome in the journal yet (E9.4 start condition)"]
    elif not reasons:
        reasons = [
            "no new closed outcome, no halt, no experiment status change and no new backtest "
            "holdout verdict since the last run"
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
    }


def realised_vs_model(conn: sqlite3.Connection, since: dt.datetime | None) -> list[dict]:
    """Latest outcome row per proposal, closed only, bucketed by structure kind x regime."""
    where = "AND l.at >= ?" if since else ""
    args = (to_db(since),) if since else ()
    rows = conn.execute(
        f"""WITH latest AS (
                SELECT o.* FROM outcomes o
                WHERE o.rowid = (SELECT MAX(o2.rowid) FROM outcomes o2
                                 WHERE o2.proposal_hash = o.proposal_hash))
            SELECT COALESCE(json_extract(p.structure_json, '$.kind'), 'unknown') AS kind,
                   COALESCE(p.regime, 'unknown') AS regime,
                   COUNT(*) AS n,
                   SUM(CAST(l.realised_pnl AS REAL)) AS pnl,
                   SUM(CAST(l.ev_total AS REAL)) AS ev,
                   AVG(CAST(l.pnl_vs_ev AS REAL)) AS avg_vs_ev,
                   AVG(l.slippage_bps) AS slip_bps,
                   AVG(l.cost_bps) AS cost_bps,
                   SUM(CASE WHEN CAST(l.realised_pnl AS REAL) > 0 THEN 1 ELSE 0 END) AS wins
            FROM latest l LEFT JOIN proposals p ON p.proposal_hash = l.proposal_hash
            WHERE {closed_filter("l")} AND l.realised_pnl IS NOT NULL {where}
            GROUP BY 1, 2 ORDER BY n DESC, kind, regime""",
        args,
    ).fetchall()
    cols = ("kind", "regime", "n", "pnl", "ev", "avg_vs_ev", "slip_bps", "cost_bps", "wins")
    return [dict(zip(cols, r)) for r in rows]


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
    """Treatment arms of a spec (v1 ``treatment`` = 1): what it holds of max_parallel_arms."""
    arms = spec.get("arms") or {}
    if "treatment" in arms:
        return 1
    return max(1, len(arms.get("treatments") or {}))


def _proposal_ref(detail) -> str | None:
    """The Analyst proposal an experiment event names (E21.4 ``proposal_ref``, or the D86
    ``owner_approval`` ref when the owner approved a ``P-<n>``)."""
    if not isinstance(detail, dict):
        return None
    for key in ("proposal_ref", "owner_approval"):
        ref = str(detail.get(key) or "")
        if PROPOSAL_ID_RE.match(ref):
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


def table_lines(rows: list[dict], slippage_frac: str) -> list[str]:
    out = [
        "| kind | regime | n | realised $ | EV $ | avg realised-EV $ | win | slippage bps "
        "(entry, realised) | cost bps (Quant, round trip) | sample |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        out.append(
            f"| {r['kind']} | {r['regime']} | {r['n']} | {_num(r['pnl'], ',.2f')} | "
            f"{_num(r['ev'], ',.2f')} | {_num(r['avg_vs_ev'], '+,.2f')} | "
            f"{r['wins']}/{r['n']} | {_num(r['slip_bps'], '.0f')} | {_num(r['cost_bps'], '.0f')} | "
            f"{f'LOW (n<{MIN_SAMPLE})' if r['n'] < MIN_SAMPLE else 'ok'} |"
        )
    if not rows:
        out.append("| (none) | | 0 | | | | | | | |")
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
    """A ``P-<n>`` draft spec (D86): the ``config/experiments/live/`` v2 format, no ``id``.

    Mirrors ``arc.experiments.models.ExperimentSpec`` (extra=forbid, ab, control empty), plus
    D86's shape: control + exactly 4 treatments ``t1..t4``, every overlay on the SAME leaf set
    (one variable at 4 levels, or 4 alternatives of one mechanism), no two arms alike. The
    owner's ``approve P-<n>`` assigns the id; ``arc experiment create`` re-validates it in full.
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
    if spec.get("spec_version", 2) != 2:
        errs.append(f"{where}.spec_version must be 2 (multi-treatment, D69)")
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
            f"FOUR-TREATMENTS: {where}.arms.treatments must be exactly "
            f"{', '.join(TREATMENT_NAMES)} (D86: control + {K_TREATMENTS}); got {got}"
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
            "must set the same leaves (one variable at 4 levels, or 4 alternatives of one "
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
        f"/ {entry.get('sessions_needed')} sessions"
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
        errs.append(f"{where}.sessions_needed must be an int >= 1 (read it off the K=4 plan grid)")
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
    if it and it["status"] not in ("proposed",) and it.get("spec") != p.get("spec"):
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
    """Why a K=4 experiment in *area* cannot start now (empty = a slot is free).

    One registered/running/queued experiment per area holds it (the registry's queue rule),
    and its 4 treatment arms must fit under ``max_parallel_arms`` with the arms already
    registered/running (D69).
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
    if used + K_TREATMENTS > max_arms:
        ids = ", ".join(e["experiment_id"] for e in busy)
        out.append(f"arms {used} + {K_TREATMENTS} > max_parallel_arms {max_arms} ({ids})")
    return out


def blockers_of(area: str, overview: list[dict], max_arms: int) -> list[str]:
    """The XP ids whose end frees the slot a K=4 experiment in *area* waits for."""
    ids = [
        e["experiment_id"]
        for e in overview
        if e["area"] == area and e["status"] in ("registered", "running", "queued")
    ]
    busy = [e for e in overview if e["status"] in ("registered", "running")]
    if sum(int(e.get("arms") or 1) for e in busy) + K_TREATMENTS > max_arms:
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
    return {"version": 1, "next_id": 1, "items": {}, "seed": {}}


def load_backlog(path: Path) -> dict:
    data = load_json(path, None)
    if not isinstance(data, dict):
        return empty_backlog()
    for k, v in empty_backlog().items():
        data.setdefault(k, v)
    return data


def _hist(entry: dict, when: str, event: str, **extra) -> None:
    entry.setdefault("history", []).append({"at": when, "event": event, **extra})


def backlog_by_id(backlog: dict, pid: str) -> dict | None:
    return next((e for e in backlog["items"].values() if e.get("id") == pid), None)


def ranked_backlog(backlog: dict, statuses: tuple = BACKLOG_RANKED) -> list[dict]:
    """Open proposals in order: owner pins first (newest pin first), then effective score."""
    items = [e for e in backlog["items"].values() if e.get("status") in statuses]
    return sorted(
        items,
        key=lambda e: (
            0 if e.get("pinned") else 1,
            "" if not e.get("pinned") else _neg_text(e["pinned"].get("at", "")),
            -effective_score(e),
            int(e["id"][2:]),
        ),
    )


def _neg_text(s: str) -> str:
    """Sort key that orders ISO timestamps newest first."""
    return "".join(chr(0x10FFFF - ord(c)) for c in s)


def sync_backlog(backlog: dict, overview: list[dict], when: str) -> list[str]:
    """Statuses from the registry: the experiment whose events name ``proposal_ref`` P-<n>
    (E21.4) moves its entry to registered / running / concluded. Returns change lines."""
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
    # several experiments can cite one P-<n> (a stopped run + its re-run): the most live wins
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
        entry = backlog_by_id(backlog, ref)
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


def backlog_lines(backlog: dict, overview: list[dict], max_arms: int) -> list[str]:
    """Context: every open proposal ranked, its formula and inputs, and slot readiness."""
    ranked = ranked_backlog(backlog)
    live = [e for e in backlog["items"].values() if e.get("status") in ("registered", "running")]
    if not ranked and not live:
        return ["- empty (no P-<n> yet: propose from the seed leads or live buckets n >= 20)"]
    out = []
    for rank, e in enumerate(ranked, 1):
        spec = e.get("spec") or {}
        why = slot_check(spec.get("area", "other"), overview, max_arms)
        slot = "slot free now" if not why else "waits: " + "; ".join(why)
        out.append(
            f"- #{rank} {e['id']} [{e['status']}] {spec.get('area')}: {spec.get('title')} "
            f"(key {e['key']}) | evidence {e.get('evidence_kind')}"
            f"{' ' + e['evidence_ref'] if e.get('evidence_ref') else ''} n={e.get('n')} | {slot}"
        )
        out.append(f"  {value_formula(e)}")
        for name in TREATMENT_NAMES:
            ov = (((spec.get("arms") or {}).get("treatments") or {}).get(name) or {}).get("overlay")
            out.append(f"  {name}: {json.dumps(ov, sort_keys=True)}")
    for e in live:
        out.append(f"- {e['id']} [{e['status']}] -> {e.get('experiment_id')} (in the registry)")
    return out


def capacity_lines(overview: list[dict], max_arms: int) -> list[str]:
    busy = [e for e in overview if e["status"] in ("registered", "running")]
    used = sum(int(e.get("arms") or 1) for e in busy)
    held = ", ".join("%s=%s" % (e["experiment_id"], e.get("arms") or 1) for e in busy)
    out = [
        f"- max_parallel_arms {max_arms}: {used} arm(s) registered/running "
        f"({held or 'none'}); a K={K_TREATMENTS} experiment needs {K_TREATMENTS}"
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
            "next_experiment is required: a P-<n> from the ranked backlog, or 'none: <why>'"
        )
        return errs
    if nxt.lower().startswith("none"):
        if not nxt.partition(":")[2].strip():
            errs.append("next_experiment 'none' needs a reason ('none: <why>')")
        return errs
    entry = backlog_by_id(backlog_after, nxt)
    if entry is None or entry.get("status") not in BACKLOG_RANKED:
        errs.append(
            f"next_experiment {nxt!r} must be a proposed/approved P-<n> in the backlog "
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
    backlog: dict, doc: dict, run: str, when: str, finding_ids: dict | None = None
) -> dict:
    """Append/update ``P-<n>`` entries from this run's xp-proposals, then drops. Idempotent:
    re-applying the same doc adds no entry and no history line."""
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
            n = backlog.get("next_id", 1)
            backlog["next_id"] = n + 1
            entry = items[f["key"]] = {
                "id": f"P-{n}",
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


def validate_findings(
    doc: dict,
    ledger: dict,
    prev_themes: dict | None = None,
    experiments: list[dict] | None = None,
    backlog: dict | None = None,
    seed_ids: frozenset = frozenset(),
    max_arms: int = MAX_PARALLEL_ARMS_DEFAULT,
    studies: list | None = None,
) -> list[str]:
    errs: list[str] = []
    studies = studies or []
    prev_themes = prev_themes or {}
    experiments = experiments or []
    backlog = backlog if backlog is not None else empty_backlog()
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
            n = None
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
        apply_backlog(after, doc, "check", "")
        n_open = sum(1 for e in after["items"].values() if e.get("status") in BACKLOG_OPEN)
        if n_open > BACKLOG_CAP:
            errs.append(
                f"backlog would hold {n_open} open proposals; cap is {BACKLOG_CAP}: drop the "
                "weakest in backlog_drops with a reason"
            )
    errs += _validate_experiment_lines(doc, after or backlog, experiments, max_arms)
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
        m = re.search(rf"\*{re.escape(title)}\*[^\n]*\n(.*?)(?=\n\s*\*[A-Z]|\Z)", text, re.S)
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
        "passes": any(bool(r.get("switch")) for r in rows),
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
    call = "SWITCH candidate" if r.get("switch") else "keep control"
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
    return out


def backtest_snapshot(p: Paths, run_dir: Path) -> dict:
    """Ledger + current specs + reports, read-only. Each spec's hash comes from
    ``arc backtest study show BT-<n> --json`` (the gate has no YAML/pydantic)."""
    entries, errs = load_bt_ledger(p.bt_root / BT_LEDGER)
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
    for sid in spec_ids:
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
    nums = [_study_num(s["id"]) for s in studies]
    return {
        "studies": studies,
        "errors": errs,
        "next_study_id": f"BT-{max(nums, default=0) + 1}",
        "holdouts": [holdout_key(e) for e in entries if e["phase"] == "holdout"],
    }


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
        if not any(s["id"] == ref for s in studies):
            return [f"findings[{i}] BT-REF: {ref} is not in the study ledger"]
        return []
    entry = (backlog.get("items") or {}).get(f.get("key")) or {}
    if ref.startswith(LEGACY_REF_PREFIXES) and (entry.get("spec") or {}).get("backtest_ref") == ref:
        return []  # grandfathered: the entry held this ref before E7.15
    return [
        f"findings[{i}] proposal.spec.backtest_ref {ref!r} must be a BT-<n> study id or 'none' "
        "(legacy docs/RESEARCH refs stay only on backlog entries that already held them)"
    ]


def check_bt_evidence(ref: str, spec, studies: list[dict]) -> list[str]:
    """Option A (D86): a ``BT-<n>`` backing an XP proposal must resolve to a study whose C500
    holdout passed under the spec hash still committed, testing the proposal's variable."""
    s = next((x for x in studies if x["id"] == ref), None)
    if s is None:
        return [f"BT-REF: {ref} is not in the study ledger (data/backtest/ledger.jsonl)"]
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
    if not h.get("passes"):
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
        **{k: spec[k] for k in ("period", "primary_metric", "max_variants") if k in spec},
    }
    path = run_dir / STUDY_SPECS_DIR / f"{sid}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"# DRAFT study spec by the Arc Analyst ({aid}, {f['key']}). Not committed, not run:\n"
        "# the Analyst never runs a backtest. Owner (or a card): copy to\n"
        f"# config/backtest_studies/{sid}.yaml (file name = id; take the next free BT-<n> if\n"
        f"# {sid} is taken), check `arc backtest study show {sid}`, commit, run dev on c125,\n"
        f"# then `arc backtest study run {sid} --holdout` (c500). The D25 rule is the default.\n"
        + json.dumps(doc, indent=2, sort_keys=True)
        + "\n"
    )
    return path


# ---------- agent / owner subcommands ----------


def write_proposal_spec(p: Paths, entry: dict) -> Path:
    """An approved ``P-<n>``'s spec for the owner's manual adoption (until E21.4's
    ``arc experiment adopt``). JSON is valid YAML, so ``arc experiment create --spec`` reads
    it as is (stdlib only: no YAML writer). The id stays a placeholder: the owner (or E21.4)
    takes the next free ``XP-<n>``."""
    spec = {
        "id": "XP-<next>",
        "kind": "ab",
        "spec_version": 2,
        "proposed_by": entry.get("finding_id") or "owner",
        **entry["spec"],
    }
    path = p.home / PROPOSALS_DIR / f"{entry['id']}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"# APPROVED Arc Analyst proposal {entry['id']} ({entry['key']}).\n"
        "# Not registered. Until E21.4 (`arc experiment adopt`): set `id` to the next free\n"
        "# XP-<n> (arc experiment list), copy to config/experiments/live/, then\n"
        f"#   arc experiment create --spec <file> --owner-approval {entry['id']} &&\n"
        f"#   arc experiment register XP-<n> --owner-approval {entry['id']}  (D86)\n"
        + json.dumps(spec, indent=2, sort_keys=True)
        + "\n"
    )
    return path


def _seed_ids(p: Paths) -> frozenset:
    ideas, _ = load_seed(p.idea_seed)
    return frozenset(x["id"] for x in ideas)


def cmd_record(p: Paths, run_dir: Path, now: dt.datetime) -> int:
    doc = json.loads((run_dir / "findings.json").read_text())
    ledger = load_json(p.ledger, {"next_id": 1, "items": {}})
    backlog = load_backlog(p.backlog)
    state = load_json(p.state, {})
    meta = load_json(run_dir / "metrics.json", {})
    experiments = meta.get("experiments", [])
    max_arms = int(meta.get("max_parallel_arms") or MAX_PARALLEL_ARMS_DEFAULT)
    bt_meta = meta.get("backtest") or {}
    studies = bt_meta.get("studies") or []
    sync_backlog(
        backlog,
        [{**e, "experiment_id": e["id"]} for e in experiments],
        iso(now),
    )
    errs = validate_findings(
        doc,
        ledger,
        state.get("themes"),
        experiments,
        backlog=backlog,
        seed_ids=_seed_ids(p),
        max_arms=max_arms,
        studies=studies,
    )
    if errs:
        print("findings.json rejected (ledger and backlog unchanged):\n- " + "\n- ".join(errs))
        return 2
    report = reconcile(ledger, doc, run_dir.name, iso(now))
    items = ledger["items"]
    for f in doc.get("findings", []):
        if f.get("category") == "quick-change":
            items[f["key"]]["quick_change"] = f["quick_change"]
            items[f["key"]]["evidence"] = f.get("evidence")
    # E7.15: draft study specs for the owner, BT-<next> upward in finding order
    next_bt = _study_num(str(bt_meta.get("next_study_id") or "BT-1"))
    study_specs = []
    for f in doc.get("findings", []):
        if "study_spec" not in f:
            continue
        sid = f"BT-{next_bt}"
        next_bt += 1
        it = items[f["key"]]
        path = write_study_spec(run_dir, sid, f["study_spec"], f, it["id"], now)
        it["study_spec"] = {"id": sid, "path": str(path), "run": run_dir.name}
        study_specs.append(f"{it['id']} -> {sid} ({path})")
    reviews = apply_reviews(ledger, doc, run_dir.name, iso(now))
    save_json(p.ledger, ledger)
    finding_ids = {k: it["id"] for k, it in items.items()}
    moves = apply_backlog(backlog, doc, run_dir.name, iso(now), finding_ids)
    backlog["next_experiment"] = {
        "value": doc.get("next_experiment"),
        "slot": doc.get("next_slot"),
        "reason": doc.get("next_reason"),
        "run": run_dir.name,
    }
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


def _owner_entry(p: Paths, pid: str) -> tuple[dict, dict | None]:
    backlog = load_backlog(p.backlog)
    return backlog, backlog_by_id(backlog, pid)


def cmd_approve(p: Paths, pid: str, note: str, now: dt.datetime) -> int:
    """Owner ``approve P-<n>``: freeze the spec and write it for adoption. E21.4's
    ``arc experiment adopt`` takes it from here; until then the owner runs the printed steps."""
    backlog, entry = _owner_entry(p, pid)
    if entry is None:
        print(f"no proposal {pid}")
        return 2
    if entry["status"] not in ("proposed", "approved"):
        print(f"{pid} is {entry['status']}; only a proposed entry can be approved")
        return 2
    if entry["status"] == "proposed":
        entry["status"] = "approved"
        _hist(entry, iso(now), "approved", by="owner", note=note)
    path = write_proposal_spec(p, entry)
    entry["spec_path"] = str(path)
    save_json(p.backlog, backlog)
    print(
        f"{pid} approved. Spec: {path}\n"
        "Until E21.4 (`arc experiment adopt`), from the repo root:\n"
        f"  1. pick the next free id: .venv/bin/arc experiment list\n"
        f"  2. cp {path} config/experiments/live/xp<n>_<slug>.yaml; set `id: XP-<n>`; commit "
        f"(PR body `Experiment: XP-<n>`)\n"
        f"  3. .venv/bin/arc experiment create --spec config/experiments/live/xp<n>_<slug>.yaml "
        f"--owner-approval {pid}\n"
        f"  4. .venv/bin/arc experiment register XP-<n> --owner-approval {pid}\n"
        "The backlog moves to registered/running at the next weekly run (proposal_ref sync)."
    )
    return 0


def cmd_drop(p: Paths, pid: str, why: str, now: dt.datetime) -> int:
    backlog, entry = _owner_entry(p, pid)
    if entry is None:
        print(f"no proposal {pid}")
        return 2
    if not why.strip():
        print("drop needs a reason")
        return 2
    if entry["status"] not in BACKLOG_RANKED:
        print(f"{pid} is {entry['status']}; stop the experiment with `arc experiment stop`")
        return 2
    entry["status"] = "dropped"
    _hist(entry, iso(now), "dropped", by="owner", reason=why)
    save_json(p.backlog, backlog)
    print(f"{pid} -> dropped ({why})")
    return 0


def cmd_reprioritise(p: Paths, pid: str, why: str, score: float | None, now: dt.datetime) -> int:
    """Owner ``reprioritise P-<n> <why> [--score X]``: pin it to the top, or override its
    score. The Analyst keeps the pin until the owner reprioritises again."""
    backlog, entry = _owner_entry(p, pid)
    if entry is None:
        print(f"no proposal {pid}")
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
    rank = [e["id"] for e in ranked_backlog(backlog)].index(pid) + 1
    print(f"{pid} -> rank #{rank} ({why})")
    return 0


def cmd_backlog(p: Paths) -> int:
    backlog = load_backlog(p.backlog)
    ranked = ranked_backlog(backlog)
    if not ranked:
        print("backlog: no open proposals")
    for i, e in enumerate(ranked, 1):
        spec = e.get("spec") or {}
        print(f"#{i} {e['id']} [{e['status']}] {spec.get('area')}: {spec.get('title')}")
        print(f"   {value_formula(e)}")
    others = [e for e in backlog["items"].values() if e.get("status") not in BACKLOG_RANKED]
    for e in sorted(others, key=lambda e: int(e["id"][2:])):
        print(f"   {e['id']} [{e['status']}] {(e.get('spec') or {}).get('title')}")
    return 0


def cmd_reset(p: Paths) -> int:
    state = load_json(p.state, {})
    for k in ("closed_watermark", "last_run_at", "bt_holdouts_seen"):
        state.pop(k, None)
    save_json(p.state, state)
    print("next run reviews unconditionally (if the journal has a closed outcome)")
    return 0


def cmd_check_report(path: Path) -> int:
    errs = validate_report(path.read_text())
    if errs:
        print("report rejected:\n- " + "\n- ".join(errs))
        return 2
    print(f"report ok ({len(path.read_text())} chars)")
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
    """``arc experiment plan --k 4 --effects … --json`` at the stored A/A sigma; with none
    stored, at the sigma ``AA_DOC`` documents (its source labelled in the output)."""
    argv = [
        "experiment",
        "plan",
        "--k",
        str(K_TREATMENTS),
        "--effects",
        ",".join(str(e) for e in PLAN_EFFECTS),
        "--json",
    ]
    text = run_arc(p, [*argv, *db], run_dir, "plan-k4")
    if "no A/A sigma" not in text:
        return text
    sigma = aa_doc_sigma(p.repo)
    if sigma is None:
        return text
    out = run_arc(p, [*argv, "--sigma", repr(sigma), *db], run_dir, "plan-k4")
    try:
        d = json.loads(out)
    except ValueError:
        return out
    d["sigma_source"] = f"{AA_DOC} (no A/A sigma in the store)"
    return json.dumps(d)


def plan_lines(text: str) -> list[str]:
    """The K=4 sessions-needed grid from ``arc experiment plan --k 4 --effects … --json``."""
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
) -> str:
    bt = bt if bt is not None else backtest_snapshot(p, run_dir)
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

    conn = sqlite3.connect(f"{copy.resolve().as_uri()}?mode=ro", uri=True)
    try:
        week_rows = realised_vs_model(conn, since)
        all_rows = realised_vs_model(conn, None)
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
        "plan-k4": run_plan(p, db, run_dir),
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
    backlog = load_backlog(p.backlog)
    synced = sync_backlog(backlog, overview, iso(now))
    if synced:
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
        "## Realised vs model, this week (kind x regime)",
        *table_lines(week_rows, frac),
        "",
        "## Realised vs model, all time (kind x regime)",
        *table_lines(all_rows, frac),
        "",
        "## Backtest <-> live drift (E7.14 reconciliation: simulator vs real fills; a persistent "
        "drift is a cost-model theme line or a -> Sentinel flaw, not a strategy change)",
        *drift_lines(p.bt_root / BT_RECONCILIATION),
        "",
        f"## Backtest studies (D84; E7.12 ledger {BT_LEDGER} + {BT_REPORTS}; next free id "
        f"{bt['next_study_id']}; cite only a C500 holdout verdict, a C125-only or dev-only "
        "result is a hypothesis)",
        *[f"- WARNING: {e}" for e in bt.get("errors") or []],
        *capped_section(
            study_lines(bt["studies"]), BT_SECTION_MAX, run_dir / "backtest-studies.log"
        ),
        "### Data checkpoints (E7.9 checkpoints.json)",
        *checkpoint_lines(p.bt_root / BT_CHECKPOINTS),
        "",
        "## Halts this week (and any still active)",
    ]
    out += [f"- {a} {k}: {r} (cleared {c or 'NO, still active'})" for a, k, r, c in halts] or [
        "- none"
    ]
    out += [
        "",
        f"## Forward experiments (D44; next free id {next_experiment_id(overview)}; "
        f"full: sqlite3 'file:{copy}?mode=ro' or arc experiment report <id> --stored --db {copy})",
        *experiment_lines(overview),
        "",
        f"## Experiment capacity (D69/D86: one experiment per area; K={K_TREATMENTS} arms each)",
        *capacity_lines(overview, max_arms),
        "",
        f"## Sessions needed at K={K_TREATMENTS} (arc experiment plan; read sessions_needed "
        "off this grid)",
        *plan_lines(views["plan-k4"]),
        "",
        f"## XP backlog (yours; {p.backlog}; ranked by value, owner pins first; "
        f"cap {BACKLOG_CAP} open, <= {MAX_NEW_PROPOSALS} new per run)",
        *([f"- registry sync: {s}" for s in synced] or []),
        *backlog_lines(backlog, overview, max_arms),
        "",
        f"## Experiment idea seed (D86: leads from retired dev drafts, not proposals; "
        f"open until proposed (seed_ref) or dropped (seed_drops); full: {p.idea_seed})",
        *seed_lines(seed, seed_errs, backlog.get("seed")),
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
    holdouts = [holdout_key(e) for e in bt_entries if e["phase"] == "holdout"]
    conn = sqlite3.connect(f"{staging.resolve().as_uri()}?mode=ro", uri=True)
    try:
        decision = wake_decision(conn, state, now, holdouts)
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
        },
    )
    ctx = build_context(p, run_dir, copy, decision, now, bt)
    (run_dir / "context.md").write_text(ctx)
    tail = f"\n...[truncated; full: {run_dir}/context.md]"
    print(ctx if len(ctx) <= MAX_STDOUT else ctx[: MAX_STDOUT - len(tail)] + tail)
    return 0


def main(argv: list[str], p: Paths | None = None, now: dt.datetime | None = None) -> int:
    p = p or Paths.from_env()
    now = now or dt.datetime.now(dt.timezone.utc)
    if not argv:
        return cmd_gate(p, now)
    cmd, *rest = argv
    if cmd == "record" and len(rest) == 1:
        return cmd_record(p, Path(rest[0]), now)
    if cmd == "mark" and len(rest) == 1:
        return cmd_mark(p, Path(rest[0]), now)
    if cmd == "triage" and len(rest) in (2, 3):
        return cmd_triage(p, rest[0], rest[1], rest[2] if len(rest) == 3 else "", now)
    if cmd == "ship" and len(rest) in (1, 2):
        return cmd_ship(p, rest[0], rest[1] if len(rest) == 2 else "", now)
    if cmd == "approve" and len(rest) in (1, 2):
        return cmd_approve(p, rest[0], rest[1] if len(rest) == 2 else "", now)
    if cmd == "drop" and len(rest) == 2:
        return cmd_drop(p, rest[0], rest[1], now)
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
    if cmd == "check-report" and len(rest) == 1:
        return cmd_check_report(Path(rest[0]))
    if cmd == "check-seed" and len(rest) <= 1:
        return cmd_check_seed(Path(rest[0]) if rest else p.idea_seed)
    print(__doc__)
    return 64


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
