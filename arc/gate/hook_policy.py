"""Policy for the Hermes ``pre_tool_call`` arc-gate hook (PLAN §2.1 boundary 2; card E3.2).

Defence in depth for the MCP path: whatever a persona session can reach, a
broker order tool only runs if its arguments carry a gate token that verifies
against the *exact* order in those arguments. Pure: the caller supplies the
secret and ``now``; the I/O shim is ``hermes/hooks/arc-gate/hook.py``.

Fail closed everywhere: an unknown tool, a malformed argument, a missing secret
or any exception is a block.

Tool coverage (Alpaca MCP server v2 names, any ``mcp__<server>__`` prefix):

- ``place_option_order``: allowed only as a *limit*, *day* order whose
  ``client_order_id`` is a gate token bound to exactly these legs, qty and limit.
- ``place_stock_order``, ``place_crypto_order``, ``replace_order_by_id``,
  ``close_position``, ``close_all_positions``, ``exercise_options_position``:
  always blocked. None can be bound to a gated options payload (stock/crypto
  are out of scope, a replace carries no legs, close/exercise are unpriced).
  Phase-1 orders go through ``arc.execution.submit()`` (PLAN §2.6).
- ``terminal``: blocked only when the command runs ``arc execute`` without a
  ``--token`` that verifies (signature + expiry; ``submit()`` then checks the
  proposal and order binding). Every other command is allowed.
"""

from __future__ import annotations

import re
import shlex
from typing import TYPE_CHECKING, Any, NamedTuple

from arc.gate.token import OrderPayload, TokenError, TokenErrorCode, verify

if TYPE_CHECKING:
    import datetime as dt

__all__ = [
    "BLOCKED_TOOLS",
    "GATED_TOOLS",
    "HookVerdict",
    "base_tool_name",
    "check_tool_call",
    "find_arc_execute",
]

GATED_TOOLS = frozenset({"place_option_order"})
BLOCKED_TOOLS = frozenset(
    {
        "place_stock_order",
        "place_crypto_order",
        "replace_order_by_id",
        "close_position",
        "close_all_positions",
        "exercise_options_position",
    }
)
_TERMINAL = "terminal"
_MCP_NAME = re.compile(r"^mcp__.+?__(?P<tool>[A-Za-z0-9_-]+)$")
_ARC_EXECUTE_LOOSE = re.compile(r"\barc(?:\.cli)?\b.*\bexecute\b", re.DOTALL)
_SEPARATORS = frozenset({";", "&", "&&", "|", "||", "(", ")", "\n"})


class HookVerdict(NamedTuple):
    """``allow`` or block with a ``message`` (the message becomes the tool result)."""

    allow: bool
    message: str = ""


def _block(message: str) -> HookVerdict:
    return HookVerdict(False, f"arc-gate: {message}")


_ALLOW = HookVerdict(True)


def base_tool_name(tool_name: str) -> str:
    """``mcp__alpaca__place_option_order`` -> ``place_option_order``; others unchanged."""
    m = _MCP_NAME.fullmatch(tool_name)
    return m["tool"] if m else tool_name


# ---------------------------------------------------------------------------
# terminal: `arc execute`
# ---------------------------------------------------------------------------


def _is_arc_program(word: str) -> bool:
    return word.rsplit("/", 1)[-1] == "arc"


def _split_commands(command: str) -> list[list[str]]:
    lex = shlex.shlex(command, posix=True, punctuation_chars=";&|()\n")
    lex.whitespace = " \t\r"
    lex.whitespace_split = True
    commands: list[list[str]] = [[]]
    for word in lex:
        if word in _SEPARATORS:
            commands.append([])
        else:
            commands[-1].append(word)
    return [c for c in commands if c]


def _arc_subcommand(argv: list[str]) -> list[str] | None:
    """The argv after ``arc`` / ``-m arc.cli`` if this simple command runs the Arc CLI."""
    for i, word in enumerate(argv):
        if _is_arc_program(word):
            return argv[i + 1 :]
        if word == "-m" and i + 1 < len(argv) and argv[i + 1] in ("arc", "arc.cli"):
            return argv[i + 2 :]
    return None


_MAX_NESTING = 3


def find_arc_execute(command: str, _depth: int = 0) -> list[list[str]] | None:
    """Every ``arc execute`` invocation's args in a shell command; ``[]`` if none.

    Quoted words that themselves contain a command (``bash -c "arc execute"``,
    ``eval '...'``) are scanned recursively. Returns ``None`` when the command
    cannot be tokenised, or nests too deep, while it *looks* like it might run
    ``arc execute`` (the caller blocks: fail closed).

    Best effort only: shell indirection (variables, aliases, scripts) can hide
    an invocation. The real enforcement is ``arc.execution.submit()``.
    """
    if _depth > _MAX_NESTING:
        return None
    try:
        commands = _split_commands(command)
    except ValueError:
        return None if _ARC_EXECUTE_LOOSE.search(command) else []
    found: list[list[str]] = []
    for argv in commands:
        rest = _arc_subcommand(argv)
        if rest is not None and "execute" in rest:
            found.append(rest)
        for word in argv:
            if word != command and any(c in word for c in " \t\n;&|"):
                nested = find_arc_execute(word, _depth + 1)
                if nested is None:
                    return None
                found += nested
    return found


