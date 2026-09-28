"""Slack integration: client wrapper, persona labels, message templates."""

from arc.slack.client import ArcSlackClient
from arc.slack.commands import CommandVerb, parse_command
from arc.slack.personas import Persona, persona_label
from arc.slack.templates import (
    card_thread_root,
    daily_session_root,
    halt_notice,
    proposal_card,
)

__all__ = [
    "ArcSlackClient",
    "CommandVerb",
    "Persona",
    "card_thread_root",
    "daily_session_root",
    "halt_notice",
    "parse_command",
    "persona_label",
    "proposal_card",
]
