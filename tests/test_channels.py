"""Tests for per-channel processors and ChannelBrief lifecycle (E4.4, PLAN D14)."""

from __future__ import annotations

import datetime as dt
import json
import shutil
from typing import TYPE_CHECKING

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from arc.config import ArcSettings
from arc.ingest.channels import CHANNELS_DIR, ChannelRegistry, default_registry
from arc.ingest.channels.base import (
    DROP_PRICE,
    DROP_SCHEMA,
    DROP_SECTION,
    DROP_TICKER,
    DROP_UNGROUNDED,
    BriefParseError,
    ChannelProcessor,
    ChannelProfile,
    VideoDoc,
    applies_to_session,
    is_grounded,
    normalize_text,
    normalize_ticker,
    strip_sponsor_segments,
    tickers_mentioned,
)
from arc.ingest.channels.briefs import (
    STATUS_ACTIVE,
    STATUS_EXPIRED,
    STATUS_SUPERSEDED,
    ChannelBriefRepo,
    active_briefs,
    brief_to_candidates,
    expires_at,
    fixture_llm,
    load_channel_fixture,
    process_new_videos,
    video_from_row,
)
from arc.ingest.llm import FixtureScoutLLM, LLMResult, ScoutLLMError
from arc.models import (
    BriefCall,
    BriefCatalyst,
    BriefLevel,
    CatalystType,
    ChannelBrief,
    Stance,
)
from arc.store.db import connect
from arc.store.migrate import migrate
from arc.utils.calendar import ET

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path

STOCKEDUP_ID = "UC-m6zNItyoDk5lSykDlhE4Q"
FIXTURES = CHANNELS_DIR / "stockedup" / "fixtures"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db() -> sqlite3.Connection:
    conn = connect(":memory:")
    migrate(conn)
    return conn


@pytest.fixture
def settings() -> ArcSettings:
    return ArcSettings(env="paper")


@pytest.fixture
def registry() -> ChannelRegistry:
    return default_registry()


@pytest.fixture
def stockedup(registry: ChannelRegistry) -> ChannelProcessor:
    proc = registry.for_slug("stockedup")
    assert proc is not None
    return proc


TRANSCRIPT = (
    "SPY is at 772 with the first major resistance around 775. "
    "Support at 768 has held. "
    "I'm buying NVDA calls tomorrow if it clears 190. "
    "AAPL could bounce off 180 but watch it. "
    "This video is sponsored by Bookmap, use code BIGMONEY for a discount. "
    "Jobs report is Friday, one hour before the open. "
    "The 10-year yield is above 5%, which is dangerous for stocks."
)


def _video(
    *,
    published: dt.datetime = dt.datetime(2026, 9, 25, 18, 0, tzinfo=ET),  # Friday evening
    video_id: str = "vid1",
    transcript: str = TRANSCRIPT,
    channel_id: str | None = STOCKEDUP_ID,
) -> VideoDoc:
    return VideoDoc(
        video_id=video_id,
        video_url=f"https://www.youtube.com/watch?v={video_id}",
        title="Market outlook",
        published_at=published,
        transcript=transcript,
        channel_id=channel_id,
    )


def _payload(**overrides: object) -> dict:
    base: dict = {
        "market_bias": {
            "stance": "bullish",
            "confidence": 0.6,
            "quote": "Support at 768 has held.",
        },
        "levels": [
            {
                "ticker": "SPY",
                "kind": "resistance",
                "price": 775.0,
                "quote": "first major resistance around 775",
            }
        ],
        "calls": [
            {
                "ticker": "NVDA",
                "stance": "bullish",
                "horizon": "next_session",
                "instrument_hint": "calls",
                "conviction": 0.8,
                "quote": "I'm buying NVDA calls tomorrow if it clears 190.",
            }
        ],
        "catalysts": [
            {
                "event": "Jobs report",
                "kind": "macro",
                "date": "2026-10-02",
                "tickers": [],
                "expected_impact": "volatile",
                "quote": "Jobs report is Friday, one hour before the open.",
            }
        ],
        "risk_flags": [
            {
                "flag": "10y above 5%",
                "severity": "high",
                "quote": "The 10-year yield is above 5%, which is dangerous for stocks.",
            }
        ],
    }
    base.update(overrides)
    return base


def _build(
    proc: ChannelProcessor,
    payload: dict,
    *,
    video: VideoDoc | None = None,
    price_lookup=None,  # noqa: ANN001
    universe: list[str] | None = None,
    brief_id: str | None = None,
):  # noqa: ANN202
    video = video or _video()
    clean, removed = strip_sponsor_segments(video.transcript, proc.sponsor_patterns)
    return proc.build_brief(
        video,
        payload,
        clean_transcript=clean,
        sponsor_removed=removed > 0,
        universe=universe or ArcSettings(env="paper").universe,
        price_lookup=price_lookup,
        brief_id=brief_id,
    )


# ---------------------------------------------------------------------------
# Schema strictness
# ---------------------------------------------------------------------------


def _brief_kwargs(**over: object) -> dict:
    kw: dict = {
        "brief_id": "b1",
        "channel_slug": "stockedup",
        "video_id": "v1",
        "video_url": "https://www.youtube.com/watch?v=v1",
        "title": "t",
        "published_at": dt.datetime(2026, 9, 25, 18, 0, tzinfo=ET),
        "applies_to_session": dt.date(2026, 9, 28),
        "guidelines_version": "g1",
    }
    kw.update(over)
    return kw


