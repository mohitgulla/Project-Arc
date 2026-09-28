"""Arc CLI — entry point for the `arc` command."""

from __future__ import annotations

import argparse
import sys

import structlog

logger = structlog.get_logger()

COMMANDS = ("scan", "propose", "gate", "approve", "execute", "reconcile", "report")
INGEST_SOURCES = ("youtube",)


def _make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="arc",
        description="Arc — agentic options trading system CLI.",
    )
    sub = parser.add_subparsers(dest="command", help="Available commands")

    for cmd in COMMANDS:
        sub.add_parser(cmd, help=f"{cmd.capitalize()} (stub)")

    ingest = sub.add_parser("ingest", help="Run a source connector (E4.1)")
    ingest.add_argument("source", choices=INGEST_SOURCES)
    ingest.add_argument(
        "--force-audio",
        action="store_true",
        help="YouTube: skip captions and transcribe audio locally (ignores grace period).",
    )
    ingest.add_argument("--max-videos", type=int, default=5, help="Videos per channel.")
    ingest.add_argument("--db", default=None, help="SQLite path (default: data/arc.db).")

    return parser


def _run_ingest(args: argparse.Namespace) -> int:
    from arc.config import ArcSettings
    from arc.ingest.youtube import fetch_youtube
    from arc.store.db import connect
    from arc.store.migrate import migrate

    conn = connect(args.db)
    migrate(conn)
    docs = fetch_youtube(
        conn,
        ArcSettings(),
        force_audio=args.force_audio,
        max_videos=args.max_videos,
    )
    for d in docs:
        logger.info(
            "ingest.doc",
            url=d.url,
            transcript_source=d.transcript_source,
            chars=len(d.text),
        )
    logger.info("ingest.done", source=args.source, new_docs=len(docs))
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = _make_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 0

    if args.command == "ingest":
        return _run_ingest(args)

    logger.info("command.stub", command=args.command)
    print(f"arc {args.command}: not yet implemented")
    return 0


if __name__ == "__main__":
    sys.exit(main())
