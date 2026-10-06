"""Ops alerts (E8.2): dedupe findings, resolve cleared ones, post one Slack message.

Policy:

- A *condition* finding (tick stale, gateway down, stuck run) opens an alert
  once per key and stays open while the check keeps failing: no repeat posts.
  When the check passes again the alert is resolved and a ✅ line is posted,
  rendered from the alert kind and the current check (not the opening text).
- A *one-off* finding (a routine slot missed its window) is recorded once per
  slot. Several missed slots of one job in one post collapse to one line.
  E8.2a: only slow-cadence jobs (slots >= ``monitoring.per_slot_min_interval``
  apart) produce these; a fast job (the 5-min loop, monitor) instead opens one
  ``coverage:<job>`` *condition* while it ran < ``coverage_min`` of its slots in
  the last ``coverage_window``, naming the likely cause (slow ticks / tick gaps).
- **Incidents absorb misses.** While a ``tick_stale``, ``gateway`` or (E8.2a)
  ``tick_slow`` alert is open (or opens in the same run), missed slots whose
  window closed during it are recorded with ``correlation.folded_into = <incident
  alert id>`` and are not posted on their own: the root cause is already
  reported. ``coverage:<job>`` alerts that open during an incident are folded the
  same way (recorded, open, not posted; their resolve is silent while the
  incident is open). The incident's resolve line summarises them ("N slot(s)
  missed: rss ×12, edgar ×3; low coverage: director, monitor"). Misses
  judged after the incident resolved (window closed during it, grace ran out
  later) go as one thread reply under the incident's post.
- Findings with ``alert=False`` (gateway warnings by default) are only kept in
  the health heartbeat.
- All new/resolved alerts of one check go out as ONE root post in the ops
  channel (#project-arc by default), with the correlation ids so the post can
  be traced back (``arc health trace <tick_id|alert id>``).

Posting is best-effort: a Slack error is logged, the alert stays recorded and
unposted (``posted_ts`` NULL).
"""

from __future__ import annotations

import datetime as _dt
from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

import structlog

from arc.monitoring.checks import ONE_OFF, CheckResult, Finding
from arc.monitoring.config import AlertChannel
from arc.monitoring.store import AlertRepo, OpsAlert

if TYPE_CHECKING:
    import sqlite3

    from arc.slack.client import ArcSlackClient

log = structlog.get_logger(__name__)

# Condition kinds this module may auto-resolve (keys of findings from the checks).
CONDITION_PREFIXES = (
    "tick_stale",
    "tick_slow",
    "gateway",
    "stuck:",
    "remote_",
    "coverage:",
    "approvals_unposted",
    "iv_crosscheck",
)
# Conditions that make routine slots miss: while open they absorb missed-window
# alerts and (E8.2a) newly opened coverage alerts.
INCIDENT_KEYS = ("tick_stale", "gateway", "tick_slow")
COVERAGE_PREFIX = "coverage:"
EARNINGS_COVERAGE = "coverage:earnings"  # E4.1d: stale calendar, not a slot ratio
MOMENTUM_COVERAGE = "coverage:universe.momentum"  # E12.2: stale SPMO list, not a slot ratio
# Which check must have run for an open alert of this key prefix to be resolved.
_RESOLVED_BY = (
    ("gateway", "gateway"),
    ("tick_stale", "tick"),
    ("tick_slow", "tick_slow"),
    ("stuck:", "stuck_runs"),
    ("remote_", "remote_access"),
    (EARNINGS_COVERAGE, "earnings_coverage"),  # E4.1d (before the generic prefix)
    (MOMENTUM_COVERAGE, "momentum_coverage"),  # E12.2 (before the generic prefix)
    (COVERAGE_PREFIX, "slot_coverage"),
    ("approvals_unposted", "approvals_unposted"),
    ("iv_crosscheck", "iv_crosscheck"),  # E4.12
)
# A slot whose window closed this long before an incident opened is still blamed on it
# (``tick_stale`` opens ``tick_stale_after`` after the last tick; its ``since`` is exact).
DEFAULT_FOLD_LEAD = _dt.timedelta(minutes=25)
FOLDED_INTO = "folded_into"
INCIDENT_SINCE = "since"


