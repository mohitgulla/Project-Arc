"""Arc CLI — entry point for the `arc` command."""

from __future__ import annotations

import argparse
import sys

import structlog

logger = structlog.get_logger()

COMMANDS = ("scan", "propose", "gate", "approve", "execute", "reconcile", "report")


def _make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="arc",
        description="Arc — agentic options trading system CLI.",
    )
    sub = parser.add_subparsers(dest="command", help="Available commands")

    for cmd in COMMANDS:
        sub.add_parser(cmd, help=f"{cmd.capitalize()} (stub)")

    from arc.data.history.cli import add_history_parser

    add_history_parser(sub)

    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = _make_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 0

    if args.command == "history":
        from arc.data.history.cli import run_history

        return run_history(args)

    logger.info("command.stub", command=args.command)
    print(f"arc {args.command}: not yet implemented")
    return 0


if __name__ == "__main__":
    sys.exit(main())
