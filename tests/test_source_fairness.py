"""E4.5 / D30: source registry, fair selection, story clustering, two-stage Scalp."""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

import pytest
import structlog.testing
from hypothesis import given
from hypothesis import settings as hsettings
from hypothesis import strategies as st

from arc.config import ArcSettings
from arc.context.kinds import KINDS, StoryPayload
from arc.context.ttl import Ttl
from arc.ingest.llm import FixtureScalpLLM, LLMResult, ScalpLLMError
from arc.ingest.scalp import (
    _Doc,
    count_corroboration,
    run_scalp,
    select_docs,
    story_payload,
)
from arc.ingest.sources import (
    FeedSpec,
    SourceCategory,
    SourceRegistry,
    SourceSpec,
    select_fair,
)
from arc.ingest.store import RawDocRepo
from arc.ingest.stories import (
    ClusterDoc,
    canonical_url,
    cluster_stories,
    headline_of,
    jaccard,
    normalize_headline,
)
from arc.routines.config import DEFAULT_ROUTINES_PATH, RoutinesConfig, load_routines
from arc.store.db import connect
from arc.store.migrate import migrate
from arc.utils.calendar import ET

NOW = dt.datetime(2026, 9, 28, 12, 0, tzinfo=ET)
UNIVERSE = ["AAPL", "NVDA", "SPY", "XOM", "TSLA"]


@pytest.fixture()
def conn():
    c = connect(":memory:")
    migrate(c)
    return c


@pytest.fixture()
def settings() -> ArcSettings:
    return ArcSettings(
        env="paper",
        universe=UNIVERSE,
        universe_mode="strict",
        scalp_min_confidence=0.6,
        scalp_doc_budget=120,
    )


def _routines(sources: dict[str, Any], scalp: dict[str, Any] | None = None) -> RoutinesConfig:
    return RoutinesConfig.model_validate(
        {
            "sources": sources,
            "personas": {
                "scalp": {
                    "schedule": ["12:00"],
                    "writes": ["candidate", "note", "story"],
                    **(scalp or {}),
                }
            },
        }
    )


FEEDS = [
    {
        "name": "wsj",
        "label": "WSJ",
        "url": "https://feeds.content.dowjones.io/x",
        "hosts": ["wsj.com"],
        "category": "market_news",
    },
    {
        "name": "cnbc",
        "label": "CNBC",
        "url": "https://www.cnbc.com/rss.html",
        "category": "market_news",
    },
    {
        "name": "sa",
        "label": "SA",
        "url": "https://seekingalpha.com/mc.xml",
        "category": "company_data",
    },
    {
        "name": "nasdaq",
        "label": "Nasdaq",
        "url": "https://www.nasdaq.com/feed",
        "category": "market_news",
    },
    {
        "name": "fed",
        "label": "Fed",
        "url": "https://www.federalreserve.gov/feeds/press_all.xml",
        "category": "market_news",  # D56: was macro_data
    },
]


def _shipped_like() -> RoutinesConfig:
    return _routines(
        {
            "rss": {"every": "30m", "writes": ["raw_doc_ref"], "feeds": FEEDS},
            "edgar": {
                "every": "15m",
                "writes": ["raw_doc_ref"],
                "label": "EDGAR",
                "category": "company_data",
            },
        }
    )


# ---------------------------------------------------------------------------
# §1 registry
# ---------------------------------------------------------------------------


