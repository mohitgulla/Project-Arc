"""Arc Sentinel lens ``experiments-integrity`` (PLAN D44, card E10.6). Stdlib only.

Deterministic evidence for the Sentinel's experiments lens. The agent runs it (skill procedure
step 4a) after the profile's pre-run gate ``arc_sentinel.py`` has written ``RUN_DIR/arc-copy.db``;
the output lands in ``RUN_DIR/experiments.md``. It reads that private COPY of ``data/arc.db`` (the
E10.1/E10.3 tables) and the private clone's git history and ``config/``. It never writes the DB, the clone, the board or a PR.

Four checks:

1. **Pre-registration lock.** For every experiment, the stored spec re-hashes to its
   ``spec_hash`` (the stored text is canonical JSON, so SHA-256 of the canonical re-dump must
   match), every non-draft event carries that hash, and no spec revision was written after the
   lock (the DB trigger forbids it; a row here means the trigger was bypassed).
2. **Control changes during a running experiment.** Commits on main inside an experiment's
   running window (running event -> stop event, or HEAD) that touch a path of its area
   (``experiment_areas.json``). For each: the touched paths and, for ``config/*.yaml``, the
   existing values it removed/changed (a changed existing value is a default flip, which is
   never "behind a flag defaulting to control") and the new keys it added.
3. **Strategy-lane citations (E10.7; D86 / E21.1).** Commits since the last review that touch a
   strategy-lane path (``config/strategy_lane.yaml`` ``strategy_paths``, else the area map):
   the ``XP-advisory: none | <reason>`` line (D86) plus any ``Experiment: XP-<n>`` and pre-D86
   ``Flag:`` / ``Lane: fast`` lines, from the commit message or, for a squash merge ``(#N)``,
   the PR *body*. Only those lines are
   extracted; no PR review thread or comment is read (D23 isolation). Committed promotion
   verdicts (``config/experiments/live/verdicts/XP-<n>.yaml``) are matched to the stored report.
4. **A/A before any A/B.** Every ab experiment that started has an aa experiment that stopped
   with a recorded sigma before it, or an owner ``aa_override`` on its running event.
"""

from __future__ import annotations

import datetime as dt
import fnmatch
import hashlib
import json
import re
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Optional

AREAS_FILE = "experiment_areas.json"
DB_FMT = "%Y-%m-%dT%H:%M:%S.%fZ"
MAX_COMMITS = 25  # per experiment window / per lane listing; the rest is counted
LANE_EXPERIMENT_RE = re.compile(r"^\s*(?:[-*>]\s*)?experiment\s*:\s*(XP-[1-9]\d*)\b", re.I | re.M)
LANE_FLAG_RE = re.compile(r"^\s*(?:[-*>]\s*)?flag\s*:\s*`?([A-Za-z0-9_.\-]+)`?", re.I | re.M)
LANE_FAST_RE = re.compile(r"^\s*(?:[-*>]\s*)?lane\s*:\s*fast\b(.*)$", re.I | re.M)
#: D86 (E21.1): the advisory line every strategy PR carries (`none` or a reason).
LANE_ADVISORY_RE = re.compile(r"^\s*(?:[-*>]\s*)?xp-advisory\s*:(.*)$", re.I | re.M)
PR_RE = re.compile(r"\(#(\d+)\)\s*$")
OFF_VALUES = ("false", "off", "none", "null", "control", "~", "''", '""')

Git = Callable[..., str]
PrBody = Callable[[int], Optional[str]]  # noqa: UP045 - runtime alias, python3.9


# ---------- pure helpers ----------


def parse_db_time(text: str) -> dt.datetime:
    return dt.datetime.strptime(text, DB_FMT).replace(tzinfo=dt.timezone.utc)  # noqa: UP017 - runs on host python3.9


