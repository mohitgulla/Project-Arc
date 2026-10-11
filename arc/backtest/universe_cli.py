"""``arc history universe build|show`` (E7.7, D84): the point-in-time backtest universe.

The I/O shell around :mod:`arc.backtest.universe`: candidates (ThetaData symbol list
or the cached symbol master, + Alpaca inactive listed assets), Alpaca SIP daily bars
cached under ``<data-dir>/underlying_daily/``, the audit DB read-only for the names
Arc ever proposed or held, the optional ThetaData stage-2 probe, and the files.
"""

from __future__ import annotations

import datetime as dt
import re
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from arc.backtest.universe import (
    WARMUP_CALENDAR_DAYS,
    UniverseBuild,
    UniverseConfig,
    UniverseError,
    build_universe,
    checkpoint_symbols,
    ever_traded,
    format_build,
    load_bars,
    load_universe,
    master_candidates,
    run_stage2,
    stage1_survivors,
    universe_dir,
    write_build,
)
from arc.utils.calendar import now_et, previous_session, sessions_between

if TYPE_CHECKING:
    import argparse
    from collections.abc import Callable, Mapping

    from arc.backtest.universe import DailyBarsBatchSource
    from arc.universe.master import SymbolMaster

log = structlog.get_logger()

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
#: Inactive-asset names that are never an option root (warrants, units, rights, notes...).
_NOT_COMMON = re.compile(r"\bWARRANTS?\b|\bUNITS?\b|\bRIGHTS?\b|PREFERRED|\bNOTES?\b|%| WTS?\b")


def add_universe_parser(hs: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    u = hs.add_parser("universe", help="E7.7 point-in-time backtest universe (D84)")
    us = u.add_subparsers(dest="universe_command", required=True)

    b = us.add_parser("build", help="Build the priority-ordered pull list + membership")
    b.add_argument("--from", dest="start", type=dt.date.fromisoformat, help="First quarter")
    b.add_argument("--as-of", type=dt.date.fromisoformat, help="Default: previous session")
    b.add_argument("--stage1-only", action="store_true", help="Underlying $-volume only")
    b.add_argument("--data-dir", type=Path, default=Path("data"))
    b.add_argument("--config", type=Path, help="Ranking YAML (default config/ranking.yaml)")
    b.add_argument("--db", type=Path, help="Audit DB, opened read-only (default: settings)")
    b.add_argument("--no-db", action="store_true", help="Skip the ever-proposed/held names")
    b.add_argument(
        "--symbol-master",
        type=Path,
        help="Symbol master JSON (default <data-dir>/symbol_master.json, else universe.yaml's)",
    )
    b.add_argument(
        "--offline", action="store_true", help="Cached bars + symbol master only, no network"
    )
    b.add_argument("--theta-url", default=None, help="Theta Terminal v3 (default 127.0.0.1)")
    b.add_argument("--theta-tier", default="free", choices=["free", "value", "standard", "pro"])
    b.add_argument("--top", type=int, default=40, help="Pull-list rows to print")

    s = us.add_parser("show", help="Print a built universe (latest by default)")
    s.add_argument("--version", help="Manifest stem (default: the latest build)")
    s.add_argument("--data-dir", type=Path, default=Path("data"))
    s.add_argument("--checkpoint", help="Print only the symbols of c125|c250|c500")
    s.add_argument("--top", type=int, default=40)


def run_universe(args: argparse.Namespace) -> int:
    if args.universe_command == "show":
        return _show(args)
    return _build(args)


# ---------------------------------------------------------------------------


def _show(args: argparse.Namespace) -> int:
    manifest, rows, _ = load_universe(args.data_dir, args.version)
    if args.checkpoint:
        sys.stdout.write("\n".join(checkpoint_symbols(rows, args.checkpoint)) + "\n")
        return 0
    build = UniverseBuild(rows=rows, membership=[], manifest=manifest)
    sys.stdout.write(format_build(build, top=args.top) + "\n")
    return 0


def _git_sha() -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(_REPO_ROOT), "rev-parse", "HEAD"],  # noqa: S607 - fixed argv
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None


def load_universe_config(path: Path | None) -> UniverseConfig:
    from arc.backtest.ranking import load_ranking_file

    return load_ranking_file(path).backtest.universe


