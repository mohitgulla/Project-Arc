"""E4.5 / D30: source registry, fair selection, story clustering, two-stage Scout."""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

import pytest
from hypothesis import given
from hypothesis import settings as hsettings
from hypothesis import strategies as st

from arc.config import ArcSettings
from arc.context.kinds import KINDS, StoryPayload
from arc.ingest.llm import FixtureScoutLLM, LLMResult, ScoutLLMError
from arc.ingest.scout import (
    _Doc,
    count_corroboration,
    run_scout,
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
        scout_min_confidence=0.6,
        scout_doc_budget=120,
    )


def _routines(sources: dict[str, Any], scout: dict[str, Any] | None = None) -> RoutinesConfig:
    return RoutinesConfig.model_validate(
        {
            "sources": sources,
            "personas": {
                "scout": {
                    "schedule": ["12:00"],
                    "writes": ["candidate", "note", "story"],
                    **(scout or {}),
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
    },
    {"name": "cnbc", "label": "CNBC", "url": "https://www.cnbc.com/rss.html"},
    {"name": "sa", "label": "SA", "url": "https://seekingalpha.com/mc.xml"},
    {"name": "nasdaq", "label": "Nasdaq", "url": "https://www.nasdaq.com/feed"},
    {"name": "fed", "label": "Fed", "url": "https://www.federalreserve.gov/feeds/press_all.xml"},
]


def _shipped_like() -> RoutinesConfig:
    return _routines(
        {
            "rss": {"every": "30m", "writes": ["raw_doc_ref"], "feeds": FEEDS},
            "edgar": {"every": "15m", "writes": ["raw_doc_ref"], "label": "EDGAR"},
        }
    )


# ---------------------------------------------------------------------------
# §1 registry
# ---------------------------------------------------------------------------


class TestRegistry:
    def test_shipped_config_has_named_feeds_and_categories(self) -> None:
        reg = SourceRegistry.from_routines(load_routines(DEFAULT_ROUTINES_PATH))
        assert {"wsj", "cnbc", "seekingalpha", "nasdaq", "fed", "edgar", "earnings"} <= set(
            reg.sources
        )
        assert reg.sources["edgar"].category is SourceCategory.FILINGS
        assert reg.sources["fed"].category is SourceCategory.MACRO
        # data jobs write typed kinds, never raw docs: not Scout sources
        assert "vol_term" not in reg.sources and "unusual_options" not in reg.sources
        assert abs(sum(reg.effective_weights().values()) - 1.0) < 1e-9

    def test_plain_url_feed_still_loads(self) -> None:
        """PR #40 shape: a bare URL string is a market_news source named by its domain."""
        reg = SourceRegistry.from_routines(
            _routines(
                {
                    "rss": {
                        "every": "30m",
                        "writes": ["raw_doc_ref"],
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

    def test_default_weights_equal_per_source(self) -> None:
        w = SourceRegistry.from_routines(_shipped_like()).effective_weights()
        # 5 market_news feeds + 1 filings source, all weight 1 -> 1/6 each
        assert all(abs(v - 1 / 6) < 1e-9 for v in w.values())

    def test_adding_or_reweighting_a_source_is_config_only(self) -> None:
        """No code change: a new feed and a weight edit in YAML change the split."""
        feeds = [*FEEDS, {"name": "reuters", "url": "https://www.reuters.com/rss"}]
        feeds[0] = {**feeds[0], "weight": 2}
        reg = SourceRegistry.from_routines(
            _routines({"rss": {"every": "30m", "writes": ["raw_doc_ref"], "feeds": feeds}})
        )
        w = reg.effective_weights()
        assert "reuters" in w
        assert abs(w["wsj"] - 2 * w["cnbc"]) < 1e-9

    def test_category_weights_split_budget_first_by_category(self) -> None:
        feeds = [*FEEDS[:4], {**FEEDS[4], "category": "macro"}]
        reg = SourceRegistry.from_routines(
            _routines(
                {"rss": {"every": "30m", "writes": ["raw_doc_ref"], "feeds": feeds}},
                {"category_weights": {"market_news": 1, "macro": 1}},
            )
        )
        w = reg.effective_weights()
        assert abs(w["fed"] - 0.5) < 1e-9  # the lone macro feed gets its category's half
        assert abs(w["wsj"] - 0.125) < 1e-9

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
        sel = select_fair(_docs(counts), reg.effective_weights(), 120)
        assert all(sel.picked.get(k, 0) >= 1 for k in counts)
        assert sel.picked["fed"] == 3
        assert sel.picked["edgar"] <= 120 // 6 + 12  # its share + what `fed`/`sa` left over
        assert sel.over_budget()["edgar"] == 181 - sel.picked["edgar"]
        # the old oldest-first pick would have been 120 EDGAR filings
        assert sel.picked["edgar"] < 60

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
                category="filings",
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
                category="filings",
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


def _seed(conn, rows: list[tuple[str, str, str, str, list[str]]]) -> None:
    """rows: (id, source_key, url, text, tickers)."""
    repo = RawDocRepo(conn)
    for i, (doc_id, key, url, text, tickers) in enumerate(rows):
        repo.insert(
            source="edgar" if key == "edgar" else "rss",
            url=url,
            published_at=f"2026-09-28T1{i % 6}:00:00+00:00",
            text=text,
            tickers_hint=tickers,
            id=doc_id,
            title=text.split(".")[0],
            source_key=key,
        )


def _scout_reply(*items: dict[str, Any]) -> str:
    return json.dumps({"candidates": list(items), "scan_summary": "s"})


class _Scripted:
    """Stage-1 answers by prompt kind; stage-2 reply fixed."""

    def __init__(self, digest: str | Exception, scout: str) -> None:
        self.digest, self.scout = digest, scout
        self.prompts: list[str] = []

    def complete(self, prompt: str) -> LLMResult:
        self.prompts.append(prompt)
        if "story digest (stage 1)" in prompt:
            if isinstance(self.digest, Exception):
                raise self.digest
            return LLMResult(
                self.digest, "m-cheap", input_tokens=100, output_tokens=20, cost_usd=0.01
            )
        return LLMResult(self.scout, "m-scout", input_tokens=300, output_tokens=50, cost_usd=0.03)


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
        from arc.ingest.scout import _cluster, _load_docs

        reg = SourceRegistry.from_routines(_shipped_like())
        docs = _load_docs(RawDocRepo(conn).list_unscouted(limit=None), reg)
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
        scout = _scout_reply(
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
        llm = _Scripted(digest, scout)
        stories: list[StoryPayload] = []
        res = run_scout(
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
        stages = [r[0] for r in conn.execute("SELECT stage FROM scout_batches ORDER BY rowid")]
        assert stages == ["digest", "scout"]
        status = {r[0]: r[1] for r in conn.execute("SELECT id, scout_status FROM raw_docs")}
        assert set(status.values()) == {"scouted"}

    def test_digest_failure_falls_back_to_extractive(self, conn, settings: ArcSettings) -> None:
        _seed(conn, NV_ROWS)
        llm = _Scripted(ScoutLLMError("boom"), _scout_reply())
        res = run_scout(
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
        assert res.batches == 1  # the Scout still ran over the headline digests
        row = conn.execute("SELECT status FROM scout_batches WHERE stage='digest'").fetchone()
        assert row[0] == "llm_error"

    def test_over_budget_docs_wait_then_close_as_skipped_budget(
        self, conn, settings: ArcSettings
    ) -> None:
        rows = [
            (f"e{i}", "edgar", f"https://sec.gov/{i}", f"Form 8-K filing number {i} unique{i}.", [])
            for i in range(10)
        ] + [("w1", "wsj", "https://wsj.com/a", "Markets rally on jobs data.", [])]
        _seed(conn, rows)
        # ingested_at is stamped with the wall clock; pin it to the test's NOW so the
        # TTL arithmetic below doesn't depend on when the suite runs.
        conn.execute("UPDATE raw_docs SET ingested_at = ?", (NOW.isoformat(),))
        s = settings.model_copy(update={"scout_doc_budget": 4})
        reg = SourceRegistry.from_routines(_shipped_like())
        res = run_scout(conn, s, llm=FixtureScoutLLM([]), now=NOW, run_id="r1", registry=reg)
        assert dict((lbl, (r, o)) for lbl, r, o in res.source_mix) == {
            "WSJ": (1, 0),
            "EDGAR": (3, 7),
        }
        assert res.over_budget == 7 and res.skipped_budget == 0  # still inside the TTL
        later = NOW + dt.timedelta(days=6)  # past the 5d raw_doc_ref TTL
        routines = load_routines(DEFAULT_ROUTINES_PATH)
        res2 = run_scout(
            conn,
            s,
            llm=FixtureScoutLLM([]),
            now=later,
            run_id="r2",
            registry=reg,
            routines=routines,
        )
        # r2 reads 4 of the 7 waiting filings, closes the 3 left over as skipped_budget
        assert res2.skipped_budget == 3
        closed = conn.execute(
            "SELECT count(*) FROM raw_docs"
            " WHERE scout_status='skipped_budget' AND scout_run_id='r2'"
        ).fetchone()[0]
        assert closed == 3

    def test_story_payload_is_a_registered_kind(self) -> None:
        assert KINDS["story"].model is StoryPayload
        assert KINDS["candidate"].schema_version == 2


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
    reg = SourceRegistry(sources={"x": SourceSpec(key="x", job="x", category=SourceCategory.MACRO)})
    assert reg.spec_for("youtube.other").category is SourceCategory.VIDEO
