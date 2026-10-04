"""E10.6: the Sentinel's lens 8 ``experiments-integrity`` evidence script.

``hermes/sentinel/sentinel_experiments.py`` is a stdlib script installed into the
``arc-sentinel`` profile. These tests drive it against a fixture registry
(``tests/experiment_fixtures.reviewer_registry``) and a throwaway git repo whose commit
dates are pinned, so nothing reads the wall clock.
"""

from __future__ import annotations

import ast
import datetime as _dt
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from arc.experiments.store import ExperimentStore
from arc.store.migrate import migrate
from tests import experiment_fixtures as fx

REPO = Path(__file__).resolve().parents[1]
SENTINEL = REPO / "hermes" / "sentinel"
SCRIPT = SENTINEL / "sentinel_experiments.py"
SKILL = SENTINEL / "skills" / "arc-sentinel" / "SKILL.md"
NOW = _dt.datetime(2026, 11, 2, 12, tzinfo=_dt.UTC)


def _load() -> Any:
    spec = importlib.util.spec_from_file_location("sentinel_experiments_e106", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


se = _load()


def _registry(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    migrate(conn)
    times = fx.reviewer_registry(conn)
    path = tmp_path / "arc-copy.db"
    disk = sqlite3.connect(path)
    conn.backup(disk)
    disk.close()
    return path, times


def _ro(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)


class Repo:
    """A throwaway git repo whose commit times are pinned (GIT_*_DATE)."""

    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir(parents=True)
        self("init", "-q", "-b", "main")
        self("config", "user.email", "t@example.com")
        self("config", "user.name", "t")

    def __call__(self, *args: str, when: _dt.datetime | None = None) -> str:
        env = {"PATH": os.environ["PATH"], "HOME": str(self.root)}
        if when is not None:
            env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = when.isoformat()
        return subprocess.run(
            ["git", "-C", str(self.root), *args],
            capture_output=True, text=True, check=True, env=env,
        ).stdout  # fmt: skip

    def commit(self, files: dict[str, str], message: str, when: _dt.datetime) -> str:
        for rel, text in files.items():
            p = self.root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)
        self("add", "-A")
        self("commit", "-q", "-m", message, when=when)
        return self("rev-parse", "HEAD").strip()


def _git(repo: Repo) -> Any:
    return lambda *a: repo(*a)


def _areas() -> dict[str, Any]:
    return se.load_areas(SENTINEL / se.AREAS_FILE)


# ---------------------------------------------------------------------------
# 1. pre-registration lock
# ---------------------------------------------------------------------------


def test_prereg_lock_verifies_every_experiment(tmp_path: Path) -> None:
    db, _ = _registry(tmp_path)
    conn = _ro(db)
    exps = {e["id"]: e for e in se.experiments(conn)}
    conn.close()
    assert set(exps) == {"X-1", "X-2", "X-3"}
    assert all(e["prereg_ok"] for e in exps.values()), exps
    assert exps["X-1"]["kind"] == "aa" and exps["X-1"]["status"] == "stopped"
    assert exps["X-1"]["sigma"] is not None
    assert exps["X-2"]["status"] == "running" and exps["X-3"]["status"] == "queued"
    # the script's re-hash is arc's spec_hash (canonical JSON, sha256)
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    store = ExperimentStore(conn)
    assert exps["X-2"]["stored_hash"] == store.require("X-2").spec_hash
    conn.close()


def test_prereg_mismatch_when_the_trigger_was_bypassed(tmp_path: Path) -> None:
    db, _ = _registry(tmp_path)
    conn = sqlite3.connect(db)
    conn.execute("DROP TRIGGER experiments_no_update")
    spec = json.loads(
        conn.execute("SELECT spec FROM experiments WHERE experiment_id = 'X-2'").fetchone()[0]
    )
    spec["hypothesis"] = "edited after registration"
    conn.execute(
        "UPDATE experiments SET spec = ? WHERE experiment_id = 'X-2'",
        (json.dumps(spec, sort_keys=True, separators=(",", ":")),),
    )
    conn.commit()
    conn.close()
    conn = _ro(db)
    x2 = next(e for e in se.experiments(conn) if e["id"] == "X-2")
    conn.close()
    assert not x2["prereg_ok"]
    assert any("re-hashes to" in p for p in x2["prereg_problems"])
    text = se.render([x2], [], [], "areas", [], [])
    assert "X-2 ab/exits [running]" in text and "MISMATCH" in text


def test_pre_e10_store_and_missing_copy_degrade(tmp_path: Path) -> None:
    repo = Repo(tmp_path / "r")
    head = repo.commit({"README.md": "x"}, "init", NOW - _dt.timedelta(days=1))
    empty = tmp_path / "old.db"
    sqlite3.connect(empty).close()
    areas = SENTINEL / se.AREAS_FILE
    out = se.run(empty, repo.root, _git(repo), head, None, NOW, lambda n: None, areas)
    assert "pre-E10 store" in out and "### 4. A/A before any A/B" in out
    out = se.run(tmp_path / "nope.db", repo.root, _git(repo), head, None, NOW, lambda n: None,
                 areas)  # fmt: skip
    assert "no copy of data/arc.db" in out
    assert not (tmp_path / "nope.db").exists()  # never creates the store


# ---------------------------------------------------------------------------
# 2. control changed during a running experiment
# ---------------------------------------------------------------------------


def test_commits_in_a_running_window_touching_its_area(tmp_path: Path) -> None:
    db, times = _registry(tmp_path)
    started = _dt.datetime.fromisoformat(times["x2_running_at"]).astimezone(_dt.UTC)
    repo = Repo(tmp_path / "r")
    exits_v1 = "default:\n  take_profit_pct: 0.5\n  stop_loss_mult: 2.0\n"
    repo.commit({"config/exits.yaml": exits_v1}, "before", started - _dt.timedelta(days=2))
    repo.commit(
        {"config/exits.yaml": exits_v1.replace("0.5", "0.45")},
        "E6.9: tighten take profit", started + _dt.timedelta(days=1),
    )  # fmt: skip
    repo.commit(
        {"config/exits.yaml": exits_v1.replace("0.5", "0.45") + "  trail_enabled: false\n"},
        "E6.10: trailing stop behind a flag", started + _dt.timedelta(days=2),
    )  # fmt: skip
    repo.commit(
        {"arc/scanner/rank.py": "x = 1\n"}, "E7.9: ranking tweak", started + _dt.timedelta(days=3)
    )
    head = repo.commit({"arc/exits/policy.py": "y = 2\n"}, "E6.11: exit code",
                       started + _dt.timedelta(days=4))  # fmt: skip
    conn = _ro(db)
    exps = se.experiments(conn)
    conn.close()
    x2 = next(e for e in exps if e["id"] == "X-2")
    w = se.window_commits(_git(repo), head, x2, _areas(), NOW)
    subjects = [c["subject"] for c in w["commits"]]
    assert subjects == ["E6.11: exit code", "E6.10: trailing stop behind a flag",
                        "E6.9: tighten take profit"]  # fmt: skip
    by = {c["subject"]: c for c in w["commits"]}
    assert by["E6.9: tighten take profit"]["changed_existing"] == ["take_profit_pct: 0.5"]
    assert by["E6.10: trailing stop behind a flag"]["new_off_keys"] == ["trail_enabled: false"]
    assert by["E6.10: trailing stop behind a flag"]["changed_existing"] == []
    text = se.render(exps, [w], [], "areas", [], [])
    assert "CHANGED EXISTING VALUES take_profit_pct: 0.5" in text
    assert "new off-default keys trail_enabled: false" in text
    assert "code only: check it ships behind a flag defaulting to control" in text
    assert "E7.9" not in text  # ranking is not X-2's area


def test_window_ends_at_the_stop_event(tmp_path: Path) -> None:
    db, _ = _registry(tmp_path)
    conn = _ro(db)
    x1 = next(e for e in se.experiments(conn) if e["id"] == "X-1")
    conn.close()
    stopped = se.parse_db_time(x1["stopped_at"])
    repo = Repo(tmp_path / "r")
    head = repo.commit({"config/costs.yaml": "a: 1\n"}, "after the A/A stopped",
                       stopped + _dt.timedelta(hours=1))  # fmt: skip
    w = se.window_commits(_git(repo), head, x1, _areas(), NOW)
    assert w["commits"] == []


def test_area_map_covers_every_area_and_other_is_the_union() -> None:
    areas = _areas()["areas"]
    assert set(areas) == {"entries", "exits", "ranking", "sizing", "other"}
    union = set().union(*(set(v) for k, v in areas.items() if k != "other"))
    assert set(areas["other"]) == union
    assert all("config/costs.yaml" in v for v in areas.values())
    for globs in areas.values():  # every literal path exists on main
        for g in globs:
            if not any(c in g for c in "*?["):
                assert (REPO / g).exists(), g


def test_config_value_changes_parser() -> None:
    diff = (
        "--- a/config/exits.yaml\n+++ b/config/exits.yaml\n"
        "@@ -2 +2 @@\n-  take_profit_pct: 0.5\n+  take_profit_pct: 0.45\n"
        "@@ -9,0 +10,2 @@\n+  new_rule: on  # comment\n+  other_rule: off\n"
    )
    out = se.config_value_changes(diff)
    assert out["changed_existing"] == ["take_profit_pct: 0.5"]
    assert out["new_keys"] == ["new_rule: on", "other_rule: off"]
    assert out["new_off_keys"] == ["other_rule: off"]


# ---------------------------------------------------------------------------
# 3. strategy-lane citations (E10.7)
# ---------------------------------------------------------------------------


def test_lane_citations_from_commit_and_pr_body(tmp_path: Path) -> None:
    repo = Repo(tmp_path / "r")
    base = repo.commit({"README.md": "x"}, "init", NOW - _dt.timedelta(days=9))
    t = NOW - _dt.timedelta(days=5)
    repo.commit({"config/exits.yaml": "a: 1\n"}, "E6.9: promote X-2\n\nExperiment: X-2", t)
    repo.commit({"config/exits.yaml": "a: 2\n"}, "E6.12: hotfix (#12)", t)
    repo.commit({"arc/exits/policy.py": "z\n"}, "E6.13: flagged\n\n**Flag:** `exits.new`", t)
    repo.commit({"arc/exits/policy.py": "w\n"}, "E6.14: cite unknown\n\nExperiment: X-9", t)
    repo.commit({"arc/exits/policy.py": "v\n"}, "E6.15: nothing cited", t)
    head = repo.commit({"docs/x.md": "d\n"}, "docs only", t)
    bodies = {12: "Lane: fast — broker outage hotfix"}
    out = se.lane_citations(
        _git(repo), [f"{base}..{head}"], _areas()["areas"]["other"], [], bodies.get,
        {"X-1", "X-2", "X-3"},
    )  # fmt: skip
    by = {c["subject"].split(":")[0]: c for c in out}
    assert set(by) == {"E6.9", "E6.12", "E6.13", "E6.14", "E6.15"}  # docs-only skipped
    assert by["E6.9"]["experiments"] == ["X-2"] and by["E6.9"]["cited"]
    assert by["E6.12"]["fast"] == "broker outage hotfix" and by["E6.12"]["pr"] == 12
    assert "PR #12 body" in by["E6.12"]["source"]
    assert by["E6.13"]["flags"] == ["exits.new"]
    assert by["E6.14"]["unknown_experiments"] == ["X-9"]
    assert not by["E6.15"]["cited"]
    text = se.render([], [], out, "areas", [], [])
    assert "NO LANE CITED" in text and "NOT IN REGISTRY: X-9" in text


def test_strategy_paths_come_from_the_lane_config_when_present(tmp_path: Path) -> None:
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "strategy_lane.yaml").write_text(
        "version: 1\nstrategy_paths:\n  - arc/exits/**  # exits\n  - 'config/exits.yaml'\n"
        "exclude_paths:\n  - tests/**\n"
    )
    globs, src = se.strategy_globs(tmp_path, _areas())
    assert globs == ["arc/exits/**", "config/exits.yaml"]
    assert src == "config/strategy_lane.yaml"
    globs, src = se.strategy_globs(tmp_path / "missing", _areas())
    assert "not on main yet" in src and "config/exits.yaml" in globs