def _theta(args: argparse.Namespace):  # noqa: ANN202 - optional provider
    if args.offline:
        return None
    from arc.data.history.thetadata import (
        THETA_DEFAULT_URL,
        ThetaDataEodProvider,
        ThetaTerminalError,
    )

    url = args.theta_url or THETA_DEFAULT_URL
    try:  # reachability probe: no retries, short timeout
        ThetaDataEodProvider(base_url=url, max_retries=0, timeout_s=10.0).list_option_symbols()
    except ThetaTerminalError as exc:
        log.info("universe.theta_unavailable", error=str(exc)[:200])
        return None
    interval = 2.5 if args.theta_tier == "free" else 0.0  # free: 20-30 req/min
    return ThetaDataEodProvider(base_url=url, tier=args.theta_tier, min_interval_s=interval)


def _inactive_listed() -> dict[str, str]:  # pragma: no cover - network
    from alpaca.trading.client import TradingClient
    from alpaca.trading.enums import AssetClass, AssetStatus
    from alpaca.trading.requests import GetAssetsRequest

    from arc.data.alpaca import _get_keys

    key, secret = _get_keys()
    client = TradingClient(key, secret, paper=True)
    req = GetAssetsRequest(status=AssetStatus.INACTIVE, asset_class=AssetClass.US_EQUITY)
    out: dict[str, str] = {}
    for a in client.get_all_assets(req):
        exch = str(getattr(a.exchange, "value", a.exchange)).upper()
        name = (a.name or "").upper()
        if exch in ("OTC", "CRYPTO") or _NOT_COMMON.search(name):
            continue
        if not re.fullmatch(r"[A-Z]{1,5}(\.[A-Z])?", a.symbol):
            continue
        out[a.symbol.upper()] = a.name or ""
    return out


def _load_master(args: argparse.Namespace, now: dt.datetime) -> SymbolMaster | None:
    """``--symbol-master``, else ``<data-dir>/symbol_master.json``, else the configured cache."""
    from arc.universe import load_symbol_master
    from arc.universe import load_universe_config as load_live_universe

    smcfg = load_live_universe().symbol_master
    path = args.symbol_master or (Path(args.data_dir) / "symbol_master.json")
    if path.is_file():
        smcfg = smcfg.model_copy(update={"cache": path.resolve()})
    return load_symbol_master(smcfg, user_agent="arc-backtest", now=now, fetch_if_missing=False)


def gather_candidates(
    cfg: UniverseConfig,
    *,
    theta_symbols: list[str] | None,
    master_rows: Mapping[str, str],
    inactive: Mapping[str, str],
    ever: list[str],
) -> tuple[dict[str, str | None], str, dict[str, int], list[str]]:
    """Merge the candidate sources → (symbol→name, source label, counts, excluded roots)."""
    names: dict[str, str | None] = {}
    by_source: dict[str, int] = {}
    if theta_symbols:
        source = "thetadata"
        for s in theta_symbols:
            names[s] = master_rows.get(s)
        by_source["thetadata"] = len(theta_symbols)
    else:
        source = "symbol_master"
        names.update(master_rows)
        by_source["symbol_master"] = len(master_rows)
    added = 0
    for s, n in inactive.items():
        if s not in names:
            names[s] = n
            added += 1
    if inactive:
        by_source["alpaca_inactive_listed"] = added
    for s in [*cfg.always, *ever]:
        names.setdefault(s, master_rows.get(s))
    excluded: list[str] = []
    if not cfg.include_index_roots:
        bypass = set(cfg.always) | set(ever)
        for s in cfg.index_roots:
            if s in names and s not in bypass:
                del names[s]
                excluded.append(s)
    return names, source, by_source, sorted(excluded)


