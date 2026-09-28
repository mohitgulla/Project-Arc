"""Run-id correlation across Kanban -> cron -> dispatcher -> Slack (E8.2, PLAN §6.12).

One set of ids follows a unit of work through every layer:

- ``tick_id``      minted by the Hermes cron script per tick (``ARC_TICK_ID``), or
                   by ``arc routines tick`` itself when run by hand
- ``cron_job``     the Hermes cron job name (``ARC_CRON_JOB``, set by the script)
- ``kanban_task``  ``HERMES_KANBAN_TASK`` when a Kanban worker runs the command
- ``hermes_session`` ``HERMES_SESSION_ID`` when run from a Hermes session
- ``run_id`` / ``chain_run_id`` / ``job``  bound by the dispatcher per routine run

:func:`bind` puts them in structlog's context variables, so every log line
emitted while they are bound carries them (``merge_contextvars`` is first in
the processor chain, see :mod:`arc.monitoring.logs`). The same dict is stored on
the ``heartbeats`` row and printed on Slack ops alerts, so a Slack post, a log
line and a DB row can be joined on one id (``arc health trace <id>``).
"""

from __future__ import annotations

import os
import uuid
from contextlib import contextmanager
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

# env var -> correlation key. Values are ids only, never secrets.
ENV_KEYS: dict[str, str] = {
    "ARC_TICK_ID": "tick_id",
    "ARC_CRON_JOB": "cron_job",
    "HERMES_KANBAN_TASK": "kanban_task",
    "HERMES_SESSION_ID": "hermes_session",
}


def new_tick_id() -> str:
    return f"tick-{uuid.uuid4().hex[:12]}"


def from_env(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """Correlation ids present in the environment (empty values dropped)."""
    env = os.environ if environ is None else environ
    return {key: env[var] for var, key in ENV_KEYS.items() if env.get(var)}


def tick_correlation(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """Ids for one tick: the environment's, plus a fresh ``tick_id`` when none was passed."""
    ids = from_env(environ)
    ids.setdefault("tick_id", new_tick_id())
    return ids


@contextmanager
def bind(**ids: str | int | None) -> Iterator[None]:
    """Bind non-empty ids to every structlog line emitted inside the block."""
    clean = {k: v for k, v in ids.items() if v is not None and v != ""}
    with structlog.contextvars.bound_contextvars(**clean):
        yield
