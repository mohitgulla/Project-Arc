"""Persona digest cards (E5.5, arc.slack.digests): layout, escaping, Slack limits."""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

import pytest

from arc.models import Candidate, CatalystType, Stance
from arc.personas.schemas import (
    AnomalyReport,
    AuditorOutput,
    DirectorOutput,
    DirectorRankedItem,
    ImprovementStep,
    InvestorPlan,
    LessonLearned,
    QuantGreeks,
    QuantLeg,
    QuantOutput,
    QuantStructureOut,
    RiskAssessment,
    RiskOutput,
)
from arc.slack import blocks as B
from arc.slack import digests as D
from arc.utils.calendar import ET

EVIL = "<!channel> & <@U1>"
EVIL_ESC = "&lt;!channel&gt; &amp; &lt;@U1&gt;"
LONG = "x" * 5000


def _texts(view: B.CardView) -> list[str]:
    """Every mrkdwn/plain string in the card (section text, fields, context)."""
    out: list[str] = []
    for b in view.blocks:
        if b["type"] == "header":  # plain_text: Slack does not parse mentions there
            continue
        if "text" in b:
            out.append(b["text"]["text"])
        out += [f["text"] for f in b.get("fields", [])]
        out += [e["text"] for e in b.get("elements", []) if "text" in e]
    return out


def _all(view: B.CardView) -> str:
    return "\n".join(_texts(view))


def _assert_slack_limits(view: B.CardView) -> None:
    assert len(view.blocks) <= B.MAX_BLOCKS
    assert view.blocks[0]["type"] == "header"
    assert len(view.blocks[0]["text"]["text"]) <= B.HEADER_MAX
    for b in view.blocks:
        if b["type"] == "section":
            assert len(b.get("fields", [])) <= 10
            assert all(len(f["text"]) <= 2000 for f in b.get("fields", []))
            if "text" in b:
                assert len(b["text"]["text"]) <= 3000
    json.dumps(view.blocks)  # serialisable as posted


def _footer(view: B.CardView) -> str:
    last = view.blocks[-1]
    assert last["type"] == "context"
    return str(last["elements"][0]["text"])


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def cand(ticker: str = "NVDA", **kw: Any) -> Candidate:
    data: dict[str, Any] = {
        "ticker": ticker,
        "stance": Stance.BULLISH,
        "catalyst_type": CatalystType.EARNINGS,
        "catalyst_date": dt.datetime(2026, 10, 28, tzinfo=ET),
        "confidence": 0.75,
        "sources": ["https://www.sec.gov/x.htm", "https://example.com/a"],
        "created_at": dt.datetime(2026, 9, 28, 22, tzinfo=ET),
    }
    data.update(kw)
    return Candidate(**data)


def ranked(ticker: str = "SPY", **kw: Any) -> DirectorRankedItem:
    data: dict[str, Any] = {
        "ticker": ticker,
        "rank": 1,
        "thesis": "Range-bound into FOMC.",
        "regime_context": "Low realised vol.",
        "suggested_structure_type": "iron_condor",
        "stance": "neutral",
        "confidence": 0.7,
    }
    data.update(kw)
    return DirectorRankedItem(**data)


def leg(strike: float, side: str, kind: str, expiry: str = "2026-10-30") -> QuantLeg:
    return QuantLeg(
        occ_symbol=f"SPY261030{kind[0].upper()}{int(strike * 1000):08d}",
        side=side,
        strike=strike,
        expiry=expiry,
        option_type=kind,
    )


def condor(**kw: Any) -> QuantStructureOut:
    data: dict[str, Any] = {
        "ticker": "SPY",
        "structure_type": "iron_condor",
        "legs": [
            leg(740, "long", "put"),
            leg(745, "short", "put"),
            leg(798, "short", "call"),
            leg(803, "long", "call"),
        ],
        "net_debit_credit": -1.66,
        "max_gain": 165.55,
        "max_loss": 334.45,
        "breakevens": [743.34, 799.66],
        "greeks": QuantGreeks(delta=-2.0, gamma=-0.24, vega=-1711.0, theta=3.14),
        "dte": 35,
        "pop": 0.62,
        "ev_per_contract": -21.78,
        "cost_bps": 386,
        "confidence": 0.7,
        "rationale": "Balanced deltas.",
    }
    data.update(kw)
    return QuantStructureOut(**data)


