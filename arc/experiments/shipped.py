"""Strategy changes shipped to every arm during an experiment run (PLAN D86, card E21.1).

Under D86 dev changes ship on, to control and every treatment arm alike. ``arc experiment
report`` lists the strategy-path commits that landed between the experiment's t0 and
now under "Changes shipped to all arms during this run": context for reading the
verdict, never a validity failure.

Read-only: ``git log`` on the local checkout and ``config/strategy_lane.yaml``. No
network, no store. A missing git or repo yields an explanatory line, never an error.
"""

from __future__ import annotations

import datetime as _dt
import subprocess
from collections.abc import Callable
from pathlib import Path

import yaml

__all__ = ["HEADING", "shipped_lines", "strategy_paths"]

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
LANE_CONFIG = Path("config") / "strategy_lane.yaml"
HEADING = "Changes shipped to all arms during this run"
MAX_COMMITS = 40

#: ``(argv after "git") -> stdout``; raises ``OSError`` / ``SubprocessError`` on failure.
GitRunner = Callable[[list[str]], str]


def _git_runner(repo: Path) -> GitRunner:
    def run(args: list[str]) -> str:
        return subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["git", "-C", str(repo), *args],  # noqa: S607 - git on PATH
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        ).stdout

    return run


def strategy_paths(repo: Path = REPO_ROOT) -> list[str]:
    """The ``strategy_paths`` globs of ``config/strategy_lane.yaml`` (empty if absent)."""
    path = repo / LANE_CONFIG
    if not path.is_file():
        return []
    data = yaml.safe_load(path.read_text()) or {}
    return [str(p) for p in data.get("strategy_paths") or ()]


def shipped_lines(
    t0: _dt.datetime,
    *,
    repo: Path = REPO_ROOT,
    git: GitRunner | None = None,
    paths: list[str] | None = None,
) -> list[str]:
    """Report lines: the heading plus ``<sha> <subject>`` per strategy-path commit since t0."""
    globs = strategy_paths(repo) if paths is None else paths
    head = f"  {HEADING} (strategy paths since t0 {t0:%Y-%m-%d %H:%M %Z}; context, not a failure):"
    if not globs:
        return [head, "    - (no strategy_paths in config/strategy_lane.yaml)"]
    run = git or _git_runner(repo)
    since = t0.astimezone(_dt.UTC).isoformat(timespec="seconds")
    # git's default pathspec wildcards cross "/", like the lane check's fnmatch globs
    try:
        out = run(["log", "--format=%h %s", f"--since={since}", "HEAD", "--", *globs])
    except (OSError, subprocess.SubprocessError) as exc:
        return [head, f"    - (git log unavailable: {type(exc).__name__})"]
    commits = [ln for ln in out.splitlines() if ln.strip()]
    if not commits:
        return [head, "    - none"]
    lines = [head, *(f"    - {c}" for c in commits[:MAX_COMMITS])]
    if len(commits) > MAX_COMMITS:
        lines.append(f"    - ... {len(commits) - MAX_COMMITS} more")
    return lines
