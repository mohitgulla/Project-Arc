"""E12.3 / D51: trending tier (daily rules-based top N) + ticker-extraction cleanup."""

from __future__ import annotations

import datetime as dt
import json
import textwrap
from typing import TYPE_CHECKING, Any

import pytest
import requests
import yaml
from hypothesis import given
from hypothesis import strategies as st

from arc.config import ArcSettings
from arc.context.store import ContextStore
from arc.journal.reasons import REASON_LABELS, ReasonCode
from arc.routines.config import RoutinesConfig, load_routines
from arc.routines.dispatcher import Dispatcher
from arc.routines.handlers import BUILTIN_HANDLERS
from arc.routines.heartbeat import RecordingNotifier
from arc.store.db import connect
from arc.store.migrate import migrate
from arc.universe.config import ExtractionConfig, load_universe_config
from arc.universe.extract import extract_tickers
from arc.universe.ingest import IngestUniverse, UniverseMode
from arc.universe.master import SymbolInfo, SymbolMaster
from arc.universe.screen import LiquidityMetrics, ScreenResult
from arc.universe.tiers import Tier, UniverseTierPayload, build_active
from arc.universe.trending import (
    InputResult,
    TrendingConfig,
    TrendingError,
    TrendingInput,
    TrendingRow,
    apewisdom_input,
    build_payload,
    eligible,
    journal_decisions,
    news_input,
    notice_line,
    rank_normalise,
    rank_trending,
    run_trending,
    scout_input,
    screen_pool,
    stocktwits_input,
)
from arc.utils.calendar import ET

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable
    from pathlib import Path

NOW = dt.datetime(2026, 10, 5, 8, 45, tzinfo=ET)  # Monday, the 08:45 slot
APE1 = "https://apewisdom.io/api/v1.0/filter/all-stocks/page/1"
APE2 = "https://apewisdom.io/api/v1.0/filter/all-stocks/page/2"
ST_URL = "https://api.stocktwits.com/api/2/trending/symbols.json"


def et(*a: int) -> dt.datetime:
    return dt.datetime(*a, tzinfo=ET)


def _settings(**kw: Any) -> ArcSettings:
    return ArcSettings(_env_file=None, env="paper", **kw)  # type: ignore[call-arg]


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    c = connect(tmp_path / "arc.db")
    migrate(c)
    return c


def _info(sym: str, **kw: Any) -> SymbolInfo:
    base: dict[str, Any] = {"exchange": "NYSE", "tradable": True, "options": True}
    return SymbolInfo(symbol=sym, **(base | kw))


MASTER_SYMS = [
    "NVDA", "AAPL", "MU", "SPY", "QQQ", "NKE", "TSM", "LULU", "VST", "RKLB", "OKLO",
    "PCVX", "WULF", "TLT", "NOW", "ET", "RSI", "SA", "TD", "CVX", "GE", "DIS", "XLE",
    "SNOW", "MCD",
]  # fmt: skip


def _master(*extra: SymbolInfo) -> SymbolMaster:
    syms = {s: _info(s) for s in MASTER_SYMS}
    syms |= {i.symbol: i for i in extra}
    return SymbolMaster(fetched_at=NOW, symbols=syms)


# -- extraction ------------------------------------------------------------------


