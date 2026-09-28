#!/usr/bin/env python3
"""Hermes ``pre_tool_call`` shell hook: arc-gate (PLAN §2.1 boundary 2; card E3.2).

Reads the Hermes hook payload (JSON) on stdin, decides with the pure policy in
``arc.gate.hook_policy``, and answers in the Hermes / Claude-Code hook dialect:

- allow: exit 0, stdout ``{}``
- block: exit 2, stdout ``{"action": "block", "message": "..."}``

Fail closed: if anything goes wrong (bad JSON, ``arc`` not importable, a crash)
this script blocks. Hermes additionally blocks on spawn error, timeout or
garbage stdout because the hook is registered with ``fail_closed: true``
(see HOOK.yaml). This file imports only the standard library until it knows it
has to decide, so a broken ``arc`` install cannot turn into an allow.

Secret: ``ARC_GATE_SECRET`` from the environment, else from ``~/.hermes/.env``
via ``arc.config``. A missing secret blocks every gated call.

Used order ids (D24): every allowed ``place_option_order`` appends its
``client_order_id`` to ``$HERMES_HOME/arc-gate/used_order_ids`` (one per line),
and an order re-using a listed id is blocked. An unreadable or unwritable
ledger blocks the order (fail closed).
"""

from __future__ import annotations

import json
import sys

BLOCK_EXIT = 2


def _emit(allow: bool, message: str = "") -> int:
    if allow:
        sys.stdout.write("{}\n")
        return 0
    sys.stdout.write(json.dumps({"action": "block", "message": message}) + "\n")
    return BLOCK_EXIT


def _fast_allow(tool_name: object, tool_input: object) -> bool:
    """Ordinary terminal commands skip the (slower) arc import.

    A shell command that never contains the word ``execute`` cannot run
    ``arc execute`` literally; indirection is out of scope for this hook and is
    caught by ``arc.execution.submit()`` anyway.
    """
    if tool_name != "terminal" or not isinstance(tool_input, dict):
        return False
    command = tool_input.get("command")
    return isinstance(command, str) and "execute" not in command


def _secret() -> bytes | None:
    import os

    raw = os.environ.get("ARC_GATE_SECRET", "")
    if raw:
        return raw.encode()
    try:
        from arc.config import get_settings
        from arc.gate.token import gate_secret

        return gate_secret(get_settings())
    except Exception:  # noqa: BLE001 — no secret => policy blocks gated calls
        return None


def _ledger_path() -> object:
    import os
    from pathlib import Path

    home = os.environ.get("HERMES_HOME") or str(Path.home() / ".hermes")
    return Path(home) / "arc-gate" / "used_order_ids"


def _used_ids(path: object) -> frozenset[str]:
    from pathlib import Path

    p = Path(str(path))
    if not p.exists():
        return frozenset()
    return frozenset(line.strip() for line in p.read_text().splitlines() if line.strip())


def _record_id(path: object, coid: str) -> None:
    import fcntl
    from pathlib import Path

    p = Path(str(path))
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        fh.write(coid + "\n")


def main(stdin: str) -> int:
    try:
        payload = json.loads(stdin)
        if not isinstance(payload, dict):
            return _emit(False, "arc-gate: hook payload is not a JSON object")
        tool_name = payload.get("tool_name")
        tool_input = payload.get("tool_input")
        if _fast_allow(tool_name, tool_input):
            return _emit(True)

        from arc.gate.hook_policy import GATED_TOOLS, base_tool_name, check_tool_call
        from arc.utils.calendar import now_et

        is_order = isinstance(tool_name, str) and base_tool_name(tool_name) in GATED_TOOLS
        ledger = _ledger_path()
        used = _used_ids(ledger) if is_order else frozenset()
        verdict = check_tool_call(
            tool_name, tool_input, secret=_secret(), now=now_et(), used_order_ids=used
        )
        if verdict.allow and is_order and isinstance(tool_input, dict):
            _record_id(ledger, str(tool_input["client_order_id"]))
        return _emit(verdict.allow, verdict.message)
    except Exception as exc:  # noqa: BLE001 — fail closed
        return _emit(False, f"arc-gate: hook error, failing closed: {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    sys.exit(main(sys.stdin.read()))
