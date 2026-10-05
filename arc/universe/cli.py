"""``arc universe``: the open-universe symbol master and liquidity screen (E5.7, D28).

- ``arc universe refresh``  fetch SEC tickers ∪ Alpaca optionable assets and
  replace the local cache (what the weekly ``symbols`` routine runs).
- ``arc universe status``  cache path, age, counts, mode (never fetches).
- ``arc universe check <TICKER>...``  would the Scout admit these names?
  Master lookup + the live liquidity screen (read-only market data).
- ``arc universe tiers [--db PATH]``  D51 tiers, dedupe, active list and drops,
  resolved now from the store opened read-only (writes nothing).
"""

from __future__ import annotations

import json
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import argparse
    import datetime as _dt

    from arc.config import ArcSettings

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
    t = usub.add_parser("tiers", help="D51 tiers -> dedupe -> active list (read-only)")
    t.add_argument("--db", default=None, help="SQLite path (default: data/arc.db), read-only")
    t.add_argument(
        "--now",
        default=None,
        help="Resolve as of this ISO time (ET if naive), e.g. a past session; default now",
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
    from arc.universe.tiers import core_tickers
    from arc.utils.calendar import now_et

    settings = ArcSettings()
    cfg = load_universe_config(settings.universe_config_file)
    now = now_et()
    cmd = args.universe_command
    if cmd == "tiers":
        return _run_tiers(args, settings, now)
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
            "core": len(core_tickers(settings)),
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


def _run_tiers(args: argparse.Namespace, settings: ArcSettings, now: _dt.datetime) -> int:
    """Resolve the active list now from the store (read-only) and print every stage."""
    from arc.control.effective import effective_settings
    from arc.store.db import connect_ro
    from arc.universe.tiers import TIER_ORDER, build_active, market_reference

    if args.now:
        import datetime as dt

        from arc.utils.calendar import ET

        parsed = dt.datetime.fromisoformat(args.now)
        now = parsed.replace(tzinfo=ET) if parsed.tzinfo is None else parsed.astimezone(ET)
    try:
        conn = connect_ro(args.db)
    except FileNotFoundError as exc:
        sys.stderr.write(f"{exc}\n")
        return 1
    try:
        eff = effective_settings(conn, base=settings)
        active, inputs = build_active(conn, eff, now)
        stored = conn.execute(
            "SELECT payload, valid_from FROM context_entries WHERE kind = 'active_universe' "
            "AND status = 'active' ORDER BY valid_from DESC, rowid DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    tiers = {
        "core": inputs.core,
        "momentum": inputs.momentum,
        "trending": inputs.trending,
        "discovery": inputs.discoveries,
    }
    out = {
        "as_of": active.as_of.isoformat(),
        "config_version": eff.config_version,
        "active_max": eff.universe_active_max,
        "tiers": {k: [m.ticker for m in v] for k, v in tiers.items()},
        "raw_counts": active.raw_counts,
        "expired_tiers": [t.value for t in active.expired_tiers],
        "dedupe": {m.ticker: [t.value for t in m.also_in] for m in active.members if m.also_in},
        "active": [
            {"ticker": m.ticker, "tier": m.tier.value, "rank": m.rank, "reason": m.reason}
            for m in active.members
        ],
        "active_count": len(active.members),
        "counts": active.counts,
        "dropped": [d.model_dump(mode="json") for d in active.dropped],
        "market_reference": market_reference(eff),
        "stored_active_at": stored["valid_from"] if stored else None,
    }
    if args.json:
        _out(out, True)
        return 0
    lines = [f"as of {out['as_of']} · config v{out['config_version']} · cap {out['active_max']}"]
    for tier in TIER_ORDER:
        names = out["tiers"][tier.value]
        lines.append(f"{tier.value:<10} {len(names):>3}  {' '.join(names) or '-'}")
    if out["expired_tiers"]:
        lines.append(f"expired (read as empty): {', '.join(out['expired_tiers'])}")
    for sym, also in out["dedupe"].items():
        lines.append(f"dedupe     {sym} also in {', '.join(also)}")
    c = active.counts
    lines.append(
        f"active     {len(active.members):>3}  core {c['core']} · momentum {c['momentum']} · "
        f"trending {c['trending']} · discovery {c['discovery']}"
    )
    lines.append(f"           {' '.join(active.tickers)}")
    for d in active.dropped:
        lines.append(f"dropped    {d.ticker} ({d.tier.value}): {d.reason}")
    lines.append(f"market reference (regime only): {' '.join(out['market_reference'])}")
    sys.stdout.write("\n".join(lines) + "\n")
    return 0
