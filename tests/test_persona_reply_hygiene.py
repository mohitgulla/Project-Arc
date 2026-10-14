"""Deterministic trim / key normalisation of persona replies (XP-1 day 1, 2026-10-14).

Replays the payload shapes that failed live persona steps and checks that real
schema drift (unknown keys, conflicting duplicates) still fails validation.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import ValidationError

from arc.personas.schemas import (
    ExitWatchItem,
    QuantExitJudgement,
    QuantSkip,
    ReplyModel,
    ResearchExclusion,
    ResearchExitOutput,
    ResearchPortfolioView,
    ResearchThesisCheck,
    RiskExitOutput,
    RiskExitVerdict,
    RiskOpenOutput,
    RiskReviseRequest,
    ScoutOutput,
    ScoutTickerCall,
    normalise_reply_keys,
)


def _watch(**over: Any) -> dict[str, Any]:
    return {
        "structure_id": "s-1",
        "ticker": "AAPL",
        "action": "hold",
        "thesis_status": "intact",
        "evidence": ["story:abc IV rank 18"],
        "reason": "thesis holds",
    } | over


def _research_exit(items: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "shortlist": [],
        "excluded": [],
        "market_regime": "risk_on",
        "session_notes": "quiet",
        "no_trade_reason": "no_fit",
        "exit_watchlist": items,
    }


def _risk_open(instruction: str) -> dict[str, Any]:
    return {
        "assessments": [
            {
                "ticker": "AAPL",
                "structure_type": "vertical_spread",
                "risk_rating": "moderate",
                "greek_budget_impact": "small",
                "calendar_concerns": "none",
                "sizing_suggestion": 1,
                "max_loss_pct_equity": 0.02,
                "narrative": "ok",
                "verdict": "revise",
                "revise_request": {"reason": "width", "instruction": instruction},
            }
        ],
        "portfolio_summary": "flat",
        "advisory_notes": "none",
    }


# --- the live failures ---------------------------------------------------------


def test_research_stray_space_reason_key_with_empty_value_parses() -> None:
    """research t1/t2 2026-10-14: ``exit_watchlist.0. reason`` extra_forbidden, ''."""
    out = ResearchExitOutput.model_validate(_research_exit([_watch(**{" reason": ""})]))
    item = out.exit_watchlist[0]
    assert item.reason == "thesis holds"
    assert " reason" not in item.model_dump()


def test_research_stray_key_carries_the_only_reason() -> None:
    raw = _watch(**{" reason": "rolled into the stray key"})
    del raw["reason"]
    assert ExitWatchItem.model_validate(raw).reason == "rolled into the stray key"


def test_research_stray_key_fills_blank_real_field() -> None:
    raw = _watch(reason="", **{"Reason ": "the real text"})
    assert ExitWatchItem.model_validate(raw).reason == "the real text"


def test_research_stray_duplicate_with_same_value_parses() -> None:
    raw = _watch(**{"reason ": "thesis holds"})
    assert ExitWatchItem.model_validate(raw).reason == "thesis holds"


def test_risk_open_over_long_instruction_parses_trimmed() -> None:
    """risk.open t2 14:12:26Z / production 14:40:06Z: instruction string_too_long."""
    long = "  " + "Narrow the spread to one strike width and cut size. " * 10 + "  "
    assert len(long) > 240
    out = RiskOpenOutput.model_validate(_risk_open(long))
    req = out.assessments[0].revise_request
    assert req is not None
    assert len(req.instruction) == 240
    assert req.instruction == long.strip()[:240]


def test_risk_open_reply_text_round_trips() -> None:
    """The same shape parsed from a JSON reply string (as the pipeline does)."""
    text = json.dumps(_risk_open("x" * 500))
    out = RiskOpenOutput.model_validate_json(text)
    assert out.assessments[0].revise_request.instruction == "x" * 240  # type: ignore[union-attr]


# --- drift still fails -----------------------------------------------------------


def test_unknown_key_still_fails() -> None:
    with pytest.raises(ValidationError) as ei:
        ResearchExitOutput.model_validate(_research_exit([_watch(size_hint=3)]))
    assert ei.value.errors()[0]["type"] == "extra_forbidden"


def test_near_miss_key_is_not_folded() -> None:
    """Only whitespace/case variants fold; a misspelling is drift."""
    with pytest.raises(ValidationError, match="extra_forbidden|Extra inputs"):
        ExitWatchItem.model_validate(_watch(reasons="x"))


def test_conflicting_duplicate_still_fails() -> None:
    with pytest.raises(ValidationError, match="Extra inputs"):
        ExitWatchItem.model_validate(_watch(**{" reason": "a different reason"}))


def test_unknown_key_on_revise_request_still_fails() -> None:
    raw = _risk_open("ok")
    raw["assessments"][0]["revise_request"]["contracts"] = 2
    with pytest.raises(ValidationError, match="Extra inputs"):
        RiskOpenOutput.model_validate(raw)


def test_non_string_instruction_still_fails() -> None:
    with pytest.raises(ValidationError):
        RiskReviseRequest.model_validate({"reason": "size", "instruction": 5})


# --- the shared helper -----------------------------------------------------------


@pytest.mark.parametrize(
    "model",
    [
        ResearchExclusion,
        ResearchPortfolioView,
        ResearchThesisCheck,
        ExitWatchItem,
        QuantSkip,
        RiskReviseRequest,
        RiskExitVerdict,
        RiskExitOutput,
        QuantExitJudgement,
        ScoutTickerCall,
        ScoutOutput,
    ],
)
def test_reply_models_forbid_extra_and_normalise(model: type[ReplyModel]) -> None:
    assert issubclass(model, ReplyModel)
    assert model.model_config.get("extra") == "forbid"


def test_normalise_reply_keys_cases() -> None:
    fields = {"reason", "ticker"}
    assert normalise_reply_keys("x", fields) == "x"
    assert normalise_reply_keys({"ticker": "A"}, fields) == {"ticker": "A"}
    assert normalise_reply_keys({" TICKER ": "A"}, fields) == {"ticker": "A"}
    assert normalise_reply_keys({"ticker": None, "Ticker": "A"}, fields) == {"ticker": "A"}
    assert normalise_reply_keys({"ticker": "A", " ticker": []}, fields) == {"ticker": "A"}
    assert normalise_reply_keys({"ticker": "A", " ticker": {}}, fields) == {"ticker": "A"}
    assert normalise_reply_keys({"ticker": "A", " ticker": "B"}, fields) == {
        "ticker": "A",
        " ticker": "B",
    }
    assert normalise_reply_keys({1: "x", "other": 2}, fields) == {1: "x", "other": 2}


def test_reply_schema_has_no_new_keywords() -> None:
    """The base adds no field, so prompts embedding the schema stay byte-identical."""
    schema = ExitWatchItem.model_json_schema()
    assert set(schema["properties"]) == {
        "structure_id",
        "ticker",
        "action",
        "thesis_status",
        "evidence",
        "reason",
    }
    assert schema["additionalProperties"] is False


# --- the sweep: other LLM-filled strings with max_length now trim -----------------


def _call(**over: Any) -> dict[str, Any]:
    return {
        "ticker": "NVDA",
        "stance": "bullish",
        "confidence": 0.6,
        "horizon": "days",
        "origins": ["youtube:stockedup"],
        "thesis": "AI capex",
        "catalyst_type": "earnings",
    } | over


def test_scout_ticker_call_thesis_trimmed() -> None:
    call = ScoutTickerCall.model_validate(_call(thesis=" " + "t" * 300))
    assert call.thesis == "t" * 240


def test_scout_themes_trimmed_to_one_line() -> None:
    out = ScoutOutput.model_validate(
        {"regime": "r", "options_sentiment": "o", "themes": ["a\nb", "z" * 200]}
    )
    assert out.themes == ["a b", "z" * 160]
