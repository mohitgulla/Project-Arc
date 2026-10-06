"""arc-gate hook policy: which broker tool calls are allowed (E3.2).

Part of ``make test-gate`` (100% branch coverage on ``arc.gate``).
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal as D

import pytest
from hypothesis import given
from hypothesis import strategies as st

from arc.gate import hook_policy as H
from arc.gate import mint, order_payload, proposal_hash
from arc.gate.hook_policy import BLOCKED_TOOLS, check_tool_call, find_arc_execute
from arc.models import GateDecision, Proposal, QuantMetrics, Sizing
from arc.structures import credit_vertical, format_occ, long_put
from arc.utils.calendar import ET

SECRET = b"s" * 32
NOW = dt.datetime(2026, 10, 9, 10, 0, tzinfo=ET)
EXP = dt.date(2026, 11, 20)
LP = format_occ("SPY", EXP, "put", 565)
SP = format_occ("SPY", EXP, "put", 570)
TOOL = "mcp__alpaca__place_option_order"


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


def token_for(p: Proposal) -> str:
    d = GateDecision(proposal_hash=proposal_hash(p), passed=True, token=None)
    return mint(
        d.proposal_hash,
        d,
        order=order_payload(p),
        secret=SECRET,
        expires_at=p.expires_at,
        now=NOW,
    )


def mleg_args(token: str | None, **kw: object) -> dict[str, object]:
    args: dict[str, object] = {
        "qty": "2",
        "type": "limit",
        "time_in_force": "day",
        "limit_price": "-0.85",
        "order_class": "mleg",
        "legs": [
            {"symbol": SP, "side": "sell", "ratio_qty": "1"},
            {"symbol": LP, "side": "buy", "ratio_qty": "1"},
        ],
    }
    if token is not None:
        args["client_order_id"] = token
    args.update(kw)
    return args


def check(tool: object, args: object, secret: bytes | None = SECRET, now: dt.datetime = NOW):
    return check_tool_call(tool, args, secret=secret, now=now)


# ---------------------------------------------------------------------------
# MCP place_option_order
# ---------------------------------------------------------------------------


def test_signed_mleg_order_allowed() -> None:
    v = check(TOOL, mleg_args(token_for(proposal())))
    assert v.allow, v.message
    assert v.message == ""


def test_unsigned_mleg_order_blocked() -> None:
    v = check(TOOL, mleg_args(None))
    assert not v.allow
    assert v.message.startswith("arc-gate: order refused")
    assert "missing" in v.message


def test_forged_token_blocked() -> None:
    tok = token_for(proposal())
    forged = tok[:-3] + ("AAA" if not tok.endswith("AAA") else "BBB")
    assert not check(TOOL, mleg_args(forged)).allow


def test_token_from_other_secret_blocked() -> None:
    assert not check(TOOL, mleg_args(token_for(proposal())), secret=b"x" * 32).allow


def test_expired_token_blocked() -> None:
    p = proposal()
    assert not check(TOOL, mleg_args(token_for(p)), now=p.expires_at).allow


@pytest.mark.parametrize(
    "change",
    [
        {"qty": "3"},
        {"limit_price": "-0.84"},
        {"legs": [{"symbol": SP, "side": "sell", "ratio_qty": "1"}]},
        {
            "legs": [
                {"symbol": SP, "side": "buy", "ratio_qty": "1"},
                {"symbol": LP, "side": "sell", "ratio_qty": "1"},
            ]
        },
        {
            "legs": [
                {"symbol": SP, "side": "sell", "ratio_qty": "2"},
                {"symbol": LP, "side": "buy", "ratio_qty": "2"},
            ]
        },
    ],
)
def test_token_reused_for_different_order_blocked(change: dict[str, object]) -> None:
    v = check(TOOL, mleg_args(token_for(proposal()), **change))
    assert not v.allow
    assert "order_mismatch" in v.message or "bad_payload" in v.message


@pytest.mark.parametrize(
    "change",
    [
        {"type": "market"},
        {"time_in_force": "gtc"},
        {"stop_price": "1"},
        {"order_class": "bracket"},
        {"legs": "nope"},
        {"legs": [1, 2]},
        {"legs": None},  # order_class=mleg without legs
        {"symbol": SP},
        {"side": "buy"},
        {"position_intent": "sell_to_open"},
        {
            "legs": [
                {"symbol": SP, "side": "sell", "ratio_qty": "1", "position_intent": "x"},
                {"symbol": LP, "side": "buy", "ratio_qty": "1"},
            ]
        },
        {"legs": [{"symbol": SP, "ratio_qty": "1"}, {"symbol": LP, "side": "buy"}]},
    ],
)
def test_ungateable_shapes_blocked(change: dict[str, object]) -> None:
    v = check(TOOL, mleg_args(token_for(proposal()), **change))
    assert not v.allow
    assert "bad_payload" in v.message


def test_market_is_default_type_and_blocked() -> None:
    args = mleg_args(token_for(proposal()))
    del args["type"]
    assert not check(TOOL, args).allow


def test_legs_infer_mleg_without_order_class() -> None:
    args = mleg_args(token_for(proposal()))
    del args["order_class"]
    del args["time_in_force"]  # default day
    assert check(TOOL, args).allow


def test_single_leg_order_signed_allowed_and_unsigned_blocked() -> None:
    p = proposal(structure=long_put("SPY", EXP, 565, "1.25", as_of=dt.date(2026, 10, 9)))
    args = {
        "qty": "2",
        "type": "limit",
        "symbol": LP,
        "side": "buy",
        "limit_price": "1.25",
        "client_order_id": token_for(p),
    }
    assert check(TOOL, args).allow
    assert check(TOOL, {**args, "order_class": "simple"}).allow
    assert not check(TOOL, {**args, "order_class": "oto"}).allow
    assert not check(TOOL, {**args, "side": "sell"}).allow
    no_tok = {k: v for k, v in args.items() if k != "client_order_id"}
    assert not check(TOOL, no_tok).allow


def test_no_secret_blocks_orders() -> None:
    v = check(TOOL, mleg_args(token_for(proposal())), secret=None)
    assert not v.allow
    assert "ARC_GATE_SECRET" in v.message


@given(
    st.dictionaries(st.text(max_size=8), st.one_of(st.none(), st.text(max_size=8), st.integers()))
)
def test_property_random_args_never_allowed_without_token(args: dict) -> None:
    args.pop("client_order_id", None)
    assert not check(TOOL, args).allow


# ---------------------------------------------------------------------------
# Always-blocked tools, unknown tools, bad input
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tool", sorted(BLOCKED_TOOLS))
def test_blocked_tools_always_blocked(tool: str) -> None:
    v = check(f"mcp__alpaca__{tool}", {"client_order_id": token_for(proposal())})
    assert not v.allow
    assert "never allowed" in v.message


def test_prefix_variants_are_recognised() -> None:
    assert H.base_tool_name("mcp__alpaca_paper__place_option_order") == "place_option_order"
    assert H.base_tool_name("place_option_order") == "place_option_order"
    assert not check("mcp__alpaca_paper__place_option_order", mleg_args(None)).allow
    assert check("place_option_order", mleg_args(token_for(proposal()))).allow


@pytest.mark.parametrize("tool", ["mcp__alpaca__get_orders", "read_file", "mcp__x__y"])
def test_unrecognised_tool_routed_here_is_blocked(tool: str) -> None:
    v = check(tool, {})
    assert not v.allow
    assert "unrecognised" in v.message


@pytest.mark.parametrize("tool", [None, "", 3])
def test_missing_tool_name_blocked(tool: object) -> None:
    assert not check(tool, {}).allow


@pytest.mark.parametrize("args", [None, [], "x"])
def test_non_object_args_blocked(args: object) -> None:
    assert not check(TOOL, args).allow


def test_internal_error_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a: object, **k: object) -> None:
        raise RuntimeError("kaboom")

    monkeypatch.setattr(H, "_check", boom)
    v = check(TOOL, mleg_args(token_for(proposal())))
    assert not v.allow
    assert "failing closed" in v.message
    assert "kaboom" in v.message


# ---------------------------------------------------------------------------
# terminal: arc execute
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "ls -la",
        "uv run pytest -q",
        "uv run arc scan",
        "arc gate --proposal p.json",
        "git commit -m 'execute plan'",
        "echo hello | grep execute",
    ],
)
def test_ordinary_terminal_commands_allowed_without_secret(command: str) -> None:
    v = check("terminal", {"command": command}, secret=None)
    assert v.allow, v.message


@pytest.mark.parametrize(
    "command",
    [
        "arc execute",
        "arc execute --proposal p.json",
        "uv run arc execute",
        "/abs/.venv/bin/arc execute",
        "python -m arc.cli execute",
        "python -m arc execute",
        "cd /repo && arc execute",
        "true; arc execute",
        "bash -c 'arc execute'",
        'sh -c "cd /x && uv run arc execute --token nope"',
        "arc --verbose execute",
        "arc execute --token",
        "arc execute --token=garbage",
    ],
)
def test_arc_execute_without_valid_token_blocked(command: str) -> None:
    v = check("terminal", {"command": command})
    assert not v.allow, command
    assert "arc execute" in v.message


def test_arc_execute_with_valid_token_allowed() -> None:
    tok = token_for(proposal())
    assert check("terminal", {"command": f"arc execute --token {tok}"}).allow
    assert check("terminal", {"command": f"uv run arc execute --token={tok} --x y"}).allow


def test_arc_execute_expired_token_blocked() -> None:
    p = proposal()
    cmd = f"arc execute --token {token_for(p)}"
    assert not check("terminal", {"command": cmd}, now=p.expires_at).allow


def test_arc_execute_needs_secret() -> None:
    v = check("terminal", {"command": f"arc execute --token {token_for(proposal())}"}, secret=None)
    assert not v.allow
    assert "ARC_GATE_SECRET" in v.message


def test_second_invocation_checked_too() -> None:
    tok = token_for(proposal())
    assert not check("terminal", {"command": f"arc execute --token {tok}; arc execute"}).allow


def test_terminal_without_string_command_blocked() -> None:
    assert not check("terminal", {"command": 5}).allow
    assert not check("terminal", {}).allow


def test_unparseable_command_mentioning_arc_execute_blocked() -> None:
    v = check("terminal", {"command": "arc execute 'unterminated"})
    assert not v.allow
    assert "could not parse" in v.message


def test_unparseable_command_without_arc_allowed() -> None:
    assert check("terminal", {"command": "echo 'unterminated"}, secret=None).allow


def test_find_arc_execute_nested_shells() -> None:
    assert find_arc_execute("sh -c 'bash -c \"arc execute\"'") == [["execute"]]


def test_find_arc_execute_depth_exceeded_is_none() -> None:
    assert find_arc_execute("arc execute", _depth=H._MAX_NESTING + 1) is None


def test_find_arc_execute_nested_unparseable_is_none() -> None:
    assert find_arc_execute('bash -c "arc execute \'x"') is None


def test_find_arc_execute_reports_args() -> None:
    assert find_arc_execute("arc execute --token T && ls") == [["execute", "--token", "T"]]
    assert find_arc_execute("python -m pytest") == []
    assert find_arc_execute("python -m") == []


# ---------------------------------------------------------------------------
# E13.11 (D1/D56): Robinhood Agentic MCP tools are always blocked
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tool", sorted(H.ROBINHOOD_ORDER_TOOLS | {"get_portfolio"}))
@pytest.mark.parametrize("server", ["robinhood", "Robinhood_Trading", "rh-robinhood"])
def test_robinhood_mcp_tools_always_blocked(server: str, tool: str) -> None:
    name = f"mcp__{server}__{tool}"
    assert H.is_robinhood_tool(name)
    v = check(name, mleg_args(token_for(proposal())))  # even a valid Alpaca token
    assert not v.allow
    assert "Robinhood is not an enabled venue" in v.message


def test_robinhood_order_tools_blocked_on_any_server() -> None:
    for tool in ("cancel_option_order", "exercise_option"):
        assert tool in BLOCKED_TOOLS
        assert not check(f"mcp__other__{tool}", {}).allow
    assert not H.is_robinhood_tool("mcp__alpaca__place_option_order")
    assert not H.is_robinhood_tool("robinhood_place_option_order")  # not an MCP name
    # Alpaca's gated order path is unchanged
    assert check(TOOL, mleg_args(token_for(proposal()))).allow
