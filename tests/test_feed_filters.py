"""E4.11 (D55): news feed retarget + per-feed title filters (``filtered`` status)."""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from pydantic import ValidationError

from arc.config import ArcSettings
from arc.control.registry import NOT_EXPOSED_PATHS, TunableError, lookup
from arc.ingest import rss
from arc.ingest.llm import FixtureScalpLLM
from arc.ingest.scalp import run_scalp
from arc.ingest.sources import FeedSpec, SourceCategory, SourceRegistry
from arc.ingest.store import FILTERED_STATUS, RawDocRepo
from arc.routines.config import DEFAULT_ROUTINES_PATH, RoutinesConfig, load_routines
from arc.slack.digests import scalp_card
from arc.store.db import connect
from arc.store.migrate import migrate
from arc.utils.calendar import ET

NOW = dt.datetime(2026, 10, 5, 12, 0, tzinfo=ET)

# Real Seeking Alpha market_currents titles (live store, 2026-10-05).
DIVIDEND_TITLES = [
    "Mulvihill Premium Yield Fund declares CAD 0.07 dividend",
    "Premium Income Corporation PFD SHS decreases dividend to $0.09",
    "Mulvihill Canadian Bank ETF declares $0.05 dividend",
    "Pembina Pipeline Corporation PFD CL A SER 15 declares CAD 0.3852 dividend",
    "Diversified Royalty declares CAD 0.02375 dividend",
]
KEPT_TITLES = [
    "Align Technology hit as appeals court revives dental aligners antitrust claims",
    "Cleveland-Cliffs rises 8% amid broader metals rebound",
    "Dividend Roundup: Mastercard, General Mills, Verizon, AT&T, and more",
]


@pytest.fixture()
def conn():
    c = connect(":memory:")
    migrate(c)
    return c


def _shipped_feeds() -> dict[str, FeedSpec]:
    routines = load_routines(DEFAULT_ROUTINES_PATH)
    feeds = [FeedSpec.parse(f) for f in routines.sources["rss"].options["feeds"]]
    return {f.key: f for f in feeds}


# ---------------------------------------------------------------------------
# shipped config
# ---------------------------------------------------------------------------


class TestShippedFeeds:
    def test_retargeted_feeds(self) -> None:
        feeds = _shipped_feeds()
        assert "cnbc" not in feeds
        assert feeds["cnbc_earnings"].url == "https://www.cnbc.com/id/15839135/device/rss/rss.html"
        assert feeds["cnbc_business"].url == "https://www.cnbc.com/id/10001147/device/rss/rss.html"
        assert feeds["wsj_business"].url == (
            "https://feeds.content.dowjones.io/public/rss/WSJcomUSBusiness"
        )
        assert feeds["seekingalpha"].url == "https://seekingalpha.com/market_currents.xml"
        assert all(f.feed == "scalp" for f in feeds.values())  # D54: every feed declares it

    def test_registry_labels_and_categories(self) -> None:
        reg = SourceRegistry.from_routines(load_routines(DEFAULT_ROUTINES_PATH))
        got = {
            k: (reg.sources[k].display, reg.sources[k].category)
            for k in ("cnbc_earnings", "cnbc_business", "wsj_business")
        }
        assert got == {
            "cnbc_earnings": ("CNBC Earnings", SourceCategory.COMPANY_DATA),
            "cnbc_business": ("CNBC Business", SourceCategory.MARKET_NEWS),
            "wsj_business": ("WSJ Business", SourceCategory.COMPANY_DATA),
        }
        assert abs(sum(reg.effective_weights().values()) - 1.0) < 1e-9

    @pytest.mark.parametrize("title", DIVIDEND_TITLES)
    def test_sa_filter_drops_dividend_declarations(self, title: str) -> None:
        assert _shipped_feeds()["seekingalpha"].title_filtered(title)

    @pytest.mark.parametrize("title", KEPT_TITLES)
    def test_sa_filter_keeps_company_news(self, title: str) -> None:
        assert not _shipped_feeds()["seekingalpha"].title_filtered(title)

    def test_other_feeds_have_no_filter(self) -> None:
        for key, f in _shipped_feeds().items():
            if key != "seekingalpha":
                assert not f.title_filtered(DIVIDEND_TITLES[0]), key

    def test_etf_distribution_pattern(self) -> None:
        sa = _shipped_feeds()["seekingalpha"]
        assert sa.title_filtered("Global X ETF announces monthly distribution")


# ---------------------------------------------------------------------------
# FeedSpec filters + alias
# ---------------------------------------------------------------------------


def _feed(**kw: Any) -> FeedSpec:
    return FeedSpec.parse({"url": "https://x.example/rss", **kw})