def canonical(spec_text: str) -> str:
    """``arc.experiments.models.canonical_json`` applied to an already-dumped spec."""
    return json.dumps(
        json.loads(spec_text), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def load_areas(path: Path) -> dict:
    data = json.loads(path.read_text())
    areas = {k: list(v) for k, v in data["areas"].items() if isinstance(v, list)}
    union = sorted({g for gs in areas.values() for g in gs} | set(data.get("always", [])))
    areas["other"] = union  # A/A and cross-cutting experiments: any strategy path
    return {
        "areas": {k: sorted(set(v) | set(data.get("always", []))) for k, v in areas.items()},
        "exclude": list(data.get("exclude", [])),
    }


def matches(path: str, globs: list[str], exclude: list[str] | tuple = ()) -> bool:
    return any(fnmatch.fnmatchcase(path, g) for g in globs) and not any(
        fnmatch.fnmatchcase(path, g) for g in exclude
    )


def strategy_globs(repo: Path, areas: dict) -> tuple[list[str], str]:
    """``strategy_paths`` from the clone's config/strategy_lane.yaml (E10.7), else the area map."""
    lane = repo / "config" / "strategy_lane.yaml"
    if lane.is_file():
        globs, inside = [], False
        for line in lane.read_text().splitlines():
            if re.match(r"^strategy_paths\s*:", line):
                inside = True
                continue
            if inside:
                m = re.match(r"^\s+-\s+['\"]?([^'\"#\s]+)", line)
                if m:
                    globs.append(m.group(1))
                elif line.strip() and not line.lstrip().startswith("#"):
                    break
        if globs:
            return globs, "config/strategy_lane.yaml"
    return areas["areas"]["other"], f"{AREAS_FILE} (config/strategy_lane.yaml not on main yet)"


def parse_lanes(text: str) -> dict:
    text = text.replace("**", "").replace("__", "")
    fast = LANE_FAST_RE.search(text)
    adv = LANE_ADVISORY_RE.search(text)
    return {
        "experiments": sorted({m.upper() for m in LANE_EXPERIMENT_RE.findall(text)}),
        "flags": sorted(set(LANE_FLAG_RE.findall(text))),
        "fast": fast.group(1).strip().strip(" \t—–-:`*").strip() if fast else None,
        "advisory": adv.group(1).strip().strip(" \t—–-:`*").strip() if adv else None,
    }


def config_value_changes(diff: str) -> dict:
    """Changed existing values vs added keys in a ``-U0`` diff of config/*.yaml."""
    removed, added, off = [], [], []
    for line in diff.splitlines():
        if line.startswith(("---", "+++")):
            continue
        body = line[1:].split("#", 1)[0].rstrip()
        if not body.strip() or ":" not in body:
            continue
        if line.startswith("-"):
            removed.append(body.strip())
        elif line.startswith("+"):
            added.append(body.strip())
            value = body.split(":", 1)[1].strip().lower()
            if value in OFF_VALUES:
                off.append(body.strip())
    removed_keys = {r.split(":", 1)[0] for r in removed}
    return {
        "changed_existing": removed,
        "new_keys": [a for a in added if a.split(":", 1)[0] not in removed_keys],
        "new_off_keys": [a for a in off if a.split(":", 1)[0] not in removed_keys],
    }


# ---------- DB reads (the COPY) ----------


def has_tables(conn: sqlite3.Connection, *names: str) -> bool:
    got = {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    }
    return all(n in got for n in names)


def experiments(conn: sqlite3.Connection) -> list[dict]:
    """Per experiment: spec revisions, events, and the pre-registration check."""
    out = []
    ids = [
        r[0]
        for r in conn.execute(
            "SELECT experiment_id FROM experiments GROUP BY experiment_id ORDER BY MIN(id)"
        )
    ]
    for eid in ids:
        revs = conn.execute(
            "SELECT revision, kind, area, spec, spec_hash, created_at FROM experiments "
            "WHERE experiment_id = ? ORDER BY revision",
            (eid,),
        ).fetchall()
        events = [
            {"id": i, "status": s, "reason": r, "spec_hash": h, "detail": json.loads(d or "{}"),
             "at": a}
            for i, s, r, h, d, a in conn.execute(
                "SELECT id, status, reason, spec_hash, detail, at FROM experiment_events "
                "WHERE experiment_id = ? ORDER BY id",
                (eid,),
            )
        ]  # fmt: skip
        rev, kind, area, spec, stored, _created = revs[-1]
        try:
            recomputed = sha256(canonical(spec))
            canonical_ok = canonical(spec) == spec
        except ValueError:
            recomputed, canonical_ok = "(spec is not JSON)", False
        locked = [e for e in events if e["status"] != "draft"]
        registered = locked[0]["spec_hash"] if locked else None
        lock_at = locked[0]["at"] if locked else None
        late_revs = [r[0] for r in revs if lock_at is not None and r[5] > lock_at]
        hashes = sorted({e["spec_hash"] for e in locked})
        problems = []
        if recomputed != stored:
            problems.append(f"stored spec re-hashes to {recomputed[:12]}, not {stored[:12]}")
        if not canonical_ok:
            problems.append("stored spec is not canonical JSON")
        if registered is not None and registered != stored:
            problems.append(f"latest revision {stored[:12]} != registered {registered[:12]}")
        if len(hashes) > 1:
            problems.append(f"non-draft events carry {len(hashes)} different hashes")
        if late_revs:
            problems.append(f"revision(s) {late_revs} written after the lock (trigger bypassed)")
        status = events[-1] if events else {"status": "draft", "reason": None}
        running = next((e for e in events if e["status"] == "running"), None)
        stopped = next((e for e in reversed(events) if e["status"] == "stopped"), None)
        out.append(
            {
                "id": eid,
                "kind": kind,
                "area": area,
                "revision": rev,
                "status": status["status"],
                "reason": status["reason"],
                "stored_hash": stored,
                "registered_hash": registered,
                "prereg_ok": not problems,
                "prereg_problems": problems,
                "running_at": running["at"] if running else None,
                "aa_override": bool(running and running["detail"].get("aa_override")),
                "stopped_at": stopped["at"] if stopped else None,
                "sigma": (stopped or {}).get("detail", {}).get("sigma") if stopped else None,
            }
        )
    return out


def aa_before_ab(exps: list[dict]) -> list[dict]:
    """Every ab that started: the A/A (stopped with sigma) that preceded it, or the override."""
    aas = [e for e in exps if e["kind"] == "aa" and e["stopped_at"] and e["sigma"] is not None]
    out = []
    for e in exps:
        if e["kind"] != "ab" or not e["running_at"]:
            continue
        before = [a["id"] for a in aas if a["stopped_at"] <= e["running_at"]]
        out.append(
            {
                "id": e["id"],
                "running_at": e["running_at"],
                "aa": before,
                "aa_override": e["aa_override"],
                "ok": bool(before),
            }
        )
    return out


def verdict_files(repo: Path, conn: sqlite3.Connection) -> list[dict]:
    """Committed promotion verdicts (E10.7) matched to the stored E10.3 report."""
    root = repo / "config" / "experiments" / "live" / "verdicts"
    out = []
    if not root.is_dir():
        return out
    reports = has_tables(conn, "experiment_reports")
    for f in sorted(root.glob("*.y*ml")):
        kv = {}
        for line in f.read_text().splitlines():
            m = re.match(r"^([a-z_]+)\s*:\s*['\"]?([^'\"#]*?)['\"]?\s*(?:#.*)?$", line)
            if m:
                kv[m.group(1)] = m.group(2).strip()
        row = None
        if reports and kv.get("report_hash"):
            row = conn.execute(
                "SELECT experiment_id, verdict FROM experiment_reports WHERE report_hash = ?",
                (kv["report_hash"],),
            ).fetchone()
        problems = []
        if row is None:
            problems.append("report_hash matches no stored experiment_reports row")
        else:
            if row[0] != kv.get("experiment_id"):
                problems.append(f"report belongs to {row[0]}, not {kv.get('experiment_id')}")
            if row[1] != kv.get("verdict") or row[1] != "win":
                problems.append(f"stored verdict {row[1]!r}, file says {kv.get('verdict')!r}")
        out.append({"file": str(f.relative_to(repo)), **kv, "problems": problems})
    return out


# ---------- git reads (the private clone) ----------


def commits(git: Git, rev_range: list[str]) -> list[dict]:
    """``git log`` with touched files: [{sha, epoch, subject, body, files}] newest first."""
    raw = git("log", "--format=%x1e%H%x1f%ct%x1f%s%x1f%b%x1f", "--name-only", *rev_range)
    out = []
    for chunk in raw.split("\x1e"):
        if not chunk.strip():
            continue
        sha, epoch, subject, body, files = (chunk.split("\x1f") + [""] * 5)[:5]
        out.append(
            {
                "sha": sha.strip(),
                "epoch": int(epoch),
                "subject": subject,
                "body": body,
                "files": [x for x in files.split("\n") if x.strip()],
            }
        )
    return out


def window_commits(git: Git, head: str, exp: dict, areas: dict, now: dt.datetime) -> dict:
    start = parse_db_time(exp["running_at"])
    end = parse_db_time(exp["stopped_at"]) if exp["stopped_at"] else now
    globs = areas["areas"].get(exp["area"], areas["areas"]["other"])
    since = (start - dt.timedelta(days=1)).strftime("%Y-%m-%d")
    hits = []
    for c in commits(git, [f"--since={since}", head]):
        if not start.timestamp() <= c["epoch"] <= end.timestamp():
            continue
        touched = [f for f in c["files"] if matches(f, globs, areas["exclude"])]
        if not touched:
            continue
        cfg = [f for f in touched if f.startswith("config/") and f.endswith((".yaml", ".yml"))]
        delta = (
            config_value_changes(git("show", "--format=", "-U0", c["sha"], "--", *cfg))
            if cfg
            else {"changed_existing": [], "new_keys": [], "new_off_keys": []}
        )
        hits.append({**c, "touched": touched, **delta})
    return {
        "id": exp["id"],
        "area": exp["area"],
        "kind": exp["kind"],
        "from": start.isoformat(timespec="minutes"),
        "to": end.isoformat(timespec="minutes") if exp["stopped_at"] else "HEAD (running)",
        "commits": hits,
    }


def lane_citations(
    git: Git,
    rev_range: list[str],
    globs: list[str],
    exclude: list[str],
    pr_body: PrBody,
    registry: set,
) -> list[dict]:
    out = []
    for c in commits(git, rev_range):
        touched = [f for f in c["files"] if matches(f, globs, exclude)]
        if not touched:
            continue
        m = PR_RE.search(c["subject"])
        pr = int(m.group(1)) if m else None
        text, source = c["subject"] + "\n" + c["body"], "commit message"
        body = pr_body(pr) if pr is not None else None
        if body is not None:
            text, source = text + "\n" + body, f"commit message + PR #{pr} body (lane lines only)"
        elif pr is not None:
            source = f"commit message (PR #{pr} body unavailable)"
        lanes = parse_lanes(text)
        unknown = [x for x in lanes["experiments"] if x not in registry]
        cited = bool(lanes["experiments"] or lanes["flags"] or lanes["fast"] or lanes["advisory"])
        out.append(
            {
                "sha": c["sha"],
                "subject": c["subject"],
                "pr": pr,
                "touched": touched,
                "source": source,
                **lanes,
                "unknown_experiments": unknown,
                "cited": cited,
            }
        )
    return out


# ---------- render ----------


def _short(items: list[str], n: int = 4) -> str:
    return ", ".join(items[:n]) + (f" (+{len(items) - n} more)" if len(items) > n else "")


def render(
    exps: list[dict] | None,
    windows: list[dict],
    lanes: list[dict],
    lane_source: str,
    aa: list[dict],
    verdicts: list[dict],
    note: str = "",
) -> str:
    out = ["## Experiments integrity (D44 lens; deterministic evidence, verify before reporting)"]
    if note:
        out.append(note)
    out.append("### 1. Pre-registration lock (stored spec re-hash vs registered hash)")
    if not exps:
        out.append("- no experiment registered in data/arc.db")
    for e in exps or []:
        lock = (
            "draft (unlocked)"
            if e["registered_hash"] is None
            else (f"registered {e['registered_hash'][:12]}")
        )
        verdict = "OK" if e["prereg_ok"] else "MISMATCH: " + "; ".join(e["prereg_problems"])
        out.append(
            f"- {e['id']} {e['kind']}/{e['area']} [{e['status']}"
            + (f" ({e['reason']})" if e["reason"] else "")
            + f"] rev {e['revision']} stored {e['stored_hash'][:12]} {lock}: {verdict}"
        )
    out.append("### 2. Commits touching the area of a running experiment (inside its window)")
    if not windows:
        out.append("- no experiment has run yet")
    for w in windows:
        cs = w["commits"]
        out.append(
            f"- {w['id']} {w['kind']}/{w['area']} {w['from']} -> {w['to']}: "
            f"{len(cs)} commit(s) touching {w['area']} paths"
        )
        for c in cs[:MAX_COMMITS]:
            flags = []
            if c["changed_existing"]:
                flags.append("CHANGED EXISTING VALUES " + _short(c["changed_existing"]))
            if c["new_off_keys"]:
                flags.append("new off-default keys " + _short(c["new_off_keys"]))
            elif c["new_keys"]:
                flags.append("new keys (default not off) " + _short(c["new_keys"]))
            if not flags:
                flags.append("code only: shipped to every arm (D86); a finding only if it swamps")
            out.append(
                f"  - {c['sha'][:7]} {c['subject']} :: {_short(c['touched'])} :: "
                + "; ".join(flags)
            )
        if len(cs) > MAX_COMMITS:
            out.append(f"  - ... {len(cs) - MAX_COMMITS} more")
    out.append(f"### 3. Strategy-lane commits since the last review (paths: {lane_source})")
    if not lanes:
        out.append("- none")
    for c in lanes[:MAX_COMMITS]:
        adv = c.get("advisory")
        if not c["cited"]:
            cite = "NO XP-ADVISORY"
        else:
            parts = []
            if adv is not None:
                parts.append(f"XP-advisory: {adv or 'EMPTY'}")
            else:
                parts.append("NO XP-ADVISORY")
            if c["experiments"]:
                parts.append("Experiment " + ", ".join(c["experiments"]))
            if c["flags"]:
                parts.append("Flag " + ", ".join(c["flags"]) + " (pre-D86 line)")
            if c["fast"] is not None:
                parts.append(f"Lane fast ({c['fast'] or 'NO REASON'}; pre-D86 line)")
            cite = "; ".join(parts)
            if c["unknown_experiments"]:
                cite += " | NOT IN REGISTRY: " + ", ".join(c["unknown_experiments"])
        out.append(
            f"- {c['sha'][:7]} {c['subject']} :: {_short(c['touched'], 3)} :: {cite} "
            f"[{c['source']}]"
        )
    if len(lanes) > MAX_COMMITS:
        out.append(f"- ... {len(lanes) - MAX_COMMITS} more")
    if verdicts:
        out.append("- committed promotion verdicts:")
        for v in verdicts:
            out.append(
                f"  - {v['file']} {v.get('experiment_id')} {v.get('verdict')}: "
                + ("OK (matches stored report)" if not v["problems"] else "; ".join(v["problems"]))
            )
    out.append("### 4. A/A before any A/B")
    if not aa:
        out.append("- no ab experiment has started")
    for a in aa:
        if a["ok"]:
            state = "after A/A " + ", ".join(a["aa"])
        elif a["aa_override"]:
            state = "NO A/A before it: owner aa_override on the running event"
        else:
            state = "NO A/A before it and no owner override"
        out.append(f"- {a['id']} started {a['running_at']}: {state}")
    return "\n".join(out)


def run(
    db: Path | None,
    repo: Path,
    git: Git,
    head: str,
    last: str | None,
    now: dt.datetime,
    pr_body: PrBody,
    areas_path: Path,
) -> str:
    """The pre-run context section. Never raises: a failure becomes a line in the section."""
    try:
        areas = load_areas(areas_path)
        globs, lane_source = strategy_globs(repo, areas)
        rng = [f"{last}..{head}"] if last else ["-n", "30", head]
        exps, windows, aa, verdicts, note = None, [], [], [], ""
        conn = None
        if db is None or not db.is_file():
            note = "(no copy of data/arc.db: registry checks skipped)"
        else:
            # immutable=1: the gate copies the live WAL-mode DB without its -shm/-wal, and
            # mode=ro alone cannot create the -shm, so older SQLite (python3.9) fails CANTOPEN.
            conn = sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro&immutable=1", uri=True)
        try:
            if conn is not None and not has_tables(conn, "experiments", "experiment_events"):
                note = "(data/arc.db has no experiment tables yet: pre-E10 store)"
            elif conn is not None:
                exps = experiments(conn)
                aa = aa_before_ab(exps)
                windows = [
                    window_commits(git, head, e, areas, now) for e in exps if e["running_at"]
                ]
                verdicts = verdict_files(repo, conn)
            lanes = lane_citations(
                git, rng, globs, areas["exclude"], pr_body, {e["id"] for e in exps or []}
            )
        finally:
            if conn is not None:
                conn.close()
        return render(exps, windows, lanes, lane_source, aa, verdicts, note)
    except Exception as e:  # noqa: BLE001 - the audit must still run; report the failure
        return (
            "## Experiments integrity (D44 lens)\n"
            f"(lens pre-run failed: {type(e).__name__}: {e}; audit it by hand from the clone "
            "and RUN_DIR/arc-copy.db)"
        )


# ---------- CLI (run by the Sentinel agent, procedure step "experiments lens") ----------


def _git_in(repo: Path) -> Git:
    import subprocess

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True, timeout=120
        ).stdout

    return git


