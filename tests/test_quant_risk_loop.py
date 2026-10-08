"""E13.9 (D56): the Quant <-> Risk open path (always on since E13.15).

Risk gives each structure a verdict; rejects never reach a proposal; one
``quant.revise`` round answers the revise requests from the same scanner menu; a step
that needs more loop budget than is left is skipped (``step_skipped_deadline``) while
later steps still run.
"""

from __future__ import annotations

import copy
import datetime as dt
import json
from typing import TYPE_CHECKING, Any

import pytest

from arc.config import ArcSettings
from arc.ingest.llm import FixtureScalpLLM
from arc.journal.reasons import REASON_LABELS, ReasonCode
from arc.pipeline import FIXTURE_NOW, PipelineEnv
from arc.pipeline.runner import ProposeReport, open_db, run_propose
from arc.routines.config import (
    AUTO_CHAINS,
    DEFAULT_ROUTINES_PATH,
    RoutinesConfig,
    load_routines,
)
from arc.routines.heartbeat import RecordingNotifier

if TYPE_CHECKING:
    import sqlite3

ON_CHAIN = ["quant.open", "risk.open", "quant.revise", "quant.propose", "broker.execute"]

# Scanner-menu legs of the offline recording (SPY neutral condors, NVDA bull puts).
SPY_FIRST = [  # menu #2: quant.json's pick
    ("SPY261030P00740000", "long", 740, "put"),
    ("SPY261030P00745000", "short", 745, "put"),
    ("SPY261030C00798000", "short", 798, "call"),
    ("SPY261030C00803000", "long", 803, "call"),
]
SPY_REVISED = [  # menu #1: call side one strike closer
    ("SPY261030P00740000", "long", 740, "put"),
    ("SPY261030P00745000", "short", 745, "put"),
    ("SPY261030C00797000", "short", 797, "call"),
    ("SPY261030C00802000", "long", 802, "call"),
]
NVDA_FIRST = [
    ("NVDA261030P00210000", "long", 210, "put"),
    ("NVDA261030P00215000", "short", 215, "put"),
]


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch) -> ArcSettings:
    monkeypatch.delenv("ARC_GATE_SECRET", raising=False)
    s = ArcSettings(_env_file=None, account_profile="margin")  # type: ignore[call-arg]
    # NVDA's recorded bull puts model at negative managed Net EV; these tests are about
    # the verdict flow, so the E6.4a live Net EV floor is off (as in test_pipeline).
    s._yaml_overrides = {"ranking": {("ranking", "filters", "live"): False}}  # noqa: SLF001
    return s


def _routines(on: bool = True) -> RoutinesConfig:
    """The shipped routines (E13.15: the loop is always on)."""
    assert on
    return load_routines(DEFAULT_ROUTINES_PATH)


def _structure(ticker: str, kind: str, legs: list[tuple[str, str, int, str]], why: str) -> dict:
    expiry = "2026-10-30"
    return {
        "ticker": ticker,
        "structure_type": kind,
        "legs": [
            {
                "occ_symbol": s,
                "side": side,
                "ratio": 1,
                "strike": k,
                "expiry": expiry,
                "option_type": t,
            }
            for s, side, k, t in legs
        ],  # fmt: skip
        "net_debit_credit": -1.0,
        "max_gain": 100.0,
        "max_loss": 400.0,
        "breakevens": [1.0],
        "greeks": {"delta": 0.0, "gamma": 0.0, "vega": 0.0, "theta": 0.0},
        "dte": 35,
        "pop": 0.7,
        "ev_per_contract": 1.0,
        "cost_bps": 1.0,
        "confidence": 0.7,
        "rationale": why,
    }


def _assessment(ticker: str, kind: str, verdict: str, req: dict | None = None) -> dict:
    return {
        "ticker": ticker,
        "structure_type": kind,
        "risk_rating": "moderate",
        "concentration_warning": False,
        "greek_budget_impact": "n/a",
        "calendar_concerns": "n/a",
        "sizing_suggestion": 5,
        "max_loss_pct_equity": 0.01,
        "narrative": f"{ticker} {verdict}",
        "verdict": verdict,
        "revise_request": req,
    }


REQ = {"reason": "strike", "instruction": "Move the short call one strike closer."}


def _quant_open_reply(*structures: dict) -> str:
    return json.dumps({"structures": list(structures), "skipped": [], "analysis_notes": "open"})


