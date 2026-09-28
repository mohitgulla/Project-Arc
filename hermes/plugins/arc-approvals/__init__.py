"""arc-approvals — Approve / Reject buttons on Arc proposal cards (E6.1).

Arc posts each proposal as a Block Kit card in the #arc-investor day thread with
two buttons: ``arc_approve`` / ``arc_reject``, whose ``value`` is the proposal
hash. This plugin registers Slack action handlers for those ids and hands the
click to the project's ``arc approve decide`` CLI (project venv).

The CLI owns every rule: allowed approver ids, the TTL, resolve-once, the
ApprovalRecord row, and rewriting the card. The clicker's id comes from the
Slack interaction payload (``body.user.id``), never from message text. The
plugin itself only acks within Slack's 3 s window and runs the CLI off the
event loop.

Install: ``cp -r hermes/plugins/arc-approvals ~/.hermes/plugins/ &&
hermes plugins enable arc-approvals`` (gateway picks up Slack action handlers on
connect; restart the gateway once). Override the repo with ``ARC_REPO_DIR``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

REPO_DIR = Path(os.environ.get("ARC_REPO_DIR", str(Path.home() / "GitHub" / "Project-Arc")))
TIMEOUT_S = 30
ACTIONS = {"arc_approve": True, "arc_reject": False}
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")


def _arc_bin() -> Path:
    return REPO_DIR / ".venv" / "bin" / "arc"


def parse_click(body: dict, action: dict) -> tuple[str, bool, str, str] | None:
    """``(proposal_hash, approve, user_id, message_ts)`` for an Arc card click, else None."""
    action_id = (action or {}).get("action_id")
    if action_id not in ACTIONS:
        return None
    value = str((action or {}).get("value") or "")
    if not _HASH_RE.match(value):
        return None
    user = str(((body or {}).get("user") or {}).get("id") or "")
    if not user:
        return None
    msg_ts = str(((body or {}).get("container") or {}).get("message_ts") or "")
    return value, ACTIONS[action_id], user, msg_ts


def run_decide(proposal: str, approve: bool, user: str, msg_ts: str) -> dict:
    """Run ``arc approve decide``; return its JSON result (or an error dict)."""
    cmd = [
        str(_arc_bin()),
        "approve",
        "decide",
        "--proposal",
        proposal,
        "--user",
        user,
        "--approve" if approve else "--reject",
    ]
    if msg_ts:
        cmd += ["--slack-ts", msg_ts]
    try:
        out = subprocess.run(
            cmd, cwd=REPO_DIR, capture_output=True, text=True, timeout=TIMEOUT_S, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.error("arc-approvals: arc CLI failed: %s", exc)
        return {"outcome": "error", "message": str(exc)}
    try:
        result = json.loads(out.stdout)
    except json.JSONDecodeError:
        logger.error("arc-approvals: arc exited %s: %s", out.returncode, out.stderr[-500:])
        return {"outcome": "error", "message": f"exit {out.returncode}"}
    logger.info(
        "arc-approvals: %s by %s -> %s", proposal[:12], user, result.get("outcome", "unknown")
    )
    return result


async def on_action(ack, body, action) -> None:
    await ack()
    click = parse_click(body, action)
    if click is None:
        logger.warning("arc-approvals: ignored malformed click: %r", (action or {}).get("value"))
        return
    await asyncio.to_thread(run_decide, *click)


def register(ctx) -> None:
    for action_id in ACTIONS:
        ctx.register_slack_action_handler(action_id, on_action)