class TestRegistry:
    def test_shipped_config_has_named_feeds_and_categories(self) -> None:
        routines = load_routines(DEFAULT_ROUTINES_PATH)
        reg = SourceRegistry.from_routines(routines)
        assert {
            "wsj",
            "wsj_business",
            "prnewswire",
            "businesswire",
            "seekingalpha",
            "nasdaq",
            "fed",
            "edgar",
            "earnings",
        } <= set(reg.sources)
        assert "cnbc" not in reg.sources  # D55: CNBC Economy retired (label alias only)
        # E14.2 (D60): CNBC Earnings/Business retired too (label aliases only)
        assert not {"cnbc_earnings", "cnbc_business"} & set(reg.sources)
        assert reg.sources["edgar"].category is SourceCategory.COMPANY_DATA
        # D56: the earnings calendar is reference data (no category, no weight)
        assert reg.sources["earnings"].category is None and reg.sources["earnings"].reference
        assert reg.sources["fed"].category is SourceCategory.MARKET_NEWS
        assert reg.sources["wsj"].category is SourceCategory.MARKET_NEWS
        # data jobs write typed kinds, never raw docs: not Scalp sources
        assert "vol_term" not in reg.sources and "unusual_options" not in reg.sources
        assert "earnings" not in reg.effective_weights()
        assert abs(sum(reg.effective_weights().values()) - 1.0) < 1e-9
        # D56 + D58: seven categories, weighted equally, each with a freshness window
        assert [c.value for c in SourceCategory] == [
            "market_news",
            "company_data",
            "options_fast",
            "options_slow",
            "youtube_macro",
            "youtube_micro",
            "retail_buzz",
        ]
        assert set(routines.categories) == set(SourceCategory)
        windows = {c.value: s.max_age.duration for c, s in routines.categories.items()}
        assert windows == {
            "market_news": dt.timedelta(hours=6),
            "company_data": dt.timedelta(hours=12),
            "options_fast": dt.timedelta(minutes=30),
            "options_slow": dt.timedelta(hours=24),
            "youtube_macro": dt.timedelta(hours=48),  # D60
            "youtube_micro": dt.timedelta(hours=48),  # D60
            "retail_buzz": dt.timedelta(hours=24),
        }
        assert {c.weight for c in routines.categories.values()} == {1.0}
        assert routines.categories[SourceCategory.MARKET_NEWS].max_age.duration == dt.timedelta(
            hours=6
        )

    def test_every_context_source_declares_a_category(self) -> None:
        """D47: a source job that writes context without a category fails config load."""
        with pytest.raises(ValueError, match="category"):
            _routines({"edgar": {"every": "15m", "writes": ["raw_doc_ref"]}})
        with pytest.raises(ValueError, match="unknown source category"):
            _routines({"edgar": {"every": "15m", "writes": ["raw_doc_ref"], "category": "x"}})

    def test_legacy_category_names_alias_to_d49(self) -> None:
        """Pre-D47 names and the renamed D47/D49 names load as logged aliases."""
        with structlog.testing.capture_logs() as logs:
            reg = SourceRegistry.from_routines(
                _routines(
                    {
                        "edgar": {"every": "15m", "writes": ["raw_doc_ref"], "category": "filings"},
                        "earnings": {
                            "every": "1h",
                            "writes": ["raw_doc_ref"],
                            "category": "calendar",
                        },
                        "sec": {"every": "1h", "writes": ["raw_doc_ref"], "category": "company"},
                        "cboe": {
                            "every": "1h",
                            "writes": ["raw_doc_ref"],
                            "category": "options_data",
                        },
                    }
                )
            )
        assert reg.sources["edgar"].category is SourceCategory.COMPANY_DATA
        assert reg.sources["earnings"].category is SourceCategory.COMPANY_DATA
        assert reg.sources["sec"].category is SourceCategory.COMPANY_DATA
        assert reg.sources["cboe"].category is SourceCategory.OPTIONS_SLOW
        aliased = {(e["old"], e["new"]) for e in logs if e["event"] == "sources.category_alias"}
        assert ("company", "company_data") in aliased
        assert ("options_data", "options_slow") in aliased
        # D56: `macro` / `macro_data` have no successor and are refused
        with pytest.raises(ValueError, match="was removed"):
            _routines({"fedwire": {"every": "1h", "writes": ["raw_doc_ref"], "category": "macro"}})

    def test_video_category_is_refused_with_a_pointer(self) -> None:
        """D49: `video` was split in two; config load names the per-channel fix."""
        with pytest.raises(ValueError, match="split in two"):
            _routines({"edgar": {"every": "15m", "writes": ["raw_doc_ref"], "category": "video"}})

    def test_categories_block_is_strict_and_partial(self) -> None:
        r = RoutinesConfig.model_validate(
            {"categories": {"company_data": {"weight": 2, "max_age": "3d"}}}
        )
        assert r.categories[SourceCategory.COMPANY_DATA].weight == 2
        old = RoutinesConfig.model_validate({"categories": {"company": {"weight": 3}}})
        assert old.categories[SourceCategory.COMPANY_DATA].weight == 3  # D49 alias
        with pytest.raises(ValueError, match="split in two"):
            RoutinesConfig.model_validate({"categories": {"video": {"weight": 1}}})
        assert r.categories[SourceCategory.MARKET_NEWS].weight == 1  # unset: default kept
        with pytest.raises(ValueError, match="unknown category"):
            RoutinesConfig.model_validate({"categories": {"crypto": {"weight": 1}}})

    def test_plain_url_feed_still_loads(self) -> None:
        """PR #40 shape: a bare URL string is a source named by its domain (job category)."""
        reg = SourceRegistry.from_routines(
            _routines(
                {
                    "rss": {
                        "every": "30m",
                        "writes": ["raw_doc_ref"],
                        "category": "market_news",
                        "feeds": [
                            "https://www.cnbc.com/id/20910258/device/rss/rss.html",
                            "https://seekingalpha.com/market_currents.xml",
                        ],
                    }
                }
            )
        )
        assert set(reg.sources) == {"cnbc", "seekingalpha"}
        assert reg.sources["cnbc"].category is SourceCategory.MARKET_NEWS

    def test_categories_are_weighted_equally_not_sources(self) -> None:
        """D47/D56: 4 market_news feeds, 2 company sources -> 1/2 per category."""
        reg = SourceRegistry.from_routines(_shipped_like())
        w = reg.effective_weights()
        assert abs(w["sa"] - 1 / 4) < 1e-9 and abs(w["edgar"] - 1 / 4) < 1e-9
        assert all(abs(w[k] - 1 / 8) < 1e-9 for k in ("wsj", "cnbc", "nasdaq", "fed"))
        assert abs(sum(w.values()) - 1.0) < 1e-9

    def test_adding_or_reweighting_a_source_is_config_only(self) -> None:
        """No code change: a new feed splits its category's share; a weight edit in YAML."""
        feeds = [
            *FEEDS,
            {"name": "reuters", "url": "https://www.reuters.com/rss", "category": "market_news"},
        ]
        feeds[0] = {**feeds[0], "weight": 2}
        reg = SourceRegistry.from_routines(
            _routines({"rss": {"every": "30m", "writes": ["raw_doc_ref"], "feeds": feeds}})
        )
        w = reg.effective_weights()
        assert "reuters" in w
        assert abs(w["wsj"] - 2 * w["cnbc"]) < 1e-9
        market = sum(v for k, v in w.items() if k in {"wsj", "cnbc", "nasdaq", "fed", "reuters"})
        assert abs(market - 1 / 2) < 1e-9  # the category did not grow

    def test_category_weight_is_config_driven(self) -> None:
        r = RoutinesConfig.model_validate(
            {
                "sources": {"rss": {"every": "30m", "writes": ["raw_doc_ref"], "feeds": FEEDS}},
                "categories": {"company_data": {"weight": 3}},
            }
        )
        w = SourceRegistry.from_routines(r).effective_weights()
        assert abs(w["sa"] - 0.75) < 1e-9  # company 3 of (1 + 3), sa alone in it

    def test_legacy_category_weights_knob_still_applies(self) -> None:
        reg = SourceRegistry.from_routines(
            _routines(
                {"rss": {"every": "30m", "writes": ["raw_doc_ref"], "feeds": FEEDS}},
                {"category_weights": {"market_news": 1, "company": 0}},
            )
        )
        w = reg.effective_weights()
        assert abs(w["fed"] - 0.25) < 1e-9  # 1 of 4 market_news feeds, the only category
        assert "sa" not in w  # weight 0: no share

    def test_duplicate_feed_name_rejected(self) -> None:
        with pytest.raises(ValueError, match="duplicate source key"):
            SourceRegistry.from_routines(
                _routines(
                    {
                        "rss": {
                            "every": "30m",
                            "writes": ["raw_doc_ref"],
                            "feeds": [FEEDS[0], FEEDS[0]],
                        }
                    }
                )
            )

    def test_feed_name_validated(self) -> None:
        with pytest.raises(ValueError):
            FeedSpec.parse({"name": "Bad Name", "url": "https://x.example/rss"})

    def test_key_for_legacy_rows_by_host(self) -> None:
        reg = SourceRegistry.from_routines(_shipped_like())
        assert reg.key_for({"source": "rss", "url": "https://www.wsj.com/articles/x"}) == "wsj"
        assert reg.key_for({"source": "rss", "url": "https://unknown.example/x"}) == "rss"
        assert reg.key_for({"source": "edgar", "url": "https://sec.gov/x"}) == "edgar"
        assert reg.key_for({"source": "rss", "source_key": "cnbc", "url": "u"}) == "cnbc"