def _env(*, quant: list[str] | None = None, risk: list[str] | None = None) -> PipelineEnv:
    env = PipelineEnv.fixtures()
    if quant is not None:
        env.llms["quant"] = FixtureScalpLLM(quant)
    if risk is not None:
        env.llms["risk"] = FixtureScalpLLM(risk)
    return env


def _run(
    settings: ArcSettings, routines: RoutinesConfig, env: PipelineEnv
) -> tuple[sqlite3.Connection, ProposeReport]:
    from arc.ingest.scalp import load_fixture_docs

    conn = open_db(":memory:", copy=False)
    load_fixture_docs(conn)
    return conn, run_propose(
        conn, settings, routines, env, now=FIXTURE_NOW, notifier=RecordingNotifier()
    )


def _outcomes(report: ProposeReport) -> dict[str, Any]:
    return {o.job: o for o in report.outcomes}


def _strikes(proposal: dict[str, Any]) -> list[int]:
    legs = json.loads(proposal["structure_json"])["legs"]
    return sorted(int(leg["occ_symbol"][-8:]) // 1000 for leg in legs)


def _codes(conn: sqlite3.Connection, stage: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for r in conn.execute(
        "SELECT subject, reason_code FROM decisions WHERE stage = ? ORDER BY rowid", (stage,)
    ):
        out.setdefault(r["subject"], []).append(r["reason_code"])
    return out


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------


class TestConfig:
    def test_fixed_chain(self) -> None:
        r = load_routines(DEFAULT_ROUTINES_PATH)
        assert AUTO_CHAINS["research"][-len(ON_CHAIN) :] == tuple(ON_CHAIN)
        assert r.personas["research"].chain[-len(ON_CHAIN) :] == ON_CHAIN
        rev = r.steps["quant.revise"]
        assert rev.min_remaining_s == 90 and rev.on_no_change == "skip"
        assert rev.writes == ["structures", "note"]
        assert "risk_review" in (r.steps["quant.propose"].reads or [])

    def test_old_step_names_rejected(self) -> None:
        """E13.15: the one-release ``quant`` / ``risk`` / ``propose`` aliases are gone."""
        c = RoutinesConfig.model_validate(
            {
                "personas": {"research": {"schedule": ["09:00"], "chain": ["quant", "risk"]}},
                "steps": {"propose": {"llm": False}},
            }
        )
        assert c.personas["research"].chain == ["quant", "risk"]  # no longer renamed
        assert "propose" in c.steps and "quant.propose" not in c.steps

    def test_switch_orphaned(self) -> None:
        from arc.control.registry import is_orphaned

        assert is_orphaned("personas.quant_risk_loop")

    def test_reason_codes_labelled(self) -> None:
        for code in (ReasonCode.RISK_REVISE, ReasonCode.RISK_REJECT, ReasonCode.QUANT_REVISED,
                     ReasonCode.QUANT_KEPT):  # fmt: skip
            assert REASON_LABELS[code]


# ---------------------------------------------------------------------------
# flag on: verdicts and one revision round
# ---------------------------------------------------------------------------


def _three_way_env(revise_reply: str) -> PipelineEnv:
    """Quant opens SPY + NVDA; Risk says SPY revise, NVDA reject; *revise_reply* answers."""
    quant_open = _quant_open_reply(
        _structure("SPY", "iron_condor", SPY_FIRST, "first SPY"),
        _structure("NVDA", "vertical_spread", NVDA_FIRST, "first NVDA"),
    )
    risk = json.dumps(
        {
            "assessments": [
                _assessment("SPY", "iron_condor", "revise", REQ),
                _assessment("NVDA", "vertical_spread", "reject"),
            ],
            "portfolio_summary": "flat",
            "advisory_notes": "",
        }
    )
    return _env(quant=[quant_open, revise_reply], risk=[risk])


class TestFlagOn:
    def test_accept_passes_through_without_a_revision_call(self, settings: ArcSettings) -> None:
        on = _routines(on=True)
        env = _env()  # fixture Risk reply has no verdicts -> every structure is accept
        conn, report = _run(settings, on, env)
        out = _outcomes(report)
        assert [o.job for o in report.outcomes][-len(ON_CHAIN) :] == ON_CHAIN
        assert out["quant.revise"].status == "skipped"
        assert out["quant.revise"].summary == "no revise requests"
        assert len(env.llms["quant"].prompts) == 1  # type: ignore[attr-defined]
        assert len(report.proposals) == 1
        # Risk's prompt carries the verdict block
        risk_prompt = env.llms["risk"].prompts[0]  # type: ignore[attr-defined]
        assert "verdict" in risk_prompt and "revise_request" in risk_prompt
        assert out["risk.open"].metrics["verdict_accept"] == 1

    def test_revise_and_reject(self, settings: ArcSettings) -> None:
        revised = json.dumps(
            {
                "structures": [
                    _structure("SPY", "iron_condor", SPY_REVISED, "closer call side"),
                    # outside the revise set (Risk rejected NVDA): dropped
                    _structure("NVDA", "vertical_spread", NVDA_FIRST, "sneak NVDA back"),
                ],
                "skipped": [],
                "kept": [],
                "analysis_notes": "revised SPY",
            }
        )
        env = _three_way_env(revised)
        conn, report = _run(settings, _routines(on=True), env)
        out = _outcomes(report)
        assert out["quant.revise"].status == "ok", out["quant.revise"].summary
        m = out["quant.revise"].metrics
        assert (m["revise_requests"], m["revised"], m["rejected"]) == (1, 1, 1)
        assert m["not_shortlisted"] == 1  # the NVDA re-proposal

        # the revise prompt carries exactly one request and names the reject
        prompt = env.llms["quant"].prompts[1]  # type: ignore[attr-defined]
        assert "## Risk requested changes" in prompt
        assert prompt.count('"revise_request"') == 1
        assert REQ["instruction"] in prompt
        assert "Rejected by Risk (dropped, do not re-propose): NVDA." in prompt
        assert "QuantReviseOutput" in prompt and '"kept"' in prompt

        # the revised structure supersedes the first; the reject never reaches a proposal
        (p,) = report.proposals
        assert p["ticker"] == "SPY"
        assert _strikes(p) == [740, 745, 797, 802]
        assert not conn.execute("SELECT 1 FROM proposals WHERE ticker='NVDA'").fetchone()
        # the revision superseded the first structures: NVDA is not in it at all
        assert "risk_reject" not in out["quant.propose"].metrics

        codes = _codes(conn, "risk_review")
        assert codes["SPY"] == [ReasonCode.RISK_REVISE.value]
        assert codes["NVDA"] == [ReasonCode.RISK_REJECT.value]
        assert ReasonCode.QUANT_REVISED.value in _codes(conn, "structure")["SPY"]

        entry = conn.execute(
            "SELECT payload, produced_by FROM context_entries WHERE kind='structures'"
            " ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        payload = json.loads(entry["payload"])
        assert entry["produced_by"] == "quant.revise"
        assert payload["revision_of"] and payload["kept"] == []
        assert [s["ticker"] for s in payload["structures"]] == ["SPY"]
        prop = conn.execute("SELECT payload FROM context_entries WHERE kind='proposal'").fetchone()
        assert json.loads(prop["payload"])["revised"] is True

        # approval card: Quant's revision and Risk's request in the persona trail
        from arc.approvals.card import render_card
        from arc.approvals.trail import load_trail
        from arc.models import Proposal

        trail = load_trail(conn, p["proposal_hash"], "SPY")
        assert trail.revision and trail.revision["rationale"] == "closer call side"
        assert trail.risk and trail.risk["verdict"] == "revise"
        proposal = Proposal.model_validate_json(prop["payload"])
        card = render_card(
            proposal, None, proposal_hash=p["proposal_hash"], actionable=False, trail=trail
        )
        text = json.dumps(card.blocks)
        assert "Revised for Risk" in text and "Asked Quant to revise (strike)" in text

    def test_kept_keeps_the_first_structure(self, settings: ArcSettings) -> None:
        kept = json.dumps(
            {"structures": [], "skipped": [], "kept": ["SPY"], "analysis_notes": "fine as is"}
        )
        conn, report = _run(settings, _routines(on=True), _three_way_env(kept))
        out = _outcomes(report)
        assert out["quant.revise"].metrics["kept"] == 1
        (p,) = report.proposals
        assert _strikes(p) == [740, 745, 798, 803]
        assert ReasonCode.QUANT_KEPT.value in _codes(conn, "structure")["SPY"]

    def test_neither_revised_nor_kept_is_dropped(self, settings: ArcSettings) -> None:
        nothing = json.dumps({"structures": [], "skipped": [], "analysis_notes": "no"})
        _, report = _run(settings, _routines(on=True), _three_way_env(nothing))
        out = _outcomes(report)
        assert out["quant.revise"].metrics["dropped"] == 1
        assert report.proposals == []
        assert out["quant.propose"].status == "ok"

    def test_off_menu_revision_is_dropped(self, settings: ArcSettings) -> None:
        legs = copy.deepcopy(SPY_REVISED)
        legs[2] = ("SPY261030C00790000", "short", 790, "call")  # not on the menu
        bad = json.dumps(
            {"structures": [_structure("SPY", "iron_condor", legs, "invented")],
             "skipped": [], "analysis_notes": ""}
        )  # fmt: skip
        _, report = _run(settings, _routines(on=True), _three_way_env(bad))
        out = _outcomes(report)
        assert out["quant.revise"].metrics["not_in_menu"] == 1
        assert report.proposals == []


# ---------------------------------------------------------------------------
# deadline guard (generic min_remaining_s)
# ---------------------------------------------------------------------------


def test_deadline_skips_the_step_and_later_steps_run(settings: ArcSettings) -> None:
    on = _routines(on=True)
    steps = dict(on.steps)
    # more seconds than the 7m loop budget: quant.revise can never start
    steps["quant.revise"] = steps["quant.revise"].model_copy(update={"min_remaining_s": 10_000})
    on = on.model_copy(update={"steps": steps})
    from arc.ingest.scalp import load_fixture_docs
    from arc.pipeline.steps import pipeline_handlers
    from arc.routines.dispatcher import Dispatcher

    conn = open_db(":memory:", copy=False)
    load_fixture_docs(conn)
    env = _three_way_env(_quant_open_reply())
    d = Dispatcher(
        conn,
        on,
        handlers=pipeline_handlers(env),
        notifier=RecordingNotifier(),
        is_halted=lambda: False,
        settings_factory=lambda: settings,
    )
    d.run_job("scalp", FIXTURE_NOW, reason="manual", now=FIXTURE_NOW)
    # a scheduled loop slot (the deadline applies to the D31 loop chain only)
    outs = {
        o.job: o for o in d.run_job("research", FIXTURE_NOW, reason="schedule", now=FIXTURE_NOW)
    }
    assert outs["quant.revise"].status == "skipped"
    assert outs["quant.revise"].reason.startswith("step_skipped_deadline: needs 10000s")
    assert outs["quant.propose"].status == "ok"
    assert outs["broker.execute"].status == "ok"
    # Risk's reject still applies without the revision; SPY's revise stays unanswered
    # and is proposed as first structured (the gate still applies).
    assert not conn.execute("SELECT 1 FROM proposals WHERE ticker='NVDA'").fetchone()
    assert outs["quant.propose"].metrics["risk_reject"] == 1  # dropped by quant.propose
    tickers = [r["ticker"] for r in conn.execute("SELECT ticker FROM proposals")]
    assert tickers == ["SPY"]


# ---------------------------------------------------------------------------
# experiments: where a paired arm forks
# ---------------------------------------------------------------------------


def test_experiment_fork_points() -> None:
    from arc.experiments.runner import ACCOUNT_STEPS, STEP_TARGETS, fork_step

    chain = ["research", *ON_CHAIN]
    # Any routines key re-runs Research.
    assert fork_step(chain, {"routines": {"loop": {"max_runtime": "5m"}}}) == "research"
    both = {"routines": {"personas": {"finnhub_context": "on"}}}
    assert fork_step(chain, both) == "research"
    assert fork_step(chain, {"exits": {"x": 1}}) == "quant.open"
    assert fork_step(chain, {}) == "quant.propose"
    assert {"quant.open", "risk.open", "quant.revise", "quant.propose"} <= set(STEP_TARGETS)
    # E13.18: exits.mandatory closes against the arm's own book
    assert frozenset({"exits.mandatory", "quant.propose", "broker.execute"}) == ACCOUNT_STEPS


def test_legacy_job_names_map_old_steps_but_not_personas() -> None:
    from arc.journal import legacy

    at = dt.datetime(2026, 10, 1, 10, tzinfo=FIXTURE_NOW.tzinfo)
    for old, new in (("quant", "quant.open"), ("risk", "risk.open"), ("propose", "quant.propose")):
        assert legacy.job_name(old, at, {}) == new
        assert (old, legacy.OPEN_PATH_CUTOVER_KEY) in legacy.legacy_names(new)
    # the stored decision persona 'quant'/'risk' stays the persona
    assert legacy.persona_key("quant", at, {}) == "quant"
    assert legacy.persona_key("risk", at, {}) == "risk"
    assert legacy.job_name("quant", at, {}, steps=False) == "quant"
    assert legacy.job_name("quant.exits", at, {}) == "quant.exits"  # exact hops only