def _gh_pr_body(repo: Path) -> PrBody:
    import subprocess

    def body(n: int) -> str | None:
        try:
            r = subprocess.run(
                ["gh", "pr", "view", str(n), "--json", "body", "--jq", ".body"],
                cwd=repo, capture_output=True, text=True, timeout=60,
            )  # fmt: skip
        except (OSError, subprocess.SubprocessError):
            return None
        if r.returncode != 0:
            return None
        # Only the lane lines leave this function: no review prose reaches the agent (D23).
        keep = [
            ln
            for ln in r.stdout.splitlines()
            if LANE_EXPERIMENT_RE.match(ln.replace("**", ""))
            or LANE_FLAG_RE.match(ln.replace("**", ""))
            or LANE_FAST_RE.match(ln.replace("**", ""))
            or LANE_ADVISORY_RE.match(ln.replace("**", ""))
        ]
        return "\n".join(keep)

    return body


def main(argv: list[str]) -> int:
    """``sentinel_experiments.py RUN_DIR``: write RUN_DIR/experiments.md and print it.

    Reads RUN_DIR/arc-copy.db (the gate's DB copy), the private clone at <profile>/sentinel/repo
    checked out at HEAD_SHA, and the last reviewed SHA from <profile>/sentinel/state.json.
    """
    if len(argv) != 1:
        print(main.__doc__)
        return 2
    run_dir = Path(argv[0])
    here = Path(__file__).resolve().parent
    root = Path(__import__("os").environ.get("ARC_SENTINEL_ROOT", here.parent / "sentinel"))
    repo = root / "repo"
    git = _git_in(repo)
    head = git("rev-parse", "HEAD").strip()
    state = json.loads((root / "state.json").read_text()) if (root / "state.json").is_file() else {}
    last = state.get("last_reviewed_sha")
    if last:
        try:
            git("merge-base", "--is-ancestor", last, head)
        except Exception:  # noqa: BLE001 - history rewritten: fall back to the last 30
            last = None
    text = run(
        run_dir / "arc-copy.db",
        repo,
        git,
        head,
        last,
        dt.datetime.now(dt.timezone.utc),  # noqa: UP017 - python3.9
        _gh_pr_body(repo),
        here / AREAS_FILE,
    )
    (run_dir / "experiments.md").write_text(text + "\n")
    print(text)
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(main(sys.argv[1:]))
