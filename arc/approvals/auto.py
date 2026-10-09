"""``arc approve auto on|off|status`` (D34): the per-environment auto-approve switch.

The switch is the tunable ``auto_approve.<env>`` (E8.5 registry, D26 change log),
so every flip is an append-only ``config_changes`` row (who, when, env, reason)
that ``arc config history`` / ``!arc config`` show and ``revert`` can undo, and it
is applied at the next tick with no restart.

Rules, enforced here and in the control service:

- ``on`` is a riskier-direction change for either env. For **paper** the CLI
  confirms it in the same call (the shell is the owner). For **live** the first
  ``on --env live`` only *stages* the change and prints a one-time code; the change
  is applied by a second call with ``--confirm-live <code>`` within 10 min
  (:data:`arc.control.service.CONFIRM_TTL`). An expired or reused code is refused.
- ``off`` is always applied at once.
- Every applied flip posts one line to the day thread (``Auto-approve: ON (live)``
  in bold for live); Slack is best-effort and never changes the outcome.
- ``ARC_AUTO_APPROVE`` (the env var) only ever sets the paper value: a live
  process forces it off (:class:`arc.config.ArcSettings`), so live is switched on
  here only.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

import structlog
from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    import argparse
    import datetime as _dt
    import sqlite3

    from arc.config import ArcSettings
    from arc.control.service import ControlService, Result

__all__ = [
    "LiveGateStatus",
    "live_gate_block",
    "add_auto_parser",
    "auto_status",
    "notice_text",
    "post_day_notice",
    "run_auto",
    "set_auto",
]

log = structlog.get_logger(__name__)

ENVS = ("paper", "live")


def add_auto_parser(asub: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    a = asub.add_parser("auto", help="D34: auto-approve switch, per environment")
    a.add_argument("state", choices=("on", "off", "status"))
    a.add_argument("--env", choices=ENVS, default=None, help="default: the current ARC_ENV")
    a.add_argument(
        "--confirm-live",
        default=None,
        metavar="CODE",
        help="the one-time code printed by a first `on --env live` (expires in 10 min)",
    )
    a.add_argument("--reason", default=None, help="recorded in the change log")
    a.add_argument("--no-slack", action="store_true", help="do not post the flip to the day thread")
    a.add_argument("--db", default=None, help="SQLite path (default: data/arc.db)")


def _key(env: str) -> str:
    return f"auto_approve.{env}"


def auto_status(svc: ControlService, base: ArcSettings) -> dict[str, Any]:
    """``{"env": <running env>, "paper": bool, "live": bool, "effective": bool, ...}``."""
    out: dict[str, Any] = {"env": base.env.value}
    for env in ENVS:
        v = svc.view(_key(env))
        out[env] = bool(v.value)
        out[f"{env}_overridden"] = v.overridden
    eff = svc.settings()
    out["effective"] = bool(eff.auto_approve)
    out["config_version"] = svc.version()
    out["scorecard_gate"] = bool(eff.auto_approve_scorecard_gate)
    if base.env.value == "live":  # D70: live auto-approve waits for the live gate
        out["live_gate_met"] = bool(eff.live_gate_met)
        out["effective"] = bool(eff.auto_approve) and (
            bool(eff.live_gate_met) or not eff.live_auto_approve_requires_gate
        )
    return out


def live_gate_block(
    conn: sqlite3.Connection, settings: ArcSettings, now: _dt.datetime
) -> dict[str, Any]:
    """D70: the ``LiveGateStatus`` block of ``arc approve auto status``.

    Paper: ``{"env": "paper", "line": "live gate: n/a (paper)"}``.
    """
    if settings.env.value != "live":
        return {"env": settings.env.value, "line": "live gate: n/a (paper)"}
    from arc.journal.scorecard import env_readiness, live_gate_met

    r = env_readiness(conn, settings, now=now)
    met = bool(settings.live_gate_met) or live_gate_met(r)
    cap = None if met else settings.live_max_contracts_until_gate
    auto = bool(settings.auto_approve) and met
    status = LiveGateStatus(
        env="live",
        live_closed_trades=r.closed_trades,
        required=r.min_closed_trades,
        readiness_ok=r.ok,
        gate_met=met,
        size_cap=cap,
        auto_approve_effective=auto,
    )
    return {**status.model_dump(mode="json"), "line": status.line()}


class LiveGateStatus(BaseModel):
    """D70: what the card / tower / auto status show about the live collection phase."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    env: Literal["paper", "live"]
    live_closed_trades: int
    required: int
    readiness_ok: bool
    gate_met: bool
    size_cap: int | None
    auto_approve_effective: bool

    def line(self) -> str:
        if self.gate_met:
            return (
                f"live gate: MET ({self.live_closed_trades}/{self.required} live closes) — "
                f"no size cap — auto-approve {'on' if self.auto_approve_effective else 'off'}"
            )
        return (
            f"live gate: NOT MET ({self.live_closed_trades}/{self.required} live closes) — "
            f"size cap {self.size_cap} — auto-approve off"
        )


def scorecard_readiness(
    conn: sqlite3.Connection, settings: ArcSettings, now: _dt.datetime
) -> dict[str, Any]:
    """E7.5a: the scorecard gate's current verdict for ``arc approve auto status``.

    Built by :func:`arc.journal.scorecard.auto_approve_gate`, the same call the
    weekly scorecard and the tower use (E6.6a), so the numbers match for one *now*.
    """
    from arc.journal.scorecard import auto_approve_gate

    g = auto_approve_gate(conn, settings, now=now)
    r = g.readiness
    return {**r.model_dump(mode="json"), "summary": r.summary(), "gate_line": g.line}


