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

COMMANDS = (
    "scan",
    "chains",
    "ingest",
    "brief",
    "propose",
    "gate",
    "approve",
    "execute",
    "reconcile",
    "report",
)
HALT_COMMANDS = ("halt", "resume", "halt-status", "slack-command")


class _StderrProxy:
    """File-like object that always writes to the *current* ``sys.stderr``."""

    def write(self, s: str) -> int:
        return sys.stderr.write(s)

    def flush(self) -> None:
        sys.stderr.flush()


def _log_to_stderr() -> None:
    """stdout carries JSON reports; structured logs go to stderr."""
    structlog.configure(
        logger_factory=structlog.PrintLoggerFactory(file=_StderrProxy())  # type: ignore[arg-type]
    )


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
        "--profile",
        default=None,
        help="Account profile (margin | cash_debit | cash_long_only); default ARC_ACCOUNT_PROFILE. "
        "Sets the default strategies and DTE window (D25).",
    )
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
            p = sub.add_parser(cmd, help="Scan: summarise ingested docs into Candidates (Sweep)")
            p.add_argument(
                "--dry-run",
                action="store_true",
                help="Fixture docs + canned Sweep responses in an in-memory DB (no network).",
            )
            p.add_argument("--db", default=None, help="SQLite path (default: data/arc.db)")
        elif cmd == "chains":
            _add_scan_args(
                sub.add_parser(cmd, help="Scan option chains for ranked option structures")
            )
        elif cmd == "ingest":
            p = sub.add_parser(cmd, help="Ingest source documents")
            p.add_argument("source", choices=["youtube"], help="Connector to run")
            p.add_argument(
                "--process",
                action="store_true",
                help="Run each channel processor on new transcripts and store ChannelBriefs.",
            )
            p.add_argument(
                "--dry-run",
                action="store_true",
                help="Fixture transcript + canned LLM reply in an in-memory DB (no network).",
            )
            p.add_argument(
                "--no-prices",
                action="store_true",
                help="Skip the market-data price check (levels are flagged unverified_price).",
            )
            p.add_argument(
                "--force-audio",
                action="store_true",
                help="Skip captions and transcribe audio locally (ignores grace period, E4.1b).",
            )
            p.add_argument("--max-videos", type=int, default=5, help="Videos per channel.")
            p.add_argument("--db", default=None, help="SQLite path (default: data/arc.db)")
        elif cmd == "brief":
            p = sub.add_parser(cmd, help="Channel briefs (E4.4)")
            bsub = p.add_subparsers(dest="brief_command", required=True)
            show = bsub.add_parser("show", help="Print the active brief(s) as JSON")
            show.add_argument("--channel", default=None, help="Channel slug, e.g. stockedup")
            show.add_argument("--db", default=None, help="SQLite path (default: data/arc.db)")
        elif cmd == "propose":
            p = sub.add_parser(
                cmd, help="Run Sweep → Director → Quant → Risk → propose (+ gate) now (E5.2)"
            )
            mode = p.add_mutually_exclusive_group()
            mode.add_argument(
                "--dry-run",
                action="store_true",
                help="No Slack, no broker; in-memory copy of the DB unless --db is given.",
            )
            mode.add_argument(
                "--fixtures",
                action="store_true",
                help="Fully offline: recorded SPY chain + canned persona replies, in-memory DB.",
            )
            p.add_argument(
                "--fixture-set",
                choices=["neutral", "bullish"],
                default="neutral",
                help="Canned persona replies for --fixtures: neutral (SPY condor) or bullish "
                "(SPY bull call debit).",
            )
            p.add_argument(
                "--fixture-offset-minutes",
                type=int,
                default=0,
                help="With --fixtures: run the clock this many minutes after the recording "
                "(a second run on the same --db then exercises the E5.9 idea dedupe).",
            )
            p.add_argument(
                "--profile",
                default=None,
                help="Account profile for this run (overrides ARC_ACCOUNT_PROFILE; D25).",
            )
            p.add_argument("--no-sweep", action="store_true", help="Skip the Sweep job.")
            p.add_argument("--no-slack", action="store_true", help="Log heartbeats only.")
            p.add_argument("--json", action="store_true", help="Print the report as JSON.")
            p.add_argument("--db", default=None, help="SQLite path (default: data/arc.db)")
            p.add_argument("--routines", default=None, help="routines.yaml path")
            p.add_argument("--lock-dir", default="data/locks")
        elif cmd == "approve":
            from arc.approvals.cli import add_approve_parser

            add_approve_parser(sub)
        elif cmd == "execute":
            from arc.execution.cli import add_execute_parser

            add_execute_parser(sub)
        elif cmd == "reconcile":
            from arc.reconcile.cli import add_reconcile_parser

            add_reconcile_parser(sub)
        else:
            sub.add_parser(cmd, help=f"{cmd.capitalize()} (stub)")

    from arc.data.history.cli import add_history_parser
    from arc.journal.cli import add_journal_parser, add_scorecard_parser
    from arc.routines.cli import add_context_parser, add_routines_parser

    add_history_parser(sub)
    add_journal_parser(sub)
    add_scorecard_parser(sub)
    add_routines_parser(sub)
    add_context_parser(sub)

    from arc.monitoring.cli import add_health_parser

    add_health_parser(sub)

    from arc.backtest.cli import add_backtest_parser

    add_backtest_parser(sub)

    from arc.exits.cli import add_exits_parser

    add_exits_parser(sub)

    from arc.positions.cli import add_positions_parser

    add_positions_parser(sub)

    from arc.budget.cli import add_budget_parser

    add_budget_parser(sub)

    from arc.tower.cli import add_tower_parser

    add_tower_parser(sub)

    from arc.control.cli import add_config_parser

    add_config_parser(sub)

    from arc.experiments.cli import add_experiment_parser

    add_experiment_parser(sub)

    from arc.universe.cli import add_universe_parser

    add_universe_parser(sub)

    from arc.remote.cli import add_remote_parser

    add_remote_parser(sub)

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


