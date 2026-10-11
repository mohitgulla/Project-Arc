"""``arc history universe pull-status|optliq`` (E7.9, D84): the pull's progress and score.

``pull-status`` counts the cached ThetaData session files of a universe's pull list per
checkpoint, measures the rate from its own snapshots, prints the progress post and,
with ``--mark-complete``, verifies quotes + OI for any checkpoint that just reached
100 % and records it in ``checkpoints.json`` (printing ``C125 complete …``).
``optliq`` writes the per-quarter options-liquidity sidecar for one checkpoint.
Read-only to everything except ``<data-dir>/backtest_universe/``. Aggregates only.
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from arc.backtest.pull import (
    CheckpointRecord,
    format_status,
    measured_rate,
    optliq_scores,
    optliq_summary,
    pull_status,
    read_checkpoints,
    record_snapshot,
    verify_oi,
    write_checkpoint,
    write_optliq,
)
from arc.backtest.universe import checkpoint_symbols, load_universe
from arc.utils.calendar import now_et, previous_session, sessions_between

if TYPE_CHECKING:
    import argparse

log = structlog.get_logger()

__all__ = ["add_pull_parsers", "run_optliq", "run_pull_status"]


def add_pull_parsers(us: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    st = us.add_parser("pull-status", help="E7.9: pull progress per checkpoint + checkpoints.json")
    st.add_argument("--version", help="Universe manifest stem (default: the latest build)")
    st.add_argument("--data-dir", type=Path, default=Path("data"))
    st.add_argument("--provider", default="thetadata")
    st.add_argument("--start", type=dt.date.fromisoformat, default=dt.date(2020, 1, 1))
    st.add_argument("--end", type=dt.date.fromisoformat, help="Last pulled session")
    st.add_argument("--deadline", type=dt.date.fromisoformat, help="Subscription cancel date")
    st.add_argument("--buffer-days", type=int, default=7)
    st.add_argument("--record", action="store_true", help="Append a rate snapshot")
    st.add_argument(
        "--mark-complete",
        action="store_true",
        help="Verify OI and write checkpoints.json for newly complete checkpoints",
    )
    st.add_argument(
        "--only-events",
        action="store_true",
        help="Print nothing unless a checkpoint completed this run (cron mode)",
    )

    oq = us.add_parser("optliq", help="E7.9: per-quarter option-liquidity score sidecar")
    oq.add_argument("--version", help="Universe manifest stem (default: the latest build)")
    oq.add_argument("--data-dir", type=Path, default=Path("data"))
    oq.add_argument("--provider", default="thetadata")
    oq.add_argument("--checkpoint", default="c500", help="Score only this checkpoint's names")
    oq.add_argument("--config", type=Path, help="Ranking YAML (default config/ranking.yaml)")


def _sessions(args: argparse.Namespace) -> list[dt.date]:
    end = args.end or previous_session(now_et().date())
    return sessions_between(args.start, end)


def run_pull_status(args: argparse.Namespace) -> int:
    manifest, rows, _ = load_universe(args.data_dir, args.version)
    sessions = _sessions(args)
    now = now_et()
    status = pull_status(
        args.data_dir, rows, version=manifest.version, provider=args.provider, sessions=sessions
    )
    last = status.checkpoints[-1]
    rate = measured_rate(args.data_dir, manifest.version, now, last.sessions_done)
    if args.record:
        record_snapshot(args.data_dir, status, now)
    events: list[str] = []
    if args.mark_complete:
        recorded = read_checkpoints(args.data_dir)
        for c in status.checkpoints:
            done = recorded.get(c.checkpoint)
            if not c.complete or (done is not None and done.complete_at is not None):
                continue
            names = checkpoint_symbols(rows, c.checkpoint)
            bad, n_rows = verify_oi(args.data_dir, names, provider=args.provider, sessions=sessions)
            if bad:
                s, d = bad[0]
                events.append(
                    f"{c.checkpoint.upper()} files all present but {len(bad)} session(s) lack OI "
                    f"(first {s} {d}); the pull's OI fill will redo them"
                )
                continue
            write_checkpoint(
                args.data_dir,
                c.checkpoint,
                CheckpointRecord(
                    complete_at=now,
                    sessions=len(sessions),
                    gb=round(c.gb, 2),
                    rows=n_rows,
                    names=c.names,
                    version=manifest.version,
                    provider=args.provider,
                    start=sessions[0],
                    end=sessions[-1],
                ),
            )
            events.append(
                f"✅ {c.checkpoint.upper()} complete · {c.names} names × {len(sessions)} sessions "
                f"({sessions[0]} → {sessions[-1]}) · quotes + OI · {c.gb:.1f} GB · "
                f"{n_rows:,} rows · recorded in checkpoints.json"
            )
    out = sys.stdout.write
    if args.only_events:
        if events:
            out("E7.9 · ThetaData pull\n" + "\n".join(events) + "\n")
        return 0
    out(
        format_status(
            status,
            now=now,
            sessions_per_hour=rate,
            deadline=args.deadline,
            buffer_days=args.buffer_days,
        )
        + "\n"
    )
    for e in events:
        out(e + "\n")
    return 0


def run_optliq(args: argparse.Namespace) -> int:
    from arc.backtest.underlying import UnderlyingStore
    from arc.backtest.universe_cli import load_universe_config

    manifest, rows, membership = load_universe(args.data_dir, args.version)
    cfg = load_universe_config(args.config).stage2
    names = checkpoint_symbols(rows, args.checkpoint)
    store = UnderlyingStore(args.data_dir)
    closes: dict[str, dict[dt.date, float]] = {}

    def spot(sym: str, d: dt.date) -> float | None:
        if sym not in closes:
            s = store.read(sym)
            closes[sym] = {k: float(v) for k, v in s.items()}
        return closes[sym].get(d)

    lo = min(membership["quarter"]) - dt.timedelta(days=60)
    sessions = sessions_between(lo, previous_session(now_et().date()))

    def progress(k: int, n: int) -> None:
        if k == n or k % 25 == 0:
            sys.stdout.write(f"optliq: {k}/{n} names\n")

    scored = optliq_scores(
        args.data_dir,
        membership,
        provider=args.provider,
        cfg=cfg,
        sessions=sessions,
        spot=spot,
        symbols=names,
        progress=progress,
    )
    path = write_optliq(args.data_dir, manifest.version, scored)
    import pandas as pd

    df = pd.read_parquet(path)
    summ = optliq_summary(df[df["symbol"].isin(names)])
    sys.stdout.write(
        f"{path}: {len(scored)} rows for {args.checkpoint} ({len(names)} names, "
        f"{summ['quarter'].nunique() if len(summ) else 0} quarters; "
        f"weights oi {cfg.oi_weight} volume {cfg.volume_weight}, ±{cfg.strike_range} strikes, "
        f"≤{cfg.max_dte} DTE, {cfg.volume_days} sessions)\n"
    )
    sys.stdout.write(summ.to_string(index=False) + "\n")
    return 0