class TestExtraction:
    CFG = load_universe_config().extraction
    ACCEPTED = frozenset({"NOW", "ET", "RSI", "SA", "TD", "COLA", "MSCI", "NVDA", "MU", "AI"})

    def test_rsi_et_now_are_words_in_a_bare_form(self) -> None:
        text = (
            "The RSI is 70 and NOW is the time; earnings at 4pm ET. SA says TD Bank "
            "beat. MSCI rebalance, COLA up 2.8%."
        )
        assert extract_tickers(text, self.ACCEPTED, self.CFG) == []

    def test_labelled_and_cashtag_forms_still_match(self) -> None:
        text = "ServiceNow (NYSE: NOW) and $et rallied; Energy Transfer (ET), ticker symbol TD."
        assert extract_tickers(text, self.ACCEPTED, self.CFG) == ["NOW", "ET", "TD"]

    def test_paren_stop_words(self) -> None:
        text = "artificial intelligence (AI), Relative Strength Index (RSI), $AI up"
        assert extract_tickers(text, self.ACCEPTED, self.CFG) == ["AI"]

    def test_bare_min_len_and_allow_list(self) -> None:
        cfg = ExtractionConfig(min_symbol_len=2, bare_min_len=4)
        assert extract_tickers("MU and NVDA up", {"MU", "NVDA"}, cfg) == ["NVDA"]
        assert extract_tickers("MU and NVDA up", {"MU", "NVDA"}, cfg, bare_allow={"MU"}) == [
            "MU",
            "NVDA",
        ]
        # bare_min_len 1 = pre-E12.3 behaviour
        old = ExtractionConfig(min_symbol_len=2, bare_min_len=1)
        assert extract_tickers("MU and NVDA up", {"MU", "NVDA"}, old) == ["MU", "NVDA"]

    def test_shipped_stop_words(self) -> None:
        stop = set(self.CFG.stop_words)
        assert {"RSI", "ET", "SA", "MLP", "TD", "RBC", "COLA", "MSCI", "NOW", "PFD"} <= stop
        assert self.CFG.bare_min_len == 4
        assert {"AI", "COLA", "RSI"} <= set(self.CFG.paren_stop_words)

    def test_ingest_universe_short_trending_name_is_not_a_word(self) -> None:
        """A 3-letter active name outside core/momentum (NOW as trending) must not
        match every bare "now" via the legacy seed rule."""
        cfg = load_universe_config()
        u = IngestUniverse(
            mode=UniverseMode.SEED,
            seed=("NVDA", "MU", "NOW"),
            config=cfg,
            master=_master(),
            reference=("SPY",),
            bare_allow=("NVDA", "MU"),
        )
        assert u.tickers_in("Buy now: NVDA and MU; SPY flat") == ["NVDA", "MU", "SPY"]
        assert u.tickers_in("ServiceNow ($NOW) beat") == ["NOW"]
        assert "NOW" not in u.mention_universe("now is the time")
        # pre-E12.3 constructor (no bare_allow): every seed keeps the legacy rule
        legacy = IngestUniverse(mode=UniverseMode.SEED, seed=("NOW",), config=cfg, master=None)
        assert legacy.tickers_in("now") == ["NOW"]


# -- pure scoring ----------------------------------------------------------------


class TestRankNormalise:
    def test_basic_and_ties(self) -> None:
        assert rank_normalise({}) == {}
        assert rank_normalise({"A": 5}) == {"A": 1.0}
        assert rank_normalise({"A": 3, "B": 2, "C": 1, "D": 0}) == {
            "A": 1.0,
            "B": 0.75,
            "C": 0.5,
            "D": 0.25,
        }
        tied = rank_normalise({"A": 2, "B": 2, "C": 1})
        assert tied["A"] == tied["B"] == pytest.approx(5 / 6, abs=1e-6)
        assert tied["C"] == pytest.approx(1 / 3, abs=1e-6)

    @given(st.dictionaries(st.from_regex(r"[A-Z]{1,4}", fullmatch=True), st.floats(-1e6, 1e6)))
    def test_properties(self, raw: dict[str, float]) -> None:
        out = rank_normalise(raw)
        assert set(out) == set(raw)
        assert all(0 < v <= 1 for v in out.values())
        if raw:
            top = max(raw.values())
            assert (max(out.values()) == 1.0) is (sum(v == top for v in raw.values()) == 1)
        for a in raw:
            for b in raw:
                if raw[a] > raw[b]:
                    assert out[a] > out[b]
                if raw[a] == raw[b]:
                    assert out[a] == out[b]


def _res(name: str, raw: dict[str, float], status: str = "ok") -> InputResult:
    return InputResult(name=name, type="news", status=status, raw=raw)  # type: ignore[arg-type]


