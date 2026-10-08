"""E14.1 (D60): ``ticker_news`` source — Alpaca/Benzinga news + Finnhub fallback.

Covers pagination, symbol chunking, the cursor (advances only after a full walk),
URL dedupe against RSS, the fallback running only for tickers the primary failed,
``filtered`` docs (no scope ticker), the no-key / 403 / 429 outcomes, the config
block, and the D60 weights loaded from ``config/routines.yaml``.
"""

from __future__ import annotations

import datetime as dt
import json
import urllib.error
from typing import TYPE_CHECKING, Any

import pytest

from arc.config import ArcSettings
from arc.context.store import ContextStore
from arc.ingest.finnhub import FinnhubClient, MemoryRateLimiter
from arc.ingest.sources import SourceRegistry
from arc.ingest.store import FILTERED_STATUS, IngestCursorRepo, RawDocRepo
from arc.ingest.ticker_news import (
    cursor_key,
    fetch_ticker_news,
    parse_alpaca,
    parse_finnhub,
    source_key,
)
from arc.ingest.ticker_news_config import TickerNewsConfig
from arc.routines.config import DEFAULT_ROUTINES_PATH, RoutinesConfig, load_routines
from arc.routines.handlers import (
    BUILTIN_HANDLERS,
    JobContext,
    JobSkippedError,
    ticker_news_source,
)
from arc.store.db import connect
from arc.store.migrate import migrate
from arc.utils.calendar import ET

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Mapping
    from pathlib import Path

NOW = dt.datetime(2026, 10, 8, 10, 0, tzinfo=ET)
MAX_AGE = dt.timedelta(hours=12)
ALPACA = {"type": "alpaca_news", "label": "Benzinga", "chunk_size": 2, "page_limit": 2}
FINNHUB = {"type": "finnhub_company_news", "enabled_when": "primary_failed"}


def _cfg(alpaca: Mapping[str, Any] | None = None, **extra: Any) -> TickerNewsConfig:
    return TickerNewsConfig.from_options(
        {"inputs": {"alpaca": {**ALPACA, **(alpaca or {})}, "finnhub": FINNHUB, **extra}}
    )


def _art(n: int, syms: list[str], *, minutes_ago: int = 30) -> dict[str, Any]:
    at = (NOW - dt.timedelta(minutes=minutes_ago)).astimezone(dt.UTC)
    return {
        "id": n,
        "url": f"https://www.benzinga.com/news/{n}",
        "headline": f"Headline {n} &amp; more",
        "summary": f"<p>Summary {n}</p>",
        "created_at": at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "symbols": syms,
    }


class FakeAlpaca:
    """``get(url, params, timeout)``: pages keyed by (symbols, page_token)."""

    def __init__(
        self,
        pages: Mapping[Any, Any],
        *,
        status: Mapping[str, list[int]] | None = None,
    ) -> None:
        self.pages = dict(pages)
        self.status = {k: list(v) for k, v in (status or {}).items()}
        self.calls: list[dict[str, str]] = []

    def __call__(
        self, url: str, params: Mapping[str, str], timeout: float
    ) -> tuple[int, Mapping[str, str], Any]:
        self.calls.append(dict(params))
        syms = params["symbols"]
        codes = self.status.get(syms)
        if codes:
            code = codes.pop(0)
            if code != 200:  # noqa: PLR2004
                return code, {"Retry-After": "7"}, None
        body = self.pages.get((syms, params.get("page_token")))
        if body is None:
            return 200, {}, {"news": [], "next_page_token": None}
        return 200, {}, body


def _finnhub(rows: Mapping[str, Any], fail: Mapping[str, int] | None = None) -> FinnhubClient:
    fail = dict(fail or {})

    def get_json(url: str, timeout: float) -> Any:
        sym = url.split("symbol=")[1].split("&")[0]
        if sym in fail:
            raise urllib.error.HTTPError(url, fail[sym], "x", None, None)  # type: ignore[arg-type]
        return rows.get(sym, [])

    limiter = MemoryRateLimiter(sleep=lambda s: None)
    return FinnhubClient("k", limiter=limiter, get_json=get_json, sleep=lambda s: None)


@pytest.fixture
def db(tmp_path: Path) -> sqlite3.Connection:
    conn = connect(tmp_path / "arc.db")
    migrate(conn)
    return conn


