"""E4.7 (D47) rule 5: the Director sees its context under the 5 category headers."""

from __future__ import annotations

import datetime as dt
from typing import Any

from arc.context.store import ContextEntry, ContextSnapshot
from arc.personas.builders import (
    build_director_prompt,
    category_context_block,
    director_input_from_context,
)

NOW = dt.datetime(2026, 9, 28, 14, 0, tzinfo=dt.UTC)
CHANNELS = [
    {"slug": "stockedup", "label": "StockedUp"},
    {"slug": "fxevo", "label": "FX Evolution"},
]


def _e(kind: str, subject: str, payload: dict[str, Any], age: dt.timedelta) -> ContextEntry:
    t = NOW - age
    return ContextEntry(
        id=f"{kind}-{subject}-{int(age.total_seconds())}",
        kind=kind,
        subject=subject,
        payload=payload,
        schema_version=1,
        produced_by="test",
        created_at=t,
        valid_from=t,
    )


def _story(cat: str, headline: str, age: dt.timedelta, tickers: list[str]) -> ContextEntry:
    return _e(
        "story",
        headline[:8],
        {
            "category": cat,
            "headline": headline,
            "last_published": (NOW - age).isoformat(),
            "tickers": tickers,
        },
        age,
    )


def _snapshot(entries: list[ContextEntry]) -> ContextSnapshot:
    return ContextSnapshot(id="snap", as_of=NOW, entries=entries)


def test_five_headers_in_order_empty_one_says_no_fresh_info() -> None:
    snap = _snapshot(
        [
            _story("market_news", "Stocks rally on jobs", dt.timedelta(minutes=22), ["SPY"]),
            _story("market_news", "Oil slips", dt.timedelta(hours=2), []),
            _story("filings", "NVDA 8-K buyback", dt.timedelta(hours=3), ["NVDA"]),  # alias
            _e("vol_term", "market", {}, dt.timedelta(hours=5)),
            _e("put_call", "market", {}, dt.timedelta(hours=8)),
            _e("unusual_options", "AAPL", {"flags": ["volume"]}, dt.timedelta(hours=1)),
            _e("channel_brief", "stockedup", {"channel_slug": "stockedup"}, dt.timedelta(hours=4)),
        ]
    )
    block = category_context_block(snap, CHANNELS)
    headers = [ln.split(":", 1)[0] for ln in block.splitlines() if not ln.startswith("- ")]
    assert headers == ["Market news", "Company", "Macro", "Options data", "YouTube"]
    lines = block.splitlines()
    assert lines[0] == "Market news: 2 stories, newest 22m"
    assert lines[1] == "- Stocks rally on jobs [SPY]"
    assert "Company: 1 story, newest 3h" in lines
    assert "Macro: no fresh info" in lines  # empty: shown, never dropped
    assert "Options data: vol_term 5h, put_call 8h, unusual_options 1 flagged" in lines
    assert "YouTube: 1/2 channels (missing: FX Evolution)" in lines


def test_director_prompt_carries_the_category_section() -> None:
    snap = _snapshot([_story("macro", "Fed holds rates", dt.timedelta(hours=1), [])])
    prompt = build_director_prompt(
        director_input_from_context(
            snap, portfolio_summary="flat", scan_date="2026-09-28", youtube_channels=CHANNELS
        )
    )
    assert "### Context by category" in prompt
    assert "Market news: no fresh info" in prompt
    assert "Macro: 1 story, newest 1h" in prompt
    assert "YouTube: no fresh info (0/2 channels" in prompt
    assert prompt.index("Market news:") < prompt.index("Company:") < prompt.index("YouTube:")


def test_no_context_at_all_adds_no_section() -> None:
    """Replay: a pre-D47 snapshot (nothing in any category) builds the old prompt."""
    assert category_context_block(_snapshot([]), CHANNELS) == ""