class TestRankTrending:
    def test_missing_input_is_not_renormalised(self) -> None:
        a = _res("a", {"X": 2, "Y": 1})
        b = _res("b", {"X": 2, "Y": 1})
        failed = _res("c", {}, status="failed")
        ranked, _, _ = rank_trending([a, b, failed], enabled_count=3, exclude={}, min_inputs=2)
        x = next(r for r in ranked if r.ticker == "X")
        assert x.trend_score == pytest.approx(2 / 3, abs=1e-6)  # (1 + 1) / 3, not / 2
        ranked2, _, _ = rank_trending([a, b], enabled_count=2, exclude={}, min_inputs=2)
        assert ranked2[0].trend_score == 1.0

    def test_two_input_rule_and_order(self) -> None:
        a = _res("a", {"X": 3, "Y": 2, "SOLO": 10})
        b = _res("b", {"X": 1, "Y": 2})
        ranked, excluded, single = rank_trending([a, b], enabled_count=2, exclude={}, min_inputs=2)
        assert [r.ticker for r in single] == ["SOLO"]
        assert single[0].n_inputs == 1 and excluded == []
        assert [r.ticker for r in ranked] == ["Y", "X"]  # Y: (.67+1)/2 > X: (1+.5)/2

    def test_ties_more_inputs_then_ticker(self) -> None:
        rows = [
            TrendingRow(ticker="B", trend_score=0.5, n_inputs=2),
            TrendingRow(ticker="A", trend_score=0.5, n_inputs=2),
            TrendingRow(ticker="C", trend_score=0.5, n_inputs=3),
        ]
        from arc.universe.trending import _order_key

        assert [r.ticker for r in sorted(rows, key=_order_key)] == ["C", "A", "B"]

    def test_core_momentum_excluded_before_the_cut(self) -> None:
        a = _res("a", {"NVDA": 9, "MU": 8, "NKE": 7, "TSM": 6})
        b = _res("b", {"NVDA": 9, "MU": 8, "NKE": 7, "TSM": 6})
        ranked, excluded, _ = rank_trending(
            [a, b], enabled_count=2, exclude={"NVDA": "core", "MU": "momentum"}, min_inputs=2
        )
        assert [r.ticker for r in ranked] == ["NKE", "TSM"]
        assert {r.ticker: r.excluded for r in excluded} == {"NVDA": "core", "MU": "momentum"}


def _screen(fails: set[str]) -> Callable[[str], ScreenResult]:
    def screen(sym: str) -> ScreenResult:
        m = LiquidityMetrics(ticker=sym, as_of=NOW.date())
        bad = sym in fails
        return ScreenResult(
            ticker=sym, passed=not bad, failures=["OI 5 < 150"] if bad else [], metrics=m
        )

    return screen


class TestScreenPool:
    ROWS = [
        TrendingRow(ticker=t, trend_score=1 - i / 10, n_inputs=2) for i, t in enumerate("ABCDE")
    ]

    def test_first_passes_in_score_order(self) -> None:
        pool = screen_pool(self.ROWS, pool=4, size=2, screen=_screen({"A", "C"}))
        assert [r.ticker for r in pool] == ["A", "B", "C", "D"]  # E outside the pool
        assert [(r.ticker, r.rank) for r in pool if r.admitted] == [("B", 1), ("D", 2)]
        assert [r.ticker for r in pool if r.screen_passed is False] == ["A", "C"]

    def test_size_cap_and_no_screen(self) -> None:
        pool = screen_pool(self.ROWS, pool=5, size=3, screen=None)
        assert [r.ticker for r in pool if r.admitted] == ["A", "B", "C"]
        assert all(r.screen_passed is None for r in pool)


def test_eligible() -> None:
    m = _master(
        _info("NOOPT", options=False),
        _info("HALT", tradable=False),
        _info("OTC", exchange="OTC"),
        _info("UNK", options=None),
    )
    assert eligible("NKE", m) is None
    assert eligible("ZZZZ", m) == "not in the symbol master"
    assert eligible("NOOPT", m) == "no listed options at the broker"
    assert eligible("UNK", m) == "no listed options at the broker"
    assert eligible("HALT", m) == "not tradable at the broker"
    assert eligible("OTC", m) == "not on a listed exchange (OTC)"


# -- config ----------------------------------------------------------------------


def _tcfg(**inputs: dict[str, Any]) -> TrendingConfig:
    return TrendingConfig.model_validate({"inputs": inputs})


