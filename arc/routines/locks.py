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


class LockBusyError(RuntimeError):
    """The lock is held by another process."""


class LockManager:
    """Hands out non-blocking exclusive file locks under *lock_dir*."""

    def __init__(self, lock_dir: Path | str) -> None:
        self.lock_dir = Path(lock_dir)

    def _path(self, name: str) -> Path:
        return self.lock_dir / f"{_SAFE.sub('_', name)}.lock"

    @contextlib.contextmanager
    def hold(self, *names: str) -> Iterator[None]:
        """Acquire every lock in *names* (in order) or raise :class:`LockBusyError`."""
        self.lock_dir.mkdir(parents=True, exist_ok=True)
        fds: list[int] = []
        try:
            for name in names:
                fd = os.open(self._path(name), os.O_RDWR | os.O_CREAT, 0o644)
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