def assessment(**kw: Any) -> RiskAssessment:
    data: dict[str, Any] = {
        "ticker": "SPY",
        "structure_type": "iron_condor",
        "risk_rating": "moderate",
        "concentration_warning": False,
        "greek_budget_impact": "Small short vega.",
        "calendar_concerns": "FOMC inside the trade.",
        "sizing_suggestion": 20,
        "max_loss_pct_equity": 0.067,
        "narrative": "Defined-risk condor.",
    }
    data.update(kw)
    return RiskAssessment(**data)


def plan(**kw: Any) -> InvestorPlan:
    data: dict[str, Any] = {
        "ticker": "SPY",
        "structure_type": "iron_condor",
        "order_type": "limit",
        "initial_limit_price": -1.25,
        "improvement_steps": [
            ImprovementStep(step_number=1, price=-1.22, wait_seconds=30),
            ImprovementStep(step_number=2, price=-1.20, wait_seconds=30),
        ],
        "timeout_seconds": 120,
        "contracts": 3,
        "notes": "Start at mid.",
    }
    data.update(kw)
    return InvestorPlan(**data)


def journal(**kw: Any) -> AuditorOutput:
    data: dict[str, Any] = {
        "journal_date": "2026-09-28",
        "daily_pnl": 312.0,
        "open_positions": 2,
        "closed_today": 1,
        "fills_reviewed": 3,
        "anomalies": [
            AnomalyReport(
                category="fill_discrepancy",
                severity="warning",
                description="Fill worse than mid.",
                affected_orders=["o-1"],
            )
        ],
        "lessons": [
            LessonLearned(topic="Timing", observation="Mid moved.", recommendation="Wait less.")
        ],
        "journal_narrative": "Quiet day.",
        "reconciliation_status": "clean",
    }
    data.update(kw)
    return AuditorOutput(**data)


# ---------------------------------------------------------------------------
# Scout
# ---------------------------------------------------------------------------


class TestScout:
    def test_title_rows_rejects_and_footer(self) -> None:
        view = D.scout_card(
            docs=12,
            accepted=3,
            candidates=[cand(), cand("XOM", stance=Stance.BEARISH, catalyst_date=None)],
            rejected={"not_in_universe": 2, "schema": 1},
            rejected_items={"not_in_universe": ["PLTR", "AAPL"], "schema": ["TSLA"]},
            run_id="run-1",
            chain_run_id="chain-1",
        )
        assert view.text == view.blocks[0]["text"]["text"] == "[Scout] Scan: 12 docs → 2 candidates"
        text = _all(view)
        assert "*3* accepted this run · 3 rejected" in text
        assert (
            "• *NVDA* bullish · earnings Oct 28 · conf 75% · "
            "<https://www.sec.gov/x.htm|sec.gov> <https://example.com/a|example.com>"
        ) in text
        assert "• *XOM* bearish · earnings · conf 75%" in text
        assert "• not in universe (2): PLTR, AAPL" in text
        assert "• invalid reply (1): TSLA" in text
        assert _footer(view) == "run `run-1` · chain `chain-1`"
        _assert_slack_limits(view)

    def test_empty_run_and_failed_batches(self) -> None:
        view = D.scout_card(docs=1, accepted=0, candidates=[], rejected={}, failed_batches=2)
        text = _all(view)
        assert view.text == "[Scout] Scan: 1 doc → 0 candidates"
        assert "*Candidates today*\nnone" in text
        assert ":warning: 2 failed batches" in text
        assert "Rejected" not in text
        assert _footer(view) == " "

    def test_escapes_sources_and_rejected_names(self) -> None:
        view = D.scout_card(
            docs=1,
            accepted=1,
            candidates=[
                cand(sources=["https://ex.com/a?b=<x>|y", "doc-<b>"] + ["https://e.com/c"] * 3)
            ],
            rejected={"schema": 1},
            rejected_items={"schema": [EVIL]},
        )
        text = _all(view)
        assert "<https://ex.com/a?b=&lt;x&gt;%7Cy|ex.com>" in text
        assert "doc-&lt;b&gt;" in text
        assert "+2" in text  # only the first three sources are linked
        assert EVIL_ESC in text and "<!channel>" not in text

    def test_many_candidates_clip_under_section_limit(self) -> None:
        many = [cand(f"T{i}", sources=[f"https://example.com/{'z' * 80}/{i}"]) for i in range(80)]
        view = D.scout_card(docs=80, accepted=80, candidates=many, rejected={})
        _assert_slack_limits(view)
        assert any(t.endswith("…") for t in _texts(view))


