"""``arc universe``: the open-universe symbol master and liquidity screen (E5.7, D28).

- ``arc universe refresh``  fetch SEC tickers ∪ Alpaca optionable assets and
  replace the local cache (what the weekly ``symbols`` routine runs).
- ``arc universe status``  cache path, age, counts, mode (never fetches).
- ``arc universe check <TICKER>...``  would the Scout admit these names?
  Master lookup + the live liquidity screen (read-only market data).
"""

from __future__ import annotations

import json
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import argparse

__all__ = ["add_universe_parser", "run_universe"]


def add_universe_parser(sub: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    p = sub.add_parser("universe", help="Open universe: symbol master + liquidity screen (E5.7)")
    usub = p.add_subparsers(dest="universe_command", required=True)
    usub.add_parser("refresh", help="Fetch SEC + Alpaca symbols and replace the local cache")
    usub.add_parser("status", help="Symbol master cache status (never fetches)")
    c = usub.add_parser("check", help="Would the Scout admit these tickers? (live screen)")
    c.add_argument("tickers", nargs="+", metavar="TICKER")
    c.add_argument(
        "--fixture",
        action="store_true",
        help="Offline: recorded chains + the fixture symbol master (no network).",
    )
    for q in (p, *usub.choices.values()):
        q.add_argument("--json", action="store_true", help="Emit JSON")


def _out(obj: object, as_json: bool) -> None:
    if as_json:
        sys.stdout.write(json.dumps(obj, indent=2, default=str) + "\n")
    elif isinstance(obj, dict):
        sys.stdout.write("\n".join(f"{k}: {v}" for k, v in obj.items()) + "\n")
    else:
        sys.stdout.write(f"{obj}\n")


def run_universe(args: argparse.Namespace) -> int:
    from arc.config import ArcSettings
    from arc.universe import load_symbol_master, load_universe_config, refresh_symbol_master
    from arc.utils.calendar import now_et

    settings = ArcSettings()
    cfg = load_universe_config(settings.universe_config_file)
    now = now_et()
    cmd = args.universe_command
    if cmd == "refresh":
        master = refresh_symbol_master(
            cfg.symbol_master, user_agent=settings.edgar_user_agent, now=now
        )
        _out(
            {
                "path": str(cfg.symbol_master.cache_path()),
                "symbols": len(master.symbols),
                "optionable": sum(1 for s in master.symbols.values() if s.options),
                "sources": master.sources,
                "fetched_at": master.fetched_at.isoformat(),
            },
            args.json,
        )
        return 0
    if cmd == "status":
        path = cfg.symbol_master.cache_path()
        loaded = load_symbol_master(
            cfg.symbol_master, user_agent=settings.edgar_user_agent, now=now, fetch_if_missing=False
        )
        info: dict[str, object] = {"path": str(path), "mode": str(settings.universe_mode)}
        if loaded is None:
            info["status"] = "missing (run `arc universe refresh`)"
            _out(info, args.json)
            return 1
        info |= {
            "status": "stale" if loaded.is_stale(now, cfg.symbol_master.refresh_days) else "fresh",
            "age_days": round(loaded.age_days(now), 1),
            "symbols": len(loaded.symbols),
            "optionable": sum(1 for s in loaded.symbols.values() if s.options),
            "sources": loaded.sources,
            "seed": len(settings.universe),
        }
        _out(info, args.json)
        return 0
    # check
    from arc.universe.guard import UniverseGuard

    if args.fixture:
        from arc.data.recorded import MULTI_NAME_FIXTURES, RecordedMarketData
        from arc.pipeline import FIXTURE_NOW
        from arc.pipeline.env import fixture_universe_guard

        guard = fixture_universe_guard(
            settings, FIXTURE_NOW, RecordedMarketData.from_files(*MULTI_NAME_FIXTURES)
        )
    else:
        guard = UniverseGuard.from_settings(settings, now=now)
    rows = []
    for raw in args.tickers:
        t = raw.strip().upper()
        row: dict[str, object] = {"ticker": t, "seed": guard.is_seed(t)}
        if (reason := guard.known(t)) is not None:
            row |= {"admitted": False, "reject": reason, "detail": guard.details.get(t, "")}
        elif guard.is_seed(t):
            row |= {"admitted": True, "reject": None, "detail": "seed (never screened)"}
        else:
            res = guard.screen(t)
            row |= {
                "admitted": res.passed,
                "reject": None if res.passed else "illiquid",
                "detail": res.detail(),
                "metrics": res.metrics.model_dump(mode="json"),
            }
        rows.append(row)
    if args.json:
        _out(rows, True)
    else:
        for r in rows:
            mark = "ok" if r["admitted"] else str(r["reject"])
            detail = f"  {r['detail']}" if r["detail"] else ""
            sys.stdout.write(f"{r['ticker']:<6} {mark}{detail}\n")
    return 0 if all(r["admitted"] for r in rows) else 1
