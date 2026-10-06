"""``arc universe``: the open-universe symbol master and liquidity screen (E5.7, D28).

- ``arc universe refresh``  fetch SEC tickers ∪ Alpaca optionable assets and
  replace the local cache (what the weekly ``symbols`` routine runs).
- ``arc universe status``  cache path, age, counts, mode (never fetches).
- ``arc universe check <TICKER>...``  would the Scalp admit these names?
  Master lookup + the live liquidity screen (read-only market data).
- ``arc universe tiers [--db PATH] [--model d51|d56]``  tiers, dedupe, active list and drops,
  resolved now from the store opened read-only (writes nothing).
- ``arc universe momentum [--dry-run] [--db PATH] [--no-slack]``  E12.2: run the
  ``universe.momentum`` routine now (same as ``arc routines run universe.momentum``)
  and print the momentum tier it wrote; ``--dry-run`` fetches and prints only.
- ``arc universe trending [--dry-run] [--no-screen] [--top N] [--db PATH] [--no-slack]``
  E12.3: run the ``universe.trending`` routine now and print the tier it wrote;
  ``--dry-run`` reads the store read-only, prints the per-input score table and the
  tier, and writes nothing (``--no-screen`` also skips the Alpaca liquidity screen).
"""

from __future__ import annotations

import json
import sys
from typing import TYPE_CHECKING, Any

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
    c = usub.add_parser("check", help="Would the Scalp admit these tickers? (live screen)")
    c.add_argument("tickers", nargs="+", metavar="TICKER")
    c.add_argument(
        "--fixture",
        action="store_true",
        help="Offline: recorded chains + the fixture symbol master (no network).",
    )
    c.add_argument(
        "--profile",
        choices=["strict", "standard", "relaxed", "loose"],
        default=None,
        help=(
            "Screen every ticker with this liquidity profile, core/momentum included "
            "(default: each name's tier profile; core + momentum are never screened)"
        ),
    )
    c.add_argument("--db", default=None, help="SQLite path for tier membership, read-only")
    t = usub.add_parser("tiers", help="Tiers -> dedupe -> active list (read-only)")
    t.add_argument("--db", default=None, help="SQLite path (default: data/arc.db), read-only")
    t.add_argument(
        "--now",
        default=None,
        help="Resolve as of this ISO time (ET if naive), e.g. a past session; default now",
    )
    t.add_argument(
        "--model",
        choices=["d51", "d56"],
        default=None,
        help="Preview under this tier layout (default: config/universe.yaml tiers.model)",
    )
    m = usub.add_parser("momentum", help="E12.2: run the monthly momentum tier job now")
    m.add_argument("--dry-run", action="store_true", help="Fetch and print only; write nothing")
    m.add_argument(
        "--source",
        choices=["stockanalysis", "schwab"],
        default=None,
        help="--dry-run only: try just this source (default: the job's source_order)",
    )
    m.add_argument("--db", default=None, help="SQLite path (default: data/arc.db)")
    m.add_argument("--no-slack", action="store_true", help="Notice to the log only")
    tr = usub.add_parser("trending", help="E12.3: run the daily trending tier job now")
    tr.add_argument("--dry-run", action="store_true", help="Score + print only; write nothing")
    tr.add_argument(
        "--no-screen", action="store_true", help="--dry-run only: skip the liquidity screen"
    )
    tr.add_argument("--top", type=int, default=40, help="Rows of the score table (dry run)")
    tr.add_argument("--db", default=None, help="SQLite path (default: data/arc.db)")
    tr.add_argument("--no-slack", action="store_true", help="Notice to the log only")
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
    from arc.universe import load_symbol_master, refresh_symbol_master
    from arc.universe.config import universe_config
    from arc.universe.guard import REJECT_NOT_IN_TIER
    from arc.universe.tiers import core_tickers
    from arc.utils.calendar import now_et

    settings = ArcSettings()
    cfg = universe_config(settings)
    now = now_et()
    cmd = args.universe_command
    if cmd == "tiers":
        return _run_tiers(args, settings, now)
    if cmd == "momentum":
        return _run_momentum(args, settings, now)
    if cmd == "trending":
        return _run_trending(args, settings, now)
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

    if args.fixture:
        from arc.data.recorded import MULTI_NAME_FIXTURES, RecordedMarketData
        from arc.pipeline import FIXTURE_NOW
        from arc.pipeline.env import fixture_universe_guard

        guard = fixture_universe_guard(
            settings, FIXTURE_NOW, RecordedMarketData.from_files(*MULTI_NAME_FIXTURES)
        )
    else:
        guard = _live_guard(args, settings, now)
    rows = []
    for raw in args.tickers:
        t = raw.strip().upper()
        tier = guard.tier_label(t)
        row: dict[str, object] = {"ticker": t, "seed": guard.is_seed(t), "tier": tier}
        reason = guard.known(t)
        # D56: a name in no tier is never admitted, but a --profile what-if still screens it
        out_of_tier = reason == REJECT_NOT_IN_TIER
        if out_of_tier and args.profile is not None:
            reason = None
        if reason is not None:
            row |= {"admitted": False, "reject": reason, "detail": guard.details.get(t, "")}
        elif guard.is_seed(t) and args.profile is None:
            row |= {"admitted": True, "reject": None, "detail": f"{tier} (never screened)"}
        else:
            profile = args.profile or guard.screen_profile(t)
            res = guard.screen(t, profile)
            seed_note = f" ({tier}: admitted unscreened)" if guard.is_seed(t) else ""
            if out_of_tier:
                seed_note = " (in no tier: mentioned, never admitted)"
            admitted = (res.passed or guard.is_seed(t)) and not out_of_tier
            row |= {
                "profile": profile,
                "screen_passed": res.passed,
                "admitted": admitted,
                "reject": None if admitted else (REJECT_NOT_IN_TIER if out_of_tier else "illiquid"),
                "detail": f"{profile}: {res.detail()}{seed_note}",
                "metrics": res.metrics.model_dump(mode="json"),
            }
        rows.append(row)
    if args.json:
        _out(rows, True)
    else:
        for r in rows:
            mark = "ok" if r["admitted"] else str(r["reject"])
            detail = f"  {r['detail']}" if r["detail"] else ""
            m = r.get("metrics")
            if isinstance(m, dict) and not m.get("error"):
                detail += "  [" + _metrics_line(m) + "]"
            sys.stdout.write(f"{r['ticker']:<6} {r['tier']:<9} {mark}{detail}\n")
    if args.profile is not None:  # a what-if screen: exit on the screen result
        return 0 if all(r.get("screen_passed", r["admitted"]) for r in rows) else 1
    return 0 if all(r["admitted"] for r in rows) else 1


