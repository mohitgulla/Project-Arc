"""E14.6 (D60): Stocktwits per-ticker sentiment (``retail_sentiment``).

- parse: tagged / untagged / missing ``entities`` / non-basic values, cursor + watchers;
- ratio: ``min_tagged`` threshold (never a 0% / 100% reading off a few tags), window;
- fetch: pacing, ``pages: 2`` only under ``min_tagged``, the 429 partial (page 1 and
  page 2), a 404 skips one ticker, ``max_requests``;
- the config block (defaults, validation at load), the kind round-trip + schema;
- the handler (writes per ticker, summary, all-fail raises, empty scope skips);
- the flag (registry, default off, XP-12 draft) and the Scout / Research prompts with
  the flag on and off (off = byte-identical), plus the D31 loop digest.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
import yaml
from hypothesis import given
from hypothesis import strategies as st

from arc.context.kinds import KINDS, RetailSentimentPayload, validate_payload
from arc.context.store import ContextStore
from arc.ingest.retail_sentiment import (
    RateLimitedError,
    StreamMessage,
    StreamPage,
    fetch_retail_sentiment,
    parse_stream,
    sentiment_payload,
)
from arc.ingest.retail_sentiment_config import RetailSentimentConfig
from arc.personas.retail_sentiment import (
    fresh_sentiment,
    research_sentiment_facts,
    scout_block,
    scout_sentiment_lines,
    sentiment_digest,
    sentiment_fact,
    window_text,
)
from arc.routines.config import DEFAULT_ROUTINES_PATH, load_routines
from arc.store.db import connect
from arc.store.migrate import migrate
from arc.utils.calendar import ET
from tests import scout_prompt_golden as golden

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Sequence

REPO = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures"
NOW = dt.datetime(2026, 10, 8, 5, 45, tzinfo=ET)
URL = "https://api.stocktwits.com/api/2/streams/symbol/{ticker}.json"


def _msg(i: int, minute: int, sentiment: str | None = None, **extra: Any) -> dict[str, Any]:
    m: dict[str, Any] = {
        "id": 1000 - i,
        "body": f"$NVDA msg {i}",
        "created_at": f"2026-10-08T{9 + minute // 60:02d}:{minute % 60:02d}:00Z",
        **extra,
    }
    if sentiment is not None:
        m["entities"] = {"sentiment": {"basic": sentiment}}
    return m


def _page(
    tags: Sequence[str | None],
    *,
    start: int = 0,
    step: int = 5,
    more: bool = True,
    watchers: int | None = 670680,
) -> dict[str, Any]:
    """Newest first: message i is ``step`` minutes older than i-1."""
    n = len(tags)
    msgs = [_msg(start + i, (n - i) * step, s) for i, s in enumerate(tags)]
    out: dict[str, Any] = {
        "messages": msgs,
        "cursor": {"more": more, "since": msgs[0]["id"] if msgs else None,
                   "max": msgs[-1]["id"] - 1 if msgs else None},
        "response": {"status": 200},
    }  # fmt: skip
    if watchers is not None:
        out["symbol"] = {"symbol": "NVDA", "watchlist_count": watchers}
    return out


def _cfg(**kw: Any) -> RetailSentimentConfig:
    return RetailSentimentConfig(**{"pace_s": 1.5, **kw})


# -- parse -------------------------------------------------------------------------------


def test_parse_tagged_untagged_and_missing_entities() -> None:
    data = {
        "symbol": {"symbol": "NVDA", "watchlist_count": "670680"},
        "cursor": {"more": True, "max": 666081242},
        "messages": [
            _msg(0, 50, "Bullish"),
            _msg(1, 40, "Bearish"),
            _msg(2, 30),  # no entities at all
            {**_msg(3, 20), "entities": {"sentiment": None}},  # entities, no sentiment
            {**_msg(4, 10), "entities": {"sentiment": {"basic": "Neutral"}}},  # not basic
            {**_msg(5, 5), "entities": "junk"},
            {"body": "no id"},  # dropped
            "not a dict",  # dropped
        ],
    }
    page = parse_stream(data)
    assert [m.sentiment for m in page.messages] == ["Bullish", "Bearish", None, None, None, None]
    assert page.more is True and page.max_cursor == 666081242
    assert page.watchlist_count == 670680
    assert page.messages[0].created_at == dt.datetime(2026, 10, 8, 9, 50, tzinfo=dt.UTC)


def test_parse_without_symbol_or_cursor_and_bad_bodies() -> None:
    page = parse_stream({"messages": [{"id": 1, "created_at": "garbage"}]})
    assert page.watchlist_count is None and page.max_cursor is None and page.more is False
    assert page.messages[0].created_at is None
    for bad in ({}, {"messages": None}, [], "x"):
        with pytest.raises(ValueError, match="no messages"):
            parse_stream(bad)


# -- ratio -------------------------------------------------------------------------------


def test_nvda_probe_shape() -> None:
    """The card's 2026-10-07 probe: 30 messages, 10 tagged (8 bull / 2 bear), ~2.7 h."""
    tags: list[str | None] = ["Bullish"] * 8 + ["Bearish"] * 2 + [None] * 20
    p = sentiment_payload([parse_stream(_page(tags, step=5))], min_tagged=5, as_of="t")
    assert (p.messages, p.tagged, p.bullish, p.bearish) == (30, 10, 8, 2)
    assert p.bull_ratio == 0.8
    assert p.window_minutes == 145.0  # 29 gaps x 5 min
    assert sentiment_fact(p.model_dump()) == "ST 80% bull (10 tagged, 2.4h)"
    assert p.watchlist_count == 670680