class TestSchema:
    def test_extra_fields_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            ChannelBrief(**_brief_kwargs(), contracts=3)  # type: ignore[call-arg]
        with pytest.raises(ValidationError):
            BriefCall(
                ticker="SPY",
                stance=Stance.BULLISH,
                horizon="next_session",
                instrument_hint="calls",
                conviction=0.5,
                quote="q",
                size=10,  # type: ignore[call-arg]
            )

    def test_no_sizing_or_order_fields(self) -> None:
        forbidden = {"size", "sizing", "contracts", "qty", "quantity", "order", "notional"}
        for model in (ChannelBrief, BriefCall, BriefLevel, BriefCatalyst):
            assert not forbidden & set(model.model_fields)

    def test_strict_types(self) -> None:
        with pytest.raises(ValidationError):
            BriefLevel.model_validate(
                {"ticker": "SPY", "kind": "support", "price": "775", "quote": "q"}
            )
        with pytest.raises(ValidationError):
            BriefCall.model_validate(
                {
                    "ticker": "SPY",
                    "stance": "bullish",
                    "horizon": "next_session",
                    "instrument_hint": "none",
                    "conviction": 1.5,
                    "quote": "q",
                }
            )

    def test_quote_length_capped(self) -> None:
        with pytest.raises(ValidationError):
            BriefLevel.model_validate(
                {"ticker": "SPY", "kind": "support", "price": 1.0, "quote": "x" * 241}
            )

    def test_ticker_pattern(self) -> None:
        with pytest.raises(ValidationError):
            BriefLevel.model_validate(
                {"ticker": "spy", "kind": "support", "price": 1.0, "quote": "q"}
            )

    def test_published_at_must_be_aware_and_is_et(self) -> None:
        with pytest.raises(ValidationError):
            ChannelBrief(**_brief_kwargs(published_at=dt.datetime(2026, 9, 25, 18)))
        b = ChannelBrief(**_brief_kwargs(published_at=dt.datetime(2026, 9, 25, 22, tzinfo=dt.UTC)))
        assert b.published_at.tzinfo == ET
        assert b.published_at.hour == 18

    def test_json_round_trip(self, stockedup: ChannelProcessor) -> None:
        brief = _build(stockedup, _payload()).brief
        assert ChannelBrief.model_validate_json(brief.model_dump_json()) == brief


# ---------------------------------------------------------------------------
# Deterministic rules
# ---------------------------------------------------------------------------


class TestGrounding:
    def test_fabricated_item_is_dropped(self, stockedup: ChannelProcessor) -> None:
        fake = {
            "ticker": "TSLA",
            "stance": "bearish",
            "horizon": "next_session",
            "instrument_hint": "puts",
            "conviction": 0.9,
            "quote": "I'm shorting Tesla into earnings.",
        }
        res = _build(stockedup, _payload(calls=[*_payload()["calls"], fake]))
        assert [c.ticker for c in res.brief.calls] == ["NVDA"]
        assert [(d.section, d.reason) for d in res.dropped] == [("calls", DROP_UNGROUNDED)]

    def test_grounding_is_case_and_whitespace_insensitive(self) -> None:
        norm = normalize_text("SPY  is at 772\nwith the FIRST major resistance")
        assert is_grounded("spy is at 772 with the first major resistance.", norm)
        assert not is_grounded("SPY is at 773", norm)
        assert not is_grounded("   ", norm)

    def test_html_entities_and_smart_quotes(self) -> None:
        norm = normalize_text("The S&amp;P 500 isn't done")
        assert is_grounded("The S&P 500 isn\u2019t done", norm)

    def test_quote_from_sponsor_read_is_dropped(self, stockedup: ChannelProcessor) -> None:
        sponsored = {
            "flag": "sponsor",
            "severity": "low",
            "quote": "use code BIGMONEY for a discount",
        }
        res = _build(stockedup, _payload(risk_flags=[sponsored]))
        assert res.brief.risk_flags == []
        assert res.dropped[0].reason == DROP_UNGROUNDED

    def test_market_bias_dropped_when_ungrounded(self, stockedup: ChannelProcessor) -> None:
        bias = {"stance": "bearish", "confidence": 0.9, "quote": "crash tomorrow"}
        res = _build(stockedup, _payload(market_bias=bias))
        assert res.brief.market_bias is None
        assert res.dropped[0].section == "market_bias"

    @given(st.text(min_size=1, max_size=60))
    def test_any_substring_of_transcript_is_grounded(self, s: str) -> None:
        transcript = f"prefix {s} suffix"
        norm = normalize_text(transcript)
        if normalize_text(s).strip(" \t\n\r.,;:!?\"'…-—()[]"):
            assert is_grounded(s, norm)


