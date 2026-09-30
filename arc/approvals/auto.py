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

from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    import argparse
    import datetime as _dt
    import sqlite3

    from arc.config import ArcSettings
    from arc.control.service import ControlService, Result

__all__ = ["add_auto_parser", "auto_status", "notice_text", "run_auto", "set_auto"]

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
    return out


def scorecard_readiness(
    conn: sqlite3.Connection, settings: ArcSettings, now: _dt.datetime
) -> dict[str, Any]:
    """E7.5a: the scorecard gate's current verdict for ``arc approve auto status``."""
    from arc.journal.scorecard import auto_approve_readiness

    r = auto_approve_readiness(
        conn,
        now=now,
        min_closed_trades=settings.auto_approve_min_closed_trades,
        slippage_tolerance=settings.auto_approve_slippage_tolerance,
    )
    return {**r.model_dump(mode="json"), "summary": r.summary()}


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


def _post_notice(conn: sqlite3.Connection, text: str, now: _dt.datetime) -> None:
    try:
        from arc.routines.heartbeat import Heartbeats, SlackDayThreadNotifier

        Heartbeats(conn, SlackDayThreadNotifier(conn)).notice(now, "approve", text)
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
        sys.stdout.write(json.dumps(st, indent=2) + "\n")
        sys.stdout.write(
            f"auto_approve: {'on' if st['effective'] else 'off'} ({st['env']}); "
            f"paper={'on' if st['paper'] else 'off'} live={'on' if st['live'] else 'off'}\n"
        )
        gate = (
            ("met" if ready["ok"] else "holding opens") if st["scorecard_gate"] else "OFF (opt-out)"
        )
        sys.stdout.write(f"scorecard gate: {gate}; {ready['summary']}\n")
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