class TestConfig:
    def test_validation(self) -> None:
        with pytest.raises(ValueError, match="needs urls"):
            TrendingInput(type="apewisdom")
        with pytest.raises(ValueError, match="takes no urls"):
            TrendingInput(type="news", urls=["x"])
        with pytest.raises(ValueError):
            TrendingInput.model_validate({"type": "alpaca_movers"})  # never a ranking input
        with pytest.raises(ValueError, match="lower-case"):
            _tcfg(**{"Bad-Name": {"type": "scout"}})
        assert TrendingInput(type="news", max_age="3d").max_age == dt.timedelta(days=3)
        with pytest.raises(TrendingError, match="missing `trending:`"):
            TrendingConfig.from_options({})

    def test_shipped(self) -> None:
        r = load_routines()
        kind, spec = r.jobs()["universe.trending"]
        assert kind.value == "source" and [t.strftime("%H:%M") for t in spec.schedule] == ["08:45"]
        assert str(r.context_policy("universe_tier", "universe.trending").ttl) == "1 session"
        cfg = TrendingConfig.from_options(spec.options)
        assert list(cfg.enabled) == ["news", "reddit", "stocktwits", "scout"]
        assert cfg.pool == 40 and cfg.min_inputs == 2
        assert cfg.inputs["reddit"].urls == [APE1, APE2]
        assert cfg.inputs["stocktwits"].urls == [ST_URL]
        assert "universe.trending" in BUILTIN_HANDLERS
        assert {
            ReasonCode.UNIVERSE_TRENDING_ADMITTED,
            ReasonCode.UNIVERSE_TRENDING_SCREEN_FAIL,
            ReasonCode.UNIVERSE_TRENDING_SINGLE_INPUT,
        } <= set(REASON_LABELS)

    def test_bad_trending_block_fails_config_load(self) -> None:
        raw = yaml.safe_load(
            textwrap.dedent(
                """
                sources:
                  universe.trending:
                    schedule: ["08:45"]
                    trending: {inputs: {x: {type: apewisdom}}}
                personas: {}
                """
            )
        )
        with pytest.raises(ValueError, match="needs urls"):
            RoutinesConfig.model_validate(raw)


# -- inputs ----------------------------------------------------------------------


def _ape_page(rows: list[tuple[str, int, int, int | None]]) -> bytes:
    return json.dumps(
        {
            "count": len(rows),
            "pages": 2,
            "results": [
                {"ticker": t, "mentions": m, "rank": r, "rank_24h_ago": p} for t, m, r, p in rows
            ],
        }
    ).encode()


APE_P1 = _ape_page([("NKE", 300, 1, 5), ("TSM", 200, 2, 2), ("LULU", 100, 3, None)])
APE_P2 = _ape_page([("VST", 50, 4, 40), ("NKE", 1, 99, 99)])
ST_BODY = json.dumps(
    {
        "symbols": [
            {"symbol": "TSM", "exchange": "NYSE", "region": "US", "trending_score": 9.0},
            {"symbol": "BTC.X", "exchange": "CRYPTO", "region": "US", "trending_score": 8.0},
            {"symbol": "LULU", "exchange": "NASDAQ", "region": "US", "trending_score": 5.0},
            {"symbol": "RY", "exchange": "TSX", "region": "CA", "trending_score": 4.0},
            {"symbol": "WULF", "exchange": "NASDAQ", "trending_score": None},
        ]
    }
).encode()


def _getter(pages: dict[str, bytes | Exception]) -> Callable[[str], bytes]:
    def get(url: str) -> bytes:
        page = pages[url]
        if isinstance(page, Exception):
            raise page
        return page

    return get


class TestNetworkInputs:
    APE = TrendingInput(type="apewisdom", urls=[APE1, APE2])
    ST = TrendingInput(type="stocktwits", urls=[ST_URL])

    def test_apewisdom(self) -> None:
        res = apewisdom_input(
            "reddit", self.APE, get=_getter({APE1: APE_P1, APE2: APE_P2}), now=NOW
        )
        assert res.status == "ok" and res.count == 5 and len(res.digest) == 64
        assert set(res.raw) == {"NKE", "TSM", "LULU", "VST"}  # NKE page-2 dup ignored
        # NKE: most mentions (1.0) + gain 4 (rank 2 of 4 -> .75); LULU: new = gain n+1-3
        assert res.raw["NKE"] == pytest.approx((1.0 + 0.75) / 2)
        assert res.detail["NKE"] == "reddit #1 (+4 24h)"
        assert res.detail["VST"] == "reddit #4 (+36 24h)"

    def test_apewisdom_any_page_failing_fails_the_input(self) -> None:
        down = requests.ConnectionError("down")
        res = apewisdom_input("reddit", self.APE, get=_getter({APE1: APE_P1, APE2: down}), now=NOW)
        assert res.status == "failed" and "ConnectionError" in (res.error or "")
        assert not res.live and res.scores() == {}

    def test_stocktwits_drops_crypto_and_non_us(self) -> None:
        res = stocktwits_input("stocktwits", self.ST, get=_getter({ST_URL: ST_BODY}), now=NOW)
        assert list(res.raw) == ["TSM", "LULU", "WULF"]
        assert set(res.dropped) == {"BTC.X", "RY"}
        assert res.detail["LULU"] == "stocktwits #3"
        bad = stocktwits_input("st", self.ST, get=_getter({ST_URL: b"<html>"}), now=NOW)
        assert bad.status == "failed"