# ---------------------------------------------------------------------------
# §2 fair selection
# ---------------------------------------------------------------------------


def _docs(counts: dict[str, int]) -> dict[str, list[str]]:
    return {k: [f"{k}-{i}" for i in range(n)] for k, n in counts.items()}


class TestFairSelection:
    @hsettings(max_examples=300, deadline=None)
    @given(
        counts=st.dictionaries(
            st.sampled_from(["a", "b", "c", "d", "e", "f"]), st.integers(0, 60), min_size=1
        ),
        weights=st.dictionaries(
            st.sampled_from(["a", "b", "c", "d", "e", "f"]), st.floats(0.1, 5.0), min_size=6
        ),
        budget=st.integers(0, 150),
    )
    def test_no_source_over_share_while_another_waits(
        self, counts: dict[str, int], weights: dict[str, float], budget: int
    ) -> None:
        sel = select_fair(_docs(counts), weights, budget)
        assert len(sel.selected) == min(budget, sum(counts.values()))
        assert len(set(sel.selected)) == len(sel.selected)
        waiting = [k for k, n in counts.items() if sel.picked.get(k, 0) < n]
        for k in counts:
            p = sel.picked.get(k, 0)
            if p == 0:
                continue
            for other in waiting:
                if other == k:
                    continue
                # k took its last pick while `other` had docs left: k's deficit-ratio
                # before that pick was <= other's next-pick ratio.
                assert (p) / weights[k] <= (sel.picked.get(other, 0) + 1) / weights[other] + 1e-9

    @given(st.integers(1, 40), st.integers(0, 30))
    def test_unused_budget_flows_to_other_sources(self, small: int, big_extra: int) -> None:
        counts = {"small": 1, "big": small + big_extra + 50}
        sel = select_fair(_docs(counts), {"small": 1.0, "big": 1.0}, small + 10)
        assert sel.picked["small"] == 1
        assert sel.picked["big"] == small + 9

    def test_caps_are_respected(self) -> None:
        sel = select_fair(_docs({"a": 30, "b": 30}), {"a": 1, "b": 1}, 40, caps={"a": 5})
        assert sel.picked == {"a": 5, "b": 30}

    def test_flood_regression_every_source_gets_a_slot(self) -> None:
        """2026-09-28: EDGAR stored 181 filings in one tick; WSJ returned 58 items."""
        reg = SourceRegistry.from_routines(_shipped_like())
        counts = {"edgar": 181, "wsj": 58, "cnbc": 30, "sa": 12, "nasdaq": 25, "fed": 3}
        cats = {k: reg.spec_for(k).category.value for k in counts}
        sel = select_fair(
            _docs(counts),
            {k: reg.spec_for(k).weight for k in counts},
            120,
            categories=cats,
            category_weights={c.value: w for c, w in reg.category_weights().items()},
        )
        assert all(sel.picked.get(k, 0) >= 1 for k in counts)
        assert sel.picked["fed"] == 3
        # company (edgar + sa) gets its 1/3 plus what macro left over, split by source
        assert sel.picked["edgar"] <= 120 // 3 + 37 - 12 + 1
        assert sel.over_budget()["edgar"] == 181 - sel.picked["edgar"]
        # the old oldest-first pick would have been 120 EDGAR filings
        assert sel.picked["edgar"] < 60

    @hsettings(max_examples=300, deadline=None)
    @given(
        counts=st.dictionaries(
            st.sampled_from(["a", "b", "c", "d", "e", "f"]), st.integers(0, 40), min_size=1
        ),
        budget=st.integers(0, 150),
    )
    def test_no_category_over_share_while_another_waits(
        self, counts: dict[str, int], budget: int
    ) -> None:
        """D47 invariant: categories (a,b | c,d | e,f) stay within one pick of each other."""
        cat = {"a": "x", "b": "x", "c": "y", "d": "y", "e": "z", "f": "z"}
        sel = select_fair(
            _docs(counts),
            {k: 1.0 for k in cat},
            budget,
            categories={k: cat[k] for k in counts},
            category_weights={"x": 1, "y": 1, "z": 1},
        )
        assert len(sel.selected) == min(budget, sum(counts.values()))
        cp = sel.category_picked()
        avail = {c: sum(n for k, n in counts.items() if cat[k] == c) for c in set(cat.values())}
        waiting = [c for c in avail if cp.get(c, 0) < avail[c]]
        for c, n in cp.items():
            for other in waiting:
                if other != c:
                    assert n <= cp.get(other, 0) + 1

    def test_newest_first_within_a_source(self) -> None:
        reg = SourceRegistry.from_routines(_shipped_like())
        docs = [
            _mkdoc(f"w{i}", "wsj", f"2026-09-2{i}T10:00:00+00:00", f"headline {i}")
            for i in range(1, 6)
        ]
        selected, rest, mix = select_docs(docs, reg, 2)
        assert [d.id for d in selected] == ["w4", "w5"]
        assert {d.id for d in rest} == {"w1", "w2", "w3"}
        assert mix == [("WSJ", 2, 3)]


