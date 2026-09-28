"""Arc CLI — entry point for the `arc` command."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from arc.data.base import MarketDataProvider
    from arc.gate.halt import HaltSwitch
    from arc.scanner import ScanCandidate

logger = structlog.get_logger()

COMMANDS = ("scan", "propose", "gate", "approve", "execute", "reconcile", "report")
INGEST_SOURCES = ("youtube",)
COMMANDS = ("scan", "chains", "propose", "gate", "approve", "execute", "reconcile", "report")
HALT_COMMANDS = ("halt", "resume", "halt-status", "slack-command")


class _StderrProxy:
    """File-like object that always writes to the *current* ``sys.stderr``."""

    def write(self, s: str) -> int:
        return sys.stderr.write(s)

    def flush(self) -> None:
        sys.stderr.flush()


def _dte_range(text: str) -> tuple[int, int]:
    """Parse ``30-45`` (or a single ``35``) into (min, max) DTE."""
    lo, sep, hi = text.partition("-")
    try:
        a = int(lo)
        b = int(hi) if sep else a
    except ValueError:
        msg = f"invalid DTE range {text!r}; expected e.g. 30-45"
        raise argparse.ArgumentTypeError(msg) from None
    if a < 1 or b < a:
        msg = f"invalid DTE range {text!r}; need 1 <= min <= max"
        raise argparse.ArgumentTypeError(msg)
    return a, b


def _delta(text: str) -> float:
    """Parse a target delta given as ``20`` (delta points) or ``0.20``."""
    try:
        v = float(text)
    except ValueError:
        msg = f"invalid delta {text!r}"
        raise argparse.ArgumentTypeError(msg) from None
    v = v / 100.0 if v >= 1 else v
    if not 0 < v < 1:
        msg = f"delta {text!r} out of range; use 1-99 or 0.01-0.99"
        raise argparse.ArgumentTypeError(msg)
    return v


def _add_scan_args(p: argparse.ArgumentParser) -> None:
    from arc.scanner import RankBy, ScanStrategy

    p.add_argument("tickers", nargs="+", metavar="TICKER", help="Underlyings, e.g. SPY QQQ")
    p.add_argument("--dte", type=_dte_range, default=None, help="DTE window, e.g. 30-45")
    p.add_argument("--delta", type=_delta, default=None, help="Target short |delta|: 20 or 0.20")
    p.add_argument("--width", type=float, default=None, help="Wing width in dollars")
    p.add_argument(
        "--strategy",
        action="append",
        choices=[s.value for s in ScanStrategy],
        default=None,
        help="Restrict to a structure (repeatable). Default: all.",
    )
    p.add_argument(
        "--rank-by", choices=[r.value for r in RankBy], default=None, help="Primary sort key"
    )
    p.add_argument("--top", type=int, default=10, help="Candidates kept per ticker")
    p.add_argument(
        "--fixture",
        action="append",
        default=None,
        metavar="PATH",
        help="Recorded chain JSON (offline). 'spy' = the bundled SPY recording.",
    )
    p.add_argument("--as-of", type=dt.date.fromisoformat, default=None, help="YYYY-MM-DD")
    p.add_argument("--iv-history-dir", default=None, help="Dir of <TICKER>.csv ATM IV history")
    p.add_argument("--record-iv", action="store_true", help="Upsert today's ATM IV into history")
    p.add_argument("--json", action="store_true", help="Emit the full result as JSON")


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
        elif cmd == "chains":
            _add_scan_args(
                sub.add_parser(cmd, help="Scan option chains for ranked credit structures")
            )
        else:
            sub.add_parser(cmd, help=f"{cmd.capitalize()} (stub)")

    from arc.data.history.cli import add_history_parser

    add_history_parser(sub)

    from arc.backtest.cli import add_backtest_parser

    add_backtest_parser(sub)

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


def _fmt_candidate(c: ScanCandidate) -> str:
    from arc.structures import parse_occ

    strikes = "/".join(
        f"{parse_occ(leg.occ_symbol).strike.normalize():f}" for leg in c.structure.legs
    )
    deltas = "/".join(f"{d * 100:.0f}" for d in c.short_deltas)
    return (
        f"{c.rank:>3}  {c.strategy.value:<11} {c.expiration} {c.dte:>3}d  {strikes:<16} "
        f"Δ{deltas:<6} w{c.width:<4g} cr {c.credit:>5.2f} nat {c.natural_credit:>5.2f}  "
        f"cr/w {c.credit_width:.3f}  PoP {c.pop:.2f}  EV {c.ev_proxy:>7.2f}  "
        f"maxL {c.structure.max_loss:.2f}"
    )


def _chains(args: argparse.Namespace) -> int:
    from pathlib import Path

    from arc.config import get_settings
    from arc.scanner import ScanParams, ScanStrategy, load_iv_history, record_iv, scan
    from arc.utils.calendar import now_et

    # stdout carries the report; structured logs go to whatever sys.stderr is at write
    # time (a proxy, so a replaced/closed stream is never captured in global config).
    structlog.configure(
        logger_factory=structlog.PrintLoggerFactory(file=_StderrProxy())  # type: ignore[arg-type]
    )
    settings = get_settings()

    provider: MarketDataProvider
    recorded = None
    if args.fixture:
        from arc.data.recorded import SPY_CHAIN_FIXTURE, RecordedMarketData

        paths = [SPY_CHAIN_FIXTURE if f.lower() == "spy" else Path(f) for f in args.fixture]
        provider = recorded = RecordedMarketData.from_files(*paths)
    else:
        from arc.data.alpaca import AlpacaMarketData

        try:
            provider = AlpacaMarketData()
        except RuntimeError as exc:
            sys.stderr.write(f"arc chains: {exc} (or pass --fixture spy to run offline)\n")
            return 2

    dte = args.dte or (None, None)
    try:
        params = ScanParams.from_settings(
            settings,
            dte_min=dte[0],
            dte_max=dte[1],
            target_delta=args.delta,
            wing_width=args.width,
            strategies=[ScanStrategy(s) for s in args.strategy] if args.strategy else None,
            rank_by=args.rank_by,
            top=args.top,
        )
    except ValueError as exc:
        sys.stderr.write(f"arc chains: {exc}\n")
        return 2

    iv_dir = Path(args.iv_history_dir) if args.iv_history_dir else settings.scanner_iv_history_dir
    results = []
    for ticker in args.tickers:
        if args.as_of is not None:
            as_of = args.as_of
        elif recorded is not None:
            try:
                as_of = recorded.recording(ticker).as_of
            except KeyError as exc:
                sys.stderr.write(f"arc chains: {exc.args[0]}\n")
                return 2
        else:
            as_of = now_et().date()
        res = scan(
            provider, ticker, params, as_of=as_of, iv_history=load_iv_history(iv_dir, ticker)
        )
        if args.record_iv and res.iv.atm_iv is not None:
            record_iv(iv_dir, ticker, as_of, res.iv.atm_iv)
        results.append(res)

    if args.json:
        payload = [r.model_dump(mode="json") for r in results]
        sys.stdout.write(json.dumps(payload, indent=2) + "\n")
        return 0

    lines: list[str] = []
    for r in results:
        iv = r.iv
        ivr = "n/a" if iv.iv_rank is None else f"{iv.iv_rank * 100:.0f}"
        ivp = "n/a" if iv.iv_percentile is None else f"{iv.iv_percentile * 100:.0f}"
        atm = "n/a" if iv.atm_iv is None else f"{iv.atm_iv * 100:.1f}%"
        fr = r.filter_report
        exps = ", ".join(str(e) for e in r.expirations) or "none"
        lines.append(f"{r.ticker} spot {r.spot:.2f}  as_of {r.as_of}  expirations {exps}")
        lines.append(
            f"  ATM IV {atm}  IVR {ivr}  IVP {ivp}  ({iv.observations} obs, lookback {iv.lookback})"
        )
        lines.append(f"  contracts {fr.total} -> liquid {fr.kept}  rejected {fr.rejected}")
        if not r.candidates:
            lines.append("  no candidates")
        lines.extend("  " + _fmt_candidate(c) for c in r.candidates)
        lines.append("")
    sys.stdout.write("\n".join(lines))
    return 0


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

    if args.command == "ingest":
        return _run_ingest(args)
    if args.command in HALT_COMMANDS:
        return _run_halt_command(args)
    if args.command == "scan":
        return _scan(args)
    if args.command == "chains":
        return _chains(args)
    if args.command == "history":
        from arc.data.history.cli import run_history

        return run_history(args)

    if args.command == "backtest":
        from arc.backtest.cli import run_backtest_cli

        return run_backtest_cli(args)

    logger.info("command.stub", command=args.command)
    print(f"arc {args.command}: not yet implemented")
    return 0


if __name__ == "__main__":
    sys.exit(main())