class TestSponsorStripping:
    def test_strips_stockedup_reads(self, stockedup: ChannelProcessor) -> None:
        text = (
            "SPY looks strong. This video is sponsored by Bookmap. "
            "Get free stocks on Moomoo with the link below. "
            "Use code BIGMONEY at checkout. Join the Discord for alerts. "
            "Consider smashing that subscribe button. NVDA breaks out above 190."
        )
        clean, removed = strip_sponsor_segments(text, stockedup.sponsor_patterns)
        assert clean == "SPY looks strong. NVDA breaks out above 190."
        assert removed == 5

    def test_brief_flags_sponsor_removal(self, stockedup: ChannelProcessor) -> None:
        assert _build(stockedup, _payload()).brief.sponsor_segments_removed is True
        clean_video = _video(transcript="Support at 768 has held.")
        res = _build(stockedup, {"market_bias": _payload()["market_bias"]}, video=clean_video)
        assert res.brief.sponsor_segments_removed is False


class TestPriceSanity:
    def test_level_dropped_when_far_from_price(self, stockedup: ChannelProcessor) -> None:
        far = {"ticker": "SPY", "kind": "support", "price": 77.5, "quote": "Support at 768"}
        res = _build(
            stockedup,
            _payload(levels=[*_payload()["levels"], far]),
            price_lookup=lambda t: 772.0,
        )
        assert [lvl.price for lvl in res.brief.levels] == [775.0]
        assert res.brief.levels[0].unverified_price is False
        assert res.dropped[0].reason == DROP_PRICE

    def test_boundary_is_inclusive(self, stockedup: ChannelProcessor) -> None:
        lvl = {"ticker": "SPY", "kind": "target", "price": 125.0, "quote": "Support at 768"}
        res = _build(stockedup, _payload(levels=[lvl]), price_lookup=lambda t: 100.0)
        assert len(res.brief.levels) == 1

    def test_no_price_keeps_level_unverified(self, stockedup: ChannelProcessor) -> None:
        res = _build(stockedup, _payload())
        assert res.brief.levels[0].unverified_price is True
        res = _build(stockedup, _payload(), price_lookup=lambda t: None)
        assert res.brief.levels[0].unverified_price is True

    def test_lookup_error_keeps_level_unverified(self, stockedup: ChannelProcessor) -> None:
        def boom(_: str) -> float:
            raise RuntimeError("no data")

        res = _build(stockedup, _payload(), price_lookup=boom)
        assert res.brief.levels[0].unverified_price is True

    def test_model_cannot_set_unverified_flag(self, stockedup: ChannelProcessor) -> None:
        lvl = dict(_payload()["levels"][0], unverified_price=False)
        res = _build(stockedup, _payload(levels=[lvl]))
        assert res.brief.levels[0].unverified_price is True


class TestTickers:
    @pytest.mark.parametrize(
        ("raw", "want"),
        [
            ("S&P 500", "SPY"),
            ("s&p", "SPY"),
            ("the market", "SPY"),
            ("Nasdaq", "QQQ"),
            ("NASDAQ 100", "QQQ"),
            ("Dow", "DIA"),
            ("Dow Jones", "DIA"),
            ("Russell 2000", "IWM"),
            ("russell", "IWM"),
            ("$nvda", "NVDA"),
            ("CO IN", "COIN"),
            ("brk.b", "BRK.B"),
            ("not a ticker!", None),
            ("", None),
        ],
    )
    def test_alias_mapping(self, raw: str, want: str | None, stockedup: ChannelProcessor) -> None:
        assert normalize_ticker(raw, stockedup.profile.aliases) == want

    def test_aliases_applied_to_items(self, stockedup: ChannelProcessor) -> None:
        lvl = dict(_payload()["levels"][0], ticker="S&P 500")
        cat = dict(_payload()["catalysts"][0], tickers=["Nasdaq", "$nvda", "??"])
        res = _build(stockedup, _payload(levels=[lvl], catalysts=[cat]))
        assert res.brief.levels[0].ticker == "SPY"
        assert res.brief.catalysts[0].tickers == ["QQQ", "NVDA"]

    def test_bad_ticker_dropped(self, stockedup: ChannelProcessor) -> None:
        lvl = dict(_payload()["levels"][0], ticker="!!!")
        res = _build(stockedup, _payload(levels=[lvl]))
        assert res.dropped[0].reason == DROP_TICKER

    def test_tickers_mentioned_is_deterministic(self) -> None:
        text = "The S&amp;P 500 and Nasdaq slid; NVDA and $AAPL rose. HD is at home. meta talk."
        assert tickers_mentioned(text, ["SPY", "QQQ", "NVDA", "AAPL", "HD", "META", "DIA"]) == [
            "SPY",
            "QQQ",
            "NVDA",
            "AAPL",
            "HD",
        ]

    def test_out_of_universe_calls_stay_in_brief(self, stockedup: ChannelProcessor) -> None:
        video = _video(transcript="I'm shorting DELL into the close.")
        call = {
            "ticker": "DELL",
            "stance": "bearish",
            "horizon": "swing",
            "instrument_hint": "none",
            "conviction": 0.8,
            "quote": "I'm shorting DELL into the close.",
        }
        res = _build(stockedup, {"calls": [call]}, video=video)
        assert [c.ticker for c in res.brief.calls] == ["DELL"]