class TestFeedSpecFilters:
    def test_include_list_keeps_only_matches(self) -> None:
        f = _feed(title_include=[r"\bearnings\b"])
        assert not f.title_filtered("Nvidia earnings beat")
        assert f.title_filtered("Fed holds rates")
        assert f.title_filtered(None)

    def test_exclude_wins_over_include(self) -> None:
        f = _feed(title_include=["earnings"], title_exclude=["preview"])
        assert f.title_filtered("Earnings preview: AAPL")

    def test_no_filters_keeps_everything(self) -> None:
        assert not _feed().title_filtered(None)

    def test_bad_regex_fails_load(self) -> None:
        with pytest.raises(ValidationError, match="invalid title filter"):
            _feed(title_exclude=["(unclosed"])

    def test_feed_level_feed_value_validated_against_cadence(self) -> None:
        raw = {
            "sources": {
                "rss": {
                    "every": "15m",
                    "writes": ["raw_doc_ref"],
                    "feeds": [
                        {
                            "name": "a",
                            "url": "https://a.example/rss",
                            "category": "market_news",
                            "feed": "scout",
                        },
                    ],
                }
            }
        }
        with pytest.raises(ValidationError, match="feed 'a'.*feed scout needs"):
            RoutinesConfig.model_validate(raw)
        raw["sources"]["rss"]["feeds"][0]["feed"] = "bogus"
        with pytest.raises(ValidationError, match="feed must be scalp"):
            RoutinesConfig.model_validate(raw)

    def test_retired_cnbc_key_keeps_its_label(self) -> None:
        reg = SourceRegistry.from_routines(load_routines(DEFAULT_ROUTINES_PATH))
        spec = reg.spec_for("cnbc")
        assert (spec.display, spec.category) == ("CNBC", SourceCategory.MARKET_NEWS)
        assert reg.key_for({"source": "rss", "source_key": "cnbc", "url": "u"}) == "cnbc"
        # never a budget source: only configured feeds draw a share
        assert "cnbc" not in reg.effective_weights()

    def test_filters_not_runtime_tunable(self) -> None:
        assert set(NOT_EXPOSED_PATHS) == {
            "sources.rss.feeds[].title_exclude",
            "sources.rss.feeds[].title_include",
            "funnel.scalp.doc_budget_split",  # D56: fixed splits
            "funnel.scout.video_budget_split",
            "steps.quant.revise.min_remaining_s",  # E13.9: loop plumbing
        }
        with pytest.raises(TunableError):
            lookup("sources.rss.feeds.seekingalpha.title_exclude")


# ---------------------------------------------------------------------------
# connector: filtered entries are stored, closed, never returned
# ---------------------------------------------------------------------------


class _Entry(dict):
    __getattr__ = dict.get


def _fetch(conn: Any, monkeypatch: pytest.MonkeyPatch, titles: list[str]) -> rss.RssFetch:
    url = "https://seekingalpha.com/market_currents.xml"
    entries = [
        _Entry(
            link=f"https://seekingalpha.com/news/{i}",
            title=t,
            summary=t,
            published=(NOW - dt.timedelta(minutes=10 + i)).strftime("%a, %d %b %Y %H:%M:%S %z"),
        )
        for i, t in enumerate(titles)
    ]

    class _Parsed:
        bozo = False

    parsed = _Parsed()
    parsed.entries = entries  # type: ignore[attr-defined]
    monkeypatch.setattr(rss, "_download", lambda *_a, **_k: b"<rss/>")
    monkeypatch.setattr(rss.feedparser, "parse", lambda *_a, **_k: parsed)
    s = ArcSettings(env="paper", ingest_rss_feeds=[url])  # type: ignore[call-arg]
    return rss.fetch_rss_feeds(
        conn,
        s,
        source_keys={url: "seekingalpha"},
        feed_specs={url: _shipped_feeds()["seekingalpha"]},
        now=NOW,
    )


