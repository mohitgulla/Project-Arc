"""E13.8 (D56/D54, D44): the compact Research prompt behind
``personas.research_compact_prompt``.

Pins: ``full`` (the control) is today's prompt byte for byte (the E12.5 golden plus a
recorded-inputs replay); ``compact`` on a snapshot at today's live counts (35
candidates, 37 regimes, 419 stories, 235 notes, 3 briefs, Finnhub facts, a 6-position
book) fits ``research_prompt_max_chars`` minus the 7,200-char E13.17 reserve with no
trimming; over budget the headlines go first, then the pool is cut to its top 40 by
confidence (journaled ``over_prompt_budget``); a replay rebuilds the compact prompt
from the recorded inputs.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

import pytest

from arc.config import ArcSettings
from arc.context.store import ContextEntry, ContextSnapshot, ContextStore
from arc.experiments.overlay import arm_config_data, load_spec
from arc.ingest.llm import FixtureScalpLLM
from arc.personas.builders import (
    COMPACT_MAX_HEADLINES,
    build_research_prompt,
    research_input_from_context,
    scout_read_block,
)
from arc.pipeline import FIXTURE_NOW, PipelineEnv
from arc.pipeline.research_pool import (
    EXIT_BLOCK_RESERVE_CHARS,
    POOL_BUDGET_CUT,
    build_idea_pool,
)
from arc.pipeline.steps import _fit_research_budget, _research_rules, build_prompt
from arc.routines.config import (
    DEFAULT_ROUTINES_PATH,
    ResearchCompactPromptSettings,
    RoutinesConfig,
    load_routines,
)
from arc.utils.calendar import ET
from tests import diversification_golden as dg
from tests import finnhub_golden as g
from tests.test_e59_research_portfolio import FIXTURES_DIR, _codes, _run, _shortlist
from tests.test_research_diversification import MAIN_RESEARCH_SHA

REPO = Path(__file__).resolve().parent.parent
NOW = dt.datetime(2026, 10, 6, 10, 40, tzinfo=ET)
LIMIT = 80_000 - EXIT_BLOCK_RESERVE_CHARS  # 72,800
CHANNELS = [
    {"category": "youtube_micro", "label": "StockedUp", "slug": "stockedup"},
    {"category": "youtube_macro", "label": "FX", "slug": "fxevolution"},
    {"category": "youtube_micro", "label": "TradeBrigade", "slug": "tradebrigade"},
    {"category": "youtube_micro", "label": "Arete", "slug": "arete"},
    {"category": "youtube_macro", "label": "Bravos", "slug": "bravos"},
]
_TICKERS = [f"T{chr(65 + i // 26)}{chr(65 + i % 26)}" for i in range(80)]


def _settings(**kw: Any) -> ArcSettings:
    return ArcSettings(_env_file=None, account_profile="margin", **kw)  # type: ignore[call-arg]


def _e(i: int, kind: str, subject: str, payload: dict[str, Any], age_h: float = 1.0) -> Any:
    t = NOW - dt.timedelta(hours=age_h)
    return ContextEntry(
        id=f"ctx-{kind}-{i:04d}",
        kind=kind,
        subject=subject,
        payload=payload,
        schema_version=1,
        produced_by="test",
        created_at=t,
        valid_from=t,
    )


def _regime(t: str, i: int) -> dict[str, Any]:
    probs = {"bear": 0.29, "bull": 0.23, "sideways": 0.47}
    return {
        "ticker": t,
        "as_of": "2026-10-06",
        "last_close": 100.0 + i,
        "regime": {
            "as_of": "2026-10-06",
            "current": "bull",
            "expected_duration": 3.3,
            "forecasts": [{"horizon": h, "probabilities": probs} for h in (1, 5, 20)],
            "lookback_days": 20,
            "n_transitions": 255,
            "stationary": probs,
            "step": 1,
            "stickiness": 0.7,
            "stickiness_by_state": probs,
            "trailing_return": 0.027,
            "transition_matrix": {k: probs for k in probs},
        },
        "vol": {"as_of": "2026-10-06", "hv20": 0.35, "hv60": 0.38, "iv": 0.37, "iv_rank": None},
        "warnings": ["vol: IV rank needs 120 obs, got 1"],
    }


def _story(i: int, cat: str) -> dict[str, Any]:
    return {
        "story_id": f"st-{i:04d}",
        "category": cat,
        "headline": f"Headline number {i} about a company announcement and guidance change",
        "summary": "A two-sentence summary of the story, long enough to look real. " * 3,
        "tickers": [_TICKERS[i % 40]],
        "last_published": (NOW - dt.timedelta(minutes=10 + i)).isoformat(),
        "first_published": (NOW - dt.timedelta(minutes=10 + i)).isoformat(),
        "evidence": [{"quote": "q" * 80, "url": f"https://example.com/{i}"}],
        "urls": [f"https://example.com/{i}"],
        "source_keys": ["seekingalpha"],
        "doc_ids": [f"d{i}"],
        "distinct_sources": 1,
        "mode": "llm",
        "catalyst_type": "news",
        "catalyst_date": None,
    }


def _brief(slug: str) -> dict[str, Any]:
    return {
        "channel_slug": slug,
        "applies_to_session": "2026-10-06",
        "brief_id": f"brief-{slug}",
        "calls": [
            {
                "ticker": _TICKERS[k],
                "stance": "bullish",
                "conviction": 0.5,
                "horizon": "swing",
                "instrument_hint": "none",
                "quote": "A long quote from the video about the setup and the level to watch. " * 2,
            }
            for k in range(8)
        ],
        "summary": "Brief summary of the video. " * 20,
    }


def _scout_read(n_calls: int = 20) -> dict[str, Any]:
    return {
        "as_of": "2026-10-06T08:00:00-04:00",
        "session": "2026-10-06",
        "regime": "Risk-on but narrowing; semis lead, small caps lag. " * 5,
        "options_sentiment": "Equity put/call 0.59, VIX term in contango, VVIX 85. " * 5,
        "themes": [f"Theme {k}: AI capex keeps broadening into power and memory" for k in range(8)],
        "risks": [f"Risk {k}: CPI Oct 14 and the FOMC on Oct 28" for k in range(6)],
        "ticker_calls": [
            {
                "ticker": _TICKERS[40 + k],
                "stance": "bullish",
                "confidence": 0.7,
                "horizon": "weeks",
                "origins": ["youtube:stockedup"],
                "thesis": "x",
                "catalyst_type": "news",
            }
            for k in range(n_calls)
        ],
        "inputs": {
            "youtube_macro": {"present": 1, "configured": 2},
            "youtube_micro": {"present": 2, "configured": 3},
        },
        "discovery": [],
        "discovery_fill": 0,
        "prompt_sha": "0" * 64,
        "model": "test",
    }


def live_size_snapshot(*, scalp: int = 35, scout: int = 20, stories: int = 419) -> ContextSnapshot:
    """A snapshot at today's live counts (2026-10-06 19:10Z research call)."""
    entries: list[Any] = []
    for k in range(scalp):
        t = _TICKERS[k]
        p = g._cand(t, 0.4 + (k % 40) / 100)
        p["sources"] = [f"https://example.com/{t.lower()}/{j}" for j in range(3)]
        entries.append(_e(k, "candidate", t, p))
    for k in range(scout):
        t = _TICKERS[40 + k]
        p = {**g._cand(t, 0.7), "sources": ["youtube:stockedup"], "feed": "scout"}
        p["origins"] = ["youtube:stockedup"]
        entries.append(_e(100 + k, "candidate", t, p))
    for k, t in enumerate([*_TICKERS[:scalp], "SPY", "QQQ"]):
        entries.append(_e(200 + k, "regime", t, _regime(t, k)))
    cats = ("market_news", "company_data")
    entries += [_e(300 + k, "story", f"st-{k}", _story(k, cats[k % 2])) for k in range(stories)]
    for k in range(235):
        note = {
            "persona": "research",
            "topic": "thesis",
            "title": f"{_TICKERS[k % 35]} bullish thesis",
            "body": "A long thesis note with evidence lines. " * 60,
            "about": [],
        }
        entries.append(_e(1000 + k, "note", _TICKERS[k % 35], note, age_h=1 + k / 10))
    for slug in ("stockedup", "fxevolution", "arete"):
        entries.append(_e(2000 + len(entries), "channel_brief", f"youtube.{slug}", _brief(slug)))
    entries.append(_e(3000, "scout_read", "market", _scout_read()))
    entries.append(
        _e(
            3001,
            "vol_term",
            "market",
            {"as_of": "2026-10-05", "structure": "contango", "vix": 15.5, "vix3m": 18.0},
        )
    )
    entries.append(
        _e(3002, "put_call", "market", {"as_of": "2026-10-05", "total": 0.83, "equity": 0.59})
    )
    for k, t in enumerate(_TICKERS[:15]):
        for j, (kind, p) in enumerate(g.finnhub_payloads(t)):
            entries.append(_e(4000 + 10 * k + j, kind, t, p, age_h=20))
    return ContextSnapshot(id="snap-live-size", as_of=NOW, entries=entries)