class TestItemValidation:
    def test_schema_failure_dropped_not_fatal(self, stockedup: ChannelProcessor) -> None:
        bad = dict(_payload()["calls"][0], stance="moon")
        res = _build(stockedup, _payload(calls=[bad, "junk"]))
        assert res.brief.calls == []
        assert [d.reason for d in res.dropped] == [DROP_SCHEMA, DROP_SCHEMA]

    def test_section_not_a_list(self, stockedup: ChannelProcessor) -> None:
        res = _build(stockedup, _payload(levels={"oops": 1}))
        assert res.brief.levels == []
        assert res.dropped[0].reason == DROP_SCHEMA

    def test_hedged_conviction_capped(self, stockedup: ChannelProcessor) -> None:
        call = {
            "ticker": "AAPL",
            "stance": "bullish",
            "horizon": "next_session",
            "instrument_hint": "none",
            "conviction": 0.9,
            "quote": "AAPL could bounce off 180 but watch it.",
        }
        res = _build(stockedup, _payload(calls=[call]))
        assert res.brief.calls[0].conviction == 0.4

    def test_disabled_section_dropped(self, tmp_path: Path) -> None:
        proc = _fake_channel(tmp_path, focus=["levels"]).for_slug("fakechan")
        assert proc is not None
        res = _build(proc, _payload())
        assert len(res.brief.levels) == 1
        assert res.brief.calls == [] and res.brief.market_bias is None
        assert {d.reason for d in res.dropped} == {DROP_SECTION}


# ---------------------------------------------------------------------------
# Session math
# ---------------------------------------------------------------------------


class TestAppliesToSession:
    @pytest.mark.parametrize(
        ("published", "session"),
        [
            (dt.datetime(2026, 9, 28, 18, 0, tzinfo=ET), dt.date(2026, 9, 29)),  # Mon after close
            (dt.datetime(2026, 9, 28, 7, 0, tzinfo=ET), dt.date(2026, 9, 28)),  # Mon pre-market
            (dt.datetime(2026, 9, 28, 11, 0, tzinfo=ET), dt.date(2026, 9, 29)),  # Mon intraday
            (dt.datetime(2026, 9, 25, 18, 0, tzinfo=ET), dt.date(2026, 9, 28)),  # Fri → Mon
            (dt.datetime(2026, 9, 27, 17, 59, tzinfo=ET), dt.date(2026, 9, 28)),  # Sunday
            (dt.datetime(2026, 11, 25, 20, 0, tzinfo=ET), dt.date(2026, 11, 27)),  # Thanksgiving
            (dt.datetime(2026, 9, 28, 1, 0, tzinfo=dt.UTC), dt.date(2026, 9, 28)),  # Sun ET
        ],
    )
    def test_next_session(self, published: dt.datetime, session: dt.date) -> None:
        assert applies_to_session(published) == session


# ---------------------------------------------------------------------------
# Lifecycle: supersede / expire
# ---------------------------------------------------------------------------


def _store(
    db: sqlite3.Connection,
    proc: ChannelProcessor,
    published: dt.datetime,
    vid: str,
    *,
    now: dt.datetime | None = None,
) -> tuple[str, str]:
    res = _build(proc, _payload(), video=_video(published=published, video_id=vid), brief_id=vid)
    status = ChannelBriefRepo(db).store(res, proc, now=now or published)
    return res.brief.brief_id, status