def _doc(
    conn: sqlite3.Connection,
    i: int,
    text: str,
    at: dt.datetime,
    *,
    source: str = "rss",
    key: str | None = "cnbc",
    hints: list[str] | None = None,
) -> None:
    conn.execute(
        "INSERT INTO raw_docs (source, url, published_at, text, tickers_hint, content_hash, "
        "ingested_at, source_key) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            source,
            f"https://x/{i}",
            at.astimezone(dt.UTC).isoformat(),
            text,
            json.dumps(hints or []),
            f"h{i}",
            at.astimezone(dt.UTC).isoformat(),
            key,
        ),
    )
    conn.commit()


def _cand(conn: sqlite3.Connection, ticker: str, day: dt.date, corr: int) -> None:
    at = dt.datetime.combine(day, dt.time(15, 30), tzinfo=ET)
    conn.execute(
        "INSERT INTO candidates (id, ticker, stance, catalyst_type, confidence, sources, "
        "created_at, day, corroboration) VALUES (?, ?, 'bullish', 'news', 0.6, '[]', ?, ?, ?)",
        (f"{ticker}-{day}", ticker, at.isoformat(), day.isoformat(), corr),
    )
    conn.commit()


def _tickers_in(text: str) -> list[str]:
    return extract_tickers(
        text, frozenset(MASTER_SYMS), load_universe_config().extraction, bare_allow={"MU"}
    )


class TestStoreInputs:
    NEWS = TrendingInput(
        type="news", lookback_sessions=3, max_age="3d", exclude_sources=["earnings"]
    )
    SCOUT = TrendingInput(type="scout", lookback_sessions=3, max_age="3d")

    def test_news_distinct_sources_edgar_one_and_lookback(self, conn: sqlite3.Connection) -> None:
        _doc(conn, 1, "$NKE beats; LULU slips", et(2026, 10, 2, 10))
        _doc(conn, 2, "Nike (NKE) surges", et(2026, 10, 5, 7), key="wsj")
        _doc(conn, 3, "NKE again", et(2026, 10, 5, 7, 30), key="wsj")  # same source
        _doc(conn, 4, "RSI 70, ET close, NOW", et(2026, 10, 5, 7), key="seekingalpha")
        _doc(
            conn,
            5,
            "filing",
            et(2026, 10, 1, 9),
            source="edgar",
            key="edgar.x",
            hints=["NKE", "LULU"],
        )
        _doc(conn, 6, "filing", et(2026, 10, 1, 9), source="edgar", key="edgar.y", hints=["NKE"])
        _doc(conn, 7, "NKE old", et(2026, 9, 30, 15), key="nasdaq")  # before Oct 1
        _doc(conn, 8, "NKE", et(2026, 10, 2, 0), source="earnings", key=None)  # excluded
        _doc(conn, 9, "NKE future", et(2026, 10, 5, 9), key="fed")  # after now
        res = news_input(
            "news",
            self.NEWS,
            conn=conn,
            now=NOW,
            tickers_in=_tickers_in,
            key_for=lambda r: r["source_key"],
        )
        assert res.status == "ok"
        assert res.raw == {"NKE": 3.0, "LULU": 1.0}  # cnbc + wsj + edgar; RSI/ET/NOW none
        assert res.detail["NKE"] == "3 news sources" and res.detail["LULU"] == "1 news source"

    def test_news_stale(self, conn: sqlite3.Connection) -> None:
        spec = TrendingInput(type="news", lookback_sessions=10, max_age="1d")
        _doc(conn, 1, "NKE", et(2026, 9, 29, 10))
        res = news_input(
            "news", spec, conn=conn, now=NOW, tickers_in=_tickers_in, key_for=lambda r: "k"
        )
        assert res.status == "stale" and "older than" in (res.error or "")
        assert res.scores() == {}

    def test_scout_weighted_by_corroboration(self, conn: sqlite3.Connection) -> None:
        _cand(conn, "NKE", dt.date(2026, 10, 2), 3)
        _cand(conn, "NKE", dt.date(2026, 10, 1), 0)  # counts as 1
        _cand(conn, "TSM", dt.date(2026, 10, 2), 2)
        _cand(conn, "VST", dt.date(2026, 9, 30), 9)  # outside 3 sessions
        res = scout_input("scout", self.SCOUT, conn=conn, now=NOW)
        assert res.raw == {"NKE": 4.0, "TSM": 2.0}
        assert res.detail["NKE"] == "scout 2d (corr 4)"
        empty = scout_input("scout", self.SCOUT, conn=conn, now=et(2026, 10, 20, 8))
        assert empty.status in {"stale", "empty"} and not empty.live


