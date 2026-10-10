"""``arc store`` (E11.3 / E19.1): store identity and the D80 store reset.

    arc store identity [--db P]
    arc store reset [--db P] [--out DIR] [--dry-run | --apply --actor <owner id>]

``identity`` prints ``{env, created_at, path, running_env, match}``. Exit 0 when
the store matches ``ARC_ENV`` (or is not stamped yet), 1 on a mismatch, 2 when
the store file does not exist. Opens the store ``mode=ro``; it never stamps or
migrates.

``reset`` (D80, :mod:`arc.store.reset`) is a dry run by default: it prints the
kept/dropped rows per table, the config carry-over list and the prechecks, and
writes nothing (exit 0 when every precheck passes, 1 otherwise). ``--apply``
archives the store (+ ``arc-exp-*.db``) under ``--out`` and swaps in a clean one.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from arc.config import get_settings

if TYPE_CHECKING:
    import argparse

__all__ = ["add_store_parser", "run_store"]

_DB_HELP = "Audit DB (default: ARC_DB_PATH or the env's default store)"


def add_store_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = sub.add_parser("store", help="Audit store identity and reset (E11.3, E19.1)")
    ssub = p.add_subparsers(dest="store_command", required=True)
    i = ssub.add_parser("identity", help="Print the env stamped on the store (read-only)")
    i.add_argument("--db", default=None, help=_DB_HELP)
    r = ssub.add_parser(
        "reset",
        help="D80: archive the store (+ arm stores) and swap in a clean one (dry run by default)",
    )
    r.add_argument("--db", default=None, help=_DB_HELP)
    r.add_argument("--out", default=None, help="Archive root (default: <db dir>/archive)")
    mode = r.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Print the plan, write nothing")
    mode.add_argument("--apply", action="store_true", help="Archive and reset (needs --actor)")
    r.add_argument("--actor", default=None, help="Owner Slack id (required with --apply)")
    r.add_argument("--lock-dir", default=None, help="Lock dir to probe (default: <db dir>/locks)")
    r.add_argument("--keep-list", default=None, help="Keep list (default: config/store_reset.yaml)")
    r.add_argument("--account-last4", default=None, help="Production account last 4 (store:epoch)")
    r.add_argument("--starting-equity", type=float, default=None, help="Starting equity $")
    r.add_argument("--config", default=None, help="routines.yaml (default: config/routines.yaml)")
    r.add_argument("--json", action="store_true", help="JSON output")


def _write(text: str) -> None:
    sys.stdout.write(text + "\n")


def run_store(args: argparse.Namespace) -> int:
    if args.store_command == "reset":
        return _run_reset(args)
    return _run_identity(args)


def _run_identity(args: argparse.Namespace) -> int:
    from arc.store.db import connect_ro
    from arc.store.identity import read_store_env, store_path

    settings = get_settings()
    path = Path(store_path(settings, args.db))
    running = settings.env.value
    if not path.is_file():
        _write(json.dumps({"path": str(path), "running_env": running, "error": "store not found"}))
        return 2
    conn = connect_ro(path)
    try:
        ident = read_store_env(conn)
    finally:
        conn.close()
    match = ident is None or ident.env == running
    out = {
        "env": ident.env if ident else None,
        "created_at": ident.created_at.isoformat() if ident else None,
        "path": str(path),
        "running_env": running,
        "match": match,
    }
    _write(json.dumps(out))
    return 0 if match else 1


def _run_reset(args: argparse.Namespace) -> int:
    from arc.store.db import connect_ro
    from arc.store.identity import StoreEnvMismatchError, check_store_env, store_path
    from arc.store.reset import ResetError, apply_reset, load_keep_list, plan_reset
    from arc.utils.calendar import now_et

    settings = get_settings()
    db = Path(store_path(settings, args.db))
    if not db.is_file():
        _write(f"error: audit store not found: {db}")
        return 2
    try:
        keep = load_keep_list(args.keep_list)
    except (OSError, ValueError) as exc:
        _write(f"error: keep list: {exc}")
        return 2
    lock_dir = Path(args.lock_dir) if args.lock_dir else db.parent / "locks"
    ro = connect_ro(db)
    try:
        check_store_env(ro, settings.env, path=str(db))  # D70: never the other env's store
    except StoreEnvMismatchError as exc:
        _write(f"error: {exc}")
        return 2
    finally:
        ro.close()

    if not args.apply:
        try:
            plan = plan_reset(db, keep=keep, lock_dir=lock_dir)
        except ResetError as exc:
            _write(f"error: {exc}")
            return 2
        if args.json:
            _write(json.dumps(plan.model_dump(mode="json"), indent=2))
        else:
            _write("\n".join([*plan.lines(), "dry run: nothing written (use --apply)"]))
        return 0 if plan.ok else 1

    if not args.actor:
        _write("error: --apply needs --actor <owner slack id>")
        return 2
    out = Path(args.out) if args.out else db.parent / "archive"
    try:
        res = apply_reset(
            db,
            out=out,
            actor=args.actor,
            settings=settings,
            now=now_et(),
            keep=keep,
            lock_dir=lock_dir,
            account_last4=args.account_last4,
            starting_equity=args.starting_equity,
            routines_path=args.config,
        )
    except ResetError as exc:
        _write(f"REFUSED: {exc}")
        return 1
    if args.json:
        _write(json.dumps(res.model_dump(mode="json"), indent=2))
    else:
        _write("\n".join(res.lines()))
    return 0