def test_pr_body_reader_passes_only_lane_lines() -> None:
    text = "Summary\nreviewer said: LGTM\n- **Experiment:** X-2\nFlag: `a.b`\nLane: fast — x\n"
    keep = [
        ln for ln in text.splitlines()
        if se.LANE_EXPERIMENT_RE.match(ln.replace("**", ""))
        or se.LANE_FLAG_RE.match(ln.replace("**", ""))
        or se.LANE_FAST_RE.match(ln.replace("**", ""))
    ]  # fmt: skip
    assert keep == ["- **Experiment:** X-2", "Flag: `a.b`", "Lane: fast — x"]
    reader = SCRIPT.read_text().split("def _gh_pr_body", 1)[1].split("\ndef ", 1)[0]
    assert '"pr", "view"' in reader and "reviews" not in reader and "comments" not in reader


def test_committed_verdict_is_matched_to_the_stored_report(tmp_path: Path) -> None:
    db, _ = _registry(tmp_path)
    conn = _ro(db)
    report_hash = conn.execute(
        "SELECT report_hash FROM experiment_reports WHERE experiment_id = 'X-1'"
    ).fetchone()[0]
    vdir = tmp_path / "repo" / "config" / "experiments" / "live" / "verdicts"
    vdir.mkdir(parents=True)
    (vdir / "X-1.yaml").write_text(
        f"experiment_id: X-1\nverdict: win\nreport_hash: {report_hash}\n"
    )
    (vdir / "X-2.yaml").write_text("experiment_id: X-2\nverdict: win\nreport_hash: deadbeef\n")
    out = {v["experiment_id"]: v for v in se.verdict_files(tmp_path / "repo", conn)}
    conn.close()
    assert any("stored verdict 'futility'" in p for p in out["X-1"]["problems"])
    assert out["X-2"]["problems"] == ["report_hash matches no stored experiment_reports row"]