@pytest.mark.parametrize(
    ("tags", "min_tagged", "ratio"),
    [
        (["Bullish"] * 4, 5, None),  # 4 < 5: too few tags, never 100%
        (["Bearish"] * 4 + [None] * 26, 5, None),  # never 0%
        (["Bullish"] * 5, 5, 1.0),  # at the threshold
        (["Bearish"] * 5, 5, 0.0),
        (["Bullish", "Bearish", "Bearish"], 3, 0.3333),
        ([None] * 30, 5, None),
        ([], 5, None),
    ],
)
def test_ratio_threshold(tags: list[str | None], min_tagged: int, ratio: float | None) -> None:
    p = sentiment_payload([parse_stream(_page(tags))], min_tagged=min_tagged, as_of="t")
    assert p.bull_ratio == ratio
    assert p.min_tagged == min_tagged


def test_window_needs_two_timestamps_and_pages_dedupe() -> None:
    one = sentiment_payload([parse_stream(_page(["Bullish"]))], min_tagged=1, as_of="t")
    assert one.window_minutes is None and one.newest_at is not None
    none = sentiment_payload([], min_tagged=5, as_of="t")
    assert none.messages == 0 and none.newest_at is None and none.pages == 1
    a = StreamPage(messages=[StreamMessage(1, None, "Bullish"), StreamMessage(2, None, None)])
    b = StreamPage(messages=[StreamMessage(2, None, None), StreamMessage(3, None, "Bearish")])
    p = sentiment_payload([a, b], min_tagged=2, as_of="t")
    assert (p.messages, p.tagged, p.pages, p.bull_ratio) == (3, 2, 2, 0.5)


@given(
    bull=st.integers(0, 30),
    bear=st.integers(0, 30),
    untagged=st.integers(0, 30),
    floor=st.integers(1, 30),
)
def test_ratio_properties(bull: int, bear: int, untagged: int, floor: int) -> None:
    tags: list[str | None] = ["Bullish"] * bull + ["Bearish"] * bear + [None] * untagged
    p = sentiment_payload([parse_stream(_page(tags))], min_tagged=floor, as_of="t")
    assert p.tagged == bull + bear and p.messages == len(tags)
    if bull + bear < floor:
        assert p.bull_ratio is None
    else:
        assert p.bull_ratio is not None and 0.0 <= p.bull_ratio <= 1.0
        assert abs(p.bull_ratio - bull / (bull + bear)) < 1e-4


