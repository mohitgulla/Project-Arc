"""E13.17 (D56): exit-case triggers, skip rules and code-built facts (pure)."""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from pydantic import ValidationError

from arc.exits.policy import ExitPolicy
from arc.models import Stance
from arc.personas.schemas import ExitWatchItem, QuantExitJudgement, QuantExitOutput
from arc.positions.evaluate import ExitSignal, PositionReview, SignalKind
from arc.positions.exit_case import (
    DISCRETIONARY_KINDS,
    MANDATORY_KINDS,
    ExitCase,
    ExitCaseFacts,
    ExitSwap,
    case_skip_reason,
    stop_state,
    triggers_for,
)
from arc.positions.portfolio import PositionFacts
from arc.positions.reallocate import pair_swaps
from tests.test_positions import RULES, _call_debit, _cand, _rv


def _review(sid: str = "os1", *signals: SignalKind, **kw: Any) -> PositionReview:
    base: dict[str, Any] = {
        "structure_id": sid,
        "ticker": "SPY",
        "kind": "bull_put_spread",
        "credit": True,
        "contracts": 2,
        "as_of": dt.date(2026, 10, 6),
        "dte": 24,
        "entry_net": -1.5,
        "current_value": -0.6,
        "pnl": 90.0,
        "pnl_total": 180.0,
        "pct_of_max_gain": 0.6,
        "take_profit_pct": 0.5,
        "theta_per_day": 2.1,
        "buying_power": 350.0,
        "close_now_net": 85.0,
        "remaining_ev": 4.0,
        "remaining_ev_per_bp": 0.011,
        "remaining_pop": 0.82,
        "signals": [ExitSignal(kind=k, detail=f"{k.value} fired") for k in signals],
        "structure": _call_debit(),
    }
    base.update(kw)
    return PositionReview(**base)


def _watch(action: str = "review", status: str = "weakened") -> ExitWatchItem:
    return ExitWatchItem.model_validate(
        {
            "structure_id": "os1",
            "ticker": "SPY",
            "action": action,
            "thesis_status": status,
            "evidence": ["[st_1] guidance cut"],
            "reason": "guidance cut undercuts the bullish thesis",
        }
    )


class TestKinds:
    def test_mandatory_and_discretionary_partition_every_signal(self) -> None:
        assert set(SignalKind) == MANDATORY_KINDS | DISCRETIONARY_KINDS
        assert not MANDATORY_KINDS & DISCRETIONARY_KINDS
        assert {
            SignalKind.STOP,
            SignalKind.PROFIT_LOCK,  # E18.1 (D78): a lock Risk could veto is not a lock
            SignalKind.DTE_EXIT,
            SignalKind.EXPIRY,
        } == MANDATORY_KINDS


class TestSkipRules:
    @pytest.mark.parametrize("kind", sorted(MANDATORY_KINDS))
    def test_mandatory_signal_never_gets_a_case(self, kind: SignalKind) -> None:
        r = _review("os1", kind, SignalKind.PROFIT_TARGET)
        assert case_skip_reason(r) == "mandatory_pending"

    def test_exit_pending_or_exit_today(self) -> None:
        assert case_skip_reason(_review(exit_pending=True)) == "exit_pending"
        assert case_skip_reason(_review(), today_exit=True) == "exit_pending"
        assert case_skip_reason(_review()) is None


class TestTriggers:
    def test_research_review_then_signals_then_swap(self) -> None:
        r = _review("os1", SignalKind.PROFIT_TARGET, SignalKind.REMAINING_EV_FLOOR)
        swap = ExitSwap(
            close_structure_id="os1", close_ticker="SPY", source_ref="d1", open_ticker="NVDA",
            rejected_for="buying_power", new_ev_per_bp=0.05, open_remaining_ev_per_bp=0.01,
            switching_cost_per_bp=0.005, edge=0.035, min_edge_required=0.02, new_pop=0.7,
            open_remaining_pop=0.8, detail="close SPY for NVDA",
        )  # fmt: skip
        kinds = [t.kind for t in triggers_for(r, _watch(), swap)]
        assert kinds == ["research_review", "profit_target", "remaining_ev_floor", "reallocate"]

    def test_hold_and_no_signal_is_no_trigger(self) -> None:
        assert triggers_for(_review(), _watch("hold", "intact")) == []
        assert triggers_for(_review(), None) == []

    def test_mandatory_signals_are_not_triggers(self) -> None:
        assert triggers_for(_review("os1", SignalKind.STOP), None) == []

    def test_detail_is_clipped(self) -> None:
        w = _watch().model_copy(update={"reason": "x" * 240})
        (t,) = triggers_for(_review(), w)
        assert len(t.detail) <= 200 and t.detail.endswith("…")