def _fmt_candidate(c: ScanCandidate) -> str:
    from arc.structures import parse_occ

    strikes = "/".join(
        f"{parse_occ(leg.occ_symbol).strike.normalize():f}" for leg in c.structure.legs
    )
    head = f"{c.rank:>3}  {c.strategy.value:<15} {c.expiration} {c.dte:>3}d  {strikes:<16} "
    tail = f"PoP {c.pop:.2f}  EV {c.ev_proxy:>7.2f}  maxL {c.structure.max_loss:.2f}"
    if c.credit_width is None:  # debit structure (D25): long Δ / short Δ, debit, EV/maxL
        longs = "/".join(f"{d * 100:.0f}" for d in c.long_deltas)
        shorts = "/".join(f"{d * 100:.0f}" for d in c.short_deltas) or "-"
        width = "-" if c.width is None else f"{c.width:g}"
        ratio = "n/a" if c.ev_ratio is None else f"{c.ev_ratio:.3f}"
        return (
            f"{head}Δ+{longs}/-{shorts:<5} w{width:<4} db {-c.credit:>5.2f} "
            f"nat {-c.natural_credit:>5.2f}  ev/L {ratio}  {tail}"
        )
    deltas = "/".join(f"{d * 100:.0f}" for d in c.short_deltas)
    return (
        f"{head}Δ{deltas:<6} w{c.width:<4g} cr {c.credit:>5.2f} nat {c.natural_credit:>5.2f}  "
        f"cr/w {c.credit_width:.3f}  {tail}"
    )


def _chains(args: argparse.Namespace) -> int:
    from pathlib import Path

    from arc.config import get_settings
    from arc.scanner import ScanParams, ScanStrategy, load_iv_history, record_iv, scan
    from arc.utils.calendar import now_et

    # stdout carries the report; structured logs go to whatever sys.stderr is at write
    # time (a proxy, so a replaced/closed stream is never captured in global config).
    _log_to_stderr()
    settings = get_settings()
    if args.profile:
        try:
            settings = settings.with_profile(args.profile)
        except KeyError as exc:
            sys.stderr.write(f"arc chains: {exc.args[0]}\n")
            return 2

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
        lines.append(
            f"{r.ticker} spot {r.spot:.2f}  as_of {r.as_of}  expirations {exps}  "
            f"profile {settings.account_profile}"
        )
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
    from arc.ingest.sweep import load_fixture_docs, run_sweep
    from arc.store.db import connect
    from arc.store.migrate import migrate

    # stdout carries the JSON report; keep structured logs on stderr.
    _log_to_stderr()
    settings = get_settings()
    conn = connect(":memory:" if args.dry_run else args.db)
    migrate(conn)
    if args.dry_run:
        load_fixture_docs(conn)

    if args.dry_run:
        # D47: fixture docs are fresh on the fixture clock, not the wall clock.
        from arc.pipeline.env import FIXTURE_NOW

        result = run_sweep(conn, settings, dry_run=True, now=FIXTURE_NOW)
    else:
        result = run_sweep(conn, settings, dry_run=False)
    report = {
        "run_id": result.run_id,
        "day": result.day,
        "dry_run": result.dry_run,
        "batches": result.batches,
        "failed_batches": result.failed_batches,
        "docs_swept": result.docs_swept,
        "accepted": result.accepted,
        "rejected": dict(result.rejected),
        "candidates": [c.model_dump(mode="json") for c in result.candidates],
    }
    sys.stdout.write(json.dumps(report, indent=2) + "\n")
    return 1 if result.failed_batches and not result.docs_swept else 0


