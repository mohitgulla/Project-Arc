"""E12.5 (D51, D44): relaxed Research diversification behind a default-strict flag.

Pins: ``strict`` (the default) leaves Research prompt and rules byte-identical to
origin/main's (hashes from tests/diversification_golden.py run against main); with
``relaxed`` the prompt carries the owner's wording, a flagged-sector
``adds_concentration`` pick drops only once the industry already holds
``max_names_per_industry`` names, stance skew alone no longer drops, and the flag
thresholds are the relaxed block (never tighter than strict).
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from arc.config import ArcSettings
from arc.control.effective import effective_routines
from arc.control.registry import REGISTRY, lookup, read_raw, write_raw
from arc.control.service import ControlService
from arc.experiments.overlay import arm_config_data, load_spec
from arc.ingest.llm import FixtureScalpLLM
from arc.ingest.scalp import load_fixture_docs
from arc.personas.builders import RELAXED_DIVERSIFICATION_FIT
from arc.personas.schemas import ResearchRankedItem
from arc.pipeline import FIXTURE_NOW, PipelineEnv
from arc.pipeline.portfolio_context import build_portfolio_context, load_industries
from arc.pipeline.runner import open_db
from arc.pipeline.steps import (
    DROP_CONCENTRATION,
    DROP_DEDUPE,
    _portfolio_filter,
    _research_rules,
    build_prompt,
)
from arc.routines.config import (
    DEFAULT_ROUTINES_PATH,
    ResearchDiversificationSettings,
    RoutinesConfig,
    load_routines,
)
from arc.store.db import connect
from arc.store.migrate import migrate
from arc.utils.calendar import ET
from tests import diversification_golden as dg
from tests import finnhub_golden as g
from tests.test_e59_research_portfolio import (
    FIXTURES_DIR,
    IRON_CONDOR,
    LONG_CALL,
    _codes,
    _open_structure,
    _outcome,
    _run,
    _shortlist,
)
from tests.test_routines_e53 import _env

REPO = Path(__file__).resolve().parent.parent
# sha256 of tests/diversification_golden.py's outputs on origin/main 58969a5 (pre-E12.5).
# E5.12 (D54) re-pinned the Director sha: "Scout" -> "Sweep" in two label lines only
# (diffed against origin/main 23be73a; no other byte changed).
# E13.1 (D56) re-pinned it: "Director" -> "Research", "Sweep" -> "Scalp" in the role,
# label and "candidates from" lines only (diffed against origin/main f1c1429).
MAIN_RESEARCH_SHA = "8f7a8bc57582ba1dcb2c974c59091b6692173c1e3632bd09ea333010c19d3da9"
MAIN_RULES_SHA = "08bc85caa583b536a000ab72d9625df6eb64eeb595bc21ad341fc32e62c63265"
RELAXED = ResearchDiversificationSettings(mode="relaxed")
STRICT = ResearchDiversificationSettings()
INDUSTRIES = {"NVDA": "semis", "AMD": "semis", "AVGO": "semis", "MU": "memory_storage"}


def _settings() -> ArcSettings:
    return ArcSettings(_env_file=None, account_profile="margin")  # type: ignore[call-arg]


def _item(ticker: str, stance: str = "bullish", fit: str | None = "adds_concentration") -> Any:
    return ResearchRankedItem(
        ticker=ticker,
        rank=1,
        thesis="t",
        regime_context="r",
        suggested_structure_type="vertical_spread",
        stance=stance,
        confidence=0.7,
        portfolio_fit=fit,  # type: ignore[arg-type]
    )


def _tickers(items: list[Any]) -> list[tuple[str, int]]:
    return [(i.ticker, i.rank) for i in items]


# ---------------------------------------------------------------------------
# Flag: config, registry, experiment overlay
# ---------------------------------------------------------------------------


def test_flag_defaults_strict_in_the_shipped_config() -> None:
    raw = yaml.safe_load(DEFAULT_ROUTINES_PATH.read_text())
    assert raw["personas"]["director_diversification"] == "strict"
    cfg = load_routines(DEFAULT_ROUTINES_PATH)
    dd = cfg.director_diversification
    assert dd.mode == "strict" and not dd.is_relaxed
    assert dd.max_names_per_industry == 2
    assert (dd.relaxed.sector_max_pct, dd.relaxed.stance_max_pct) == (0.55, 0.85)
    assert dd.relaxed.expiry_max_pct == 0.70
    assert "director_diversification" not in cfg.personas  # a switch, not a job
    assert RoutinesConfig().director_diversification.mode == "strict"  # absent = strict


@pytest.mark.parametrize("value", ["strict", "relaxed", " Relaxed "])
def test_flag_parses(value: str) -> None:
    cfg = RoutinesConfig.model_validate({"personas": {"director_diversification": value}})
    assert cfg.director_diversification.mode == value.strip().lower()


@pytest.mark.parametrize(
    ("data", "match"),
    [
        ({"personas": {"director_diversification": "loose"}}, "strict | relaxed"),
        ({"personas": {"director_diversification": True}}, "strict | relaxed"),
        (
            {
                "personas": {"director_diversification": "relaxed"},
                "director_diversification": {"mode": "relaxed"},
            },
            "set the switch as personas",
        ),
        ({"director_diversification": {"max_names_per_industry": 0}}, "greater than"),
        ({"director_diversification": {"relaxed": {"sector_max_pct": 1.5}}}, "less than"),
        ({"director_diversification": {"nope": 1}}, "Extra inputs"),
    ],
)
def test_flag_and_knobs_are_validated(data: dict[str, Any], match: str) -> None:
    with pytest.raises(ValueError, match=match):
        RoutinesConfig.model_validate(data)


def test_thresholds_strict_passthrough_relaxed_never_tighter() -> None:
    assert STRICT.thresholds(0.40, 0.75, 0.60) == (0.40, 0.75, 0.60)
    assert RELAXED.thresholds(0.40, 0.75, 0.60) == (0.55, 0.85, 0.70)
    # an owner who already loosened strict past relaxed keeps the looser value
    assert RELAXED.thresholds(0.60, 0.90, 0.65) == (0.60, 0.90, 0.70)


def test_registry_choice_key_reads_and_writes_the_switch() -> None:
    t = lookup("personas.director_diversification")
    assert t is REGISTRY["personas.director_diversification"]
    assert lookup("routines.personas.director_diversification") is t
    assert t.choices == ("strict", "relaxed")
    assert read_raw(t, {"personas": {"director_diversification": "relaxed"}}) == "relaxed"
    assert read_raw(t, {"personas": {}}) == "strict"
    assert write_raw(t, "relaxed", {}) == [(("personas", "director_diversification"), "relaxed")]


def test_slack_override_needs_a_confirm_and_reaches_the_effective_routines() -> None:
    conn = connect(":memory:")
    migrate(conn)
    now = dt.datetime(2026, 10, 6, 9, 0, tzinfo=ET)
    base = ArcSettings(_env_file=None, approver_slack_user_ids=["U0OWNER"])  # type: ignore[call-arg]
    svc = ControlService(conn, base=base, now=lambda: now)
    assert svc.view("personas.director_diversification").value == "strict"
    r = svc.set("personas.director_diversification", "relaxed", actor="U0OWNER", source="slack")
    assert r.pending is not None  # strict -> relaxed is the riskier direction
    svc.confirm(r.pending.code, actor="U0OWNER", source="slack")
    assert effective_routines(conn).director_diversification.is_relaxed
    r = svc.set("personas.director_diversification", "strict", actor="U0OWNER", source="slack")
    assert r.outcome == "applied"
    assert not effective_routines(conn).director_diversification.is_relaxed


def test_xp3_draft_spec_turns_only_the_flag_on() -> None:
    spec = load_spec(REPO / "config" / "experiments" / "live" / "xp3_relaxed_diversification.yaml")
    assert spec.id == "XP-3" and spec.kind.value == "ab"
    assert spec.arms.treatment.overlay == {
        "routines": {"personas": {"director_diversification": "relaxed"}}
    }
    treat = RoutinesConfig.model_validate(arm_config_data(spec, "treatment", "routines"))
    base = load_routines(DEFAULT_ROUTINES_PATH)
    assert treat.director_diversification.is_relaxed
    assert (
        treat.model_copy(update={"director_diversification": base.director_diversification}) == base
    )


def test_every_core_semis_name_has_an_industry() -> None:
    ind = load_industries()
    assert {ind[t] for t in ("NVDA", "AMD", "AVGO", "INTC")} == {"semis"}


# ---------------------------------------------------------------------------
# Golden: strict == main; relaxed carries the owner wording
# ---------------------------------------------------------------------------


def test_strict_prompt_and_rules_are_byte_identical_to_main() -> None:
    snap = g.snapshot(with_finnhub=False)
    assert dg.sha(dg.research_prompt(snap)) == MAIN_RESEARCH_SHA
    assert dg.sha(dg.research_prompt(snap, diversification="strict")) == MAIN_RESEARCH_SHA
    assert dg.sha("\n".join(dg.strict_rules())) == MAIN_RULES_SHA
    pctx = dg.portfolio_context(
        [("NVDA", "technology", "bullish", 420.0)], flagged_sectors=[], flagged_stances=[]
    )
    from arc.models import Stance

    cands = {"AMD": Stance.BULLISH}
    assert _research_rules(cands, _settings(), None, pctx, STRICT) == _research_rules(
        cands, _settings(), None, pctx
    )


def test_relaxed_prompt_swaps_only_the_concentration_wording() -> None:
    snap = g.snapshot(with_finnhub=False)
    strict = dg.research_prompt(snap)
    relaxed = dg.research_prompt(snap, diversification="relaxed")
    assert relaxed != strict
    assert "is not, by itself, a reason to exclude" in relaxed
    assert "Rank two names in the same industry when each has its own catalyst" in relaxed
    assert "push a sector past the flagged level" in relaxed
    assert "or a name already held" not in relaxed and "or a name already held" in strict
    assert "`portfolio_fit`" in relaxed  # the field stays
    head, _, tail = strict.partition("adds_concentration (piles onto")
    assert relaxed.startswith(head) and RELAXED_DIVERSIFICATION_FIT in relaxed
    assert relaxed.endswith(tail.split("Give `portfolio_view`", 1)[1])


def test_empty_book_prompt_is_the_same_in_both_modes() -> None:
    snap = g.snapshot(with_finnhub=False)
    a = g.research_prompt(snap)
    assert g.research_prompt(snap, diversification="relaxed") == a


def test_relaxed_rules_state_the_industry_cap() -> None:
    from arc.models import Stance

    pctx = dg.portfolio_context(
        [("NVDA", "technology", "bullish", 420.0)],
        flagged_sectors=["technology"],
        flagged_stances=[],
    )
    rules = "\n".join(_research_rules({"AMD": Stance.BULLISH}, _settings(), None, pctx, RELAXED))
    assert "once the book holds 2 names in that industry" in rules
    assert "flagged sector/stance/expiry" not in rules


def test_replay_rebuilds_the_relaxed_prompt_from_recorded_inputs() -> None:
    snap = g.snapshot(with_finnhub=False)
    inputs = {
        "portfolio_summary": "2 open",
        "scan_date": "2026-10-06",
        "portfolio_block": dg.PORTFOLIO_BLOCK,
        "categories": None,
        "diversification": "relaxed",
        "rules": ["r"],
    }
    p = build_prompt("research", snap, inputs)
    assert RELAXED_DIVERSIFICATION_FIT in p
    strict = build_prompt(
        "research", snap, {k: v for k, v in inputs.items() if k != "diversification"}
    )
    assert RELAXED_DIVERSIFICATION_FIT not in strict


# ---------------------------------------------------------------------------
# _portfolio_filter (deterministic drops)
# ---------------------------------------------------------------------------


def _semis_book(*, flagged_sectors: list[str], flagged_stances: list[str]) -> Any:
    return dg.portfolio_context(
        [("NVDA", "technology", "bullish", 420.0), ("XOM", "energy", "bearish", 100.0)],
        flagged_sectors=flagged_sectors,
        flagged_stances=flagged_stances,
    )


def test_relaxed_keeps_the_second_semis_name_and_drops_the_third() -> None:
    pctx = _semis_book(flagged_sectors=["technology"], flagged_stances=["bullish"])
    kept = [_item("AMD"), _item("AVGO"), _item("MU")]
    out, dropped, rejected = _portfolio_filter(
        kept, pctx, {}, _settings(), RELAXED, industries=INDUSTRIES
    )
    # NVDA held + AMD = 2 semis -> AVGO (3rd) drops; MU is memory_storage (1st) -> kept
    assert _tickers(out) == [("AMD", 1), ("MU", 2)]
    assert dropped[DROP_CONCENTRATION] == 1 and [i.ticker for i, _ in rejected] == ["AVGO"]


def test_strict_drops_every_adds_concentration_pick_on_the_flagged_sector() -> None:
    pctx = _semis_book(flagged_sectors=["technology"], flagged_stances=["bullish"])
    kept = [_item("AMD"), _item("AVGO"), _item("MU")]
    for div in (None, STRICT):
        out, dropped, _ = _portfolio_filter(kept, pctx, {}, _settings(), div, industries=INDUSTRIES)
        assert out == [] and dropped[DROP_CONCENTRATION] == 3


def test_relaxed_stance_skew_alone_does_not_drop() -> None:
    pctx = _semis_book(flagged_sectors=[], flagged_stances=["bullish"])
    kept = [_item("AMD"), _item("AVGO")]
    out, dropped, _ = _portfolio_filter(kept, pctx, {}, _settings(), RELAXED, industries=INDUSTRIES)
    assert _tickers(out) == [("AMD", 1), ("AVGO", 2)] and not dropped
    out, dropped, _ = _portfolio_filter(kept, pctx, {}, _settings(), STRICT, industries=INDUSTRIES)
    assert out == [] and dropped[DROP_CONCENTRATION] == 2


def test_relaxed_unflagged_sector_never_drops_even_past_the_industry_cap() -> None:
    pctx = _semis_book(flagged_sectors=[], flagged_stances=[])
    kept = [_item("AMD"), _item("AVGO"), _item("INTC")]
    out, dropped, _ = _portfolio_filter(
        kept, pctx, {}, _settings(), RELAXED, industries={**INDUSTRIES, "INTC": "semis"}
    )
    assert [t for t, _ in _tickers(out)] == ["AMD", "AVGO", "INTC"] and not dropped


def test_relaxed_held_ticker_still_drops_and_dedupe_is_unchanged() -> None:
    pctx = _semis_book(flagged_sectors=[], flagged_stances=[])
    # NVDA held bearish-side pick flagged by Research: already-held name drops
    out, dropped, _ = _portfolio_filter(
        [_item("NVDA", stance="bearish")], pctx, {}, _settings(), RELAXED, industries=INDUSTRIES
    )
    assert out == [] and dropped[DROP_CONCENTRATION] == 1
    # same ticker + stance as an open structure: the D33 dedupe, in both modes
    for div in (STRICT, RELAXED):
        out, dropped, _ = _portfolio_filter(
            [_item("NVDA", fit="neutral")], pctx, {"NVDA": "bullish"}, _settings(), div
        )
        assert out == [] and dropped[DROP_DEDUPE] == 1


def test_relaxed_neutral_or_diversifying_picks_are_untouched() -> None:
    pctx = _semis_book(flagged_sectors=["technology"], flagged_stances=["bullish"])
    kept = [_item(t, fit="diversifies") for t in ("AMD", "AVGO", "INTC")]
    out, dropped, _ = _portfolio_filter(kept, pctx, {}, _settings(), RELAXED, industries=INDUSTRIES)
    assert len(out) == 3 and not dropped


def test_relaxed_unmapped_industry_falls_back_to_the_sector() -> None:
    pctx = dg.portfolio_context(
        [("ZZZ", "technology", "bullish", 100.0), ("YYY", "technology", "bullish", 100.0)],
        flagged_sectors=["technology"],
        flagged_stances=[],
    )
    out, dropped, _ = _portfolio_filter(
        [_item("XXX")], pctx, {}, _settings(), RELAXED, industries={}
    )
    # XXX is not in sectors.yaml -> sector unknown -> its own bucket, kept
    assert _tickers(out) == [("XXX", 1)] and not dropped


def test_relaxed_loads_the_shipped_industry_map_by_default() -> None:
    pctx = dg.portfolio_context(
        [("NVDA", "technology", "bullish", 100.0), ("AMD", "technology", "bullish", 100.0)],
        flagged_sectors=["technology"],
        flagged_stances=[],
    )
    out, dropped, _ = _portfolio_filter(
        [_item("AVGO"), _item("MSFT")], pctx, {}, _settings(), RELAXED
    )
    assert _tickers(out) == [("MSFT", 1)] and dropped[DROP_CONCENTRATION] == 1


# ---------------------------------------------------------------------------
# Portfolio context thresholds + the research step end to end
# ---------------------------------------------------------------------------


def test_relaxed_thresholds_reach_the_portfolio_context() -> None:
    conn = open_db(":memory:", copy=False)
    env = _env([])
    settings = _settings()
    _open_structure(conn, env, IRON_CONDOR, contracts=1)  # neutral, ~$334 max loss
    _open_structure(conn, env, LONG_CALL, stance="bullish", entry="12.10", contracts=1)
    kw: dict[str, Any] = {
        "info": env.account(),
        "now": FIXTURE_NOW,
        "halted": False,
        "budget_tier": "normal",
    }
    strict = build_portfolio_context(conn, env, settings, **kw)
    relaxed = build_portfolio_context(conn, env, settings, diversification=RELAXED, **kw)
    s_ag, r_ag = strict.aggregates, relaxed.aggregates
    assert s_ag is not None and r_ag is not None
    share = s_ag.by_stance["bullish"]
    assert 0.75 < share <= 0.85  # flagged strict, not relaxed
    assert s_ag.flagged_stances == ["bullish"] and r_ag.flagged_stances == []
    assert strict.thresholds["sector_max_pct"] == 0.40
    assert relaxed.thresholds["sector_max_pct"] == 0.55
    assert relaxed.thresholds["stance_max_pct"] == 0.85
    assert relaxed.thresholds["expiry_max_pct"] == 0.70
    assert build_portfolio_context(conn, env, settings, diversification=STRICT, **kw) == strict


def _research_run(mode: str) -> tuple[Any, Any]:
    conn = open_db(":memory:", copy=False)
    load_fixture_docs(conn)
    env = PipelineEnv.fixtures()
    _open_structure(conn, env, LONG_CALL, stance="bullish", entry="12.10", contracts=1)
    _open_structure(
        conn, env, LONG_CALL, stance="bullish", entry="12.10", contracts=1,
        at=FIXTURE_NOW - dt.timedelta(days=2),
    )  # fmt: skip
    d = json.loads((FIXTURES_DIR / "research.json").read_text())
    base = d["shortlist"][0]
    d["shortlist"] = [
        {**base, "ticker": "NVDA", "stance": "bullish", "rank": 1,
         "portfolio_fit": "adds_concentration"},  # only the bullish stance is flagged
    ]  # fmt: skip
    d["excluded"] = []
    env.llms["research"] = FixtureScalpLLM([json.dumps(d)])
    routines = load_routines()
    routines = routines.model_copy(
        update={"director_diversification": ResearchDiversificationSettings(mode=mode)}  # type: ignore[arg-type]
    )
    conn, report = _run(_settings(), routines, env, conn=conn)
    return conn, report


@pytest.mark.parametrize("mode", ["strict", "relaxed"])
def test_research_step_applies_the_mode(mode: str) -> None:
    conn, report = _research_run(mode)
    tickers = [i["ticker"] for i in _shortlist(conn)["shortlist"]]
    row = conn.execute(
        "SELECT prompt_inputs FROM persona_calls WHERE persona='research' ORDER BY rowid DESC"
    ).fetchone()
    inputs = json.loads(row[0])
    if mode == "strict":
        assert tickers == [] and _codes(conn, "shortlist")["drop_concentration"] == ["NVDA"]
        assert "diversification" not in inputs  # strict records nothing (replay unchanged)
    else:
        assert tickers == ["NVDA"]
        assert "drop_concentration" not in _outcome(report, "research").metrics
        assert inputs["diversification"] == "relaxed"
        assert any("names in that industry" in r for r in inputs["rules"])