class TestFreshness:
    """D47: per-category max_age; stale docs never read, connectors never store them."""

    def test_is_stale_uses_category_window_and_override(self) -> None:
        feeds = [*FEEDS[:4], {**FEEDS[4], "max_age": "1h"}]
        reg = SourceRegistry.from_routines(
            _routines({"rss": {"every": "30m", "writes": ["raw_doc_ref"], "feeds": feeds}})
        )
        pub = NOW - dt.timedelta(hours=5)
        assert not reg.is_stale("wsj", published=pub, ingested=NOW, now=NOW)  # 6h window
        assert reg.is_stale("wsj", published=NOW - dt.timedelta(hours=7), ingested=NOW, now=NOW)
        assert reg.is_stale("fed", published=pub, ingested=NOW, now=NOW)  # its own 1h override
        assert not reg.is_stale("sa", published=pub, ingested=NOW, now=NOW)  # company 24h

    def test_ingested_age_basis(self) -> None:
        reg = SourceRegistry.from_routines(
            _routines(
                {
                    "earnings": {
                        "every": "1h",
                        "writes": ["raw_doc_ref"],
                        "category": "company_data",
                        "age_basis": "ingested",
                    }
                }
            )
        )
        old = NOW - dt.timedelta(days=30)
        assert not reg.is_stale("earnings", published=old, ingested=NOW, now=NOW)

    def test_freshness_ttl_is_shortest_window_plus_grace_capped(self) -> None:
        reg = SourceRegistry.from_routines(_shipped_like())
        ttl = reg.freshness_ttl(["wsj", "edgar"], None, NOW)
        assert ttl is not None and ttl.duration == dt.timedelta(hours=8)  # 6h + 2h
        capped = reg.freshness_ttl(["edgar"], Ttl(duration=dt.timedelta(hours=4)), NOW)
        assert capped is not None and capped.duration == dt.timedelta(hours=4)

    def test_scalp_closes_stale_docs_and_never_reads_them(self, conn, settings) -> None:
        repo = RawDocRepo(conn)
        repo.insert(
            source="rss",
            url="https://wsj.com/old",
            published_at=(NOW - dt.timedelta(hours=9)).isoformat(),
            text="Stocks slid last night on rate fears. More.",
            tickers_hint=["SPY"],
            id="old",
            source_key="wsj",
        )
        repo.insert(
            source="rss",
            url="https://wsj.com/new",
            published_at=(NOW - dt.timedelta(hours=1)).isoformat(),
            text="Nvidia rallies on new chip orders. More.",
            tickers_hint=["NVDA"],
            id="new",
            source_key="wsj",
        )
        llm = FixtureScalpLLM([_scalp_reply()])
        res = run_scalp(
            conn,
            settings,
            llm=llm,
            now=NOW,
            run_id="r1",
            registry=SourceRegistry.from_routines(_shipped_like()),
        )
        status = {r[0]: r[1] for r in conn.execute("SELECT id, scalp_status FROM raw_docs")}
        assert status == {"old": "skipped_stale", "new": "scouted"}
        assert res.skipped_stale == 1 and res.stale_by_source == {"wsj": 1}
        assert all("rate fears" not in p for p in llm.prompts)
        [mix] = res.category_mix
        assert (mix.label, mix.picked, mix.sources) == ("Market news", 1, [("WSJ", 1, 0, 1)])
        # the story built from the fresh doc expires at its window + 2h, not 1 session
        assert all(t.duration == dt.timedelta(hours=8) for t in res.story_ttls.values())

    def test_rss_connector_never_stores_stale_entries(self, conn, monkeypatch) -> None:
        import arc.ingest.rss as rss

        class _Entry(dict):
            __getattr__ = dict.get

        url = "https://www.cnbc.com/rss.html"
        entries = [
            _Entry(
                link="https://cnbc.com/old",
                title="Old",
                summary="old",
                published=(NOW - dt.timedelta(hours=9)).strftime("%a, %d %b %Y %H:%M:%S %z"),
            ),
            _Entry(
                link="https://cnbc.com/new",
                title="New",
                summary="new",
                published=(NOW - dt.timedelta(hours=1)).strftime("%a, %d %b %Y %H:%M:%S %z"),
            ),
        ]

        class _Parsed:
            bozo = False
            entries: list[Any] = []

        parsed = _Parsed()
        parsed.entries = entries
        monkeypatch.setattr(rss, "_download", lambda *_a, **_k: b"<rss/>")
        monkeypatch.setattr(rss.feedparser, "parse", lambda *_a, **_k: parsed)
        s = ArcSettings(env="paper", ingest_rss_feeds=[url])  # type: ignore[call-arg]
        docs = rss.fetch_rss(
            conn,
            s,
            source_keys={url: "cnbc"},
            max_ages={url: Ttl(duration=dt.timedelta(hours=6))},
            now=NOW,
        )
        assert [d.url for d in docs] == ["https://cnbc.com/new"]
        assert conn.execute("SELECT count(*) FROM raw_docs").fetchone()[0] == 1