def _metrics_line(m: dict[str, object]) -> str:
    def num(k: str, fmt: str) -> str:
        v = m.get(k)
        return "-" if v is None else format(v, fmt)

    return (
        f"px {num('price', '.2f')} · ADV {num('adv_shares', ',.0f')} · "
        f"OI {num('atm_open_interest', 'd')} · spread {num('atm_spread_pct', '.1%')} · "
        f"expiries {m.get('expiries_in_window', 0)}"
    )


def _live_guard(args: argparse.Namespace, settings: ArcSettings, now: _dt.datetime) -> Any:
    """Live guard with tier membership from the store (read-only) when it exists."""
    from arc.control.effective import effective_settings
    from arc.store.db import connect_ro
    from arc.universe.guard import UniverseGuard

    try:
        conn = connect_ro(args.db)
    except FileNotFoundError:
        return UniverseGuard.from_settings(settings, now=now)
    try:
        eff = effective_settings(conn, base=settings)
        return UniverseGuard.from_settings(eff, now=now, conn=conn)
    finally:
        conn.close()


def _run_tiers(args: argparse.Namespace, settings: ArcSettings, now: _dt.datetime) -> int:
    """Resolve the active list now from the store (read-only) and print every stage."""
    from arc.control.effective import effective_settings
    from arc.store.db import connect_ro
    from arc.universe.tiers import build_active, market_reference, tier_order

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
        active, inputs = build_active(conn, eff, now, model=args.model)
        stored = conn.execute(
            "SELECT payload, valid_from FROM context_entries WHERE kind = 'active_universe' "
            "AND status = 'active' ORDER BY valid_from DESC, rowid DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    feeds = {
        "core": inputs.core,
        "momentum": inputs.momentum,
        "trending": inputs.trending,
        "discovery": inputs.discoveries,
    }
    order = tier_order(active.model)
    tiers = {t.value: feeds[t.value] for t in order}
    reference = market_reference(eff, model=active.model)
    out = {
        "model": active.model,
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
        "market_reference": reference,
        "stored_active_at": stored["valid_from"] if stored else None,
    }
    if args.json:
        _out(out, True)
        return 0
    lines = [
        f"as of {out['as_of']} · model {out['model']} · config v{out['config_version']} · "
        f"cap {out['active_max']}"
    ]
    for tier in order:
        names = out["tiers"][tier.value]
        lines.append(f"{tier.value:<10} {len(names):>3}  {' '.join(names) or '-'}")
    if out["expired_tiers"]:
        lines.append(f"expired (read as empty): {', '.join(out['expired_tiers'])}")
    for sym, also in out["dedupe"].items():
        lines.append(f"dedupe     {sym} also in {', '.join(also)}")
    c = active.counts
    lines.append(
        f"active     {len(active.members):>3}  "
        + " · ".join(f"{t.value} {c.get(t.value, 0)}" for t in order)
    )
    lines.append(f"           {' '.join(active.tickers)}")
    for d in active.dropped:
        rank = f" #{d.rank}" if d.rank is not None else ""
        lines.append(f"dropped    {d.ticker} ({d.tier.value}{rank}): {d.reason}")
    lines.append(f"market reference (regime only): {' '.join(out['market_reference'])}")
    sys.stdout.write("\n".join(lines) + "\n")
    return 0


