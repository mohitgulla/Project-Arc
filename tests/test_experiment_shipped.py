"""E21.1 (D86): ``arc experiment report`` lists strategy commits shipped to all arms."""

from __future__ import annotations

import datetime as dt
import os
import subprocess
from pathlib import Path

import pytest

from arc.experiments.shipped import HEADING, shipped_lines, strategy_paths
from arc.utils.calendar import ET

REPO = Path(__file__).resolve().parent.parent
T0 = dt.datetime(2026, 10, 5, 9, 0, tzinfo=ET)


def _git(root: Path, *args: str, when: dt.datetime | None = None) -> str:
    env = {**os.environ}
    if when is not None:
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = when.isoformat()
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True, env=env
    ).stdout.strip()


def _commit(root: Path, rel: str, text: str, msg: str, when: dt.datetime) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", msg, when=when)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "r"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    lane = "strategy_paths:\n  - arc/scanner/*\n  - config/exits.yaml\n"
    _commit(root, "config/strategy_lane.yaml", lane, "lane", T0 - dt.timedelta(days=3))
    _commit(root, "arc/scanner/rank.py", "x=1\n", "E1: before t0", T0 - dt.timedelta(days=1))
    _commit(root, "arc/scanner/deep/a.py", "x=2\n", "E2: scanner after", T0 + dt.timedelta(days=1))
    _commit(root, "docs/x.md", "d\n", "E3: docs only", T0 + dt.timedelta(days=2))
    _commit(root, "config/exits.yaml", "a: 1\n", "E4: exits after", T0 + dt.timedelta(days=3))
    return root


def test_lists_strategy_commits_since_t0(repo: Path) -> None:
    lines = shipped_lines(T0, repo=repo)
    assert HEADING in lines[0] and "context, not a failure" in lines[0]
    subjects = [ln.split(" ", 6)[-1] for ln in lines[1:]]
    assert subjects == ["E4: exits after", "E2: scanner after"]  # newest first, docs skipped
    assert all(ln.startswith("    - ") for ln in lines[1:])


def test_none_when_nothing_shipped(repo: Path) -> None:
    lines = shipped_lines(T0 + dt.timedelta(days=10), repo=repo)
    assert lines[1:] == ["    - none"]


def test_git_failure_is_a_line_not_an_error(repo: Path) -> None:
    def boom(_args: list[str]) -> str:
        raise subprocess.CalledProcessError(128, "git")

    lines = shipped_lines(T0, repo=repo, git=boom)
    assert lines[1] == "    - (git log unavailable: CalledProcessError)"


def test_no_lane_config(tmp_path: Path) -> None:
    assert strategy_paths(tmp_path) == []
    assert "no strategy_paths" in shipped_lines(T0, repo=tmp_path)[1]


def test_truncates_long_lists(repo: Path) -> None:
    out = "\n".join(f"abc{i:04d} c{i}" for i in range(45))
    lines = shipped_lines(T0, repo=repo, git=lambda _a: out)
    assert len(lines) == 1 + 40 + 1 and lines[-1] == "    - ... 5 more"


def test_reads_the_repo_lane_config() -> None:
    assert "config/exits.yaml" in strategy_paths(REPO)