def _slot_coverage(key: str) -> bool:
    """A per-job slot-coverage condition (foldable into an incident). E4.1d's
    ``coverage:earnings`` is a stale-calendar condition with its own cause: never folded.
    E12.2's ``coverage:universe.momentum`` (a stale source list) likewise."""
    return key.startswith(COVERAGE_PREFIX) and key not in (EARNINGS_COVERAGE, MOMENTUM_COVERAGE)


class OpsNotifier(Protocol):
    def post(self, text: str, thread_ts: str | None = None) -> str | None: ...


class LogOpsNotifier:
    """Alerts to the structured log only (``--no-slack``, tests)."""

    def post(self, text: str, thread_ts: str | None = None) -> str | None:
        log.warning("ops.alert_post", text=text, thread_ts=thread_ts)
        return None


class RecordingOpsNotifier:
    """Test double: ``posts`` are root messages, ``replies`` are ``(thread_ts, text)``."""

    def __init__(self) -> None:
        self.posts: list[str] = []
        self.replies: list[tuple[str, str]] = []

    def post(self, text: str, thread_ts: str | None = None) -> str | None:
        if thread_ts:
            self.replies.append((thread_ts, text))
            return f"{thread_ts}.r{len(self.replies)}"
        self.posts.append(text)
        return f"ts-{len(self.posts)}"


class SlackOpsNotifier:
    """Posts ops alerts as a root message (or a thread reply) in the configured channel."""

    def __init__(self, channel: AlertChannel, client: ArcSlackClient | None = None) -> None:
        from arc.slack.client import CHANNEL_ARC_INVESTOR, CHANNEL_PROJECT_ARC, ArcSlackClient

        self._channel = (
            CHANNEL_PROJECT_ARC if channel is AlertChannel.PROJECT_ARC else CHANNEL_ARC_INVESTOR
        )
        self._client = client if client is not None else ArcSlackClient()

    def post(self, text: str, thread_ts: str | None = None) -> str | None:
        try:
            if thread_ts:
                resp = self._client.reply(channel=self._channel, thread_ts=thread_ts, text=text)
            else:
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
    # Missed slots recorded under an incident instead of posted on their own.
    folded: list[OpsAlert] = field(default_factory=list)
    posted_ts: str | None = None
    text: str = ""
    # Thread replies under an (already resolved) incident post: (thread_ts, text).
    replies: list[tuple[str, str]] = field(default_factory=list)
    # Resolve lines, by alert id (rendered from the kind + current check).
    resolve_lines: dict[str, str] = field(default_factory=dict)


def _corr_line(correlation: dict[str, str]) -> str:
    keys = ("tick_id", "cron_job", "kanban_task", "hermes_session")
    parts = [f"{k}={correlation[k]}" for k in keys if correlation.get(k)]
    return " · ".join(parts)


def missed_job(alert: OpsAlert) -> str:
    """Job name of a ``missed:<job>:<slot>`` alert."""
    parts = alert.key.split(":", 2)
    return parts[1] if len(parts) > 1 else alert.key


def summarize_missed(missed: list[OpsAlert]) -> str:
    """``N slot(s) missed: rss ×12, edgar ×3; low coverage: director`` (most-missed first).

    E8.2a: folded ``coverage:<job>`` conditions are listed by job after the misses.
    """
    slots = [a for a in missed if not a.key.startswith(COVERAGE_PREFIX)]
    low = sorted(
        a.key.removeprefix(COVERAGE_PREFIX) for a in missed if a.key.startswith(COVERAGE_PREFIX)
    )
    parts: list[str] = []
    if slots:
        counts = Counter(missed_job(a) for a in slots)
        ranked = sorted(counts.items(), key=lambda x: (-x[1], x[0]))
        jobs = ", ".join(f"{job} ×{n}" for job, n in ranked)
        parts.append(f"{len(slots)} routine slot(s) missed: {jobs}")
    if low:
        parts.append(f"low slot coverage: {', '.join(low)}")
    return "; ".join(parts)