def test_payload_validator_rejects_inconsistent_counts() -> None:
    base = {"as_of": "t", "messages": 10, "tagged": 6, "bullish": 4, "bearish": 2,
            "bull_ratio": 0.6667, "min_tagged": 5}  # fmt: skip
    RetailSentimentPayload.model_validate(base)
    with pytest.raises(ValueError, match="bullish \\+ bearish"):
        RetailSentimentPayload.model_validate({**base, "bullish": 5})
    with pytest.raises(ValueError, match="bull_ratio is set iff"):
        RetailSentimentPayload.model_validate({**base, "bull_ratio": None})
    with pytest.raises(ValueError, match="bull_ratio is set iff"):
        RetailSentimentPayload.model_validate({**base, "min_tagged": 7})
    with pytest.raises(ValueError, match="Extra inputs"):
        RetailSentimentPayload.model_validate({**base, "score": 1})


# -- fetch -------------------------------------------------------------------------------


class _Get:
    def __init__(self, pages: dict[str, Any]) -> None:
        self.pages = pages
        self.urls: list[str] = []

    def __call__(self, url: str, timeout_s: float, retries: int) -> bytes:
        self.urls.append(url)
        v = self.pages.get(url)
        if isinstance(v, Exception):
            raise v
        if v is None:
            raise RuntimeError(f"404 Client Error for url: {url}")
        return json.dumps(v).encode()


def _u(t: str, cursor: int | None = None) -> str:
    return URL.format(ticker=t) + (f"?max={cursor}" if cursor is not None else "")


def test_fetch_paces_and_reads_one_page_by_default() -> None:
    get = _Get({_u(t): _page(["Bullish"] * 6) for t in ("NVDA", "AMD", "TSLA")})
    sleeps: list[float] = []
    res = fetch_retail_sentiment(_cfg(), ["NVDA", "AMD", "TSLA"], now=NOW, get=get,
                                 sleep=sleeps.append)  # fmt: skip
    assert sleeps == [1.5, 1.5]  # between requests, not before the first
    assert res.requests == 3 and list(res.readings) == ["NVDA", "AMD", "TSLA"]
    assert res.with_ratio == ["NVDA", "AMD", "TSLA"] and not res.partial
    assert res.readings["NVDA"].as_of == "2026-10-08T05:45:00-04:00"


def test_fetch_pages_two_only_under_min_tagged() -> None:
    p1 = _page(["Bullish", None, None])  # 1 tag < 5
    cursor = p1["cursor"]["max"]
    get = _Get({
        _u("LOW"): p1,
        _u("LOW", cursor): _page(["Bearish"] * 4, start=10),
        _u("HIGH"): _page(["Bullish"] * 6),
    })  # fmt: skip
    res = fetch_retail_sentiment(_cfg(pages=2), ["LOW", "HIGH"], now=NOW, get=get,
                                 sleep=lambda _: None)  # fmt: skip
    assert get.urls == [_u("LOW"), _u("LOW", cursor), _u("HIGH")]
    low = res.readings["LOW"]
    assert (low.pages, low.tagged, low.bull_ratio) == (2, 5, 0.2)
    assert res.readings["HIGH"].pages == 1


def test_fetch_no_second_page_when_stream_has_no_more() -> None:
    get = _Get({_u("X"): _page(["Bullish"], more=False)})
    res = fetch_retail_sentiment(_cfg(pages=2), ["X"], now=NOW, get=get, sleep=lambda _: None)
    assert res.requests == 1 and res.readings["X"].bull_ratio is None


def test_429_on_first_page_stops_the_run_and_keeps_what_was_fetched() -> None:
    get = _Get({_u("A"): _page(["Bullish"] * 5), _u("B"): RateLimitedError("429"),
                _u("C"): _page(["Bullish"] * 5)})  # fmt: skip
    res = fetch_retail_sentiment(_cfg(), ["A", "B", "C"], now=NOW, get=get, sleep=lambda _: None)
    assert res.rate_limited and res.partial
    assert list(res.readings) == ["A"] and res.not_reached == ["B", "C"]
    assert _u("C") not in get.urls


def test_429_on_second_page_keeps_the_first() -> None:
    p1 = _page(["Bullish"])
    get = _Get({_u("A"): p1, _u("A", p1["cursor"]["max"]): RateLimitedError("429"),
                _u("B"): _page(["Bullish"] * 5)})  # fmt: skip
    res = fetch_retail_sentiment(_cfg(pages=2), ["A", "B"], now=NOW, get=get,
                                 sleep=lambda _: None)  # fmt: skip
    assert res.rate_limited and list(res.readings) == ["A"] and res.not_reached == ["B"]
    assert res.readings["A"].pages == 1


