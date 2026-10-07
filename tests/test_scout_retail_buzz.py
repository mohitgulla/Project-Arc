"""E13.20 (D58): the Scout reads ``retail_buzz`` as context; the Scout card's trending fact.

- golden Scout prompt with and without a fresh ``retail_buzz`` entry;
- the retail-buzz view ranks like the trending ranker (both inputs first), drops crypto,
  caps at 15 and marks ``in trending tier`` from the tier only;
- a stale entry reads ``no info``; the category line counts n/4;
- the Scout card's ``Trending: n/25 (k both-source) · m in active list`` fact;
- the handler records ``inputs.retail_buzz`` (scout_read v2) and passes today's tier.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest import mock

import pytest

from arc.context.kinds import ScoutReadPayload
from arc.context.store import ContextStore
from arc.personas.schemas import ScoutOutput
from arc.personas.scout import (
    RETAIL_BUZZ_TOP,
    SCOUT_CATEGORIES,
    category_line,
    category_max_ages,
    retail_buzz_lines,
    retail_buzz_view,
    scout_input_from_context,
    validate_calls,
)
from arc.slack.digests import TrendingFact, scout_card
from arc.universe.tiers import Tier, TierMember, UniverseTierPayload
from tests import scout_prompt_golden as golden
from tests.test_scout_persona import (
    CHANNELS,
    NOW,
    REPLY,
    FakeLLM,
    _ctx,
    _guard,
    _routines,
    _seed,
    _settings,
    db,  # noqa: F401 - pytest fixture
)

if TYPE_CHECKING:
    import sqlite3

FIXTURES = Path(__file__).parent / "fixtures" / "scout"


@pytest.fixture(autouse=True)
def _stop_patches() -> Any:
    """``_guard`` starts a ``measure_liquidity`` patch: stop it so no later test sees it."""
    yield
    mock.patch.stopall()


def _write_buzz(conn: sqlite3.Connection, *, age: dt.timedelta = dt.timedelta(minutes=20)) -> None:
    at = NOW - age
    ContextStore(conn).write(
        kind="retail_buzz",
        subject="all",
        payload=golden.buzz().model_dump(mode="json"),
        produced_by="retail_buzz",
        ttl="3d",
        valid_from=at,
        now=at,
    )


def _write_trending(conn: sqlite3.Connection, names: list[tuple[str, int]]) -> None:
    at = NOW - dt.timedelta(minutes=10)
    ContextStore(conn).write(
        kind="universe_tier",
        subject=Tier.TRENDING.value,
        payload=UniverseTierPayload(
            tier=Tier.TRENDING,
            members=[
                TierMember(
                    ticker=t,
                    tier=Tier.TRENDING,
                    rank=i,
                    source="reddit+stocktwits",
                    as_of=NOW.date(),
                    inputs=n,
                )
                for i, (t, n) in enumerate(names, 1)
            ],
            fetched_at=at,
            source="retail_buzz:reddit+stocktwits",
        ),
        produced_by="universe.trending",
        ttl="1d",
        valid_from=at,
        now=at,
    )


def _inp(conn: sqlite3.Connection, trending: list[str] | None = None) -> Any:
    return scout_input_from_context(
        ContextStore(conn).snapshot(NOW),
        channels=CHANNELS,
        budget_chars=12_000,
        max_age=category_max_ages(_routines()),
        max_discovery=25,
        discovery_floor=0.6,
        trending=trending or [],
        now=NOW,
    )


# -- golden prompt ----------------------------------------------------------------------


def test_prompt_golden_with_retail_buzz() -> None:
    assert golden.prompt(with_buzz=True) == (FIXTURES / "prompt_with_retail_buzz.txt").read_text()


def test_prompt_golden_without_retail_buzz() -> None:
    assert golden.prompt(with_buzz=False) == (FIXTURES / "prompt_no_retail_buzz.txt").read_text()


# -- the view ---------------------------------------------------------------------------


def test_view_orders_both_inputs_first_and_drops_crypto() -> None:
    view = retail_buzz_view(golden.buzz().model_dump(), trending=["GME", "SOFI"])
    tickers = [n.ticker for n in view.names]
    assert tickers[:3] == ["RKLB", "GME", "SOFI"]  # both inputs first
    assert set(tickers[3:]) == {"TSLA", "ACHR", "OKLO"}
    assert "BTC.X" not in tickers  # crypto dropped like the ranker does
    by = {n.ticker: n for n in view.names}
    assert by["GME"].reddit_rank == 1 and by["GME"].stocktwits_rank == 3
    assert by["GME"].reddit_mentions == 1234.0
    assert by["OKLO"].reddit_rank is None and by["OKLO"].n_inputs == 1
    # "in trending tier" comes from the tier only, never from buzz alone
    assert {n.ticker for n in view.names if n.in_trending} == {"GME", "SOFI"}
    assert by["RKLB"].line().endswith("in trending tier n")
    assert view.inputs == {"reddit": "ok", "stocktwits": "ok"}


def test_view_caps_top_and_skips_a_failed_input() -> None:
    payload = golden.buzz().model_dump()
    payload["inputs"]["stocktwits"] = {
        **payload["inputs"]["stocktwits"],
        "status": "failed",
        "rows": [],
        "error": "HTTP 503",
    }
    view = retail_buzz_view(payload, top=2)
    assert len(view.names) == 2
    assert all(n.stocktwits_rank is None for n in view.names)
    assert view.inputs["stocktwits"] == "failed"
    lines = retail_buzz_lines(view)
    assert "Inputs: reddit ok, stocktwits failed." in lines[1]
    assert RETAIL_BUZZ_TOP == 15


def test_empty_view_and_absent_section() -> None:
    payload = golden.buzz().model_dump()
    for i in payload["inputs"].values():
        i["status"], i["rows"] = "failed", []
    assert retail_buzz_lines(retail_buzz_view(payload))[-1] == "- no names"
    assert retail_buzz_lines(None) == [
        "## Retail buzz (retail_buzz, Reddit + Stocktwits)",
        "- no info",
    ]


# -- from the store -------------------------------------------------------------------


def test_fresh_entry_is_read_and_marked(db: sqlite3.Connection) -> None:  # noqa: F811
    _seed(db)
    _write_buzz(db)
    db.commit()
    inp = _inp(db, trending=["RKLB"])
    assert inp.retail_buzz is not None
    assert inp.retail_buzz.as_of == "2026-10-08T05:40:00-04:00"
    assert [n.ticker for n in inp.retail_buzz.names if n.in_trending] == ["RKLB"]
    assert inp.categories_present == [c.value for c in SCOUT_CATEGORIES]
    assert category_line(inp).startswith("Categories: 4/4 present")


def test_stale_entry_reads_no_info(db: sqlite3.Connection) -> None:  # noqa: F811
    _seed(db)
    _write_buzz(db, age=dt.timedelta(hours=30))  # > retail_buzz max_age 24h
    db.commit()
    inp = _inp(db)
    assert inp.retail_buzz is None
    assert category_line(inp) == (
        "Categories: 3/4 present (youtube_macro, youtube_micro, options_slow; "
        "no fresh info: retail_buzz)"
    )


def test_category_line_counts_none(db: sqlite3.Connection) -> None:  # noqa: F811
    inp = _inp(db)
    assert category_line(inp) == (
        "Categories: 0/4 present (none; no fresh info: youtube_macro, youtube_micro, "
        "options_slow, retail_buzz)"
    )


# -- Slack card ---------------------------------------------------------------------------


def _read(**inputs: Any) -> ScoutReadPayload:
    out = ScoutOutput.model_validate(REPLY)
    calls, _ = validate_calls(out, frozenset({"youtube:stockedup", "youtube:arete"}))
    return ScoutReadPayload(
        as_of=NOW.isoformat(),
        session=NOW.date().isoformat(),
        regime="r",
        options_sentiment="o",
        ticker_calls=calls,
        inputs={
            "youtube_macro": {"present": 2, "configured": 2},
            "youtube_micro": {"present": 3, "configured": 3},
            "options_daily": "2026-10-05",
            "vx_curve": "2026-10-05",
            "vol_term": "2026-10-05",
            **inputs,
        },
        discovery=["A", "B", "C", "D", "E"],
        discovery_fill=5,
        prompt_sha="x",
        model="m",
    )


def test_card_trending_fact() -> None:
    fact = TrendingFact(names=23, size=25, both=11, active=17)
    assert fact.line() == "Trending: 23/25 (11 both-source) · 17 in active list"
    card = scout_card(
        read=_read(retail_buzz="2026-10-06T05:40:00-04:00"),
        max_discovery=25,
        min_discovery_alert=5,
        candidates=5,
        trending=fact,
    )
    text = json.dumps(card.blocks, ensure_ascii=False)
    assert "Trending: 23/25 (11 both-source) · 17 in active list" in text
    assert "No fresh input" not in text


def test_card_names_missing_retail_buzz_only_with_the_fact() -> None:
    fact = TrendingFact(names=0, size=25)
    text = json.dumps(
        scout_card(
            read=_read(), max_discovery=25, min_discovery_alert=5, candidates=5, trending=fact
        ).blocks,
        ensure_ascii=False,
    )
    assert "Trending: 0/25 (0 both-source) · 0 in active list" in text
    assert "No fresh input: retail_buzz" in text
    # a pre-E13.20 caller (no fact) renders exactly as before
    old = json.dumps(
        scout_card(read=_read(), max_discovery=25, min_discovery_alert=5, candidates=5).blocks
    )
    assert "Trending" not in old and "No fresh input" not in old


def test_scout_read_v1_row_still_loads() -> None:
    assert _read().inputs.retail_buzz is None


# -- handler ------------------------------------------------------------------------------


def test_handler_reads_buzz_and_reports_trending(db: sqlite3.Connection) -> None:  # noqa: F811
    _seed(db)
    _write_buzz(db)
    _write_trending(db, [("GME", 2), ("SOFI", 2), ("ACHR", 1)])
    db.commit()
    from arc.routines.handlers import scout_persona

    settings = _settings()
    llm = FakeLLM()
    res = scout_persona(_ctx(db, _routines(), settings), llm=llm, guard=_guard(db, settings))
    db.commit()
    prompt = llm.prompts[0]
    assert "## Retail buzz (as of 2026-10-08T05:40:00-04:00" in prompt
    assert "- GME · reddit #1/1,234 mentions · stocktwits #3 · in trending tier y" in prompt
    assert "- RKLB · reddit #2/640 mentions · stocktwits #1 · in trending tier n" in prompt
    assert "Categories: 4/4 present" in prompt
    read = ScoutReadPayload.model_validate(
        ContextStore(db)
        .snapshot(NOW + dt.timedelta(minutes=1))
        .latest("scout_read", "session")
        .payload
    )
    assert read.inputs.retail_buzz == "2026-10-08T05:40:00-04:00"
    assert res.metrics["retail_buzz"] is True and res.metrics["trending"] == 3
    text = json.dumps(res.card.blocks, ensure_ascii=False)
    assert "Trending: 3/25 (2 both-source) · 3 in active list" in text
    # the persisted prompt (persona_calls) carries the section
    stored = db.execute("SELECT prompt_text FROM persona_calls WHERE persona = 'scout'").fetchone()[
        0
    ]
    assert "## Retail buzz (as of" in stored


def test_handler_without_buzz_says_no_info(db: sqlite3.Connection) -> None:  # noqa: F811
    _seed(db)
    from arc.routines.handlers import scout_persona

    settings = _settings()
    llm = FakeLLM()
    res = scout_persona(_ctx(db, _routines(), settings), llm=llm, guard=_guard(db, settings))
    assert "## Retail buzz (retail_buzz, Reddit + Stocktwits)\n- no info" in llm.prompts[0]
    assert res.metrics["retail_buzz"] is False and res.metrics["trending"] == 0
    assert "Trending: 0/25 (0 both-source) · 0 in active list" in json.dumps(
        res.card.blocks, ensure_ascii=False
    )