def _market_price_lookup():  # noqa: ANN202 — Callable[[str], float | None] | None
    """Reference underlying price from the market-data provider, or None if unavailable.

    Market data only: the processor never gets a broker handle (AGENTS.md).
    """
    try:
        from arc.data.alpaca import AlpacaMarketData
        from arc.data.base import reference_price
        from arc.utils.calendar import now_et

        md = AlpacaMarketData()
    except Exception as exc:  # noqa: BLE001 — no keys / no network → unverified levels
        logger.warning("brief.price_lookup_unavailable", error=str(exc))
        return None

    def lookup(symbol: str) -> float | None:
        return reference_price(md, symbol, today=now_et().date())

    return lookup


def _ingest(args: argparse.Namespace) -> int:
    from arc.config import get_settings
    from arc.ingest.channels import default_registry
    from arc.ingest.channels.briefs import (
        fixture_llm,
        load_channel_fixture,
        process_new_videos,
    )
    from arc.ingest.llm import HermesSweepLLM
    from arc.ingest.youtube import fetch_youtube
    from arc.store.db import connect
    from arc.store.migrate import migrate

    _log_to_stderr()
    settings = get_settings()
    conn = connect(":memory:" if args.dry_run else args.db)
    migrate(conn)
    registry = default_registry()
    sources: list[object] = []

    if args.dry_run:
        proc = registry.for_slug("stockedup") or registry.default
        load_channel_fixture(conn, proc)
        new_docs = 1
        llm = fixture_llm(proc)
    else:
        docs = fetch_youtube(
            conn, settings, force_audio=args.force_audio, max_videos=args.max_videos
        )
        new_docs = len(docs)
        sources = [d.transcript_source for d in docs]
        llm = HermesSweepLLM.from_settings(settings)

    report: dict[str, object] = {"dry_run": args.dry_run, "new_videos": new_docs}
    if not args.dry_run:
        report["transcript_sources"] = [str(src) if src else None for src in sources]
    if args.process:
        lookup = None if (args.dry_run or args.no_prices) else _market_price_lookup()
        run = process_new_videos(conn, settings, llm, registry=registry, price_lookup=lookup)
        report.update(
            {
                "processed": run.processed,
                "stored": run.stored,
                "skipped": run.skipped,
                "failed": run.failed,
                "briefs": [
                    {
                        "brief": r.brief.model_dump(mode="json"),
                        "kept": r.kept,
                        "dropped": r.dropped_by_reason,
                        "dropped_items": [d.as_dict() for d in r.dropped],
                        "sponsor_sentences_removed": r.sponsor_sentences_removed,
                        "model": r.model,
                    }
                    for r in run.results
                ],
                "candidates": [c.model_dump(mode="json") for c in run.candidates],
            }
        )
    sys.stdout.write(json.dumps(report, indent=2, default=str) + "\n")
    return 1 if args.process and report.get("failed") and not report.get("processed") else 0


def _brief(args: argparse.Namespace) -> int:
    from arc.ingest.channels.briefs import active_briefs
    from arc.store.db import connect
    from arc.store.migrate import migrate

    _log_to_stderr()
    conn = connect(args.db)
    migrate(conn)
    briefs = active_briefs(conn, channel_slug=args.channel)
    if args.channel:
        payload: object = briefs[0].model_dump(mode="json") if briefs else None
    else:
        payload = [b.model_dump(mode="json") for b in briefs]
    sys.stdout.write(json.dumps(payload, indent=2) + "\n")
    return 0 if briefs else 1


