"""arc.execution.submit(): refuses without a valid gate token AND an approval (E3.2)."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal as D
from typing import TYPE_CHECKING

import pytest

from arc.config import ArcEnv, ArcSettings
from arc.execution import RefusalCode, SubmitRefused, attempt_order_id, build_order, submit
from arc.gate import HaltSwitch, issue_token, order_payload, proposal_hash
from arc.gate.band import PriceBand
from arc.gate.token import TokenError
from arc.models import (
    ApprovalDecision,
    ApprovalRecord,
    GateDecision,
    Proposal,
    QuantMetrics,
    Sizing,
)
from arc.store.db import connect
from arc.store.migrate import migrate
from arc.store.repos import HaltRepo
from arc.structures import credit_vertical, format_occ
from arc.utils.calendar import ET

if TYPE_CHECKING:
    from arc.broker.base import MlegOrder

SECRET = "s" * 32
NOW = dt.datetime(2026, 10, 9, 10, 0, tzinfo=ET)
EXP = dt.date(2026, 11, 20)
LP = format_occ("SPY", EXP, "put", 565)
SP = format_occ("SPY", EXP, "put", 570)


class FakeBroker:
    """Records submissions; any other adapter method is unused here."""

    def __init__(self) -> None:
        self.orders: list[MlegOrder] = []

    def submit_mleg(self, order: MlegOrder) -> str:
        self.orders.append(order)
        return f"brk-{len(self.orders)}"


def cfg(**kw: object) -> ArcSettings:
    base: dict[str, object] = {"_env_file": None, "gate_secret": SECRET}
    base.update(kw)
    return ArcSettings(**base)  # type: ignore[arg-type]


def proposal(**kw: object) -> Proposal:
    base: dict[str, object] = {
        "candidate_id": "c",
        "structure": credit_vertical(
            "put",
            "SPY",
            EXP,
            short_strike=570,
            short_premium="2.10",
            long_strike=565,
            long_premium="1.25",
            as_of=dt.date(2026, 10, 9),
        ),
        "thesis": "t",
        "quant": QuantMetrics(pop=0.7, ev=D("1"), cost_bps=0.0),
        "sizing": Sizing(contracts=2, notional=D("830"), pct_equity=0.01),
        "expires_at": NOW + dt.timedelta(minutes=10),
    }
    base.update(kw)
    return Proposal(**base)  # type: ignore[arg-type]


def gated(p: Proposal, band: PriceBand | None = None) -> GateDecision:
    d = GateDecision(proposal_hash=proposal_hash(p), passed=True)
    return issue_token(d, p, secret=SECRET.encode(), now=NOW, band=band)


def switch() -> HaltSwitch:
    conn = connect(":memory:")
    migrate(conn)
    return HaltSwitch(HaltRepo(conn))


BAND = PriceBand(lo=D("-0.85"), hi=D("-0.76"), max_steps=3)


def approved(p: Proposal, **kw: object) -> ApprovalRecord:
    base: dict[str, object] = {
        "proposal_hash": proposal_hash(p),
        "slack_user": "U0C5KUMH28G",
        "slack_ts": "1790000000.000100",
        "decision": ApprovalDecision.APPROVED,
        "at": NOW - dt.timedelta(minutes=1),
    }
    base.update(kw)
    return ApprovalRecord(**base)  # type: ignore[arg-type]


def go(p, d, a, *, config: ArcSettings | None = None, now: dt.datetime = NOW, **kw):
    broker = FakeBroker()
    halt = kw.pop("halt", None) or switch()
    result = submit(p, d, a, broker=broker, config=config or cfg(), now=now, halt=halt, **kw)
    return result, broker


_UNSET = object()


def refused(p, d, a, **kw) -> tuple[RefusalCode, FakeBroker]:
    broker = FakeBroker()
    halt = kw.pop("halt", _UNSET)
    with pytest.raises(SubmitRefused) as e:
        submit(
            p,
            d,
            a,
            broker=broker,
            config=kw.pop("config", cfg()),
            now=kw.pop("now", NOW),
            halt=switch() if halt is _UNSET else halt,
            **kw,
        )
    assert broker.orders == [], "broker must not be touched on refusal"
    return e.value.code, broker


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_submits_exact_order_with_token_as_client_order_id() -> None:
    p = proposal()
    d = gated(p)
    broker_id, broker = go(p, d, approved(p))
    assert broker_id == "brk-1"
    (order,) = broker.orders
    assert order.client_order_id == d.token
    assert order.qty == 2
    assert order.limit_price == D("-0.85")
    assert order.time_in_force == "day"
    assert {(x.symbol, x.side, x.ratio_qty) for x in order.legs} == {
        (LP, "buy", 1),
        (SP, "sell", 1),
    }


def test_build_order_matches_order_payload() -> None:
    p = proposal(limit_price=D("-0.80"))
    tok = gated(p).token
    assert tok is not None
    o = build_order(p, tok)
    op = order_payload(p)
    assert o.limit_price == op.limit_price == D("-0.80")
    assert o.client_order_id == tok
    with pytest.raises(TokenError):
        build_order(p, "not-a-token")


# ---------------------------------------------------------------------------
# Refusals: token side
# ---------------------------------------------------------------------------


def test_refuses_without_decision() -> None:
    p = proposal()
    assert refused(p, None, approved(p))[0] is RefusalCode.NO_DECISION


def test_refuses_failed_decision() -> None:
    p = proposal()
    d = GateDecision(proposal_hash=proposal_hash(p), passed=False, violations=["halted: x"])
    assert refused(p, d, approved(p))[0] is RefusalCode.GATE_FAILED


def test_refuses_passed_decision_with_violations() -> None:
    p = proposal()
    d = gated(p).model_copy(update={"violations": ["x"]})
    assert refused(p, d, approved(p))[0] is RefusalCode.GATE_FAILED


def test_refuses_passed_decision_without_token() -> None:
    p = proposal()
    d = GateDecision(proposal_hash=proposal_hash(p), passed=True)
    assert refused(p, d, approved(p))[0] is RefusalCode.TOKEN_INVALID


def test_refuses_forged_token() -> None:
    p = proposal()
    d = gated(p)
    assert d.token is not None
    forged = d.model_copy(update={"token": d.token[:-2] + ("AA" if d.token[-2:] != "AA" else "BB")})
    assert refused(p, forged, approved(p))[0] is RefusalCode.TOKEN_INVALID


def test_refuses_decision_for_other_proposal() -> None:
    p, other = proposal(), proposal(thesis="other")
    assert refused(p, gated(other), approved(p))[0] is RefusalCode.DECISION_MISMATCH


def test_refuses_token_lifted_onto_modified_proposal() -> None:
    """A valid token from proposal A, pasted into a decision relabelled for B, fails binding."""
    a = proposal()
    b = proposal(sizing=Sizing(contracts=5, notional=D("1"), pct_equity=0.01))
    d = gated(a).model_copy(update={"proposal_hash": proposal_hash(b)})
    assert refused(b, d, approved(b))[0] is RefusalCode.TOKEN_INVALID


def test_refuses_expired_token() -> None:
    p = proposal()
    code, _ = refused(p, gated(p), approved(p), now=p.expires_at)
    assert code is RefusalCode.TOKEN_INVALID


def test_refuses_when_secret_missing_or_rotated() -> None:
    p = proposal()
    d = gated(p)
    assert refused(p, d, approved(p), config=cfg(gate_secret=None))[0] is RefusalCode.TOKEN_INVALID
    rotated = cfg(gate_secret="r" * 32)
    assert refused(p, d, approved(p), config=rotated)[0] is RefusalCode.TOKEN_INVALID


# ---------------------------------------------------------------------------
# Refusals: approval side
# ---------------------------------------------------------------------------


def test_refuses_without_approval() -> None:
    p = proposal()
    assert refused(p, gated(p), None)[0] is RefusalCode.NO_APPROVAL


def test_refuses_approval_for_other_proposal() -> None:
    p, other = proposal(), proposal(thesis="other")
    assert refused(p, gated(p), approved(other))[0] is RefusalCode.APPROVAL_MISMATCH


@pytest.mark.parametrize("decision", [ApprovalDecision.REJECTED, ApprovalDecision.EXPIRED])
def test_refuses_non_approved_decision(decision: ApprovalDecision) -> None:
    p = proposal()
    code, _ = refused(p, gated(p), approved(p, decision=decision))
    assert code is RefusalCode.NOT_APPROVED


@pytest.mark.parametrize(
    "at",
    [
        NOW + dt.timedelta(seconds=1),  # in the future
        dt.datetime(2026, 10, 9, 9, 59),  # naive
    ],
)
def test_refuses_bad_approval_time(at: dt.datetime) -> None:
    p = proposal()
    assert refused(p, gated(p), approved(p, at=at))[0] is RefusalCode.APPROVAL_TIME


def test_refuses_approval_at_or_after_proposal_expiry() -> None:
    p = proposal(expires_at=NOW + dt.timedelta(minutes=10))
    late = approved(p, at=p.expires_at)
    # `now` before expiry so only the approval timestamp is wrong
    assert refused(p, gated(p), late, now=NOW)[0] is RefusalCode.APPROVAL_TIME


# ---------------------------------------------------------------------------
# Refusals: environment
# ---------------------------------------------------------------------------


def test_refuses_naive_now() -> None:
    p = proposal()
    code, _ = refused(p, gated(p), approved(p), now=dt.datetime(2026, 10, 9, 10, 0))
    assert code is RefusalCode.BAD_TIME


def test_refuses_non_paper_env() -> None:
    p = proposal()
    c = cfg()
    c.env = ArcEnv.LIVE  # bypass the live.env validator: submit() must still refuse
    assert refused(p, gated(p), approved(p), config=c)[0] is RefusalCode.NOT_PAPER


def test_refusal_str() -> None:
    assert str(SubmitRefused(RefusalCode.NO_APPROVAL)) == "no_approval"
    assert str(SubmitRefused(RefusalCode.NO_APPROVAL, "x")) == "no_approval: x"


# ---------------------------------------------------------------------------
# Halt (Sentinel S-4): re-read at submit time, before token/approval, fail closed
# ---------------------------------------------------------------------------


def test_submit_refuses_when_halted() -> None:
    p = proposal()
    sw = switch()
    sw.halt(actor="U1", reason="vol spike", now=NOW)
    code, broker = refused(p, gated(p, BAND), approved(p), halt=sw)
    assert code is RefusalCode.HALTED
    assert broker.orders == []


def test_submit_refuses_halted_before_checking_the_token() -> None:
    """Halt wins even over a missing decision: it is checked first."""
    p = proposal()
    sw = switch()
    sw.halt(actor="U1", reason="x", now=NOW)
    assert refused(p, None, None, halt=sw)[0] is RefusalCode.HALTED


def test_submit_without_halt_switch_fails_closed() -> None:
    p = proposal()
    assert refused(p, gated(p, BAND), approved(p), halt=None)[0] is RefusalCode.HALTED


# ---------------------------------------------------------------------------
# arc2 price band (D24)
# ---------------------------------------------------------------------------


def test_arc2_steps_inside_band_use_unique_ids() -> None:
    p = proposal()
    d = gated(p, BAND)
    assert d.token is not None and d.token.startswith("arc2.")
    ids = []
    for k, price in enumerate(BAND.ladder(D("0.01"))):
        _, broker = go(p, d, approved(p), step=k, limit_price=price)
        (order,) = broker.orders
        assert order.limit_price == price
        assert order.client_order_id == f"{d.token}.s{k}"
        assert len(order.client_order_id) <= 128
        ids.append(order.client_order_id)
    assert len(set(ids)) == 4


def test_arc2_refuses_price_outside_band() -> None:
    p = proposal()
    d = gated(p, BAND)
    for bad in (D("-0.86"), D("-0.75")):
        code, _ = refused(p, d, approved(p), step=1, limit_price=bad)
        assert code is RefusalCode.TOKEN_INVALID


def test_arc2_refuses_step_beyond_max_steps() -> None:
    p = proposal()
    d = gated(p, BAND)
    code, _ = refused(p, d, approved(p), step=4, limit_price=D("-0.80"))
    assert code is RefusalCode.TOKEN_INVALID


def test_arc1_legacy_token_allows_one_exact_attempt_only() -> None:
    p = proposal()
    d = gated(p)  # arc1
    assert d.token is not None and d.token.startswith("arc1.")
    _, broker = go(p, d, approved(p))
    assert broker.orders[0].client_order_id == d.token
    assert refused(p, d, approved(p), step=1)[0] is RefusalCode.TOKEN_INVALID
    code, _ = refused(p, d, approved(p), limit_price=D("-0.84"))
    assert code is RefusalCode.TOKEN_INVALID


def test_attempt_order_id() -> None:
    p = proposal()
    arc2 = gated(p, BAND).token
    arc1 = gated(p).token
    assert arc2 is not None and arc1 is not None
    assert attempt_order_id(arc2, 2) == f"{arc2}.s2"
    assert attempt_order_id(arc1, 0) == arc1
    with pytest.raises(TokenError):
        attempt_order_id(arc1, 1)
