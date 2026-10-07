"""Plain-text rendering of a ``note`` (E13.13, D56): what the Tower shows.

``render_note_text`` and :func:`arc.slack.blocks.render_note` share
:func:`note_lines`, so Slack and the Tower read the same object the same way: a
section renders ``Label: text`` (Slack: ``*Label:* text``), sections in the fixed
:data:`~arc.context.kinds.NOTE_SECTION_ORDER`, missing sections omitted (never
"n/a"), then one facts line. A v1-style note (``body`` only, no sections) renders
its body unchanged.

No Slack import: the read-only Tower uses this module.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from arc.context.kinds import NOTE_SECTION_ORDER

if TYPE_CHECKING:
    from collections.abc import Mapping

    from arc.context.kinds import NotePayload

__all__ = ["fact_text", "facts_line", "note_lines", "render_note_text"]


def fact_text(value: str | float | bool) -> str:
    """One fact value: ``17.6``, ``23``, ``yes``; floats print in full (never rounded away)."""
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value)


def facts_line(facts: Mapping[str, str | float | bool]) -> str:
    """``vix 17.6 · pool 23`` (insertion order; ``_`` in keys reads as a space)."""
    return " · ".join(f"{k.replace('_', ' ')} {fact_text(v)}" for k, v in facts.items())


def note_lines(note: NotePayload) -> list[tuple[str | None, str]]:
    """``(label, text)`` pairs in render order; ``label`` None = the legacy body."""
    rank = {label: i for i, label in enumerate(NOTE_SECTION_ORDER)}
    ordered = sorted(note.sections, key=lambda s: rank[s.label])
    out: list[tuple[str | None, str]] = [(s.label, s.text.strip()) for s in ordered]
    if not out and note.body and note.body.strip():
        out.append((None, note.body.strip()))
    return out


def render_note_text(note: NotePayload) -> str:
    """The note as plain text: ``Label: text`` lines, then the facts line."""
    lines = [f"{label}: {text}" if label else text for label, text in note_lines(note)]
    if note.facts:
        lines.append(f"Facts: {facts_line(note.facts)}")
    return "\n".join(lines)
