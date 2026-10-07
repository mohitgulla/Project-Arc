"""Persona label formatting for Slack messages.

E13.13 (D56): every persona post leads with an emoji label, ``🔭 [Scout]``. The
emoji map lives here only; the heartbeat, the digest cards and the approval card
read it (the Tower mirrors it in ``web/src/lib/performance.ts``).
"""

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


#: E13.13 (D56): one emoji per persona (PLAN §2.5).
PERSONA_EMOJI: dict[Persona, str] = {
    Persona.SCOUT: "🔭",
    Persona.SCALP: "⚡",
    Persona.RESEARCH: "🧠",
    Persona.QUANT: "📐",
    Persona.RISK: "🛡️",
    Persona.BROKER: "🏦",
    Persona.OPS: "⚙️",
}

#: Stored persona keys (``note.persona``, routine job prefixes) -> persona. The last
#: four are pre-D56 names; they read as the current persona (``arc.journal.legacy``).
PERSONA_KEYS: dict[str, Persona] = {
    "scout": Persona.SCOUT,
    "scalp": Persona.SCALP,
    "research": Persona.RESEARCH,
    "quant": Persona.QUANT,
    "risk": Persona.RISK,
    "broker": Persona.BROKER,
    "ops": Persona.OPS,
    "scorecard": Persona.OPS,  # D56 (E13.2): the weekly scorecard posts as Ops
    "sweep": Persona.SCALP,
    "director": Persona.RESEARCH,
    "investor": Persona.BROKER,
    "auditor": Persona.BROKER,
}


def persona_label(persona: Persona, *, suffix: str = "") -> str:
    """Format a persona tag for Slack message prefixes: ``"🔭 [Scout]"``.

    *suffix* goes inside the brackets: ``"📐 [Quant (revised)]"``.
    """
    return f"{PERSONA_EMOJI[persona]} [{persona.value}{suffix}]"


def persona_for(key: str) -> Persona | None:
    """The persona of a stored key (``"director"`` -> Research); ``None`` if unknown."""
    return PERSONA_KEYS.get(key.strip().lower())