# -- one run -----------------------------------------------------------------------


def _seed_store(conn: sqlite3.Connection) -> None:
    _doc(conn, 1, "$NKE beats; MU too; TSMC (TSM) capex", et(2026, 10, 2, 10))
    _doc(conn, 2, "Nike (NKE) and $TSM", et(2026, 10, 5, 7), key="wsj")
    _doc(conn, 3, "SPY (SPY) and OKLO", et(2026, 10, 5, 7), key="nasdaq")
    _cand(conn, "NKE", dt.date(2026, 10, 2), 3)
    _cand(conn, "MU", dt.date(2026, 10, 2), 3)
    _cand(conn, "PCVX", dt.date(2026, 10, 2), 1)


PAGES: dict[str, bytes | Exception] = {
    APE1: _ape_page(
        [("NKE", 300, 1, 5), ("TSM", 200, 2, 2), ("NVDA", 150, 3, 3), ("OKLO", 90, 4, 9)]
    ),
    APE2: _ape_page([("PCVX", 10, 50, 80), ("SPY", 80, 5, 5)]),
    ST_URL: json.dumps(
        {
            "symbols": [
                {"symbol": "TSM", "trending_score": 3},
                {"symbol": "WULF", "trending_score": 2},
            ]
        }
    ).encode(),
}


def _run(
    conn: sqlite3.Connection, pages: dict[str, bytes | Exception] | None = None, **kw: Any
) -> Any:
    cfg = TrendingConfig.from_options(load_routines().jobs()["universe.trending"][1].options)
    args: dict[str, Any] = {
        "conn": conn,
        "now": NOW,
        "master": _master(),
        "size": 25,
        "exclude": {"NVDA": "core", "MU": "momentum", "SPY": "market_reference"},
        "get": _getter(pages or PAGES),
        "tickers_in": _tickers_in,
        "key_for": lambda r: r["source_key"] or r["source"],
        "screen": _screen({"PCVX"}),
    }
    return run_trending(cfg, **(args | kw))


