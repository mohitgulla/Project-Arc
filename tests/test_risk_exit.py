"""E13.18 (D56): Risk exit review + the quant.propose close branch (always on since
E13.15).

Fixture chain (bundled SPY recording + fixture personas). The fixture book holds one
SPY long call; its entry price decides which deterministic signal fires:

* entry ``12.10`` -> no signal (a Research ``review`` makes the case);
* entry ``1.00``  -> ``profit_target`` (discretionary: needs a Risk close, or the
  fallback when Risk is unavailable, or the hold limit);
* a 40-day ``close_at_dte`` override -> ``dte_exit`` (mandatory: ``exits.mandatory``).
"""

from __future__ import annotations

import json
from decimal import Decimal as D
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from arc.broker.base import BrokerPosition
from arc.ingest.llm import FixtureScalpLLM
from arc.ingest.scalp import load_fixture_docs
from arc.journal.reasons import REASON_LABELS, ReasonCode
from arc.personas.schemas import RiskExitOutput, RiskExitVerdict
from arc.pipeline import FIXTURE_NOW, PipelineEnv
from arc.pipeline.runner import open_db
from arc.routines.runs import RoutineStateRepo
from tests.test_e59_research_portfolio import FIXTURES_DIR, LONG_CALL, _open_structure, _outcome
from tests.test_research_exit_watch import _codes, _kind, _research_reply, _routines, _run
from tests.test_research_exit_watch import _settings as _base_settings

if TYPE_CHECKING:
    import sqlite3

    from arc.config import ArcSettings


def _settings(**kw: object) -> ArcSettings:
    return _base_settings(**kw)


def _book(entry: str = "12.10") -> tuple[sqlite3.Connection, PipelineEnv, str]:
    conn = open_db(":memory:", copy=False)
    load_fixture_docs(conn)
    env = PipelineEnv.fixtures()
    sid = _open_structure(
        conn, env, LONG_CALL, stance="bullish", entry=entry, contracts=2,
        thesis="AI capex keeps SPY bid",
    )  # fmt: skip
    # the broker holds the legs, so the gate's closing check passes (close_mismatch off)
    held = [BrokerPosition(symbol=LONG_CALL[0][0], qty=D(2), side="long")]
    env.positions = lambda: list(held)
    return conn, env, sid


def _watch(sid: str, action: str = "review") -> str:
    return _research_reply(
        portfolio_view={"verdict": "concentrated", "notes": "all SPY"},
        exit_watchlist=[
            {
                "structure_id": sid,
                "ticker": "SPY",
                "action": action,
                "thesis_status": "broken" if action == "review" else "intact",
                "evidence": ["[st_9] capex guide cut"],
                "reason": "capex guide cut breaks the thesis",
            }
        ],
    )


def _personas(
    env: PipelineEnv,
    sid: str,
    *,
    research: str,
    quant_rec: str = "close",
    risk_reply: str | None,
) -> tuple[FixtureScalpLLM, FixtureScalpLLM]:
    env.llms["research"] = FixtureScalpLLM([research])
    quant_exit = json.dumps(
        {"cases": [{"structure_id": sid, "recommendation": quant_rec, "rationale": "r"}]}
    )
    quant = FixtureScalpLLM([quant_exit, (FIXTURES_DIR / "quant.json").read_text()])
    risk_open = (FIXTURES_DIR / "risk.json").read_text()
    risk = FixtureScalpLLM([risk_reply, risk_open] if risk_reply is not None else [risk_open])
    env.llms["quant"] = quant
    env.llms["risk"] = risk
    return quant, risk


def _verdict(sid: str, verdict: str = "close", code: str = "thesis_broken") -> str:
    return json.dumps(
        {"verdicts": [{"structure_id": sid, "verdict": verdict, "reason_code": code,
                       "reason": f"{verdict} it"}]}
    )  # fmt: skip