def _book(n: int = 6) -> str:
    pos = dg.PORTFOLIO_BLOCK.split("Open positions", 1)[1].split("\n- ")[1]
    lines = "\n- ".join(pos for _ in range(n))
    return dg.PORTFOLIO_BLOCK.split("- os-1", 1)[0] + "- " + lines + "\n\n" + "Flags: none."


def _inputs(snap: ContextSnapshot, *, merged: bool, compact: bool) -> dict[str, Any]:
    pool = build_idea_pool(snap, merged=merged, max_scout_only=20)
    cands = {i.ticker: i.stance for i in pool.items}
    out: dict[str, Any] = {
        "portfolio_summary": "6 open",
        "scan_date": "2026-10-06",
        "max_notes": 20,
        "portfolio_block": _book(),
        "recent_ideas": "\n".join(f"- {t} bullish (proposed 1d ago)" for t in _TICKERS[:8]),
        "entry_terms": None,
        "rules": _research_rules(cands, _settings(), compact=compact),
        "youtube_channels": CHANNELS,
        "categories": None,
        "ticker_facts": {"max_tickers": 15, "tickers": _TICKERS[:15], "max_chars": 300},
        "candidate_tickers": sorted(cands),
    }
    if merged or compact:
        out["idea_pool"] = [i.model_dump(mode="json") for i in pool.items]
    if merged:
        out["pool_merged"] = True
    if compact:
        out["compact"] = True
    return out