class TestLifecycle:
    def test_new_brief_supersedes_previous(
        self, db: sqlite3.Connection, stockedup: ChannelProcessor
    ) -> None:
        mon = dt.datetime(2026, 9, 28, 18, 0, tzinfo=ET)
        tue = dt.datetime(2026, 9, 29, 18, 0, tzinfo=ET)
        a, sa = _store(db, stockedup, mon, "a")
        b, sb = _store(db, stockedup, tue, "b")
        repo = ChannelBriefRepo(db)
        assert (sa, sb) == (STATUS_ACTIVE, STATUS_ACTIVE)
        assert repo.status_of(a) == STATUS_SUPERSEDED
        assert repo.row(a)["superseded_by"] == "b"  # type: ignore[index]
        assert [x.brief_id for x in active_briefs(db, tue)] == ["b"]

    def test_only_one_active_per_channel(
        self, db: sqlite3.Connection, stockedup: ChannelProcessor
    ) -> None:
        for i in range(4):
            published = dt.datetime(2026, 9, 28, 18, 0, tzinfo=ET) + dt.timedelta(days=i)
            _store(db, stockedup, published, f"v{i}")
        n = db.execute(
            "SELECT COUNT(*) FROM channel_briefs WHERE status = 'active' AND channel_slug = ?",
            ("stockedup",),
        ).fetchone()[0]
        assert n == 1

    def test_older_video_stored_superseded(
        self, db: sqlite3.Connection, stockedup: ChannelProcessor
    ) -> None:
        # Two uploads the same evening (both apply to Tue 9/29); the older loses.
        late = dt.datetime(2026, 9, 28, 20, 0, tzinfo=ET)
        early = dt.datetime(2026, 9, 28, 18, 0, tzinfo=ET)
        _store(db, stockedup, late, "new")
        _, status = _store(db, stockedup, early, "old", now=late)
        assert status == STATUS_SUPERSEDED
        assert ChannelBriefRepo(db).status_of("new") == STATUS_ACTIVE

    def test_expires_after_one_session(
        self, db: sqlite3.Connection, stockedup: ChannelProcessor
    ) -> None:
        # E4.6 / D45: max_active_sessions 1. Monday evening → applies Tue 9/29 only.
        _store(db, stockedup, dt.datetime(2026, 9, 28, 18, 0, tzinfo=ET), "m")
        assert active_briefs(db, dt.datetime(2026, 9, 29, 15, 59, tzinfo=ET))
        assert active_briefs(db, dt.datetime(2026, 9, 29, 16, 0, tzinfo=ET)) == []
        assert ChannelBriefRepo(db).status_of("m") == STATUS_EXPIRED

    def test_weekend_expiry_skips_to_monday(
        self, db: sqlite3.Connection, stockedup: ChannelProcessor
    ) -> None:
        # Friday evening → applies Mon 9/28; active over the weekend and Monday only.
        _store(db, stockedup, dt.datetime(2026, 9, 25, 18, 0, tzinfo=ET), "f")
        assert active_briefs(db, dt.datetime(2026, 9, 27, 12, 0, tzinfo=ET))
        assert active_briefs(db, dt.datetime(2026, 9, 28, 12, 0, tzinfo=ET))
        assert active_briefs(db, dt.datetime(2026, 9, 28, 16, 1, tzinfo=ET)) == []

    def test_holiday_expiry(self, stockedup: ChannelProcessor) -> None:
        # Wed before Thanksgiving → applies Fri 11/27 (early close, 13:00), one session.
        brief = _build(
            stockedup,
            _payload(),
            video=_video(published=dt.datetime(2026, 11, 25, 18, 0, tzinfo=ET)),
        ).brief
        assert brief.applies_to_session == dt.date(2026, 11, 27)
        assert expires_at(brief, stockedup) == dt.datetime(2026, 11, 27, 13, 0, tzinfo=ET)

    def test_stored_already_expired(
        self, db: sqlite3.Connection, stockedup: ChannelProcessor
    ) -> None:
        _, status = _store(
            db,
            stockedup,
            dt.datetime(2026, 9, 1, 18, 0, tzinfo=ET),
            "stale",
            now=dt.datetime(2026, 9, 27, 12, 0, tzinfo=ET),
        )
        assert status == STATUS_EXPIRED
        assert active_briefs(db, dt.datetime(2026, 9, 27, 12, 0, tzinfo=ET)) == []

    def test_active_filter_by_channel(
        self, db: sqlite3.Connection, stockedup: ChannelProcessor, tmp_path: Path
    ) -> None:
        fake = _fake_channel(tmp_path).for_slug("fakechan")
        assert fake is not None
        t = dt.datetime(2026, 9, 28, 18, 0, tzinfo=ET)
        _store(db, stockedup, t, "s")
        _store(db, fake, t, "f")
        assert {b.channel_slug for b in active_briefs(db, t)} == {"stockedup", "fakechan"}
        assert [b.brief_id for b in active_briefs(db, t, channel_slug="fakechan")] == ["f"]


# ---------------------------------------------------------------------------
# Brief → Candidate bridge
# ---------------------------------------------------------------------------