def _closes(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [
        dict(r) for r in conn.execute("SELECT * FROM proposals WHERE kind = 'close' ORDER BY rowid")
    ]


def _exit_hash(conn: sqlite3.Connection, sid: str) -> str | None:
    return conn.execute(
        "SELECT exit_proposal_hash FROM open_structures WHERE id = ?", (sid,)
    ).fetchone()[0]


# ---------------------------------------------------------------------------
# schema
# ---------------------------------------------------------------------------


class TestSchema:
    def test_verdict_contract(self) -> None:
        v = RiskExitVerdict(
            structure_id="os-1", verdict="close", reason_code="ev_exhausted", reason="  x  "
        )
        assert v.reason == "x"
        with pytest.raises(ValueError, match="reason_code"):
            RiskExitVerdict(structure_id="a", verdict="hold", reason_code="vibes", reason="r")
        with pytest.raises(ValueError, match="extra"):
            RiskExitOutput.model_validate({"verdicts": [], "notes": "x"})
        assert RiskExitOutput.model_validate({}).verdicts == []

    def test_reason_codes_labelled(self) -> None:
        for code in (
            ReasonCode.EXIT_RESEARCH_REVIEW,
            ReasonCode.EXIT_HOLD_REVIEWED,
            ReasonCode.EXIT_HOLD_LIMIT,
            ReasonCode.EXIT_REVIEW_UNAVAILABLE,
        ):
            assert code in REASON_LABELS


# ---------------------------------------------------------------------------
# the research exit path, end to end
# ---------------------------------------------------------------------------


class TestRiskExit:
    def test_close_becomes_a_close_proposal_with_review(self) -> None:
        conn, env, sid = _book()
        _, risk = _personas(env, sid, research=_watch(sid), risk_reply=_verdict(sid))
        conn, report = _run(_settings(), _routines(), env, conn=conn)
        assert not report.failed, report
        jobs = [o.job for o in report.outcomes]
        assert jobs[1:5] == ["research", "exits.mandatory", "quant.exit", "risk.exit"]
        assert "Exit review" in risk.prompts[0]  # type: ignore[attr-defined]
        assert sid in risk.prompts[0]  # type: ignore[attr-defined]
        (rv,) = _kind(conn, "risk_exit_review")
        assert rv["unavailable"] is False and rv["verdicts"][0]["verdict"] == "close"
        (close,) = _closes(conn)
        assert _exit_hash(conn, sid) == close["proposal_hash"]
        props = [p for p in _kind(conn, "proposal") if p.get("exit_review")]
        assert props and props[-1]["exit_review"]["reason_code"] == "thesis_broken"
        codes = _codes(conn, "exit")
        assert sid in codes["exit:research_review"]  # Risk journal row
        row = conn.execute(
            "SELECT reason_code FROM decisions WHERE proposal_hash = ? AND stage = 'exit'",
            (close["proposal_hash"],),
        ).fetchone()
        assert row is not None, codes  # propose_close journals the close proposal
        assert row["reason_code"] == "exit:research_review"
        gate = conn.execute(
            "SELECT passed FROM gate_decisions WHERE proposal_hash = ?",
            (close["proposal_hash"],),
        ).fetchone()
        assert gate["passed"] == 1  # the closing=True gate passed (token minted on approval)
        p = _outcome(report, "quant.propose")
        assert p.metrics["exit_closes"] == 1, p.summary

    def test_hold_is_journaled_no_action(self) -> None:
        conn, env, sid = _book()
        _personas(env, sid, research=_watch(sid), risk_reply=_verdict(sid, "hold", "thesis_intact"))
        conn, report = _run(_settings(), _routines(), env, conn=conn)
        assert not report.failed, report
        assert _closes(conn) == [] and _exit_hash(conn, sid) is None
        assert _codes(conn, "exit")["exit:hold_reviewed"] == [sid]
        # no deterministic signal: no hold streak is counted
        assert RoutineStateRepo(conn).get(f"exit_hold:{sid}:profit_target") is None

    def test_missing_verdict_holds_fail_closed(self) -> None:
        conn, env, sid = _book()
        _personas(env, sid, research=_watch(sid), risk_reply=json.dumps({"verdicts": []}))
        conn, report = _run(_settings(), _routines(), env, conn=conn)
        assert not report.failed
        (rv,) = _kind(conn, "risk_exit_review")
        assert rv["verdicts"][0]["verdict"] == "hold"
        assert rv["verdicts"][0]["reason"] == "no verdict: held (fail closed)"
        assert _closes(conn) == []

    def test_unavailable_review_only_case_holds(self) -> None:
        conn, env, sid = _book()
        _personas(env, sid, research=_watch(sid), risk_reply="not json")
        conn, report = _run(_settings(), _routines(), env, conn=conn)
        assert not report.failed
        (rv,) = _kind(conn, "risk_exit_review")
        assert rv["unavailable"] is True
        assert _closes(conn) == []  # Research review only: no D23 signal -> hold
        assert "exit:review_unavailable" in _codes(conn, "exit")

    def test_unavailable_profit_target_falls_back_to_policy(self) -> None:
        """tests/fixtures/exit_review_unavailable.json: risk.exit fails; the D23 profit
        target closes as today."""
        fx = json.loads(Path("tests/fixtures/exit_review_unavailable.json").read_text())
        conn, env, sid = _book(entry=fx["entry"])
        _personas(env, sid, research=_watch(sid, fx["research_action"]),
                  quant_rec=fx["quant_recommendation"], risk_reply=fx["risk_reply"])  # fmt: skip
        conn, report = _run(_settings(), _routines(), env, conn=conn)
        assert not report.failed, report
        (case,) = _kind(conn, "exit_case")
        assert [t["kind"] for t in case["triggers"]] == [fx["expect"]["trigger"]]
        (rv,) = _kind(conn, "risk_exit_review")
        assert rv["unavailable"] is True
        (close,) = _closes(conn)
        assert _exit_hash(conn, sid) == close["proposal_hash"]
        row = conn.execute(
            "SELECT reason_code, choice FROM decisions WHERE proposal_hash = ? AND stage='exit'",
            (close["proposal_hash"],),
        ).fetchone()
        assert row["reason_code"] == fx["expect"]["close_reason_code"]
        assert _outcome(report, "quant.propose").metrics["exit_fallback_closes"] == 1

    def test_hold_limit_closes_on_the_next_review(self) -> None:
        conn, env, sid = _book(entry="1.00")
        state = RoutineStateRepo(conn)
        state.set(f"exit_hold:{sid}:profit_target", "3")
        _personas(env, sid, research=_watch(sid, "hold"), quant_rec="hold",
                  risk_reply=_verdict(sid, "hold", "ev_remaining"))  # fmt: skip
        conn, report = _run(_settings(), _routines(), env, conn=conn)
        assert not report.failed, report
        (close,) = _closes(conn)
        row = conn.execute(
            "SELECT reason_code FROM decisions WHERE proposal_hash = ? AND stage='exit'",
            (close["proposal_hash"],),
        ).fetchone()
        assert row["reason_code"] == "exit:hold_limit_reached"
        assert state.get(f"exit_hold:{sid}:profit_target") is None  # reset by the close

    def test_hold_under_the_limit_counts(self) -> None:
        conn, env, sid = _book(entry="1.00")
        _personas(env, sid, research=_watch(sid, "hold"), quant_rec="hold",
                  risk_reply=_verdict(sid, "hold", "ev_remaining"))  # fmt: skip
        conn, report = _run(_settings(), _routines(), env, conn=conn)
        assert not report.failed
        assert _closes(conn) == []
        assert RoutineStateRepo(conn).get(f"exit_hold:{sid}:profit_target") == "1"
        assert _codes(conn, "exit")["exit:hold_reviewed"] == [sid]

    def test_no_case_no_risk_call(self) -> None:
        conn, env, sid = _book()
        _, risk = _personas(env, sid, research=_watch(sid, "hold"), risk_reply=None)
        conn, report = _run(_settings(), _routines(), env, conn=conn)
        assert not report.failed
        assert _outcome(report, "risk.exit").status == "skipped"
        assert not any("Exit review" in p for p in risk.prompts)  # type: ignore[attr-defined]
        assert _kind(conn, "risk_exit_review") == []

    def test_halt_proposes_nothing(self) -> None:
        from arc.gate.halt import HaltSwitch
        from arc.store.repos import HaltRepo

        conn, env, sid = _book()
        _personas(env, sid, research=_watch(sid), risk_reply=_verdict(sid))
        HaltSwitch(HaltRepo(conn)).halt(reason="test", actor="t", now=FIXTURE_NOW)
        conn, report = _run(_settings(), _routines(), env, conn=conn)
        assert _closes(conn) == []
        p = _outcome(report, "quant.propose")
        assert p.status != "ok" or p.metrics.get("exit_closes", 0) == 0


# ---------------------------------------------------------------------------
# exits.mandatory + chain wiring
# ---------------------------------------------------------------------------


class TestMandatory:
    def test_dte_exit_closes_before_any_llm_exit_step(self) -> None:
        conn, env, sid = _book()
        _, risk = _personas(env, sid, research=_watch(sid, "hold"), quant_rec="hold",
                            risk_reply=_verdict(sid, "hold", "thesis_intact"))  # fmt: skip
        settings = _settings()
        # the fixture call has 35 DTE; a 40-day DTE exit fires the mandatory floor
        settings._yaml_overrides = {"exits": {("kinds", "long_call", "close_at_dte"): 40}}  # noqa: SLF001
        conn, report = _run(settings, _routines(), env, conn=conn)
        assert not report.failed, report
        m = _outcome(report, "exits.mandatory")
        assert m.status == "ok" and m.metrics["mandatory"] is True, m.summary
        (close,) = _closes(conn)
        assert _exit_hash(conn, sid) == close["proposal_hash"]
        # the mandatory close takes the position out of the LLM exit path
        assert _kind(conn, "exit_case") == []
        assert _outcome(report, "risk.exit").status == "skipped"
        assert not any("Exit review" in p for p in risk.prompts)  # type: ignore[attr-defined]

    def test_positions_chain(self) -> None:
        from arc.routines.dispatcher import DEADLINE_EXEMPT_STEPS
        from arc.routines.handlers import BUILTIN_HANDLERS

        r = _routines()
        assert r.personas["positions.evaluate"].chain == ["exits.mandatory", "broker.execute"]
        assert r.personas["research"].chain[:3] == ["exits.mandatory", "quant.exit", "risk.exit"]
        assert "exits.mandatory" in DEADLINE_EXEMPT_STEPS
        # E13.15: the deterministic-only exit chain is gone
        assert not {"quant.exits", "risk.reallocate"} & set(BUILTIN_HANDLERS)
        assert not {"quant.exits", "risk.reallocate"} & set(r.steps)

    def test_registry_and_settings(self) -> None:
        from arc.control.registry import lookup
        from arc.experiments.runner import ACCOUNT_STEPS, STEP_TARGETS

        t = lookup("exit_review_max_consecutive_holds")
        assert t.hard_ceiling == 10
        s = _settings()
        assert (s.exit_review_max_consecutive_holds, s.exit_steps_min_remaining_s) == (3, 60)
        assert "exits.mandatory" in ACCOUNT_STEPS
        assert "exits" in STEP_TARGETS["risk.exit"]

    def test_card_renders(self) -> None:
        from arc.slack.digests import risk_exit_card

        conn, env, sid = _book()
        _personas(env, sid, research=_watch(sid), risk_reply=_verdict(sid))
        conn, report = _run(_settings(), _routines(), env, conn=conn)
        card = _outcome(report, "risk.exit")
        assert card.status == "ok"
        view = risk_exit_card([], {}, unavailable=True)
        assert "0 reviewed" in view.text
