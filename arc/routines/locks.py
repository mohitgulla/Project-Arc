"""File locks for the dispatcher (POSIX ``flock``; released when the process dies).

- One lock per job, so an overlapping tick can't run the same job twice at once.
- One global ``llm`` lock, taken only by jobs whose persona routes to a local,
  on-device model (``local: true`` in ``config/llm_routing.yaml``, D39), so local
  runs happen one at a time. Remote API routes run concurrently.

Locks are non-blocking: a job whose lock is held is *deferred* (not recorded),
and the next tick picks it up while it is still inside its catch-up window.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

LLM_LOCK = "_llm"
_SAFE = re.compile(r"[^A-Za-z0-9_.-]")


#: E15.4: an experiment arm's lock dir is ``<control lock dir>/arm-<xp>-<arm>``.
ARM_LOCK_PREFIX = "arm-"


def arm_lock_dir(lock_dir: Path | str, experiment_id: str, arm: str) -> Path:
    """The lock dir of arm *arm* of *experiment_id* under control's *lock_dir* (D69:
    two experiments may reuse an arm name, so the dir is per experiment + arm)."""
    return Path(lock_dir) / f"{ARM_LOCK_PREFIX}{experiment_id}-{arm}"


def llm_lock_dir(lock_dir: Path | str) -> Path:
    """Where *lock_dir*'s :data:`LLM_LOCK` lives: control's dir for an arm's dir."""
    p = Path(lock_dir)
    return p.parent if p.name.startswith(ARM_LOCK_PREFIX) else p


class LockBusyError(RuntimeError):
    """The lock is held by another process."""


class LockManager:
    """Hands out non-blocking exclusive file locks under *lock_dir*.

    E15.4 (D69): the global :data:`LLM_LOCK` lives in :func:`llm_lock_dir`, so the
    experiment arms (each with its own lock dir ``<dir>/arm-<xp>-<arm>``, also handed
    to their detached children) share control's D39 local-tier lock: local LLM runs
    stay serialised across control and every arm; remote routes run concurrently.
    """

    def __init__(self, lock_dir: Path | str, *, llm_dir: Path | str | None = None) -> None:
        self.lock_dir = Path(lock_dir)
        self.llm_dir = Path(llm_dir) if llm_dir is not None else llm_lock_dir(self.lock_dir)

    def _path(self, name: str) -> Path:
        base = self.llm_dir if name == LLM_LOCK else self.lock_dir
        return base / f"{_SAFE.sub('_', name)}.lock"

    @contextlib.contextmanager
    def hold(self, *names: str) -> Iterator[None]:
        """Acquire every lock in *names* (in order) or raise :class:`LockBusyError`."""
        self.lock_dir.mkdir(parents=True, exist_ok=True)
        fds: list[int] = []
        try:
            for name in names:
                path = self._path(name)
                path.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    os.close(fd)
                    msg = f"lock {name!r} is held by another process"
                    raise LockBusyError(msg) from None
                fds.append(fd)
            yield
        finally:
            for fd in reversed(fds):
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)

    def discard(self, name: str) -> None:
        """Remove *name*'s lock file (E11.2: per-run locks would otherwise pile up)."""
        with contextlib.suppress(OSError):
            self._path(name).unlink()


class NullLocks(LockManager):
    """No-op locks for dry runs."""

    def __init__(self) -> None:
        super().__init__(Path("."))

    @contextlib.contextmanager
    def hold(self, *names: str) -> Iterator[None]:
        yield

    def discard(self, name: str) -> None:
        return