# ---------------------------------------------------------------------------
# Director
# ---------------------------------------------------------------------------


class TestDirector:
    def test_picks_and_dropped(self) -> None:
        out = DirectorOutput(
            shortlist=[
                ranked(),
                ranked(
                    "NVDA", rank=2, stance="bullish", suggested_structure_type="vertical_spread"
                ),
            ],
            market_regime="risk_on",
            session_notes="Two setups.",
        )
        view = D.director_card(
            out,
            candidates=5,
            dropped=[("AAPL", "not_a_candidate"), ("XOM", "not_picked"), ("TSLA", "not_picked")],
            run_id="r",
            chain_run_id="c",
        )
        assert view.text == "[Director] Shortlist: 2 of 5 • market risk_on"
        text = _all(view)
        assert (
            "*[Director] SPY*\nrank 1 · neutral · confidence 70% · Iron Condor\n"
            "Range-bound into FOMC.\n_Regime:_ Low realised vol."
        ) in text
        assert "rank 2 · bullish · confidence 70% · Vertical Spread" in text
        assert "• not a Scout candidate (1): AAPL" in text
        assert "• not picked by Director (2): XOM, TSLA" in text
        assert "*[Director] Session notes*\nTwo setups." in text
        assert _footer(view) == "run `r` · chain `c`"
        _assert_slack_limits(view)

    def test_empty_shortlist_and_escaping(self) -> None:
        out = DirectorOutput(shortlist=[], market_regime=EVIL, session_notes=EVIL)
        view = D.director_card(out, candidates=0)
        text = _all(view)
        assert "nothing worth trading today" in text
        assert EVIL_ESC in text and "<!channel>" not in text
        assert "<!channel>" not in view.text  # mrkdwn fallback is escaped
        # The header is plain_text (Slack does not parse mentions there).
        assert view.blocks[0]["text"]["type"] == "plain_text"

    def test_long_thesis_is_clipped(self) -> None:
        out = DirectorOutput(shortlist=[ranked(thesis=LONG)], market_regime="", session_notes=LONG)
        view = D.director_card(out, candidates=1)
        assert view.text.endswith("market unknown")
        _assert_slack_limits(view)


# ---------------------------------------------------------------------------
# Quant
# ---------------------------------------------------------------------------