def _missed_lines(missed: list[OpsAlert]) -> list[str]:
    """One line per job: the single message, or a count with first/last slot."""
    by_job: dict[str, list[OpsAlert]] = {}
    for a in missed:
        by_job.setdefault(missed_job(a), []).append(a)
    lines: list[str] = []
    for job, items in by_job.items():
        if len(items) == 1:
            lines.append(f":rotating_light: [Ops] {items[0].message}  `{items[0].id}`")
            continue
        items = sorted(items, key=lambda a: a.key)
        first, last = items[0].message, items[-1].message
        lines.append(
            f":rotating_light: [Ops] {job}: {len(items)} slots missed their window "
            f"(first: {first}; last: {last})  `{items[0].id}`…`{items[-1].id}`"
        )
    return lines


def _resolve_line(alert: OpsAlert, results: dict[str, CheckResult]) -> str:
    """What is true *now* for a resolved condition (not its stale opening message)."""
    if alert.key == "tick_stale":
        tick = results.get("tick")
        now = f" ({tick.summary})" if tick else ""
        return f"routines tick heartbeat is fresh again{now}"
    if alert.key == "tick_slow":
        ts = results.get("tick_slow")
        now = f" ({ts.summary})" if ts else ""
        return f"routines ticks back within limits{now}"
    if alert.key == EARNINGS_COVERAGE:
        ec = results.get("earnings_coverage")
        now = f" ({ec.summary})" if ec else ""
        return f"earnings calendar fresh again{now}"
    if alert.key == "iv_crosscheck":
        ic = results.get("iv_crosscheck")
        now = f" ({ic.summary})" if ic else ""
        return f"IV back within the Cboe cross-check threshold{now}"
    if alert.key == MOMENTUM_COVERAGE:
        mc = results.get("momentum_coverage")
        now = f" ({mc.summary})" if mc else ""
        return f"momentum tier source fresh again{now}"
    if alert.key.startswith(COVERAGE_PREFIX):
        job = alert.key.removeprefix(COVERAGE_PREFIX)
        sc = results.get("slot_coverage")
        cov = (sc.detail.get("jobs") or {}).get(job) if sc else None
        if cov and cov.get("judged"):
            ratio = cov["ran"] / cov["judged"]
            mins = sc.detail.get("window_min") if sc else None
            return (
                f"{job} slot coverage recovered: ran {cov['ran']}/{cov['judged']} slots "
                f"in the last {mins} min ({ratio:.0%})"
            )
        return f"{job} slot coverage recovered (no slots judged in the window)"
    if alert.key == "gateway":
        gw = results.get("gateway")
        now = f" ({gw.summary})" if gw else ""
        return f"Hermes gateway healthy again{now}"
    if alert.key.startswith("stuck:"):
        return f"run {alert.key.removeprefix('stuck:')} is no longer stuck"
    if alert.key == "approvals_unposted":
        return "every pending approval request has its Slack card (or expired)"
    if alert.key.startswith("remote_"):
        ra = results.get("remote_access")
        what = {
            "remote_hermes": "remote Hermes dashboard answering with basic auth again",
            "remote_tower": "remote tower answering again",
            "remote_exposed": "remote access no longer answering off the tailnet",
        }.get(alert.key, f"{alert.key} cleared")
        ok = ra.summary if ra and ra.severity == "ok" else ""
        return f"{what} ({ok})" if ok else what
    return alert.message


