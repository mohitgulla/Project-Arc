"""Make ``~/.hermes/.env`` win over stale inherited ``ALPACA_*`` values for a local test run.

The agent's terminal (and every kanban worker it spawns) inherits the Hermes
gateway's environment from when the gateway started. When the Alpaca keys in
``~/.hermes/.env`` are rotated (D80 account swap) the inherited copies go stale,
so the integration tests 401 locally while CI is green, and every post-merge
suite needs a hand re-export to tell a stale-key failure from a real one.

``arc_routines_tick.py`` already lets ``.env`` win for the live tick; this does
the same for pytest. It only **refreshes** ``ALPACA_*`` names that are already
exported; it never adds one, so which integration tests run or skip doesn't
change. It's a no-op when the file is missing (CI has none). It runs once at
``pytest_configure``, before any test, so a test's own
``monkeypatch.setenv``/``delenv`` still applies on top.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

from dotenv import dotenv_values

if TYPE_CHECKING:
    from collections.abc import MutableMapping

__all__ = ["HERMES_ENV", "refresh_alpaca_env"]

HERMES_ENV = Path.home() / ".hermes" / ".env"
_PREFIX = "ALPACA_"


def refresh_alpaca_env(
    env_file: Path = HERMES_ENV, environ: MutableMapping[str, str] | None = None
) -> list[str]:
    """Overwrite already-set ``ALPACA_*`` vars in ``environ`` with the file's values.

    Returns the names whose value changed (never the values). Names not already in
    ``environ`` and empty values in the file are ignored.
    """
    target = os.environ if environ is None else environ
    if not env_file.is_file():
        return []
    changed: list[str] = []
    for key, value in dotenv_values(env_file).items():
        if not key.startswith(_PREFIX) or not value or key not in target:
            continue
        if target[key] != value:
            target[key] = value
            changed.append(key)
    return sorted(changed)
