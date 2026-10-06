"""E4.7 (D47) / E4.9 (D49) / E13.3 (D56): Research sees its context under the 6 category
headers. The D49 block is kept frozen for replay (``d49_category_context_block``)."""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest

from arc.context.store import ContextEntry, ContextSnapshot
from arc.personas.builders import (
    build_research_prompt,
    category_context_block,
    category_specs_input,
    d47_category_context_block,
    d49_category_context_block,
    research_input_from_context,
)
from arc.routines.config import RoutinesConfig

NOW = dt.datetime(2026, 9, 28, 14, 0, tzinfo=dt.UTC)
CHANNELS = [
    {"slug": "stockedup", "label": "StockedUp", "category": "youtube_micro"},
    {"slug": "fxevo", "label": "FX Evolution", "category": "youtube_macro"},
    {"slug": "bravos", "label": "Bravos", "category": "youtube_macro"},
]
HEADERS = [
    "Market news",
    "Company data",
    "Options fast",
    "Options slow",
    "YouTube macro",
    "YouTube micro",
]
D49_HEADERS = [
    "Market news",
    "Company data",
    "Macro data",
    "Options data",
    "YouTube macro",
    "YouTube micro",
]
D49_CATS = {  # what a D49-era Research call recorded as its `categories` input
    "market_news": {"label": "Market news", "max_age": "6h"},
    "company_data": {"label": "Company data", "max_age": "1d"},
    "macro_data": {"label": "Macro data", "max_age": "1d"},
    "options_data": {"label": "Options data", "max_age": "12h"},
    "youtube_macro": {"label": "YouTube macro", "max_age": "1d"},
    "youtube_micro": {"label": "YouTube micro", "max_age": "1d"},
}


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


def _brief(slug: str, age: dt.timedelta) -> ContextEntry:
    return _e("channel_brief", slug, {"channel_slug": slug}, age)


def _snapshot(entries: list[ContextEntry]) -> ContextSnapshot:
    return ContextSnapshot(id="snap", as_of=NOW, entries=entries)


def _headers(block: str) -> list[str]:
    return [ln.split(":", 1)[0] for ln in block.splitlines() if not ln.startswith("- ")]


def test_six_headers_in_order_empty_one_says_no_fresh_info() -> None:
    snap = _snapshot(
        [
            _story("market_news", "Stocks rally on jobs", dt.timedelta(minutes=22), ["SPY"]),
            _story("market_news", "Oil slips", dt.timedelta(hours=2), []),
            _story("filings", "NVDA 8-K buyback", dt.timedelta(hours=3), ["NVDA"]),  # alias
            _e("vol_term", "market", {}, dt.timedelta(hours=5)),
            _e("put_call", "market", {}, dt.timedelta(hours=8)),
            _e("ex_dividend", "KO", {}, dt.timedelta(hours=1)),  # D56: reference data
            _e("macro_calendar", "market", {}, dt.timedelta(hours=1)),  # D56: reference data
            _brief("stockedup", dt.timedelta(hours=4)),
        ]
    )
    block = category_context_block(snap, CHANNELS)
    assert _headers(block) == HEADERS
    lines = block.splitlines()
    assert lines[0] == "Market news: 2 stories, newest 22m"
    assert lines[1] == "- Stocks rally on jobs [SPY]"
    assert "Company data: 1 story, newest 3h" in lines
    assert "Options fast: no fresh info" in lines  # empty: shown, never dropped
    assert "Options slow: vol_term 5h, put_call 8h" in lines
    assert "ex_dividend" not in block and "macro_calendar" not in block
    # D49: presence per YouTube category; the denominator is that category's channels
    assert "YouTube macro: no fresh info (0/2 channels (missing: FX Evolution, Bravos))" in lines
    assert "YouTube micro: 1/1 channels" in lines


