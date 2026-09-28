"""Slack side of approval cards: post into the #arc-investor day thread (E6.1)."""

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
    """Posts cards as replies in the day thread shared with the routine heartbeats."""

    def __init__(self, conn: sqlite3.Connection, client: ArcSlackClient | None = None) -> None:
        from arc.slack.client import ArcSlackClient

        self._conn = conn
        self._client = client if client is not None else ArcSlackClient()

    def post(self, day: _dt.date, view: CardView) -> PostedCard:
        from arc.routines.heartbeat import day_thread_ts
        from arc.slack.client import CHANNEL_ARC_INVESTOR

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

    def notify_user(self, channel: str, user: str, text: str, thread_ts: str | None) -> None:
        self._client.ephemeral(channel=channel, user=user, text=text, thread_ts=thread_ts)