def _token_arg(args: list[str]) -> str | None:
    for i, word in enumerate(args):
        if word.startswith("--token="):
            return word.split("=", 1)[1]
        if word == "--token" and i + 1 < len(args):
            return args[i + 1]
    return None


def _check_terminal(
    tool_input: dict[str, Any], secret: bytes | None, now: dt.datetime
) -> HookVerdict:
    command = tool_input.get("command")
    if not isinstance(command, str):
        return _block("terminal call without a string command")
    invocations = find_arc_execute(command)
    if invocations is None:
        return _block("could not parse a command that may run `arc execute`")
    if invocations and not secret:
        return _block("ARC_GATE_SECRET is not configured")
    for args in invocations:
        try:
            verify(_token_arg(args), secret=secret or b"", now=now)
        except TokenError as exc:
            return _block(f"`arc execute` needs a valid --token ({exc})")
    return _ALLOW  # ordinary shell commands need no secret


# ---------------------------------------------------------------------------
# MCP place_option_order
# ---------------------------------------------------------------------------


_ORDER_KEYS = frozenset(
    {
        "qty",
        "type",
        "time_in_force",
        "symbol",
        "side",
        "limit_price",
        "stop_price",
        "client_order_id",
        "order_class",
        "legs",
    }
)
_LEG_KEYS = frozenset({"symbol", "side", "ratio_qty"})


def _bad(detail: str) -> TokenError:
    return TokenError(TokenErrorCode.BAD_PAYLOAD, detail)


def _mcp_order_payload(args: dict[str, Any]) -> OrderPayload:
    """The canonical payload an MCP ``place_option_order`` call would send; raises.

    Any argument the token cannot bind (e.g. ``position_intent``) is refused.
    """
    unknown = set(args) - _ORDER_KEYS
    if unknown:
        raise _bad(f"arguments not covered by the gate token: {sorted(unknown)}")
    if str(args.get("type", "market")).lower() != "limit":
        raise _bad("only limit orders can be gated")
    if str(args.get("time_in_force", "day")).lower() != "day":
        raise _bad("only time_in_force=day can be gated")
    if args.get("stop_price") is not None:
        raise _bad("stop orders cannot be gated")
    order_class = args.get("order_class")
    legs_in = args.get("legs")
    legs: list[tuple[object, object, object]]
    if legs_in is not None or order_class == "mleg":
        if order_class not in (None, "mleg"):
            raise _bad(f"unsupported order_class {order_class!r}")
        if not isinstance(legs_in, list) or not all(isinstance(x, dict) for x in legs_in):
            raise _bad("legs must be a list of objects")
        if any(set(x) - _LEG_KEYS for x in legs_in):
            raise _bad("leg fields not covered by the gate token")
        if args.get("symbol") is not None or args.get("side") is not None:
            raise _bad("multi-leg order must not set top-level symbol/side")
        legs = [(x.get("symbol"), x.get("side"), x.get("ratio_qty")) for x in legs_in]
    else:
        if order_class not in (None, "simple"):
            raise _bad(f"unsupported order_class {order_class!r}")
        legs = [(args.get("symbol"), args.get("side"), 1)]
    for sym, side, ratio in legs:
        if sym is None or side is None or ratio is None:
            raise _bad("every leg needs symbol, side and ratio_qty")
    return OrderPayload.from_values(legs, qty=args.get("qty"), limit_price=args.get("limit_price"))


def _check_option_order(
    tool_input: dict[str, Any], secret: bytes | None, now: dt.datetime
) -> HookVerdict:
    if not secret:
        return _block("ARC_GATE_SECRET is not configured")
    try:
        payload = _mcp_order_payload(tool_input)
        verify(tool_input.get("client_order_id"), secret=secret, now=now, order=payload)
    except TokenError as exc:
        return _block(f"order refused: no valid gate token for this exact order ({exc})")
    return _ALLOW


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def check_tool_call(
    tool_name: object,
    tool_input: object,
    *,
    secret: bytes | None,
    now: dt.datetime,
) -> HookVerdict:
    """Decide one ``pre_tool_call``. Never raises: any error is a block."""
    try:
        return _check(tool_name, tool_input, secret, now)
    except Exception as exc:  # noqa: BLE001 — fail closed on anything unexpected
        return _block(f"internal error, failing closed: {type(exc).__name__}: {exc}")


def _check(
    tool_name: object, tool_input: object, secret: bytes | None, now: dt.datetime
) -> HookVerdict:
    if not isinstance(tool_name, str) or not tool_name:
        return _block("missing tool name")
    if not isinstance(tool_input, dict):
        return _block(f"{tool_name}: arguments are not an object")
    if tool_name == _TERMINAL:
        return _check_terminal(tool_input, secret, now)
    base = base_tool_name(tool_name)
    if base in BLOCKED_TOOLS:
        return _block(
            f"{base} is never allowed from an agent session; orders go through "
            "arc.execution.submit() with a gate token and an approval"
        )
    if base in GATED_TOOLS:
        return _check_option_order(tool_input, secret, now)
    return _block(f"unrecognised tool {tool_name!r} routed to the order gate")