class TestEdgarFreshness:
    """D47 root cause: EDGAR stamped filings with the filing *date* and stored every
    re-listed filing; old 10-Qs of new tickers flooded the queue. Now: accepted time,
    and filings older than the company window are skipped before download."""

    def _subs(self) -> dict[str, Any]:
        return {
            "filings": {
                "recent": {
                    "form": ["8-K", "10-Q", "8-K"],
                    "accessionNumber": ["0001-26-000003", "0001-26-000002", "0001-26-000001"],
                    "filingDate": ["2026-09-28", "2026-08-01", "2026-07-15"],
                    "acceptanceDateTime": [
                        "2026-09-28T14:05:11.000Z",
                        "2026-08-01T20:00:00.000Z",
                        "2026-07-15T12:00:00.000Z",
                    ],
                    "primaryDocument": ["a.htm", "b.htm", "c.htm"],
                }
            }
        }

    def test_published_at_is_acceptance_time(self) -> None:
        from arc.ingest.edgar import _filings_of_form, filing_published_at

        [f] = _filings_of_form(self._subs(), "1", "10-Q", count=5)
        assert filing_published_at(f) == dt.datetime(2026, 8, 1, 20, tzinfo=dt.UTC)
        assert filing_published_at({"filingDate": "2026-08-01"}) == dt.datetime(
            2026, 8, 1, tzinfo=dt.UTC
        )
        assert filing_published_at({}) is None

    def test_stale_filings_are_never_downloaded_or_stored(self, conn, monkeypatch) -> None:
        import arc.ingest.edgar as edgar

        fetched: list[str] = []
        monkeypatch.setattr(edgar, "_fetch_submissions", lambda *_a, **_k: self._subs())
        monkeypatch.setattr(
            edgar, "_fetch_filing_text", lambda url, *_a: fetched.append(url) or f"text {url}"
        )
        monkeypatch.setattr(
            edgar.IngestUniverse,
            "from_settings",
            classmethod(
                lambda cls, s, **_kw: type(
                    "U",
                    (),
                    {
                        "cik": lambda self, t: "1",
                        "tickers_in": lambda self, x: [],
                        "seed": tuple(s.universe),
                    },
                )()
            ),
        )
        s = ArcSettings(env="paper", universe=["AAPL"])  # type: ignore[call-arg]
        now = dt.datetime(2026, 9, 28, 12, tzinfo=ET)
        docs = edgar.fetch_edgar(conn, s, now=now, max_age=Ttl(duration=dt.timedelta(hours=24)))
        assert [d.url.rsplit("/", 1)[-1] for d in docs] == ["a.htm"]
        assert len(fetched) == 1  # old 10-Q / 8-K never downloaded
        # the cursor still advanced past the skipped filings: the next run does nothing
        docs2 = edgar.fetch_edgar(conn, s, now=now, max_age=Ttl(duration=dt.timedelta(hours=24)))
        assert docs2 == [] and len(fetched) == 1


def _mkdoc(doc_id: str, key: str, published: str, text: str, **kw: Any) -> _Doc:
    return _Doc(
        id=doc_id,
        source=kw.get("source", "rss"),
        url=kw.get("url", f"https://{key}.example/{doc_id}"),
        published_at=published,
        text=text,
        tickers_hint=kw.get("tickers", []),
        title=kw.get("title"),
        source_key=key,
        ingested_at=kw.get("ingested_at", published),
        category=kw.get("category", "market_news"),
    )


# ---------------------------------------------------------------------------
# §3 story clustering
# ---------------------------------------------------------------------------


def _cd(i: str, key: str, headline: str, hours: float = 0, **kw: Any) -> ClusterDoc:
    return ClusterDoc(
        id=i,
        source_key=key,
        source=kw.get("source", "rss"),
        url=kw.get("url", f"https://{key}.example/{i}"),
        published_at=NOW + dt.timedelta(hours=hours),
        headline=headline,
        tickers=tuple(kw.get("tickers", ())),
        category=kw.get("category", "market_news"),
        form_type=kw.get("form_type"),
    )


class TestStories:
    def test_normalize_strips_suffix_case_and_entities(self) -> None:
        a = normalize_headline("Nvidia Announces $50B Buyback - WSJ")
        b = normalize_headline("NVIDIA announces $50B buyback | Reuters")
        assert a == b

    def test_headline_fallback_keeps_abbreviations(self) -> None:
        """Live 2026-09-29: untitled legacy rows all clustered as the headline "U.S."."""
        assert headline_of(None, "U.S. stocks dropped as yields jumped. More text.") == (
            "U.S. stocks dropped as yields jumped."
        )
        assert headline_of(None, "Apple Inc. said revenue rose. Shares up 3%.") == (
            "Apple Inc. said revenue rose."
        )
        assert headline_of("<b>Title</b> &amp; more", "x") == "Title  & more"

    def test_canonical_url_drops_tracking(self) -> None:
        assert canonical_url("https://x.com/a?utm_source=rss&id=3#frag") == "https://x.com/a?id=3"

    def test_near_duplicates_cluster_and_count_distinct_sources(self) -> None:
        docs = [
            _cd("1", "wsj", "Nvidia announces $50 billion buyback"),
            _cd("2", "cnbc", "Nvidia announces $50 billion share buyback", 1),
            _cd("3", "wsj", "Nvidia announces $50 billion buyback program", 2),
            _cd("4", "nasdaq", "Oil slides as OPEC signals output hike", 1),
        ]
        stories = cluster_stories(docs, threshold=0.5, window=dt.timedelta(hours=24))
        assert len(stories) == 2
        nv = next(s for s in stories if len(s.docs) == 3)
        assert nv.distinct_sources == 2  # wsj twice counts once
        assert sorted(nv.source_keys) == ["cnbc", "wsj"]

    def test_window_splits_old_coverage(self) -> None:
        docs = [
            _cd("1", "wsj", "Fed holds rates steady"),
            _cd("2", "cnbc", "Fed holds rates steady", 30),
        ]
        assert len(cluster_stories(docs, threshold=0.5, window=dt.timedelta(hours=24))) == 2

    def test_filings_group_by_filer_and_form(self) -> None:
        docs = [
            _cd(
                str(i),
                "edgar",
                f"8-K item {i}",
                i * 0.1,
                source="edgar",
                category="company_data",
                tickers=["TSLA"],
                form_type="8-K",
            )
            for i in range(5)
        ] + [
            _cd(
                "x",
                "edgar",
                "10-Q",
                0.2,
                source="edgar",
                category="company_data",
                tickers=["TSLA"],
                form_type="10-Q",
            )
        ]
        stories = cluster_stories(docs, threshold=0.9, window=dt.timedelta(hours=24))
        assert sorted(len(s.docs) for s in stories) == [1, 5]

    @given(st.lists(st.text(alphabet="abcde ", min_size=1, max_size=20), min_size=1, max_size=15))
    def test_clustering_is_a_partition_and_deterministic(self, heads: list[str]) -> None:
        docs = [_cd(str(i), f"s{i % 3}", h, i * 0.5) for i, h in enumerate(heads)]
        a = cluster_stories(docs, threshold=0.5, window=dt.timedelta(hours=24))
        b = cluster_stories(list(reversed(docs)), threshold=0.5, window=dt.timedelta(hours=24))
        ids = sorted(d.id for s in a for d in s.docs)
        assert ids == sorted(d.id for d in docs)
        assert [sorted(d.id for d in s.docs) for s in a] == [
            sorted(d.id for d in s.docs) for s in b
        ]

    def test_jaccard_bounds(self) -> None:
        assert jaccard(frozenset(), frozenset()) == 0.0
        assert jaccard(frozenset({"a"}), frozenset({"a"})) == 1.0


