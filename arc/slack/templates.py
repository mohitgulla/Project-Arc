"""Slack message templates for Arc channels.

All helpers return plain dicts / strings suitable for ``ArcSlackClient``
posting methods.  Block Kit payloads are plain Python dicts so they can
be serialised by ``slack_sdk`` without extra dependencies.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from arc.slack.personas import Persona, persona_label

if TYPE_CHECKING:
    from datetime import date

# ---------------------------------------------------------------------------
# #project-arc  —  one thread per kanban card
# ---------------------------------------------------------------------------


def card_thread_root(
    *,
    card_id: str,
    title: str,
    assignee: str = "",
    url: str = "",
) -> str:
    """Format the root message for a kanban-card thread in #project-arc.

    Parameters
    ----------
    card_id:
        Short kanban card id (e.g. ``"t_c4c31109"``).
    title:
        Card title.
    assignee:
        Profile name working the card (optional).
    url:
        Link to the PR or board (optional).
    """
    parts = [f"📋 *{card_id}* — {title}"]
    if assignee:
        parts.append(f"Assignee: `{assignee}`")
    if url:
        parts.append(f"<{url}|View>")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# #arc-investor  —  one thread per trading day
# ---------------------------------------------------------------------------


def daily_session_root(session_date: date) -> str:
    """Format the root message for a daily session thread in #arc-investor.

    Parameters
    ----------
    session_date:
        The trading date (``datetime.date``).
    """
    formatted = session_date.strftime("%Y-%m-%d · %A")
    return f"📅 {formatted} · session"


# ---------------------------------------------------------------------------
# Proposal card  —  Block Kit skeleton
# ---------------------------------------------------------------------------


def proposal_card(
    *,
    ticker: str,
    thesis: str,
    structure_summary: str,
    pop: float,
    ev: str,
    sizing: str,
    persona: Persona = Persona.QUANT,
    proposal_id: str = "",
) -> list[dict]:
    """Build a Block Kit payload for a proposal card.

    Returns a list of block dicts.  The caller posts them via
    ``chat_postMessage(blocks=...)`` in #arc-investor.

    The Approve / Reject action buttons carry ``proposal_id`` in the
    ``value`` field so the interaction handler can look up the proposal.
    """
    label = persona_label(persona)
    blocks: list[dict] = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"{label} Proposal: {ticker}",
                "emoji": True,
            },
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": thesis,
            },
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Structure:*\n{structure_summary}"},
                {"type": "mrkdwn", "text": f"*PoP:* {pop:.0%}  |  *EV:* {ev}"},
                {"type": "mrkdwn", "text": f"*Sizing:* {sizing}"},
            ],
        },
        {"type": "divider"},
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "✅ Approve", "emoji": True},
                    "style": "primary",
                    "action_id": "arc_approve",
                    "value": proposal_id,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "❌ Reject", "emoji": True},
                    "style": "danger",
                    "action_id": "arc_reject",
                    "value": proposal_id,
                },
            ],
        },
    ]
    return blocks


# ---------------------------------------------------------------------------
# Halt notice
# ---------------------------------------------------------------------------


def halt_notice(*, triggered_by: str, reason: str = "") -> str:
    """Format a halt notice message.

    Parameters
    ----------
    triggered_by:
        Slack user ID or display name of the person who triggered the halt.
    reason:
        Optional free-text reason.
    """
    msg = f"🛑 *HALT* triggered by <@{triggered_by}>"
    if reason:
        msg += f"\nReason: {reason}"
    msg += "\nAll trading activity is suspended. Only the owner can `!resume`."
    return msg


def daily_loss_halt_notice(*, detail: str) -> str:
    """Auto-halt notice when the daily-loss rule trips (posted to #arc-investor)."""
    return (
        f"🛑 *HALT* — daily loss limit reached\n{detail}\n"
        "All new orders are refused for the rest of the session. Only the owner can `!resume`."
    )


def resume_notice(*, resumed_by: str, cleared: int) -> str:
    """Trading resumed by the owner."""
    if cleared == 0:
        return f"ℹ️ Trading was not halted (`!resume` by <@{resumed_by}>)."
    plural = "" if cleared == 1 else "s"
    return f"✅ *RESUMED* by <@{resumed_by}> — cleared {cleared} halt{plural}. Trading allowed."


def resume_denied_notice(*, user: str) -> str:
    """A non-owner tried to resume."""
    return f"⛔ <@{user}> is not allowed to `!resume`. Only the owner can lift a halt."