def test_a_failing_ticker_is_skipped_not_fatal() -> None:
    get = _Get({_u("OK"): _page(["Bullish"] * 5), _u("BAD"): {"error": "x"}})
    res = fetch_retail_sentiment(_cfg(), ["NOPE", "BAD", "OK"], now=NOW, get=get,
                                 sleep=lambda _: None)  # fmt: skip
    assert list(res.readings) == ["OK"] and not res.partial
    assert set(res.skipped) == {"NOPE", "BAD"}
    assert res.skipped["NOPE"].startswith("RuntimeError: 404")


def test_max_requests_caps_the_run() -> None:
    get = _Get({_u(t): _page(["Bullish"] * 5) for t in "ABCD"})
    res = fetch_retail_sentiment(_cfg(max_requests=2), list("ABCD"), now=NOW, get=get,
                                 sleep=lambda _: None)  # fmt: skip
    assert res.capped and res.partial and not res.rate_limited
    assert list(res.readings) == ["A", "B"] and res.not_reached == ["C", "D"]


def test_wall_time_from_the_clock() -> None:
    ticks = iter([10.0, 52.4])
    res = fetch_retail_sentiment(_cfg(), [], now=NOW, get=_Get({}), sleep=lambda _: None,
                                 clock=lambda: next(ticks))  # fmt: skip
    assert res.wall_s == 42.4 and res.requests == 0


# -- config ------------------------------------------------------------------------------


def test_config_block_defaults_and_pacing() -> None:
    r = load_routines(DEFAULT_ROUTINES_PATH)
    spec = r.sources["retail_sentiment"]
    cfg = RetailSentimentConfig.from_options(spec.options)
    assert cfg.pace_s == 1.5 and cfg.pages == 1 and cfg.min_tagged == 5
    assert cfg.tickers == "active_list" and cfg.max_tickers == 55
    assert cfg.max_requests == 150 and cfg.url == URL
    # (55 tickers x <= 2 pages) <= max_requests; the paced run fits the 1h catch-up ttl
    assert cfg.max_tickers * cfg.pages <= cfg.max_requests
    assert [t.strftime("%H:%M") for t in spec.schedule] == ["05:45", "12:30"]
    assert spec.options["category"] == "retail_buzz" and spec.options["feed"] == "scout"
    assert spec.lane.value == "background" and spec.writes == ["retail_sentiment"]
    assert spec.context is not None and r.context_ttl.get("retail_sentiment") is None
    # before the 06:00 Scout
    scout = r.personas["scout"]
    assert spec.schedule[0] < scout.schedule[0]
    assert "retail_sentiment" in (scout.reads or [])


@pytest.mark.parametrize(
    ("bad", "match"),
    [
        ({"pace_s": -1}, "greater than or equal"),
        ({"pages": 3}, "less than or equal"),
        ({"min_tagged": 0}, "greater than or equal"),
        ({"url": "https://x/no-ticker"}, "must contain"),
        ({"tickers": "core"}, "active_list or a list"),
        ({"pase_s": 1}, "Extra inputs"),
    ],
)
def test_config_validation(bad: dict[str, Any], match: str) -> None:
    with pytest.raises(ValueError, match=match):
        RetailSentimentConfig.from_options(bad)


def test_bad_block_fails_config_load() -> None:
    raw = yaml.safe_load(DEFAULT_ROUTINES_PATH.read_text())
    raw["sources"]["retail_sentiment"]["pages"] = 9
    from arc.routines.config import RoutinesConfig

    with pytest.raises(ValueError, match="pages"):
        RoutinesConfig.model_validate(raw)


# -- kind --------------------------------------------------------------------------------


def test_kind_round_trip_and_schema() -> None:
    p = sentiment_payload([parse_stream(_page(["Bullish"] * 3 + ["Bearish"] * 2))],
                          min_tagged=5, as_of="2026-10-08T05:45:00-04:00")  # fmt: skip
    assert KINDS["retail_sentiment"].schema_version == 1
    back = validate_payload("retail_sentiment", p.model_dump(mode="json"))
    assert back == p
    schema = json.loads((REPO / "schemas/context/retail_sentiment.v1.json").read_text())
    assert schema["additionalProperties"] is False
    assert {"bull_ratio", "tagged", "window_minutes", "watchlist_count"} <= set(
        schema["properties"]
    )