class TestQuant:
    def test_structure_legs_facts_and_drops(self) -> None:
        out = QuantOutput(structures=[condor()], analysis_notes="Menu #2.")
        view = D.quant_card(
            out,
            dropped={"not_in_menu": 1},
            dropped_items=[("SPY", "not_in_menu")],
            no_chain=["XOM"],
            run_id="r",
            chain_run_id="c",
        )
        assert view.text == "[Quant] Structures: SPY Iron Condor • PoP 62% • EV -$21.78"
        text = _all(view)
        assert (
            "*SPY Iron Condor · Oct 30 (35 DTE)*\n```LONG   1x      740P\nSHORT  1x      745P\n"
            "SHORT  1x      798C\nLONG   1x      803C```"
        ) in text
        assert "*Net*\nCredit 1.66/sh ($166)" in text
        assert "*Payoff*\nMax gain $165.55\nMax loss $334.45" in text
        assert "*Edge*\nPoP 62%\nEV -$21.78/contract\nCost 386 bps" in text
        assert "*Breakevens*\n743.34 / 799.66" in text
        assert "ν -17.11 $/vol pt · Θ +3.14 $/day" in text
        assert "*Confidence*\n70%" in text
        assert "*[Quant] Rationale*\nBalanced deltas." in text
        assert "• not in the scanner menu (1): SPY" in text
        assert "• no tradable chain (1): XOM" in text
        assert "*[Quant] Analysis*\nMenu #2." in text
        _assert_slack_limits(view)

    @pytest.mark.parametrize(
        ("kw", "name"),
        [
            (
                {
                    "structure_type": "vertical_spread",
                    "legs": [leg(745, "short", "put"), leg(740, "long", "put")],
                },
                "Put Credit Spread",
            ),
            (
                {
                    "structure_type": "vertical_spread",
                    "net_debit_credit": 2.1,
                    "legs": [leg(760, "long", "call"), leg(765, "short", "call")],
                },
                "Call Debit Spread",
            ),
            ({"structure_type": "long_put", "legs": [leg(740, "long", "put")]}, "Long Put"),
            ({"structure_type": "strangle"}, "Custom"),
        ],
    )
    def test_structure_names(self, kw: dict[str, Any], name: str) -> None:
        assert D.structure_name(condor(**kw)) == name

    def test_multi_expiry_and_more_and_none(self) -> None:
        cal = condor(legs=[leg(745, "short", "put"), leg(745, "long", "put", "2026-11-20")])
        view = D.quant_card(QuantOutput(structures=[cal, condor()], analysis_notes=""))
        assert "+1 more" in view.text
        assert "Nov 20" in _all(view)
        none = D.quant_card(QuantOutput(structures=[], analysis_notes=""))
        assert none.text == "[Quant] Structures: none chosen"

    def test_escaping_and_clip(self) -> None:
        out = QuantOutput(structures=[condor(rationale=EVIL + LONG)], analysis_notes=EVIL)
        view = D.quant_card(out)
        assert "<!channel>" not in _all(view) and EVIL_ESC in _all(view)
        _assert_slack_limits(view)

    def test_bad_expiry_falls_back_to_dte(self) -> None:
        s = condor(legs=[leg(740, "long", "put", "soon"), leg(745, "short", "put", "later")])
        view = D.quant_card(QuantOutput(structures=[s], analysis_notes=""))
        assert "SPY Iron Condor · 35 DTE" in _all(view)
        assert "soon" in _all(view)

    def test_more_than_ten_fields_spill_over(self) -> None:
        # Many structures: fact grids stay <= 10 fields per section and <= 50 blocks.
        out = QuantOutput(structures=[condor(ticker=f"T{i}") for i in range(12)], analysis_notes="")
        view = D.quant_card(out)
        _assert_slack_limits(view)
        assert len(view.blocks) == B.MAX_BLOCKS


# ---------------------------------------------------------------------------
# Risk
# ---------------------------------------------------------------------------


class TestRisk:
    def test_review(self) -> None:
        out = RiskOutput(
            assessments=[assessment(), assessment(ticker="NVDA", concentration_warning=True)],
            portfolio_summary="Flat book.",
            advisory_notes="Mind FOMC.",
        )
        view = D.risk_card(
            out,
            dropped={"unknown_structure": 1},
            dropped_items=[("QQQ iron_condor", "unknown_structure")],
            not_assessed=["XOM vertical_spread"],
            run_id="r",
            chain_run_id="c",
        )
        assert view.text == "[Risk] Review: SPY moderate +1 more • suggests 20"
        text = _all(view)
        assert "*SPY Iron Condor*\nRating *moderate*" in text
        assert "*Suggested size*\n20 contract(s) (advisory)" in text
        assert "*Max loss*\n6.7% of equity" in text
        assert "*Concentration*\n:warning: over limit" in text
        assert ":warning: 1 concentration" in text and "1 not assessed" in text
        assert (
            "*[Risk] SPY review*\n• _Greek budget:_ Small short vega.\n"
            "• _Calendar:_ FOMC inside the trade.\nDefined-risk condor."
        ) in text
        assert "*[Risk] Portfolio*\nFlat book." in text
        assert "*[Risk] Advisory*\nMind FOMC." in text
        assert "• structure Quant did not propose (1): QQQ iron_condor" in text
        assert "• not assessed by Risk (1): XOM vertical_spread" in text
        assert _footer(view) == "run `r` · chain `c`"
        _assert_slack_limits(view)

    def test_nothing_assessed_and_escaping(self) -> None:
        view = D.risk_card(RiskOutput(assessments=[], portfolio_summary=EVIL, advisory_notes=""))
        assert view.text == "[Risk] Review: nothing assessed"
        assert EVIL_ESC in _all(view) and "<!channel>" not in _all(view)

    def test_long_narrative_is_clipped(self) -> None:
        a = assessment(narrative=LONG, greek_budget_impact=EVIL)
        view = D.risk_card(RiskOutput(assessments=[a], portfolio_summary=LONG, advisory_notes=""))
        _assert_slack_limits(view)
        assert EVIL_ESC in _all(view)


# ---------------------------------------------------------------------------
# Investor
# ---------------------------------------------------------------------------


