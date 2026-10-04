"""E4.6 (D45): daily ``youtube.briefs`` job — four channels, one pre-market run.

Network pieces (channel listing, video metadata, transcripts, the LLM) are
replaced by fakes; the channel profiles, fixtures, store and context writes
are the real ones.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from arc.config import ArcSettings
from arc.context.store import ContextStore
from arc.ingest.channels import CHANNELS_DIR, default_registry
from arc.ingest.channels.base import is_grounded, normalize_text, strip_sponsor_segments
from arc.ingest.channels.daily import (
    JOB,
    DailyBriefConfig,
    DailyChannel,
    brief_agreement,
    brief_presence_line,
    configured_channels,
    pick_video,
)
from arc.ingest.llm import FixtureScoutLLM, LLMResult
from arc.ingest.sources import SCOUT_EXCLUDED, SourceCategory, SourceRegistry
from arc.ingest.youtube import TranscriptSource, YoutubeListError
from arc.personas.builders import (
    build_director_prompt,
    channel_brief_block,
    director_input_from_context,
)
from arc.routines.config import RoutinesConfig, load_routines
from arc.routines.handlers import JobContext, youtube_briefs
from arc.store.db import connect
from arc.store.migrate import migrate
from arc.utils.calendar import ET

if TYPE_CHECKING:
    import sqlite3

SLUGS = ["stockedup", "fxevolution", "tradebrigade", "arete"]
RUN_AT = dt.datetime(2026, 10, 5, 5, 0, tzinfo=ET)  # Monday 05:00 ET


def _fixture(slug: str) -> tuple[dict[str, Any], str, str]:
    d = CHANNELS_DIR / slug / "fixtures"
    video = json.loads((d / "video.json").read_text())
    return video, (d / "transcript.txt").read_text(), (d / "llm_reply.json").read_text()


def _ts(t: dt.datetime) -> int:
    return int(t.timestamp())


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = connect(":memory:")
    migrate(c)
    return c


@pytest.fixture(scope="module")
def shipped() -> RoutinesConfig:
    return load_routines()


class FakeSession:
    """Stands in for :class:`TranscriptSession` (captions -> audio, shared per run)."""

    def __init__(self, texts: dict[str, str], pending: dict[str, str] | None = None) -> None:
        self.texts = texts
        self.pending = pending or {}
        self.calls: list[str] = []

    def transcript(
        self, info: dict[str, Any], vid: str, *, max_audio_minutes: int | None = None
    ) -> tuple[str, TranscriptSource | None, str | None]:
        self.calls.append(vid)
        if vid in self.pending:
            return "", None, self.pending[vid]
        return self.texts.get(vid, ""), TranscriptSource.CAPTIONS, None

    def finish(self) -> Any:
        from arc.ingest.youtube import YoutubeRunStats

        return YoutubeRunStats()


class RoutedLLM:
    """Answers each extraction with the fixture reply of the channel it names."""

    def __init__(self, replies: dict[str, str]) -> None:
        self.replies = replies
        self.prompts: list[str] = []

    def complete(self, prompt: str) -> LLMResult:
        self.prompts.append(prompt)
        for name, reply in self.replies.items():
            if f"Guidelines version: {name}-" in prompt or f'"{name}-' in prompt:
                return LLMResult(text=reply, model="fixture", input_tokens=100, output_tokens=50)
        for name, reply in self.replies.items():
            if name in prompt:
                return LLMResult(text=reply, model="fixture", input_tokens=100, output_tokens=50)
        return LLMResult(text="{}", model="fixture")


def _world(
    published: dict[str, dt.datetime] | None = None,
    *,
    extra_listing: dict[str, list[dict[str, Any]]] | None = None,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, dict[str, Any]], dict[str, str], dict]:
    """Listings, metadata and transcripts for the four fixture videos."""
    published = published or {}
    listings: dict[str, list[dict[str, Any]]] = {}
    infos: dict[str, dict[str, Any]] = {}
    texts: dict[str, str] = {}
    replies: dict[str, str] = {}
    for slug in SLUGS:
        video, transcript, reply = _fixture(slug)
        vid = video["video_id"]
        when = published.get(slug, RUN_AT - dt.timedelta(hours=8))
        listings[slug] = [
            *(extra_listing or {}).get(slug, []),
            {"id": vid, "title": video["title"]},
        ]
        infos[vid] = {
            "id": vid,
            "title": video["title"],
            "timestamp": _ts(when),
            "duration": 1800,
            "channel": slug,
            "live_status": "was_live" if slug == "arete" else "not_live",
        }
        texts[vid] = transcript
        replies[slug] = reply
    return listings, infos, texts, replies


def _ctx(
    conn: sqlite3.Connection, routines: RoutinesConfig, now: dt.datetime = RUN_AT
) -> JobContext:
    kind, step = routines.step(JOB)
    return JobContext(
        job=JOB,
        kind=kind,
        spec=step,
        run_id="run-yt",
        chain_run_id=None,
        scheduled_for=now,
        now=now,
        conn=conn,
        snapshot=ContextStore(conn).snapshot(now),
        routines=routines,
        settings_factory=lambda: ArcSettings(_env_file=None, env="paper"),  # type: ignore[call-arg]
    )


def _run(
    conn: sqlite3.Connection,
    routines: RoutinesConfig,
    *,
    now: dt.datetime = RUN_AT,
    published: dict[str, dt.datetime] | None = None,
    list_error: set[str] | None = None,
    pending: dict[str, str] | None = None,
    extra_listing: dict[str, list[dict[str, Any]]] | None = None,
) -> tuple[Any, FakeSession, list[str]]:
    listings, infos, texts, replies = _world(published, extra_listing=extra_listing)
    cfg = DailyBriefConfig.from_options(routines.job(JOB)[1].options)  # type: ignore[index]
    url_slug = {c.url: c.slug for c in cfg.channels}
    fetched: list[str] = []

    def list_videos(url: str, n: int) -> list[dict[str, Any]]:
        slug = url_slug[url]
        if slug in (list_error or set()):
            raise YoutubeListError(f"yt-dlp exited 1 for {slug}")
        return listings[slug][:n]

    def fetch_info(url: str) -> dict[str, Any]:
        vid = url.rsplit("=", 1)[1]
        fetched.append(vid)
        return infos.get(vid, {})

    session = FakeSession(texts, pending)
    result = youtube_briefs(
        _ctx(conn, routines, now),
        RoutedLLM(replies),
        session=session,
        list_videos=list_videos,
        fetch_info=fetch_info,
        price_lookup=None,
    )
    return result, session, fetched


# ---------------------------------------------------------------------------
# Config: four channels, config-driven
# ---------------------------------------------------------------------------


class TestConfig:
    def test_shipped_job(self, shipped: RoutinesConfig) -> None:
        found = shipped.job(JOB)
        assert found is not None
        spec = found[1]
        assert spec.schedule == [dt.time(5, 0)]
        assert spec.days == "trading"
        assert spec.llm is True
        assert spec.lane == "background"
        cfg = DailyBriefConfig.from_options(spec.options)
        assert [c.slug for c in cfg.channels] == SLUGS
        assert cfg.lookback == dt.timedelta(hours=24)
        policy = shipped.context_policy("channel_brief", JOB)
        assert policy.ttl is not None and policy.ttl.duration == dt.timedelta(hours=24)
        assert policy.supersede == "latest"

    def test_every_configured_channel_has_a_profile(self, shipped: RoutinesConfig) -> None:
        reg = default_registry()
        cfg = DailyBriefConfig.from_options(shipped.job(JOB)[1].options)  # type: ignore[index]
        for ch in cfg.channels:
            proc = reg.for_slug(ch.slug)
            assert proc is not None, ch.slug
            assert proc.profile.channel_id == ch.channel_id
            assert proc.profile.max_active_sessions == 1
            assert proc.profile.trust_weight == 0.5  # equal weight (D45)

    def test_adding_a_channel_is_config_only(self) -> None:
        opts = {
            "lookback": "36h",
            "channels": [
                {"slug": "stockedup", "channel": "UC-m6zNItyoDk5lSykDlhE4Q"},
                {"slug": "newone", "channel": "https://www.youtube.com/@someone/videos"},
            ],
        }
        cfg = DailyBriefConfig.from_options(opts)
        assert cfg.lookback == dt.timedelta(hours=36)
        assert cfg.channels[1].url == "https://www.youtube.com/@someone/videos"
        assert cfg.channels[1].source_key == "youtube.newone"
        routines = RoutinesConfig.model_validate(
            {
                "sources": {
                    JOB: {
                        "schedule": ["05:00"],
                        "category": "video",
                        "writes": ["raw_doc_ref"],
                        **opts,
                    }
                }
            }
        )
        reg = SourceRegistry.from_routines(routines)
        assert {"youtube.stockedup", "youtube.newone"} <= set(reg.sources)

    @pytest.mark.parametrize(
        "channels",
        [
            [{"slug": "a", "channel": "UCx"}, {"slug": "a", "channel": "UCy"}],
            [{"slug": "Bad Slug", "channel": "UCx"}],
            [{"slug": "a", "channel": "UCx", "title_exclude": ["("]}],
            [{"slug": "a", "channel": "UCx", "unknown": 1}],
            [],
        ],
    )
    def test_bad_channels_rejected(self, channels: list[dict[str, Any]]) -> None:
        with pytest.raises(ValueError):
            DailyBriefConfig.from_options({"channels": channels})

    def test_configured_channels(self, shipped: RoutinesConfig) -> None:
        opts = shipped.job(JOB)[1].options  # type: ignore[index]
        assert configured_channels(opts) == [
            {"slug": "stockedup", "label": "StockedUp"},
            {"slug": "fxevolution", "label": "FX"},
            {"slug": "tradebrigade", "label": "TradeBrigade"},
            {"slug": "arete", "label": "Arete"},
        ]
        assert configured_channels(None) == []


# ---------------------------------------------------------------------------
# Scout separation (rule: video never reaches the 30-min Scout)
# ---------------------------------------------------------------------------


class TestScoutSeparation:
    def test_video_category_has_no_scout_weight(self, shipped: RoutinesConfig) -> None:
        reg = SourceRegistry.from_routines(shipped)
        assert SourceCategory.VIDEO in SCOUT_EXCLUDED
        for slug in SLUGS:
            assert reg.sources[f"youtube.{slug}"].category is SourceCategory.VIDEO
        weights = reg.effective_weights()
        assert not any(k.startswith("youtube") for k in weights)
        assert sum(weights.values()) == pytest.approx(1.0)

    def test_scout_closes_video_docs_as_brief_only(self, conn: sqlite3.Connection) -> None:
        from arc.ingest.scout import load_fixture_docs, run_scout
        from arc.ingest.store import RawDocRepo

        load_fixture_docs(conn)
        RawDocRepo(conn).insert(
            source="youtube",
            url="https://www.youtube.com/watch?v=zzz",
            published_at="2026-09-25T16:00:00+00:00",
            text="Transcript: [Ch] [t] SPY to 800 tomorrow",
            tickers_hint=["SPY"],
            source_key="youtube.stockedup",
        )
        llm = FixtureScoutLLM([])
        settings = ArcSettings(_env_file=None, env="paper", universe_mode="strict")  # type: ignore[call-arg]
        res = run_scout(
            conn, settings, llm=llm, dry_run=True, now=dt.datetime(2026, 9, 28, 7, tzinfo=ET)
        )
        statuses = {
            r["url"]: r["scout_status"]
            for r in conn.execute("SELECT url, scout_status FROM raw_docs WHERE source='youtube'")
        }
        assert statuses == {"https://www.youtube.com/watch?v=zzz": "brief_only"}
        assert all("SPY to 800 tomorrow" not in p for p in llm.prompts)
        assert res.docs_scouted == 11


# ---------------------------------------------------------------------------
# Picking the video
# ---------------------------------------------------------------------------


def _ch(**kw: Any) -> DailyChannel:
    return DailyChannel(slug="x", channel="UCxxxxxxxxxxxxxxxxxxxxxx", **kw)


class TestPick:
    LB = dt.timedelta(hours=24)

    def _info(self, vid: str, hours_ago: float, **kw: Any) -> dict[str, Any]:
        return {
            "id": vid,
            "title": kw.pop("title", vid),
            "timestamp": _ts(RUN_AT - dt.timedelta(hours=hours_ago)),
            **kw,
        }

    def test_newest_in_window(self) -> None:
        infos = {"a": self._info("a", 2), "b": self._info("b", 10)}
        pick = pick_video(
            [{"id": "a"}, {"id": "b"}], lambda u: infos[u[-1]], _ch(), now=RUN_AT, lookback=self.LB
        )
        assert pick.video_id == "a"
        assert pick.metadata_fetched == 1

    def test_nothing_in_window_is_no_info(self) -> None:
        infos = {"a": self._info("a", 30), "b": self._info("b", 50)}
        pick = pick_video(
            [{"id": "a"}, {"id": "b"}], lambda u: infos[u[-1]], _ch(), now=RUN_AT, lookback=self.LB
        )
        assert pick.info is None
        assert pick.metadata_fetched == 1  # stops at the first video older than the window

    def test_live_shorts_and_title_excluded(self) -> None:
        infos = {
            "live": self._info("live", 1, live_status="is_live"),
            "short": self._info("short", 2, duration=45),
            "clip": self._info("clip", 3, title="PREMARKET LIVE: NVDA"),
            "real": self._info("real", 5, duration=1500, live_status="was_live"),
        }
        listing = [
            {"id": "live", "live_status": "is_live"},
            {"id": "short", "url": "https://www.youtube.com/shorts/short"},
            {"id": "clip", "title": "PREMARKET LIVE: NVDA"},
            {"id": "real"},
        ]
        pick = pick_video(
            listing, lambda u: infos[u.rsplit("=", 1)[1]], _ch(title_exclude=["^PREMARKET LIVE"]),
            now=RUN_AT, lookback=self.LB,
        )  # fmt: skip
        assert pick.video_id == "real"
        assert [(e.video_id, e.reason) for e in pick.excluded] == [
            ("live", "live"), ("short", "short"), ("clip", "title_exclude"),
        ]  # fmt: skip
        assert pick.metadata_fetched == 1  # listing-level exclusions cost no metadata call

    def test_short_detected_from_metadata_and_premiere_skipped(self) -> None:
        infos = {
            "prem": self._info("prem", -2),  # premieres in 2 h
            "sh": self._info("sh", 1, duration=30),
            "ok": self._info("ok", 4, duration=900),
        }
        pick = pick_video(
            [{"id": "prem"}, {"id": "sh"}, {"id": "ok"}], lambda u: infos[u.rsplit("=", 1)[1]],
            _ch(), now=RUN_AT, lookback=self.LB,
        )  # fmt: skip
        assert pick.video_id == "ok"

    def test_shorts_kept_when_channel_allows(self) -> None:
        infos = {"sh": self._info("sh", 1, duration=30)}
        pick = pick_video(
            [{"id": "sh"}],
            lambda u: infos["sh"],
            _ch(skip_shorts=False),
            now=RUN_AT,
            lookback=self.LB,
        )
        assert pick.video_id == "sh"


# ---------------------------------------------------------------------------
# The job
# ---------------------------------------------------------------------------


def _briefs(conn: sqlite3.Connection, now: dt.datetime = RUN_AT) -> dict[str, dict[str, Any]]:
    snap = ContextStore(conn).snapshot(now)
    return {e.subject: e.payload for e in snap.of_kind("channel_brief")}


class TestJob:
    def test_four_channels_four_briefs(
        self, conn: sqlite3.Connection, shipped: RoutinesConfig
    ) -> None:
        result, session, _ = _run(conn, shipped)
        briefs = _briefs(conn)
        assert set(briefs) == {f"youtube.{s}" for s in SLUGS}
        assert result.metrics["briefs"] == 4
        assert result.metrics["channels_failed"] == 0
        assert result.summary.startswith("briefs 4/4 · StockedUp ✓ FX ✓ TradeBrigade ✓ Arete ✓")
        assert not result.notice
        assert len(session.calls) == 4
        # every item grounded in its transcript; self-promo stripped
        for slug in SLUGS:
            b = briefs[f"youtube.{slug}"]
            assert b["channel_slug"] == slug
            assert b["calls"] or b["levels"] or b["risk_flags"]
        # transcripts stored as brief-only raw docs, never queued for the Scout
        rows = conn.execute(
            "SELECT source_key, scout_status FROM raw_docs"
            " WHERE source = 'youtube' ORDER BY source_key"
        ).fetchall()
        assert [(r[0], r[1]) for r in rows] == sorted((f"youtube.{s}", "brief_only") for s in SLUGS)
        per = result.metrics["channels"]
        assert per["tradebrigade"]["outcome"] == "briefed"
        assert per["tradebrigade"]["transcript_source"] == "captions"
        assert per["arete"]["input_tokens"] == 100

    def test_brief_expires_24h_after_the_run(
        self, conn: sqlite3.Connection, shipped: RoutinesConfig
    ) -> None:
        _run(conn, shipped)
        assert len(_briefs(conn, RUN_AT + dt.timedelta(hours=23, minutes=59))) == 4
        assert _briefs(conn, RUN_AT + dt.timedelta(hours=24, minutes=1)) == {}
        exp = {r[0] for r in conn.execute("SELECT expires_at FROM channel_briefs")}
        assert len(exp) == 1
        assert dt.datetime.fromisoformat(exp.pop()) <= RUN_AT + dt.timedelta(hours=24)

    def test_no_video_means_no_info(
        self, conn: sqlite3.Connection, shipped: RoutinesConfig
    ) -> None:
        old = RUN_AT - dt.timedelta(hours=30)
        result, _, _ = _run(conn, shipped, published={"tradebrigade": old})
        briefs = _briefs(conn)
        assert "youtube.tradebrigade" not in briefs
        assert len(briefs) == 3
        assert "TradeBrigade – (no video 24h)" in result.summary
        assert result.metrics["channels"]["tradebrigade"]["outcome"] == "no_video"
        assert not result.notice  # no info is not an error

    def test_yesterdays_brief_does_not_carry_over(
        self, conn: sqlite3.Connection, shipped: RoutinesConfig
    ) -> None:
        _run(conn, shipped)
        tomorrow = RUN_AT + dt.timedelta(days=1)
        # Next morning: nobody posted since; yesterday's videos are now > 24 h old.
        result, _, _ = _run(conn, shipped, now=tomorrow)
        assert _briefs(conn, tomorrow) == {}
        assert result.metrics["briefs"] == 0

    def test_listing_failure_alerts_and_others_carry_on(
        self, conn: sqlite3.Connection, shipped: RoutinesConfig
    ) -> None:
        result, _, _ = _run(conn, shipped, list_error={"fxevolution"})
        assert len(_briefs(conn)) == 3
        assert result.metrics["channels_failed"] == 1
        assert result.notice.startswith("YouTube briefs: FX failed (YoutubeListError")
        assert "FX ✗" in result.summary

    def test_pending_transcript(self, conn: sqlite3.Connection, shipped: RoutinesConfig) -> None:
        vid = _fixture("arete")[0]["video_id"]
        result, _, _ = _run(conn, shipped, pending={vid: "audio cap 4/slot reached"})
        assert "youtube.arete" not in _briefs(conn)
        assert result.metrics["channels"]["arete"]["outcome"] == "pending"
        assert "Arete – (pending: audio cap 4/slot reached)" in result.summary

    def test_rerun_same_day_is_idempotent(
        self, conn: sqlite3.Connection, shipped: RoutinesConfig
    ) -> None:
        _run(conn, shipped)
        later = RUN_AT + dt.timedelta(hours=1)
        result, session, _ = _run(conn, shipped, now=later)
        assert session.calls == []  # no transcript re-fetched
        assert {c["outcome"] for c in result.metrics["channels"].values()} == {"existing"}
        assert conn.execute("SELECT count(*) FROM channel_briefs").fetchone()[0] == 4
        assert len(_briefs(conn, later)) == 4

    def test_newer_video_supersedes(
        self, conn: sqlite3.Connection, shipped: RoutinesConfig
    ) -> None:
        _run(conn, shipped)
        nxt = RUN_AT + dt.timedelta(days=1)
        published = {s: nxt - dt.timedelta(hours=6) for s in SLUGS}
        # same fixture video ids → same brief rows; prove supersede on the context side
        new_vid = "NEWVIDEO001"
        listings_extra = {"stockedup": [{"id": new_vid, "title": "Fresh"}]}
        listings, infos, texts, replies = _world(published, extra_listing=listings_extra)
        video, transcript, _ = _fixture("stockedup")
        infos[new_vid] = {
            "id": new_vid,
            "title": "Fresh",
            "timestamp": _ts(nxt - dt.timedelta(hours=2)),
            "duration": 900,
        }
        texts[new_vid] = transcript
        cfg = DailyBriefConfig.from_options(shipped.job(JOB)[1].options)  # type: ignore[index]
        url_slug = {c.url: c.slug for c in cfg.channels}
        youtube_briefs(
            _ctx(conn, shipped, nxt),
            RoutedLLM(replies),
            session=FakeSession(texts),
            list_videos=lambda url, n: listings[url_slug[url]][:n],
            fetch_info=lambda url: infos.get(url.rsplit("=", 1)[1], {}),
            price_lookup=None,
        )
        snap = ContextStore(conn).snapshot(nxt)
        su = [e for e in snap.of_kind("channel_brief") if e.subject == "youtube.stockedup"]
        assert len(su) == 1 and su[0].payload["video_id"] == new_vid

    def test_contract_writes_only_declared_kinds(
        self, conn: sqlite3.Connection, shipped: RoutinesConfig
    ) -> None:
        _run(conn, shipped)
        kinds = {
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT kind FROM context_entries WHERE produced_by = ?", (JOB,)
            )
        }
        assert kinds == {"channel_brief", "raw_doc_ref"}


# ---------------------------------------------------------------------------
# Fixtures: real transcripts, grounded replies, promo stripped
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("slug", SLUGS[1:])
def test_new_channel_fixture_is_grounded_and_promo_stripped(slug: str) -> None:
    proc = default_registry().for_slug(slug)
    assert proc is not None
    video, transcript, reply = _fixture(slug)
    promo = video["promo_sentence"]
    assert promo in transcript
    cleaned, removed = strip_sponsor_segments(transcript, proc.profile.sponsor_patterns)
    assert removed and promo not in cleaned
    norm = normalize_text(cleaned)
    data = json.loads(reply)
    quotes = [i["quote"] for k in ("levels", "calls", "catalysts", "risk_flags") for i in data[k]]
    if data.get("market_bias"):
        quotes.append(data["market_bias"]["quote"])
    assert quotes
    assert all(is_grounded(q, norm) for q in quotes)
    guidelines = (CHANNELS_DIR / slug / "GUIDELINES.md").read_text()
    assert proc.profile.guidelines_version in guidelines


# ---------------------------------------------------------------------------
# Director view (code-built presence + agreement)
# ---------------------------------------------------------------------------

CHANNELS = [
    {"slug": "stockedup", "label": "StockedUp"},
    {"slug": "fxevolution", "label": "FX"},
    {"slug": "tradebrigade", "label": "TradeBrigade"},
    {"slug": "arete", "label": "Arete"},
]


class TestDirectorView:
    def test_presence_line(self) -> None:
        assert brief_presence_line(["stockedup", "arete"], CHANNELS) == (
            "YouTube briefs: 2/4 channels (missing: FX, TradeBrigade)"
        )
        assert brief_presence_line(SLUGS, CHANNELS) == "YouTube briefs: 4/4 channels"
        assert brief_presence_line([], CHANNELS).startswith("YouTube briefs: 0/4 channels")

    def test_agreement_counts_distinct_channels(self) -> None:
        briefs = [
            {"channel_slug": "stockedup", "market_bias": {"stance": "bullish"},
             "calls": [{"ticker": "SPY", "stance": "bullish"},
                       {"ticker": "SPY", "stance": "bullish"}]},
            {"channel_slug": "tradebrigade", "market_bias": {"stance": "bullish"},
             "calls": [{"ticker": "SPY", "stance": "bullish"}]},
            {"channel_slug": "arete", "calls": [{"ticker": "SPY", "stance": "bearish"}]},
        ]  # fmt: skip
        lines = brief_agreement(briefs, CHANNELS)
        assert lines[0] == "SPY bullish: 2/4 channels (StockedUp, TradeBrigade)"
        assert "market bullish: 2/4 channels (StockedUp, TradeBrigade)" in lines
        assert "SPY bearish: 1/4 channels (Arete)" in lines

    def test_director_prompt_has_the_briefs(
        self, conn: sqlite3.Connection, shipped: RoutinesConfig
    ) -> None:
        old = RUN_AT - dt.timedelta(hours=30)
        _run(conn, shipped, published={"fxevolution": old})
        snap = ContextStore(conn).snapshot(RUN_AT + dt.timedelta(hours=5))
        block = channel_brief_block(snap, configured_channels(shipped.job(JOB)[1].options))  # type: ignore[index]
        assert block.splitlines()[0] == "YouTube briefs: 3/4 channels (missing: FX)"
        inp = director_input_from_context(
            snap, portfolio_summary="flat", scan_date="2026-10-05", youtube_channels=CHANNELS
        )
        prompt = build_director_prompt(inp)
        assert "### YouTube channel briefs" in prompt
        assert "YouTube briefs: 3/4 channels (missing: FX)" in prompt
        assert '"channel": "TradeBrigade"' in prompt
        # no channels configured -> no section at all
        assert "YouTube channel briefs" not in build_director_prompt(
            director_input_from_context(snap, portfolio_summary="flat", scan_date="2026-10-05")
        )

    def test_brief_from_unconfigured_channel_ignored(
        self, conn: sqlite3.Connection, shipped: RoutinesConfig
    ) -> None:
        _run(conn, shipped)
        snap = ContextStore(conn).snapshot(RUN_AT)
        block = channel_brief_block(snap, CHANNELS[:1])
        assert block.splitlines()[0] == "YouTube briefs: 1/1 channels"
        assert "TradeBrigade" not in block


def test_guidelines_files_exist() -> None:
    for slug in SLUGS:
        assert (CHANNELS_DIR / slug / "GUIDELINES.md").is_file()
        assert (CHANNELS_DIR / slug / "profile.yaml").is_file()
    assert isinstance(CHANNELS_DIR, Path)


# ---------------------------------------------------------------------------
# Rule 1: a fifth channel = one config entry + one profile dir, no code change
# ---------------------------------------------------------------------------


def test_fifth_channel_from_temp_config_and_profile_root(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    import shutil

    src = CHANNELS_DIR / "fxevolution"
    dst = tmp_path / "fifthchan"
    shutil.copytree(src, dst)
    profile = (dst / "profile.yaml").read_text().replace("slug: fxevolution", "slug: fifthchan")
    profile = profile.replace("UCvJZEG5x-DVYZKTz--pS39w", "UCfifthfifthfifthfifth00")
    (dst / "profile.yaml").write_text(profile)
    routines = RoutinesConfig.model_validate(
        {
            "sources": {
                JOB: {
                    "schedule": ["05:00"],
                    "days": "trading",
                    "context": {"ttl": "24h", "supersede": "latest"},
                    "writes": ["raw_doc_ref", "channel_brief"],
                    "lookback": "24h",
                    "category": "video",
                    "profiles_dir": str(tmp_path),
                    "channels": [
                        {
                            "slug": "fifthchan",
                            "channel": "UCfifthfifthfifthfifth00",
                            "label": "Fifth",
                        }
                    ],
                }
            }
        }
    )
    video, transcript, reply = _fixture("fxevolution")
    vid = "FIFTH000001"
    info = {"id": vid, "title": video["title"], "duration": 1500,
            "timestamp": _ts(RUN_AT - dt.timedelta(hours=6))}  # fmt: skip
    result = youtube_briefs(
        _ctx(conn, routines),
        RoutedLLM({"fifthchan": reply}),
        session=FakeSession({vid: transcript}),
        list_videos=lambda url, n: [{"id": vid, "title": video["title"]}],
        fetch_info=lambda url: info,
        price_lookup=None,
    )
    assert result.summary.startswith("briefs 1/1 · Fifth ✓")
    assert set(_briefs(conn)) == {"youtube.fifthchan"}
    reg = SourceRegistry.from_routines(routines)
    assert reg.sources["youtube.fifthchan"].category is SourceCategory.VIDEO


# ---------------------------------------------------------------------------
# Rule 5: Trade Brigade Wednesday video, Thursday run without one -> no brief
# ---------------------------------------------------------------------------


def test_trade_brigade_wednesday_brief_gone_on_thursday(
    conn: sqlite3.Connection, shipped: RoutinesConfig
) -> None:
    thu = dt.datetime(2026, 10, 8, 5, 0, tzinfo=ET)
    wed = thu - dt.timedelta(days=1)
    wed_evening = dt.datetime(2026, 10, 6, 20, 0, tzinfo=ET)  # Tue evening video
    _run(conn, shipped, now=wed, published={s: wed_evening for s in SLUGS})
    assert "youtube.tradebrigade" in _briefs(conn, wed)
    # Thursday: the only Trade Brigade video is now ~33 h old -> nothing in the window.
    result, _, _ = _run(conn, shipped, now=thu, published={s: wed_evening for s in SLUGS})
    assert "youtube.tradebrigade" not in _briefs(conn, thu)
    assert _briefs(conn, thu) == {}
    assert result.metrics["channels"]["tradebrigade"]["outcome"] == "no_video"
    rows = conn.execute(
        "SELECT expires_at FROM channel_briefs WHERE channel_slug = 'tradebrigade'"
    ).fetchall()
    assert [dt.datetime.fromisoformat(r[0]) for r in rows] == [wed + dt.timedelta(hours=24)]


# ---------------------------------------------------------------------------
# Rule 6: one of four present never reads as agreement
# ---------------------------------------------------------------------------


def test_one_of_four_present(conn: sqlite3.Connection, shipped: RoutinesConfig) -> None:
    old = RUN_AT - dt.timedelta(hours=40)
    _run(conn, shipped, published={"fxevolution": old, "tradebrigade": old, "arete": old})
    snap = ContextStore(conn).snapshot(RUN_AT)
    block = channel_brief_block(snap, CHANNELS)
    lines = block.splitlines()
    assert lines[0] == "YouTube briefs: 1/4 channels (missing: FX, TradeBrigade, Arete)"
    agreement = [ln for ln in lines if ln.startswith("- ")]
    assert agreement and all(" 1/4 channels (StockedUp)" in ln for ln in agreement)


# ---------------------------------------------------------------------------
# Rule 7: one caption breaker + one audio cap across all channels of a run
# ---------------------------------------------------------------------------


def test_shared_session_429_sends_rest_to_audio_and_caps_audio(
    conn: sqlite3.Connection, shipped: RoutinesConfig
) -> None:
    from unittest import mock

    from arc.ingest.transcribe import FixtureTranscriber
    from arc.ingest.youtube import CaptionResult, CaptionStatus, TranscriptSession

    settings = ArcSettings(  # type: ignore[call-arg]
        _env_file=None, env="paper", yt_max_audio_per_slot=2, yt_caption_sleep_seconds=7.5
    )
    sleep = mock.Mock()
    session = TranscriptSession.start(
        conn, settings, now=RUN_AT, transcriber=FixtureTranscriber(text="spy audio words"),
        sleep=sleep,
    )  # fmt: skip
    infos = {
        f"v{i}": {
            "id": f"v{i}",
            "duration": 600,
            "timestamp": _ts(RUN_AT - dt.timedelta(hours=3)),
            "automatic_captions": {"en-orig": [{"ext": "vtt", "url": f"https://yt/tt?v=v{i}"}]},
        }
        for i in range(4)
    }
    ok = CaptionResult.ok("spy caption words")
    rl = CaptionResult(CaptionStatus.RATE_LIMITED, http_status=429)
    with (
        mock.patch("arc.ingest.youtube._download_subtitle", side_effect=[ok, rl]) as dl,
        mock.patch("arc.ingest.youtube.transcribe_video_audio", return_value="audio text"),
        mock.patch("arc.ingest.youtube.resolve_ffmpeg", return_value="/fake/ffmpeg"),
    ):
        out = [session.transcript(infos[f"v{i}"], f"v{i}") for i in range(4)]
    assert out[0] == ("spy caption words", TranscriptSource.CAPTIONS, None)
    assert out[1] == ("audio text", TranscriptSource.AUDIO, None)  # the 429 video
    assert out[2] == ("audio text", TranscriptSource.AUDIO, None)  # breaker: no caption call
    assert out[3] == ("", None, "run_cap_reached")  # yt_max_audio_per_slot = 2
    assert dl.call_count == 2
    sleep.assert_called_once_with(7.5)
    stats = session.finish()
    assert stats.audio == 2
    assert stats.captions_skipped == 2
    assert stats.cooldown_until is not None  # one persisted youtube:captions_backoff


# ---------------------------------------------------------------------------
# Rule 8: 4 video + 20 RSS pending -> the Scout selects 0 video docs
# ---------------------------------------------------------------------------


def test_scout_selects_no_video_docs(conn: sqlite3.Connection, shipped: RoutinesConfig) -> None:
    from arc.ingest.scout import _load_docs, select_docs
    from arc.ingest.store import RawDocRepo

    repo = RawDocRepo(conn)
    for i, slug in enumerate(SLUGS):
        repo.insert(
            source="youtube", url=f"https://www.youtube.com/watch?v=v{i}",
            published_at="2026-10-05T03:00:00+00:00", text=f"Transcript: SPY {i}",
            tickers_hint=["SPY"], source_key=f"youtube.{slug}",
        )  # fmt: skip
    for i in range(20):
        repo.insert(
            source="rss", url=f"https://example.com/n{i}",
            published_at="2026-10-05T08:00:00+00:00", text=f"Headline {i} about AAPL",
            tickers_hint=["AAPL"], source_key="rss.cnbc",
        )  # fmt: skip
    registry = SourceRegistry.from_routines(shipped)
    docs = _load_docs(repo.list_unscouted(limit=None), registry)
    assert len(docs) == 24
    selected, unselected, _ = select_docs(docs, registry, budget=120)
    assert [d.source for d in selected].count("youtube") == 0
    assert len(selected) == 20
    assert unselected == []  # video is neither picked nor left waiting
