"""``!halt`` / ``!resume`` command parser stub.

Full enforcement lands in E3.3 (kill switch + daily halt). This module
only parses the command text and returns a structured result; it does
not mutate any state.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class CommandVerb(StrEnum):
    """Recognised bang-commands."""

    HALT = "halt"
    RESUME = "resume"


@dataclass(frozen=True, slots=True)
class ParsedCommand:
    """Result of parsing a ``!command`` message."""

    verb: CommandVerb
    raw_text: str
    slack_user: str


def parse_command(text: str, *, slack_user: str = "") -> ParsedCommand | None:
    """Parse a ``!halt`` or ``!resume`` message.

    Returns ``None`` if the text is not a recognised command.
    Leading/trailing whitespace and case are ignored.
    Only the first token after ``!`` is inspected.

    Parameters
    ----------
    text:
        Raw Slack message text.
    slack_user:
        Slack user ID of the sender (carried through for downstream use).
    """
    stripped = text.strip()
    if not stripped.startswith("!"):
        return None

    token = stripped.split()[0][1:].lower()  # drop the '!' prefix
    try:
        verb = CommandVerb(token)
    except ValueError:
        return None

    return ParsedCommand(verb=verb, raw_text=stripped, slack_user=slack_user)
