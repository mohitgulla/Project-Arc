"""Slack client wrapper for Arc channels.

Wraps ``slack_sdk.WebClient`` with Arc-specific helpers:
thread-per-card in #project-arc, persona-labelled posts, and
template-based messages.

Channel IDs and the bot token come from configuration — see
``arc.slack.client.CHANNEL_PROJECT_ARC`` / ``CHANNEL_ARC_INVESTOR``
constants and the ``SLACK_BOT_TOKEN`` env var.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

import structlog
from slack_sdk import WebClient

from arc.slack.personas import Persona, persona_label

if TYPE_CHECKING:
    from slack_sdk.web import SlackResponse

log = structlog.get_logger(__name__)

# Channel IDs from workspace config (PLAN.md §2.5, D2)
CHANNEL_PROJECT_ARC: str = "C0C4KBPN7T5"
CHANNEL_ARC_INVESTOR: str = "C0C4NS1AL3X"


class ArcSlackClient:
    """Thin wrapper around ``slack_sdk.WebClient`` for Arc posting patterns.

    Parameters
    ----------
    client:
        A ``slack_sdk.WebClient`` instance.  When ``None``, one is
        created from the ``SLACK_BOT_TOKEN`` environment variable.
    """

    def __init__(self, client: WebClient | None = None) -> None:
        if client is None:
            token = os.environ.get("SLACK_BOT_TOKEN", "")
            if not token:
                log.warning("SLACK_BOT_TOKEN not set; Slack calls will fail")
            client = WebClient(token=token)
        self._client = client

    # -- low-level helpers ------------------------------------------------

    def post_thread_root(
        self,
        *,
        channel: str,
        text: str,
        blocks: list[dict[str, Any]] | None = None,
    ) -> SlackResponse:
        """Post a new root message (starting a thread).

        Returns the Slack API response; the caller should capture
        ``response["ts"]`` to use as ``thread_ts`` for replies.
        """
        kwargs: dict[str, Any] = {"channel": channel, "text": text}
        if blocks:
            kwargs["blocks"] = blocks
        resp = self._client.chat_postMessage(**kwargs)
        log.info("slack.thread_root", channel=channel, ts=resp.get("ts"))
        return resp

    def reply(
        self,
        *,
        channel: str,
        thread_ts: str,
        text: str,
        blocks: list[dict[str, Any]] | None = None,
        persona: Persona | None = None,
    ) -> SlackResponse:
        """Post a threaded reply, optionally prefixed with a persona label.

        Parameters
        ----------
        persona:
            If provided, the message text is prefixed with ``[Sweep]`` etc.
        """
        if persona is not None:
            text = f"{persona_label(persona)} {text}"
        kwargs: dict[str, Any] = {
            "channel": channel,
            "thread_ts": thread_ts,
            "text": text,
        }
        if blocks:
            kwargs["blocks"] = blocks
        resp = self._client.chat_postMessage(**kwargs)
        log.info("slack.reply", channel=channel, thread_ts=thread_ts, ts=resp.get("ts"))
        return resp

    def update(
        self,
        *,
        channel: str,
        ts: str,
        text: str,
        blocks: list[dict[str, Any]] | None = None,
    ) -> SlackResponse:
        """Update an existing message."""
        kwargs: dict[str, Any] = {"channel": channel, "ts": ts, "text": text}
        if blocks:
            kwargs["blocks"] = blocks
        resp = self._client.chat_update(**kwargs)
        log.info("slack.update", channel=channel, ts=ts)
        return resp

    def ephemeral(
        self, *, channel: str, user: str, text: str, thread_ts: str | None = None
    ) -> SlackResponse:
        """Post a message only *user* can see (e.g. a refused approval click)."""
        kwargs: dict[str, Any] = {"channel": channel, "user": user, "text": text}
        if thread_ts:
            kwargs["thread_ts"] = thread_ts
        resp = self._client.chat_postEphemeral(**kwargs)
        log.info("slack.ephemeral", channel=channel, user=user)
        return resp

    # -- convenience methods for Arc channels ----------------------------

    def post_card_thread(
        self,
        *,
        card_id: str,
        title: str,
        assignee: str = "",
        url: str = "",
    ) -> SlackResponse:
        """Post a card thread root in #project-arc."""
        from arc.slack.templates import card_thread_root

        text = card_thread_root(card_id=card_id, title=title, assignee=assignee, url=url)
        return self.post_thread_root(channel=CHANNEL_PROJECT_ARC, text=text)

    def post_daily_session(self, session_date: Any = None) -> SlackResponse:
        """Post a daily session root in #arc-investor.

        Parameters
        ----------
        session_date:
            ``datetime.date`` for the session.  Defaults to today.
        """
        from datetime import date as _date

        from arc.slack.templates import daily_session_root

        if session_date is None:
            session_date = _date.today()
        text = daily_session_root(session_date)
        return self.post_thread_root(channel=CHANNEL_ARC_INVESTOR, text=text)

    def post_halt(
        self,
        *,
        channel: str,
        thread_ts: str,
        triggered_by: str,
        reason: str = "",
    ) -> SlackResponse:
        """Post a halt notice in a thread."""
        from arc.slack.templates import halt_notice

        text = halt_notice(triggered_by=triggered_by, reason=reason)
        return self.reply(channel=channel, thread_ts=thread_ts, text=text)

    def post_investor(self, *, text: str, thread_ts: str | None = None) -> SlackResponse:
        """Post to #arc-investor: into ``thread_ts`` if given, else as a new root."""
        if thread_ts:
            return self.reply(channel=CHANNEL_ARC_INVESTOR, thread_ts=thread_ts, text=text)
        return self.post_thread_root(channel=CHANNEL_ARC_INVESTOR, text=text)

    def post_proposal(
        self,
        *,
        thread_ts: str,
        ticker: str,
        thesis: str,
        structure_summary: str,
        pop: float,
        ev: str,
        sizing: str,
        persona: Persona = Persona.QUANT,
        proposal_id: str = "",
    ) -> SlackResponse:
        """Post a proposal card (Block Kit) in #arc-investor."""
        from arc.slack.templates import proposal_card

        blocks = proposal_card(
            ticker=ticker,
            thesis=thesis,
            structure_summary=structure_summary,
            pop=pop,
            ev=ev,
            sizing=sizing,
            persona=persona,
            proposal_id=proposal_id,
        )
        label = persona_label(persona)
        fallback = f"{label} Proposal: {ticker}"
        return self.reply(
            channel=CHANNEL_ARC_INVESTOR,
            thread_ts=thread_ts,
            text=fallback,
            blocks=blocks,
        )