def _build(
    args: argparse.Namespace,
    *,
    bars_source_factory: Callable[[], DailyBarsBatchSource] | None = None,
) -> int:
    cfg = load_universe_config(args.config)
    if args.start:
        cfg = cfg.model_copy(update={"start": args.start})
    now = now_et()
    as_of = args.as_of or previous_session(now.date() + dt.timedelta(days=1))
    out = sys.stdout.write

    ever: list[str] = []
    if cfg.include_ever_traded and not args.no_db:
        db = args.db
        if db is None:
            from arc.config import get_settings

            db = get_settings().db_path or (_REPO_ROOT / "data" / "arc.db")
        if Path(db).is_file():
            ever = ever_traded(Path(db))
        else:
            out(f"note: audit DB {db} not found; no ever-proposed/held names\n")

    master = _load_master(args, now)
    master_rows = master_candidates(master) if master is not None else {}
    # names for anything the master knows (always/ever names not flagged optionable)
    master_names = {s: i.name for s, i in master.symbols.items()} if master is not None else {}

    theta = None if args.stage1_only and args.offline else _theta(args)
    theta_symbols = theta.list_option_symbols() if theta is not None else None
    if not args.offline:
        from arc.data.history.cli import _load_alpaca_env

        _load_alpaca_env()
    inactive = _inactive_listed() if cfg.add_inactive_listed and not args.offline else {}
    names, source, by_source, excluded = gather_candidates(
        cfg,
        theta_symbols=theta_symbols,
        master_rows=master_rows,
        inactive=inactive,
        ever=ever,
    )
    for s in names:
        if names[s] is None and s in master_names:
            names[s] = master_names[s]
    out(f"candidates: {len(names)} ({source}; {by_source})\n")

    lo = cfg.start - dt.timedelta(days=WARMUP_CALENDAR_DAYS)
    from arc.backtest.underlying import UnderlyingStore

    store = UnderlyingStore(args.data_dir)
    source_bars = None
    if not args.offline:
        if bars_source_factory is not None:
            source_bars = bars_source_factory()
        else:  # pragma: no cover - network
            from arc.backtest.universe import AlpacaDailyBarsBatch

            source_bars = AlpacaDailyBarsBatch()

    def progress(done: int, total: int) -> None:
        if done == total or done % (cfg.fetch_batch * 10) == 0:
            out(f"bars: {done}/{total} symbols fetched\n")

    bars, missing = load_bars(
        store,
        sorted(names),
        lo,
        as_of,
        source_bars,
        batch=cfg.fetch_batch,
        fetched_path=universe_dir(args.data_dir) / "cache" / "bars_fetched.json",
        progress=progress,
    )
    sessions = sessions_between(lo, as_of)

    scores = None
    stage1_only = args.stage1_only or not cfg.stage2.enabled or theta is None
    if not args.stage1_only and stage1_only:
        out("note: stage 2 skipped (disabled or no Theta Terminal); ranking by $-volume\n")
    skipped_q: list[dt.date] = []
    if not stage1_only and theta is not None:
        survivors = stage1_survivors(names, bars, cfg, as_of, sessions)
        scores, skipped_q = run_stage2(
            theta,
            survivors,
            sessions,
            cfg,
            with_oi=theta.with_oi,
            cache_path=universe_dir(args.data_dir) / "cache" / f"stage2_{args.theta_tier}.parquet",
            progress=lambda m: log.info("universe.stage2_progress", msg=m),
        )
    try:
        build = build_universe(
            candidates=names,
            bars=bars,
            cfg=cfg,
            as_of=as_of,
            built_at=now,
            sessions=sessions,
            ever=ever,
            stage2_scores=scores,
            stage1_only=stage1_only,
            missing_underlying=missing,
            candidate_source=source,
            candidates_by_source=by_source,
            excluded_index_roots=excluded,
            git_sha=_git_sha(),
        )
    except UniverseError as exc:
        sys.stderr.write(f"universe build failed: {exc}\n")
        return 2
    if skipped_q:
        build.manifest.notes.append(
            f"stage 2 skipped {len(skipped_q)} quarter(s) older than the {args.theta_tier} "
            f"tier serves ({skipped_q[0]}..{skipped_q[-1]}): ranked by $-volume (E7.8 month)"
        )
    if source != "thetadata":
        build.manifest.notes.append(
            "candidates from the current symbol master + Alpaca's inactive asset list, which "
            "misses many delisted names (e.g. TWTR, SIVB, ATVI): survivorship coverage is "
            "partial; rebuild with the Theta Terminal up for its full root list"
        )
    paths = write_build(build, args.data_dir)
    out(format_build(build, top=args.top) + "\n")
    for k, p in paths.items():
        out(f"{k}: {p}\n")
    return 0
