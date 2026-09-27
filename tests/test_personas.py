"""Golden tests for persona schemas and prompt builders.

Tests validate:
1. Schema validation of sample outputs (all 6 personas).
2. Prompt builders are pure functions that return strings.
3. Risk persona output is advisory only (schema enforces this).
4. Round-trip serialization of all output schemas.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from arc.personas.builders import (
    AuditorInput,
    DirectorInput,
    ExecutionInput,
    QuantInput,
    RiskInput,
    ScoutInput,
    build_auditor_prompt,
    build_director_prompt,
    build_execution_prompt,
    build_quant_prompt,
    build_risk_prompt,
    build_scout_prompt,
)
from arc.personas.schemas import (
    AnomalyReport,
    AuditorOutput,
    DirectorOutput,
    DirectorRankedItem,
    ExecutionOutput,
    ImprovementStep,
    LessonLearned,
    QuantOutput,
    QuantStructureOut,
    RiskAssessment,
    RiskOutput,
    ScoutCandidateOut,
    ScoutOutput,
)

# ---------------------------------------------------------------------------
# Sample data fixtures
# ---------------------------------------------------------------------------

SAMPLE_SCOUT_OUTPUT = {
    "candidates": [
        {
            "ticker": "AAPL",
            "stance": "bullish",
            "catalyst_type": "earnings",
            "catalyst_date": "2026-10-28",
            "confidence": 0.8,
            "sources": ["https://reuters.com/aapl-earnings"],
            "rationale": "Strong iPhone 18 pre-orders suggest beat on revenue.",
        },
        {
            "ticker": "NVDA",
            "stance": "bullish",
            "catalyst_type": "sector",
            "catalyst_date": None,
            "confidence": 0.7,
            "sources": ["https://sec.gov/edgar/nvda-10q"],
            "rationale": "Data center GPU demand continues to accelerate.",
        },
    ],
    "scan_summary": (
        "Scanned RSS, EDGAR, earnings calendar."
        " Key themes: tech earnings season, AI infrastructure spend."
    ),
}

SAMPLE_DIRECTOR_OUTPUT = {
    "shortlist": [
        {
            "ticker": "AAPL",
            "rank": 1,
            "thesis": "Earnings beat expected; risk-on regime favors bullish vertical.",
            "regime_context": (
                "Risk-on with moderate IV; historical post-earnings moves suggest upside."
            ),
            "suggested_structure_type": "vertical_spread",
            "stance": "bullish",
            "confidence": 0.85,
        },
    ],
    "market_regime": "risk_on",
    "session_notes": "Low VIX environment, tech leading. Prefer defined-risk bullish structures.",
}

SAMPLE_QUANT_OUTPUT = {
    "structures": [
        {
            "ticker": "AAPL",
            "structure_type": "vertical_spread",
            "legs": [
                {
                    "occ_symbol": "AAPL  261115C00200000",
                    "side": "long",
                    "ratio": 1,
                    "strike": 200.0,
                    "expiry": "2026-11-15",
                    "option_type": "call",
                },
                {
                    "occ_symbol": "AAPL  261115C00210000",
                    "side": "short",
                    "ratio": 1,
                    "strike": 210.0,
                    "expiry": "2026-11-15",
                    "option_type": "call",
                },
            ],
            "net_debit_credit": 3.50,
            "max_gain": 6.50,
            "max_loss": 3.50,
            "breakevens": [203.50],
            "greeks": {"delta": 0.35, "gamma": 0.02, "vega": 0.15, "theta": -0.05},
            "dte": 45,
            "pop": 0.55,
            "ev_per_contract": 12.50,
            "cost_bps": 8.0,
            "confidence": 0.7,
            "rationale": "Bull call spread targeting post-earnings move.",
        },
    ],
    "analysis_notes": "Moderate IV environment supports debit spreads.",
}

SAMPLE_RISK_OUTPUT = {
    "assessments": [
        {
            "ticker": "AAPL",
            "structure_type": "vertical_spread",
            "risk_rating": "moderate",
            "concentration_warning": False,
            "greek_budget_impact": "Adds 0.35 delta; portfolio delta moves to 0.55, within budget.",
            "calendar_concerns": "Earnings on 10/28 — structure expires after event, acceptable.",
            "sizing_suggestion": 2,
            "max_loss_pct_equity": 0.03,
            "narrative": (
                "Moderate risk. Defined-risk structure limits downside."
                " Earnings event is within the DTE window"
                " but structure is designed for it."
            ),
        },
    ],
    "portfolio_summary": "3 open positions, net delta 0.20, net vega 0.10. Within all budgets.",
    "advisory_notes": "Advisory: sizing is suggestion only. Gate will enforce hard limits.",
}

SAMPLE_EXECUTION_OUTPUT = {
    "plans": [
        {
            "ticker": "AAPL",
            "structure_type": "vertical_spread",
            "order_type": "limit",
            "initial_limit_price": 3.25,
            "improvement_steps": [
                {"step_number": 1, "price": 3.30, "wait_seconds": 30},
                {"step_number": 2, "price": 3.35, "wait_seconds": 30},
                {"step_number": 3, "price": 3.40, "wait_seconds": 30},
            ],
            "timeout_seconds": 120,
            "contracts": 2,
            "notes": "Starting at mid. Widening in 5c increments every 30s. Cancel after 2 min.",
        },
    ],
    "market_conditions_note": "Normal spread conditions, liquidity adequate.",
}

SAMPLE_AUDITOR_OUTPUT = {
    "journal_date": "2026-09-27",
    "daily_pnl": 150.25,
    "open_positions": 3,
    "closed_today": 1,
    "fills_reviewed": 4,
    "anomalies": [
        {
            "category": "fill_discrepancy",
            "severity": "warning",
            "description": "Fill price 0.02 worse than expected mid.",
            "affected_orders": ["ord_001"],
        },
    ],
    "lessons": [
        {
            "topic": "Execution slippage",
            "observation": "Afternoon fills consistently worse than morning.",
            "recommendation": "Prioritize morning execution for less liquid names.",
        },
    ],
    "journal_narrative": (
        "Solid day. One fill discrepancy flagged but within tolerance."
        " Portfolio performing as expected."
    ),
    "reconciliation_status": "clean",
}


# ---------------------------------------------------------------------------
# Schema validation tests — golden outputs
# ---------------------------------------------------------------------------


class TestScoutSchema:
    def test_valid_output(self) -> None:
        out = ScoutOutput.model_validate(SAMPLE_SCOUT_OUTPUT)
        assert len(out.candidates) == 2
        assert out.candidates[0].ticker == "AAPL"
        assert out.candidates[0].confidence == 0.8

    def test_roundtrip(self) -> None:
        out = ScoutOutput.model_validate(SAMPLE_SCOUT_OUTPUT)
        data = json.loads(out.model_dump_json())
        out2 = ScoutOutput.model_validate(data)
        assert out == out2

    def test_requires_sources(self) -> None:
        bad = {**SAMPLE_SCOUT_OUTPUT["candidates"][0], "sources": []}
        with pytest.raises(ValidationError, match="sources"):
            ScoutCandidateOut.model_validate(bad)

    def test_confidence_bounds(self) -> None:
        bad = {**SAMPLE_SCOUT_OUTPUT["candidates"][0], "confidence": 1.5}
        with pytest.raises(ValidationError, match="confidence"):
            ScoutCandidateOut.model_validate(bad)


class TestDirectorSchema:
    def test_valid_output(self) -> None:
        out = DirectorOutput.model_validate(SAMPLE_DIRECTOR_OUTPUT)
        assert len(out.shortlist) == 1
        assert out.shortlist[0].rank == 1
        assert out.market_regime == "risk_on"

    def test_roundtrip(self) -> None:
        out = DirectorOutput.model_validate(SAMPLE_DIRECTOR_OUTPUT)
        data = json.loads(out.model_dump_json())
        out2 = DirectorOutput.model_validate(data)
        assert out == out2

    def test_rank_must_be_positive(self) -> None:
        bad = {**SAMPLE_DIRECTOR_OUTPUT["shortlist"][0], "rank": 0}
        with pytest.raises(ValidationError, match="rank"):
            DirectorRankedItem.model_validate(bad)


class TestQuantSchema:
    def test_valid_output(self) -> None:
        out = QuantOutput.model_validate(SAMPLE_QUANT_OUTPUT)
        assert len(out.structures) == 1
        s = out.structures[0]
        assert len(s.legs) == 2
        assert s.pop == 0.55
        assert s.greeks.delta == 0.35

    def test_roundtrip(self) -> None:
        out = QuantOutput.model_validate(SAMPLE_QUANT_OUTPUT)
        data = json.loads(out.model_dump_json())
        out2 = QuantOutput.model_validate(data)
        assert out == out2

    def test_pop_bounds(self) -> None:
        bad_struct = {**SAMPLE_QUANT_OUTPUT["structures"][0], "pop": 1.5}
        with pytest.raises(ValidationError, match="pop"):
            QuantStructureOut.model_validate(bad_struct)

    def test_requires_at_least_one_leg(self) -> None:
        bad_struct = {**SAMPLE_QUANT_OUTPUT["structures"][0], "legs": []}
        with pytest.raises(ValidationError, match="legs"):
            QuantStructureOut.model_validate(bad_struct)


class TestRiskSchema:
    def test_valid_output(self) -> None:
        out = RiskOutput.model_validate(SAMPLE_RISK_OUTPUT)
        assert len(out.assessments) == 1
        assert out.assessments[0].risk_rating == "moderate"

    def test_roundtrip(self) -> None:
        out = RiskOutput.model_validate(SAMPLE_RISK_OUTPUT)
        data = json.loads(out.model_dump_json())
        out2 = RiskOutput.model_validate(data)
        assert out == out2

    def test_risk_output_is_advisory(self) -> None:
        """Risk schema docstring must state advisory-only nature."""
        assert "ADVISORY ONLY" in (RiskOutput.__doc__ or "")

    def test_sizing_is_nonnegative(self) -> None:
        bad = {**SAMPLE_RISK_OUTPUT["assessments"][0], "sizing_suggestion": -1}
        with pytest.raises(ValidationError, match="sizing_suggestion"):
            RiskAssessment.model_validate(bad)

    def test_max_loss_pct_equity_bounds(self) -> None:
        bad = {**SAMPLE_RISK_OUTPUT["assessments"][0], "max_loss_pct_equity": 1.5}
        with pytest.raises(ValidationError, match="max_loss_pct_equity"):
            RiskAssessment.model_validate(bad)


class TestExecutionSchema:
    def test_valid_output(self) -> None:
        out = ExecutionOutput.model_validate(SAMPLE_EXECUTION_OUTPUT)
        assert len(out.plans) == 1
        plan = out.plans[0]
        assert plan.order_type == "limit"
        assert len(plan.improvement_steps) == 3

    def test_roundtrip(self) -> None:
        out = ExecutionOutput.model_validate(SAMPLE_EXECUTION_OUTPUT)
        data = json.loads(out.model_dump_json())
        out2 = ExecutionOutput.model_validate(data)
        assert out == out2

    def test_step_number_positive(self) -> None:
        bad = {"step_number": 0, "price": 3.30, "wait_seconds": 30}
        with pytest.raises(ValidationError, match="step_number"):
            ImprovementStep.model_validate(bad)


class TestAuditorSchema:
    def test_valid_output(self) -> None:
        out = AuditorOutput.model_validate(SAMPLE_AUDITOR_OUTPUT)
        assert out.daily_pnl == 150.25
        assert out.reconciliation_status == "clean"
        assert len(out.anomalies) == 1
        assert len(out.lessons) == 1

    def test_roundtrip(self) -> None:
        out = AuditorOutput.model_validate(SAMPLE_AUDITOR_OUTPUT)
        data = json.loads(out.model_dump_json())
        out2 = AuditorOutput.model_validate(data)
        assert out == out2

    def test_anomaly_validates(self) -> None:
        a = AnomalyReport.model_validate(SAMPLE_AUDITOR_OUTPUT["anomalies"][0])
        assert a.severity == "warning"

    def test_lesson_validates(self) -> None:
        lesson = LessonLearned.model_validate(SAMPLE_AUDITOR_OUTPUT["lessons"][0])
        assert lesson.topic == "Execution slippage"


# ---------------------------------------------------------------------------
# Prompt builder tests — pure functions, return strings
# ---------------------------------------------------------------------------


class TestPromptBuilders:
    """Verify prompt builders are pure functions that return non-empty strings."""

    def test_scout_builder(self) -> None:
        inp = ScoutInput(
            universe=["AAPL", "NVDA"],
            raw_feeds=["AAPL earnings beat expectations."],
            scan_date="2026-09-27",
        )
        result = build_scout_prompt(inp)
        assert isinstance(result, str)
        assert len(result) > 100
        assert "Scout" in result
        assert "AAPL" in result
        assert "broker" in result.lower()  # forbidden actions mentioned

    def test_director_builder(self) -> None:
        inp = DirectorInput(
            candidates_json=json.dumps(SAMPLE_SCOUT_OUTPUT),
            regime_features_json='{"regime": "risk_on"}',
            portfolio_summary="3 open positions",
            scan_date="2026-09-27",
        )
        result = build_director_prompt(inp)
        assert isinstance(result, str)
        assert "Director" in result

    def test_quant_builder(self) -> None:
        inp = QuantInput(
            shortlist_json=json.dumps(SAMPLE_DIRECTOR_OUTPUT),
            chains_json='{"AAPL": []}',
            underlying_prices_json='{"AAPL": 205.0}',
            scan_date="2026-09-27",
        )
        result = build_quant_prompt(inp)
        assert isinstance(result, str)
        assert "Quant" in result

    def test_risk_builder(self) -> None:
        inp = RiskInput(
            structures_json=json.dumps(SAMPLE_QUANT_OUTPUT),
            portfolio_json='{"positions": []}',
            calendar_json='{"earnings": []}',
            account_equity=100000.0,
            scan_date="2026-09-27",
        )
        result = build_risk_prompt(inp)
        assert isinstance(result, str)
        assert "Risk" in result
        assert "ADVISORY" in result

    def test_execution_builder(self) -> None:
        inp = ExecutionInput(
            proposal_json='{"proposal": "test"}',
            current_quotes_json='{"quotes": []}',
            scan_date="2026-09-27",
        )
        result = build_execution_prompt(inp)
        assert isinstance(result, str)
        assert "Execution" in result or "Exec" in result

    def test_auditor_builder(self) -> None:
        inp = AuditorInput(
            fills_json='{"fills": []}',
            positions_json='{"positions": []}',
            broker_positions_json='{"positions": []}',
            pnl_json='{"pnl": 0}',
            journal_date="2026-09-27",
        )
        result = build_auditor_prompt(inp)
        assert isinstance(result, str)
        assert "Auditor" in result

    def test_scout_builder_empty_feeds(self) -> None:
        """Builder handles empty feeds gracefully."""
        inp = ScoutInput(
            universe=["SPY"],
            raw_feeds=[],
            scan_date="2026-09-27",
        )
        result = build_scout_prompt(inp)
        assert "(no feeds)" in result

    def test_builders_are_deterministic(self) -> None:
        """Same input produces same output (pure function)."""
        inp = ScoutInput(
            universe=["AAPL"],
            raw_feeds=["test feed"],
            scan_date="2026-09-27",
        )
        r1 = build_scout_prompt(inp)
        r2 = build_scout_prompt(inp)
        assert r1 == r2

    def test_all_prompts_mention_forbidden_broker(self) -> None:
        """Every persona prompt must mention broker prohibition."""
        scout_inp = ScoutInput(universe=["SPY"], raw_feeds=["x"], scan_date="2026-09-27")
        director_inp = DirectorInput(
            candidates_json="{}",
            regime_features_json="{}",
            portfolio_summary="",
            scan_date="2026-09-27",
        )
        quant_inp = QuantInput(
            shortlist_json="{}",
            chains_json="{}",
            underlying_prices_json="{}",
            scan_date="2026-09-27",
        )
        risk_inp = RiskInput(
            structures_json="{}",
            portfolio_json="{}",
            calendar_json="{}",
            account_equity=100000.0,
            scan_date="2026-09-27",
        )
        exec_inp = ExecutionInput(
            proposal_json="{}",
            current_quotes_json="{}",
            scan_date="2026-09-27",
        )
        auditor_inp = AuditorInput(
            fills_json="{}",
            positions_json="{}",
            broker_positions_json="{}",
            pnl_json="{}",
            journal_date="2026-09-27",
        )

        for name, builder, inp in [
            ("scout", build_scout_prompt, scout_inp),
            ("director", build_director_prompt, director_inp),
            ("quant", build_quant_prompt, quant_inp),
            ("risk", build_risk_prompt, risk_inp),
            ("execution", build_execution_prompt, exec_inp),
            ("auditor", build_auditor_prompt, auditor_inp),
        ]:
            prompt = builder(inp)
            assert "broker" in prompt.lower() or "order" in prompt.lower(), (
                f"{name} prompt must mention broker/order prohibition"
            )
