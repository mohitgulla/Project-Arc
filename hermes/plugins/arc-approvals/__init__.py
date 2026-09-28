"""arc-approvals — Approve / Reject buttons on Arc proposal cards (E6.1, D22).

Arc posts each proposal as a Block Kit card in the #arc-investor day thread with
two buttons: ``arc_approve`` / ``arc_reject``, whose ``value`` is the proposal
hash. This plugin registers Slack action handlers for those ids and hands the
click to the project's ``arc approve decide`` CLI (project venv).

The CLI owns every rule: allowed approver ids, the TTL, resolve-once, the
ApprovalRecord row, the decision journal, and rewriting the card. The clicker's
id comes from the Slack interaction payload (``body.user.id``), never from
message text. The plugin itself only acks within Slack's 3 s window and runs
the CLI off the event loop.

Optional reject reason (D22): a Reject click also opens a modal
(``views.open`` with the click's ``trigger_id``) holding one optional multiline
"Reason" field. The rejection is recorded on the click regardless, so closing
the modal changes nothing. Submitting it runs ``arc approve reason <hash>
--user <id> --text ...``, which journals the reason (allowed approvers only).
The modal needs the Slack web client, which a ``register_platform_handler``
factory takes from the AsyncApp; the same factory registers the
``view_submission`` handler. ``views.open`` needs no OAuth scope beyond the
interactivity the buttons already use.

Install: ``cp -r hermes/plugins/arc-approvals ~/.hermes/plugins/ &&
hermes plugins enable arc-approvals`` (gateway picks up Slack handlers on
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
from typing import Any

logger = logging.getLogger(__name__)

REPO_DIR = Path(os.environ.get("ARC_REPO_DIR", str(Path.home() / "GitHub" / "Project-Arc")))
TIMEOUT_S = 30
ACTIONS = {"arc_approve": True, "arc_reject": False}
REASON_CALLBACK_ID = "arc_reject_reason"
REASON_BLOCK_ID = "arc_reason"
REASON_ACTION_ID = "reason"
REASON_MAX_CHARS = 2000
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")


class _Slack:
    """The Slack web client of the live AsyncApp, set when the gateway connects."""

    def __init__(self) -> None:
        self.client: Any = None


SLACK = _Slack()


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


def build_reason_modal(proposal_hash: str) -> dict[str, Any]:
    """The optional-reason modal (the ``view`` of ``views.open``) for a rejected proposal."""
    return {
        "type": "modal",
        "callback_id": REASON_CALLBACK_ID,
        "private_metadata": proposal_hash,
        "title": {"type": "plain_text", "text": "Rejected"},
        "submit": {"type": "plain_text", "text": "Submit"},
        "close": {"type": "plain_text", "text": "Skip"},
        "blocks": [
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": f"The rejection of `{proposal_hash[:12]}` is already recorded. "
                        "A reason is optional; it goes to the decision journal.",
                    }
                ],
            },
            {
                "type": "input",
                "block_id": REASON_BLOCK_ID,
                "optional": True,
                "label": {"type": "plain_text", "text": "Reason"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": REASON_ACTION_ID,
                    "multiline": True,
                    "max_length": REASON_MAX_CHARS,
                },
            },
        ],
    }


def parse_submission(body: dict, view: dict) -> tuple[str, str, str] | None:
    """``(proposal_hash, user_id, reason)`` for an Arc reason-modal submission, else None."""
    view = view or {}
    if view.get("callback_id") != REASON_CALLBACK_ID:
        return None
    phash = str(view.get("private_metadata") or "")
    if not _HASH_RE.match(phash):
        return None
    user = str(((body or {}).get("user") or {}).get("id") or "")
    if not user:
        return None
    values = ((view.get("state") or {}).get("values") or {}).get(REASON_BLOCK_ID) or {}
    text = str((values.get(REASON_ACTION_ID) or {}).get("value") or "")
    return phash, user, text[:REASON_MAX_CHARS]


def _run(cmd: list[str], what: str) -> dict:
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
    logger.info("arc-approvals: %s -> %s", what, result.get("outcome", "unknown"))
    return result


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
    return _run(cmd, f"{proposal[:12]} by {user}")


def run_reason(proposal: str, user: str, text: str) -> dict:
    """Run ``arc approve reason``; return its JSON result (or an error dict)."""
    cmd = [str(_arc_bin()), "approve", "reason", proposal, "--user", user, "--text", text]
    return _run(cmd, f"reason {proposal[:12]} by {user}")


async def open_reason_modal(trigger_id: str, proposal_hash: str) -> bool:
    """Open the optional-reason modal. Best effort: the rejection never depends on it."""
    if SLACK.client is None or not trigger_id:
        logger.warning("arc-approvals: no Slack client or trigger_id; reason modal skipped")
        return False
    try:
        await SLACK.client.views_open(trigger_id=trigger_id, view=build_reason_modal(proposal_hash))
    except Exception as exc:  # noqa: BLE001 - never block the click on the modal
        logger.warning("arc-approvals: views.open failed: %s", exc)
        return False
    return True


async def on_action(ack, body, action) -> None:
    await ack()
    click = parse_click(body, action)
    if click is None:
        logger.warning("arc-approvals: ignored malformed click: %r", (action or {}).get("value"))
        return
    if not click[1]:  # Reject: a trigger_id is valid for 3 s, so open the modal first
        await open_reason_modal(str((body or {}).get("trigger_id") or ""), click[0])
    await asyncio.to_thread(run_decide, *click)


async def on_reason_submit(ack, body, view) -> None:
    await ack()  # closes the modal; a blank reason is fine
    sub = parse_submission(body, view)
    if sub is None:
        logger.warning("arc-approvals: ignored malformed reason submission")
        return
    await asyncio.to_thread(run_reason, *sub)


def slack_handlers(app: Any, adapter: Any = None) -> None:
    """``register_platform_handler("slack", ...)`` factory: keep the client, add the view."""
    SLACK.client = getattr(app, "client", None)
    app.view(REASON_CALLBACK_ID)(on_reason_submit)


def register(ctx) -> None:
    for action_id in ACTIONS:
        ctx.register_slack_action_handler(action_id, on_action)
    register_platform = getattr(ctx, "register_platform_handler", None)
    if register_platform is not None:
        register_platform("slack", slack_handlers)
