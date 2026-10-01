"""``arc tower serve|snapshot`` (E8.3, E8.7, D35): the read-only control tower.

- ``serve``     start the FastAPI + React tower (:mod:`arc.tower.api`) with uvicorn
                on the Tailscale address, port 4174 (see :mod:`arc.tower.net`).
                Refuses to start without a Tailscale address unless ``--local``
                (127.0.0.1) is given. The SPA is built by ``make web``.
- ``snapshot``  print what ``GET /api/snapshot`` would return, as JSON or text (no
                server; same loader, :func:`arc.tower.routes.snapshot.read_snapshot`).

Neither command creates, migrates or writes the DB: it is opened ``mode=ro``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    import argparse

log = structlog.get_logger(__name__)

DEFAULT_PORT = 4174
DEFAULT_REFRESH = 60


def add_tower_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = sub.add_parser("tower", help="Read-only control tower over Tailscale (E8.7, D35)")
    tsub = p.add_subparsers(dest="tower_command", required=True)

    s = tsub.add_parser("serve", help="Serve the tower on the Tailscale interface, :4174")
    _common(s)
    s.add_argument("--port", type=int, default=DEFAULT_PORT)
    s.add_argument("--address", default=None, help="Tailscale (100.64/10) or loopback IP")
    s.add_argument("--local", action="store_true", help="Bind 127.0.0.1 (this machine only)")
    s.add_argument(
        "--refresh",
        type=int,
        default=DEFAULT_REFRESH,
        help="Seconds between client re-reads: 30 | 60 (default) | 120",
    )
    s.add_argument("--print-command", action="store_true", help="Print the command, don't run")

    n = tsub.add_parser("snapshot", help="Print /api/snapshot's data (no server)")
    _common(n)
    n.add_argument("--json", action="store_true")


def _common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--db", default=None, help="SQLite path (default: ARC_DB_PATH / data/arc.db)")
    p.add_argument("--lookback-days", type=int, default=7, help="Proposal / violation window")


def _db(args: argparse.Namespace) -> Path:
    if args.db:
        return Path(args.db).expanduser().resolve()
    from arc.config import get_settings
    from arc.store.db import DEFAULT_DB_PATH

    return (get_settings().db_path or DEFAULT_DB_PATH).resolve()


def _write(text: str) -> None:
    sys.stdout.write(text + "\n")


def _serve(args: argparse.Namespace) -> int:
    from arc.tower.net import NoTailscaleAddressError, resolve_bind_address
    from arc.tower.schemas import REFRESH_CHOICES
    from arc.tower.serve import ENV_DB, ENV_LOOKBACK, ENV_REFRESH, uvicorn_command

    if args.refresh not in REFRESH_CHOICES:
        _write(f"error: --refresh must be one of {list(REFRESH_CHOICES)}")
        return 2
    db = _db(args)
    if not db.is_file():
        _write(f"error: audit store not found: {db} (the tower never creates one)")
        return 2
    try:
        address = resolve_bind_address(args.address, local=args.local)
    except NoTailscaleAddressError as exc:
        _write(f"error: {exc}")
        return 2
    argv = uvicorn_command(address, args.port)
    env = {
        **os.environ,
        ENV_DB: str(db),
        ENV_REFRESH: str(args.refresh),
        ENV_LOOKBACK: str(args.lookback_days),
    }
    if args.print_command:
        _write(" ".join(argv))
        return 0
    _write(f"arc tower: http://{address}:{args.port} (read-only, db {db})")
    log.info("tower.serve", address=address, port=args.port, db=str(db))
    return subprocess.call(argv, env=env)  # noqa: S603 - fixed argv, no shell


def _snapshot(args: argparse.Namespace) -> int:
    from arc.config import get_settings
    from arc.tower.api import TowerConfig, TowerError
    from arc.tower.data import connect_ro
    from arc.tower.routes.snapshot import read_snapshot

    db = _db(args)
    try:
        conn = connect_ro(db)
    except FileNotFoundError as exc:
        _write(f"error: {exc}")
        return 2
    cfg = TowerConfig(db_path=db, settings=get_settings(), lookback_days=args.lookback_days)
    try:
        snap = read_snapshot(cfg, conn)
    except TowerError as exc:
        _write(f"error: {exc.error}: {exc.detail}")
        return 2
    finally:
        conn.close()
    if args.json:
        _write(json.dumps(snap.model_dump(mode="json"), indent=2))
        return 0
    g, p = snap.greeks, snap.pnl
    _write(f"as of {snap.as_of:%Y-%m-%d %H:%M %Z} · {db}")
    _write(f"halted: {'YES' if snap.halted else 'no'} ({sum(h.active for h in snap.halts)} active)")
    _write(
        f"pnl: equity {p.intraday_equity or p.equity} · reconciled {p.reconciled_day} "
        f"realized {p.realized} unrealized {p.unrealized} clean {p.reconcile_clean}"
    )
    _write(
        f"greeks: {'as of ' + str(g.at) if g.at else 'none'} valued={g.valued} "
        f"Δ {g.delta} Γ {g.gamma} ν {g.vega} Θ {g.theta}"
    )
    _write(f"positions: {len(snap.structures)} open structure(s), {len(snap.legs)} broker leg(s)")
    _write(f"proposals: {len(snap.proposals)} in {args.lookback_days}d")
    _write(f"gate violations: {len(snap.violations)} {snap.violation_counts}")
    _write(f"ops: tick {snap.ops.tick_status} health {snap.ops.health_status}, "
           f"{len(snap.ops.open_alerts)} open alert(s)")  # fmt: skip
    ob = snap.ops.order_budget
    _write("orders today: " + (f"{ob.used}/{ob.limit} ({ob.tier})" if ob else "n/a"))
    return 0


def run_tower(args: argparse.Namespace) -> int:
    if args.tower_command == "serve":
        return _serve(args)
    return _snapshot(args)
