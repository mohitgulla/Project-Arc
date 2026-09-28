"""Arc CLI — entry point for the `arc` command."""

from __future__ import annotations

import argparse
import sys
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from arc.gate.halt import HaltSwitch

logger = structlog.get_logger()

COMMANDS = ("scan", "propose", "gate", "approve", "execute", "reconcile", "report")
HALT_COMMANDS = ("halt", "resume", "halt-status", "slack-command")


def _make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="arc",
        description="Arc — agentic options trading system CLI.",
    )
    sub = parser.add_subparsers(dest="command", help="Available commands")

    for cmd in COMMANDS:
        sub.add_parser(cmd, help=f"{cmd.capitalize()} (stub)")

    # -- kill switch (E3.3) ---------------------------------------------------
    halt = sub.add_parser("halt", help="Halt trading now (kill switch)")
    halt.add_argument("--actor", required=True, help="Who is halting (Slack user id or name)")
    halt.add_argument("--reason", default="manual halt", help="Why")

    resume = sub.add_parser("resume", help="Clear all halts (owner only)")
    resume.add_argument("--actor", required=True, help="Slack user id; must be the owner")

    sub.add_parser("halt-status", help="Show active halts (exit 1 when halted)")

    slack_cmd = sub.add_parser(
        "slack-command", help="Apply a Slack `!halt`/`!resume` message (gateway plugin)"
    )
    slack_cmd.add_argument("--user", required=True, help="Sender's Slack user id")
    slack_cmd.add_argument("--text", required=True, help="Raw message text")

    return parser


def _out(text: str) -> None:
    sys.stdout.write(text + "\n")


def _switch() -> HaltSwitch:
    from arc.config import get_settings
    from arc.gate.halt import HaltSwitch
    from arc.store.db import connect
    from arc.store.migrate import migrate
    from arc.store.repos import HaltRepo

    conn = connect(get_settings().db_path)
    migrate(conn)
    return HaltSwitch(HaltRepo(conn))


def _run_halt_command(args: argparse.Namespace) -> int:
    from arc.config import get_settings
    from arc.gate.halt import ResumeNotAuthorizedError
    from arc.slack.halt import handle_command
    from arc.utils.calendar import now_et

    switch = _switch()
    if args.command == "halt":
        rec = switch.halt(actor=args.actor, reason=args.reason, now=now_et())
        _out(f"HALTED {rec.id} by {rec.actor}: {rec.reason}")
        return 0
    if args.command == "resume":
        try:
            cleared = switch.resume(actor=args.actor, config=get_settings(), now=now_et())
        except ResumeNotAuthorizedError as exc:
            _out(f"DENIED: {exc}")
            return 2
        _out(f"RESUMED: cleared {len(cleared)} halt(s)")
        return 0
    if args.command == "halt-status":
        state = switch.state()
        if not state.halted:
            _out("trading allowed (no active halts)")
            return 0
        if state.error:
            _out(f"HALTED (state unreadable, failing closed): {state.error}")
        for h in state.active:
            _out(f"HALTED {h.id} {h.kind} at {h.at.isoformat()} by {h.actor}: {h.reason}")
        return 1
    # slack-command
    reply = handle_command(
        args.text, user=args.user, switch=switch, config=get_settings(), now=now_et()
    )
    if reply is None:
        return 3
    _out(reply)
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = _make_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 0

    if args.command in HALT_COMMANDS:
        return _run_halt_command(args)

    logger.info("command.stub", command=args.command)
    print(f"arc {args.command}: not yet implemented")
    return 0


if __name__ == "__main__":
    sys.exit(main())
