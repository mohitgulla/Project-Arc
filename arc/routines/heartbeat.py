"""Heartbeats for routine runs: one line per job in the #arc-investor day thread.

Policy (E5.4 §3):

- Persona jobs post a one-line summary (``notify: summary``, the default).
- Source jobs are quiet by default: their summaries queue in ``routine_state``
  and are folded into the next persona heartbeat, so the day thread is not
  spammed every 15 minutes.
- Failures always alert, whatever ``notify`` says.

Posting is best-effort: a Slack error is logged and never fails the run.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Protocol

import structlog

from arc.routines.runs import RoutineStateRepo

if TYPE_CHECKING:
    import datetime as _dt
    import sqlite3

log = structlog.get_logger(__name__)

_PENDING_KEY = "heartbeat:pending_sources"
_PERSONA_LABELS = {
    "scout": "[Scout]",
    "director": "[Director]",
    "quant": "[Quant]",
    "risk": "[Risk]",
    "investor": "[Investor]",
    "auditor": "[Auditor]",
}


def label_for(job: str) -> str:
    """Slack label for a job: the persona tag (D12) or ``[Routines]``."""
    return _PERSONA_LABELS.get(job.split(".", 1)[0], "[Routines]")


class Notifier(Protocol):
    def post(self, day: _dt.date, text: str) -> None: ...


class LogNotifier:
    """Writes heartbeats to the structured log only (dry runs, no Slack token)."""

    def post(self, day: _dt.date, text: str) -> None:
        log.info("routines.heartbeat", day=day.isoformat(), text=text)


class RecordingNotifier:
    """Keeps posted lines in memory (tests)."""

    def __init__(self) -> None:
        self.posts: list[tuple[_dt.date, str]] = []

    def post(self, day: _dt.date, text: str) -> None:
        self.posts.append((day, text))


class SlackDayThreadNotifier:
    """Posts into the ``📅 <date> · session`` thread in #arc-investor.

    The thread root is created on first use per day and its ``ts`` is kept in
    ``routine_state`` (``day_thread:<date>``) so later ticks reply in place.
    """

    def __init__(self, conn: sqlite3.Connection, client: object | None = None) -> None:
        from arc.slack.client import ArcSlackClient

        self._state = RoutineStateRepo(conn)
        self._client = client if client is not None else ArcSlackClient()

    def _thread_ts(self, day: _dt.date) -> str:
        from arc.slack.client import ArcSlackClient

        key = f"day_thread:{day.isoformat()}"
        ts = self._state.get(key)
        if ts:
            return ts
        assert isinstance(self._client, ArcSlackClient)
        resp = self._client.post_daily_session(day)
        ts = str(resp["ts"])
        self._state.set(key, ts)
        return ts

    def post(self, day: _dt.date, text: str) -> None:
        from arc.slack.client import CHANNEL_ARC_INVESTOR, ArcSlackClient

        try:
            assert isinstance(self._client, ArcSlackClient)
            self._client.reply(
                channel=CHANNEL_ARC_INVESTOR, thread_ts=self._thread_ts(day), text=text
            )
        except Exception as exc:  # noqa: BLE001 - heartbeats must never fail a run
            log.warning("routines.heartbeat_failed", error=str(exc), text=text)


class Heartbeats:
    """Applies the quiet/summary/alert policy on top of a :class:`Notifier`."""

    def __init__(self, conn: sqlite3.Connection, notifier: Notifier) -> None:
        self._state = RoutineStateRepo(conn)
        self._notifier = notifier

    def _pending(self) -> list[str]:
        raw = self._state.get(_PENDING_KEY)
        return list(json.loads(raw)) if raw else []

    def queue_source(self, job: str, summary: str) -> None:
        pending = self._pending()
        pending.append(f"{job}: {summary}")
        self._state.set(_PENDING_KEY, json.dumps(pending[-50:]))

    def summary(self, day: _dt.date, job: str, text: str) -> None:
        line = f"{label_for(job)} {job} ✓ {text}".rstrip()
        pending = self._pending()
        if pending:
            line += f"\n> sources since last update: {'; '.join(pending)}"
            self._state.delete(_PENDING_KEY)
        self._notifier.post(day, line)

    def alert(self, day: _dt.date, job: str, text: str) -> None:
        self._notifier.post(day, f":rotating_light: {label_for(job)} {job} FAILED: {text}")
