"""Persona label formatting for Slack messages."""

from __future__ import annotations

from enum import StrEnum


class Persona(StrEnum):
    """Arc persona identifiers matching PLAN.md §2.4."""

    SCALP = "Scalp"
    SCOUT = "Scout"  # E13.7 (D56): the daily slow-feed read
    RESEARCH = "Research"
    QUANT = "Quant"
    RISK = "Risk"
    BROKER = "Broker"  # D56 (E13.2): order ladders and the post-market reconcile
    OPS = "Ops"  # D56 (E13.2): the weekly scorecard


def persona_label(persona: Persona) -> str:
    """Format a persona tag for Slack message prefixes.

    Returns e.g. ``"[Scalp]"`` — used at the start of persona-labelled
    messages in #arc-investor threads.
    """
    return f"[{persona.value}]"
