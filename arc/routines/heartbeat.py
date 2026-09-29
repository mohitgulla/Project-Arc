"""Heartbeats for routine runs: one post per job in the #arc-investor day thread.

Policy (E5.4 §3, tuned in E5.3, cards in E5.5):

- Persona jobs post a digest card (``notify: card``, the default): the Block
  Kit layout from :mod:`arc.slack.digests`, with the old one-line summary kept
  as the notification fallback text. A job whose handler returns no card
  falls back to the one-liner.
- ``notify: summary`` posts the one-liner only.
- Source jobs are quiet by default: their summaries queue in ``routine_state``
  and are folded into the next persona heartbeat, so the day thread is not
  spammed every 15 minutes. Repeated runs of one source fold into a single
  entry (run count, total new docs, last summary).
- Failures always alert (one-line format), whatever ``notify`` says. A handler
  can also raise an immediate notice (e.g. the intraday monitor tripping the daily-loss halt).
- Day thread: a post goes to today's session thread while today is a trading
  session and it is before ``heartbeat.day_rollover`` (default 20:00 ET).
  Later posts (the 22:00 Scout) and weekend/holiday posts go to the **next**
  session's thread, so Sunday night's StockedUp run lands in Monday's thread.

Posting is best-effort: a Slack error is logged and never fails the run.
"""

from __future__ import annotations

import datetime as _dt
import json
from typing import TYPE_CHECKING, Any, Protocol

import structlog

from arc.routines.runs import RoutineStateRepo
from arc.slack import blocks as B
from arc.slack.personas import Persona, persona_label
from arc.utils.calendar import ET, is_session, next_session

if TYPE_CHECKING:
    import sqlite3

log = structlog.get_logger(__name__)

Blocks = list[dict[str, Any]]

_PENDING_KEY = "heartbeat:pending_sources"
_MAX_PENDING_JOBS = 50
_ROUTINES_LABEL = "[Routines]"
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


def thread_day(now: _dt.datetime, rollover: _dt.time = _dt.time(20, 0)) -> _dt.date:
    """The trading session whose #arc-investor day thread a post at *now* belongs to."""
    now = now.astimezone(ET)
    d = now.date()
    if is_session(d) and now.time() < rollover:
        return d
    return next_session(d)


class Notifier(Protocol):
    def post(self, day: _dt.date, text: str, blocks: Blocks | None = None) -> str | None:
        """Post *text*; return the message ``ts`` when the backend has one (D27 manifest)."""
        ...


class LogNotifier:
    """Writes heartbeats to the structured log only (dry runs, no Slack token)."""

    def post(self, day: _dt.date, text: str, blocks: Blocks | None = None) -> str | None:
        log.info("routines.heartbeat", day=day.isoformat(), text=text, blocks=len(blocks or []))
        return None


class RecordingNotifier:
    """Keeps posted lines (and any blocks) in memory (tests)."""

    def __init__(self) -> None:
        self.posts: list[tuple[_dt.date, str]] = []
        self.blocks: list[Blocks | None] = []

    def post(self, day: _dt.date, text: str, blocks: Blocks | None = None) -> str | None:
        self.posts.append((day, text))
        self.blocks.append(blocks)
        return f"rec-{len(self.posts)}"


def day_thread_ts(conn: sqlite3.Connection, client: object, day: _dt.date) -> str:
    """``ts`` of the ``📅 <date> · session`` root in #arc-investor, created on first use.

    Shared by heartbeats and proposal cards (E6.1) so both land in one thread.
    """
    from arc.slack.client import ArcSlackClient

    state = RoutineStateRepo(conn)
    key = f"day_thread:{day.isoformat()}"
    ts = state.get(key)
    if ts:
        return ts
    assert isinstance(client, ArcSlackClient)
    resp = client.post_daily_session(day)
    ts = str(resp["ts"])
    state.set(key, ts)
    _post_day_banner(conn, client, ts)
    return ts


def day_banner(conn: sqlite3.Connection) -> str:
    """D34: the first line of every day thread, ``Auto-approve: ON (paper)`` etc.

    Read from the effective config (store overrides included) so a flip made
    through ``arc approve auto`` / ``!arc config`` shows on the next day thread.
    """
    from arc.approvals.auto import notice_text
    from arc.control.effective import effective_settings

    s = effective_settings(conn)
    return notice_text(s.env.value, bool(s.auto_approve))


def _post_day_banner(conn: sqlite3.Connection, client: object, thread_ts: str) -> None:
    from arc.slack.client import CHANNEL_ARC_INVESTOR, ArcSlackClient

    try:
        assert isinstance(client, ArcSlackClient)
        client.reply(channel=CHANNEL_ARC_INVESTOR, thread_ts=thread_ts, text=day_banner(conn))
    except Exception as exc:  # noqa: BLE001 - a banner must never block the thread
        log.warning("routines.day_banner_failed", error=str(exc))


class SlackDayThreadNotifier:
    """Posts into the ``📅 <date> · session`` thread in #arc-investor.

    The thread root is created on first use per day and its ``ts`` is kept in
    ``routine_state`` (``day_thread:<date>``) so later ticks reply in place.
    """

    def __init__(self, conn: sqlite3.Connection, client: object | None = None) -> None:
        from arc.slack.client import ArcSlackClient

        self._conn = conn
        self._client = client if client is not None else ArcSlackClient()

    def _thread_ts(self, day: _dt.date) -> str:
        return day_thread_ts(self._conn, self._client, day)

    def post(self, day: _dt.date, text: str, blocks: Blocks | None = None) -> None:
        from arc.slack.client import CHANNEL_ARC_INVESTOR, ArcSlackClient

        try:
            assert isinstance(self._client, ArcSlackClient)
            resp = self._client.reply(
                channel=CHANNEL_ARC_INVESTOR,
                thread_ts=self._thread_ts(day),
                text=text,
                blocks=blocks,
            )
        except Exception as exc:  # noqa: BLE001 - heartbeats must never fail a run
            log.warning("routines.heartbeat_failed", error=str(exc), text=text)
            return None
        ts = resp.get("ts") if hasattr(resp, "get") else None
        return str(ts) if ts else None