class TestRun:
    def test_end_to_end_pure(self, conn: sqlite3.Connection) -> None:
        _seed_store(conn)
        res = _run(conn)
        assert all(i.live for i in res.inputs)
        assert {r.ticker for r in res.excluded} == {"NVDA", "MU", "SPY"}
        # NKE: news+reddit+scout; TSM: news+reddit+stocktwits; OKLO: news+reddit;
        # PCVX: reddit+scout (fails the screen); WULF: stocktwits only
        assert [r.ticker for r in res.single_input] == ["WULF"]
        assert set(res.tickers[:2]) == {"TSM", "NKE"} and "OKLO" in res.tickers
        assert "PCVX" not in res.tickers
        nke = next(r for r in res.members if r.ticker == "NKE")
        assert nke.reason(res.order).startswith("2 news sources, reddit #1 (+4 24h), scout 1d")
        pay = build_payload(res, now=NOW)
        assert pay.tier is Tier.TRENDING and pay.partial is False
        src = {m.ticker: m.source for m in pay.members}
        assert src["TSM"] == "news+reddit+stocktwits" and src["NKE"] == "news+reddit+scout"
        assert "score" in pay.members[0].reason
        assert pay.source == "rules:news+reddit+stocktwits+scout"

    def test_one_failed_input_contributes_nothing(self, conn: sqlite3.Connection) -> None:
        _seed_store(conn)
        pages = PAGES | {ST_URL: requests.ConnectionError("down")}
        full, part = _run(conn), _run(conn, pages)
        assert part.failed_inputs == {"stocktwits": "failed: ConnectionError: down"}
        tsm_full = next(r for r in full.ranked if r.ticker == "TSM").trend_score
        tsm_part = next(r for r in part.ranked if r.ticker == "TSM").trend_score
        assert tsm_part < tsm_full  # still divided by 4 enabled inputs
        assert build_payload(part, now=NOW).partial is True
        assert "no data: stocktwits" in notice_line(part, None)

    def test_too_few_live_inputs_raises(self, conn: sqlite3.Connection) -> None:
        down = requests.ConnectionError("down")
        with pytest.raises(TrendingError, match="only 1 live trending inputs"):
            _run(conn, {APE1: down, APE2: down, ST_URL: PAGES[ST_URL]})
        with pytest.raises(TrendingError, match="symbol master unavailable"):
            _run(conn, master=None)

    def test_input_registry_is_config_only(self, conn: sqlite3.Connection) -> None:
        """Removing an input or adding a second one of a known type is YAML only."""
        _seed_store(conn)
        opts = load_routines().jobs()["universe.trending"][1].options["trending"]
        inputs = dict(opts["inputs"])
        inputs.pop("stocktwits")
        inputs["reddit_p1"] = {"type": "apewisdom", "urls": [APE1]}
        inputs["scout"] = {**inputs["scout"], "enabled": False}
        cfg = TrendingConfig.model_validate({**opts, "inputs": inputs})
        assert list(cfg.enabled) == ["news", "reddit", "reddit_p1"]
        res = run_trending(
            cfg,
            conn=conn,
            now=NOW,
            master=_master(),
            size=25,
            exclude={},
            get=_getter(PAGES),
            tickers_in=_tickers_in,
            key_for=lambda r: r["source_key"],
            screen=None,
        )
        assert [i.name for i in res.inputs] == ["news", "reddit", "reddit_p1"]
        nke = next(r for r in res.ranked if r.ticker == "NKE")
        assert set(nke.scores) == {"news", "reddit", "reddit_p1"}

    def test_notice_diff(self, conn: sqlite3.Connection) -> None:
        _seed_store(conn)
        res = _run(conn)
        assert notice_line(res, res.tickers).startswith(
            f"Trending tier: {len(res.tickers)} names (no change)"
        )
        line = notice_line(res, ["TSM", "LULU"])
        assert "+NKE" in line and "\u2212LULU" in line and "+TSM" not in line
        assert "1 failed the screen" in line


def test_journal_decisions_idempotent(conn: sqlite3.Connection) -> None:
    _seed_store(conn)
    res = _run(conn)
    first = journal_decisions(conn, res, at=NOW, run_id="r1")
    assert first == {
        "universe:trending_admitted": len(res.members),
        "universe:trending_screen_fail": 1,
        "universe:trending_single_input": 1,
    }
    assert journal_decisions(conn, res, at=NOW + dt.timedelta(hours=1)) == dict.fromkeys(first, 0)
    rows = conn.execute(
        "SELECT subject, choice, reason_text, payload FROM decisions "
        "WHERE reason_code = 'universe:trending_screen_fail'"
    ).fetchall()
    assert rows[0][0] == "PCVX" and rows[0][1] == "rejected" and "OI 5 < 150" in rows[0][2]
    assert json.loads(rows[0][3])["tier"] == "trending"


# -- handler + dispatcher + CLI ------------------------------------------------------


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch) -> dict[str, bytes | Exception]:
    import arc.routines.handlers as h
    from arc.universe.guard import UniverseGuard

    pages = dict(PAGES)
    monkeypatch.setattr(h, "trending_get", lambda cfg, ua: _getter(pages))
    monkeypatch.setattr("arc.universe.load_symbol_master", lambda *a, **k: _master())
    fake = _screen({"PCVX"})
    monkeypatch.setattr(UniverseGuard, "screen", lambda self, sym, profile=None: fake(sym))
    return pages


def _disp(conn: sqlite3.Connection, notifier: RecordingNotifier) -> Dispatcher:
    from arc.routines.handlers import universe_trending_source

    return Dispatcher(
        conn,
        load_routines(),
        handlers={"universe.trending": universe_trending_source},
        notifier=notifier,
        is_halted=lambda: False,
    )


