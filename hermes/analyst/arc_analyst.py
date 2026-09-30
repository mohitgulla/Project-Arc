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
     drawdown event). The first rule is the E9.4 start condition: no review of
     an empty journal.
  3. Otherwise snapshot ``config/*.yaml``, run the read-only ``arc`` views on the
     copy (attribution, weekly scorecard, gaps, counterfactual, ``journal show``
     per trade closed this week, config diff/history/show), build the
     realised-vs-model table by structure kind x regime, list this week's halts,
     the newest ``docs/RESEARCH/*.md`` and the A-id ledger, and print the context
     (<= 30k chars; full text in ``RUN_DIR/context.md``).

Agent subcommands:
  record RUN_DIR              validate RUN_DIR/findings.json, reconcile into the ledger
  mark RUN_DIR                advance the watermark to this run (only after record)
  check-report FILE           the Slack report respects the size cap and sections
Owner subcommands (run by an interactive Hermes session on the owner's thread reply):
  triage A-ID STATUS [NOTE]   STATUS in accepted|wontfix|fixed
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
MIN_SAMPLE = 30  # same as arc.journal.views.MIN_SAMPLE
MAX_STDOUT = 30_000
REPORT_MAX = 3_500
MAX_RECOMMENDATIONS = 3
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
CATEGORIES = ("recommendation", "flaw", "theme", "data-gap")
ACTIONS = ("new-card", "comment-on-card", "owner-decision", "no-action")
ACTIVE = ("open", "regressed", "accepted")
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
EXPERIMENT_FIELDS = ("variable", "values", "metric", "effect_size", "status")
EXPERIMENT_STATUSES = ("hypothesis, untested", "harness-run")
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
    "Recommendations",
    "Obvious flaws",
    "create A-",
)


@dataclasses.dataclass(frozen=True)
class Paths:
    """Every filesystem location the gate touches (tests build their own)."""

    home: Path  # analyst state: ledger.json, state.json, runs/
    repo: Path  # the checkout whose config/ and .venv the live tick uses
    live_db: Path
    arc: tuple[str, ...]  # argv prefix of the arc CLI

    @property
    def ledger(self) -> Path:
        return self.home / "ledger.json"

    @property
    def state(self) -> Path:
        return self.home / "state.json"

    @property
    def runs(self) -> Path:
        return self.home / "runs"

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
        return cls(home=home.expanduser(), repo=repo, live_db=db, arc=(arc,))


# ---------- pure helpers (unit-tested) ----------


def load_json(path: Path, default):
    return json.loads(path.read_text()) if path.exists() else default


def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
    tmp.replace(path)


def to_db(when: dt.datetime) -> str:
    return when.astimezone(dt.UTC).strftime(DB_FMT)


def iso(when: dt.datetime) -> str:
    return when.astimezone(dt.UTC).isoformat(timespec="seconds")


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


def wake_decision(conn: sqlite3.Connection, state: dict, now: dt.datetime) -> dict:
    """Whether the agent should run, and why. Reads the COPY only."""
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
        dict(zip(("at", "kind", "reason"), r, strict=True))
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
    wake = closed_total > 0 and bool(reasons)
    if closed_total == 0:
        reasons = ["no closed outcome in the journal yet (E9.4 start condition)"]
    elif not reasons:
        reasons = ["no new closed outcome and no halt since the last run"]
    return {
        "wake": wake,
        "reasons": reasons,
        "closed_total": closed_total,
        "closed_max_rowid": closed_max,
        "new_closed": new_closed,
        "halts": halts,
        "since": iso(since),
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
    return [dict(zip(cols, r, strict=True)) for r in rows]


def _num(v, fmt: str) -> str:
    return "n/a" if v is None else format(v, fmt)


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


def validate_findings(doc: dict, ledger: dict, prev_themes: dict | None = None) -> list[str]:
    errs: list[str] = []
    prev_themes = prev_themes or {}
    if doc.get("verdict") not in ("quiet", "findings"):
        errs.append("verdict must be 'quiet' or 'findings'")
    findings, resolved = doc.get("findings", []), doc.get("resolved", [])
    keys = [f.get("key") for f in findings]
    if len(keys) != len(set(keys)):
        errs.append("duplicate keys in findings")
    recs = 0
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
        if f.get("category") == "recommendation":
            recs += 1
            if n is not None and n < MIN_SAMPLE:
                errs.append(
                    f"findings[{i}] MIN-SAMPLE: a recommendation needs >= {MIN_SAMPLE} "
                    f"closed trades in its bucket (n={n}); report it as a theme "
                    "status or a data-gap instead"
                )
            exp = f.get("experiment") or {}
            gone = [k for k in EXPERIMENT_FIELDS if not exp.get(k)]
            if gone:
                errs.append(f"findings[{i}] ONE-VARIABLE: experiment missing {gone}")
            if isinstance(exp.get("variable"), list):
                errs.append(f"findings[{i}] ONE-VARIABLE: experiment.variable must be one name")
            vals = exp.get("values")
            if vals is not None and (not isinstance(vals, list) or len(vals) < 2):
                errs.append(
                    f"findings[{i}] experiment.values must list the incumbent and >= 1 challenger"
                )
            if exp.get("status") and exp["status"] not in EXPERIMENT_STATUSES:
                errs.append(f"findings[{i}] experiment.status must be one of {EXPERIMENT_STATUSES}")
            if exp.get("status") == "harness-run" and not exp.get("harness_ref"):
                errs.append(
                    f"findings[{i}] harness-run needs experiment.harness_ref "
                    "(docs/RESEARCH file or run dir)"
                )
    if recs > MAX_RECOMMENDATIONS:
        errs.append(f"{recs} recommendations: at most {MAX_RECOMMENDATIONS} per run")
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
    return errs


# ---------- agent / owner subcommands ----------


def cmd_record(p: Paths, run_dir: Path, now: dt.datetime) -> int:
    doc = json.loads((run_dir / "findings.json").read_text())
    ledger = load_json(p.ledger, {"next_id": 1, "items": {}})
    state = load_json(p.state, {})
    errs = validate_findings(doc, ledger, state.get("themes"))
    if errs:
        print("findings.json rejected (ledger unchanged):\n- " + "\n- ".join(errs))
        return 2
    report = reconcile(ledger, doc, run_dir.name, iso(now))
    save_json(p.ledger, ledger)
    state["themes"] = {
        t["theme"]: {k: v for k, v in t.items() if k != "theme"} for t in doc.get("themes", [])
    }
    save_json(p.state, state)
    save_json(run_dir / "reconciled.json", report)
    print(json.dumps({k: [x["id"] for x in v] for k, v in report.items()}, indent=2))
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


def cmd_reset(p: Paths) -> int:
    state = load_json(p.state, {})
    for k in ("closed_watermark", "last_run_at"):
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


def build_context(p: Paths, run_dir: Path, copy: Path, decision: dict, now: dt.datetime) -> str:
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
                *db,
            ],
            run_dir,
            "attribution-all",
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
        "## Halts this week (and any still active)",
    ]
    out += [f"- {a} {k}: {r} (cleared {c or 'NO, still active'})" for a, k, r, c in halts] or [
        "- none"
    ]
    out += [
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
    conn = sqlite3.connect(f"{staging.resolve().as_uri()}?mode=ro", uri=True)
    try:
        decision = wake_decision(conn, state, now)
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
    save_json(
        run_dir / "metrics.json",
        {"now": iso(now), **{k: v for k, v in decision.items() if k != "wake"}},
    )
    ctx = build_context(p, run_dir, copy, decision, now)
    (run_dir / "context.md").write_text(ctx)
    tail = f"\n...[truncated; full: {run_dir}/context.md]"
    print(ctx if len(ctx) <= MAX_STDOUT else ctx[: MAX_STDOUT - len(tail)] + tail)
    return 0


def main(argv: list[str], p: Paths | None = None, now: dt.datetime | None = None) -> int:
    p = p or Paths.from_env()
    now = now or dt.datetime.now(dt.UTC)
    if not argv:
        return cmd_gate(p, now)
    cmd, *rest = argv
    if cmd == "record" and len(rest) == 1:
        return cmd_record(p, Path(rest[0]), now)
    if cmd == "mark" and len(rest) == 1:
        return cmd_mark(p, Path(rest[0]), now)
    if cmd == "triage" and len(rest) in (2, 3):
        return cmd_triage(p, rest[0], rest[1], rest[2] if len(rest) == 3 else "", now)
    if cmd == "reset" and not rest:
        return cmd_reset(p)
    if cmd == "check-report" and len(rest) == 1:
        return cmd_check_report(Path(rest[0]))
    print(__doc__)
    return 64


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
