"""``arc history stats build|show`` (E7.16, D84): per-ticker daily option stats.

``build`` summarises each pulled ticker from the cached parquet (no network). With
``--follow`` it polls the E7.9 pull every ``--poll-seconds`` (600) and processes each
ticker the moment all of its sessions carry quotes + OI, in universe priority order,
until the last checkpoint is done (or the cache stops changing for ``--idle-polls``
polls). Re-running is idempotent: a ticker whose source files, code and settings are
unchanged since the manifest entry is skipped.
"""

from __future__ import annotations

import datetime as dt
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

import pandas as pd
import structlog

from arc.utils.calendar import now_et, previous_session

if TYPE_CHECKING:
    import argparse

log = structlog.get_logger()


def _date(s: str) -> dt.date:
    return dt.date.fromisoformat(s)


def add_stats_parser(hs: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = hs.add_parser("stats", help="E7.16 per-ticker daily option stats from the cache")
    ss = p.add_subparsers(dest="stats_command", required=True)

    b = ss.add_parser("build", help="Compute daily stats / earnings events / quarterly liquidity")
    b.add_argument("--tickers", help="Comma-separated (default: the --universe checkpoint)")
    b.add_argument("--universe", default="c500", help="c125|c250|c500 of the E7.7 pull list")
    b.add_argument("--version", help="E7.7 universe version (default: latest build)")
    b.add_argument("--start", type=_date, help="Default: the universe's start (2020-01-01)")
    b.add_argument("--end", type=_date, help="Default: previous session")
    b.add_argument("--data-dir", type=Path, default=Path("data"))
    b.add_argument("--config", type=Path, help="Ranking YAML (default config/ranking.yaml)")
    b.add_argument("--provider", default="thetadata")
    b.add_argument(
        "--allow-incomplete",
        action="store_true",
        help="Also compute tickers the pull hasn't finished (manifest marks them partial)",
    )
    b.add_argument("--follow", action="store_true", help="Poll the pull until C500 is done")
    b.add_argument("--poll-seconds", type=float, default=600.0)
    b.add_argument(
        "--idle-polls",
        type=int,
        default=36,
        help="--follow exits after this many polls with no cache change (36 x 10 min = 6 h)",
    )

    s = ss.add_parser("show", help="Summary table for one ticker")
    s.add_argument("ticker")
    s.add_argument("--from", dest="start", type=_date)
    s.add_argument("--to", dest="end", type=_date)
    s.add_argument("--data-dir", type=Path, default=Path("data"))


def run_stats(args: argparse.Namespace) -> int:
    if args.stats_command == "show":
        return _show(args)
    return _build(args)


def _runner(args: argparse.Namespace):  # noqa: ANN202
    from arc.backtest.costs import load_cost_model
    from arc.backtest.option_stats import StatsConfig, StatsRunner
    from arc.backtest.ranking import load_ranking_file
    from arc.backtest.universe import load_universe
    from arc.config import ArcSettings

    cfg = load_ranking_file(args.config)
    stats_cfg = StatsConfig(
        provider=args.provider,
        data=cfg.backtest.data,
        stage2=cfg.backtest.universe.stage2,
        flat_rate=cfg.backtest.risk_free_rate,
        min_iv_obs=ArcSettings().iv_min_obs_rank,
    )
    manifest, rows, membership = load_universe(args.data_dir, args.version)
    start = args.start or cfg.backtest.universe.start
    end = args.end or previous_session(now_et().date() + dt.timedelta(days=1))
    runner = StatsRunner(
        args.data_dir,
        cfg=stats_cfg,
        cost=load_cost_model(),
        rows=rows,
        start=start,
        end=end,
        version=manifest.version,
        membership=membership,
    )
    return runner, manifest.version


def _targets(args: argparse.Namespace, runner) -> list[str]:  # noqa: ANN001
    if args.tickers:
        names = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
        ref = runner._ref()  # noqa: SLF001 - the market reference goes first
        return ([ref] if ref and ref not in names and len(names) else []) + names
    size = int(args.universe.lower().removeprefix("c"))
    return [r.symbol for r in runner.rows if r.position <= size]


def _build(args: argparse.Namespace) -> int:
    runner, version = _runner(args)
    names = _targets(args, runner)
    out = sys.stdout
    out.write(
        f"option stats: universe {version}, {len(names)} names, window {runner.start} -> "
        f"{runner.end}, code {runner.code}\n"
    )

    def done(t: str, o: str) -> None:
        if o == "computed":
            e = runner.manifest["tickers"][t]
            out.write(
                f"  {t:<6} {e['sessions']:>5} sessions  {e['events']:>3} events  "
                f"{e['wall_seconds']:>6.1f} s{'' if e['complete_in_pull'] else '  (partial)'}\n"
            )
            out.flush()

    if not args.follow:
        res = runner.pass_once(
            tickers=names, require_complete=not args.allow_incomplete, on_done=done
        )
        runner.write_quarterly()
        runner.record_checkpoints(now_et())
        counts = pd.Series(list(res.values())).value_counts().to_dict() if res else {}
        out.write(f"done: {counts}\n")
        return 0

    idle = 0
    last_sig: tuple[int, int] | None = None

    def sleep(sec: float) -> None:
        nonlocal idle, last_sig
        sig = _cache_signature(runner)
        idle = idle + 1 if sig == last_sig else 0
        last_sig = sig
        if idle >= args.idle_polls:
            raise _Idle
        time.sleep(sec)

    try:
        polls = runner.follow(
            now=now_et,
            sleep=sleep,
            poll_seconds=args.poll_seconds,
            on_event=lambda m: out.write(m + "\n"),
            tickers=names,
        )
    except _Idle:
        left = [t for t in names if t not in runner.manifest["tickers"]]
        out.write(
            f"follow: cache unchanged for {args.idle_polls} polls; exiting with "
            f"{len(left)} names not computed: {', '.join(left[:40])}\n"
        )
        return 1
    out.write(f"follow: every name done after {polls} polls\n")
    return 0


class _Idle(Exception):  # noqa: N818 - control flow
    pass


def _cache_signature(runner) -> tuple[int, int]:  # noqa: ANN001
    root = runner.store.root / f"provider={runner.cfg.provider}"
    if not root.is_dir():
        return (0, 0)
    dirs = [p for p in root.iterdir() if p.is_dir()]
    return (len(dirs), max((p.stat().st_mtime_ns for p in dirs), default=0))


def _show(args: argparse.Namespace) -> int:
    from arc.backtest.option_stats import read_daily, stats_dir

    t = args.ticker.upper()
    df = read_daily(args.data_dir, t)
    if args.start:
        df = df[df["date"] >= args.start]
    if args.end:
        df = df[df["date"] <= args.end]
    out = sys.stdout
    if df.empty:
        out.write(f"{t}: no option stats under {stats_dir(args.data_dir)}\n")
        return 1
    out.write(format_show(t, df, _events(args.data_dir, t, args.start, args.end)) + "\n")
    return 0


def _events(data_dir: Path, t: str, a: dt.date | None, b: dt.date | None) -> pd.DataFrame:
    from arc.backtest.option_stats import stats_dir

    p = stats_dir(data_dir) / "earnings_events.parquet"
    if not p.is_file():
        return pd.DataFrame()
    ev = pd.read_parquet(p)
    ev = ev[ev["ticker"] == t]
    if len(ev):
        ev["event_date"] = pd.to_datetime(ev["event_date"]).dt.date
        if a:
            ev = ev[ev["event_date"] >= a]
        if b:
            ev = ev[ev["event_date"] <= b]
    return ev


def _pct(v: object, nd: int = 1) -> str:
    return "  n/a" if v is None or not pd.notna(v) else f"{float(v) * 100:.{nd}f}"  # type: ignore[arg-type]


def format_show(t: str, df: pd.DataFrame, ev: pd.DataFrame) -> str:
    """Rows, sessions, IV30 range and per-year medians, plus the earnings summary."""
    iv = pd.to_numeric(df["iv30"], errors="coerce")
    lines = [
        f"{t}: {len(df)} sessions {df['date'].min()} -> {df['date'].max()} · "
        f"option rows {int(df['rows'].fillna(0).sum()):,} · "
        f"IV30 {_pct(iv.min())}–{_pct(iv.max())} (median {_pct(iv.median())}) · "
        f"IV rank available {int(df['iv_rank_252'].notna().sum())} sessions · "
        f"D9 pass {_pct(df['d9_pass'].fillna(False).astype(bool).mean(), 0)}%",
        f"{'year':<5} {'sess':>5} {'rows/sess':>9} {'iv30':>6} {'ivr':>5} {'rv20':>6} "
        f"{'vrp20':>6} {'skew25':>6} {'term':>6} {'atm_spr%':>8} {'2side%':>6} "
        f"{'oi_atm':>9} {'opt_vol':>9} {'pc_oi':>5} {'d9%':>4}",
    ]
    d = df.copy()
    d["year"] = pd.to_datetime(d["date"]).dt.year
    for y, g in d.groupby("year"):
        med = g.median(numeric_only=True)
        lines.append(
            f"{y:<5} {len(g):>5} {med['rows']:>9.0f} {_pct(med['iv30']):>6} "
            f"{_pct(med['iv_rank_252'], 0):>5} {_pct(med['rv20']):>6} {_pct(med['vrp20']):>6} "
            f"{_pct(med['skew_25d']):>6} {_pct(med['term_slope']):>6} "
            f"{_pct(med['atm_spread_pct'], 2):>8} {_pct(med['two_sided_pct'], 0):>6} "
            f"{med['oi_near_atm']:>9,.0f} {med['opt_volume']:>9,.0f} "
            f"{med['put_call_oi']:>5.2f} "
            f"{_pct(g['d9_pass'].fillna(False).astype(bool).mean(), 0):>4}"
        )
    lines.append("(vols, spreads and shares in %; medians per year)")
    if len(ev):
        crush = pd.to_numeric(ev["iv_crush_pts"], errors="coerce")
        roi = pd.to_numeric(ev["realised_over_implied"], errors="coerce")
        crush_pct = pd.to_numeric(ev["iv_crush_pct"], errors="coerce").median()
        lines.append(
            f"earnings: {len(ev)} detected events ({ev['basis'].iloc[0]}) · median IV crush "
            f"{_pct(crush.median())} pts ({_pct(crush_pct, 0)}%) · "
            f"median implied move {_pct(pd.to_numeric(ev['implied_move']).median())}% · "
            f"realised {_pct(pd.to_numeric(ev['realised_move']).median())}% · "
            f"realised/implied {roi.median():.2f}"
        )
    else:
        lines.append("earnings: no detected events")
    return "\n".join(lines)