class TestInvestor:
    def test_plan_only(self) -> None:
        view = D.investor_card(plan(), run_id="r")
        assert view.text == "[Investor] Order: SPY Iron Condor • x3 • limit -1.25"
        text = _all(view)
        assert "limit order · 2 improvement steps · timeout 120s" in text
        assert (
            "*Order plan*\n• start -1.25 (mid)\n"
            "• step 1: -1.22 (wait 30s)\n• step 2: -1.20 (wait 30s)"
        ) in text
        assert "Result" not in text
        assert "*[Investor] Notes*\nStart at mid." in text
        _assert_slack_limits(view)

    def test_fill_with_slippage(self) -> None:
        res = D.ExecutionResult(
            status="filled",
            filled_qty=3,
            fill_price=-1.22,
            mid_at_submit=-1.25,
            steps_used=1,
            detail="Filled at step 1.",
        )
        view = D.investor_card(plan(), res, chain_run_id="c")
        assert view.text.endswith(" • filled")
        text = _all(view)
        assert "*Result*\n:white_check_mark: filled" in text
        assert "*Filled*\n3 of 3" in text
        assert "*Fill price*\n-1.22" in text
        assert "*Slippage vs mid*\n+0.03/sh (+$9.00 total, + = cost)" in text
        assert "*Steps used*\n1 of 2" in text
        assert "Filled at step 1." in text
        assert res.slippage == pytest.approx(0.03)

    def test_cancel_without_fill(self) -> None:
        res = D.ExecutionResult(status="cancelled", steps_used=2, detail=EVIL)
        view = D.investor_card(plan(), res)
        text = _all(view)
        assert view.text.endswith(" • cancelled")
        assert ":x: cancelled" in text and "*Filled*\n0 of 3" in text
        assert "Slippage" not in text and "Fill price" not in text
        assert res.slippage is None
        assert EVIL_ESC in text

    def test_execution_result_is_strict(self) -> None:
        with pytest.raises(ValueError):
            D.ExecutionResult.model_validate({"status": "filled", "venue": "x"})


# ---------------------------------------------------------------------------
# Auditor
# ---------------------------------------------------------------------------


class TestAuditor:
    def test_journal(self) -> None:
        view = D.auditor_card(journal(), run_id="r")
        assert view.blocks[0]["text"]["text"] == "[Auditor] Journal: Sep 28 • P&L +$312 • 1 anomaly"
        assert view.text == "[Auditor] Journal: Sep 28 • P&amp;L +$312 • 1 anomaly"
        text = _all(view)
        assert "reconciliation :white_check_mark: *clean*" in text
        assert "*Daily P&L*\n+$312.00" in text
        assert "*Open positions*\n2" in text and "*Closed today*\n1" in text
        assert "*Fills reviewed*\n3" in text
        assert "• *warning* fill_discrepancy: Fill worse than mid. (orders o-1)" in text
        assert "• *Timing*: Mid moved. → _Wait less._" in text
        assert "*[Auditor] Journal*\nQuiet day." in text
        assert _footer(view) == "run `r`"
        _assert_slack_limits(view)

    def test_loss_no_anomalies_discrepancy(self) -> None:
        view = D.auditor_card(
            journal(
                daily_pnl=-45.5,
                anomalies=[],
                lessons=[],
                reconciliation_status="discrepancies_found",
                journal_date="bad",
                journal_narrative=EVIL,
            )
        )
        assert view.blocks[0]["text"]["text"] == "[Auditor] Journal: bad • P&L -$46 • 0 anomalies"
        text = _all(view)
        assert ":warning: *discrepancies_found*" in text
        assert "*Daily P&L*\n-$45.50" in text
        assert "Anomalies" not in text and "Lessons" not in text
        assert EVIL_ESC in text

    def test_many_anomalies_are_clipped(self) -> None:
        many = [
            AnomalyReport(category="other", severity="info", description=LONG) for _ in range(5)
        ]
        view = D.auditor_card(journal(anomalies=many))
        assert view.text.endswith("5 anomalies")
        _assert_slack_limits(view)


def test_unknown_reason_key_is_humanised() -> None:
    view = D.risk_card(
        RiskOutput(assessments=[], portfolio_summary="", advisory_notes=""),
        dropped={"weird_reason": 2},
    )
    assert "• weird reason (2)" in _all(view)
