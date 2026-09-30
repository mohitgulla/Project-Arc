"""Slack side of approval cards (E6.1 / D36).

Cards go into the thread of the loop that produced the proposal when that loop
has a root line in #arc-investor (D36); otherwise into the day thread shared
with the routine heartbeats. Decisions and fills re-render the loop's root line.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from arc.approvals.service import PostedCard

if TYPE_CHECKING:
    import datetime as _dt
    import sqlite3

    from arc.approvals.card import CardView
    from arc.slack.client import ArcSlackClient

__all__ = ["SlackCardPoster"]


class SlackCardPoster:
    """Posts cards as replies in the loop thread (D36) or the day thread."""

    def __init__(self, conn: sqlite3.Connection, client: ArcSlackClient | None = None) -> None:
        from arc.slack.client import ArcSlackClient

        self._conn = conn
        self._client = client if client is not None else ArcSlackClient()

    def post(self, day: _dt.date, view: CardView, *, chain_run_id: str | None = None) -> PostedCard:
        from arc.routines.heartbeat import day_thread_ts
        from arc.routines.loop import LoopState
        from arc.slack.client import CHANNEL_ARC_INVESTOR

        thread_ts = LoopState(self._conn).thread_ts(chain_run_id) if chain_run_id else None
        if thread_ts is None:
            thread_ts = day_thread_ts(self._conn, self._client, day)
        resp = self._client.reply(
            channel=CHANNEL_ARC_INVESTOR, thread_ts=thread_ts, text=view.text, blocks=view.blocks
        )
        return PostedCard(
            channel=str(resp.get("channel") or CHANNEL_ARC_INVESTOR),
            thread_ts=thread_ts,
            message_ts=str(resp["ts"]),
        )

    def update(self, channel: str, message_ts: str, view: CardView) -> None:
        self._client.update(channel=channel, ts=message_ts, text=view.text, blocks=view.blocks)

    def update_root(self, ts: str, text: str) -> None:
        from arc.slack.client import CHANNEL_ARC_INVESTOR

        self._client.update(channel=CHANNEL_ARC_INVESTOR, ts=ts, text=text)

    def notify_user(self, channel: str, user: str, text: str, thread_ts: str | None) -> None:
        self._client.ephemeral(channel=channel, user=user, text=text, thread_ts=thread_ts)
