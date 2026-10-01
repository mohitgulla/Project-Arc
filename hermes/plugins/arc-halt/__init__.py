"""arc-halt — Slack `!halt` / `!resume` for Project Arc (E3.3).

Hooks ``pre_gateway_dispatch`` so the command is handled before auth, the agent
loop, and Hermes' built-in ``/resume`` (a session command with the same name):

- ``!halt [reason]`` in any Slack thread halts trading immediately (anyone);
- ``!resume`` clears every halt, owner only (``ARC_OWNER_SLACK_USER_ID``).

State lives in the Arc audit store, not here: the plugin shells out to the
project's ``arc slack-command`` CLI (project venv), which persists the halt
before replying. The sender id comes from the platform event, never from text.

Install: ``cp -r hermes/plugins/arc-halt ~/.hermes/plugins/ && hermes plugins enable arc-halt``.
Override the repo with ``ARC_REPO_DIR``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

REPO_DIR = Path(os.environ.get("ARC_REPO_DIR", str(Path.home() / "GitHub" / "Project-Arc")))
TIMEOUT_S = 20

# Leading @mentions (``<@U123>``), then ``!halt``/``/halt`` or ``!resume``/``/resume``.
_CMD_RE = re.compile(r"^(?:\s*<@[A-Z0-9]+>\s*)*[!/](halt|resume)\b(.*)$", re.IGNORECASE | re.DOTALL)


def parse(text: str | None) -> tuple[str, str] | None:
    """``(verb, rest)`` for an Arc halt command, else None."""
    m = _CMD_RE.match(text or "")
    if not m:
        return None
    return m.group(1).lower(), m.group(2).strip()


def _arc_bin() -> Path:
    return REPO_DIR / ".venv" / "bin" / "arc"


# The gateway runs on Hermes's own Python and exports PYTHONPATH/PYTHONHOME for it.
# Inherited by `arc`, they put Hermes's site-packages ahead of Arc's venv and the CLI
# dies importing a foreign pydantic_core (exit 1). Arc gets a clean env (as arc-status).
_PY_ENV_PREFIXES = ("PYTHON", "VIRTUAL_ENV", "CONDA_", "UV_", "PIP_", "__PYVENV")


def _arc_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """``os.environ`` minus Python/venv variables, with Arc's venv first on PATH."""
    env = {
        k: v
        for k, v in (os.environ if base is None else base).items()
        if not k.startswith(_PY_ENV_PREFIXES)
    }
    venv_bin = str(REPO_DIR / ".venv" / "bin")
    env["PATH"] = os.pathsep.join([venv_bin, env.get("PATH", "")]).rstrip(os.pathsep)
    return env


def run_arc(verb: str, rest: str, user: str) -> str:
    """Apply the command through the Arc CLI; return the reply text."""
    text = f"!{verb} {rest}".strip()
    try:
        out = subprocess.run(
            [str(_arc_bin()), "slack-command", "--user", user, "--text", text],
            cwd=REPO_DIR,
            env=_arc_env(),
            capture_output=True,
            text=True,
            timeout=TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.error("arc-halt: arc CLI failed: %s", exc)
        return f"⚠️ `!{verb}` failed: {exc}. Trading state unchanged — use `arc {verb}` on the host."
    if out.returncode != 0:
        logger.error("arc-halt: arc exited %s: %s", out.returncode, out.stderr[-500:])
        return f"⚠️ `!{verb}` failed (exit {out.returncode}). Check the gateway log."
    return out.stdout.strip()


async def _reply(gateway, source, event, text: str) -> None:
    adapter = getattr(gateway, "adapters", {}).get(source.platform)
    if adapter is None:
        logger.warning("arc-halt: no adapter for %s; reply dropped: %s", source.platform, text)
        return
    metadata = {"thread_id": source.thread_id} if source.thread_id else None
    try:
        await adapter.send(source.chat_id, text, reply_to=event.message_id, metadata=metadata)
    except Exception:  # noqa: BLE001 — the halt is already persisted
        logger.exception("arc-halt: reply failed")


async def on_pre_gateway_dispatch(event=None, gateway=None, session_store=None, **_kw):
    source = getattr(event, "source", None)
    if source is None or getattr(getattr(source, "platform", None), "value", None) != "slack":
        return None
    cmd = parse(getattr(event, "text", ""))
    if cmd is None:
        return None
    verb, rest = cmd
    user = source.user_id or getattr(event, "user_id", None) or ""
    reply = await asyncio.to_thread(run_arc, verb, rest, user)
    await _reply(gateway, source, event, reply)
    return {"action": "skip", "reason": f"arc-halt handled !{verb}"}


def register(ctx) -> None:
    ctx.register_hook("pre_gateway_dispatch", on_pre_gateway_dispatch)