def test_stored_macro_data_story_has_no_category() -> None:
    """D56: a stored `macro_data` story (pre-D56 Fed doc) lands in no category; the
    renamed `company` and `options_data` still map."""
    snap = _snapshot(
        [
            _story("company", "AAPL buyback", dt.timedelta(hours=1), ["AAPL"]),
            _story("macro_data", "Fed holds rates", dt.timedelta(hours=2), []),
        ]
    )
    block = category_context_block(snap, CHANNELS)
    assert "Company data: 1 story, newest 1h" in block.splitlines()
    assert "Fed holds rates" not in block


def test_options_slow_window_is_24h() -> None:
    stale = _snapshot([_e("vol_term", "market", {}, dt.timedelta(hours=25))])
    lines = category_context_block(stale, CHANNELS).splitlines()
    assert "Options slow: no fresh info (vol_term stale (25h))" in lines
    fresh = _snapshot([_e("vol_term", "market", {}, dt.timedelta(hours=13))])
    assert "Options slow: vol_term 13h" in category_context_block(fresh, CHANNELS).splitlines()


def test_finnhub_kinds_are_reference_data() -> None:
    """D56: Finnhub kinds never show in the category block (the ticker-facts section
    still carries them with personas.finnhub_context on)."""
    snap = _snapshot([_e("earnings_history", "AAPL", {}, dt.timedelta(hours=2))])
    assert category_context_block(snap, CHANNELS) == ""


def test_window_follows_the_recorded_categories() -> None:
    """A Slack max_age change reaches the verdict; recorded windows replay it."""
    snap = _snapshot([_e("vol_term", "market", {}, dt.timedelta(hours=13))])
    r = RoutinesConfig.model_validate({"categories": {"options_slow": {"max_age": "12h"}}})
    cats = category_specs_input(r)
    assert cats["options_slow"] == {"label": "Options slow", "max_age": "12h"}
    lines = category_context_block(snap, CHANNELS, categories=cats).splitlines()
    assert "Options slow: no fresh info (vol_term stale (13h))" in lines


def test_research_prompt_carries_the_category_section() -> None:
    snap = _snapshot([_story("market_news", "Fed holds rates", dt.timedelta(hours=1), [])])
    prompt = build_research_prompt(
        research_input_from_context(
            snap,
            portfolio_summary="flat",
            scan_date="2026-09-28",
            youtube_channels=CHANNELS,
            categories=category_specs_input(RoutinesConfig()),
        )
    )
    assert "### Context by category (D56: 6 equal-weight categories" in prompt
    assert "Weigh the six categories equally." in prompt
    assert "Market news: 1 story, newest 1h" in prompt
    assert "Options fast: no fresh info" in prompt
    assert "YouTube macro: no fresh info (0/2 channels" in prompt
    assert "YouTube micro: no fresh info (0/1 channels" in prompt
    order = [prompt.index(f"\n{h}:") for h in HEADERS]
    assert order == sorted(order)


# ---------------------------------------------------------------------------
# D49 block (frozen for replay since D56)
# ---------------------------------------------------------------------------


def test_d49_six_headers_in_order_empty_one_says_no_fresh_info() -> None:
    snap = _snapshot(
        [
            _story("market_news", "Stocks rally on jobs", dt.timedelta(minutes=22), ["SPY"]),
            _story("market_news", "Oil slips", dt.timedelta(hours=2), []),
            _story("filings", "NVDA 8-K buyback", dt.timedelta(hours=3), ["NVDA"]),  # alias
            _e("vol_term", "market", {}, dt.timedelta(hours=5)),
            _e("put_call", "market", {}, dt.timedelta(hours=8)),
            _e("unusual_options", "AAPL", {"flags": ["volume"]}, dt.timedelta(hours=1)),
            _brief("stockedup", dt.timedelta(hours=4)),
        ]
    )
    block = d49_category_context_block(snap, CHANNELS)
    assert _headers(block) == D49_HEADERS
    lines = block.splitlines()
    assert lines[0] == "Market news: 2 stories, newest 22m"
    assert lines[1] == "- Stocks rally on jobs [SPY]"
    assert "Company data: 1 story, newest 3h" in lines
    assert "Macro data: no fresh info" in lines  # empty: shown, never dropped
    assert "Options data: vol_term 5h, put_call 8h, unusual_options 1 flagged, newest 1h" in lines
    # D49: presence per YouTube category; the denominator is that category's channels
    assert "YouTube macro: no fresh info (0/2 channels (missing: FX Evolution, Bravos))" in lines
    assert "YouTube micro: 1/1 channels" in lines


