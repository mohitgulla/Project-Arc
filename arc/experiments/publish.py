"""Commit experiment files to ``main`` through an auto-merged PR (E21.4, D86).

``arc experiment adopt`` commits the registered spec (``config/experiments/live/
xp<n>_<slug>.yaml``) and every concluded experiment commits its verdict
(``config/experiments/live/verdicts/XP-<n>.yaml``). Both go the same way:

1. ``git fetch origin main`` and a throw-away worktree on ``origin/main`` in a temp dir
   (the checkout the routines tick runs from is never switched or dirtied);
2. a branch (``xp/<n>`` or ``xp/<n>-verdict``), one commit with the files, push;
3. ``gh pr create`` with a ``Lane: fast`` body the strategy-lane check accepts (E21.1),
   then ``gh pr merge --auto --squash``: GitHub merges it once the required checks pass.

Idempotent: an open or merged PR for the branch is reused (its URL returned), and a
file already on ``origin/main`` with the same content needs no PR at all.

:class:`MainView` reads ``origin/main`` (the autostart's "spec merged" check and the
next-free-id scan). Both are I/O at the edge; tests inject fakes.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

import structlog

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

__all__ = [
    "EXPERIMENTS_DIR",
    "VERDICTS_DIR",
    "GitMainView",
    "GitPublisher",
    "MainView",
    "PublishError",
    "PublishResult",
    "Publisher",
]

log = structlog.get_logger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
EXPERIMENTS_DIR = "config/experiments/live"
VERDICTS_DIR = "config/experiments/live/verdicts"
_TIMEOUT_S = 120


class PublishError(RuntimeError):
    """git / gh failed; the DB state is kept and the publish can be retried."""


@dataclass(frozen=True)
class PublishResult:
    branch: str
    url: str | None  # None: nothing to publish (already on main)
    status: str  # opened | existing | merged | on-main
    auto_merge: bool = False


class Publisher(Protocol):
    def publish(
        self, *, branch: str, files: Mapping[str, str], title: str, body: str
    ) -> PublishResult: ...


class MainView(Protocol):
    """Read-only view of ``origin/main``: ``{repo path: text}`` of the files in a dir."""

    def files(self, directory: str) -> dict[str, str]: ...


def _run(argv: Sequence[str], cwd: Path, *, check: bool = True) -> str:
    try:
        r = subprocess.run(  # noqa: S603 - fixed argv, no shell
            list(argv), cwd=cwd, capture_output=True, text=True, timeout=_TIMEOUT_S, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        msg = f"{' '.join(argv[:3])}: {exc}"
        raise PublishError(msg) from exc
    if check and r.returncode != 0:
        msg = f"{' '.join(argv[:4])} failed ({r.returncode}): {(r.stderr or r.stdout).strip()}"
        raise PublishError(msg[:1000])
    return r.stdout


@dataclass
class GitMainView:
    """``origin/main`` of *repo* (``git ls-tree`` / ``git show``; fetched once if *fetch*)."""

    repo: Path = REPO_ROOT
    ref: str = "origin/main"
    fetch: bool = True
    _fetched: bool = field(default=False, init=False)

    def _ensure(self) -> None:
        if self.fetch and not self._fetched:
            _run(["git", "fetch", "-q", "origin", "main"], self.repo)
            self._fetched = True

    def files(self, directory: str) -> dict[str, str]:
        self._ensure()
        out = _run(
            ["git", "ls-tree", "--name-only", self.ref, directory.rstrip("/") + "/"],
            self.repo,
            check=False,
        )
        return {
            name: _run(["git", "show", f"{self.ref}:{name}"], self.repo)
            for name in sorted(out.splitlines())
            if name.endswith((".yaml", ".json"))
        }


@dataclass
class GitPublisher:
    """Commit *files* on a branch off ``origin/main`` and open an auto-merge PR."""

    repo: Path = REPO_ROOT
    gh: tuple[str, ...] = ("gh",)

    def _existing_pr(self, branch: str) -> tuple[str, str] | None:
        out = _run(
            [*self.gh, "pr", "list", "--state", "all", "--head", branch, "--json", "url,state"],
            self.repo,
        )
        prs = [p for p in json.loads(out or "[]") if p.get("state") in ("OPEN", "MERGED")]
        return (prs[0]["url"], prs[0]["state"]) if prs else None

    def publish(
        self, *, branch: str, files: Mapping[str, str], title: str, body: str
    ) -> PublishResult:
        found = self._existing_pr(branch)
        if found is not None:
            url, state = found
            log.info("experiments.publish.existing", branch=branch, url=url, state=state)
            return PublishResult(branch, url, "merged" if state == "MERGED" else "existing")
        _run(["git", "fetch", "-q", "origin", "main"], self.repo)
        on_main = {
            path: _run(["git", "show", f"origin/main:{path}"], self.repo, check=False)
            for path in files
        }
        if all(on_main[p] == text for p, text in files.items()):
            return PublishResult(branch, None, "on-main")
        tmp = Path(tempfile.mkdtemp(prefix="arc-xp-"))
        wt = tmp / "wt"
        try:
            _run(["git", "worktree", "add", "-q", "-B", branch, str(wt), "origin/main"], self.repo)
            for path, text in files.items():
                dest = wt / path
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(text)
                _run(["git", "add", path], wt)
            _run(["git", "commit", "-q", "-m", title], wt)
            _run(["git", "push", "-q", "-u", "origin", branch], wt)
            body_file = tmp / "body.md"
            body_file.write_text(body)
            out = _run(
                [
                    *self.gh,
                    "pr",
                    "create",
                    "--base",
                    "main",
                    "--head",
                    branch,
                    "--title",
                    title,
                    "--body-file",
                    str(body_file),
                ],
                wt,
            )
            url = out.strip().splitlines()[-1] if out.strip() else ""
            auto = True
            try:  # GitHub merges once the required checks are green
                _run([*self.gh, "pr", "merge", url or branch, "--auto", "--squash"], wt)
            except PublishError as exc:  # auto-merge disabled on the repo: owner merges
                log.warning("experiments.publish.no_auto_merge", branch=branch, error=str(exc))
                auto = False
        finally:
            _run(["git", "worktree", "remove", "--force", str(wt)], self.repo, check=False)
            shutil.rmtree(tmp, ignore_errors=True)
        log.info("experiments.publish.opened", branch=branch, url=url, auto_merge=auto)
        return PublishResult(branch, url or None, "opened", auto_merge=auto)
