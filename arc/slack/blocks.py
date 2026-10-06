"""Shared Block Kit layout for every persona post in #arc-investor.

One visual grammar for all personas (Scalp, Research, Quant, Risk, Broker,
Ops), so a reviewer reads every card the same way:

- ``header``      one plain-text title line, e.g.
                  ``[Quant] Proposal: SPY • Oct 30 (35 DTE) • Iron Condor``
- ``summary``     one context line with the at-a-glance facts
- ``facts``       a two-column grid of ``*Label*`` / value pairs
- ``persona``     a section attributed to the persona that wrote it
                  (``*[Risk]* Review`` + body), so authorship is never ambiguous
- ``bullets``     a titled bullet list (violations, warnings, levels)
- ``footer``      ids for audit (proposal hash, run id, snapshot id)

Pure functions: dicts in, dicts out. Untrusted persona text is escaped here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from arc.slack.personas import Persona, persona_label

__all__ = [
    "HEADER_MAX",
    "MAX_BLOCKS",
    "SECTION_MAX",
    "Block",
    "CardView",
    "bullets",
    "clip",
    "code_block",
    "divider",
    "esc",
    "facts",
    "footer",
    "header",
    "persona_section",
    "summary",
]

Block = dict[str, Any]

HEADER_MAX = 150  # Slack header plain_text limit
SECTION_MAX = 2900  # Slack caps section text at 3000 chars
_FIELD_MAX = 1900  # Slack caps a field at 2000 chars
_MAX_FIELDS = 10  # Slack allows 10 fields per section
MAX_BLOCKS = 50  # Slack allows 50 blocks per message


@dataclass(frozen=True)
class CardView:
    """What gets posted: plain fallback text (notifications) + Block Kit blocks."""

    text: str
    blocks: list[Block]


def esc(text: str) -> str:
    """Escape Slack mrkdwn control characters (persona text is untrusted)."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def clip(text: str, limit: int = SECTION_MAX) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def header(text: str) -> Block:
    return {
        "type": "header",
        "text": {"type": "plain_text", "text": text[:HEADER_MAX], "emoji": True},
    }


def summary(*parts: str) -> Block:
    """One context line of at-a-glance facts joined with `` · `` (parts are mrkdwn)."""
    return {
        "type": "context",
        "elements": [{"type": "mrkdwn", "text": " · ".join(p for p in parts if p)}],
    }


def facts(pairs: list[tuple[str, str]]) -> list[Block]:
    """``*Label*\\nvalue`` fields in a two-column grid; >10 pairs spill into more sections."""
    fields = [
        {"type": "mrkdwn", "text": clip(f"*{label}*\n{value}", _FIELD_MAX)}
        for label, value in pairs
    ]
    return [
        {"type": "section", "fields": fields[i : i + _MAX_FIELDS]}
        for i in range(0, len(fields), _MAX_FIELDS)
    ]


def persona_section(
    persona: Persona, title: str, body: str, *, escape: bool = True
) -> Block | None:
    """A section attributed to ``persona``: ``*[Risk]* Review`` then the body.

    Returns ``None`` for an empty body so callers can skip it.
    """
    text = body.strip()
    if not text:
        return None
    if escape:
        text = esc(text)
    return {
        "type": "section",
        "text": {"type": "mrkdwn", "text": clip(f"*{persona_label(persona)} {title}*\n{text}")},
    }


def bullets(title: str, items: list[str], *, escape: bool = True) -> Block | None:
    if not items:
        return None
    lines = "\n".join(f"• {esc(i) if escape else i}" for i in items)
    return {"type": "section", "text": {"type": "mrkdwn", "text": clip(f"*{title}*\n{lines}")}}


def divider() -> Block:
    return {"type": "divider"}


def code_block(text: str) -> str:
    """Wrap *text* in a ``` fence for mrkdwn (E5.5b: ``[Routines]`` lines).

    A fence inside the content would close ours early, so triple backticks in
    *text* are broken up with zero-width spaces.
    """
    body = text.replace("```", "`\u200b`\u200b`").strip("\n")
    return f"```\n{body}\n```"


def footer(**ids: str | None) -> Block:
    """Audit ids, e.g. ``proposal `ab12…` · run `r-9` ``; empty values are dropped."""
    parts = [f"{k.replace('_', ' ')} `{v}`" for k, v in ids.items() if v]
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": " · ".join(parts) or " "}]}
