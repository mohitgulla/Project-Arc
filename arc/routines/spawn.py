"""Detached ``arc`` children: the D34 Broker ladders, the D39 background lane, E10 arms.

Moved out of the Investor module in E13.2 (D56) so the generic spawner does not
live under :mod:`arc.broker`.
"""

from __future__ import annotations

import subprocess
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from arc.routines.handlers import RunEnv

__all__ = ["arc_command", "spawn_detached"]


def arc_command(env: RunEnv, args: Sequence[str]) -> list[str]:
    """``arc <args…>`` with this process's interpreter plus the run env's db/config/locks/slack.

    Shared by every detached child: the D34 Broker ladder and the D39 background lane.
    """
    # Same interpreter as this process; `arc.cli:main` is the `arc` console script.
    argv = [sys.executable, "-c", "from arc.cli import main; raise SystemExit(main())"]
    argv += list(args)
    if env.db_path:
        argv += ["--db", env.db_path]
    if env.config_path:
        argv += ["--config", env.config_path]
    if env.lock_dir:
        argv += ["--lock-dir", env.lock_dir]
    if not env.slack:
        argv.append("--no-slack")
    return argv


def spawn_detached(argv: Sequence[str], env: Mapping[str, str] | None = None) -> int:
    """Start an ``arc`` child detached (its own session); returns the pid.

    D34 (Broker ladders) and D39 (the tick's background lane) both use this one
    spawner. *env* defaults to this process's environment.
    """
    proc = subprocess.Popen(  # noqa: S603 - argv is built from our own constants
        list(argv),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        env=dict(env) if env is not None else None,
    )
    return proc.pid