# -- handler -----------------------------------------------------------------------------


@pytest.fixture
def db(tmp_path: Path) -> sqlite3.Connection:
    conn = connect(tmp_path / "arc.db")
    migrate(conn)
    return conn


def _ctx(conn: sqlite3.Connection, **options: Any) -> Any:
    from arc.config import ArcSettings
    from arc.routines.handlers import JobContext

    routines = load_routines(DEFAULT_ROUTINES_PATH)
    kind, spec = routines.step("retail_sentiment")
    if options:
        spec = type(spec).model_validate({**spec.model_dump(), **options})
    return JobContext(
        job="retail_sentiment",
        kind=kind,
        spec=spec,
        run_id="run-1",
        chain_run_id=None,
        scheduled_for=NOW,
        now=NOW,
        conn=conn,
        snapshot=ContextStore(conn).snapshot(NOW, kinds=[]),
        routines=routines,
        settings_factory=lambda: ArcSettings(_env_file=None),  # type: ignore[call-arg]
    )


def test_handler_writes_one_entry_per_ticker(db: sqlite3.Connection) -> None:
    from arc.routines.handlers import retail_sentiment_source

    get = _Get({_u("NVDA"): _page(["Bullish"] * 8 + ["Bearish"] * 2 + [None] * 20),
                _u("AMD"): _page(["Bullish"] * 2)})  # fmt: skip
    res = retail_sentiment_source(_ctx(db, tickers=["NVDA", "AMD", "ZZZZ"]), get=get,
                                  sleep=lambda _: None)  # fmt: skip
    assert res.summary.startswith("2 tickers · 1 with ratio · 1 skipped · 3 requests in ")
    assert res.metrics["with_ratio"] == 1 and res.metrics["partial"] is False
    rows = ContextStore(db).query(as_of=NOW + dt.timedelta(minutes=1), kinds=["retail_sentiment"])
    by = {e.subject: e.payload for e in rows}
    assert set(by) == {"NVDA", "AMD"}
    assert by["NVDA"]["bull_ratio"] == 0.8 and by["AMD"]["bull_ratio"] is None
    # 24h context ttl from the job
    e = next(x for x in rows if x.subject == "NVDA")
    assert e.expires_at is not None and e.expires_at - e.valid_from == dt.timedelta(hours=24)


def test_handler_partial_on_429(db: sqlite3.Connection) -> None:
    from arc.routines.handlers import retail_sentiment_source

    get = _Get({_u("A"): _page(["Bullish"] * 5), _u("B"): RateLimitedError("429")})
    res = retail_sentiment_source(_ctx(db, tickers=["A", "B", "C"]), get=get,
                                  sleep=lambda _: None)  # fmt: skip
    assert "partial (429; 2 not reached)" in res.summary
    assert res.metrics["rate_limited"] is True and res.metrics["not_reached"] == ["B", "C"]


def test_handler_fails_when_nothing_answers(db: sqlite3.Connection) -> None:
    from arc.routines.handlers import retail_sentiment_source

    with pytest.raises(RuntimeError, match="rate limited"):
        retail_sentiment_source(_ctx(db, tickers=["A"]), get=_Get({_u("A"): RateLimitedError("x")}),
                                sleep=lambda _: None)  # fmt: skip
    with pytest.raises(RuntimeError, match="no ticker answered"):
        retail_sentiment_source(_ctx(db, tickers=["A"]), get=_Get({}), sleep=lambda _: None)


def test_handler_skips_an_empty_scope(db: sqlite3.Connection) -> None:
    from arc.routines.handlers import JobSkippedError, retail_sentiment_source

    with pytest.raises(JobSkippedError, match="empty scope"):
        retail_sentiment_source(_ctx(db, tickers=[]), get=_Get({}), sleep=lambda _: None)


def test_handler_registered() -> None:
    from arc.routines.handlers import BUILTIN_HANDLERS

    assert BUILTIN_HANDLERS["retail_sentiment"].endswith(":retail_sentiment_source")


# -- flag / registry / experiment --------------------------------------------------------