def _fetch(db: sqlite3.Connection, scope: list[str], get: Any, **kw: Any) -> Any:
    return fetch_ticker_news(
        db,
        kw.pop("cfg", _cfg()),
        scope,
        now=NOW,
        max_age=MAX_AGE,
        alpaca_get=get,
        finnhub_client=kw.pop("finnhub", None),
        sleep=kw.pop("sleep", lambda s: None),
        **kw,
    )


def _rows(db: sqlite3.Connection) -> list[dict[str, Any]]:
    return [dict(r) for r in db.execute("SELECT * FROM raw_docs ORDER BY url").fetchall()]


# -- parsing -----------------------------------------------------------------------------


def test_parse_alpaca_cleans_and_normalises() -> None:
    body = {"news": [_art(1, ["aapl", "BRK-B"]), {"url": "", "created_at": "x"}]}
    (a,) = parse_alpaca(body)
    assert a.headline == "Headline 1 & more" and a.summary == "Summary 1"
    assert a.symbols == ("AAPL", "BRK.B")
    assert a.text == "Headline 1 & more\n\nSummary 1"
    assert a.created_at.tzinfo is not None


def test_parse_finnhub_uses_related_else_ticker() -> None:
    ts = int(NOW.timestamp())
    rows = [
        {"url": "https://x/1", "headline": "h", "summary": "", "datetime": ts, "related": ""},
        {"url": "https://x/2", "headline": "h2", "datetime": ts, "related": "MSFT,NVDA"},
        {"url": "", "datetime": ts},
    ]
    a, b = parse_finnhub(rows, "AAPL")
    assert a.symbols == ("AAPL",) and a.text == "h"
    assert b.symbols == ("MSFT", "NVDA")


# -- alpaca: chunking, pagination, cursor --------------------------------------------------