def _run_momentum(args: argparse.Namespace, settings: ArcSettings, now: _dt.datetime) -> int:
    """``--dry-run``: fetch + select and print (writes nothing). Otherwise run the
    ``universe.momentum`` routine (run row, manifest, notice) and print what it wrote."""
    if not args.dry_run:
        if args.source:
            _out({"error": "--source is only valid with --dry-run"}, args.json)
            return 2
        return _run_momentum_job(args)
    from arc.routines.config import load_routines
    from arc.routines.handlers import run_momentum
    from arc.universe.momentum import MomentumError

    options: dict[str, object] = {}
    job = load_routines().jobs().get("universe.momentum")
    if job is not None:
        options = job[1].options
    if args.source:
        options["source_order"] = [args.source]
    try:
        fetch = run_momentum(settings=settings, options=options, now=now)
    except MomentumError as exc:
        _out({"error": str(exc)}, args.json)
        return 1
    _print_tier(
        {
            "written": False,
            "source": fetch.source,
            "url": fetch.url,
            "as_of": fetch.as_of.isoformat() if fetch.as_of else None,
            "rows": len(fetch.rows),
            "names": len(fetch.picks),
            "partial": fetch.partial,
            "members": [
                {"rank": i, "ticker": p.symbol, "weight": p.weight, "merged": list(p.merged)}
                for i, p in enumerate(fetch.picks, 1)
            ],
            "dropped": [f"{s}:{r}" for s, r in fetch.dropped],
            "source_errors": fetch.errors,
        },
        args.json,
    )
    return 0


def _run_momentum_job(args: argparse.Namespace) -> int:
    import argparse as _argparse

    from arc.routines.cli import DEFAULT_LOCK_DIR, _conn, _dispatcher, _outcome_json
    from arc.universe.tiers import UniverseTierPayload
    from arc.utils.calendar import now_et

    rargs = _argparse.Namespace(
        db=args.db, config=None, now=None, no_slack=args.no_slack, lock_dir=str(DEFAULT_LOCK_DIR)
    )
    conn = _conn(rargs)
    outcomes = _dispatcher(rargs, conn).run_manual("universe.momentum", now=now_et())
    out = outcomes[-1] if outcomes else None
    info: dict[str, object] = {"run": _outcome_json(out) if out else None}
    row = conn.execute(
        "SELECT payload FROM context_entries WHERE kind = 'universe_tier' AND subject = "
        "'momentum' ORDER BY valid_from DESC, rowid DESC LIMIT 1"
    ).fetchone()
    if out is not None and out.status == "ok" and row is not None:
        pay = UniverseTierPayload.model_validate_json(row[0])
        info |= {
            "written": True,
            "source": pay.source,
            "url": pay.url,
            "as_of": pay.source_as_of.isoformat() if pay.source_as_of else None,
            "names": len(pay.members),
            "partial": pay.partial,
            "members": [
                {"rank": m.rank, "ticker": m.ticker, "reason": m.reason} for m in pay.members
            ],
            **{k: out.metrics.get(k) for k in ("added", "removed", "stale", "dropped", "active")},
        }
    _print_tier(info, args.json)
    return 0 if out is not None and out.status == "ok" else 1