def notice_text(env: str, on: bool) -> str:
    """The day-thread line: ``Auto-approve: ON (paper)``; live in bold."""
    state = "ON" if on else "OFF"
    text = f"Auto-approve: {state} ({env})"
    return f"*{text}*" if env == "live" else text


def set_auto(
    svc: ControlService,
    *,
    env: str,
    on: bool,
    reason: str | None,
    confirm_code: str | None = None,
) -> Result:
    """Apply (or stage for live) the flip through the control service.

    - ``off``: applied at once (safer direction).
    - ``on`` / paper: staged then confirmed in the same call (owner at the shell).
    - ``on`` / live: without *confirm_code* the change is staged and the returned
      ``Result.pending.code`` must be passed back; with it, the staged change is
      confirmed (refused if expired, reused or for another key/value).
    """
    from arc.control.service import LOCAL_ACTOR

    key = _key(env)
    if env == "live" and on and confirm_code:
        res = svc.confirm(confirm_code, actor=LOCAL_ACTOR, source="cli")
        if res.outcome == "applied" and (res.key != key or res.new is not True):
            # The code belonged to some other pending change: undo nothing, but say so.
            return type(res)(
                "refused",
                key=key,
                message=f"code {confirm_code} confirmed {res.key}, not {key}=true",
            )
        return res
    res = svc.set(key, "true" if on else "false", actor=LOCAL_ACTOR, source="cli", reason=reason)
    if res.outcome == "pending" and res.pending is not None and env == "paper":
        # Paper: the shell is the owner; confirm in the same call.
        return svc.confirm(res.pending.code, actor=LOCAL_ACTOR, source="cli")
    return res


def post_day_notice(
    conn: sqlite3.Connection, text: str, now: _dt.datetime, client: object | None = None
) -> None:
    """E6.6a: one line in the day thread (best-effort; never raises)."""
    try:
        from arc.routines.heartbeat import Heartbeats, SlackDayThreadNotifier

        Heartbeats(conn, SlackDayThreadNotifier(conn, client)).notice(now, "approve", text)
    except Exception as exc:  # noqa: BLE001 - the change is already recorded
        log.warning("approve.day_notice_failed", error=str(exc))


def _post_notice(
    conn: sqlite3.Connection, text: str, now: _dt.datetime, client: object | None = None
) -> None:
    """Post the flip in the day thread, unless the thread already shows that state.

    The thread's first reply is the day banner (the current state). A flip that
    creates the thread, or repeats the state last shown there, would only print
    the same line twice (owner 2026-09-30), so it is skipped.
    """
    try:
        from arc.routines.heartbeat import (
            Heartbeats,
            RoutineStateRepo,
            SlackDayThreadNotifier,
            banner_key,
            day_thread_ts,
        )

        notifier = SlackDayThreadNotifier(conn, client)
        hb = Heartbeats(conn, notifier)
        day = hb.day(now)
        day_thread_ts(conn, notifier._client, day)  # creates root + banner on first use
        state = RoutineStateRepo(conn)
        if state.get(banner_key(day)) == text:
            log.info("approve.auto_notice_skipped", day=day.isoformat(), text=text)
            return
        hb.notice(now, "approve", text)
        state.set(banner_key(day), text)
    except Exception as exc:  # noqa: BLE001 - best-effort; the flip is already recorded
        log.warning("approve.auto_notice_failed", error=str(exc))


def run_auto(args: argparse.Namespace, *, base: ArcSettings, conn: sqlite3.Connection) -> int:
    """CLI body; returns the exit code. Prints one JSON object."""
    import json
    import sys

    from arc.control.service import ControlService
    from arc.utils.calendar import now_et

    svc = ControlService(conn, base=base)
    env = args.env or base.env.value
    if args.state == "status":
        st = auto_status(svc, base)
        ready = scorecard_readiness(conn, svc.settings(), now_et())
        st["scorecard"] = ready
        live_gate = live_gate_block(conn, svc.settings(), now_et())
        st["live_gate"] = live_gate
        sys.stdout.write(json.dumps(st, indent=2) + "\n")
        sys.stdout.write(
            f"auto_approve: {'on' if st['effective'] else 'off'} ({st['env']}); "
            f"paper={'on' if st['paper'] else 'off'} live={'on' if st['live'] else 'off'}\n"
        )
        sys.stdout.write(f"{ready['gate_line']}\n")
        sys.stdout.write(f"{live_gate['line']}\n")
        return 0
    on = args.state == "on"
    res = set_auto(svc, env=env, on=on, reason=args.reason, confirm_code=args.confirm_live)
    payload: dict[str, Any] = {
        "outcome": res.outcome,
        "key": res.key,
        "env": env,
        "message": res.message,
        "config_version": res.config_version,
    }
    if res.outcome == "pending" and res.pending is not None:
        payload["confirm_code"] = res.pending.code
        payload["message"] = (
            f"live auto-approve staged, not on. Re-run within 10 min: "
            f"arc approve auto on --env live --confirm-live {res.pending.code}"
        )
    sys.stdout.write(json.dumps(payload, indent=2) + "\n")
    if res.outcome in ("applied", "reverted"):
        log.info("approve.auto", env=env, on=on, actor="local", reason=args.reason)
        if not args.no_slack:
            _post_notice(conn, notice_text(env, on), now_et())
        return 0
    if res.outcome in ("pending", "unchanged"):
        return 0
    return 1