# ---------------------------------------------------------------------------
# full (the control) is unchanged
# ---------------------------------------------------------------------------


def test_full_mode_prompt_is_byte_identical_to_main() -> None:
    snap = g.snapshot(with_finnhub=False)
    assert dg.sha(dg.research_prompt(snap)) == MAIN_RESEARCH_SHA
    assert dg.sha(dg.research_prompt(snap, compact=False)) == MAIN_RESEARCH_SHA
    # every Scalp entry in the pool: restricting to it changes nothing
    tickers = [e.subject for e in snap.of_kind("candidate")]
    assert dg.sha(dg.research_prompt(snap, candidate_tickers=tickers)) == MAIN_RESEARCH_SHA


def test_full_prompt_today_is_over_the_budget_and_compact_fits() -> None:
    snap = live_size_snapshot()
    full = build_prompt("research", snap, _inputs(snap, merged=False, compact=False))
    compact = build_prompt("research", snap, _inputs(snap, merged=True, compact=True))
    assert len(full) > 140_000  # today's prompt (the live call was 174,805 chars)
    assert len(compact) <= LIMIT, len(compact)
    assert len(compact) < len(full) / 3


# ---------------------------------------------------------------------------
# compact layout
# ---------------------------------------------------------------------------


def test_compact_prompt_layout() -> None:
    snap = live_size_snapshot()
    p = build_prompt("research", snap, _inputs(snap, merged=True, compact=True))
    assert "### Idea pool (Scalp + Scout; one line per ticker, counted by code)" in p
    assert f"{_TICKERS[40]} · bullish · conf 0.70 · feeds scout · origins 1 · single" in p
    assert "### Scout's read (daily slow feed" in p and "Themes: Theme 0" in p
    assert "### Regime lines" in p and "TAA · bull (stick 0.70)" in p
    assert "Vol term (2026-10-05): contango" in p and "Put/call (2026-10-05): total 0.83" in p
    assert "YouTube micro briefs: 2/3 channels (missing: TradeBrigade)" in p
    assert "### Current portfolio (open book; deterministic, E5.9)" in p
    # no raw JSON blocks, no exclusion request
    assert '"candidates": [' not in p and '"transition_matrix"' not in p
    assert '"quote"' not in p and '"summary"' not in p
    assert "Tickers you do not rank need no reason; leave `excluded` empty." in p
    assert "every candidate is either ranked or excluded" not in p
    assert '"excluded"' not in p.split("## Output format", 1)[1].split("JSON Schema", 1)[0]
    # headlines: at most COMPACT_MAX_HEADLINES per news category
    news = p.split("Market news:", 1)[1].split("Company data:", 1)[0]
    assert news.count("\n- Headline number") == COMPACT_MAX_HEADLINES


