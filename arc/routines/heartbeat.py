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
  session and it is before ``heartbeat.day_rollover`` (default 24:00 ET, so the
  22:00 Scout stays in that day's thread). Weekend/holiday posts go to the
  **next** session's thread, so Sunday night's StockedUp run lands in Monday's.

Posting is best-effort: a Slack error is logged and never fails the run.
"""

from __future__ import annotations

import datetime as _dt
import json
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

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


def thread_day(now: _dt.datetime, rollover: _dt.time = _dt.time.max) -> _dt.date:
    """The trading session whose #arc-investor day thread a post at *now* belongs to.

    ``rollover = time.max`` ("24:00") keeps a trading day's posts in its own thread
    until midnight ET.
    """
    now = now.astimezone(ET)
    d = now.date()
    if is_session(d) and (rollover == _dt.time.max or now.time() < rollover):
        return d
    return next_session(d)


class Notifier(Protocol):
    def post(self, day: _dt.date, text: str, blocks: Blocks | None = None) -> str | None:
        """Post *text*; return the message ``ts`` when the backend has one (D27 manifest)."""
        ...


@runtime_checkable
class ThreadBound(Protocol):
    """D36: a notifier / poster that can redirect its posts into one loop's thread.

    ``bind_thread(ts)`` routes every following ``post`` as a reply to *ts* (a loop
    root in #arc-investor) until ``bind_thread(None)``. ``post_root`` posts a new
    root in the channel and ``update_root`` edits one.
    """

    def bind_thread(self, ts: str | None) -> None: ...

    def post_root(self, text: str) -> str | None: ...

    def update_root(self, ts: str, text: str) -> None: ...


class LogNotifier:
    """Writes heartbeats to the structured log only (dry runs, no Slack token)."""

    def __init__(self) -> None:
        self.thread_ts: str | None = None
        self._roots = 0

    def post(self, day: _dt.date, text: str, blocks: Blocks | None = None) -> str | None:
        log.info(
            "routines.heartbeat",
            day=day.isoformat(),
            text=text,
            blocks=len(blocks or []),
            thread_ts=self.thread_ts,
        )
        return None

    def bind_thread(self, ts: str | None) -> None:
        self.thread_ts = ts

    def post_root(self, text: str) -> str | None:
        self._roots += 1
        log.info("routines.loop_root", text=text)
        return f"log-root-{self._roots}"

    def update_root(self, ts: str, text: str) -> None:
        log.info("routines.loop_root_update", ts=ts, text=text)


class RecordingNotifier:
    """Keeps posted lines (and any blocks) in memory (tests)."""

    def __init__(self) -> None:
        self.posts: list[tuple[_dt.date, str]] = []
        self.blocks: list[Blocks | None] = []
        self.threads: list[str | None] = []  # the bound thread at each post (D36)
        self.roots: dict[str, str] = {}  # ts -> current text (D36 root lines)
        self.root_edits: list[tuple[str, str]] = []
        self.thread_ts: str | None = None

    def post(self, day: _dt.date, text: str, blocks: Blocks | None = None) -> str | None:
        self.posts.append((day, text))
        self.blocks.append(blocks)
        self.threads.append(self.thread_ts)
        return f"rec-{len(self.posts)}"

    def bind_thread(self, ts: str | None) -> None:
        self.thread_ts = ts

    def post_root(self, text: str) -> str | None:
        ts = f"root-{len(self.roots) + 1}"
        self.roots[ts] = text
        return ts

    def update_root(self, ts: str, text: str) -> None:
        self.roots[ts] = text
        self.root_edits.append((ts, text))

    def in_thread(self, ts: str) -> list[str]:
        return [t for (_, t), th in zip(self.posts, self.threads, strict=True) if th == ts]

    def day_thread_posts(self) -> list[str]:
        return [t for (_, t), th in zip(self.posts, self.threads, strict=True) if th is None]


def day_thread_ts(conn: sqlite3.Connection, client: object, day: _dt.date) -> str:
    """``ts`` of the ``💡 <Ddd Mmm D> · Session Notes`` root in #arc-investor, created on first use.

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
    _post_day_banner(conn, client, ts, day)
    return ts


def banner_key(day: _dt.date) -> str:
    """``routine_state`` key of the auto-approve state last shown in *day*'s thread."""
    return f"day_banner:{day.isoformat()}"


def day_banner(conn: sqlite3.Connection) -> str:
    """D34: the first line of every day thread, ``Auto-approve: ON (paper)`` etc.

    Read from the effective config (store overrides included) so a flip made
    through ``arc approve auto`` / ``!arc config`` shows on the next day thread.
    """
    from arc.approvals.auto import notice_text
    from arc.control.effective import effective_settings

    s = effective_settings(conn)
    return notice_text(s.env.value, bool(s.auto_approve))


def _post_day_banner(
    conn: sqlite3.Connection, client: object, thread_ts: str, day: _dt.date
) -> None:
    from arc.slack.client import CHANNEL_ARC_INVESTOR, ArcSlackClient

    try:
        assert isinstance(client, ArcSlackClient)
        text = day_banner(conn)
        client.reply(channel=CHANNEL_ARC_INVESTOR, thread_ts=thread_ts, text=text)
        # The auto-approve notice skips a flip that only repeats this line.
        RoutineStateRepo(conn).set(banner_key(day), text)
    except Exception as exc:  # noqa: BLE001 - a banner must never block the thread
        log.warning("routines.day_banner_failed", error=str(exc))


class SlackDayThreadNotifier:
    """Posts into the ``💡 <Ddd Mmm D> · Session Notes`` thread in #arc-investor.

    The thread root is created on first use per day and its ``ts`` is kept in
    ``routine_state`` (``day_thread:<date>``) so later ticks reply in place.
    """

    def __init__(self, conn: sqlite3.Connection, client: object | None = None) -> None:
        from arc.slack.client import ArcSlackClient

        self._conn = conn
        self._client = client if client is not None else ArcSlackClient()
        self.thread_ts: str | None = None  # D36: bound loop root, else the day thread

    def _thread_ts(self, day: _dt.date) -> str:
        if self.thread_ts:
            return self.thread_ts
        return day_thread_ts(self._conn, self._client, day)

    def bind_thread(self, ts: str | None) -> None:
        self.thread_ts = ts

    def post_root(self, text: str) -> str | None:
        """D36: a new one-line loop root in #arc-investor (not in the day thread)."""
        from arc.slack.client import CHANNEL_ARC_INVESTOR, ArcSlackClient

        try:
            assert isinstance(self._client, ArcSlackClient)
            resp = self._client.post_thread_root(channel=CHANNEL_ARC_INVESTOR, text=text)
        except Exception as exc:  # noqa: BLE001 - a missing root must never fail the loop
            log.warning("routines.loop_root_failed", error=str(exc), text=text)
            return None
        ts = resp.get("ts") if hasattr(resp, "get") else None
        return str(ts) if ts else None

    def update_root(self, ts: str, text: str) -> None:
        from arc.slack.client import CHANNEL_ARC_INVESTOR, ArcSlackClient

        assert isinstance(self._client, ArcSlackClient)
        self._client.update(channel=CHANNEL_ARC_INVESTOR, ts=ts, text=text)

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
        day_rollover: _dt.time = _dt.time.max,
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
        return self._notifier.post(self.day(now), f":warning: {_detail(job, text, inline=True)}")

    def card(self, now: _dt.datetime, text: str, blocks: Blocks | None = None) -> str | None:
        """A handler-rendered post (E10.5: ``[XP-n] Day …`` line / stop card), as-is.

        The handler owns the label and layout (:mod:`arc.slack.blocks`); nothing is
        folded in and no code fence is added.
        """
        return self._notifier.post(self.day(now), text, blocks or None)

    def alert(
        self, now: _dt.datetime, job: str, text: str, *, run_id: str | None = None
    ) -> str | None:
        # E8.2: the run id lets a Slack alert be traced (`arc health trace <run_id>`).
        ref = f" `{run_id}`" if run_id else ""
        detail = _detail(job, f"FAILED: {text}", sep=" ", inline=True)
        return self._notifier.post(self.day(now), f":rotating_light: {detail}{ref}")

    # -- D36: one root per loop slot ---------------------------------------

    def open_loop_root(self, text: str) -> str | None:
        """Post a loop root line in the channel and route later posts into its thread.

        Returns the root ``ts`` (None when the notifier cannot post roots, e.g. a
        plain fake, in which case posts keep going to the day thread).
        """
        n = self._notifier
        if not isinstance(n, ThreadBound):
            return None
        ts = n.post_root(text)
        if ts:
            n.bind_thread(ts)
        return ts

    def close_loop_root(self) -> None:
        n = self._notifier
        if isinstance(n, ThreadBound):
            n.bind_thread(None)

    def update_loop_root(self, ts: str, text: str) -> None:
        n = self._notifier
        if isinstance(n, ThreadBound):
            n.update_root(ts, text)

    def loop_metadata(self, now: _dt.datetime, text: str) -> str | None:
        """The ``[Routines]`` reply in a loop thread (step durations, digest, run ids)."""
        return self._notifier.post(self.day(now), B.code_block(f"{_ROUTINES_LABEL} {text}"))


def _detail(job: str, text: str, *, sep: str = ": ", inline: bool = False) -> str:
    """``[Label] job<sep>text``; for ``[Routines]`` jobs the whole detail is code.

    Alerts keep their emoji outside the code. With *inline* (notices and alerts)
    a one-line detail is inline code, so it stays on the emoji's line (owner
    2026-09-30); a multi-line detail is still fenced.
    """
    body = f"{label_for(job)} {job}{sep}{text}"
    if label_for(job) != _ROUTINES_LABEL:
        return body
    if inline and "\n" not in body:
        return "`" + body.replace("`", "'") + "`"
    return B.code_block(body)


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