# ---------------------------------------------------------------------------
# §4 corroboration + two-stage synthesis
# ---------------------------------------------------------------------------


class TestCorroboration:
    def test_repeated_items_from_one_source_count_once(self) -> None:
        keys = {"u1": "wsj", "u2": "wsj", "u3": "wsj", "u4": "cnbc"}
        assert count_corroboration(["u1", "u2", "u3"], keys.__getitem__) == 1
        assert count_corroboration(["u1", "u2", "u4"], keys.__getitem__) == 2

    @given(st.lists(st.sampled_from(["a", "b", "c"]), min_size=1, max_size=30))
    def test_corroboration_equals_distinct_sources(self, srcs: list[str]) -> None:
        urls = [f"https://{s}.example/{i}" for i, s in enumerate(srcs)]
        key = {u: s for u, s in zip(urls, srcs, strict=True)}
        assert count_corroboration(urls, key.__getitem__) == len(set(srcs))


def _seed(conn, rows: list[tuple[str, str, str, str, list[str]]], now: dt.datetime = NOW) -> None:
    """rows: (id, source_key, url, text, tickers); published inside every D47 window."""
    repo = RawDocRepo(conn)
    for i, (doc_id, key, url, text, tickers) in enumerate(rows):
        repo.insert(
            source="edgar" if key == "edgar" else "rss",
            url=url,
            published_at=(now - dt.timedelta(minutes=10 * (i + 1))).isoformat(),
            text=text,
            tickers_hint=tickers,
            id=doc_id,
            title=text.split(".")[0],
            source_key=key,
        )


def _scalp_reply(*items: dict[str, Any]) -> str:
    return json.dumps({"candidates": list(items), "scan_summary": "s"})


class _Scripted:
    """Stage-1 answers by prompt kind; stage-2 reply fixed."""

    def __init__(self, digest: str | Exception, scalp: str) -> None:
        self.digest, self.scalp = digest, scalp
        self.prompts: list[str] = []

    def complete(self, prompt: str) -> LLMResult:
        self.prompts.append(prompt)
        if "story digest (stage 1)" in prompt:
            if isinstance(self.digest, Exception):
                raise self.digest
            return LLMResult(
                self.digest, "m-cheap", input_tokens=100, output_tokens=20, cost_usd=0.01
            )
        return LLMResult(self.scalp, "m-scalp", input_tokens=300, output_tokens=50, cost_usd=0.03)


NV_ROWS = [
    (
        "d1",
        "wsj",
        "https://wsj.com/nv1",
        "Nvidia announces $50 billion buyback. Board approved.",
        ["NVDA"],
    ),
    (
        "d2",
        "wsj",
        "https://wsj.com/nv2",
        "Nvidia announces $50 billion buyback plan. More.",
        ["NVDA"],
    ),
    (
        "d3",
        "cnbc",
        "https://cnbc.com/nv",
        "Nvidia announces $50 billion share buyback. Shares up.",
        ["NVDA"],
    ),
    (
        "d4",
        "nasdaq",
        "https://nasdaq.com/xom",
        "Exxon slides as crude drops. Oil down 3%.",
        ["XOM"],
    ),
]