# ---------------------------------------------------------------------------
# 4. A/A before A/B
# ---------------------------------------------------------------------------


def test_aa_before_ab(tmp_path: Path) -> None:
    db, _ = _registry(tmp_path)
    conn = _ro(db)
    rows = se.aa_before_ab(se.experiments(conn))
    conn.close()
    assert [(r["id"], r["aa"], r["ok"]) for r in rows] == [("X-2", ["X-1"], True)]
    # an ab started with no A/A on record goes through the owner override (fx.start)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    migrate(conn)
    fx.start(conn, fx.spec("X-2"))
    rows = se.aa_before_ab(se.experiments(conn))
    assert rows[0]["ok"] is False and rows[0]["aa_override"] is True
    assert "owner aa_override" in se.render([], [], [], "areas", rows, [])


# ---------------------------------------------------------------------------
# CLI, install, skill text
# ---------------------------------------------------------------------------


def test_cli_writes_experiments_md(tmp_path: Path) -> None:
    db, _ = _registry(tmp_path)
    root = tmp_path / "sentinel"
    repo = Repo(root / "repo")
    head = repo.commit({"config/exits.yaml": "a: 1\n"}, "E6.9: x", NOW - _dt.timedelta(days=1))
    (root / "state.json").write_text(json.dumps({"last_reviewed_sha": head}))
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "arc-copy.db").write_bytes(db.read_bytes())
    r = subprocess.run(
        [sys.executable, str(SCRIPT), str(run_dir)],
        capture_output=True, text=True, timeout=120,
        env={**os.environ, "ARC_SENTINEL_ROOT": str(root)},
    )  # fmt: skip
    assert r.returncode == 0, r.stderr
    text = (run_dir / "experiments.md").read_text()
    assert text.startswith("## Experiments integrity")
    assert "X-2 ab/exits [running]" in text and ": OK" in text
    assert "after A/A X-1" in text


