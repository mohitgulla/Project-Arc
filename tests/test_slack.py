"""Tests for arc.slack — client, personas, templates, commands.

Uses a fake WebClient to verify Slack API calls without network.
"""

from __future__ import annotations

from datetime import date
from typing import Any
from unittest.mock import MagicMock

import pytest

from arc.slack.client import (
    CHANNEL_ARC_INVESTOR,
    CHANNEL_PROJECT_ARC,
    ArcSlackClient,
)
from arc.slack.commands import CommandVerb, parse_command
from arc.slack.personas import Persona, persona_label
from arc.slack.templates import (
    card_thread_root,
    daily_session_root,
    halt_notice,
    proposal_card,
)

# ---------------------------------------------------------------------------
# Helpers — fake WebClient
# ---------------------------------------------------------------------------


def _fake_client() -> MagicMock:
    """Return a ``MagicMock`` mimicking ``slack_sdk.WebClient``."""
    client = MagicMock()
    # chat_postMessage and chat_update return a dict-like object
    resp = MagicMock()
    resp.get.return_value = "1234567890.123456"
    resp.__getitem__ = lambda self, k: "1234567890.123456" if k == "ts" else None
    client.chat_postMessage.return_value = resp
    client.chat_update.return_value = resp
    return client


# ===================================================================
# Persona labels
# ===================================================================


class TestPersonaLabel:
    @pytest.mark.parametrize(
        ("persona", "expected"),
        [
            (Persona.SCALP, "⚡ [Scalp]"),
            (Persona.SCOUT, "🔭 [Scout]"),
            (Persona.RESEARCH, "🧠 [Research]"),
            (Persona.QUANT, "🤺 [Quant]"),
            (Persona.RISK, "🛡️ [Risk]"),
            (Persona.BROKER, "🏦 [Broker]"),
            (Persona.OPS, "⚙️ [Ops]"),
        ],
    )
    def test_all_personas(self, persona: Persona, expected: str) -> None:
        assert persona_label(persona) == expected


# ===================================================================
# Command parser
# ===================================================================


class TestParseCommand:
    def test_halt(self) -> None:
        result = parse_command("!halt", slack_user="U123")
        assert result is not None
        assert result.verb == CommandVerb.HALT
        assert result.slack_user == "U123"

    def test_resume(self) -> None:
        result = parse_command("!resume", slack_user="U456")
        assert result is not None
        assert result.verb == CommandVerb.RESUME

    def test_case_insensitive(self) -> None:
        result = parse_command("!HALT")
        assert result is not None
        assert result.verb == CommandVerb.HALT

    def test_with_trailing_text(self) -> None:
        result = parse_command("!halt market crash incoming")
        assert result is not None
        assert result.verb == CommandVerb.HALT
        assert result.raw_text == "!halt market crash incoming"

    def test_whitespace(self) -> None:
        result = parse_command("  !resume  ")
        assert result is not None
        assert result.verb == CommandVerb.RESUME

    def test_unknown_command_returns_none(self) -> None:
        assert parse_command("!deploy") is None

    def test_no_bang_returns_none(self) -> None:
        assert parse_command("halt") is None

    def test_empty_returns_none(self) -> None:
        assert parse_command("") is None

    def test_bang_only_returns_none(self) -> None:
        assert parse_command("!") is None

    def test_parsed_command_frozen(self) -> None:
        result = parse_command("!halt")
        assert result is not None
        with pytest.raises(AttributeError):
            result.verb = CommandVerb.RESUME  # type: ignore[misc]


# ===================================================================
# Templates
# ===================================================================


class TestCardThreadRoot:
    def test_minimal(self) -> None:
        text = card_thread_root(card_id="t_abc", title="My card")
        assert "t_abc" in text
        assert "My card" in text

    def test_with_assignee_and_url(self) -> None:
        text = card_thread_root(
            card_id="t_abc",
            title="My card",
            assignee="default",
            url="https://github.com/mohitgulla/Project-Arc/pull/1",
        )
        assert "default" in text
        assert "github.com" in text


class TestDailySessionRoot:
    def test_format(self) -> None:
        text = daily_session_root(date(2026, 9, 28))
        assert text == "💡 Mon Sep 28 · Session Notes"
        assert daily_session_root(date(2026, 10, 1)) == "💡 Thu Oct 1 · Session Notes"


class TestProposalCard:
    def test_structure(self) -> None:
        blocks = proposal_card(
            ticker="AAPL",
            thesis="Earnings catalyst play",
            structure_summary="Bull call spread 150/160",
            pop=0.62,
            ev="$1.45",
            sizing="5 contracts, 2.1% equity",
            proposal_id="prop_001",
        )
        assert isinstance(blocks, list)
        # header, section, section-fields, divider, actions
        assert len(blocks) == 5
        assert blocks[0]["type"] == "header"
        assert "AAPL" in blocks[0]["text"]["text"]
        # actions block has approve + reject
        actions = blocks[4]
        assert actions["type"] == "actions"
        assert len(actions["elements"]) == 2
        assert actions["elements"][0]["action_id"] == "arc_approve"
        assert actions["elements"][0]["value"] == "prop_001"
        assert actions["elements"][1]["action_id"] == "arc_reject"

    def test_persona_in_header(self) -> None:
        blocks = proposal_card(
            ticker="SPY",
            thesis="Neutral",
            structure_summary="IC",
            pop=0.7,
            ev="$0",
            sizing="1",
            persona=Persona.RISK,
        )
        assert "🛡️ [Risk]" in blocks[0]["text"]["text"]


