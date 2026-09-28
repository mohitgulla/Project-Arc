"""Persona label formatting for Slack messages."""

from __future__ import annotations

from enum import StrEnum


class Persona(StrEnum):
    """Arc persona identifiers matching PLAN.md §2.4."""

    SCOUT = "Scout"
    DIRECTOR = "Director"
    QUANT = "Quant"
    RISK = "Risk"
    INVESTOR = "Investor"
    AUDITOR = "Auditor"


def persona_label(persona: Persona) -> str:
    """Format a persona tag for Slack message prefixes.

    Returns e.g. ``"[Scout]"`` — used at the start of persona-labelled
    messages in #arc-investor threads.
    """
    return f"[{persona.value}]"
