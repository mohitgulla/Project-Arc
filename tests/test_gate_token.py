"""Gate token: mint/verify, payload binding, expiry, tamper resistance (E3.2).

Part of ``make test-gate`` (100% branch coverage on ``arc.gate``).
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal as D

import pytest
from hypothesis import given
from hypothesis import strategies as st

from arc.config import ArcSettings
from arc.gate import (
    GateToken,
    OrderPayload,
    TokenError,
    TokenErrorCode,
    gate_secret,
    issue_token,
    mint,
    order_payload,
    payload_hash,
    proposal_hash,
    verify,
)
from arc.gate import token as T
from arc.models import GateDecision, Leg, LegIntent, Proposal, QuantMetrics, Sizing, Structure
from arc.structures import credit_vertical, format_occ
from arc.utils.calendar import ET

SECRET = b"s" * 32
OTHER = b"o" * 32
NOW = dt.datetime(2026, 10, 9, 10, 0, tzinfo=ET)
EXP = dt.date(2026, 11, 20)
LP = format_occ("SPY", EXP, "put", 565)
SP = format_occ("SPY", EXP, "put", 570)


def bull_put() -> Structure:
    return credit_vertical(
        "put",
        "SPY",
        EXP,
        short_strike=570,
        short_premium="2.10",
        long_strike=565,
        long_premium="1.25",
        as_of=dt.date(2026, 10, 9),
    )


def make_proposal(**kw: object) -> Proposal:
    base: dict[str, object] = {
        "candidate_id": "cand_1",
        "structure": bull_put(),
        "thesis": "t",
        "quant": QuantMetrics(pop=0.7, ev=D("10")),
        "sizing": Sizing(contracts=2, notional=D("830"), pct_equity=0.0083),
        "expires_at": NOW + dt.timedelta(minutes=10),
    }
    base.update(kw)
    return Proposal(**base)  # type: ignore[arg-type]


def passed(p: Proposal) -> GateDecision:
    return GateDecision(proposal_hash=proposal_hash(p), passed=True)


def signed(p: Proposal | None = None, **kw: object) -> str:
    p = p or make_proposal()
    args: dict[str, object] = {
        "order": order_payload(p),
        "secret": SECRET,
        "expires_at": p.expires_at,
        "now": NOW,
    }
    args.update(kw)
    return mint(proposal_hash(p), passed(p), **args)  # type: ignore[arg-type]


def code_of(exc: pytest.ExceptionInfo[TokenError]) -> TokenErrorCode:
    return exc.value.code


# ---------------------------------------------------------------------------
# Order payload
# ---------------------------------------------------------------------------


def test_order_payload_from_proposal_uses_mid_when_no_limit() -> None:
    p = make_proposal()
    op = order_payload(p)
    assert op.qty == 2
    assert op.limit_price == D("-0.85")
    assert {(x.symbol, x.side, x.ratio_qty) for x in op.legs} == {
        (LP, "buy", 1),
        (SP, "sell", 1),
    }


def test_order_payload_uses_explicit_limit() -> None:
    assert order_payload(make_proposal(limit_price=D("-0.80"))).limit_price == D("-0.80")


def test_canonical_json_is_leg_order_and_format_independent() -> None:
    a = OrderPayload.from_values([(LP, "buy", 1), (SP, "sell", 1)], qty=2, limit_price="-0.850")
    b = OrderPayload.from_values(
        [(SP.lower(), "SELL", "1"), (LP, " buy ", 1.0)], qty="2", limit_price=D("-0.85")
    )
    assert a.canonical_json() == b.canonical_json()
    assert payload_hash(a) == payload_hash(b)
    assert '"limit_price":"-0.85"' in a.canonical_json()


def test_zero_limit_canonicalises() -> None:
    a = OrderPayload.from_values([(LP, "buy", 1)], qty=1, limit_price="0.00")
    b = OrderPayload.from_values([(LP, "buy", 1)], qty=1, limit_price="-0")
    assert payload_hash(a) == payload_hash(b)
    assert '"limit_price":"0"' in a.canonical_json()


def test_large_limit_has_no_exponent() -> None:
    op = OrderPayload.from_values([(LP, "buy", 1)], qty=1, limit_price="100")
    assert '"limit_price":"100"' in op.canonical_json()


@pytest.mark.parametrize(
    ("legs", "qty", "limit"),
    [
        ([(LP, "buy", 1)], 0, "1"),  # qty < 1
        ([(LP, "buy", 1)], "1.5", "1"),  # fractional qty
        ([(LP, "buy", 0)], 1, "1"),  # ratio < 1
        ([(LP, "hold", 1)], 1, "1"),  # bad side
        ([(LP, "buy", 1)], 1, "abc"),  # not a number
        ([(LP, "buy", 1)], 1, "NaN"),  # not finite
        ([(LP, "buy", 1)], 1, None),  # missing
        ([(LP, "buy", 1)], True, "1"),  # bool is not a number
        ([], 1, "1"),  # no legs
        ([(LP, "buy", 1)] * 5, 1, "1"),  # > 4 legs
    ],
)
def test_bad_payload_values_raise_token_error(legs: list, qty: object, limit: object) -> None:
    with pytest.raises(TokenError) as e:
        OrderPayload.from_values(legs, qty=qty, limit_price=limit)
    assert code_of(e) is TokenErrorCode.BAD_PAYLOAD


def test_order_payload_model_rejects_non_finite_limit_directly() -> None:
    with pytest.raises(ValueError, match="finite"):
        OrderPayload(legs=(T.OrderLeg(symbol=LP, side="buy"),), qty=1, limit_price=D("Infinity"))


# ---------------------------------------------------------------------------
# mint / verify round trip
# ---------------------------------------------------------------------------


def test_round_trip_binds_proposal_and_order() -> None:
    p = make_proposal()
    tok = signed(p)
    assert len(tok) <= 128  # fits Alpaca client_order_id
    t = verify(tok, secret=SECRET, now=NOW, proposal_hash=proposal_hash(p), order=order_payload(p))
    assert isinstance(t, GateToken)
    assert t.encode() == tok
    assert t.expires_at == p.expires_at


def test_verify_signature_only_when_no_binding_given() -> None:
    verify(signed(), secret=SECRET, now=NOW)


def test_wrong_secret_fails() -> None:
    with pytest.raises(TokenError) as e:
        verify(signed(), secret=OTHER, now=NOW)
    assert code_of(e) is TokenErrorCode.BAD_SIGNATURE


def test_expired_at_boundary() -> None:
    p = make_proposal()
    tok = signed(p)
    verify(tok, secret=SECRET, now=p.expires_at - dt.timedelta(seconds=1))
    with pytest.raises(TokenError) as e:
        verify(tok, secret=SECRET, now=p.expires_at)
    assert code_of(e) is TokenErrorCode.EXPIRED


def test_proposal_mismatch() -> None:
    other = make_proposal(thesis="different")
    with pytest.raises(TokenError) as e:
        verify(signed(), secret=SECRET, now=NOW, proposal_hash=proposal_hash(other))
    assert code_of(e) is TokenErrorCode.PROPOSAL_MISMATCH


@pytest.mark.parametrize(
    "mutate",
    [
        lambda op: op.model_copy(update={"qty": op.qty + 1}),
        lambda op: op.model_copy(update={"limit_price": op.limit_price + D("0.01")}),
        lambda op: op.model_copy(update={"legs": op.legs[:1]}),
        lambda op: op.model_copy(
            update={
                "legs": tuple(
                    x.model_copy(update={"side": "buy" if x.side == "sell" else "sell"})
                    for x in op.legs
                )
            }
        ),
    ],
)
def test_order_mismatch(mutate) -> None:
    p = make_proposal()
    with pytest.raises(TokenError) as e:
        verify(signed(p), secret=SECRET, now=NOW, order=mutate(order_payload(p)))
    assert code_of(e) is TokenErrorCode.ORDER_MISMATCH


@pytest.mark.parametrize("bad", [None, ""])
def test_missing_token(bad: object) -> None:
    with pytest.raises(TokenError) as e:
        verify(bad, secret=SECRET, now=NOW)
    assert code_of(e) is TokenErrorCode.MISSING


@pytest.mark.parametrize(
    "bad",
    [
        123,
        "x" * 200,
        "arc1.abc",
        "arc2" + signed()[4:],
        signed() + "A",
        signed().replace(".", ":"),
        " " + signed(),
    ],
)
def test_malformed_token(bad: object) -> None:
    with pytest.raises(TokenError) as e:
        verify(bad, secret=SECRET, now=NOW)
    assert code_of(e) is TokenErrorCode.MALFORMED


def test_tampered_expiry_fails_signature() -> None:
    tok = signed()
    parts = tok.split(".")
    parts[3] = str(int(parts[3]) + 3600)
    with pytest.raises(TokenError) as e:
        verify(".".join(parts), secret=SECRET, now=NOW)
    assert code_of(e) is TokenErrorCode.BAD_SIGNATURE


@given(st.integers(min_value=5, max_value=104), st.sampled_from("ABCxyz019_-"))
def test_any_single_char_tamper_is_rejected(pos: int, ch: str) -> None:
    tok = signed()
    if tok[pos] == ch or tok[pos] == ".":
        return
    bad = tok[:pos] + ch + tok[pos + 1 :]
    with pytest.raises(TokenError):
        verify(bad, secret=SECRET, now=NOW, proposal_hash=proposal_hash(make_proposal()))


@given(
    qty=st.integers(min_value=1, max_value=50),
    cents=st.integers(min_value=-500, max_value=500),
    other_cents=st.integers(min_value=-500, max_value=500),
)
def test_property_token_binds_exact_limit(qty: int, cents: int, other_cents: int) -> None:
    p = make_proposal(
        sizing=Sizing(contracts=qty, notional=D("1"), pct_equity=0.01),
        limit_price=D(cents) / 100,
    )
    tok = signed(p)
    other = order_payload(p).model_copy(update={"limit_price": D(other_cents) / 100})
    if cents == other_cents:
        verify(tok, secret=SECRET, now=NOW, order=other)
    else:
        with pytest.raises(TokenError):
            verify(tok, secret=SECRET, now=NOW, order=other)


# ---------------------------------------------------------------------------
# mint refusals
# ---------------------------------------------------------------------------


def test_mint_refuses_failed_decision() -> None:
    p = make_proposal()
    d = GateDecision(proposal_hash=proposal_hash(p), passed=False, violations=["halted: x"])
    with pytest.raises(TokenError) as e:
        mint(
            d.proposal_hash,
            d,
            order=order_payload(p),
            secret=SECRET,
            expires_at=p.expires_at,
            now=NOW,
        )
    assert code_of(e) is TokenErrorCode.NOT_PASSED


def test_mint_refuses_passed_flag_with_violations() -> None:
    p = make_proposal()
    d = GateDecision(proposal_hash=proposal_hash(p), passed=True, violations=["x"])
    with pytest.raises(TokenError) as e:
        mint(
            d.proposal_hash,
            d,
            order=order_payload(p),
            secret=SECRET,
            expires_at=p.expires_at,
            now=NOW,
        )
    assert code_of(e) is TokenErrorCode.NOT_PASSED


def test_mint_refuses_decision_for_other_proposal() -> None:
    p = make_proposal()
    other = proposal_hash(make_proposal(thesis="other"))
    with pytest.raises(TokenError) as e:
        mint(
            other,
            passed(p),
            order=order_payload(p),
            secret=SECRET,
            expires_at=p.expires_at,
            now=NOW,
        )
    assert code_of(e) is TokenErrorCode.PROPOSAL_MISMATCH


def test_mint_refuses_past_expiry() -> None:
    with pytest.raises(TokenError) as e:
        signed(expires_at=NOW)
    assert code_of(e) is TokenErrorCode.BAD_EXPIRY


@pytest.mark.parametrize("field", ["now", "expires_at"])
def test_naive_times_refused(field: str) -> None:
    with pytest.raises(TokenError) as e:
        signed(**{field: dt.datetime(2026, 10, 9, 10, 5)})
    assert code_of(e) is TokenErrorCode.BAD_EXPIRY


def test_verify_naive_now_refused() -> None:
    with pytest.raises(TokenError):
        verify(signed(), secret=SECRET, now=dt.datetime(2026, 10, 9, 10, 5))


@pytest.mark.parametrize("bad", [b"", b"short", "s" * 32])
def test_short_or_non_bytes_secret_refused(bad: object) -> None:
    with pytest.raises(TokenError) as e:
        signed(secret=bad)
    assert code_of(e) is TokenErrorCode.BAD_SECRET
    with pytest.raises(TokenError):
        verify("x", secret=bad, now=NOW)  # type: ignore[arg-type]


@pytest.mark.parametrize("bad", ["zz" * 32, "ab" * 16])
def test_bad_hash_text_refused(bad: str) -> None:
    p = make_proposal()
    d = GateDecision(proposal_hash=bad, passed=True)
    with pytest.raises(TokenError) as e:
        mint(bad, d, order=order_payload(p), secret=SECRET, expires_at=p.expires_at, now=NOW)
    assert code_of(e) is TokenErrorCode.BAD_PAYLOAD


# ---------------------------------------------------------------------------
# issue_token / gate_secret
# ---------------------------------------------------------------------------


def test_issue_token_sets_token_on_pass_only() -> None:
    p = make_proposal()
    d = issue_token(passed(p), p, secret=SECRET, now=NOW)
    assert d.token is not None
    verify(d.token, secret=SECRET, now=NOW, proposal_hash=proposal_hash(p), order=order_payload(p))
    failed = GateDecision(proposal_hash=proposal_hash(p), passed=False, violations=["x"])
    assert issue_token(failed, p, secret=SECRET, now=NOW) is failed


def test_order_payload_ratio_from_leg() -> None:
    s = Structure(
        legs=[Leg(occ_symbol=LP, side=LegIntent.LONG, ratio=2, premium=D("1"))],
        net_debit_credit=D("2"),
        dte=42,
    )
    op = order_payload(make_proposal(structure=s))
    assert op.legs[0].ratio_qty == 2


def test_gate_secret_from_config() -> None:
    cfg = ArcSettings(_env_file=None, gate_secret="k" * 32)  # type: ignore[call-arg]
    assert gate_secret(cfg) == b"k" * 32


@pytest.mark.parametrize("value", [None, "short"])
def test_gate_secret_missing_or_short(value: str | None) -> None:
    cfg = ArcSettings(_env_file=None, gate_secret=value)  # type: ignore[call-arg]
    with pytest.raises(TokenError) as e:
        gate_secret(cfg)
    assert code_of(e) is TokenErrorCode.BAD_SECRET


def test_token_error_str() -> None:
    assert str(TokenError(TokenErrorCode.MISSING)) == "missing"
    assert str(TokenError(TokenErrorCode.MISSING, "x")) == "missing: x"


def test_secret_not_in_repr() -> None:
    cfg = ArcSettings(_env_file=None, gate_secret="k" * 32)  # type: ignore[call-arg]
    assert "kkkk" not in repr(cfg)