def test_d49_old_category_names_on_stored_stories_still_land() -> None:
    """Rows written before D49 carry `company` / `macro`: read through the aliases."""
    snap = _snapshot(
        [
            _story("company", "AAPL buyback", dt.timedelta(hours=1), ["AAPL"]),
            _story("macro", "Fed holds rates", dt.timedelta(hours=2), []),
        ]
    )
    lines = d49_category_context_block(snap, CHANNELS).splitlines()
    assert "Company data: 1 story, newest 1h" in lines
    assert "Macro data: 1 story, newest 2h" in lines


class TestD49TypedKindFreshness:
    """D49: a typed entry older than its category's max_age is stale, not fresh."""

    def test_13h_vol_term_is_stale_under_12h_options_window(self) -> None:
        snap = _snapshot([_e("vol_term", "market", {}, dt.timedelta(hours=13))])
        lines = d49_category_context_block(snap, CHANNELS).splitlines()
        assert "Options data: no fresh info (vol_term stale (13h))" in lines

    def test_3h_vol_term_is_fresh(self) -> None:
        snap = _snapshot([_e("vol_term", "market", {}, dt.timedelta(hours=3))])
        assert (
            "Options data: vol_term 3h" in d49_category_context_block(snap, CHANNELS).splitlines()
        )

    def test_one_fresh_kind_keeps_the_category_fresh_and_lists_the_stale_one(self) -> None:
        snap = _snapshot(
            [
                _e("vol_term", "market", {}, dt.timedelta(hours=2)),
                _e("put_call", "market", {}, dt.timedelta(hours=14)),
            ]
        )
        lines = d49_category_context_block(snap, CHANNELS).splitlines()
        assert "Options data: vol_term 2h, put_call stale (14h)" in lines

    def test_stale_ticker_kinds_count_only_fresh_entries(self) -> None:
        snap = _snapshot(
            [
                _e("unusual_options", "AAPL", {"flags": ["volume"]}, dt.timedelta(hours=1)),
                _e("unusual_options", "MSFT", {"flags": ["volume"]}, dt.timedelta(hours=20)),
                _e("ex_dividend", "KO", {}, dt.timedelta(hours=30)),
            ]
        )
        lines = d49_category_context_block(snap, CHANNELS).splitlines()
        [line] = [ln for ln in lines if ln.startswith("Options data:")]
        assert line == "Options data: unusual_options 1 flagged, newest 1h, ex_dividend stale (30h)"

    def test_macro_calendar_goes_by_the_macro_window(self) -> None:
        stale = _snapshot([_e("macro_calendar", "market", {}, dt.timedelta(hours=30))])
        assert any(
            ln.startswith("Macro data: no fresh info (macro_calendar stale")
            for ln in d49_category_context_block(stale, CHANNELS).splitlines()
        )
        fresh = _snapshot([_e("macro_calendar", "market", {}, dt.timedelta(hours=6))])
        assert "Macro data: macro_calendar 6h" in d49_category_context_block(fresh, CHANNELS)

    def test_stale_brief_is_not_present(self) -> None:
        snap = _snapshot(
            [_brief("fxevo", dt.timedelta(hours=3)), _brief("bravos", dt.timedelta(hours=30))]
        )
        lines = d49_category_context_block(snap, CHANNELS).splitlines()
        [macro] = [ln for ln in lines if ln.startswith("YouTube macro:")]
        assert macro.startswith("YouTube macro: 1/2 channels (missing: Bravos), Bravos stale (")

    def test_finnhub_kinds_only_with_the_flag(self) -> None:
        snap = _snapshot(
            [
                _e("earnings_history", "AAPL", {}, dt.timedelta(hours=2)),
                _e("fundamentals", "AAPL", {}, dt.timedelta(hours=30)),
            ]
        )
        assert d49_category_context_block(snap, CHANNELS) == ""  # XP-2 control arm: never seen
        on = d49_category_context_block(snap, CHANNELS, finnhub=True).splitlines()
        [line] = [ln for ln in on if ln.startswith("Company data:")]
        assert line.startswith("Company data: earnings_history 1 tickers, newest 2h")
        assert "fundamentals stale (" in line

    def test_window_follows_the_recorded_categories(self) -> None:
        """A Slack max_age change reaches the verdict; recorded windows replay it."""
        snap = _snapshot([_e("vol_term", "market", {}, dt.timedelta(hours=13))])
        cats = {**D49_CATS, "options_data": {"label": "Options data", "max_age": "1d"}}
        lines = d49_category_context_block(snap, CHANNELS, categories=cats).splitlines()
        assert "Options data: vol_term 13h" in lines

    def test_context_ttl_is_untouched(self) -> None:
        """Stale entries stay in the snapshot (audit / tower); only the verdict changes."""
        snap = _snapshot([_e("vol_term", "market", {}, dt.timedelta(hours=13))])
        d49_category_context_block(snap, CHANNELS)
        assert [e.kind for e in snap.entries] == ["vol_term"]