class TestTwoStage:
    def _story_ids(self, conn, settings: ArcSettings) -> dict[str, str]:
        from arc.ingest.scalp import _cluster, _load_docs

        reg = SourceRegistry.from_routines(_shipped_like())
        docs = _load_docs(RawDocRepo(conn).list_unscalped(limit=None), reg)
        return {s.docs[0].tickers[0]: s.id for s in _cluster(docs, settings)}

    def test_stage2_reads_digests_and_corroboration_is_code_owned(
        self, conn, settings: ArcSettings
    ) -> None:
        _seed(conn, NV_ROWS)
        sid = self._story_ids(conn, settings)
        digest = json.dumps(
            {
                "stories": [
                    {
                        "story_id": sid["NVDA"],
                        "summary": "Nvidia authorised a $50B buyback.",
                        "catalyst_type": "news",
                        "evidence": [
                            {
                                "url": "https://cnbc.com/nv",
                                "quote": "Nvidia announces $50 billion share buyback",
                            },
                            {
                                "url": "https://wsj.com/nv1",
                                "quote": "invented quote not in the doc",
                            },
                        ],
                    }
                ]
            }
        )
        scalp = _scalp_reply(
            {
                "ticker": "NVDA",
                "stance": "bullish",
                "catalyst_type": "news",
                "catalyst_date": None,
                "confidence": 0.8,
                # three URLs, two of them WSJ: corroboration must be 2, not 3
                "sources": ["https://wsj.com/nv1", "https://wsj.com/nv2", "https://cnbc.com/nv"],
                "rationale": "buyback",
            }
        )
        llm = _Scripted(digest, scalp)
        stories: list[StoryPayload] = []
        res = run_scalp(
            conn,
            settings,
            llm=llm,
            digest_llm=llm,
            now=NOW,
            run_id="r1",
            registry=SourceRegistry.from_routines(_shipped_like()),
            on_story=stories.append,
        )
        assert res.digest_batches == 1 and res.batches == 1
        assert len(res.stories) == 2 == len(stories)
        nv = next(p for p in stories if "NVDA" in p.tickers)
        assert nv.distinct_sources == 2 and nv.mode == "llm"
        assert [e.url for e in nv.evidence] == ["https://cnbc.com/nv"]  # invented quote dropped
        xom = next(p for p in stories if "XOM" in p.tickers)
        assert xom.mode == "extractive"  # the LLM skipped it: headline fallback
        stage2 = llm.prompts[-1]
        assert "story digests (D30)" in stage2
        assert "distinct_sources=2" in stage2
        assert "Board approved" not in stage2  # raw doc text never reaches stage 2
        [cand] = res.candidates
        assert cand.corroboration == 2
        row = conn.execute("SELECT corroboration FROM candidates WHERE ticker='NVDA'").fetchone()
        assert row[0] == 2
        # usage summed across both stages (D27 manifest totals)
        assert (res.input_tokens, res.output_tokens) == (400, 70)
        assert res.cost_usd == pytest.approx(0.04)
        stages = [r[0] for r in conn.execute("SELECT stage FROM scalp_batches ORDER BY rowid")]
        assert stages == ["digest", "scalp"]
        status = {r[0]: r[1] for r in conn.execute("SELECT id, scalp_status FROM raw_docs")}
        assert set(status.values()) == {"scouted"}

    def test_digest_failure_falls_back_to_extractive(self, conn, settings: ArcSettings) -> None:
        _seed(conn, NV_ROWS)
        llm = _Scripted(ScalpLLMError("boom"), _scalp_reply())
        res = run_scalp(
            conn,
            settings,
            llm=llm,
            digest_llm=llm,
            now=NOW,
            run_id="r1",
            registry=SourceRegistry.from_routines(_shipped_like()),
        )
        assert res.failed_digest_batches == 1
        assert all(p.mode == "extractive" for p in res.stories)
        assert res.batches == 1  # the Scalp still ran over the headline digests
        row = conn.execute("SELECT status FROM scalp_batches WHERE stage='digest'").fetchone()
        assert row[0] == "llm_error"

    @pytest.mark.parametrize(
        "now",
        [
            NOW,
            # Day boundary: midnight ET is 04:00Z, so ET and UTC dates differ around it.
            dt.datetime(2026, 9, 29, 0, 0, tzinfo=ET),
        ],
        ids=["midday", "et-midnight"],
    )
    def test_over_budget_docs_wait_then_close_as_stale(
        self, conn, settings: ArcSettings, now: dt.datetime
    ) -> None:
        rows = [
            (f"e{i}", "edgar", f"https://sec.gov/{i}", f"Form 8-K filing number {i} unique{i}.", [])
            for i in range(10)
        ] + [("w1", "wsj", "https://wsj.com/a", "Markets rally on jobs data.", [])]
        _seed(conn, rows, now)
        # ingested_at is stamped with the wall clock; pin it to the injected `now` so the
        # TTL arithmetic below doesn't depend on when the suite runs (E4.5a, E1.1b).
        conn.execute("UPDATE raw_docs SET ingested_at = ?", (now.isoformat(),))
        s = settings.model_copy(update={"scalp_doc_budget": 4})
        reg = SourceRegistry.from_routines(_shipped_like())
        res = run_scalp(conn, s, llm=FixtureScalpLLM([]), now=now, run_id="r1", registry=reg)
        # D47: market_news (WSJ, 1 doc) and company (EDGAR) split the 4 equally; WSJ's
        # unused share flows to EDGAR
        assert dict((lbl, (r, o)) for lbl, r, o in res.source_mix) == {
            "WSJ": (1, 0),
            "EDGAR": (3, 7),
        }
        assert res.over_budget == 7 and res.skipped_budget == 0 and res.skipped_stale == 0
        # past EDGAR's 24h company window: the waiting filings close skipped_stale
        later = now + dt.timedelta(days=2)
        routines = load_routines(DEFAULT_ROUTINES_PATH)
        res2 = run_scalp(
            conn,
            s,
            llm=FixtureScalpLLM([]),
            now=later,
            run_id="r2",
            registry=reg,
            routines=routines,
        )
        assert res2.skipped_stale == 7 and res2.docs_scalped == 0
        closed = conn.execute(
            "SELECT count(*) FROM raw_docs WHERE scalp_status='skipped_stale' AND scalp_run_id='r2'"
        ).fetchone()[0]
        assert closed == 7

    def test_budget_skip_still_closes_docs_past_the_raw_doc_ttl(
        self, conn, settings: ArcSettings
    ) -> None:
        """A source with a window longer than the raw_doc_ref TTL still gets skipped_budget."""
        rows = [
            (f"e{i}", "edgar", f"https://sec.gov/{i}", f"Form 8-K filing number {i} unique{i}.", [])
            for i in range(6)
        ]
        _seed(conn, rows)
        conn.execute("UPDATE raw_docs SET ingested_at = ?", (NOW.isoformat(),))
        s = settings.model_copy(update={"scalp_doc_budget": 2})
        r = RoutinesConfig.model_validate(
            {
                "sources": {
                    "edgar": {
                        "every": "15m",
                        "writes": ["raw_doc_ref"],
                        "category": "company_data",
                        "max_age": "30d",
                    }
                },
                "personas": {"scalp": {"schedule": ["12:00"], "writes": ["candidate", "note"]}},
            }
        )
        reg = SourceRegistry.from_routines(r)
        run_scalp(conn, s, llm=FixtureScalpLLM([]), now=NOW, run_id="r1", registry=reg)
        res2 = run_scalp(
            conn,
            s,
            llm=FixtureScalpLLM([]),
            now=NOW + dt.timedelta(days=6),
            run_id="r2",
            registry=reg,
            routines=load_routines(DEFAULT_ROUTINES_PATH),
        )
        assert res2.skipped_stale == 0 and res2.skipped_budget == 2

    def test_story_payload_is_a_registered_kind(self) -> None:
        assert KINDS["story"].model is StoryPayload
        assert KINDS["candidate"].schema_version == 3