class Heartbeats:
    """Applies the quiet/summary/alert policy on top of a :class:`Notifier`."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        notifier: Notifier,
        *,
        day_rollover: _dt.time = _dt.time(20, 0),
    ) -> None:
        self._state = RoutineStateRepo(conn)
        self._notifier = notifier
        self._rollover = day_rollover

    def day(self, now: _dt.datetime) -> _dt.date:
        return thread_day(now, self._rollover)

    def _pending_raw(self) -> list[dict[str, object]]:
        raw = self._state.get(_PENDING_KEY)
        items = json.loads(raw) if raw else []
        # Pre-E5.3 rows were plain "job: summary" strings.
        out: list[dict[str, object]] = []
        for i in items:
            if isinstance(i, dict):
                out.append(i)
            else:
                job, _, last = str(i).partition(": ")
                out.append({"job": job, "runs": 1, "last": last})
        return out

    def _pending(self) -> list[str]:
        out: list[str] = []
        for item in self._pending_raw():
            runs = int(str(item.get("runs", 1)))
            job, last = item["job"], item.get("last", "")
            if runs == 1:
                out.append(f"{job}: {last}".rstrip(": "))
            else:
                docs = item.get("new_docs")
                total = f", {docs} new docs total" if docs is not None else ""
                out.append(f"{job} ×{runs}{total} (last: {last})")
        return out

    def queue_source(self, job: str, summary: str, *, new_docs: int | None = None) -> None:
        """Fold a quiet run into the pending list (one entry per job)."""
        items = self._pending_raw()
        entry = next((i for i in items if i["job"] == job), None)
        if entry is None:
            entry = {"job": job, "runs": 0}
            items.append(entry)
        entry["runs"] = int(str(entry.get("runs", 0))) + 1
        entry["last"] = summary
        if new_docs is not None:
            entry["new_docs"] = int(str(entry.get("new_docs") or 0)) + new_docs
        self._state.set(_PENDING_KEY, json.dumps(items[-_MAX_PENDING_JOBS:]))

    def summary(
        self, now: _dt.datetime, job: str, text: str, *, blocks: Blocks | None = None
    ) -> str | None:
        """Post a run's heartbeat.

        ``text`` is the one-line summary and always the fallback text (what a
        notification shows). With ``blocks`` (a digest card) the post is the
        card; queued source summaries are folded into both.

        E5.5b: a ``[Routines]`` line (non-persona job) is posted inside a ```
        code block, folded sources included. On a card the folded sources go in
        a ``[Scout] Session notes`` section before the audit footer (the sources
        belong to the Scout, whatever card they land on).
        """
        label = label_for(job)
        line = f"{label} {job} ✓ {text}".rstrip()
        pending = self._pending()
        if pending:
            folded = f"sources since last update: {'; '.join(pending)}"
            line += f"\n> {folded}"
            if blocks:
                blocks = _fold_sources_into_card(blocks, folded)
            self._state.delete(_PENDING_KEY)
        if label == _ROUTINES_LABEL:
            line = B.code_block(line)
        return self._notifier.post(self.day(now), line, blocks or None)

    def notice(self, now: _dt.datetime, job: str, text: str) -> str | None:
        """An immediate, non-failure alert raised by a handler (always posted)."""
        return self._notifier.post(self.day(now), f":warning: {_detail(job, text)}")

    def alert(
        self, now: _dt.datetime, job: str, text: str, *, run_id: str | None = None
    ) -> str | None:
        # E8.2: the run id lets a Slack alert be traced (`arc health trace <run_id>`).
        ref = f" `{run_id}`" if run_id else ""
        return self._notifier.post(
            self.day(now), f":rotating_light: {_detail(job, f'FAILED: {text}{ref}', sep=' ')}"
        )


def _detail(job: str, text: str, *, sep: str = ": ") -> str:
    """``[Label] job<sep>text``; for ``[Routines]`` jobs the whole detail is fenced.

    Alerts keep their emoji outside the fence (the notification stays readable).
    """
    body = f"{label_for(job)} {job}{sep}{text}"
    return B.code_block(body) if label_for(job) == _ROUTINES_LABEL else body


def _fold_sources_into_card(blocks: Blocks, folded: str) -> Blocks:
    """Put the folded sources line in ``[Scout] Session notes`` before the footer.

    The footer (a context block of audit ids, always last on a digest card) stays
    last. When the card already has a ``[Scout] Session notes`` section, the line
    is appended to it; otherwise a new one is inserted before the footer.
    """
    body = [*blocks]
    footer = body.pop() if body and body[-1].get("type") == "context" else None
    title = f"*{persona_label(Persona.SCOUT)} Session notes*"
    idx = next(
        (
            i
            for i, b in enumerate(body)
            if b.get("type") == "section" and b.get("text", {}).get("text", "").startswith(title)
        ),
        None,
    )
    if idx is not None:
        merged = f"{body[idx]['text']['text']}\n{B.esc(folded)}"
        body[idx] = {"type": "section", "text": {"type": "mrkdwn", "text": B.clip(merged)}}
    else:
        body = body[: B.MAX_BLOCKS - 2]
        section = B.persona_section(Persona.SCOUT, "Session notes", folded)
        assert section is not None  # ``folded`` is never empty
        body.append(section)
    if footer is not None:
        body.append(footer)
    return body