def test_d49_research_prompt_carries_the_category_section() -> None:
    snap = _snapshot([_story("macro_data", "Fed holds rates", dt.timedelta(hours=1), [])])
    prompt = build_research_prompt(
        research_input_from_context(
            snap,
            portfolio_summary="flat",
            scan_date="2026-09-28",
            youtube_channels=CHANNELS,
            categories=D49_CATS,
            d49_replay=True,
        )
    )
    assert "### Context by category (D49: 6 equal-weight categories" in prompt
    assert "Weigh the six categories equally." in prompt
    assert "Market news: no fresh info" in prompt
    assert "Macro data: 1 story, newest 1h" in prompt
    assert "YouTube macro: no fresh info (0/2 channels" in prompt
    assert "YouTube micro: no fresh info (0/1 channels" in prompt
    order = [prompt.index(f"\n{h}:") for h in D49_HEADERS]
    assert order == sorted(order)


def test_no_context_at_all_adds_no_section() -> None:
    """Replay: a pre-D47 snapshot (nothing in any category) builds the old prompt."""
    assert d49_category_context_block(_snapshot([]), CHANNELS) == ""


def test_pre_d49_inputs_replay_the_d47_block() -> None:
    """A Research call recorded before D49 (no `categories` input) rebuilds byte for byte."""
    old_channels = [{"slug": "stockedup", "label": "StockedUp"}, {"slug": "fxevo", "label": "FX"}]
    snap = _snapshot(
        [
            _story("company", "NVDA 8-K buyback", dt.timedelta(hours=3), ["NVDA"]),
            _e("vol_term", "market", {}, dt.timedelta(hours=13)),
            _brief("stockedup", dt.timedelta(hours=4)),
        ]
    )
    block = d47_category_context_block(snap, old_channels)
    assert _headers(block) == ["Market news", "Company", "Macro", "Options data", "YouTube"]
    assert "Options data: vol_term 13h" in block.splitlines()  # D47: no typed staleness
    assert "YouTube: 1/2 channels (missing: FX)" in block.splitlines()
    prompt = build_research_prompt(
        research_input_from_context(
            snap,
            portfolio_summary="flat",
            scan_date="2026-09-28",
            youtube_channels=old_channels,
            d47_replay=True,
        )
    )
    assert "### Context by category (D47: 5 equal-weight categories" in prompt
    assert "Weigh the five categories equally." in prompt


@pytest.mark.parametrize("slug", ["fxevo", "bravos"])
def test_brief_category_follows_its_channel(slug: str) -> None:
    from arc.context.categories import SourceCategory, channel_category, normalize_category

    assert channel_category(f"youtube.{slug}", CHANNELS) is SourceCategory.YOUTUBE_MACRO
    assert normalize_category("video", channel=slug, channels=CHANNELS) is (
        SourceCategory.YOUTUBE_MACRO
    )
    assert normalize_category("video", channel="gone", channels=CHANNELS) is None
    assert normalize_category("video") is None