def render(
    outcome: AlertOutcome,
    correlation: dict[str, str],
    folded_by: dict[str, list[OpsAlert]] | None = None,
) -> str:
    folded_by = folded_by or {}
    lines: list[str] = []
    conditions = [a for a in outcome.opened if a.kind != "missed_window"]
    missed = [a for a in outcome.opened if a.kind == "missed_window"]
    for a in conditions:
        lines.append(f":rotating_light: [Ops] {a.message}  `{a.id}`")
    lines += _missed_lines(missed)
    for a in outcome.resolved:
        text = outcome.resolve_lines.get(a.id) or a.message
        if folded := folded_by.get(a.id):
            text += f"; during it {summarize_missed(folded)}"
        lines.append(f":white_check_mark: [Ops] resolved: {text}  `{a.id}`")
    corr = _corr_line(correlation)
    if corr and lines:
        lines.append(f"_{corr}_")
    return "\n".join(lines)


def incident_start(inc: OpsAlert, lead: _dt.timedelta) -> _dt.datetime:
    """When the outage behind *inc* began: its ``since`` (last good tick) or open time - lead.

    A ``tick_stale`` alert opens ``tick_stale_after`` after the last tick at the
    earliest, and much later if the health check itself was down, so slots whose
    window closed after the last tick belong to the incident.
    """
    start = inc.opened_at - lead
    raw = inc.correlation.get(INCIDENT_SINCE)
    if isinstance(raw, str):
        start = min(start, _dt.datetime.fromisoformat(raw))
    return start


def _covering(
    incidents: list[OpsAlert], deadline: _dt.datetime | None, lead: _dt.timedelta
) -> OpsAlert | None:
    """The incident whose span contains the slot's window close (``None``: a lone miss)."""
    for inc in incidents:
        start = incident_start(inc, lead)
        if inc.resolved_at is None:
            if deadline is None or deadline >= start:
                return inc
        elif deadline is not None and start <= deadline <= inc.resolved_at:
            return inc
    return None


def _deadline(f: Finding) -> _dt.datetime | None:
    raw = f.detail.get("deadline")
    return _dt.datetime.fromisoformat(raw) if isinstance(raw, str) else None