class TestBriefToCandidates:
    def test_maps_universe_calls(self, stockedup: ChannelProcessor, settings: ArcSettings) -> None:
        brief = _build(stockedup, _payload()).brief
        cands = brief_to_candidates(brief, settings)
        assert len(cands) == 1
        c = cands[0]
        assert c.ticker == "NVDA"
        assert c.stance is Stance.BULLISH
        assert c.confidence == pytest.approx(0.8 * stockedup.profile.trust_weight)
        assert c.sources == [brief.video_url]
        assert c.catalyst_type is CatalystType.NEWS
        assert c.catalyst_date == dt.datetime(2026, 9, 28, tzinfo=ET)
        assert c.created_at == brief.published_at

    def test_out_of_universe_not_candidates(
        self, stockedup: ChannelProcessor, settings: ArcSettings
    ) -> None:
        video = _video(transcript="I'm shorting DELL into the close.")
        call = {
            "ticker": "DELL",
            "stance": "bearish",
            "horizon": "swing",
            "instrument_hint": "none",
            "conviction": 0.8,
            "quote": "I'm shorting DELL into the close.",
        }
        brief = _build(stockedup, {"calls": [call]}, video=video).brief
        assert brief_to_candidates(brief, settings) == []

    def test_catalyst_types_and_levels(
        self, stockedup: ChannelProcessor, settings: ArcSettings
    ) -> None:
        text = "NVDA reports Wednesday. I like NVDA here. SPY breaks 775. Fed hikes hurt JPM."
        video = _video(transcript=text)
        payload = {
            "levels": [
                {"ticker": "SPY", "kind": "pivot", "price": 775.0, "quote": "SPY breaks 775"}
            ],
            "calls": [
                {
                    "ticker": "NVDA",
                    "stance": "bullish",
                    "horizon": "swing",
                    "instrument_hint": "none",
                    "conviction": 0.6,
                    "quote": "I like NVDA here.",
                },
                {
                    "ticker": "SPY",
                    "stance": "bullish",
                    "horizon": "next_session",
                    "instrument_hint": "none",
                    "conviction": 0.5,
                    "quote": "SPY breaks 775",
                },
            ],
            "catalysts": [
                {
                    "event": "NVDA earnings",
                    "kind": "earnings",
                    "date": "2026-09-30",
                    "tickers": ["NVDA"],
                    "expected_impact": "volatile",
                    "quote": "NVDA reports Wednesday.",
                },
                {
                    "event": "Fed",
                    "kind": "fed",
                    "date": None,
                    "tickers": ["JPM", "DELL"],
                    "expected_impact": "bearish",
                    "quote": "Fed hikes hurt JPM.",
                },
            ],
        }
        brief = _build(stockedup, payload, video=video).brief
        by = {c.ticker: c for c in brief_to_candidates(brief, settings)}
        assert set(by) == {"NVDA", "SPY", "JPM"}
        assert by["NVDA"].catalyst_type is CatalystType.EARNINGS
        assert by["NVDA"].catalyst_date == dt.datetime(2026, 9, 30, tzinfo=ET)
        assert by["SPY"].catalyst_type is CatalystType.TECHNICAL
        assert by["JPM"].catalyst_type is CatalystType.MACRO
        assert by["JPM"].stance is Stance.BEARISH
        assert by["JPM"].confidence == pytest.approx(0.5 * 0.5)
        assert by["JPM"].catalyst_date is None

    def test_duplicate_calls_merge(
        self, stockedup: ChannelProcessor, settings: ArcSettings
    ) -> None:
        video = _video(transcript="I like NVDA. I really like NVDA.")
        mk = lambda conv, q: {  # noqa: E731
            "ticker": "NVDA",
            "stance": "bullish",
            "horizon": "swing",
            "instrument_hint": "none",
            "conviction": conv,
            "quote": q,
        }
        brief = _build(
            stockedup,
            {"calls": [mk(0.5, "I like NVDA."), mk(0.8, "I really like NVDA.")]},
            video=video,
        ).brief
        cands = brief_to_candidates(brief, settings)
        assert [c.ticker for c in cands] == ["NVDA"]
        assert cands[0].confidence == pytest.approx(0.4)

    def test_is_deterministic(self, stockedup: ChannelProcessor, settings: ArcSettings) -> None:
        brief = _build(stockedup, _payload()).brief
        assert brief_to_candidates(brief, settings) == brief_to_candidates(brief, settings)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def _fake_channel(
    root: Path, *, focus: list[str] | None = None, channel_id: str = "UCfake123"
) -> ChannelRegistry:
    d = root / "fakechan"
    d.mkdir(parents=True, exist_ok=True)
    lines = [
        "slug: fakechan",
        f"channel_id: {channel_id}",
        "display_name: Fake Channel",
        "cadence: weekly",
        "horizon: multi_day",
        "trust_weight: 0.8",
        "guidelines_version: fake-1",
    ]
    if focus:
        lines.append("focus: [" + ", ".join(focus) + "]")
    (d / "profile.yaml").write_text("\n".join(lines) + "\n")
    (d / "GUIDELINES.md").write_text("# Fake\nOnly extract levels.\n")
    return ChannelRegistry.load(CHANNELS_DIR, root)


class TestRegistry:
    def test_stockedup_registered(self, registry: ChannelRegistry) -> None:
        proc = registry.for_channel(STOCKEDUP_ID)
        assert proc.profile.slug == "stockedup"
        assert proc.profile.cadence.value == "trading_daily"
        assert proc.profile.horizon.value == "next_session"
        assert proc.profile.active_sessions == 1  # E4.6 / D45
        assert "Extract only what the host" in proc.guidelines

    def test_unknown_channel_falls_back_to_default(self, registry: ChannelRegistry) -> None:
        assert registry.for_channel("UCunknown").profile.slug == "default"
        assert registry.for_channel(None) is registry.default

    def test_second_channel_from_yaml_no_code_change(self, tmp_path: Path) -> None:
        reg = _fake_channel(tmp_path)
        proc = reg.for_channel("UCfake123")
        assert proc.profile.slug == "fakechan"
        assert proc.profile.trust_weight == 0.8
        assert proc.profile.active_sessions == 5  # weekly default
        assert reg.for_channel(STOCKEDUP_ID).profile.slug == "stockedup"

    def test_duplicate_channel_id_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="duplicate channel_id"):
            _fake_channel(tmp_path, channel_id=STOCKEDUP_ID)

    def test_profile_rejects_unknown_keys(self) -> None:
        with pytest.raises(ValidationError):
            ChannelProfile.model_validate(
                {
                    "slug": "x",
                    "display_name": "x",
                    "cadence": "weekly",
                    "horizon": "multi_day",
                    "guidelines_version": "1",
                    "max_position": 5,
                }
            )

    def test_profile_rejects_bad_regex(self) -> None:
        with pytest.raises(ValidationError):
            ChannelProfile.model_validate(
                {
                    "slug": "x",
                    "display_name": "x",
                    "cadence": "weekly",
                    "horizon": "multi_day",
                    "guidelines_version": "1",
                    "sponsor_patterns": ["("],
                }
            )

    def test_registry_uses_builtin_default_when_root_lacks_one(self, tmp_path: Path) -> None:
        shutil.copytree(CHANNELS_DIR / "stockedup", tmp_path / "stockedup")
        reg = ChannelRegistry.load(tmp_path)
        assert reg.default.profile.slug == "default"
        assert reg.for_channel(STOCKEDUP_ID).profile.slug == "stockedup"