def test_story_payload_keeps_code_fields_over_llm(conn) -> None:
    from arc.ingest.stories import Story

    d = _mkdoc(
        "a", "wsj", "2026-09-28T10:00:00+00:00", "Apple beats. Revenue up.", tickers=["AAPL"]
    )
    story = Story(
        id="s1",
        headline="Apple beats",
        docs=[
            ClusterDoc(
                id="a",
                source_key="wsj",
                source="rss",
                url=d.url,
                published_at=NOW,
                headline="Apple beats",
                tickers=("AAPL",),
                category="market_news",
            )
        ],
    )

    class _D:
        summary = "  Apple beat estimates.  "
        catalyst_type = None
        catalyst_date = "2026-10-29"
        evidence: list[Any] = []

    p = story_payload(story, {"a": d}, digest=_D())
    assert p.distinct_sources == 1 and p.source_keys == ["wsj"] and p.urls == [d.url]
    assert p.summary == "Apple beat estimates." and p.catalyst_date == "2026-10-29"


def test_registry_spec_default_for_unknown_key() -> None:
    reg = SourceRegistry(
        sources={"x": SourceSpec(key="x", job="x", category=SourceCategory.COMPANY_DATA)}
    )
    # A removed channel's legacy rows: a YouTube category, never a Scalp one (D45, D49)
    assert reg.spec_for("youtube.other").category is SourceCategory.YOUTUBE_MICRO


def test_category_tunables_reach_the_registry(conn, tmp_path) -> None:
    """D47: `!arc config set categories.<c>.weight|max_age` changes the next Scalp run."""
    from arc.control.effective import effective_routines
    from arc.control.registry import REGISTRY
    from arc.control.service import ControlService

    owner = "U0OWNER001"
    assert REGISTRY["categories.company_data.weight"].max == 5
    assert REGISTRY["categories.market_news.max_age"].min == 30
    # D56: both options categories have a duration window, so max_age is tunable
    assert REGISTRY["categories.options_fast.max_age"].min == 30
    assert REGISTRY["categories.options_slow.max_age"].min == 30
    old = (
        "categories.company.",
        "categories.macro.",
        "categories.video.",
        "categories.macro_data.",
        "categories.options_data.",
    )
    assert not any(k.startswith(old) for k in REGISTRY)
    p = tmp_path / "routines.yaml"
    p.write_text(DEFAULT_ROUTINES_PATH.read_text())
    svc = ControlService(
        conn,
        base=ArcSettings(_env_file=None, approver_slack_user_ids=[owner]),  # type: ignore[call-arg]
        now=lambda: NOW,
        optionable=lambda s: True,
        is_halted=lambda: False,
    )
    for key, value in (
        ("categories.company_data.weight", "3"),
        ("categories.market_news.max_age", "90"),
    ):
        r = svc.set(key, value, actor=owner, source="slack")
        if r.pending is not None:
            r = svc.confirm(r.pending.code, actor=owner, source="slack")
        assert r.outcome == "applied", r
    reg = SourceRegistry.from_routines(effective_routines(conn, p))
    # company 3 of (market 1 + company 3); categories without doc sources take no share
    assert reg.category_weights()[SourceCategory.COMPANY_DATA] == pytest.approx(0.75)
    assert reg.max_age_for("wsj").duration == dt.timedelta(minutes=90)
    assert reg.is_stale("wsj", published=NOW - dt.timedelta(hours=2), ingested=NOW, now=NOW)


def test_stored_override_on_a_renamed_category_key_migrates(conn, tmp_path) -> None:
    """D49: a change-log row on `categories.company.*` applies to company_data; one on
    the split `categories.video.*` is reported and dropped (it has no single successor)."""
    from arc.control.effective import effective_routines
    from arc.control.service import ControlService
    from arc.control.store import ConfigChangeRepo

    p = tmp_path / "routines.yaml"
    p.write_text(DEFAULT_ROUTINES_PATH.read_text())
    repo = ConfigChangeRepo(conn)
    for key, new in (
        ("categories.company.weight", 3),
        ("categories.macro.max_age", 2880),  # D56: removed category, orphaned
        ("categories.video.weight", 4),
    ):
        repo.append(
            key=key, old=1, new=new, is_default=False, actor="U0OWNER001", reason=None,
            at=NOW, source="slack", status="applied", direction="neutral",
        )  # fmt: skip
    with structlog.testing.capture_logs() as logs:
        r = effective_routines(conn, p)
    assert r.categories[SourceCategory.COMPANY_DATA].weight == 3
    assert r.categories[SourceCategory.YOUTUBE_MACRO].weight == 1  # video: dropped
    assert r.categories[SourceCategory.YOUTUBE_MICRO].weight == 1
    dropped = [e["key"] for e in logs if e["event"] == "control.override_unknown_key"]
    assert dropped == ["categories.video.weight"]
    orphaned = [e["key"] for e in logs if e["event"] == "config.override_orphaned"]
    assert orphaned == ["categories.macro.max_age"]
    # `!arc config` shows the migrated value under the new key, never the old one
    svc = ControlService(
        conn,
        base=ArcSettings(_env_file=None),  # type: ignore[call-arg]
        now=lambda: NOW,
        optionable=lambda s: True,
        is_halted=lambda: False,
    )
    shown = list(svc.keys())
    assert "categories.company.weight" not in shown
    v = svc.view("categories.company_data.weight")
    assert v.overridden and v.last is not None and v.last.key == "categories.company.weight"