def test_scalp_pool_compact_names_only_the_scalp() -> None:
    snap = live_size_snapshot()
    p = build_prompt("research", snap, _inputs(snap, merged=False, compact=True))
    assert "### Idea pool (Scalp; one line" in p
    assert f"{_TICKERS[40]} · " not in p.split("### Regime lines", 1)[0]


def test_scout_read_block_is_capped_and_empty_without_a_read() -> None:
    snap = live_size_snapshot()
    block = scout_read_block(snap, max_chars=200)
    assert len(block) <= 200 and block.endswith("…")
    assert scout_read_block(g.snapshot(with_finnhub=False)) == ""


# ---------------------------------------------------------------------------
# budget: headlines first, then the pool to its top 40
# ---------------------------------------------------------------------------


def _fit(snap: ContextSnapshot, max_chars: int) -> tuple[dict[str, Any], list[Any], str]:
    settings = _settings(research_prompt_max_chars=max_chars)
    inputs = _inputs(snap, merged=True, compact=True)
    pool = build_idea_pool(snap, merged=True, max_scout_only=20)
    out, cut = _fit_research_budget(
        snap,
        inputs,
        pool.items,
        settings,
        lambda c: _research_rules(c, settings, compact=True),
    )
    return out, cut, build_prompt("research", snap, out)


def test_under_budget_nothing_is_trimmed() -> None:
    snap = live_size_snapshot()
    out, cut, _ = _fit(snap, 80_000)
    assert cut == [] and "max_headlines" not in out


def test_over_budget_trims_headlines_first() -> None:
    snap = live_size_snapshot()
    base = len(build_prompt("research", snap, _inputs(snap, merged=True, compact=True)))
    no_heads = len(
        build_prompt(
            "research", snap, {**_inputs(snap, merged=True, compact=True), "max_headlines": 0}
        )
    )
    out, cut, p = _fit(snap, no_heads + EXIT_BLOCK_RESERVE_CHARS + 10)
    assert base > no_heads and out["max_headlines"] == 0 and cut == []
    assert "- Headline number" not in p


