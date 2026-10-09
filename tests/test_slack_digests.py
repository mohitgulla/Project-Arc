"""Persona digest cards (E5.5, arc.slack.digests): layout, escaping, Slack limits."""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

import pytest

from arc.models import Candidate, CatalystType, Stance
from arc.personas.schemas import (
    AnomalyReport,
    BrokerPlan,
    ImprovementStep,
    LessonLearned,
    QuantGreeks,
    QuantLeg,
    QuantOutput,
    QuantStructureOut,
    ReconcileOutput,
    ResearchOutput,
    ResearchRankedItem,
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


def ranked(ticker: str = "SPY", **kw: Any) -> ResearchRankedItem:
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
    return ResearchRankedItem(**data)


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


def plan(**kw: Any) -> BrokerPlan:
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
    return BrokerPlan(**data)


def journal(**kw: Any) -> ReconcileOutput:
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
    return ReconcileOutput(**data)


# ---------------------------------------------------------------------------
# Scalp
# ---------------------------------------------------------------------------


class TestScalp:
    def test_title_rows_rejects_and_footer(self) -> None:
        view = D.scalp_card(
            docs=12,
            accepted=3,
            candidates=[cand(), cand("XOM", stance=Stance.BEARISH, catalyst_date=None)],
            rejected={"not_in_universe": 2, "schema": 1},
            rejected_items={"not_in_universe": ["PLTR", "AAPL"], "schema": ["TSLA"]},
            rationales={"NVDA": "Buyback plus raised guidance."},
            run_id="run-1",
            chain_run_id="chain-1",
        )
        assert (
            view.text
            == view.blocks[0]["text"]["text"]
            == "⚡ [Scalp] Scan: 12 Sources → 2 Candidates"
        )
        text = _all(view)
        assert "*3* accepted this run · 3 rejected" in text
        # D65: one line per candidate, grouped by stance; no line break per ticker.
        sections = [
            b["text"]["text"]
            for b in view.blocks
            if b["type"] == "section" and b["text"]["text"].startswith("*")
        ]
        assert sections[0] == (
            "*Bullish (1)*\n*NVDA* 75% confidence · earnings Oct 28 — Buyback plus raised guidance."
        )
        assert sections[1] == "*Bearish (1)*\n*XOM* 75% confidence · earnings"
        kinds = [b["type"] for b in view.blocks]
        assert kinds[2:6] == ["divider", "section", "divider", "section"]
        assert not any(line.startswith(" ") for t in _texts(view) for line in t.split("\n")), (
            "no line in any block starts with a space"
        )
        assert "•" not in sections[0] and "•" not in sections[1]
        assert "source" not in text.lower().replace("sources →", "")
        assert "http" not in text  # owner: no source links on the Scalp card
        assert "• not in universe (2): PLTR, AAPL" in text
        assert "• invalid reply (1): TSLA" in text
        assert _footer(view) == "run `run-1` · chain `chain-1`"
        _assert_slack_limits(view)

    def test_empty_run_and_failed_batches(self) -> None:
        view = D.scalp_card(docs=1, accepted=0, candidates=[], rejected={}, failed_batches=2)
        text = _all(view)
        assert view.text == "⚡ [Scalp] Scan: 1 Source → 0 Candidates"
        assert "*Candidates*\nnone" in text
        assert ":warning: 2 failed batches" in text
        assert "Rejected" not in text
        assert "Source mix" not in text and "stor" not in text  # pre-D30 callers unchanged
        assert _footer(view) == " "

    def test_source_mix_story_count_and_corroboration(self) -> None:
        """D30: source mix fact with over-budget counts, story count, distinct sources."""
        view = D.scalp_card(
            docs=70,
            accepted=1,
            candidates=[cand(corroboration=3)],
            rejected={},
            source_mix=[("WSJ", 12, 46), ("CNBC", 12, 0), ("Fed", 3, 0), ("EDGAR", 24, 157)],
            stories=41,
        )
        text = _all(view)
        assert "41 stories" in text
        assert (
            "*Source mix*\nWSJ 12 (46 over budget) · CNBC 12 · Fed 3 · EDGAR 24 (157 over budget)"
            in text
        )
        assert "*NVDA* 75% confidence · earnings Oct 28 · 3 sources" in text
        _assert_slack_limits(view)
        one = _all(
            D.scalp_card(
                docs=1, accepted=0, candidates=[cand(corroboration=1)], rejected={}, stories=1
            )
        )
        assert "1 story" in one and "· 1 source" in one

    def test_escapes_rationale_and_rejected_names(self) -> None:
        view = D.scalp_card(
            docs=1,
            accepted=1,
            candidates=[
                cand(sources=["https://ex.com/a?b=<x>|y", "doc-<b>"] + ["https://e.com/c"] * 3)
            ],
            rejected={"schema": 1},
            rejected_items={"schema": [EVIL]},
            rationales={"NVDA": EVIL},
        )
        text = _all(view)
        assert "ex.com" not in text and "doc-" not in text  # sources are counted, not shown
        assert text.count(EVIL_ESC) == 2 and "<!channel>" not in text

    def test_many_candidates_clip_under_block_limit(self) -> None:
        # D65: rows are lines in one stance section, so a long list is clipped to
        # 40 rows plus a "+N more" line, and the whole card stays under 50 blocks.
        many = [cand(f"T{i}", sources=[f"https://example.com/{'z' * 80}/{i}"]) for i in range(80)]
        view = D.scalp_card(docs=80, accepted=80, candidates=many, rejected={"schema": 1})
        _assert_slack_limits(view)
        contexts = [b["elements"][0]["text"] for b in view.blocks if b["type"] == "context"]
        assert any(t.startswith("+40 more: T40, T41") for t in contexts)
        assert sum(b["type"] == "divider" for b in view.blocks) == 2  # one stance + Rejected
        assert view.blocks[-1]["type"] == "context"  # footer stays last

    def test_ten_candidates_fit_without_clipping(self) -> None:
        many = [cand(f"T{i}") for i in range(10)]
        view = D.scalp_card(docs=10, accepted=10, candidates=many, rejected={})
        _assert_slack_limits(view)
        assert "more" not in _all(view)
        assert sum(b["type"] == "divider" for b in view.blocks) == 1  # D65: one stance group
        assert "*Bullish (10)*" in _all(view)

    def test_long_rationale_is_clipped(self) -> None:
        view = D.scalp_card(
            docs=1, accepted=1, candidates=[cand()], rejected={}, rationales={"NVDA": LONG}
        )
        _assert_slack_limits(view)
        assert any(t.endswith("…") for t in _texts(view))


# ---------------------------------------------------------------------------
# Research
# ---------------------------------------------------------------------------


def _ctx_entry(ticker: str, stance: str, conf: float, at: dt.datetime, **payload: Any) -> Any:
    from arc.context.store import ContextEntry

    return ContextEntry(
        id=f"ctx-{ticker}",
        kind="candidate",
        subject=ticker,
        payload={"stance": stance, "confidence": conf, **payload},
        schema_version=1,
        produced_by="scalp",
        run_id=f"run-{at:%H%M}",
        created_at=at,
        valid_from=at,
    )


class TestScalpContext:
    """D65: the loop-thread Scalp card is one line per ticker, grouped by stance."""

    def test_grouped_one_line_per_ticker(self) -> None:
        run = dt.datetime(2026, 10, 8, 15, 30, tzinfo=ET)
        entries = [
            _ctx_entry(
                "TSM",
                "bullish",
                0.72,
                run,
                catalyst_type="news",
                catalyst_date="2026-10-08T00:00:00-04:00",
                corroboration=4,
            ),
            _ctx_entry("ORCL", "bearish", 0.62, run, catalyst_type="news", corroboration=2),
            _ctx_entry(
                "AMD",
                "bullish",
                0.50,
                run - dt.timedelta(minutes=90),
                catalyst_type="sector",
                catalyst_date="2026-10-15",
                corroboration=1,
            ),
            _ctx_entry("NVDA", "neutral", 0.48, run, catalyst_type="news", corroboration=6),
        ]
        view = D.scalp_context_card(entries, chain_run_id="chain-1")
        _assert_slack_limits(view)
        assert view.text == "⚡ [Scalp] Context: 4 Candidates • run 2026-10-08 15:30ET"
        sections = [b["text"]["text"] for b in view.blocks if b["type"] == "section"]
        assert sections == [
            "*Bullish (2)*\n*TSM* 72% confidence · news · 4 sources\n"
            "*AMD* 50% confidence · sector Oct 15 · 1 source",  # D65: no per-row "as of"
            "*Bearish (1)*\n*ORCL* 62% confidence · news · 2 sources",
            "*Neutral (1)*\n*NVDA* 48% confidence · news · 6 sources",
        ]
        assert sum(b["type"] == "divider" for b in view.blocks) == 3  # one per stance

    def test_past_catalyst_dates_are_hidden(self) -> None:
        # D65 (owner): "news Oct 08" on an Oct 9 card is noise; only upcoming dates show.
        run = dt.datetime(2026, 10, 9, 10, 30, tzinfo=ET)
        entries = [
            _ctx_entry(
                "BA",
                "bullish",
                0.62,
                run,
                catalyst_type="news",
                catalyst_date="2026-10-08",
                corroboration=4,
            ),
            _ctx_entry(
                "MU",
                "bullish",
                0.55,
                run - dt.timedelta(days=1),
                catalyst_type="earnings",
                catalyst_date="2026-10-28",
                corroboration=2,
            ),
        ]
        text = _all(D.scalp_context_card(entries, chain_run_id="chain-1"))
        assert "*BA* 62% confidence · news · 4 sources" in text
        assert "*MU* 55% confidence · earnings Oct 28 · 2 sources" in text
        assert "as of" not in text and "Oct 08" not in text

    def test_scan_card_hides_past_catalyst_dates(self) -> None:
        past = cand(
            "BA", catalyst_type=CatalystType.NEWS, catalyst_date=dt.datetime(2020, 1, 2, tzinfo=ET)
        )
        text = _all(D.scalp_card(docs=1, accepted=1, candidates=[past], rejected={}))
        assert "Jan 02" not in text and "*BA*" in text

    def test_empty(self) -> None:
        view = D.scalp_context_card([], chain_run_id="chain-1")
        assert "*Candidates*\nnone" in _all(view)


class TestResearch:
    def test_picks_and_dropped(self) -> None:
        out = ResearchOutput(
            shortlist=[
                ranked(),
                ranked(
                    "NVDA", rank=2, stance="bullish", suggested_structure_type="vertical_spread"
                ),
            ],
            market_regime="risk_on",
            session_notes="Two setups.",
        )
        view = D.research_card(
            out,
            candidates=5,
            dropped=[("AAPL", "not_a_candidate"), ("XOM", "not_picked"), ("TSLA", "not_picked")],
            evidence={"SPY": "Scalp neutral · macro catalyst Oct 28"},
            run_id="r",
            chain_run_id="c",
        )
        assert view.text == "🧠 [Research] Ranked: 2 / 5 • Market Risk ON"
        text = _all(view)
        assert (
            "*SPY*\nRank 1 · Neutral · 70% confidence · Iron Condor\n"
            "*Thesis:* Range-bound into FOMC.\n*Regime:* Low realised vol.\n"
            "*Evidence:* Scalp neutral · macro"
        ) in text
        assert "Rank 2 · Bullish · 70% confidence · Vertical Spread" in text
        assert "🧠 [Research] SPY" not in text and "_Regime:_" not in text
        assert "• not a Scalp candidate (1): AAPL" in text
        assert "• not ranked or excluded by Research (2): XOM, TSLA" in text
        assert "*🧠 [Research] Session notes*\nTwo setups." in text
        assert _footer(view) == "run `r` · chain `c`"
        _assert_slack_limits(view)

    def test_budget_splits_ranked_list(self) -> None:
        """E5.7: every ranked name is shown; past the budget they are listed, not carded."""
        out = ResearchOutput(
            shortlist=[
                ranked(evidence=["8-K buyback", "IV rank 18"]),
                ranked("NVDA", rank=2, stance="bullish"),
                ranked("PLTR", rank=3, stance="bullish", thesis="Contract win."),
            ],
            excluded=[{"ticker": "XOM", "reason": "Crude already priced."}],  # type: ignore[list-item]
            market_regime="risk_on",
            session_notes="",
        )
        view = D.research_card(
            out,
            candidates=4,
            funnel=[("XOM", "excluded", "Crude already priced.")],
            budget=2,
        )
        assert view.text == "🧠 [Research] Ranked: 3 / 4 • Market Risk ON"
        text = _all(view)
        assert "*SPY*\nRank 1" in text and "*NVDA*\nRank 2" in text
        assert "*PLTR*\nRank 3" not in text
        assert "Ranked, not structured (1, over the budget of 2)" in text
        assert "#3 *PLTR* · Bullish · 70% · Contract win." in text
        assert "*Research evidence:* 8-K buyback · IV rank 18" in text
        # E13.13 (D56): no excluded list on the card
        assert "XOM" not in text and "Crude already priced." not in text and "Excluded" not in text
        _assert_slack_limits(view)

    def test_empty_shortlist_and_escaping(self) -> None:
        out = ResearchOutput(shortlist=[], market_regime=EVIL, session_notes=EVIL)
        view = D.research_card(out, candidates=0)
        text = _all(view)
        assert "nothing worth trading today" in text
        assert EVIL_ESC in text and "<!channel>" not in text
        assert "<!channel>" not in view.text  # mrkdwn fallback is escaped
        # The header is plain_text (Slack does not parse mentions there).
        assert view.blocks[0]["text"]["type"] == "plain_text"

    def test_long_thesis_is_clipped(self) -> None:
        out = ResearchOutput(shortlist=[ranked(thesis=LONG)], market_regime="", session_notes=LONG)
        view = D.research_card(out, candidates=1)
        assert view.text.endswith("Market Unknown")
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
        assert view.text == "🤺 [Quant] Structures: SPY Iron Condor • PoP 62% • EV -$21.78"
        text = _all(view)
        assert (
            "*SPY Iron Condor · Oct 30 (35 DTE)*\nLong 1x 740P\nShort 1x 745P\n"
            "Short 1x 798C\nLong 1x 803C\n"  # E5.5b: blank line between legs and metrics
        ) in text
        assert "```" not in text  # legs are plain lines, like the proposal card
        # E5.5b: every metric category ends with a blank line ("\n\n" once the
        # fields are joined), so categories never run together on mobile.
        fields = [f["text"] for b in view.blocks for f in b.get("fields", [])]
        assert len(fields) == 6
        assert all(f.endswith("\n") and not f.endswith("\n\n") for f in fields)
        assert "\n".join(fields).count("\n\n") == 5  # one blank line between each category
        assert "*Entry (1 contract)*\nCredit 1.66/sh\n$166 per contract\n" in fields
        assert "*Payoff*\nMax gain $165.55\nMax loss $334.45\nRisk/Reward 2.02 : 1\n" in fields
        assert (
            "*Edge (hold to expiry)*\nPoP 62%\nEV -$21.78 per contract\nCost 386 bps round trip\n"
        ) in fields
        assert "*Breakevens*\n743.34\n799.66\n" in fields
        assert (
            "*Greeks (1 contract)*\nΔ Delta -2.0 sh\nΓ Gamma -0.24 sh\n"
            "ν Vega -$17.11 / vol pt\nΘ Theta +$3.14 / day\n"
        ) in fields
        assert "*Confidence*\n70%\n" in fields
        assert "*🤺 [Quant] Rationale*\nBalanced deltas." in text
        assert "• not in the scanner menu (1): SPY" in text
        assert "• no tradable chain (1): XOM" in text
        assert "*🤺 [Quant] Analysis*\nMenu #2." in text
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
        assert none.text == "🤺 [Quant] Structures: none chosen"

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
        assert view.text == "🛡️ [Risk] Review: SPY Moderate +1 more • Suggests 20"
        text = _all(view)
        assert "*SPY Iron Condor*\nRating *Moderate*" in text
        assert "*Size*\nSuggested 20 (advisory)" in text
        assert "*Max loss*\n6.7% of equity (Risk estimate)" in text
        assert "*Concentration*\n:warning: over limit" in text
        assert ":warning: 1 concentration" in text and "1 not assessed" in text
        assert (
            "*🛡️ [Risk] SPY review*\n*Greek budget:* Small short vega.\n"
            "*Calendar:* FOMC inside the trade.\n*Risks:* Defined-risk condor."
        ) in text
        assert "*🛡️ [Risk] Portfolio*\nFlat book." in text
        assert "*🛡️ [Risk] Advisory*\nMind FOMC." in text
        assert "• structure Quant did not propose (1): QQQ iron_condor" in text
        assert "• not assessed by Risk (1): XOM vertical_spread" in text
        assert _footer(view) == "run `r` · chain `c`"
        _assert_slack_limits(view)

    def test_sized_by_d18_cap(self) -> None:
        from decimal import Decimal

        from arc.sizing import size_contracts

        out = RiskOutput(
            assessments=[assessment(), assessment(ticker="XOM", sizing_suggestion=0)],
            portfolio_summary="",
            advisory_notes="",
        )
        eq = Decimal(100_000)
        sized = {
            ("SPY", "iron_condor"): size_contracts(
                suggestion=20, max_loss_per_contract=Decimal("334.45"), equity=eq, cap_pct=0.05
            ),
            ("XOM", "iron_condor"): size_contracts(
                suggestion=0, max_loss_per_contract=Decimal(300), equity=eq, cap_pct=0.05
            ),
        }
        view = D.risk_card(
            out, sized=sized, max_gain={("SPY", "iron_condor"): 165.55}, cap_pct=0.05
        )
        assert view.text == "🛡️ [Risk] Review: SPY Moderate +1 more • 14 Contracts"
        text = _all(view)
        assert "*Size*\nSuggested 20\nSized 14 (capped by 5% cap)" in text
        assert (
            "*Payoff (sized)*\nMax gain $2,317.70\nMax loss $4,682.30\n4.68% of equity at risk"
        ) in text
        assert "*Size*\nSuggested 0\nNo trade: Risk suggested 0 contracts" in text
        assert "_" not in text.replace("iron_condor", "")  # no italics anywhere

    def test_nothing_assessed_and_escaping(self) -> None:
        view = D.risk_card(RiskOutput(assessments=[], portfolio_summary=EVIL, advisory_notes=""))
        assert view.text == "🛡️ [Risk] Review: nothing assessed"
        assert EVIL_ESC in _all(view) and "<!channel>" not in _all(view)

    def test_long_narrative_is_clipped(self) -> None:
        a = assessment(narrative=LONG, greek_budget_impact=EVIL)
        view = D.risk_card(RiskOutput(assessments=[a], portfolio_summary=LONG, advisory_notes=""))
        _assert_slack_limits(view)
        assert EVIL_ESC in _all(view)


# ---------------------------------------------------------------------------
# Broker orders
# ---------------------------------------------------------------------------


class TestBrokerOrder:
    def test_plan_only(self) -> None:
        view = D.broker_card(plan(), run_id="r")
        assert view.text == "🏦 [Broker] Order: SPY Iron Condor • x3 • Limit -1.25"
        text = _all(view)
        assert "Limit order · 3 attempts max · timeout 120s" in text
        assert (
            "*Order plan*\n1. Start -1.25 (mid)\n"
            "2. Step 1 -1.22 (wait 30s)\n3. Step 2 -1.20 (wait 30s)"
        ) in text
        assert "Each attempt" not in text
        assert "Result" not in text
        assert "*🏦 [Broker] Notes*\nStart at mid." in text
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
        view = D.broker_card(plan(), res, chain_run_id="c")
        assert view.text.endswith(" • Filled")
        text = _all(view)
        assert "*Result*\n:white_check_mark: filled" in text
        assert "*Filled*\n3 of 3" in text
        assert "*Fill price*\n-1.22" in text
        assert "*Slippage vs mid*\n+0.03/sh (+$9.00 total, + = cost)" in text
        assert "*Filled on attempt*\n2 of 3" in text
        assert "Filled at step 1." in text
        assert res.slippage == pytest.approx(0.03)

    def test_cancel_without_fill(self) -> None:
        res = D.ExecutionResult(status="cancelled", steps_used=2, detail=EVIL)
        view = D.broker_card(plan(), res)
        text = _all(view)
        assert view.text.endswith(" • Cancelled")
        assert ":x: cancelled" in text and "*Filled*\n0 of 3" in text
        assert "*Filled on attempt*\nnone of 3" in text
        assert "Slippage" not in text and "Fill price" not in text
        assert res.slippage is None
        assert EVIL_ESC in text

    def test_execution_result_is_strict(self) -> None:
        with pytest.raises(ValueError):
            D.ExecutionResult.model_validate({"status": "filled", "venue": "x"})


# ---------------------------------------------------------------------------
# Broker reconcile
# ---------------------------------------------------------------------------


class TestBrokerReconcile:
    def test_journal(self) -> None:
        perf = D.Performance(
            day_pnl=312.0,
            day_pct=0.0031,
            mtd_pnl=1184.0,
            mtd_pct=0.0119,
            ytd_pnl=-250.0,
            ytd_pct=-0.0025,
            equity=100_812.0,
        )
        view = D.reconcile_card(journal(), performance=perf, run_id="r")
        assert view.blocks[0]["text"]["text"] == "🏦 [Broker] Reconcile: Sep 28 • P&L +$312 (+0.3%)"
        assert view.text == "🏦 [Broker] Reconcile: Sep 28 • P&amp;L +$312 (+0.3%)"
        assert "anomal" not in view.text  # owner: anomalies only in the body
        text = _all(view)
        assert "Reconciliation :white_check_mark: *clean* · equity $100,812" in text
        assert "*Performance*\nDay +$312 (+0.3%)\nMTD +$1,184 (+1.2%)\nYTD -$250 (-0.2%)" in text
        assert "*Positions*\nOpen 2\nClosed today 1\nFills reviewed 3" in text
        assert (
            "*Anomalies (1)*\n• *Warning* · Fill discrepancy: Fill worse than mid. (orders o-1)"
        ) in text
        assert "• *Timing*: Mid moved.\n   → Wait less." in text
        assert "*🏦 [Broker] Journal*\nQuiet day." in text
        assert _footer(view) == "run `r`"
        _assert_slack_limits(view)

    def test_without_performance_shows_na(self) -> None:
        view = D.reconcile_card(journal())
        assert view.text == "🏦 [Broker] Reconcile: Sep 28 • P&amp;L +$312"
        assert "MTD n/a\nYTD n/a" in _all(view)

    def test_loss_no_anomalies_discrepancy(self) -> None:
        view = D.reconcile_card(
            journal(
                daily_pnl=-45.5,
                anomalies=[],
                lessons=[],
                reconciliation_status="discrepancies_found",
                journal_date="bad",
                journal_narrative=EVIL,
            )
        )
        assert view.blocks[0]["text"]["text"] == "🏦 [Broker] Reconcile: bad • P&L -$46"
        text = _all(view)
        assert ":warning: *discrepancies found*" in text
        assert "Day -$46" in text
        assert "Anomalies" not in text and "Lessons" not in text
        assert EVIL_ESC in text

    def test_many_anomalies_are_clipped(self) -> None:
        many = [
            AnomalyReport(category="other", severity="info", description=LONG) for _ in range(5)
        ]
        view = D.reconcile_card(journal(anomalies=many))
        assert "*Anomalies (5)*" in _all(view)
        _assert_slack_limits(view)

    def test_ops_slots_line(self) -> None:
        """E8.2a: the day's slot coverage is one line in an Ops section (no new post)."""
        line = "Slots: research 71/75, monitor 77/78 · missed 6 (list in tower Ops)"
        text = _all(D.reconcile_card(journal(), ops_line=line))
        assert f"*Ops*\n{line}" in text
        assert "*Ops*" not in _all(D.reconcile_card(journal()))


def test_regime_names() -> None:
    assert D.regime_name("risk_on") == "Risk ON"
    assert D.regime_name("RISK-OFF") == "Risk OFF"
    assert D.regime_name("range_bound") == "Range Bound"
    assert D.regime_name("") == "Unknown"


def test_unknown_reason_key_is_humanised() -> None:
    view = D.risk_card(
        RiskOutput(assessments=[], portfolio_summary="", advisory_notes=""),
        dropped={"weird_reason": 2},
    )
    assert "• weird reason (2)" in _all(view)