def test_flag_registered_default_off() -> None:
    from arc.control.registry import lookup, read_raw

    raw = yaml.safe_load(DEFAULT_ROUTINES_PATH.read_text())
    f = lookup("personas.retail_sentiment_context")
    assert f.choices == ("off", "on") and read_raw(f, raw) == "off"
    assert lookup("retail_sentiment_context") is f
    assert load_routines().retail_sentiment_context.enabled is False
    on = load_routines(overrides={("personas", "retail_sentiment_context"): "on"})
    assert on.retail_sentiment_context.enabled is True
    assert on.retail_sentiment_context.scout_top == 15


def test_xp12_draft_is_the_flag_and_owns_the_scout() -> None:
    from arc.experiments.config import RunnerConfig
    from arc.experiments.models import ExperimentSpec
    from arc.experiments.runner import arm_owned_personas, arm_plan

    spec = ExperimentSpec.model_validate(
        yaml.safe_load((REPO / "config/experiments/live/xp12_retail_sentiment.yaml").read_text())
    )
    assert spec.id == "XP-12"
    ov = spec.arms.treatment.overlay
    assert ov == {"routines": {"personas": {"retail_sentiment_context": "on"}}}
    assert arm_owned_personas(ov) == {"scout"}
    routines = load_routines(overrides={("personas", "retail_sentiment_context"): "on"})
    plan = arm_plan(routines, ov, RunnerConfig())
    assert "retail_sentiment" in plan.shared_kinds  # the source runs once, in control


def test_never_a_gate_or_ranking_input() -> None:
    for path in [*REPO.joinpath("arc/gate").rglob("*.py"), REPO / "arc/universe/trending.py",
                 REPO / "arc/universe/tiers.py"]:  # fmt: skip
        assert "retail_sentiment" not in path.read_text(), path


# -- persona context ---------------------------------------------------------------------


def _reading(tagged: int, bull: int, window: float | None, *, msgs: int = 30) -> dict[str, Any]:
    return sentiment_payload(
        [StreamPage(messages=[
            StreamMessage(i, (dt.datetime(2026, 10, 8, 9, tzinfo=dt.UTC)
                              - dt.timedelta(minutes=(window or 0) * i / max(1, msgs - 1))),
                          "Bullish" if i < bull else ("Bearish" if i < tagged else None))
            for i in range(msgs)
        ])],
        min_tagged=5,
        as_of="2026-10-08T05:45:00-04:00",
    ).model_dump(mode="json")  # fmt: skip


def test_fact_text() -> None:
    assert sentiment_fact(_reading(10, 8, 162)) == "ST 80% bull (10 tagged, 2.7h)"
    assert sentiment_fact(_reading(3, 3, 45)) == "ST too few tags (3 tagged, 45m)"
    assert window_text(None) is None and window_text(59.6) == "60m" and window_text(60) == "1.0h"
    assert window_text(31067.3) == "21.6d" and window_text(2879) == "48.0h"
    assert sentiment_fact({"tagged": 0, "bull_ratio": None}) == "ST too few tags (0 tagged)"


def test_scout_lines_top_n_by_tagged() -> None:
    readings = {"AMD": _reading(6, 3, 300), "NVDA": _reading(10, 8, 162),
                "TSLA": _reading(3, 1, 20), "AAPL": _reading(6, 6, 90)}  # fmt: skip
    lines = scout_sentiment_lines(readings, top=3)
    assert lines == [
        "- NVDA · 80% bull (10 tagged, 2.7h) of 30 messages",
        "- AAPL · 100% bull (6 tagged, 1.5h) of 30 messages",
        "- AMD · 50% bull (6 tagged, 5.0h) of 30 messages",
    ]
    assert scout_block(None) == []
    assert scout_block([]) == ["", "## Retail sentiment (Stocktwits, latest messages per ticker; "
                               "context only)", "- no info"]  # fmt: skip
    assert "fewer than 5 tags = too few tags" in "\n".join(scout_block(lines, min_tagged=5))


def _write(conn: sqlite3.Connection, ticker: str, payload: dict[str, Any], at: dt.datetime) -> None:
    ContextStore(conn).write(
        kind="retail_sentiment",
        subject=ticker,
        payload=payload,
        produced_by="retail_sentiment",
        ttl="24h",
        valid_from=at,
        now=at,
    )


