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
    QuantInput,
    ResearchInput,
    RiskInput,
    ScalpInput,
    build_quant_prompt,
    build_research_prompt,
    build_risk_prompt,
    build_scalp_prompt,
)
from arc.personas.schemas import (
    AnomalyReport,
    BrokerPlan,
    ImprovementStep,
    LessonLearned,
    QuantOutput,
    QuantStructureOut,
    ReconcileOutput,
    ResearchOutput,
    ResearchRankedItem,
    RiskAssessment,
    RiskOutput,
    ScalpCandidateOut,
    ScalpOutput,
)

# ---------------------------------------------------------------------------
# Sample data fixtures
# ---------------------------------------------------------------------------

SAMPLE_SCALP_OUTPUT = {
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

SAMPLE_RESEARCH_OUTPUT = {
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

SAMPLE_BROKER_PLAN = {
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
}

SAMPLE_RECONCILE_OUTPUT = {
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


class TestScalpSchema:
    def test_valid_output(self) -> None:
        out = ScalpOutput.model_validate(SAMPLE_SCALP_OUTPUT)
        assert len(out.candidates) == 2
        assert out.candidates[0].ticker == "AAPL"
        assert out.candidates[0].confidence == 0.8

    def test_roundtrip(self) -> None:
        out = ScalpOutput.model_validate(SAMPLE_SCALP_OUTPUT)
        data = json.loads(out.model_dump_json())
        out2 = ScalpOutput.model_validate(data)
        assert out == out2

    def test_requires_sources(self) -> None:
        bad = {**SAMPLE_SCALP_OUTPUT["candidates"][0], "sources": []}
        with pytest.raises(ValidationError, match="sources"):
            ScalpCandidateOut.model_validate(bad)

    def test_confidence_bounds(self) -> None:
        bad = {**SAMPLE_SCALP_OUTPUT["candidates"][0], "confidence": 1.5}
        with pytest.raises(ValidationError, match="confidence"):
            ScalpCandidateOut.model_validate(bad)


class TestResearchSchema:
    def test_valid_output(self) -> None:
        out = ResearchOutput.model_validate(SAMPLE_RESEARCH_OUTPUT)
        assert len(out.shortlist) == 1
        assert out.shortlist[0].rank == 1
        assert out.market_regime == "risk_on"

    def test_roundtrip(self) -> None:
        out = ResearchOutput.model_validate(SAMPLE_RESEARCH_OUTPUT)
        data = json.loads(out.model_dump_json())
        out2 = ResearchOutput.model_validate(data)
        assert out == out2

    def test_rank_must_be_positive(self) -> None:
        bad = {**SAMPLE_RESEARCH_OUTPUT["shortlist"][0], "rank": 0}
        with pytest.raises(ValidationError, match="rank"):
            ResearchRankedItem.model_validate(bad)


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
        plan = BrokerPlan.model_validate(SAMPLE_BROKER_PLAN)
        assert plan.order_type == "limit"
        assert len(plan.improvement_steps) == 3

    def test_roundtrip(self) -> None:
        out = BrokerPlan.model_validate(SAMPLE_BROKER_PLAN)
        data = json.loads(out.model_dump_json())
        out2 = BrokerPlan.model_validate(data)
        assert out == out2

    def test_step_number_positive(self) -> None:
        bad = {"step_number": 0, "price": 3.30, "wait_seconds": 30}
        with pytest.raises(ValidationError, match="step_number"):
            ImprovementStep.model_validate(bad)


class TestReconcileSchema:
    def test_valid_output(self) -> None:
        out = ReconcileOutput.model_validate(SAMPLE_RECONCILE_OUTPUT)
        assert out.daily_pnl == 150.25
        assert out.reconciliation_status == "clean"
        assert len(out.anomalies) == 1
        assert len(out.lessons) == 1

    def test_roundtrip(self) -> None:
        out = ReconcileOutput.model_validate(SAMPLE_RECONCILE_OUTPUT)
        data = json.loads(out.model_dump_json())
        out2 = ReconcileOutput.model_validate(data)
        assert out == out2

    def test_anomaly_validates(self) -> None:
        a = AnomalyReport.model_validate(SAMPLE_RECONCILE_OUTPUT["anomalies"][0])
        assert a.severity == "warning"

    def test_lesson_validates(self) -> None:
        lesson = LessonLearned.model_validate(SAMPLE_RECONCILE_OUTPUT["lessons"][0])
        assert lesson.topic == "Execution slippage"


# ---------------------------------------------------------------------------
# Prompt builder tests — pure functions, return strings
# ---------------------------------------------------------------------------


class TestPromptBuilders:
    """Verify prompt builders are pure functions that return non-empty strings."""

    def test_scalp_builder(self) -> None:
        inp = ScalpInput(
            universe=["AAPL", "NVDA"],
            raw_feeds=["AAPL earnings beat expectations."],
            scan_date="2026-09-27",
        )
        result = build_scalp_prompt(inp)
        assert isinstance(result, str)
        assert len(result) > 100
        assert "Scalp" in result
        assert "AAPL" in result
        assert "broker" in result.lower()  # forbidden actions mentioned

    def test_research_builder(self) -> None:
        inp = ResearchInput(
            candidates_json=json.dumps(SAMPLE_SCALP_OUTPUT),
            regime_features_json='{"regime": "risk_on"}',
            portfolio_summary="3 open positions",
            scan_date="2026-09-27",
        )
        result = build_research_prompt(inp)
        assert isinstance(result, str)
        assert "Research" in result

    def test_quant_builder(self) -> None:
        inp = QuantInput(
            shortlist_json=json.dumps(SAMPLE_RESEARCH_OUTPUT),
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

    def test_scalp_builder_empty_feeds(self) -> None:
        """Builder handles empty feeds gracefully."""
        inp = ScalpInput(
            universe=["SPY"],
            raw_feeds=[],
            scan_date="2026-09-27",
        )
        result = build_scalp_prompt(inp)
        assert "(no feeds)" in result

    def test_builders_are_deterministic(self) -> None:
        """Same input produces same output (pure function)."""
        inp = ScalpInput(
            universe=["AAPL"],
            raw_feeds=["test feed"],
            scan_date="2026-09-27",
        )
        r1 = build_scalp_prompt(inp)
        r2 = build_scalp_prompt(inp)
        assert r1 == r2

    def test_all_prompts_mention_forbidden_broker(self) -> None:
        """Every persona prompt must mention broker prohibition."""
        scalp_inp = ScalpInput(universe=["SPY"], raw_feeds=["x"], scan_date="2026-09-27")
        research_inp = ResearchInput(
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

        for name, builder, inp in [
            ("scalp", build_scalp_prompt, scalp_inp),
            ("research", build_research_prompt, research_inp),
            ("quant", build_quant_prompt, quant_inp),
            ("risk", build_risk_prompt, risk_inp),
        ]:
            prompt = builder(inp)
            assert "broker" in prompt.lower() or "order" in prompt.lower(), (
                f"{name} prompt must mention broker/order prohibition"
            )
