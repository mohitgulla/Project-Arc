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


def main(stdin: str) -> int:
    try:
        payload = json.loads(stdin)
        if not isinstance(payload, dict):
            return _emit(False, "arc-gate: hook payload is not a JSON object")
        tool_name = payload.get("tool_name")
        tool_input = payload.get("tool_input")
        if _fast_allow(tool_name, tool_input):
            return _emit(True)

        from arc.gate.hook_policy import check_tool_call
        from arc.utils.calendar import now_et

        verdict = check_tool_call(tool_name, tool_input, secret=_secret(), now=now_et())
        return _emit(verdict.allow, verdict.message)
    except Exception as exc:  # noqa: BLE001 — fail closed
        return _emit(False, f"arc-gate: hook error, failing closed: {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    sys.exit(main(sys.stdin.read()))