def test_handler_end_to_end(conn: sqlite3.Connection, wired: dict[str, bytes | Exception]) -> None:
    _seed_store(conn)
    notifier = RecordingNotifier()
    [out] = _disp(conn, notifier).run_job("universe.trending", NOW, reason="manual", now=NOW)
    assert out.status == "ok", out.summary
    assert out.metrics["inputs"] == dict.fromkeys(["news", "reddit", "stocktwits", "scout"], "ok")
    assert any("Trending tier:" in t and "first list" in t for _, t in notifier.posts)
    entry = ContextStore(conn).query(as_of=NOW, kinds=["universe_tier"], subjects=["trending"])[0]
    assert entry.expires_at is not None and entry.expires_at - NOW < dt.timedelta(days=1)
    pay = UniverseTierPayload.model_validate(entry.payload)
    assert [m.ticker for m in pay.members] == out.metrics["tickers"]
    manifest = json.loads(
        conn.execute(
            "SELECT payload FROM run_manifests WHERE run_id = ?", (out.run_id,)
        ).fetchone()[0]
    )
    names = {i["name"]: i for i in manifest["external_inputs"]}
    assert names["trending.reddit"]["source"] == f"{APE1} {APE2}"
    assert len(names["trending.reddit"]["digest"]) == 64
    active, _ = build_active(conn, _settings(), NOW)
    tiers = {m.ticker: m.tier for m in active.members}
    assert all(tiers.get(t) is Tier.TRENDING for t in out.metrics["tickers"])
    n_dec = conn.execute(
        "SELECT count(*) FROM decisions WHERE reason_code LIKE 'universe:trending_%'"
    ).fetchone()[0]
    assert n_dec == len(out.metrics["tickers"]) + 2
    # a failed run writes nothing; the entry expires after its session
    wired[APE1] = wired[APE2] = wired[ST_URL] = requests.ConnectionError("down")
    later = et(2026, 10, 6, 8, 45)
    [bad] = _disp(conn, notifier).run_job("universe.trending", later, reason="manual", now=later)
    assert bad.status == "failed" and "live trending inputs" in bad.summary
    assert (
        ContextStore(conn).query(as_of=later, kinds=["universe_tier"], subjects=["trending"]) == []
    )


def test_share_class_of_core_is_excluded(
    conn: sqlite3.Connection,
    wired: dict[str, bytes | Exception],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from arc.routines.handlers import run_trending_tier

    wired[APE1] = _ape_page([("GOOG", 300, 1, 5), ("NKE", 200, 2, 2)])
    st_rows = [{"symbol": "GOOG", "trending_score": 3}, {"symbol": "NKE", "trending_score": 2}]
    wired[ST_URL] = json.dumps({"symbols": st_rows}).encode()
    master = _master(_info("GOOG"), _info("GOOGL"))
    monkeypatch.setattr("arc.universe.load_symbol_master", lambda *a, **k: master)
    res = run_trending_tier(
        conn=conn,
        settings=_settings(),
        routines=load_routines(),
        options=load_routines().jobs()["universe.trending"][1].options,
        now=NOW,
        screen=False,
        get=_getter(wired),
    )
    assert {r.ticker: r.excluded for r in res.excluded}.get("GOOG") == "core"
    assert "GOOG" not in res.tickers and "NKE" in res.tickers


def test_cli_dry_run_and_job(
    tmp_path: Path,
    wired: dict[str, bytes | Exception],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from arc.cli import main

    monkeypatch.chdir(tmp_path)
    db = tmp_path / "arc.db"
    c = connect(db)
    migrate(c)
    _seed_store(c)
    c.close()
    monkeypatch.setattr("arc.universe.cli.now_et", lambda: NOW, raising=False)
    monkeypatch.setattr("arc.utils.calendar.now_et", lambda: NOW)
    assert main(["universe", "trending", "--dry-run", "--json", "--db", str(db)]) == 0
    raw_out = capsys.readouterr().out
    dry = json.loads(raw_out[raw_out.index("{") :])
    assert dry["written"] is False and dry["screened"] is True
    row = next(r for r in dry["table"] if r["ticker"] == "PCVX")
    assert row["screen"] == "fail" and row["admitted"] is False and row["reddit"] is not None
    assert main(["universe", "trending", "--dry-run", "--no-screen", "--db", str(db)]) == 0
    text = capsys.readouterr().out
    assert "ticker" in text and "news" in text and "screened: False" in text
    assert main(["universe", "trending", "--no-screen", "--db", str(db)]) == 2
    capsys.readouterr()
    assert main(["universe", "trending", "--json", "--db", str(db), "--no-slack"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["written"] is True and out["run"]["status"] == "ok" and out["names"] >= 2