def test_fresh_sentiment_respects_max_age(db: sqlite3.Connection) -> None:
    from arc.context.ttl import Ttl

    _write(db, "NVDA", _reading(10, 8, 162), NOW - dt.timedelta(hours=2))
    _write(db, "AMD", _reading(6, 3, 300), NOW - dt.timedelta(hours=20))
    db.commit()
    snap = ContextStore(db).snapshot(NOW, kinds=["retail_sentiment"])
    assert set(fresh_sentiment(snap, as_of=NOW, max_age=Ttl.model_validate("24h"))) == {
        "NVDA",
        "AMD",
    }
    assert set(fresh_sentiment(snap, as_of=NOW, max_age=Ttl.model_validate("6h"))) == {"NVDA"}
    facts = research_sentiment_facts(fresh_sentiment(snap, as_of=NOW, max_age=None),
                                     ["NVDA", "TSLA"])  # fmt: skip
    assert facts == {"NVDA": "ST 80% bull (10 tagged, 2.7h)"}
    assert sentiment_digest({"NVDA": {"as_of": "x"}}, ["NVDA", "TSLA"]) == [
        "retail_sentiment:NVDA@x"
    ]


def test_scout_prompt_flag_off_is_byte_identical_and_on_adds_the_block(
    db: sqlite3.Connection,
) -> None:
    from arc.personas.scout import build_scout_prompt, category_max_ages, scout_input_from_context
    from arc.routines.config import load_routines as lr

    _write(db, "NVDA", _reading(10, 8, 162), NOW - dt.timedelta(minutes=5))
    _write(db, "TSLA", _reading(3, 1, 20), NOW - dt.timedelta(minutes=5))
    db.commit()
    snap = ContextStore(db).snapshot(NOW, kinds=["retail_sentiment", "retail_buzz"])
    kw: dict[str, Any] = dict(channels=[], budget_chars=1000,
                              max_age=category_max_ages(lr()), max_discovery=25,
                              discovery_floor=0.6, now=NOW)  # fmt: skip
    off = scout_input_from_context(snap, **kw)
    on = scout_input_from_context(snap, **kw, sentiment_top=15)
    assert off.retail_sentiment is None
    assert on.retail_sentiment == [
        "- NVDA · 80% bull (10 tagged, 2.7h) of 30 messages",
        "- TSLA · too few tags (3 tagged, 20m) of 30 messages",
    ]
    p_off, p_on = build_scout_prompt(off), build_scout_prompt(on)
    assert "Retail sentiment" not in p_off
    assert p_on.startswith(p_off.split("\n## Already in a higher tier")[0])
    assert "## Retail sentiment (Stocktwits" in p_on and "- NVDA · 80% bull" in p_on
    # the golden prompts (flag off) are unchanged
    assert (golden.prompt(with_buzz=True)
            == (FIXTURES / "scout" / "prompt_with_retail_buzz.txt").read_text())  # fmt: skip


def test_research_pool_line_fact() -> None:
    from arc.personas.builders import pool_line, pool_lines

    item = {"ticker": "NVDA", "stance": "bullish", "confidence": 0.72, "feeds": ["scalp"],
            "origins": 3, "agreement": "single", "tier": "core"}  # fmt: skip
    base = "NVDA · bullish · conf 0.72 · feeds scalp · origins 1 · single · tier core"
    assert pool_line({**item, "origins": 1}) == base
    assert pool_lines([{**item, "origins": 1}], sentiment=None) == base
    assert pool_lines([{**item, "origins": 1}], sentiment={"NVDA": "ST 80% bull (10 tagged, 2.7h)"}
                      ) == base + " · ST 80% bull (10 tagged, 2.7h)"  # fmt: skip


#: the tickers the fixture Scalp writes candidates for (tests/fixtures scalp reply)
FIXTURE_TICKERS = ("JPM", "NVDA", "PLTR", "SPY", "XOM")


def _settings() -> Any:
    from arc.config import ArcSettings

    return ArcSettings(_env_file=None, account_profile="margin")  # type: ignore[call-arg]