def test_chunks_symbols_and_walks_every_page(db: sqlite3.Connection) -> None:
    pages = {
        ("AAPL,MSFT", None): {"news": [_art(1, ["AAPL"])], "next_page_token": "p2"},
        ("AAPL,MSFT", "p2"): {"news": [_art(2, ["MSFT"])], "next_page_token": None},
        ("NVDA", None): {"news": [_art(3, ["NVDA", "AMD"])], "next_page_token": None},
    }
    get = FakeAlpaca(pages)
    out = _fetch(db, ["AAPL", "MSFT", "NVDA"], get)
    assert [c["symbols"] for c in get.calls] == ["AAPL,MSFT", "AAPL,MSFT", "NVDA"]
    assert get.calls[1]["page_token"] == "p2"
    assert all(c["include_content"] == "false" and c["limit"] == "2" for c in get.calls)
    assert all(c["end"] == NOW.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ") for c in get.calls)
    assert out.new["alpaca"] == 3 and out.inputs["finnhub"].status == "not_needed"
    rows = _rows(db)
    assert {r["source"] for r in rows} == {"ticker_news"}
    assert {r["source_key"] for r in rows} == {"ticker_news.alpaca"}
    nvda = next(r for r in rows if r["url"].endswith("/3"))
    assert json.loads(nvda["tickers_hint"]) == ["NVDA"]  # AMD is not in scope
    assert nvda["title"] == "Headline 3 & more"
    assert out.per_ticker == {"AAPL": 1, "MSFT": 1, "NVDA": 1}


def test_cursor_is_start_and_advances_to_newest(db: sqlite3.Connection) -> None:
    pages = {
        ("AAPL", None): {
            "news": [_art(1, ["AAPL"], minutes_ago=50), _art(2, ["AAPL"], minutes_ago=5)]
        }
    }
    get = FakeAlpaca(pages)
    _fetch(db, ["AAPL"], get)
    # first run: no cursor -> start = now - max_age
    assert get.calls[0]["start"] == (NOW - MAX_AGE).astimezone(dt.UTC).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    cur = IngestCursorRepo(db).get(cursor_key("alpaca"))
    newest = (NOW - dt.timedelta(minutes=5)).astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert cur == newest
    get2 = FakeAlpaca({})
    _fetch(db, ["AAPL"], get2)
    assert get2.calls[0]["start"] == newest  # second run starts at the cursor
    assert IngestCursorRepo(db).get(cursor_key("alpaca")) == newest  # nothing new: kept


def test_cursor_not_advanced_when_a_page_walk_fails(db: sqlite3.Connection) -> None:
    pages = {
        ("AAPL,MSFT", None): {"news": [_art(1, ["AAPL"])], "next_page_token": "p2"},
        ("NVDA", None): {"news": [_art(3, ["NVDA"])]},
    }
    # page 2 of the first chunk 500s -> chunk fails, but NVDA's chunk still stores
    get = FakeAlpaca(pages, status={"AAPL,MSFT": [200, 500]})
    out = _fetch(db, ["AAPL", "MSFT", "NVDA"], get)
    assert IngestCursorRepo(db).get(cursor_key("alpaca")) is None
    assert out.inputs["alpaca"].status == "partial"
    assert out.inputs["alpaca"].failed == {"AAPL": "error", "MSFT": "error"}
    assert out.new["alpaca"] == 1  # only the finished chunk's article


def test_page_cap_fails_the_chunk(db: sqlite3.Connection) -> None:
    pages = {("AAPL", None): {"news": [_art(1, ["AAPL"])], "next_page_token": "p2"}}
    pages[("AAPL", "p2")] = {"news": [], "next_page_token": "p2"}
    out = _fetch(db, ["AAPL"], FakeAlpaca(pages), cfg=_cfg({"max_pages": 2}))
    assert out.inputs["alpaca"].failed == {"AAPL": "error"}
    assert "page walk not finished" in out.inputs["alpaca"].errors[0]


# -- dedupe, filtered, stale -----------------------------------------------------------------


def test_url_already_stored_from_rss_is_dropped(db: sqlite3.Connection) -> None:
    art = _art(1, ["AAPL"])
    RawDocRepo(db).insert(
        source="rss", url=art["url"], published_at=NOW.isoformat(), text="t", source_key="sa"
    )
    pages = {("AAPL", None): {"news": [art, art, _art(2, ["AAPL"])]}}
    out = _fetch(db, ["AAPL"], FakeAlpaca(pages))
    assert out.new["alpaca"] == 1 and out.duplicates == 2
    assert [r["source"] for r in _rows(db)] == ["rss", "ticker_news"]


def test_same_story_twice_across_runs_is_stored_once(db: sqlite3.Connection) -> None:
    pages = {("AAPL", None): {"news": [_art(1, ["AAPL"])]}}
    _fetch(db, ["AAPL"], FakeAlpaca(pages))
    IngestCursorRepo(db).set(cursor_key("alpaca"), "2020-01-01T00:00:00Z")  # re-walk
    out = _fetch(db, ["AAPL"], FakeAlpaca(pages))
    assert out.new["alpaca"] == 0 and out.duplicates == 1 and len(_rows(db)) == 1


def test_no_scope_ticker_is_stored_filtered(db: sqlite3.Connection) -> None:
    pages = {("AAPL", None): {"news": [_art(1, ["ZZZZ"]), _art(2, ["AAPL"])]}}
    out = _fetch(db, ["AAPL"], FakeAlpaca(pages))
    assert out.filtered["alpaca"] == 1 and out.new["alpaca"] == 1
    assert len(out.docs) == 1
    filt = next(r for r in _rows(db) if r["url"].endswith("/1"))
    assert filt["scalp_status"] == FILTERED_STATUS and filt["scalped_at"] is not None
    assert json.loads(filt["tickers_hint"]) == []
    assert RawDocRepo(db).claim_filtered(run_id="s1") == {"ticker_news.alpaca": 1}


def test_older_than_max_age_is_not_stored(db: sqlite3.Connection) -> None:
    pages = {("AAPL", None): {"news": [_art(1, ["AAPL"], minutes_ago=13 * 60)]}}
    out = _fetch(db, ["AAPL"], FakeAlpaca(pages))
    assert out.stale == 1 and not _rows(db)


# -- fallback ----------------------------------------------------------------------------


def test_fallback_runs_only_for_tickers_the_primary_failed(db: sqlite3.Connection) -> None:
    ts = int((NOW - dt.timedelta(minutes=10)).timestamp())
    fh_rows = {
        "NVDA": [{"url": "https://fh/1", "headline": "n", "datetime": ts, "related": "NVDA"}],
        "AAPL": [{"url": "https://fh/2", "headline": "a", "datetime": ts, "related": "AAPL"}],
    }
    client = _finnhub(fh_rows)
    pages = {("AAPL,MSFT", None): {"news": [_art(1, ["AAPL"])]}}
    get = FakeAlpaca(pages, status={"NVDA": [403]})
    out = _fetch(db, ["AAPL", "MSFT", "NVDA"], get, finnhub=client)
    assert out.inputs["alpaca"].failed == {"NVDA": "forbidden"}
    assert out.inputs["finnhub"].tickers == ["NVDA"]
    assert client.endpoints == {"/company-news": 1}
    assert out.new == {"alpaca": 1, "finnhub": 1}
    assert out.unresolved == {}
    keys = {r["source_key"] for r in _rows(db)}
    assert keys == {source_key("alpaca"), source_key("finnhub")}


def test_fallback_not_called_when_primary_answers(db: sqlite3.Connection) -> None:
    client = _finnhub({})
    out = _fetch(db, ["AAPL"], FakeAlpaca({}), finnhub=client)
    assert client.calls == 0 and out.inputs["finnhub"].status == "not_needed"


def test_fallback_without_key_keeps_tickers_unresolved(db: sqlite3.Connection) -> None:
    out = _fetch(db, ["AAPL"], FakeAlpaca({}, status={"AAPL": [401]}), finnhub=None)
    assert out.inputs["finnhub"].status == "no_api_key"
    assert out.unresolved == {"AAPL": "no_api_key"}


def test_429_retries_once_after_retry_after(db: sqlite3.Connection) -> None:
    slept: list[float] = []
    pages = {("AAPL", None): {"news": [_art(1, ["AAPL"])]}}
    get = FakeAlpaca(pages, status={"AAPL": [429, 200]})
    out = _fetch(db, ["AAPL"], get, sleep=slept.append)
    assert slept == [7.0] and out.new["alpaca"] == 1
    get2 = FakeAlpaca(pages, status={"AAPL": [429, 429]})
    out2 = _fetch(db, ["AAPL"], get2, sleep=slept.append)
    assert out2.inputs["alpaca"].failed == {"AAPL": "rate_limited"} and len(get2.calls) == 2


def test_network_error_fails_the_chunk(db: sqlite3.Connection) -> None:
    def boom(url: str, params: Mapping[str, str], timeout: float) -> Any:
        raise ConnectionError("down")

    out = _fetch(db, ["AAPL"], boom)
    assert out.inputs["alpaca"].failed == {"AAPL": "error"}


# -- config ------------------------------------------------------------------------------


def test_config_validates_inputs() -> None:
    with pytest.raises(ValueError, match="enabled_when: always"):
        TickerNewsConfig.from_options({"inputs": {"f": FINNHUB}})
    with pytest.raises(ValueError, match="lower-case"):
        TickerNewsConfig.from_options({"inputs": {"Bad": ALPACA}})
    with pytest.raises(ValueError):
        TickerNewsConfig.from_options({"inputs": {"a": {**ALPACA, "chunk_size": 51}}})
    with pytest.raises(ValueError):
        TickerNewsConfig.from_options({})
    cfg = _cfg()
    assert list(cfg.primaries) == ["alpaca"] and list(cfg.fallbacks) == ["finnhub"]


def test_bad_inputs_block_fails_routines_load() -> None:
    raw = {
        "sources": {
            "ticker_news": {
                "every": "15m",
                "category": "company_data",
                "writes": ["raw_doc_ref"],
                "inputs": {"a": {"type": "nope"}},
            }
        }
    }
    with pytest.raises(ValueError):
        RoutinesConfig.model_validate(raw)


def test_shipped_yaml_job_and_d60_weights() -> None:
    routines = load_routines(DEFAULT_ROUTINES_PATH)
    spec = routines.sources["ticker_news"]
    assert spec.options["category"] == "company_data" and spec.options["feed"] == "scalp"
    assert spec.options["tickers"] == "active_list"
    assert spec.writes == ["raw_doc_ref"]
    assert BUILTIN_HANDLERS["ticker_news"].endswith(":ticker_news_source")
    cfg = TickerNewsConfig.from_options(spec.options)
    assert cfg.inputs["alpaca"].type == "alpaca_news"
    assert cfg.inputs["finnhub"].enabled_when == "primary_failed"
    reg = SourceRegistry.from_routines(routines)
    assert reg.sources["ticker_news.alpaca"].weight == 2
    assert reg.sources["ticker_news.alpaca"].category_key == "company_data"
    assert reg.sources["seekingalpha"].weight == 0.5
    assert reg.sources["nasdaq"].weight == 0.5
    assert reg.sources["nasdaq"].category_key == "market_news"
    assert reg.max_age_for("ticker_news.alpaca").duration == dt.timedelta(hours=12)
    # inside company_data, Benzinga outweighs SA four to one
    eff = reg.effective_weights()
    assert eff["ticker_news.alpaca"] == pytest.approx(4 * eff["seekingalpha"])
    # a stored doc resolves to its input's registry source
    assert reg.key_for({"source": "ticker_news", "source_key": "ticker_news.alpaca"}) == (
        "ticker_news.alpaca"
    )
    assert reg.spec_for("ticker_news.gone").category_key == "company_data"


# -- handler outcomes ------------------------------------------------------------------


def _ctx(db: sqlite3.Connection, **opts: Any) -> JobContext:
    routines = load_routines(DEFAULT_ROUTINES_PATH)
    kind, spec = routines.step("ticker_news")
    spec = type(spec).model_validate({**spec.model_dump(exclude_unset=True), **opts})
    return JobContext(
        job="ticker_news",
        kind=kind,
        spec=spec,
        run_id="r1",
        chain_run_id=None,
        scheduled_for=NOW,
        now=NOW,
        conn=db,
        snapshot=ContextStore(db).snapshot(NOW, kinds=[]),
        routines=routines,
        settings_factory=lambda: ArcSettings(_env_file=None, env="paper"),  # type: ignore[call-arg]
    )


def test_handler_ok_summary_metrics_and_refs(db: sqlite3.Connection) -> None:
    pages = {
        ("AAPL,MSFT", None): {"news": [_art(1, ["AAPL", "MSFT"]), _art(2, ["QQQQ"])]},
    }
    get = FakeAlpaca(pages)
    res = ticker_news_source(
        _ctx(db, tickers=["AAPL", "MSFT"]), alpaca_get=get, finnhub_client=None
    )
    assert res.summary == "1 new doc (alpaca 1, finnhub 0), 1 filtered · 2/2 names"
    assert res.metrics["per_ticker"] == {"AAPL": 1, "MSFT": 1}
    assert res.metrics["filtered"] == 1 and res.metrics["covered"] == 2
    assert res.metrics["inputs"] == {"alpaca": "ok", "finnhub": "not_needed"}
    refs = db.execute("SELECT COUNT(*) FROM context_entries WHERE kind='raw_doc_ref'").fetchone()
    assert refs[0] == 1


def test_handler_no_keys_is_skipped(db: sqlite3.Connection) -> None:
    with pytest.raises(JobSkippedError, match="no_api_key"):
        ticker_news_source(_ctx(db, tickers=["AAPL"]), alpaca_get=None, finnhub_client=None)


@pytest.mark.parametrize(("code", "reason"), [(403, "forbidden"), (401, "forbidden")])
def test_handler_forbidden_fails(db: sqlite3.Connection, code: int, reason: str) -> None:
    get = FakeAlpaca({}, status={"AAPL": [code]})
    with pytest.raises(RuntimeError, match=f"ticker_news {reason}"):
        ticker_news_source(_ctx(db, tickers=["AAPL"]), alpaca_get=get, finnhub_client=None)


def test_handler_rate_limited_fails(db: sqlite3.Connection) -> None:
    get = FakeAlpaca({}, status={"AAPL": [429, 429]})
    with pytest.raises(RuntimeError, match="ticker_news rate_limited"):
        ticker_news_source(
            _ctx(db, tickers=["AAPL"]), alpaca_get=get, finnhub_client=None, sleep=lambda s: None
        )


def test_handler_fallback_403_fails_forbidden(db: sqlite3.Connection) -> None:
    get = FakeAlpaca({}, status={"AAPL": [500]})
    client = _finnhub({}, fail={"AAPL": 403})
    with pytest.raises(RuntimeError, match="ticker_news forbidden"):
        ticker_news_source(_ctx(db, tickers=["AAPL"]), alpaca_get=get, finnhub_client=client)


def test_handler_partial_is_ok_with_failed_tickers(db: sqlite3.Connection) -> None:
    pages = {("AAPL", None): {"news": [_art(1, ["AAPL"])]}}
    get = FakeAlpaca(pages, status={"MSFT": [500]})
    res = ticker_news_source(
        _ctx(db, tickers=["AAPL", "MSFT"], inputs={"alpaca": {**ALPACA, "chunk_size": 1}}),
        alpaca_get=get,
        finnhub_client=None,
    )
    assert res.metrics["failed_tickers"] == {"MSFT": "error"}
    assert "1 failed (MSFT)" in res.summary


def test_handler_empty_scope_is_skipped(db: sqlite3.Connection) -> None:
    with pytest.raises(JobSkippedError, match="empty scope"):
        ticker_news_source(_ctx(db, tickers=[]), alpaca_get=FakeAlpaca({}), finnhub_client=None)
