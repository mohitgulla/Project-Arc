"""Ops alerts (E8.2): dedupe findings, resolve cleared ones, post one Slack message.

Policy:

- A *condition* finding (tick stale, gateway down, stuck run) opens an alert
  once per key and stays open while the check keeps failing: no repeat posts.
  When the check passes again the alert is resolved and a ✅ line is posted.
- A *one-off* finding (a routine slot missed its window) is recorded and
  posted exactly once per slot, then closed.
- Findings with ``alert=False`` (gateway warnings by default) are only kept in
  the health heartbeat.
- All new/resolved alerts of one check go out as ONE post in the ops channel
  (#project-arc by default), with the correlation ids so the post can be traced
  back (``arc health trace <tick_id|alert id>``).

Posting is best-effort: a Slack error is logged, the alert stays recorded and
unposted (``posted_ts`` NULL).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

import structlog

from arc.monitoring.checks import ONE_OFF, CheckResult, Finding
from arc.monitoring.config import AlertChannel
from arc.monitoring.store import AlertRepo, OpsAlert

if TYPE_CHECKING:
    import datetime as _dt
    import sqlite3

    from arc.slack.client import ArcSlackClient

log = structlog.get_logger(__name__)

# Condition kinds this module may auto-resolve (keys of findings from the checks).
CONDITION_PREFIXES = ("tick_stale", "gateway", "stuck:")


class OpsNotifier(Protocol):
    def post(self, text: str) -> str | None: ...


class LogOpsNotifier:
    """Alerts to the structured log only (``--no-slack``, tests)."""

    def post(self, text: str) -> str | None:
        log.warning("ops.alert_post", text=text)
        return None


class RecordingOpsNotifier:
    def __init__(self) -> None:
        self.posts: list[str] = []

    def post(self, text: str) -> str | None:
        self.posts.append(text)
        return f"ts-{len(self.posts)}"


class SlackOpsNotifier:
    """Posts ops alerts as a root message in the configured channel."""

    def __init__(self, channel: AlertChannel, client: ArcSlackClient | None = None) -> None:
        from arc.slack.client import CHANNEL_ARC_INVESTOR, CHANNEL_PROJECT_ARC, ArcSlackClient

        self._channel = (
            CHANNEL_PROJECT_ARC if channel is AlertChannel.PROJECT_ARC else CHANNEL_ARC_INVESTOR
        )
        self._client = client if client is not None else ArcSlackClient()

    def post(self, text: str) -> str | None:
        try:
            resp = self._client.post_thread_root(channel=self._channel, text=text)
            return str(resp.get("ts") or "") or None
        except Exception as exc:  # noqa: BLE001 - alerting must never crash the check
            log.error("ops.alert_post_failed", error=str(exc))
            return None


@dataclass
class AlertOutcome:
    opened: list[OpsAlert] = field(default_factory=list)
    resolved: list[OpsAlert] = field(default_factory=list)
    still_open: list[OpsAlert] = field(default_factory=list)
    posted_ts: str | None = None
    text: str = ""


def _corr_line(correlation: dict[str, str]) -> str:
    keys = ("tick_id", "cron_job", "kanban_task", "hermes_session")
    parts = [f"{k}={correlation[k]}" for k in keys if correlation.get(k)]
    return " · ".join(parts)


def render(outcome: AlertOutcome, correlation: dict[str, str]) -> str:
    lines: list[str] = []
    for a in outcome.opened:
        lines.append(f":rotating_light: [Ops] {a.message}  `{a.id}`")
    for a in outcome.resolved:
        lines.append(f":white_check_mark: [Ops] resolved: {a.message}  `{a.id}`")
    corr = _corr_line(correlation)
    if corr and lines:
        lines.append(f"_{corr}_")
    return "\n".join(lines)


def apply(
    conn: sqlite3.Connection,
    results: list[CheckResult],
    *,
    now: _dt.datetime,
    correlation: dict[str, str],
    notifier: OpsNotifier,
) -> AlertOutcome:
    repo = AlertRepo(conn)
    out = AlertOutcome()
    active: set[str] = set()
    findings: list[Finding] = [f for r in results for f in r.findings]
    for f in findings:
        if not f.alert:
            continue
        if f.mode == ONE_OFF:
            if not repo.seen(f.key):
                out.opened.append(
                    repo.open(f.key, f.kind, f.message, at=now, correlation=correlation,
                              resolved=True)
                )  # fmt: skip
            continue
        active.add(f.key)
        existing = repo.open_for(f.key)
        if existing is None:
            out.opened.append(repo.open(f.key, f.kind, f.message, at=now, correlation=correlation))
        else:
            out.still_open.append(existing)
    checked = {r.name for r in results}
    for a in repo.open_alerts():
        if a.key in active or not a.key.startswith(CONDITION_PREFIXES):
            continue
        # Only resolve what this run actually re-checked (e.g. --no-gateway leaves it alone).
        if a.key.startswith("gateway") and "gateway" not in checked:
            continue
        if a.key == "tick_stale" and "tick" not in checked:
            continue
        if a.key.startswith("stuck:") and "stuck_runs" not in checked:
            continue
        resolved = repo.resolve(a.key, at=now)
        if resolved is not None:
            out.resolved.append(resolved)
    out.text = render(out, correlation)
    if out.text:
        out.posted_ts = notifier.post(out.text)
        if out.posted_ts:
            repo.set_posted([a.id for a in out.opened], out.posted_ts)
        log.warning(
            "ops.alerts",
            opened=[a.key for a in out.opened],
            resolved=[a.key for a in out.resolved],
            posted_ts=out.posted_ts,
        )
    return out