class TestHaltNotice:
    def test_basic(self) -> None:
        text = halt_notice(triggered_by="U0C5KUMH28G")
        assert "HALT" in text
        assert "U0C5KUMH28G" in text
        assert "!resume" in text

    def test_with_reason(self) -> None:
        text = halt_notice(triggered_by="U123", reason="flash crash")
        assert "flash crash" in text


# ===================================================================
# ArcSlackClient (with fake WebClient)
# ===================================================================


class TestArcSlackClient:
    def _make(self) -> tuple[ArcSlackClient, MagicMock]:
        fake = _fake_client()
        return ArcSlackClient(client=fake), fake

    def test_post_thread_root(self) -> None:
        arc, fake = self._make()
        arc.post_thread_root(channel="C123", text="hello")
        fake.chat_postMessage.assert_called_once_with(channel="C123", text="hello")

    def test_post_thread_root_with_blocks(self) -> None:
        arc, fake = self._make()
        blocks: list[dict[str, Any]] = [
            {"type": "section", "text": {"type": "mrkdwn", "text": "hi"}}
        ]
        arc.post_thread_root(channel="C123", text="fallback", blocks=blocks)
        call_kwargs = fake.chat_postMessage.call_args.kwargs
        assert call_kwargs["blocks"] == blocks

    def test_reply_plain(self) -> None:
        arc, fake = self._make()
        arc.reply(channel="C123", thread_ts="1234.5678", text="update")
        call_kwargs = fake.chat_postMessage.call_args.kwargs
        assert call_kwargs["thread_ts"] == "1234.5678"
        assert call_kwargs["text"] == "update"

    def test_reply_with_persona(self) -> None:
        arc, fake = self._make()
        arc.reply(
            channel="C123",
            thread_ts="1234.5678",
            text="scanning RSS feeds",
            persona=Persona.SCALP,
        )
        call_kwargs = fake.chat_postMessage.call_args.kwargs
        assert call_kwargs["text"].startswith("⚡ [Scalp]")

    def test_update(self) -> None:
        arc, fake = self._make()
        arc.update(channel="C123", ts="1234.5678", text="edited")
        fake.chat_update.assert_called_once()
        call_kwargs = fake.chat_update.call_args.kwargs
        assert call_kwargs["ts"] == "1234.5678"

    def test_post_card_thread(self) -> None:
        arc, fake = self._make()
        arc.post_card_thread(card_id="t_abc", title="E1.5 test")
        call_kwargs = fake.chat_postMessage.call_args.kwargs
        assert call_kwargs["channel"] == CHANNEL_PROJECT_ARC
        assert "t_abc" in call_kwargs["text"]

    def test_post_daily_session(self) -> None:
        arc, fake = self._make()
        arc.post_daily_session(date(2026, 10, 1))
        call_kwargs = fake.chat_postMessage.call_args.kwargs
        assert call_kwargs["channel"] == CHANNEL_ARC_INVESTOR
        assert call_kwargs["text"] == "💡 Thu Oct 1 · Session Notes"

    def test_post_halt(self) -> None:
        arc, fake = self._make()
        arc.post_halt(
            channel="C123",
            thread_ts="1234.5678",
            triggered_by="U0C5KUMH28G",
            reason="test halt",
        )
        call_kwargs = fake.chat_postMessage.call_args.kwargs
        assert "HALT" in call_kwargs["text"]
        assert "test halt" in call_kwargs["text"]

    def test_post_proposal(self) -> None:
        arc, fake = self._make()
        arc.post_proposal(
            thread_ts="1234.5678",
            ticker="NVDA",
            thesis="AI demand",
            structure_summary="Bull call 800/850",
            pop=0.55,
            ev="$2.30",
            sizing="3 contracts",
            proposal_id="prop_002",
        )
        call_kwargs = fake.chat_postMessage.call_args.kwargs
        assert call_kwargs["channel"] == CHANNEL_ARC_INVESTOR
        assert call_kwargs["thread_ts"] == "1234.5678"
        assert "blocks" in call_kwargs

    def test_default_client_without_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No SLACK_BOT_TOKEN logs a warning but doesn't crash."""
        monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
        arc = ArcSlackClient()
        assert arc._client is not None


# ===================================================================
# Channel ID constants
# ===================================================================


class TestChannelConstants:
    def test_project_arc(self) -> None:
        assert CHANNEL_PROJECT_ARC == "C0C4KBPN7T5"

    def test_arc_investor(self) -> None:
        assert CHANNEL_ARC_INVESTOR == "C0C4NS1AL3X"
