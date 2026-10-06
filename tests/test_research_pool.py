"""E13.8 (D56/D53, D44): Research's idea pool behind ``personas.research_idea_pool``.

Pins: the pool rules (feeds from sources, origins, stance agreement, the Scout-only
cap), ``scalp`` mode = today's candidate set and prompt, ``all`` mode lets a
Scout-only idea through the ``not_a_candidate`` filter, the switch + registry +
strategy-lane plumbing, and the draft XP-4 spec.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from arc.config import ArcSettings
from arc.context.store import ContextEntry, ContextSnapshot, ContextStore
from arc.control.registry import REGISTRY, lookup, read_raw, write_raw
from arc.experiments.overlay import arm_config_data, load_spec
from arc.ingest.llm import FixtureScalpLLM
from arc.journal.reasons import REASON_LABELS, ReasonCode
from arc.models import Stance
from arc.personas.builders import pool_line
from arc.personas.schemas import ResearchOutput, ResearchRankedItem
from arc.pipeline import FIXTURE_NOW, PipelineEnv
from arc.pipeline.research_pool import (
    build_idea_pool,
    cut_pool,
    entry_feeds,
    scalp_entries,
)
from arc.pipeline.steps import DROP_NOT_CANDIDATE, RESEARCH_READS, _filter_shortlist
from arc.routines.config import (
    DEFAULT_ROUTINES_PATH,
    ResearchIdeaPoolSettings,
    RoutinesConfig,
    load_routines,
)
from arc.utils.calendar import ET
from tests.test_e59_research_portfolio import FIXTURES_DIR, _codes, _run, _shortlist

REPO = Path(__file__).resolve().parent.parent
NOW = dt.datetime(2026, 10, 6, 10, 15, tzinfo=ET)


def _settings() -> ArcSettings:
    return ArcSettings(_env_file=None, account_profile="margin")  # type: ignore[call-arg]


def _cand(
    t: str,
    stance: str = "bullish",
    conf: float = 0.6,
    *,
    sources: list[str] | None = None,
    feed: str = "scalp",
    origins: list[str] | None = None,
    corroboration: int | None = None,
) -> dict[str, Any]:
    return {
        "id": f"cand-{t.lower()}",
        "ticker": t,
        "stance": stance,
        "catalyst_type": "earnings",
        "catalyst_date": "2026-10-28T00:00:00-04:00",
        "confidence": conf,
        "sources": sources if sources is not None else [f"https://example.com/{t.lower()}"],
        "corroboration": corroboration,
        "created_at": "2026-10-06T09:00:00-04:00",
        "feed": feed,
        "origins": origins or [],
    }


def _entry(i: int, kind: str, subject: str, payload: dict[str, Any]) -> ContextEntry:
    t = NOW - dt.timedelta(hours=1)
    return ContextEntry(
        id=f"ctx-{i:03d}",
        kind=kind,
        subject=subject,
        payload=payload,
        schema_version=3,
        produced_by="test",
        created_at=t,
        valid_from=t,
    )


def _read(calls: list[tuple[str, str, float]]) -> dict[str, Any]:
    return {
        "as_of": "2026-10-06T08:00:00-04:00",
        "session": "2026-10-06",
        "regime": "Risk-on, breadth narrowing.",
        "options_sentiment": "Put/call 0.83, contango.",
        "themes": ["AI capex"],
        "risks": ["CPI Oct 14"],
        "ticker_calls": [
            {
                "ticker": t,
                "stance": s,
                "confidence": c,
                "horizon": "weeks",
                "origins": ["youtube:stockedup"],
                "thesis": "x",
                "catalyst_type": "news",
            }
            for t, s, c in calls
        ],
        "inputs": {},
        "discovery": [],
        "discovery_fill": 0,
        "prompt_sha": "0" * 64,
        "model": "test",
    }


def _snap(*payloads: dict[str, Any], read: dict[str, Any] | None = None) -> ContextSnapshot:
    entries = [_entry(i, "candidate", p["ticker"], p) for i, p in enumerate(payloads)]
    if read is not None:
        entries.append(_entry(99, "scout_read", "market", read))
    return ContextSnapshot(id="snap-pool", as_of=NOW, entries=entries)


YT = "youtube:stockedup"


# ---------------------------------------------------------------------------
# Pool rules (pure)
# ---------------------------------------------------------------------------


def test_feeds_come_from_sources_not_the_feed_field() -> None:
    assert entry_feeds(_cand("AAPL")) == {"scalp"}
    # a Scalp run re-wrote a Scout candidate as feed=scalp: its youtube source still says scout
    assert entry_feeds(_cand("TEM", sources=[YT], feed="scalp")) == {"scout"}
    assert entry_feeds(_cand("NVDA", sources=["https://x/1", YT])) == {"scalp", "scout"}
    assert entry_feeds(_cand("CRWD", sources=[], feed="scout", origins=[YT])) == {"scout"}
    assert entry_feeds(_cand("OLD", sources=[])) == {"scalp"}  # pre-E13.7 row


def test_scalp_mode_is_the_scalp_entries_and_all_of_them_before_the_scout() -> None:
    snap = _snap(_cand("AAPL"), _cand("NVDA"))
    assert [e.subject for e in scalp_entries(snap.of_kind("candidate"))] == ["AAPL", "NVDA"]
    pool = build_idea_pool(snap, merged=False, max_scout_only=20)
    assert sorted(pool.tickers) == ["AAPL", "NVDA"]
    mixed = _snap(_cand("AAPL"), _cand("TEM", sources=[YT], feed="scout", origins=[YT]))
    assert build_idea_pool(mixed, merged=False, max_scout_only=20).tickers == ["AAPL"]
    assert sorted(build_idea_pool(mixed, merged=True, max_scout_only=20).tickers) == [
        "AAPL",
        "TEM",
    ]


def test_merge_origins_and_agreement() -> None:
    snap = _snap(
        _cand("NVDA", conf=0.7, sources=["https://a/1", "https://b/2", YT], corroboration=2),
        _cand("AMD", conf=0.6, sources=["https://a/3", YT]),
        _cand("AAPL", conf=0.5),
        _cand("TEM", conf=0.8, sources=[YT, "youtube:arete"], feed="scout"),
        read=_read([("NVDA", "bullish", 0.65), ("AMD", "bearish", 0.9)]),
    )
    pool = {i.ticker: i for i in build_idea_pool(snap, merged=True, max_scout_only=20).items}
    nvda = pool["NVDA"]
    assert nvda.feeds == ["scalp", "scout"] and nvda.origins == 3  # 2 registry + 1 channel
    assert nvda.agreement == "agree" and nvda.confidence == 0.7  # stored stance, max conf
    amd = pool["AMD"]
    assert amd.agreement == "disagree" and amd.stance == Stance.BULLISH
    assert pool["AAPL"].agreement == "single" and pool["AAPL"].origins == 1
    tem = pool["TEM"]
    assert tem.feeds == ["scout"] and tem.origins == 2 and tem.agreement == "single"
    assert tem.candidate_ids == ["cand-tem"] and tem.catalyst_date == "2026-10-28"


def test_both_feeds_without_a_scout_read_count_as_agree() -> None:
    snap = _snap(_cand("NVDA", sources=["https://a/1", YT]))
    (item,) = build_idea_pool(snap, merged=True, max_scout_only=20).items
    assert item.agreement == "agree"


def test_scout_call_with_higher_confidence_lifts_the_pool_confidence() -> None:
    snap = _snap(
        _cand("NVDA", conf=0.5, sources=["https://a/1", YT]),
        read=_read([("NVDA", "bullish", 0.9)]),
    )
    (item,) = build_idea_pool(snap, merged=True, max_scout_only=20).items
    assert item.confidence == 0.9 and item.agreement == "disagree"  # merged value stale


def test_scout_only_cap_keeps_the_most_confident_and_reports_the_rest() -> None:
    scouts = [
        _cand(f"S{i}", conf=0.5 + i / 100, sources=[YT], feed="scout", origins=[YT])
        for i in range(5)
    ]
    snap = _snap(_cand("AAPL", conf=0.1), *scouts)
    pool = build_idea_pool(snap, merged=True, max_scout_only=2)
    assert pool.tickers == ["S4", "S3", "AAPL"]  # ordered by confidence
    assert [i.ticker for i in pool.capped] == ["S2", "S1", "S0"]
    assert pool.counts() == {"scalp": 1, "scout": 2, "both": 0, "scout_only_capped": 3}
    assert build_idea_pool(snap, merged=True, max_scout_only=0).tickers == ["AAPL"]


def test_cut_pool_orders_by_confidence_then_origins_then_ticker() -> None:
    snap = _snap(
        _cand("BBB", conf=0.6, sources=["https://a/1", "https://a/2"]),
        _cand("AAA", conf=0.6),
        _cand("CCC", conf=0.9),
    )
    items = build_idea_pool(snap, merged=True, max_scout_only=20).items
    kept, cut = cut_pool(items, 2)
    assert [i.ticker for i in kept] == ["CCC", "BBB"] and [i.ticker for i in cut] == ["AAA"]


def test_pool_line_is_code_built() -> None:
    snap = _snap(_cand("NVDA", conf=0.72, sources=["https://a/1", "https://b/2", YT]))
    (item,) = build_idea_pool(snap, merged=True, max_scout_only=20, tiers={"NVDA": "core"}).items
    assert pool_line(item.model_dump(mode="json")) == (
        "NVDA · bullish · conf 0.72 · feeds scalp+scout · origins 3 · agree · tier core"
        " · earnings 2026-10-28"
    )


def test_not_a_candidate_fires_only_outside_the_pool() -> None:
    def ranked(t: str) -> ResearchRankedItem:
        return ResearchRankedItem(
            ticker=t,
            rank=1,
            thesis="t",
            regime_context="r",
            suggested_structure_type="vertical_spread",
            stance="bullish",
            confidence=0.7,
        )

    out = ResearchOutput(
        shortlist=[ranked("TEM"), ranked("ZZZ")], market_regime="risk_on", session_notes=""
    )
    snap = _snap(_cand("AAPL"), _cand("TEM", sources=[YT], feed="scout", origins=[YT]))
    for merged, kept_names in ((False, []), (True, ["TEM"])):
        pool = build_idea_pool(snap, merged=merged, max_scout_only=20)
        kept, dropped, _ = _filter_shortlist(out, {i.ticker: i.stance for i in pool.items})
        assert [i.ticker for i in kept] == kept_names
        assert dropped[DROP_NOT_CANDIDATE] == 2 - len(kept_names)


# ---------------------------------------------------------------------------
# Switch, registry, strategy lane, XP-4
# ---------------------------------------------------------------------------


def test_switches_default_to_the_control_in_the_shipped_config() -> None:
    raw = yaml.safe_load(DEFAULT_ROUTINES_PATH.read_text())
    assert raw["personas"]["research_idea_pool"] == "scalp"
    assert raw["personas"]["research_compact_prompt"] == "full"
    cfg = load_routines(DEFAULT_ROUTINES_PATH)
    assert not cfg.research_idea_pool.merged and not cfg.research_compact_prompt.compact
    assert "research_idea_pool" not in cfg.personas  # a switch, not a job
    assert RoutinesConfig().research_idea_pool.mode == "scalp"
    assert RoutinesConfig().research_compact_prompt.mode == "full"
    assert cfg.funnel.research.max_scout_only_ideas == 20
    assert _settings().research_prompt_max_chars == 80_000


def test_research_reads_scout_read_in_code_and_yaml() -> None:
    cfg = load_routines(DEFAULT_ROUTINES_PATH)
    found = cfg.job("research")
    assert found is not None
    assert "scout_read" in RESEARCH_READS
    assert list(found[1].reads or []) == RESEARCH_READS


@pytest.mark.parametrize(
    ("key", "choices"),
    [
        ("personas.research_idea_pool", ("scalp", "all")),
        ("personas.research_compact_prompt", ("full", "compact")),
    ],
)
def test_registry_choice_keys(key: str, choices: tuple[str, str]) -> None:
    t = lookup(key)
    assert t is REGISTRY[key] and lookup(f"routines.{key}") is t
    assert t.choices == choices
    name = key.split(".", 1)[1]
    assert read_raw(t, {"personas": {}}) == choices[0]
    assert read_raw(t, {"personas": {name: choices[1]}}) == choices[1]
    assert write_raw(t, choices[1], {}) == [(("personas", name), choices[1])]


@pytest.mark.parametrize(
    ("data", "match"),
    [
        ({"personas": {"research_idea_pool": "both"}}, "scalp | all"),
        ({"personas": {"research_compact_prompt": "short"}}, "full | compact"),
    ],
)
def test_switch_values_are_validated(data: dict[str, Any], match: str) -> None:
    with pytest.raises(ValueError, match=match):
        RoutinesConfig.model_validate(data)


def test_controls_are_strategy_lane_off_values() -> None:
    lane = yaml.safe_load((REPO / "config" / "strategy_lane.yaml").read_text())
    assert {"scalp", "full"} <= set(lane["flag_off_values"])
    assert "arc/pipeline/research_pool.py" in yaml.safe_dump(lane)


def test_new_reason_codes_have_labels() -> None:
    assert ReasonCode("over_scout_only_cap") is ReasonCode.POOL_SCOUT_ONLY_CAP
    assert REASON_LABELS[ReasonCode.POOL_SCOUT_ONLY_CAP]
    assert REASON_LABELS[ReasonCode.POOL_OVER_PROMPT_BUDGET]


def test_xp4_draft_spec_turns_only_the_flag_on() -> None:
    spec = load_spec(REPO / "config" / "experiments" / "live" / "xp4_research_idea_pool.yaml")
    assert spec.id == "XP-4" and spec.kind.value == "ab"
    assert spec.arms.treatment.overlay == {"routines": {"personas": {"research_idea_pool": "all"}}}
    treat = RoutinesConfig.model_validate(arm_config_data(spec, "treatment", "routines"))
    base = load_routines(DEFAULT_ROUTINES_PATH)
    assert treat.research_idea_pool.merged
    assert treat.model_copy(update={"research_idea_pool": base.research_idea_pool}) == base


# ---------------------------------------------------------------------------
# The research step end to end (fixture chain)
# ---------------------------------------------------------------------------


def _research_run(mode: str) -> tuple[Any, Any]:
    from arc.ingest.scalp import load_fixture_docs
    from arc.pipeline.runner import open_db

    conn = open_db(":memory:", copy=False)
    load_fixture_docs(conn)
    store = ContextStore(conn)
    at = FIXTURE_NOW - dt.timedelta(hours=2)
    store.write(
        kind="candidate",
        subject="TEM",
        payload={
            **_cand("TEM", conf=0.8, sources=[YT], feed="scout", origins=[YT]),
            "catalyst_date": None,
            "created_at": at.isoformat(),
        },
        produced_by="scout",
        ttl="24h",
        valid_from=at,
        now=at,
    )
    env = PipelineEnv.fixtures()
    d = json.loads((FIXTURES_DIR / "research.json").read_text())
    base = d["shortlist"][0]
    d["shortlist"] = [base, {**base, "ticker": "TEM", "rank": 2}]
    env.llms["research"] = FixtureScalpLLM([json.dumps(d)])
    routines = load_routines().model_copy(
        update={"research_idea_pool": ResearchIdeaPoolSettings(mode=mode)}  # type: ignore[arg-type]
    )
    return _run(_settings(), routines, env, conn=conn)


@pytest.mark.parametrize("mode", ["scalp", "all"])
def test_research_step_ranks_scout_ideas_only_in_all_mode(mode: str) -> None:
    conn, _ = _research_run(mode)
    sl = _shortlist(conn)
    tickers = [i["ticker"] for i in sl["shortlist"]]
    row = conn.execute(
        "SELECT prompt_inputs, prompt_text FROM persona_calls WHERE persona='research' "
        "ORDER BY rowid DESC"
    ).fetchone()
    inputs = json.loads(row["prompt_inputs"])
    codes = _codes(conn, "shortlist")
    if mode == "scalp":
        assert "TEM" not in tickers and codes["not_a_candidate"] == ["TEM"]
        assert "idea_pool" not in inputs and "pool_merged" not in inputs
        assert "TEM" not in inputs["candidate_tickers"]  # Scout-only entry left out
        assert '"TEM"' not in row["prompt_text"] and sl.get("pool_counts") is None
    else:
        assert "TEM" in tickers and "not_a_candidate" not in codes
        assert inputs["pool_merged"] is True
        assert [i["ticker"] for i in inputs["idea_pool"] if i["feeds"] == ["scout"]] == ["TEM"]
        assert "### Idea pool (Scalp + Scout" in row["prompt_text"]
        assert sl["pool_counts"]["scout"] == 1
        cand = _codes(conn, "candidate")
        assert cand.get("scout_feed_candidate") == ["TEM"]


def test_replay_rebuilds_the_merged_prompt_from_recorded_inputs() -> None:
    from arc.pipeline.steps import build_prompt

    conn, _ = _research_run("all")
    row = conn.execute(
        "SELECT snapshot_id, prompt_inputs, prompt_text FROM persona_calls "
        "WHERE persona='research' ORDER BY rowid DESC"
    ).fetchone()
    snap = ContextStore(conn).load_snapshot(row["snapshot_id"])
    assert build_prompt("research", snap, json.loads(row["prompt_inputs"])) == row["prompt_text"]
