"""Persona label formatting for Slack messages."""

from __future__ import annotations

from enum import StrEnum


class Persona(StrEnum):
    """Arc persona identifiers matching PLAN.md §2.4."""

    SCALP = "Scalp"
    RESEARCH = "Research"
    QUANT = "Quant"
    RISK = "Risk"
    INVESTOR = "Investor"
    AUDITOR = "Auditor"


def persona_label(persona: Persona) -> str:
    """Format a persona tag for Slack message prefixes.

    Returns e.g. ``"[Scalp]"`` — used at the start of persona-labelled
    messages in #arc-investor threads.
    """
    return f"[{persona.value}]"