def _propose(args: argparse.Namespace) -> int:
    """``arc propose``: one end-to-end pipeline pass now (E5.2). Never submits orders."""
    import json

    from arc.config import get_settings
    from arc.pipeline import PipelineEnv, run_propose
    from arc.pipeline.runner import fixture_run, open_db
    from arc.routines.heartbeat import LogNotifier, Notifier, SlackDayThreadNotifier
    from arc.routines.locks import LockManager, NullLocks
    from arc.utils.calendar import now_et

    _log_to_stderr()
    settings = get_settings()
    # D26: the same effective config (DB overrides) the routines tick uses. Read-only:
    # --fixtures without --db runs in memory, so no overrides apply there.
    from arc.control.effective import effective_from_path

    settings, routines = effective_from_path(
        ":memory:" if args.fixtures and not args.db else args.db,
        base=settings,
        routines_path=args.routines,
    )
    if args.profile:
        try:
            settings = settings.with_profile(args.profile)
        except KeyError as exc:
            sys.stderr.write(f"arc propose: {exc.args[0]}\n")
            return 2
    if not (args.fixtures or args.dry_run):
        from arc.gate.token import TokenError, gate_secret

        try:
            gate_secret(settings)
        except TokenError as exc:
            sys.stderr.write(
                f"arc propose: {exc}. Set ARC_GATE_SECRET in ~/.hermes/.env, "
                "or use --dry-run / --fixtures.\n"
            )
            return 2
    if args.fixtures:
        from arc.pipeline.env import FIXTURE_NOW

        conn, report = fixture_run(
            settings,
            routines,
            db=args.db,
            fixture_set=args.fixture_set,
            now=FIXTURE_NOW + dt.timedelta(minutes=args.fixture_offset_minutes),
        )
    else:
        conn = open_db(args.db, copy=args.dry_run and args.db is None)
        env = PipelineEnv.live(settings, broker=not args.dry_run, conn=conn)
        notifier: Notifier = (
            LogNotifier() if args.dry_run or args.no_slack else SlackDayThreadNotifier(conn)
        )
        report = run_propose(
            conn,
            settings,
            routines,
            env,
            now=now_et(),
            clock=now_et,  # E5.2b: quotes/gate/token/expiry judged at step time
            notifier=notifier,
            sweep=not args.no_sweep,
            locks=NullLocks() if args.dry_run else LockManager(args.lock_dir),
            mode="dry-run" if args.dry_run else "live",
        )
    # E6.1: a card per new proposal. Only a live run posts to Slack; dry runs and
    # fixtures log the card (their proposals carry no token, so it is info-only).
    # A live --no-slack run leaves the cards to the next `arc routines tick`.
    from arc.approvals.cli import make_service

    offline = args.fixtures or args.dry_run
    sweep = None
    if offline or not args.no_slack:
        svc = make_service(conn, settings, slack=not offline)
        sweep = svc.sweep(report.now, day=report.day)
    if args.json:
        payload = {**report.as_json(), "approvals": sweep.as_json() if sweep else None}
        _out(json.dumps(payload, indent=2))
    else:
        _out("\n".join(report.lines()))
        if sweep is not None:
            where = "log" if offline else "slack"
            _out(f"approval cards: {len(sweep.published)} posted ({where})")
    return 1 if report.failed else 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = _make_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 0

    if args.command in HALT_COMMANDS:
        return _run_halt_command(args)
    if args.command == "scan":
        return _scan(args)
    if args.command == "chains":
        return _chains(args)
    if args.command == "ingest":
        return _ingest(args)
    if args.command == "brief":
        return _brief(args)
    if args.command == "propose":
        return _propose(args)
    if args.command == "approve":
        from arc.approvals.cli import run_approve

        _log_to_stderr()
        return run_approve(args)
    if args.command == "execute":
        from arc.execution.cli import run_execute

        _log_to_stderr()
        return run_execute(args)
    if args.command == "reconcile":
        from arc.reconcile.cli import run_reconcile

        _log_to_stderr()
        return run_reconcile(args)
    if args.command == "universe":
        from arc.universe.cli import run_universe

        _log_to_stderr()
        return run_universe(args)
    if args.command == "journal":
        from arc.journal.cli import run_journal

        _log_to_stderr()
        return run_journal(args)
    if args.command == "scorecard":
        from arc.journal.cli import run_scorecard

        _log_to_stderr()
        return run_scorecard(args)
    if args.command == "history":
        from arc.data.history.cli import run_history

        return run_history(args)

    if args.command == "routines":
        from arc.routines.cli import run_routines

        return run_routines(args)

    if args.command == "health":
        from arc.monitoring.cli import run_health

        return run_health(args)

    if args.command == "context":
        from arc.routines.cli import run_context

        return run_context(args)

    if args.command == "backtest":
        from arc.backtest.cli import run_backtest_cli

        return run_backtest_cli(args)

    if args.command == "exits":
        from arc.exits.cli import run_exits

        _log_to_stderr()
        return run_exits(args)

    if args.command == "budget":
        from arc.budget.cli import run_budget

        _log_to_stderr()
        return run_budget(args)

    if args.command == "positions":
        from arc.positions.cli import run_positions

        _log_to_stderr()
        return run_positions(args)

    if args.command == "tower":
        from arc.tower.cli import run_tower

        _log_to_stderr()
        return run_tower(args)

    if args.command == "remote":
        from arc.remote.cli import run_remote

        _log_to_stderr()
        return run_remote(args)

    if args.command == "config":
        from arc.control.cli import run_config

        _log_to_stderr()
        return run_config(args)

    if args.command == "experiment":
        from arc.experiments.cli import run_experiment

        _log_to_stderr()
        return run_experiment(args)

    logger.info("command.stub", command=args.command)
    print(f"arc {args.command}: not yet implemented")
    return 0


if __name__ == "__main__":
    sys.exit(main())