def apply(
    conn: sqlite3.Connection,
    results: list[CheckResult],
    *,
    now: _dt.datetime,
    correlation: dict[str, str],
    notifier: OpsNotifier,
    fold_lead: _dt.timedelta = DEFAULT_FOLD_LEAD,
) -> AlertOutcome:
    repo = AlertRepo(conn)
    out = AlertOutcome()
    active: set[str] = set()
    findings: list[Finding] = [f for r in results for f in r.findings if f.alert]
    by_name = {r.name: r for r in results}

    # 1) Conditions first, so an incident opened in this run can absorb this run's misses.
    #    Coverage conditions wait until step 2 has settled which incidents are open.
    for f in findings:
        if f.mode == ONE_OFF:
            continue
        active.add(f.key)
        if _slot_coverage(f.key):
            continue
        existing = repo.open_for(f.key)
        if existing is None:
            corr: dict[str, str] = dict(correlation)
            if isinstance(since := f.detail.get("last_tick"), str):
                corr[INCIDENT_SINCE] = since
            out.opened.append(repo.open(f.key, f.kind, f.message, at=now, correlation=corr))
        else:
            out.still_open.append(existing)

    # 2) Resolve conditions this run re-checked and found passing.
    checked = set(by_name)
    for a in repo.open_alerts():
        if a.key in active or not a.key.startswith(CONDITION_PREFIXES):
            continue
        # Only resolve what this run actually re-checked (e.g. --no-gateway leaves it alone).
        owner = next((check for p, check in _RESOLVED_BY if a.key.startswith(p)), None)
        if owner is not None and owner not in checked:
            continue
        resolved = repo.resolve(a.key, at=now)
        if resolved is None:  # pragma: no cover - open_alerts() just listed it
            continue
        if resolved.correlation.get(FOLDED_INTO):
            continue  # a folded coverage alert closes silently: its incident reports it
        done = resolved.model_copy(update={"resolved_at": now})
        out.resolved.append(done)
        out.resolve_lines[done.id] = _resolve_line(done, by_name)

    # 2b) E8.2a coverage: fold into an open incident (tick_slow, tick_stale, gateway),
    #     else alert; a folded one whose incident closed while it still fails is posted.
    open_incident = next((i for k in INCIDENT_KEYS if (i := repo.open_for(k))), None)
    for f in findings:
        if f.mode == ONE_OFF or not _slot_coverage(f.key):
            continue
        existing = repo.open_for(f.key)
        if existing is None:
            if open_incident is not None:
                corr = {**correlation, FOLDED_INTO: open_incident.id}
                out.folded.append(repo.open(f.key, f.kind, f.message, at=now, correlation=corr))
            else:
                out.opened.append(
                    repo.open(f.key, f.kind, f.message, at=now, correlation=dict(correlation))
                )
            continue
        inc_id = existing.correlation.get(FOLDED_INTO)
        inc = repo.get(str(inc_id)) if inc_id else None
        if inc_id and (inc is None or inc.resolved_at is not None):
            corr = {k: v for k, v in existing.correlation.items() if k != FOLDED_INTO}
            corr["unfolded_from"] = str(inc_id)
            repo.unfold(existing.id, message=f.message, correlation=corr)
            out.opened.append(existing.model_copy(update={"message": f.message,
                                                          "correlation": corr}))  # fmt: skip
        else:
            out.still_open.append(existing)

    # 3) Missed slots: fold into a covering incident, else post (collapsed per job).
    incidents = repo.incidents(INCIDENT_KEYS, since=now - _dt.timedelta(days=2))
    late: dict[str, list[OpsAlert]] = {}
    for f in findings:
        if f.mode != ONE_OFF or repo.seen(f.key):
            continue
        inc = _covering(incidents, _deadline(f), fold_lead) if f.kind == "missed_window" else None
        if inc is None:
            out.opened.append(
                repo.open(f.key, f.kind, f.message, at=now, correlation=correlation,
                          resolved=True)
            )  # fmt: skip
            continue
        folded_corr = {**correlation, FOLDED_INTO: inc.id}
        rec = repo.open(f.key, f.kind, f.message, at=now, correlation=folded_corr, resolved=True)
        out.folded.append(rec)
        resolving_now = any(r.id == inc.id for r in out.resolved)
        if inc.resolved_at is not None and not resolving_now:
            # Judged after the incident closed (grace ran out later): reply in its thread,
            # or post normally if the incident itself never reached Slack.
            if inc.posted_ts:
                late.setdefault(inc.posted_ts, []).append(rec)
            else:
                out.folded.remove(rec)
                out.opened.append(rec)

    folded_by = {a.id: repo.folded_into(a.id) for a in out.resolved if a.key in INCIDENT_KEYS}
    out.text = render(out, correlation, folded_by)
    if out.text:
        out.posted_ts = notifier.post(out.text)
        if out.posted_ts:
            # Folded misses reported in a resolve line point at that post too.
            summarized = [m.id for ms in folded_by.values() for m in ms]
            repo.set_posted([a.id for a in out.opened] + summarized, out.posted_ts)
    for ts, recs in late.items():
        text = f":information_source: [Ops] after the incident: {summarize_missed(recs)}"
        out.replies.append((ts, text))
        if notifier.post(text, thread_ts=ts):
            repo.set_posted([a.id for a in recs], ts)
    if out.text or out.folded or out.replies:
        log.warning(
            "ops.alerts",
            opened=[a.key for a in out.opened],
            resolved=[a.key for a in out.resolved],
            folded=len(out.folded),
            replies=len(out.replies),
            posted_ts=out.posted_ts,
        )
    return out