class TestFacts:
    def test_from_review_copies_numbers_and_facts(self) -> None:
        facts = PositionFacts(
            iv_rank=0.42, next_earnings="2026-10-20", ex_dividend=None, stories_fresh=2,
            review_signals=[], remaining_ev=4.0,
        )  # fmt: skip
        f = ExitCaseFacts.from_review(
            _review(), facts, policy=ExitPolicy(), thesis_status="weakened"
        )
        assert f.remaining_ev_hold == 4.0 and f.close_now_net == 85.0
        assert f.buying_power_freed == 700.0  # 350 × 2 contracts
        assert f.iv_rank == 0.42 and f.next_earnings == "2026-10-20"
        assert f.thesis_status == "weakened"
        assert f.stop_state in {"armed_eod", "armed", "off"}

    def test_stop_state(self) -> None:
        assert stop_state(None) == "off"
        p = ExitPolicy()
        if p.stop is not None:
            assert stop_state(p) == ("armed_eod" if p.stop_eod_only else "armed")
            assert stop_state(p.model_copy(update={"stop_eod_only": False})) == "armed"
        assert stop_state(p.model_copy(update={"stop": None})) == "off"

    def test_case_needs_a_trigger_and_forbids_roll(self) -> None:
        f = ExitCaseFacts.from_review(_review(), None)
        with pytest.raises(ValidationError):
            ExitCase(
                structure_id="os1", ticker="SPY", triggers=[], facts=f,
                recommendation="hold", rationale="x",
            )  # fmt: skip
        with pytest.raises(ValidationError):
            ExitCase.model_validate(
                {
                    "structure_id": "os1", "ticker": "SPY", "facts": f.model_dump(),
                    "triggers": [{"kind": "profit_target", "detail": "x"}],
                    "recommendation": "roll", "rationale": "x",
                }
            )  # fmt: skip


class TestSwapPairing:
    def test_pairs_a_capacity_rejection_gate_free(self) -> None:
        paired = pair_swaps([_rv()], [_cand()], RULES)
        s = ExitSwap.of(paired["os-1"])
        assert s.rejected_for == "buying_power" and s.close_structure_id == "os-1"
        assert s.edge > 0 and (triggers_for(_rv(), None, s)[0].kind == "reallocate")
        assert pair_swaps([_rv()], [], RULES) == {}


class TestReplySchemas:
    def test_watch_item_trims_evidence_and_reason(self) -> None:
        w = ExitWatchItem.model_validate(
            {
                **_watch().model_dump(),
                "evidence": ["a", " ", "b", "c", "d", "e" * 300],
                "reason": "r" * 500,
            }
        )
        assert w.evidence == ["a", "b", "c", "d"] and len(w.reason) == 240
        long = ExitWatchItem.model_validate({**_watch().model_dump(), "evidence": ["x" * 300]})
        assert len(long.evidence[0]) == 160
        with pytest.raises(ValidationError):
            ExitWatchItem.model_validate({**_watch().model_dump(), "thesis_status": "invalidated"})

    def test_quant_exit_output(self) -> None:
        out = QuantExitOutput.model_validate(
            {"cases": [{"structure_id": "os1", "recommendation": "close", "rationale": "y"}]}
        )
        assert out.cases == [
            QuantExitJudgement(structure_id="os1", recommendation="close", rationale="y")
        ]
        with pytest.raises(ValidationError):
            QuantExitOutput.model_validate(
                {"cases": [{"structure_id": "os1", "recommendation": "roll", "rationale": "y"}]}
            )


def test_stance_import_used() -> None:  # keeps the PositionFacts scout field typed
    assert PositionFacts(scout_mention=Stance.BULLISH).scout_mention is Stance.BULLISH