def test_still_over_budget_cuts_the_pool_to_its_top_40() -> None:
    snap = live_size_snapshot()
    out, cut, p = _fit(snap, 20_000)
    pool = build_idea_pool(snap, merged=True, max_scout_only=20)
    assert len(out["idea_pool"]) == POOL_BUDGET_CUT == 40
    assert len(cut) == len(pool.items) - 40
    kept_conf = min(i["confidence"] for i in out["idea_pool"])
    assert all(c.confidence <= kept_conf for c in cut)
    assert out["candidate_tickers"] == sorted(i["ticker"] for i in out["idea_pool"])
    assert all(c.ticker not in p.split("## Hard constraints", 1)[1] for c in cut)


# ---------------------------------------------------------------------------
# Replay + the research step
# ---------------------------------------------------------------------------


def test_replay_rebuilds_the_compact_prompt_from_recorded_inputs() -> None:
    snap = live_size_snapshot()
    out, _, p = _fit(snap, 20_000)
    recorded = json.loads(json.dumps(out))  # the persona_calls.prompt_inputs round trip
    assert build_prompt("research", snap, recorded) == p
    inp = research_input_from_context(
        snap,
        portfolio_summary="6 open",
        scan_date="2026-10-06",
        compact=True,
        idea_pool=out["idea_pool"],
    )
    assert inp.compact and inp.pool_block.count("\n") == 39
    assert build_research_prompt(inp).startswith("You are a persona in Project Arc")


def _compact_run() -> Any:
    from arc.ingest.scalp import load_fixture_docs
    from arc.pipeline.runner import open_db

    conn = open_db(":memory:", copy=False)
    load_fixture_docs(conn)
    at = FIXTURE_NOW - dt.timedelta(hours=2)
    ContextStore(conn).write(
        kind="scout_read",
        subject="market",
        payload=_scout_read(0) | {"as_of": at.isoformat()},
        produced_by="scout",
        ttl="24h",
        valid_from=at,
        now=at,
    )
    env = PipelineEnv.fixtures()
    env.llms["research"] = FixtureScalpLLM([(FIXTURES_DIR / "research.json").read_text()])
    routines = load_routines().model_copy(
        update={"research_compact_prompt": ResearchCompactPromptSettings(mode="compact")}
    )
    conn, _ = _run(_settings(), routines, env, conn=conn)
    return conn


def test_research_step_compact_records_and_replays() -> None:
    conn = _compact_run()
    row = conn.execute(
        "SELECT snapshot_id, prompt_inputs, prompt_text FROM persona_calls "
        "WHERE persona='research' ORDER BY rowid DESC"
    ).fetchone()
    inputs = json.loads(row["prompt_inputs"])
    assert inputs["compact"] is True and "pool_merged" not in inputs
    assert inputs["idea_pool"] and len(row["prompt_text"]) <= LIMIT
    assert "### Scout's read" in row["prompt_text"]
    snap = ContextStore(conn).load_snapshot(row["snapshot_id"])
    assert build_prompt("research", snap, inputs) == row["prompt_text"]
    assert _shortlist(conn)["pool_counts"]["scalp"] == len(inputs["idea_pool"])
    assert "over_prompt_budget" not in _codes(conn, "shortlist")


def test_xp6_draft_spec_turns_only_the_flag_on() -> None:
    spec = load_spec(REPO / "config" / "experiments" / "live" / "xp6_research_compact_prompt.yaml")
    assert spec.id == "XP-6" and spec.kind.value == "ab"
    assert spec.arms.treatment.overlay == {
        "routines": {"personas": {"research_compact_prompt": "compact"}}
    }
    treat = RoutinesConfig.model_validate(arm_config_data(spec, "treatment", "routines"))
    base = load_routines(DEFAULT_ROUTINES_PATH)
    assert treat.research_compact_prompt.compact
    assert (
        treat.model_copy(update={"research_compact_prompt": base.research_compact_prompt}) == base
    )


@pytest.mark.parametrize("bad", [19_999, 400_001])
def test_prompt_budget_is_bounded(bad: int) -> None:
    with pytest.raises(ValueError, match="research_prompt_max_chars"):
        _settings(research_prompt_max_chars=bad)
