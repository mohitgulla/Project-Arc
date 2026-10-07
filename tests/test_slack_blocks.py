"""Shared persona Block Kit layout (arc.slack.blocks)."""

from __future__ import annotations

from arc.slack import blocks as B
from arc.slack.personas import Persona


def test_esc_and_clip() -> None:
    assert B.esc("<!here> & co") == "&lt;!here&gt; &amp; co"
    assert B.clip("x" * 10, 5) == "xxxx…"


def test_header_is_capped() -> None:
    assert len(B.header("t" * 400)["text"]["text"]) == B.HEADER_MAX


def test_facts_spill_past_ten_fields() -> None:
    sections = B.facts([(f"L{i}", "v") for i in range(13)])
    assert [len(s["fields"]) for s in sections] == [10, 3]
    assert sections[0]["fields"][0]["text"] == "*L0*\nv"


def test_persona_section_is_attributed_and_escaped() -> None:
    block = B.persona_section(Persona.RISK, "Review", "<@U1> sized down")
    assert block is not None
    assert block["text"]["text"] == "*🛡️ [Risk] Review*\n&lt;@U1&gt; sized down"
    assert B.persona_section(Persona.RISK, "Review", "   ") is None


def test_bullets_and_footer() -> None:
    assert B.bullets("Warnings", []) is None
    block = B.bullets("Warnings", ["a", "b"])
    assert block is not None and block["text"]["text"] == "*Warnings*\n• a\n• b"
    text = B.footer(proposal="abc", run_id=None, chain="c-1")["elements"][0]["text"]
    assert text == "proposal `abc` · chain `c-1`"


def test_summary_skips_empty_parts() -> None:
    assert B.summary("a", "", "b")["elements"][0]["text"] == "a · b"


def test_code_block_fences_and_escapes_inner_fences() -> None:
    """E5.5b: ``[Routines]`` lines go in a ``` fence; inner fences can't close it."""
    assert B.code_block("[Routines] propose ✓ ok") == "```\n[Routines] propose ✓ ok\n```"
    fenced = B.code_block("a ``` b\n")
    assert fenced.count("```") == 2 and fenced == "```\na `\u200b`\u200b` b\n```"
