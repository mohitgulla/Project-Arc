"""E13.19 (D58): trending tier v2 — retail_buzz (Reddit + Stocktwits) → 4th tier.

Covers the retail_buzz source (mocked HTTP), the pure ranker (both-input first, then
single-input fill; exclusions incl. leveraged; stale / missing inputs contribute 0
with no renormalise), the 4-tier resolver (order, tail cuts trending → discovery),
MAX_CORE 25, the 25 size caps, the 7th category and Scalp admission of trending names.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest import mock

import pytest
from hypothesis import given
from hypothesis import settings as hsettings
from hypothesis import strategies as st

from arc.config import DEFAULT_UNIVERSE, ArcSettings
from arc.context.categories import (
    CATEGORY_ORDER,
    KIND_CATEGORY,
    RESEARCH_CATEGORIES,
    SourceCategory,
)
from arc.context.kinds import (
    KINDS,
    RetailBuzzInput,
    RetailBuzzPayload,
    RetailBuzzRow,
)
from arc.context.store import ContextStore
from arc.control.registry import MAX_UNIVERSE, lookup
from arc.ingest.retail_buzz import fetch_retail_buzz, parse_apewisdom, parse_stocktwits
from arc.ingest.retail_buzz_config import RetailBuzzConfig
from arc.journal.reasons import REASON_LABELS, ReasonCode
from arc.routines.config import DEFAULT_ROUTINES_PATH, load_routines
from arc.routines.handlers import (
    BUILTIN_HANDLERS,
    JobContext,
    retail_buzz_source,
    run_trending_tier,
    universe_trending_source,
)
from arc.store.db import connect
from arc.store.migrate import migrate
from arc.universe.config import TiersConfig, UniverseConfig, load_universe_config
from arc.universe.guard import UniverseGuard
from arc.universe.master import SymbolInfo, SymbolMaster
from arc.universe.screen import LiquidityMetrics, ScreenResult
from arc.universe.tiers import (
    DROP_OVER_ACTIVE_CAP,
    MAX_CORE,
    TIER_ORDER,
    Tier,
    TierMember,
    UniverseTierPayload,
    build_active,
    resolve_active,
    tier_floor,
    tier_membership,
    tier_sizes,
)
from arc.universe.trending import (
    EXCLUDED_LEVERAGED,
    TrendingError,
    TrendingOptions,
    latest_buzz,
    leveraged,
    rank_normalise,
    rank_trending,
    run_trending,
    score_inputs,
    screen_pool,
)
from arc.utils.calendar import ET

if TYPE_CHECKING:
    import sqlite3

REPO = Path(__file__).resolve().parents[1]
NOW = dt.datetime(2026, 10, 8, 5, 50, tzinfo=ET)
DAY = NOW.date()
ENABLED = ["reddit", "stocktwits"]


def _settings(**kw: Any) -> ArcSettings:
    return ArcSettings(_env_file=None, env="paper", **kw)  # type: ignore[call-arg]


def _master(*syms: str, names: dict[str, str] | None = None, **flags: Any) -> SymbolMaster:
    names = names or {}
    return SymbolMaster(
        fetched_at=NOW,
        symbols={
            s: SymbolInfo(
                symbol=s,
                name=names.get(s, f"{s} Inc"),
                exchange=flags.get("exchange", "NASDAQ"),
                options=flags.get("options", True),
                tradable=True,
                sources=["sec", "alpaca"],
            )
            for s in syms
        },
    )


def _ape(rows: list[tuple[str, float, int | None]]) -> RetailBuzzInput:
    """(symbol, mentions, rank_24h_ago) in rank order."""
    return RetailBuzzInput(
        type="apewisdom",
        status="ok",
        fetched_at=NOW.isoformat(),
        urls=["https://ape/1"],
        rows=[
            RetailBuzzRow(symbol=s, position=i, rank=i, mentions=m, rank_24h_ago=prev)
            for i, (s, m, prev) in enumerate(rows, 1)
        ],
    )


def _st(syms: list[str], **extra: dict[str, Any]) -> RetailBuzzInput:
    return RetailBuzzInput(
        type="stocktwits",
        status="ok",
        fetched_at=NOW.isoformat(),
        urls=["https://st/1"],
        rows=[
            RetailBuzzRow(
                symbol=s,
                position=i,
                rank=i,
                trending_score=float(100 - i),
                **extra.get(s, {}),
            )
            for i, s in enumerate(syms, 1)
        ],
    )


def _buzz(**inputs: RetailBuzzInput) -> RetailBuzzPayload:
    return RetailBuzzPayload(as_of=NOW.isoformat(), session=DAY.isoformat(), inputs=inputs)


def _failed(type_: str = "stocktwits") -> RetailBuzzInput:
    return RetailBuzzInput(
        type=type_,  # type: ignore[arg-type]
        status="failed",
        fetched_at=NOW.isoformat(),
        error="URLError: boom",
    )


def _job_ctx(db: sqlite3.Connection, routines: Any, job: str) -> JobContext:
    kind, spec = routines.step(job)
    return JobContext(
        job=job,
        kind=kind,
        spec=spec,
        run_id="r1",
        chain_run_id=None,
        scheduled_for=NOW,
        now=NOW,
        conn=db,
        snapshot=ContextStore(db).snapshot(NOW, kinds=[]),
        routines=routines,
        settings_factory=_settings,
    )


@pytest.fixture
def db(tmp_path: Path) -> sqlite3.Connection:
    conn = connect(tmp_path / "arc.db")
    migrate(conn)
    return conn


# -- categories, kinds, config ---------------------------------------------------------


class TestCategory:
    def test_seven_categories_retail_buzz_last(self) -> None:
        assert len(CATEGORY_ORDER) == 7
        assert CATEGORY_ORDER[-1] is SourceCategory.RETAIL_BUZZ
        assert KIND_CATEGORY["retail_buzz"] is SourceCategory.RETAIL_BUZZ
        assert SourceCategory.RETAIL_BUZZ not in RESEARCH_CATEGORIES
        assert len(RESEARCH_CATEGORIES) == 6

    def test_kind_registered(self) -> None:
        assert KINDS["retail_buzz"].model is RetailBuzzPayload
        assert KINDS["universe_tier"].schema_version == 5

    def test_routines_yaml(self) -> None:
        r = load_routines(DEFAULT_ROUTINES_PATH)
        spec = r.category_spec(SourceCategory.RETAIL_BUZZ)
        assert spec.weight == 1 and str(spec.max_age) == "1d" and spec.label == "Retail buzz"
        buzz = r.sources["retail_buzz"]
        assert buzz.options["category"] == "retail_buzz"
        assert buzz.writes == ["retail_buzz"]
        cfg = RetailBuzzConfig.from_options(buzz.options)
        assert {n: s.type for n, s in cfg.inputs.items()} == {
            "reddit": "apewisdom",
            "stocktwits": "stocktwits",
        }
        assert len(cfg.inputs["reddit"].urls) == 2
        trend = r.sources["universe.trending"]
        assert trend.options["pool"] == 40 and trend.options["min_inputs_first"] == 2
        assert trend.reads == ["retail_buzz", "universe_tier"]
        assert "retail_buzz" in (r.personas["scout"].reads or [])
        assert r.funnel.scout.max_discovery == 25
        for job in ("retail_buzz", "universe.trending"):
            assert job in BUILTIN_HANDLERS

    def test_bad_inputs_block_fails_load(self) -> None:
        with pytest.raises(ValueError):
            RetailBuzzConfig.from_options({"inputs": {"x": {"type": "news", "urls": ["u"]}}})
        with pytest.raises(ValueError):
            RetailBuzzConfig.from_options({})


class TestSizes:
    def test_max_core_25(self) -> None:
        assert MAX_CORE == 25 == MAX_UNIVERSE
        assert lookup("universe").max_items == 25
        with pytest.raises(ValueError):
            UniverseConfig(core=[f"T{i}" for i in range(26)])
        assert len(DEFAULT_UNIVERSE) == 20  # core stays 20 names

    def test_size_caps_25(self) -> None:
        s = _settings()
        assert s.universe_trending_size == 25 and s.universe_discovery_size == 25
        assert s.universe_momentum_size_d56 == 20 and s.universe_active_max == 50
        assert s.universe_floor_trending == 0.6
        for key in ("universe_trending_size", "universe_discovery_size"):
            t = lookup(key)
            assert t.max == 25 and t.hard_ceiling == 25
            with pytest.raises(ValueError):
                _settings(**{key: 26})
        assert lookup("universe_floor_trending").min == 0.30
        assert tier_sizes(s) == {Tier.MOMENTUM: 20, Tier.DISCOVERY: 25, Tier.TRENDING: 25}
        assert tier_floor(s, Tier.TRENDING) == 0.6

    def test_universe_yaml_four_tiers(self) -> None:
        cfg = load_universe_config(REPO / "config" / "universe.yaml")
        assert cfg.tiers.order == ["core", "momentum", "discovery", "trending"]
        assert cfg.tier_screen("trending") == "loose"
        assert TiersConfig().order == ["core", "momentum", "discovery", "trending"]
        with pytest.raises(ValueError, match="fixed"):
            TiersConfig(order=["core", "momentum", "trending", "discovery"])

    def test_reason_label(self) -> None:
        assert REASON_LABELS[ReasonCode.UNIVERSE_TRENDING_LEVERAGED]


# -- the source (mocked HTTP) -----------------------------------------------------------

APE_P1 = {
    "results": [
        {"rank": 1, "ticker": "TEM", "name": "Tempus", "mentions": "90", "rank_24h_ago": "4"},
        {"rank": 2, "ticker": "BULL", "name": "Webull", "mentions": "60", "rank_24h_ago": None},
    ]
}
APE_P2 = {"results": [{"rank": 3, "ticker": "tem", "mentions": 5}, {"rank": 4, "ticker": "SOXS"}]}
ST = {
    "symbols": [
        {"symbol": "BTC.X", "exchange": "CRYPTO", "region": "US", "trending_score": 9},
        {"symbol": "TEM", "exchange": "NASDAQ", "region": "US", "trending_score": 7.5, "rank": 2},
    ]
}


def _getter(pages: dict[str, Any], fail: set[str] | None = None) -> Any:
    calls: list[tuple[str, float, int]] = []

    def get(url: str, timeout: float, retries: int) -> bytes:
        calls.append((url, timeout, retries))
        if fail and url in fail:
            msg = f"down: {url}"
            raise OSError(msg)
        return json.dumps(pages[url]).encode()

    get.calls = calls  # type: ignore[attr-defined]
    return get


CFG = RetailBuzzConfig.from_options(
    {
        "inputs": {
            "reddit": {"type": "apewisdom", "urls": ["ape1", "ape2"], "timeout_s": 7, "retries": 2},
            "stocktwits": {"type": "stocktwits", "urls": ["st"]},
        }
    }
)


class TestSource:
    def test_parsers(self) -> None:
        rows = parse_apewisdom([APE_P1, APE_P2])
        assert [(r.symbol, r.position, r.mentions) for r in rows] == [
            ("TEM", 1, 90.0),
            ("BULL", 2, 60.0),
            ("SOXS", 3, None),
        ]  # duplicate `tem` on page 2 dropped (first listing wins)
        st_rows = parse_stocktwits([ST])
        assert [(r.symbol, r.exchange, r.trending_score) for r in st_rows] == [
            ("BTC.X", "CRYPTO", 9.0),
            ("TEM", "NASDAQ", 7.5),
        ]

    def test_fetch_uses_timeout_and_retries(self) -> None:
        get = _getter({"ape1": APE_P1, "ape2": APE_P2, "st": ST})
        p = fetch_retail_buzz(CFG, now=NOW, get=get)
        assert p.live == ["reddit", "stocktwits"] and p.session == "2026-10-08"
        assert get.calls[0] == ("ape1", 7.0, 2) and get.calls[2] == ("st", 15.0, 1)
        assert p.inputs["reddit"].digest and len(p.inputs["reddit"].rows) == 3

    def test_one_input_failing_is_isolated(self) -> None:
        p = fetch_retail_buzz(
            CFG, now=NOW, get=_getter({"ape1": APE_P1, "ape2": APE_P2}, fail={"st"})
        )
        assert p.live == ["reddit"]
        assert p.inputs["stocktwits"].status == "failed"
        assert "down: st" in (p.inputs["stocktwits"].error or "")

    def test_bad_json_and_empty_pages_fail_the_input(self) -> None:
        get = _getter({"ape1": {"nope": 1}, "ape2": APE_P2, "st": {"symbols": []}})
        p = fetch_retail_buzz(CFG, now=NOW, get=get)
        assert p.live == []
        assert p.inputs["reddit"].status == "failed" and "KeyError" in str(p.inputs["reddit"].error)
        assert p.inputs["stocktwits"].error == "no rows"

    def _ctx(self, db: sqlite3.Connection, routines: Any) -> JobContext:
        return _job_ctx(db, routines, "retail_buzz")

    def test_job_writes_one_entry(self, db: sqlite3.Connection) -> None:
        routines = load_routines(DEFAULT_ROUTINES_PATH)
        urls = RetailBuzzConfig.from_options(routines.sources["retail_buzz"].options).inputs
        pages = {urls["reddit"].urls[0]: APE_P1, urls["reddit"].urls[1]: APE_P2}
        get = _getter(pages, fail={urls["stocktwits"].urls[0]})
        res = retail_buzz_source(self._ctx(db, routines), get=get)
        assert "reddit 3" in res.summary and "no data: stocktwits" in res.summary
        rows = db.execute(
            "SELECT subject, payload FROM context_entries WHERE kind='retail_buzz'"
        ).fetchall()
        assert len(rows) == 1 and rows[0][0] == "all"
        assert RetailBuzzPayload.model_validate_json(rows[0][1]).live == ["reddit"]

    def test_job_fails_when_no_input_answers(self, db: sqlite3.Connection) -> None:
        routines = load_routines(DEFAULT_ROUTINES_PATH)
        with pytest.raises(RuntimeError, match="no input answered"):
            retail_buzz_source(self._ctx(db, routines), get=_getter({}, fail=None))
        assert not db.execute("SELECT 1 FROM context_entries WHERE kind='retail_buzz'").fetchall()


# -- pure ranking -----------------------------------------------------------------------


@hsettings(max_examples=100, deadline=None)
@given(st.dictionaries(st.sampled_from([f"T{i}" for i in range(30)]), st.floats(0, 1e6)))
def test_rank_normalise_properties(raw: dict[str, float]) -> None:
    out = rank_normalise(raw)
    assert set(out) == set(raw)
    assert all(0 < v <= 1 for v in out.values())
    for a in raw:
        for b in raw:
            if raw[a] > raw[b]:
                assert out[a] > out[b]
            if raw[a] == raw[b]:
                assert out[a] == out[b]


SYMS = ("AAA", "BBB", "CCC", "DDD", "EEE", "FFF", "GGG")


class TestRank:
    def test_both_inputs_first_then_single_fill(self) -> None:
        # reddit: AAA BBB CCC DDD (AAA strongest); stocktwits: EEE CCC BBB
        buzz = _buzz(
            reddit=_ape([("AAA", 100, 1), ("BBB", 80, 9), ("CCC", 50, 9), ("DDD", 40, 9)]),
            stocktwits=_st(["EEE", "CCC", "BBB"]),
        )
        inputs = score_inputs(buzz, ENABLED, master=_master(*SYMS))
        ranked, excluded = rank_trending(inputs, enabled_count=2, exclude={}, min_inputs_first=2)
        assert not excluded
        both = [r.ticker for r in ranked if r.n_inputs == 2]
        assert both == ["CCC", "BBB"] or both == ["BBB", "CCC"]
        assert [r.ticker for r in ranked[:2]] == both  # both-input names lead
        single = ranked[2:]
        assert {r.ticker for r in single} == {"AAA", "DDD", "EEE"}
        # AAA (top reddit) scores higher than any both-input name yet still comes after
        aaa = next(r for r in ranked if r.ticker == "AAA")
        assert aaa.trend_score < 1 and all(r.n_inputs == 1 for r in single)
        assert [r.trend_score for r in single] == sorted(
            (r.trend_score for r in single), reverse=True
        )
        # trend_score = sum / 2 enabled inputs
        ccc = next(r for r in ranked if r.ticker == "CCC")
        assert ccc.trend_score == pytest.approx(sum(ccc.scores.values()) / 2, abs=1e-6)
        assert ccc.reason(ENABLED).startswith("reddit #3 · stocktwits #2 · score ")

    def test_ties_break_by_ticker(self) -> None:
        # same mentions, same 24h gain (0): identical scores
        buzz = _buzz(reddit=_ape([("BBB", 5, 1), ("AAA", 5, 2)]))
        inputs = score_inputs(buzz, ["reddit"], master=_master(*SYMS))
        ranked, _ = rank_trending(inputs, enabled_count=1, exclude={}, min_inputs_first=2)
        assert [r.trend_score for r in ranked] == [ranked[0].trend_score] * 2
        assert [r.ticker for r in ranked] == ["AAA", "BBB"]

    def test_missing_input_contributes_zero_no_renormalise(self) -> None:
        full = score_inputs(
            _buzz(reddit=_ape([("AAA", 9, 1)]), stocktwits=_st(["AAA"])),
            ENABLED,
            master=_master(*SYMS),
        )
        half = score_inputs(
            _buzz(reddit=_ape([("AAA", 9, 1)]), stocktwits=_failed()),
            ENABLED,
            master=_master(*SYMS),
        )
        absent = score_inputs(_buzz(reddit=_ape([("AAA", 9, 1)])), ENABLED, master=_master(*SYMS))
        assert [i.status for i in absent] == ["ok", "missing"]
        r_full, _ = rank_trending(full, enabled_count=2, exclude={}, min_inputs_first=2)
        r_half, _ = rank_trending(half, enabled_count=2, exclude={}, min_inputs_first=2)
        r_abs, _ = rank_trending(absent, enabled_count=2, exclude={}, min_inputs_first=2)
        assert r_full[0].trend_score == 1.0
        assert r_half[0].trend_score == 0.5 == r_abs[0].trend_score  # never renormalised

    def test_stale_entry_contributes_zero(self, db: sqlite3.Connection) -> None:
        buzz = _buzz(reddit=_ape([("AAA", 9, 1)]), stocktwits=_st(["AAA"]))
        inputs = score_inputs(buzz, ENABLED, master=_master(*SYMS), stale=True)
        assert [i.status for i in inputs] == ["stale", "stale"]
        assert all(i.scores() == {} for i in inputs)
        with pytest.raises(TrendingError, match="0 live"):
            run_trending(
                buzz,
                enabled=ENABLED,
                stale=True,
                opts=TrendingOptions(),
                now=NOW,
                master=_master(*SYMS),
                size=25,
                exclude={},
                screen=None,
            )
        # latest_buzz: an entry older than max_age reads stale
        at = NOW - dt.timedelta(hours=30)
        ContextStore(db).write(
            kind="retail_buzz",
            subject="all",
            payload=buzz,
            produced_by="t",
            ttl="3d",
            valid_from=at,
            now=at,
        )
        db.commit()
        got, stale = latest_buzz(db, now=NOW, max_age=dt.timedelta(hours=24))
        assert got is not None and stale
        _, fresh_stale = latest_buzz(
            db, now=at + dt.timedelta(hours=1), max_age=dt.timedelta(hours=24)
        )
        assert fresh_stale is False
        assert latest_buzz(db, now=at - dt.timedelta(hours=1), max_age=dt.timedelta(1)) == (
            None,
            False,
        )

    def test_eligibility_and_crypto(self) -> None:
        master = _master("AAA", "BBB")
        master.symbols["BBB"] = master.symbols["BBB"].model_copy(update={"options": False})
        buzz = _buzz(
            reddit=_ape([("AAA", 9, 1), ("BBB", 8, 1), ("ZZZ", 7, 1)]),
            stocktwits=_st(["ETH.X", "AAA", "SHOP"], **{"ETH.X": {"exchange": "CRYPTO"}}),
        )
        reddit, stw = score_inputs(buzz, ENABLED, master=master)
        assert set(reddit.raw) == {"AAA"}
        assert reddit.dropped == {
            "BBB": "no listed options at the broker",
            "ZZZ": "not in the symbol master",
        }
        assert set(stw.raw) == {"AAA"} and stw.dropped["ETH.X"] == "CRYPTO symbol"

    @pytest.mark.parametrize(
        ("name", "lev"),
        [
            ("Direxion Daily Semiconductor Bear 3X ETF", True),
            ("ProShares UltraPro QQQ", True),
            ("GraniteShares ETF Trust GraniteShares 2x Long NVDA Daily ETF", True),
            ("Direxion Shares ETF Trust Direxion Daily TSLA Bull 2X ETF", True),
            ("ProShares UltraPro Short S&P 500", True),
            ("Ultra Clean Holdings, Inc.", False),
            ("BUILD-A-BEAR WORKSHOP INC", False),
            ("Webull Corp", False),
            ("SPDR Gold Trust", False),
        ],
    )
    def test_leveraged_names(self, name: str, lev: bool) -> None:
        assert leveraged(name) is lev


def _screen_all(passing: set[str] | None = None) -> Any:
    def screen(sym: str) -> ScreenResult:
        ok = passing is None or sym in passing
        return ScreenResult(
            ticker=sym,
            passed=ok,
            failures=[] if ok else ["price 1.00 < 3.00"],
            metrics=LiquidityMetrics(ticker=sym, as_of=DAY),
        )

    return screen


class TestRun:
    def _run(self, buzz: RetailBuzzPayload, **kw: Any) -> Any:
        master = kw.pop(
            "master",
            _master(
                *SYMS,
                "SOXS",
                "GOOG",
                "GOOGL",
                "SPY",
                names={"SOXS": "Direxion Daily Semiconductor Bear 3X ETF"},
            ),
        )
        return run_trending(
            buzz,
            enabled=ENABLED,
            stale=False,
            opts=kw.pop("opts", TrendingOptions()),
            now=NOW,
            master=master,
            size=kw.pop("size", 25),
            exclude=kw.pop("exclude", {}),
            screen=kw.pop("screen", None),
        )

    def test_exclusions_before_the_cut(self) -> None:
        buzz = _buzz(
            reddit=_ape([("GOOG", 9, 1), ("SPY", 8, 1), ("SOXS", 7, 1), ("AAA", 6, 1)]),
            stocktwits=_st(["SOXS", "BBB"]),
        )
        from arc.universe.trending import normalise_exclusions

        excl = normalise_exclusions(
            {"GOOGL": "core", "SPY": "market_reference", "CCC": "discovery"}, {"GOOG": "GOOGL"}
        )
        res = self._run(buzz, exclude=excl, size=2)
        # the cut is taken after the exclusions (BBB = stocktwits #2 outscores reddit #4)
        assert res.tickers == ["BBB", "AAA"]
        assert {r.ticker: r.excluded for r in res.excluded} == {
            "GOOG": "core",
            "SPY": "market_reference",
            "SOXS": EXCLUDED_LEVERAGED,
        }
        assert [r.ticker for r in res.leveraged] == ["SOXS"]

    def test_pool_screen_and_size(self) -> None:
        buzz = _buzz(
            reddit=_ape([(s, 10 - i, 1) for i, s in enumerate(SYMS)]),
            stocktwits=_st(["CCC", "DDD"]),
        )
        res = self._run(
            buzz,
            size=2,
            opts=TrendingOptions(pool=4),
            screen=_screen_all(passing={"DDD", "AAA", "BBB"}),
        )
        # both-input CCC, DDD first; CCC fails the screen; then AAA fills
        assert [r.ticker for r in res.pool] == ["CCC", "DDD", "AAA", "BBB"]
        assert res.tickers == ["DDD", "AAA"]
        assert [r.n_inputs for r in res.members] == [2, 1]
        assert res.pool[0].screen_passed is False
        assert res.pool[3].screen_passed is None  # tier full: not screened

    def test_one_live_input_still_builds(self) -> None:
        res = self._run(_buzz(reddit=_ape([("AAA", 9, 1)]), stocktwits=_failed()))
        assert res.tickers == ["AAA"]
        assert res.failed_inputs == {"stocktwits": "failed: URLError: boom"}
        from arc.universe.trending import build_payload, notice_line

        line = notice_line(res, None)
        assert "1 names, 0 in both inputs (first list)" in line
        assert "no data: stocktwits" in line
        pay = build_payload(res, now=NOW)
        assert pay.partial and pay.members[0].inputs == 1 and pay.tier is Tier.TRENDING

    def test_zero_live_inputs_raise(self) -> None:
        with pytest.raises(TrendingError, match="0 live"):
            self._run(_buzz(reddit=_failed("apewisdom"), stocktwits=_failed()))
        with pytest.raises(TrendingError, match="0 live"):
            self._run(None)  # type: ignore[arg-type]
        with pytest.raises(TrendingError, match="symbol master"):
            self._run(_buzz(reddit=_ape([("AAA", 1, 1)])), master=None)

    def test_screen_pool_without_screen(self) -> None:
        buzz = _buzz(reddit=_ape([("AAA", 9, 1), ("BBB", 8, 1)]))
        res = self._run(buzz, size=1)
        rows = screen_pool(res.ranked, pool=5, size=1, screen=None)
        assert [r.admitted for r in rows] == [True, False]


# -- the 4-tier resolver ------------------------------------------------------------------


def _m(t: str, tier: Tier, rank: int) -> TierMember:
    return TierMember(ticker=t, tier=tier, rank=rank, source="t", as_of=DAY)


def _tier(tier: Tier, names: list[str]) -> list[TierMember]:
    return [_m(t, tier, i) for i, t in enumerate(names, 1)]


class TestResolve:
    def test_order_and_tail_cut_trending_then_discovery(self) -> None:
        core = [f"C{i}" for i in range(20)]
        mom = [f"M{i}" for i in range(20)]
        disc = [f"D{i}" for i in range(8)]
        trend = [f"T{i}" for i in range(25)]
        kw: dict[str, Any] = {
            "core": _tier(Tier.CORE, core),
            "momentum": _tier(Tier.MOMENTUM, mom),
            "discoveries": _tier(Tier.DISCOVERY, disc),
            "trending": _tier(Tier.TRENDING, trend),
            "tier_sizes": {Tier.MOMENTUM: 20, Tier.DISCOVERY: 25, Tier.TRENDING: 25},
            "as_of": DAY,
        }
        a = resolve_active(active_max=50, **kw)
        assert a.counts == {"core": 20, "momentum": 20, "discovery": 8, "trending": 2}
        assert [m.tier for m in a.members] == sorted(
            (m.tier for m in a.members), key=TIER_ORDER.index
        )
        cuts = [d for d in a.dropped if d.reason == DROP_OVER_ACTIVE_CAP]
        assert [d.ticker for d in cuts] == trend[2:]  # the trending tail, in rank order
        # a tighter cap empties trending first, then cuts the discovery tail
        b = resolve_active(active_max=45, **kw)
        assert b.counts == {"core": 20, "momentum": 20, "discovery": 5, "trending": 0}
        b_cuts = [(d.ticker, d.tier) for d in b.dropped if d.reason == DROP_OVER_ACTIVE_CAP]
        assert b_cuts[:3] == [
            ("D5", Tier.DISCOVERY),
            ("D6", Tier.DISCOVERY),
            ("D7", Tier.DISCOVERY),
        ]
        assert {t for t, tier in b_cuts if tier is Tier.TRENDING} == set(trend)

    def test_dedupe_keeps_highest_tier(self) -> None:
        a = resolve_active(
            core=_tier(Tier.CORE, ["NVDA"]),
            momentum=[],
            discoveries=_tier(Tier.DISCOVERY, ["RKLB"]),
            trending=_tier(Tier.TRENDING, ["NVDA", "RKLB", "TEM"]),
            active_max=50,
            as_of=DAY,
        )
        assert a.tickers == ["NVDA", "RKLB", "TEM"]
        tiers = {m.ticker: (m.tier, m.also_in) for m in a.members}
        assert tiers["NVDA"] == (Tier.CORE, [Tier.TRENDING])
        assert tiers["RKLB"] == (Tier.DISCOVERY, [Tier.TRENDING])

    def test_build_active_reads_trending_feed(self, db: sqlite3.Connection) -> None:
        _write_tier(db, Tier.TRENDING, ["TEM", "PENG", "NVDA"])
        s = _settings()
        a, inputs = build_active(db, s, NOW)
        assert [m.ticker for m in inputs.trending] == ["TEM", "PENG", "NVDA"]
        assert a.counts["trending"] == 2 and a.tier_tickers(Tier.TRENDING) == ["TEM", "PENG"]
        assert tier_membership(db, s, NOW)["TEM"] is Tier.TRENDING


def _write_tier(conn: sqlite3.Connection, tier: Tier, names: list[str]) -> None:
    at = NOW - dt.timedelta(hours=1)
    ContextStore(conn).write(
        kind="universe_tier",
        subject=tier.value,
        payload=UniverseTierPayload(
            tier=tier, members=_tier(tier, names), fetched_at=at, source="test"
        ),
        produced_by="test",
        ttl="8d",
        valid_from=at,
        now=at,
    )
    conn.commit()


# -- Scalp admission ------------------------------------------------------------------------


def test_scalp_admits_trending_names(db: sqlite3.Connection) -> None:
    _write_tier(db, Tier.TRENDING, ["TEM"])
    g = UniverseGuard.from_settings(
        _settings(),
        now=NOW,
        conn=db,
        master=_master("TEM", "OUTX", *DEFAULT_UNIVERSE),
        market_factory=mock.MagicMock,
    )
    assert g.known("TEM") is None
    assert g.membership("TEM") is Tier.TRENDING
    assert g.floor_for("TEM") == 0.6
    assert g.screen_profile("TEM") == "loose"
    assert g.known("OUTX") == "not_in_tier" and "trending" in g.details["OUTX"]


# -- the job end to end (scratch store, no network) ------------------------------------------


def test_universe_trending_job_writes_tier_and_journals(db: sqlite3.Connection) -> None:
    routines = load_routines(DEFAULT_ROUTINES_PATH)
    at = NOW - dt.timedelta(minutes=10)
    ContextStore(db).write(
        kind="retail_buzz",
        subject="all",
        payload=_buzz(
            reddit=_ape([("TEM", 90, 4), ("NVDA", 80, 1), ("SOXS", 70, 9), ("AAA", 10, 9)]),
            stocktwits=_st(["TEM", "BBB"]),
        ),
        produced_by="t",
        ttl="24h",
        valid_from=at,
        now=at,
    )
    db.commit()
    master = _master(
        "TEM",
        "AAA",
        "BBB",
        "SOXS",
        *DEFAULT_UNIVERSE,
        names={"SOXS": "Direxion Daily Semiconductor Bear 3X ETF"},
    )
    spec = routines.sources["universe.trending"]
    ctx = _job_ctx(db, routines, "universe.trending")
    with (
        mock.patch("arc.universe.load_symbol_master", return_value=master),
        mock.patch.object(UniverseGuard, "screen", side_effect=lambda s, _p=None: _screen_all()(s)),
    ):
        res = universe_trending_source(ctx)
        dry = run_trending_tier(
            conn=db,
            settings=_settings(),
            routines=routines,
            options=spec.options,
            now=NOW,
            screen=False,
        )
    assert res.metrics["tickers"] == ["TEM", "BBB", "AAA"] == dry.tickers
    assert res.metrics["both_inputs"] == 1 and res.metrics["leveraged"] == ["SOXS"]
    assert res.metrics["active_trending"] == 3
    row = db.execute(
        "SELECT payload FROM context_entries WHERE kind='universe_tier' AND subject='trending'"
    ).fetchone()
    pay = UniverseTierPayload.model_validate_json(row[0])
    assert [(m.ticker, m.inputs) for m in pay.members] == [("TEM", 2), ("BBB", 1), ("AAA", 1)]
    codes = {
        r[0]
        for r in db.execute("SELECT reason_code FROM decisions WHERE subject IN ('TEM','SOXS')")
    }
    assert codes == {"universe:trending_admitted", "universe:trending_leveraged"}
    # idempotent per day: a re-run journals nothing new
    with mock.patch("arc.universe.load_symbol_master", return_value=master):
        from arc.universe.trending import journal_decisions

        assert set(journal_decisions(db, dry, at=NOW).values()) == {0}