# ---------------------------------------------------------------------------
# Processor + runner (LLM injected)
# ---------------------------------------------------------------------------


class _FailingLLM:
    def complete(self, prompt: str) -> LLMResult:
        raise ScoutLLMError("boom")


class TestProcessor:
    def test_prompt_contains_guidelines_and_clean_transcript(
        self, stockedup: ChannelProcessor
    ) -> None:
        llm = FixtureScoutLLM([json.dumps(_payload())])
        res = stockedup.process(_video(), llm, universe=["SPY", "NVDA"])
        prompt = llm.prompts[0]
        assert stockedup.profile.guidelines_version in prompt
        assert "Extract only what the host" in prompt
        assert "2026-09-28" in prompt
        transcript_part = prompt.split("TRANSCRIPT>>>", 1)[1]
        assert "BIGMONEY" not in transcript_part  # sponsor read stripped before the LLM
        assert "Support at 768 has held." in transcript_part
        assert "unverified_price" not in prompt
        assert res.kept == 5 and res.dropped == []
        assert res.brief.tickers_mentioned == ["SPY", "NVDA"]
        assert res.model == "fixture"

    def test_unparsable_reply_raises(self, stockedup: ChannelProcessor) -> None:
        with pytest.raises(BriefParseError):
            stockedup.process(_video(), FixtureScoutLLM(["no json here"]), universe=[])
        with pytest.raises(BriefParseError):
            stockedup.process(_video(), FixtureScoutLLM(["{bad json}"]), universe=[])

    def test_llm_error_propagates(self, stockedup: ChannelProcessor) -> None:
        with pytest.raises(ScoutLLMError):
            stockedup.process(_video(), _FailingLLM(), universe=[])


def _insert_video(
    db: sqlite3.Connection,
    vid: str,
    published: dt.datetime,
    *,
    channel_id: str | None = STOCKEDUP_ID,
    prefix: str = "[StockedUp] [Outlook] ",
) -> None:
    from arc.ingest.store import RawDocRepo

    RawDocRepo(db).insert(
        source="youtube",
        url=f"https://www.youtube.com/watch?v={vid}",
        published_at=published.astimezone(dt.UTC).isoformat(),
        text=prefix + TRANSCRIPT,
        channel_id=channel_id,
        title="Outlook" if channel_id else None,
    )


class TestRunner:
    def test_processes_newest_video_only(
        self, db: sqlite3.Connection, settings: ArcSettings
    ) -> None:
        _insert_video(db, "old", dt.datetime(2026, 9, 24, 18, tzinfo=ET))
        _insert_video(db, "new", dt.datetime(2026, 9, 25, 18, tzinfo=ET))
        llm = FixtureScoutLLM([json.dumps(_payload())])
        now = dt.datetime(2026, 9, 26, 9, tzinfo=ET)
        run = process_new_videos(db, settings, llm, now=now)
        assert run.processed == 1 and run.stored == {STATUS_ACTIVE: 1}
        assert len(llm.prompts) == 1
        (brief,) = active_briefs(db, now)
        assert brief.video_id == "new"
        assert [c.ticker for c in run.candidates] == ["NVDA"]

        # Second run: nothing new.
        again = process_new_videos(db, settings, FixtureScoutLLM([]), now=now)
        assert again.processed == 0 and again.skipped == 1

    def test_next_video_supersedes(self, db: sqlite3.Connection, settings: ArcSettings) -> None:
        _insert_video(db, "fri", dt.datetime(2026, 9, 25, 18, tzinfo=ET))
        process_new_videos(
            db,
            settings,
            FixtureScoutLLM([json.dumps(_payload())]),
            now=dt.datetime(2026, 9, 26, tzinfo=ET),
        )
        _insert_video(db, "mon", dt.datetime(2026, 9, 28, 18, tzinfo=ET))
        now = dt.datetime(2026, 9, 28, 19, tzinfo=ET)
        process_new_videos(db, settings, FixtureScoutLLM([json.dumps(_payload())]), now=now)
        assert [b.video_id for b in active_briefs(db, now)] == ["mon"]
        statuses = dict(db.execute("SELECT video_id, status FROM channel_briefs").fetchall())
        assert statuses == {"fri": STATUS_SUPERSEDED, "mon": STATUS_ACTIVE}

    def test_llm_failure_leaves_video_for_retry(
        self, db: sqlite3.Connection, settings: ArcSettings
    ) -> None:
        _insert_video(db, "v", dt.datetime(2026, 9, 25, 18, tzinfo=ET))
        now = dt.datetime(2026, 9, 26, tzinfo=ET)
        run = process_new_videos(db, settings, _FailingLLM(), now=now)
        assert run.failed == 1 and run.processed == 0
        run = process_new_videos(db, settings, FixtureScoutLLM(["not json"]), now=now)
        assert run.failed == 1
        run = process_new_videos(db, settings, FixtureScoutLLM([json.dumps(_payload())]), now=now)
        assert run.processed == 1

    def test_unknown_channel_uses_default_processor(
        self, db: sqlite3.Connection, settings: ArcSettings
    ) -> None:
        _insert_video(
            db,
            "u",
            dt.datetime(2026, 9, 25, 18, tzinfo=ET),
            channel_id="UCother",
            prefix="[Other] [T] ",
        )
        now = dt.datetime(2026, 9, 26, tzinfo=ET)
        run = process_new_videos(db, settings, FixtureScoutLLM([json.dumps(_payload())]), now=now)
        assert run.results[0].brief.channel_slug == "default"
        assert run.candidates[0].confidence == pytest.approx(0.8 * 0.3)

    def test_legacy_row_matched_by_channel_name(
        self, db: sqlite3.Connection, settings: ArcSettings
    ) -> None:
        _insert_video(db, "legacy", dt.datetime(2026, 9, 25, 18, tzinfo=ET), channel_id=None)
        now = dt.datetime(2026, 9, 26, tzinfo=ET)
        run = process_new_videos(db, settings, FixtureScoutLLM([json.dumps(_payload())]), now=now)
        assert run.results[0].brief.channel_slug == "stockedup"
        assert run.results[0].brief.title == "Outlook"

    def test_video_from_row_strips_prefix(self) -> None:
        row = {
            "id": "r1",
            "url": "https://www.youtube.com/watch?v=abc",
            "published_at": "2026-09-25T22:00:00+00:00",
            "text": "[StockedUp] [Big [week]] Kind: captions Language: en Hello SPY.",
            "channel_id": STOCKEDUP_ID,
            "title": "Big [week]",
        }
        v = video_from_row(row)
        assert v.video_id == "abc"
        assert v.transcript.startswith("Hello SPY")
        assert v.title == "Big [week]"
        assert v.published_at.tzinfo == ET
        legacy = video_from_row(dict(row, text="[StockedUp] [Plain] Hi.", title=None))
        assert (legacy.title, legacy.transcript) == ("Plain", "Hi.")


