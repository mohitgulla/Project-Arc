"""Slack surface of the kill switch (E3.3): ``!halt`` / ``!resume`` and halt notices.

State and policy live in :mod:`arc.gate.halt`; this module parses commands,
calls the switch, and formats/posts the notices.

- ``!halt [reason]`` from any thread, by anyone, halts immediately.
- ``!resume`` works only for ``ARC_OWNER_SLACK_USER_ID``; anyone else gets a
  denial and the halt stays.
- A daily-loss auto-halt is announced in ``#arc-investor``.

Slack posting is best-effort: the halt is persisted *before* anything is posted,
so a Slack failure can never leave trading un-halted.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from arc.gate.halt import ResumeNotAuthorizedError
from arc.slack.commands import CommandVerb, parse_command
from arc.slack.templates import (
    daily_loss_halt_notice,
    halt_notice,
    resume_denied_notice,
    resume_notice,
)

if TYPE_CHECKING:
    import datetime as dt

    from arc.config import ArcSettings
    from arc.gate.halt import HaltRecord, HaltSwitch
    from arc.gate.inputs import AccountSnapshot
    from arc.slack.client import ArcSlackClient

log = structlog.get_logger(__name__)


def _reason(raw_text: str) -> str:
    parts = raw_text.split(maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else ""


def handle_command(
    text: str,
    *,
    user: str,
    switch: HaltSwitch,
    config: ArcSettings,
    now: dt.datetime,
) -> str | None:
    """Apply a ``!halt`` / ``!resume`` message; return the reply text, or None if not a command.

    ``user`` must be the Slack user id as delivered by the platform (never taken
    from message text).
    """
    cmd = parse_command(text, slack_user=user)
    if cmd is None:
        return None
    if not user:
        log.warning("halt.command_without_user", verb=cmd.verb)
        if cmd.verb is CommandVerb.RESUME:
            return resume_denied_notice(user="unknown")
    if cmd.verb is CommandVerb.HALT:
        reason = _reason(cmd.raw_text) or "manual !halt"
        switch.halt(actor=user or "unknown", reason=reason, now=now)
        return halt_notice(triggered_by=user or "unknown", reason=reason)
    try:
        cleared = switch.resume(actor=user, config=config, now=now)
    except ResumeNotAuthorizedError:
        return resume_denied_notice(user=user)
    return resume_notice(resumed_by=user, cleared=len(cleared))


def auto_halt_on_daily_loss(
    switch: HaltSwitch,
    account: AccountSnapshot,
    config: ArcSettings,
    *,
    now: dt.datetime,
    slack: ArcSlackClient | None,
    thread_ts: str | None = None,
) -> HaltRecord | None:
    """Run the daily-loss auto-halt; on a new halt, announce it in #arc-investor.

    Callers: the pipeline before each gate evaluation, and reconciliation (E6.3)
    after each PnL snapshot. Returns the new halt, if one was raised.
    """
    record = switch.check_daily_loss(account, config, now=now)
    if record is None or slack is None:
        return record
    try:
        slack.post_investor(text=daily_loss_halt_notice(detail=record.reason), thread_ts=thread_ts)
    except Exception as exc:  # noqa: BLE001 — halt is already persisted; posting is best-effort
        log.error("halt.post_failed", halt_id=record.id, error=str(exc))
    return record