def test_script_is_stdlib_only_and_parses_under_python39() -> None:
    tree = ast.parse(SCRIPT.read_text(), feature_version=(3, 9))
    stdlib = set(sys.stdlib_module_names) | {"__future__"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert all(a.name.split(".")[0] in stdlib for a in node.names), ast.dump(node)
        if isinstance(node, ast.ImportFrom):
            assert (node.module or "").split(".")[0] in stdlib, node.module
        if isinstance(node, ast.Attribute) and node.attr == "UTC":
            raise AssertionError(f"datetime.UTC needs 3.11 (line {node.lineno})")
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "zip":
            assert not node.keywords


def test_install_dry_run_copies_the_lens_script(tmp_path: Path) -> None:
    out = subprocess.run(
        ["bash", str(SENTINEL / "install.sh"), "--dry-run"],
        env={"ARC_SENTINEL_PROFILE_HOME": str(tmp_path / "p"), "PATH": "/usr/bin:/bin"},
        capture_output=True, text=True, check=True,
    ).stdout  # fmt: skip
    for f in ("sentinel_experiments.py", "experiment_areas.json"):
        assert f"{tmp_path / 'p'}/scripts/{f}" in out, f
    assert "arc_sentinel.py" not in out  # the gate stays profile-owned
    assert not (tmp_path / "p").exists()


@pytest.mark.parametrize(
    "phrase",
    [
        "8. **Experiments integrity (D44)**",
        "sentinel_experiments.py RUN_DIR",
        "Pre-registration lock",
        "Control changed mid-run",
        "Strategy-lane citations (E10.7)",
        "A/A before A/B",
        "never PR review\n     threads or comments",
        "Never run `arc experiment` write verbs",
        "*Experiments:*",
    ],
)
def test_skill_documents_lens_8(phrase: str) -> None:
    assert phrase in SKILL.read_text()