class TestFixtureDryRun:
    """The shipped fixture: a real (trimmed) StockedUp transcript + canned reply."""

    def test_fixture_end_to_end(
        self, db: sqlite3.Connection, settings: ArcSettings, stockedup: ChannelProcessor
    ) -> None:
        load_channel_fixture(db, stockedup)
        meta = json.loads((FIXTURES / "video.json").read_text())
        now = dt.datetime.fromisoformat(meta["published_at"]) + dt.timedelta(hours=1)
        run = process_new_videos(db, settings, fixture_llm(stockedup), now=now)
        assert run.processed == 1
        res = run.results[0]
        # The canned reply contains one fabricated call and one call quoting the
        # stripped Discord shout-out; both must be dropped by grounding.
        assert res.dropped_by_reason == {DROP_UNGROUNDED: 2}
        assert {d.item["ticker"] for d in res.dropped} == {"NVDA", "MSFT"}
        assert res.sponsor_sentences_removed >= 3
        brief = res.brief
        assert brief.applies_to_session == dt.date(2026, 9, 28)
        assert brief.sponsor_segments_removed
        assert "SPY" in brief.tickers_mentioned
        assert {lvl.ticker for lvl in brief.levels} >= {"SPY", "DELL", "RKLB", "COIN", "CRWD"}
        # Hedged momentum plays are capped at 0.4 conviction.
        assert all(c.conviction <= 0.4 for c in brief.calls if c.horizon.value == "next_session")
        assert active_briefs(db, now)[0].brief_id == brief.brief_id

    def test_cli_dry_run(self, capsys: pytest.CaptureFixture[str]) -> None:
        from arc.cli import main

        assert main(["ingest", "youtube", "--process", "--dry-run"]) == 0
        out = json.loads(capsys.readouterr().out)
        assert out["processed"] == 1
        assert out["briefs"][0]["dropped"] == {DROP_UNGROUNDED: 2}

    def test_cli_brief_show(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from arc.cli import main

        now = dt.datetime(2026, 9, 28, 9, 0, tzinfo=ET)
        # `arc brief show` reads the clock via briefs.now_et; pin it to the same instant.
        monkeypatch.setattr("arc.ingest.channels.briefs.now_et", lambda: now)
        path = tmp_path / "arc.db"
        conn = connect(path)
        migrate(conn)
        proc = default_registry().for_slug("stockedup")
        assert proc is not None
        res = _build(
            proc,
            _payload(),
            video=_video(published=now - dt.timedelta(minutes=5)),
        )
        ChannelBriefRepo(conn).store(res, proc, now=now)
        conn.close()

        assert main(["brief", "show", "--channel", "stockedup", "--db", str(path)]) == 0
        shown = json.loads(capsys.readouterr().out)
        assert shown["brief_id"] == res.brief.brief_id
        assert main(["brief", "show", "--db", str(path)]) == 0
        assert len(json.loads(capsys.readouterr().out)) == 1
        assert main(["brief", "show", "--channel", "nobody", "--db", str(path)]) == 1


# ---------------------------------------------------------------------------
# Guard rails
# ---------------------------------------------------------------------------


def test_processor_has_no_broker_access() -> None:
    """AGENTS.md: personas never call the broker. The channel code imports none."""
    for path in CHANNELS_DIR.glob("*.py"):
        src = path.read_text()
        assert "arc.broker" not in src, path
        assert "arc.execution" not in src, path
        assert "alpaca" not in src.lower(), path