def _run_trending(args: argparse.Namespace, settings: ArcSettings, now: _dt.datetime) -> int:
    """``--dry-run``: gather + rank (+ screen) from the store read-only and print the
    score table (writes nothing). Otherwise run the ``universe.trending`` routine."""
    if not args.dry_run:
        if args.no_screen:
            _out({"error": "--no-screen is only valid with --dry-run"}, args.json)
            return 2
        return _run_tier_job(args, "universe.trending", "trending")
    from arc.control.effective import effective_settings
    from arc.routines.config import load_routines
    from arc.routines.handlers import run_trending_tier
    from arc.store.db import connect_ro
    from arc.universe.trending import TrendingError, table

    routines = load_routines()
    job = routines.jobs().get("universe.trending")
    options: dict[str, object] = dict(job[1].options) if job is not None else {}
    try:
        conn = connect_ro(args.db)
    except FileNotFoundError as exc:
        sys.stderr.write(f"{exc}\n")
        return 1
    try:
        eff = effective_settings(conn, base=settings)
        res = run_trending_tier(
            conn=conn,
            settings=eff,
            routines=routines,
            options=options,
            now=now,
            screen=not args.no_screen,
        )
    except TrendingError as exc:
        _out({"error": str(exc)}, args.json)
        return 1
    finally:
        conn.close()
    info: dict[str, object] = {
        "written": False,
        "as_of": res.as_of.isoformat(),
        "screened": res.screened,
        "inputs": {
            i.name: f"{i.status} · {len(i.raw)} names" + (f" · {i.error}" if i.error else "")
            for i in res.inputs
        },
        "ranked": len(res.ranked),
        "single_input": len(res.single_input),
        "excluded (core/momentum/reference)": len(res.excluded),
        "names": len(res.members),
        "tickers": " ".join(res.tickers),
    }
    rows = table(res, limit=args.top)
    if args.json:
        _out({**info, "table": rows}, True)
        return 0
    _out(info, False)
    head = f"{'#':>3} {'ticker':<6} " + " ".join(f"{n[:10]:>10}" for n in res.order)
    sys.stdout.write(head + f" {'score':>6} {'n':>2} {'screen':<6} reason\n")
    for r in rows:
        cells = " ".join(f"{'-' if r[n] is None else format(r[n], '.2f'):>10}" for n in res.order)
        rank = r["rank"] if r["rank"] is not None else ""
        sys.stdout.write(
            f"{rank!s:>3} {r['ticker']:<6} {cells} {r['trend_score']:>6.3f} {r['inputs']:>2} "
            f"{r['screen']:<6} {r['reason']}"
            + (f" [{r['screen_detail']}]" if r["screen"] == "fail" else "")
            + "\n"
        )
    return 0


def _run_tier_job(args: argparse.Namespace, job: str, subject: str) -> int:
    """Run *job* through the dispatcher and print the ``universe_tier`` it wrote."""
    import argparse as _argparse

    from arc.routines.cli import DEFAULT_LOCK_DIR, _conn, _dispatcher, _outcome_json
    from arc.universe.tiers import UniverseTierPayload
    from arc.utils.calendar import now_et

    rargs = _argparse.Namespace(
        db=args.db, config=None, now=None, no_slack=args.no_slack, lock_dir=str(DEFAULT_LOCK_DIR)
    )
    conn = _conn(rargs)
    outcomes = _dispatcher(rargs, conn).run_manual(job, now=now_et())
    out = outcomes[-1] if outcomes else None
    info: dict[str, object] = {"run": _outcome_json(out) if out else None}
    row = conn.execute(
        "SELECT payload FROM context_entries WHERE kind = 'universe_tier' AND subject = ? "
        "ORDER BY valid_from DESC, rowid DESC LIMIT 1",
        (subject,),
    ).fetchone()
    if out is not None and out.status == "ok" and row is not None:
        pay = UniverseTierPayload.model_validate_json(row[0])
        info |= {
            "written": True,
            "source": pay.source,
            "as_of": pay.source_as_of.isoformat() if pay.source_as_of else None,
            "names": len(pay.members),
            "partial": pay.partial,
            "members": [
                {"rank": m.rank, "ticker": m.ticker, "reason": m.reason} for m in pay.members
            ],
            **{k: out.metrics.get(k) for k in ("added", "removed", "input_errors", "active")},
        }
    _print_tier(info, args.json)
    return 0 if out is not None and out.status == "ok" else 1


def _print_tier(info: dict[str, object], as_json: bool) -> None:
    if as_json:
        _out(info, True)
        return
    _out({k: v for k, v in info.items() if k != "members"}, False)
    for m in info.get("members") or []:  # type: ignore[union-attr]
        detail = (
            f"{m['weight']:>6.2f}%" + (f"  (incl. {'+'.join(m['merged'])})" if m["merged"] else "")
            if "weight" in m
            else str(m.get("reason", ""))
        )
        sys.stdout.write(f"{m['rank']:>3} {m['ticker']:<6} {detail}\n")
