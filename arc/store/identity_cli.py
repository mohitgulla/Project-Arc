"""``arc store identity`` (E11.3, D70): which env a store belongs to (read-only).

    arc store identity [--db P]

Prints ``{env, created_at, path, running_env, match}``. Exit 0 when the store
matches ``ARC_ENV`` (or is not stamped yet), 1 on a mismatch, 2 when the store
file does not exist. Opens the store ``mode=ro``; it never stamps or migrates.
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


def add_store_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = sub.add_parser("store", help="Audit store facts (E11.3 store identity)")
    ssub = p.add_subparsers(dest="store_command", required=True)
    i = ssub.add_parser("identity", help="Print the env stamped on the store (read-only)")
    i.add_argument(
        "--db", default=None, help="Audit DB (default: ARC_DB_PATH or the env's default store)"
    )


def run_store(args: argparse.Namespace) -> int:
    from arc.store.db import connect_ro
    from arc.store.identity import read_store_env, store_path

    settings = get_settings()
    path = Path(store_path(settings, args.db))
    running = settings.env.value
    if not path.is_file():
        sys.stdout.write(
            json.dumps({"path": str(path), "running_env": running, "error": "store not found"})
            + "\n"
        )
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
    sys.stdout.write(json.dumps(out) + "\n")
    return 0 if match else 1