class TestConnector:
    def test_filtered_entries_stored_closed_not_returned(self, conn, monkeypatch) -> None:
        out = _fetch(conn, monkeypatch, DIVIDEND_TITLES + KEPT_TITLES[:2])
        assert out.new == {"seekingalpha": 2}
        assert out.filtered == {"seekingalpha": 5}
        assert sorted(d.url for d in out.docs) == [
            "https://seekingalpha.com/news/5",
            "https://seekingalpha.com/news/6",
        ]
        rows = conn.execute(
            "SELECT title, scalp_status, scalped_at IS NOT NULL AS closed, scalp_run_id"
            " FROM raw_docs ORDER BY url"
        ).fetchall()
        assert len(rows) == 7  # never silently dropped
        by_title = {r["title"]: r for r in rows}
        for t in DIVIDEND_TITLES:
            assert by_title[t]["scalp_status"] == FILTERED_STATUS
            assert by_title[t]["closed"] == 1
            assert by_title[t]["scalp_run_id"] is None  # claimed by the next Scalp
        for t in KEPT_TITLES[:2]:
            assert by_title[t]["scalp_status"] is None and by_title[t]["closed"] == 0

    def test_refetch_is_deduplicated(self, conn, monkeypatch) -> None:
        _fetch(conn, monkeypatch, DIVIDEND_TITLES)
        conn.execute("DELETE FROM ingest_cursors")
        again = _fetch(conn, monkeypatch, DIVIDEND_TITLES)
        assert again.filtered == {} and again.new == {}
        assert conn.execute("SELECT count(*) FROM raw_docs").fetchone()[0] == 5

    def test_fetch_rss_wrapper_returns_docs_only(self, conn, monkeypatch) -> None:
        url = "https://seekingalpha.com/market_currents.xml"
        monkeypatch.setattr(rss, "_download", lambda *_a, **_k: b"<rss/>")

        class _Parsed:
            bozo = False
            entries = [
                _Entry(
                    link="https://sa/1",
                    title=DIVIDEND_TITLES[0],
                    published="Mon, 05 Oct 2026 15:00:00 +0000",
                ),
                _Entry(
                    link="https://sa/2",
                    title=KEPT_TITLES[0],
                    published="Mon, 05 Oct 2026 15:01:00 +0000",
                ),
            ]

        monkeypatch.setattr(rss.feedparser, "parse", lambda *_a, **_k: _Parsed())
        s = ArcSettings(env="paper", ingest_rss_feeds=[url])  # type: ignore[call-arg]
        docs = rss.fetch_rss(conn, s, feed_specs={url: _shipped_feeds()["seekingalpha"]}, now=NOW)
        assert [d.url for d in docs] == ["https://sa/2"]


# ---------------------------------------------------------------------------
# Scalp: filtered docs counted once, never read
# ---------------------------------------------------------------------------


def _scalp_settings() -> ArcSettings:
    return ArcSettings(
        env="paper",
        universe=["AAPL", "NVDA", "SPY"],
        universe_mode="strict",
        scalp_doc_budget=120,
    )  # type: ignore[call-arg]


class TestScalpCountsFiltered:
    def test_filtered_counted_not_read(self, conn) -> None:
        repo = RawDocRepo(conn)
        for i, t in enumerate(DIVIDEND_TITLES):
            repo.insert(
                source="rss",
                url=f"https://sa/{i}",
                published_at=(NOW - dt.timedelta(minutes=5)).isoformat(),
                text=t,
                title=t,
                source_key="seekingalpha",
                closed_status=FILTERED_STATUS,
            )
        llm = FixtureScalpLLM([])
        reg = SourceRegistry.from_routines(load_routines(DEFAULT_ROUTINES_PATH))
        res = run_scalp(conn, _scalp_settings(), llm=llm, now=NOW, run_id="s1", registry=reg)
        assert res.filtered == 5 and res.filtered_by_source == {"seekingalpha": 5}
        assert res.docs_scalped == 0 and llm.prompts == []
        assert {r[0] for r in conn.execute("SELECT scalp_run_id FROM raw_docs")} == {"s1"}
        # each filtered doc is counted by exactly one run
        res2 = run_scalp(conn, _scalp_settings(), llm=llm, now=NOW, run_id="s2", registry=reg)
        assert res2.filtered == 0
        statuses = {r[0] for r in conn.execute("SELECT scalp_status FROM raw_docs")}
        assert statuses == {FILTERED_STATUS}

    def test_card_shows_filtered_line(self) -> None:
        view = scalp_card(
            docs=3, accepted=0, candidates=[], rejected={}, filtered={"SA": 5, "WSJ": 0}
        )
        text = str(view.blocks)
        assert "Filtered (title filter, not read)" in text and "SA 5" in text
        assert "WSJ 0" not in text
        assert "Filtered" not in str(
            scalp_card(docs=3, accepted=0, candidates=[], rejected={}).blocks
        )


def test_tower_sources_count_filtered_today(conn) -> None:
    from arc.tower.data_ops import load_sources

    repo = RawDocRepo(conn)
    for i, t in enumerate(DIVIDEND_TITLES[:3]):
        repo.insert(
            source="rss",
            url=f"https://sa/{i}",
            published_at=NOW.isoformat(),
            text=t,
            title=t,
            source_key="seekingalpha",
            closed_status=FILTERED_STATUS,
        )
    repo.insert(
        source="rss",
        url="https://sa/kept",
        published_at=NOW.isoformat(),
        text=KEPT_TITLES[0],
        source_key="seekingalpha",
    )
    conn.execute(  # pin ingested_at (the insert stamps the wall clock)
        "UPDATE raw_docs SET ingested_at = ?",
        (NOW.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),),
    )
    resp = load_sources(conn, load_routines(DEFAULT_ROUTINES_PATH), now=NOW)
    rows = {r.key: r for r in resp.sources}
    sa = rows["seekingalpha"]
    assert (sa.docs_today, sa.filtered_today) == (4, 3)
    assert rows["cnbc_earnings"].filtered_today == 0
