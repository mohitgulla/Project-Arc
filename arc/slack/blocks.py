"""Shared Block Kit layout for every persona post in #arc-investor.

One visual grammar for all personas (Scalp, Research, Quant, Risk, Broker,
Ops), so a reviewer reads every card the same way:

- ``header``      one plain-text title line, e.g.
                  ``🤺 [Quant] Proposal: SPY • Oct 30 (35 DTE) • Iron Condor``
- ``summary``     one context line with the at-a-glance facts
- ``facts``       a two-column grid of ``*Label*`` / value pairs
- ``persona``     a section attributed to the persona that wrote it
                  (``*🛡️ [Risk]* Review`` + body), so authorship is never ambiguous
- ``sections``    E13.13: bold-labelled lines (``*Thesis:* …``, ``*Regime:* …``)
- ``bullets``     a titled bullet list (violations, warnings, levels)
- ``footer``      ids for audit (proposal hash, run id, snapshot id)

Pure functions: dicts in, dicts out. Untrusted persona text is escaped here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from arc.context.kinds import NOTE_SECTION_MAX
from arc.context.render import facts_line, note_lines
from arc.slack.personas import Persona, persona_for, persona_label

if TYPE_CHECKING:
    from arc.context.kinds import NotePayload

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
    "render_note",
    "section_text",
    "sections",
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


def section_text(pairs: list[tuple[str, str]], *, escape: bool = True) -> str:
    """``*Label:* text`` lines (E13.13); each text clipped to :data:`NOTE_SECTION_MAX`.

    Empty texts are omitted (never "n/a").
    """
    lines = []
    for label, text in pairs:
        body = text.strip()
        if not body:
            continue
        body = clip(body, NOTE_SECTION_MAX)
        lines.append(f"*{label}:* {esc(body) if escape else body}")
    return "\n".join(lines)


def sections(pairs: list[tuple[str, str]], *, escape: bool = True) -> Block | None:
    """One section of bold-labelled lines: ``*Thesis:* …`` / ``*Regime:* …`` (E13.13).

    ``None`` when every text is empty, so callers can skip it.
    """
    text = section_text(pairs, escape=escape)
    if not text:
        return None
    return {"type": "section", "text": {"type": "mrkdwn", "text": clip(text)}}


def render_note(note: NotePayload) -> list[Block]:
    """A ``note`` as blocks (E13.13): the persona-labelled title, its sections, the facts.

    Same lines as :func:`arc.context.render.render_note_text` (the Tower), with the
    labels bold and the text escaped. A legacy body-only note renders its body.
    """
    persona = persona_for(note.persona) or Persona.OPS
    pairs = note_lines(note)
    head = f"*{persona_label(persona)} {esc(note.title)}*"
    body = [
        f"*{label}:* {esc(clip(t, NOTE_SECTION_MAX))}" if label else esc(t) for label, t in pairs
    ]
    out: list[Block] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": clip("\n".join([head, *body]))}}
    ]
    if note.facts:
        out.append(summary(esc(facts_line(note.facts))))
    return out


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
