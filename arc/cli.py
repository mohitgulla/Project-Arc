"""Arc CLI — entry point for the `arc` command."""

from __future__ import annotations

import argparse
import json
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
        if cmd == "scan":
            p = sub.add_parser(cmd, help="Scan: summarise ingested docs into Candidates (Scout)")
            p.add_argument(
                "--dry-run",
                action="store_true",
                help="Fixture docs + canned Scout responses in an in-memory DB (no network).",
            )
            p.add_argument("--db", default=None, help="SQLite path (default: data/arc.db)")
        else:
            sub.add_parser(cmd, help=f"{cmd.capitalize()} (stub)")

    from arc.data.history.cli import add_history_parser

    add_history_parser(sub)

    return parser


def _scan(args: argparse.Namespace) -> int:
    from arc.config import get_settings
    from arc.ingest.scout import load_fixture_docs, run_scout
    from arc.store.db import connect
    from arc.store.migrate import migrate

    # stdout carries the JSON report; keep structured logs on stderr.
    structlog.configure(logger_factory=structlog.PrintLoggerFactory(file=sys.stderr))
    settings = get_settings()
    conn = connect(":memory:" if args.dry_run else args.db)
    migrate(conn)
    if args.dry_run:
        load_fixture_docs(conn)

    result = run_scout(conn, settings, dry_run=args.dry_run)
    report = {
        "run_id": result.run_id,
        "day": result.day,
        "dry_run": result.dry_run,
        "batches": result.batches,
        "failed_batches": result.failed_batches,
        "docs_scouted": result.docs_scouted,
        "accepted": result.accepted,
        "rejected": dict(result.rejected),
        "candidates": [c.model_dump(mode="json") for c in result.candidates],
    }
    sys.stdout.write(json.dumps(report, indent=2) + "\n")
    return 1 if result.failed_batches and not result.docs_scouted else 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = _make_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 0

    if args.command == "scan":
        return _scan(args)

    if args.command == "history":
        from arc.data.history.cli import run_history

        return run_history(args)

    logger.info("command.stub", command=args.command)
    print(f"arc {args.command}: not yet implemented")
    return 0


if __name__ == "__main__":
    sys.exit(main())