def _research(routines: Any) -> tuple[sqlite3.Connection, dict[str, Any], str]:
    from arc.ingest.scalp import load_fixture_docs
    from arc.pipeline import FIXTURE_NOW, PipelineEnv
    from arc.pipeline.runner import open_db
    from tests.test_e59_research_portfolio import _run

    conn = open_db(":memory:", copy=False)
    load_fixture_docs(conn)
    at = FIXTURE_NOW - dt.timedelta(hours=1)
    for t in FIXTURE_TICKERS:
        _write(conn, t, _reading(10, 8, 162), at)
    conn.commit()
    _run(_settings(), routines, PipelineEnv.fixtures(), conn=conn)
    row = conn.execute(
        "SELECT prompt_inputs, prompt_text FROM persona_calls "
        "WHERE persona='research' ORDER BY rowid DESC"
    ).fetchone()
    return conn, json.loads(row["prompt_inputs"]), row["prompt_text"]


@pytest.mark.parametrize("flag", ["off", "on"])
def test_research_prompt_with_the_flag(flag: str) -> None:
    from arc.context.store import ContextStore as CS
    from arc.pipeline.steps import build_prompt

    routines = load_routines(overrides={("personas", "retail_sentiment_context"): flag})
    conn, inputs, text = _research(routines)
    if flag == "off":
        assert "retail_sentiment" not in inputs and " ST " not in text
        assert "Stocktwits" not in text
    else:
        assert inputs["retail_sentiment"]
        assert all(
            v == "ST 80% bull (10 tagged, 2.7h)" for v in inputs["retail_sentiment"].values()
        )
        assert "· ST 80% bull (10 tagged, 2.7h)" in text
        assert "ST = Stocktwits bull %" in text
    # journal replay rebuilds the same prompt
    row = conn.execute(
        "SELECT snapshot_id FROM persona_calls WHERE persona='research' ORDER BY rowid DESC"
    ).fetchone()
    snap = CS(conn).load_snapshot(row["snapshot_id"])
    assert build_prompt("research", snap, inputs) == text


def test_loop_digest_includes_sentiment_only_with_keys() -> None:
    from arc.routines.loop import LoopInputs

    base: dict[str, Any] = dict(candidates=[], regimes=[], positions=[], pnl_bucket=0,
                                pending_orders=0, budget_tier="normal", suppressed=[])  # fmt: skip
    off = LoopInputs(**base)
    on = LoopInputs(**base, facts=["retail_sentiment:NVDA@2026-10-08T05:45:00-04:00"])
    later = LoopInputs(**base, facts=["retail_sentiment:NVDA@2026-10-08T12:30:00-04:00"])
    assert "facts" not in off.payload()
    assert len({off.digest(), on.digest(), later.digest()}) == 3


def test_tower_universe_row_carries_sentiment(tmp_path: Path) -> None:
    from arc.config import ArcSettings
    from arc.context.ttl import Ttl
    from arc.tower.data import connect_ro
    from arc.tower.data_universe import load_universe
    from arc.universe.tiers import Tier, TierMember, resolve_active

    day = NOW.date()
    p = tmp_path / "arc.db"
    c = connect(p)
    migrate(c)

    def m(t: str, rank: int) -> TierMember:
        return TierMember(ticker=t, tier=Tier.CORE, rank=rank, source="x", reason="r", as_of=day)

    active = resolve_active(core=[m("NVDA", 1), m("AMD", 2), m("TSLA", 3)], active_max=10,
                            as_of=day)  # fmt: skip
    ContextStore(c).write(kind="active_universe", subject="active", payload=active,
                          produced_by="t", ttl=Ttl(duration=dt.timedelta(hours=20)),
                          valid_from=NOW, now=NOW)  # fmt: skip
    old = NOW - dt.timedelta(days=2)  # expired: never shown
    _write(c, "TSLA", _reading(10, 1, 30), old)
    _write(c, "NVDA", _reading(6, 6, 30), NOW - dt.timedelta(hours=6))
    _write(c, "NVDA", _reading(10, 8, 162), NOW)  # newest wins
    c.commit()
    c.close()
    ro = connect_ro(p)
    try:
        r = load_universe(ro, ArcSettings(), now=NOW + dt.timedelta(minutes=5))
    finally:
        ro.close()
    by = {a.ticker: a.sentiment for a in r.active}
    assert by == {"NVDA": "ST 80% bull (10 tagged, 2.7h)", "AMD": None, "TSLA": None}
