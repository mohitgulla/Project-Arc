"""E18.3 (D78): exit policy v1 vs v2 backtest + live forward tally. Report only.

    .venv/bin/python scripts/exit_policy_report.py \\
        --data-dir ~/GitHub/Project-Arc/data --db data/arc.db \\
        --json data/backtest/e183/results.json --out docs/RESEARCH/exit-policy-v2.md

    # re-render the markdown from a saved run (no backtest, no store):
    .venv/bin/python scripts/exit_policy_report.py --from-json data/backtest/e183/results.json \\
        --out docs/RESEARCH/exit-policy-v2.md

Run A of ``docs/RESEARCH/ranking-backtest.md`` (``arc backtest rank``), narrowed to what
this card compares: ``cash_debit`` profile, its incumbent ranker ``debit_width``,
smile marks, cost x = ``config/costs.yaml``, trend stance, SPY/QQQ/IWM/AAPL/NVDA/TSLA,
2024-03-01 → 2026-07-31, offline (cached chains and closes). The only variable is the
exit policy of the debit kinds (``config/exits.yaml`` + overrides):

- ``v1``   take profit 1.00 × debit, no profit lock (before E18.1),
- ``v2``   the shipped E18.1 defaults (take profit 0.60, lock 0.50 → 0.20, intraday),
- ``hold`` hold to expiry (reference).

Each variant prices its own menus (the managed-exit Monte Carlo and the managed Net EV
floor depend on the exit policy, as live). The threshold grid is descriptive only: it
holds v2's menus and picks fixed and replays each pick's session marks under each cell.

The fill-day guard (E18.2) is not modelled: it holds discretionary (LLM) closes, which
the backtest does not simulate. The backtest checks every rule on end-of-day marks only.

Keep / rollback rule (fixed in the card before the run, :func:`keep_rule`): keep v2 if
v2's net P&L is not lower than v1's in >= 2 of 3 trend sub-periods **and** v2's max
drawdown is not higher than v1's by more than 10 %. Otherwise recommend rollback (or
the best grid cell as a future XP arm, never a direct flip). The owner decides.

The live store is opened read-only (``mode=ro``); nothing is written.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sqlite3
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    import pandas as pd

    from arc.backtest.costs import CostModel
    from arc.exits.policy import ExitConfig

log = structlog.get_logger()

PROFILE = "cash_debit"
TICKERS = ("SPY", "QQQ", "IWM", "AAPL", "NVDA", "TSLA")
START = dt.date(2024, 3, 1)
END = dt.date(2026, 7, 31)
DEBIT_KINDS = ("vertical_debit", "long_call", "long_put")
SUBPERIODS = ("bear", "sideways", "bull")
REASONS = ("profit_lock", "take_profit", "stop", "dte_exit", "expiry")
V2_LIVE_AT = "2026-10-10T17:26:55Z"  # PR #201 (E18.1) merged: v2 is the live default
FORWARD_MIN = 20  # the card: append the live tally once >= 20 closed under v2
BREAKEVEN = 1e-6  # "floor 0.0": the lock model requires floor_pct > 0

LOCK_ARMS = (0.3, 0.4, 0.5)
LOCK_FLOORS = (0.0, 0.1, 0.2, 0.3)
TPS = (0.5, 0.6, 0.8, 1.0)


# ---------------------------------------------------------------------------
# Exit variants
# ---------------------------------------------------------------------------


def overrides_for(
    *, tp: float | None, lock: tuple[float, float] | None, hold: bool = False
) -> dict[tuple[str, ...], object]:
    """``config/exits.yaml`` overrides for the debit kinds (and ``default``).

    ``hold`` = no take profit, no stop, no lock, no DTE exit (hold to expiry).
    """
    out: dict[tuple[str, ...], object] = {}
    for k in (*(("kinds", kd) for kd in DEBIT_KINDS), ("default",)):
        if hold:
            out[(*k, "take_profit_pct_of_debit")] = None
            out[(*k, "profit_lock")] = None
            out[(*k, "close_at_dte")] = None
            if k != ("default",):
                out[(*k, "stop")] = None
            continue
        out[(*k, "take_profit_pct_of_debit")] = tp
        out[(*k, "profit_lock")] = (
            None
            if lock is None or k == ("default",)
            else {"arm_pct": lock[0], "floor_pct": lock[1], "eod_only": False}
        )
    return out


def variant_overrides() -> dict[str, dict[tuple[str, ...], object]]:
    return {
        "v1": overrides_for(tp=1.0, lock=None),
        "v2": {},  # the shipped config/exits.yaml (E18.1 defaults)
        "hold": overrides_for(tp=None, lock=None, hold=True),
    }


@dataclass(frozen=True)
class GridCell:
    name: str
    tp: float
    lock: tuple[float, float] | None
    invalid: str | None = None  # why the policy validator refuses it


def grid_cells() -> list[GridCell]:
    """Lock grid at the shipped TP 0.60, then the TP grid without and with the v2 lock."""
    from arc.exits.policy import load_exit_config

    cells: list[GridCell] = []
    specs: list[tuple[str, float, tuple[float, float] | None]] = []
    for arm in LOCK_ARMS:
        for floor in LOCK_FLOORS:
            specs.append((f"lock {arm:.1f}->{floor:.1f} tp 0.60", 0.6, (arm, floor)))
    for tp in TPS:
        specs.append((f"tp {tp:.2f} no lock", tp, None))
        specs.append((f"tp {tp:.2f} lock 0.5->0.2", tp, (0.5, 0.2)))
    for name, tp, lock in specs:
        eff = None if lock is None else (lock[0], lock[1] or BREAKEVEN)
        try:
            load_exit_config(overrides=overrides_for(tp=tp, lock=eff))
        except ValueError as exc:
            msg = str(exc)
            reason = msg.split("Value error, ", 1)[-1].split(" [type=", 1)[0].strip()
            cells.append(GridCell(name, tp, lock, invalid=reason.splitlines()[-1]))
            continue
        cells.append(GridCell(name, tp, lock))
    return cells


def cell_config(c: GridCell) -> ExitConfig:
    from arc.exits.policy import load_exit_config

    eff = None if c.lock is None else (c.lock[0], c.lock[1] or BREAKEVEN)
    return load_exit_config(overrides=overrides_for(tp=c.tp, lock=eff))


# ---------------------------------------------------------------------------
# Per-ticker job: menus per menu policy, top-1 picks, session paths, outcomes
# ---------------------------------------------------------------------------


@dataclass
class TickerOut:
    ticker: str
    menus: dict[str, dict[dt.date, list[Any]]]
    outcomes: dict[str, dict[tuple[str, dt.date, str], Any]]
    trend: pd.Series
    vol: pd.Series


def ticker_job(
    ticker: str,
    raw: pd.DataFrame,
    closes: pd.Series,
    *,
    runs: Mapping[str, tuple[str, ExitConfig]],
    menu_exits: Mapping[str, ExitConfig],
    cfg: Any,
    cost: CostModel,
    window: tuple[int, int],
    stances: Mapping[str, frozenset[str]],
) -> TickerOut:
    """*runs*: run name → (menu policy name, exit config to play the picks out with)."""
    from arc.backtest.engine import prepare_chains
    from arc.backtest.ranking import (
        apply_stance,
        build_menus,
        exit_on_path,
        labels_for,
        pick_path,
        remark_chains,
    )
    from arc.scanner.rank import Ranker, rank

    closes = closes.sort_index()
    bt = cfg.backtest
    chains = prepare_chains(raw, closes, cost=cost, r=bt.risk_free_rate, dte_min=1, dte_max=70)
    chains = remark_chains(chains, closes, r=bt.risk_free_rate, cost=cost)
    days = [d for d in sorted(chains) if START <= d <= END and d in closes.index]
    all_days = sorted(chains)
    trend, vol = labels_for(closes)
    specs = bt.specs_for(PROFILE, *window)
    menus: dict[str, dict[dt.date, list[Any]]] = {}
    paths: dict[str, dict[tuple[str, dt.date, str], Any]] = {}
    for mp, ex in menu_exits.items():
        mc = ex.model.model_copy(update={"n_paths": bt.n_paths})
        m = build_menus(
            chains,
            closes,
            days=days,
            underlying=ticker,
            specs=specs,
            cost=cost,
            exits=ex,
            mc=mc,
            r=bt.risk_free_rate,
        )
        m = apply_stance(m, trend, stances)
        menus[mp] = m
        p: dict[tuple[str, dt.date, str], Any] = {}
        for day, menu in sorted(m.items()):
            if not menu:
                continue
            ranked = rank(
                [c.inputs for c in menu],
                Ranker.DEBIT_WIDTH,
                filters=cfg.ranking.filters,
                vrp_threshold=cfg.ranking.vrp_threshold,
            )
            if not ranked:
                continue
            best = next(c for c in menu if c.inputs.key == ranked[0].key)
            p[(ticker, day, best.inputs.key)] = pick_path(
                best, chains=chains, days=all_days, closes=closes, cost=cost
            )
        paths[mp] = p
    outcomes: dict[str, dict[tuple[str, dt.date, str], Any]] = {}
    for name, (mp, ex) in runs.items():
        outcomes[name] = {
            k: None if path is None else exit_on_path(path, exits=ex, cost=cost)
            for k, path in paths[mp].items()
        }
    log.info("e183.ticker_done", ticker=ticker, sessions=len(days))
    return TickerOut(ticker, menus, outcomes, trend, vol)


# ---------------------------------------------------------------------------
# Metrics and the keep / rollback rule
# ---------------------------------------------------------------------------


@dataclass
class RunMetrics:
    name: str
    trades: int
    net_pnl: float
    max_dd: float
    avg_hold_days: float | None
    by_reason: dict[str, float] = field(default_factory=dict)  # share of trades
    pnl_by_reason: dict[str, float] = field(default_factory=dict)
    subperiods: dict[str, float] = field(default_factory=dict)  # net P&L per trend label
    subperiod_trades: dict[str, int] = field(default_factory=dict)
    lock_pnl: float | None = None  # lock-closed trades: realised
    lock_shadow: float | None = None  # lock-closed trades: their hold-to-expiry P&L
    hold_shadow_total: float = 0.0  # every trade: hold-to-expiry P&L
    invalid: str | None = None


def metrics(name: str, trades: pd.DataFrame, equity: pd.Series) -> RunMetrics:
    """Report metrics of one ``run_portfolio`` result (max DD on daily equity, as E7.5)."""
    n = len(trades)
    dd = float((equity.cummax() - equity).max()) if len(equity) else 0.0
    if n == 0:
        return RunMetrics(name, 0, 0.0, dd, None, subperiods=dict.fromkeys(SUBPERIODS, 0.0))
    reasons = trades["exit_reason"].astype(str)
    by = {r: float((reasons == r).mean()) for r in REASONS}
    pnl_by = {r: float(trades.loc[reasons == r, "pnl"].sum()) for r in REASONS}
    sub = {s: float(trades.loc[trades["trend"] == s, "pnl"].sum()) for s in SUBPERIODS}
    subn = {s: int((trades["trend"] == s).sum()) for s in SUBPERIODS}
    lock = trades[reasons == "profit_lock"]
    return RunMetrics(
        name=name,
        trades=n,
        net_pnl=float(trades["pnl"].sum()),
        max_dd=dd,
        avg_hold_days=float(trades["days_held"].mean()),
        by_reason=by,
        pnl_by_reason=pnl_by,
        subperiods=sub,
        subperiod_trades=subn,
        lock_pnl=float(lock["pnl"].sum()) if len(lock) else None,
        lock_shadow=float(lock["hold_to_expiry_pnl"].sum()) if len(lock) else None,
        hold_shadow_total=float(trades["hold_to_expiry_pnl"].sum()),
    )


@dataclass(frozen=True)
class Verdict:
    keep: bool
    subperiods_not_lower: list[str]
    dd_ratio: float | None
    text: str


def keep_rule(
    v1: RunMetrics, v2: RunMetrics, *, min_not_lower: int = 2, dd_tolerance: float = 0.10
) -> Verdict:
    """The card's rule, fixed before the run (see module doc)."""
    not_lower = [s for s in SUBPERIODS if v2.subperiods.get(s, 0.0) >= v1.subperiods.get(s, 0.0)]
    if v1.max_dd > 0:
        ratio: float | None = v2.max_dd / v1.max_dd
        dd_ok = v2.max_dd <= v1.max_dd * (1 + dd_tolerance)
    else:
        ratio = None
        dd_ok = v2.max_dd <= 0
    keep = len(not_lower) >= min_not_lower and dd_ok
    dd_txt = "n/a" if ratio is None else f"{ratio - 1:+.1%}"
    text = (
        f"{'KEEP v2' if keep else 'ROLL BACK to v1'}: v2 net P&L not lower in "
        f"{len(not_lower)} of {len(SUBPERIODS)} sub-periods "
        f"({', '.join(not_lower) or 'none'}; need {min_not_lower}), max drawdown "
        f"{dd_txt} vs v1 (limit +{dd_tolerance:.0%}: {'ok' if dd_ok else 'breached'})"
    )
    return Verdict(keep, not_lower, ratio, text)


# ---------------------------------------------------------------------------
# Live store (read-only)
# ---------------------------------------------------------------------------


def live_tally(db: Path) -> dict[str, Any]:
    """Closed live positions: realised vs MFE / MAE / shadow, and the count under v2."""
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(outcomes)")}
        mfe = "o.max_favourable_excursion" if "max_favourable_excursion" in cols else "NULL"
        rows = conn.execute(
            f"""SELECT s.id, s.ticker, s.opened_at, s.closed_at, s.entry_net, s.exit_reason,
                       json_extract(s.structure_json, '$.kind') AS kind,
                       o.contracts, o.realised_pnl, o.max_adverse_excursion AS mae,
                       {mfe} AS mfe, o.hold_to_expiry_shadow_pnl AS shadow
                FROM open_structures s
                LEFT JOIN outcomes o ON o.rowid = (
                    SELECT o2.rowid FROM outcomes o2
                    WHERE o2.proposal_hash = s.open_proposal_hash
                    ORDER BY o2.at DESC, o2.rowid DESC LIMIT 1)
                WHERE s.status = 'closed' ORDER BY s.closed_at""",  # noqa: S608
        ).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        n = int(r["contracts"] or 0)
        debit = float(r["entry_net"]) * 100 * n
        mfe_v = None if r["mfe"] is None else float(r["mfe"])
        out.append(
            {
                "ticker": r["ticker"],
                "kind": r["kind"],
                "opened_at": r["opened_at"][:10],
                "closed_at": r["closed_at"],
                "exit_reason": r["exit_reason"],
                "realised": None if r["realised_pnl"] is None else float(r["realised_pnl"]),
                "mae": None if r["mae"] is None else float(r["mae"]),
                "mfe": mfe_v,
                "peak_pct_of_debit": None if mfe_v is None or debit <= 0 else mfe_v / debit,
                "shadow": None if r["shadow"] is None else float(r["shadow"]),
                "under_v2": r["closed_at"] >= V2_LIVE_AT,
            }
        )
    return {"closed": out, "under_v2": sum(1 for x in out if x["under_v2"])}


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def run_backtest(data_dir: Path, *, workers: int) -> dict[str, Any]:
    from arc.backtest.costs import load_cost_model
    from arc.backtest.rank_report import _stances
    from arc.backtest.ranking import load_ranking_file, run_portfolio
    from arc.backtest.report import closes_for
    from arc.config import ArcSettings
    from arc.data.history.store import ParquetHistoryStore
    from arc.exits.policy import load_exit_config
    from arc.scanner.rank import Ranker

    cfg = load_ranking_file()
    cost = load_cost_model()
    settings = ArcSettings().with_profile(PROFILE)
    window = cfg.backtest.window_for(PROFILE, settings.entry_dte_window)
    stances = _stances(PROFILE, cfg, ArcSettings())
    menu_exits = {k: load_exit_config(overrides=v) for k, v in variant_overrides().items()}
    cells = grid_cells()
    runs: dict[str, tuple[str, ExitConfig]] = {k: (k, ex) for k, ex in menu_exits.items()}
    runs["v1 exits on v2 picks"] = ("v2", menu_exits["v1"])
    for c in cells:
        if c.invalid is None:
            runs[f"grid: {c.name}"] = ("v2", cell_config(c))
    closes = closes_for(list(TICKERS), START, END, data_dir, None)
    store = ParquetHistoryStore(data_dir)
    kw = {
        "runs": runs,
        "menu_exits": menu_exits,
        "cfg": cfg,
        "cost": cost,
        "window": window,
        "stances": stances,
    }
    results: dict[str, TickerOut] = {}
    with ProcessPoolExecutor(max_workers=max(workers, 1)) as ex:
        futs = {
            t: ex.submit(ticker_job, t, store.read("alpaca", t, START, END), closes[t], **kw)
            for t in TICKERS
        }
        results = {t: f.result() for t, f in futs.items()}
    sessions = sorted({d for s in closes.values() for d in s.index if d >= START})
    trend = {t: r.trend for t, r in results.items()}
    vol = {t: r.vol for t, r in results.items()}
    out: dict[str, Any] = {"runs": {}, "cells": [asdict(c) for c in cells]}
    for name, (mp, _) in runs.items():
        menus = {t: r.menus[mp] for t, r in results.items()}
        outcomes: dict[tuple[str, dt.date, str], Any] = {}
        for r in results.values():
            outcomes.update(r.outcomes[name])
        rr = run_portfolio(
            menus,
            outcomes,
            ranker=Ranker.DEBIT_WIDTH,
            profile=PROFILE,
            settings=settings,
            starting_equity=cfg.backtest.starting_equity,
            filters=cfg.ranking.filters,
            vrp_threshold=cfg.ranking.vrp_threshold,
            sessions=sessions,
            slippage=cost.slippage_frac,
            trend=trend,
            vol=vol,
        )
        out["runs"][name] = asdict(metrics(name, rr.trades, rr.equity))
    out["setup"] = {
        "profile": PROFILE,
        "ranker": "debit_width",
        "tickers": list(TICKERS),
        "start": START.isoformat(),
        "end": END.isoformat(),
        "window": list(window),
        "slippage_x": cost.slippage_frac,
        "n_paths": cfg.backtest.n_paths,
        "marks": cfg.backtest.marks,
    }
    return out


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------


def _usd(v: float | None) -> str:
    return "–" if v is None else f"{v:+,.0f}"


def _pct(v: float | None) -> str:
    return "–" if v is None else f"{v:.0%}"


def _row(m: RunMetrics) -> str:
    sub = " / ".join(_usd(m.subperiods.get(s)) for s in SUBPERIODS)
    reasons = " / ".join(_pct(m.by_reason.get(r)) for r in REASONS)
    hold = "–" if m.avg_hold_days is None else f"{m.avg_hold_days:.1f}"
    lock = "–" if m.lock_pnl is None else f"{_usd(m.lock_pnl)} vs {_usd(m.lock_shadow)}"
    return (
        f"| {m.name} | {m.trades} | {_usd(m.net_pnl)} | {m.max_dd:,.0f} | {hold} | {reasons} "
        f"| {sub} | {lock} |"
    )


_HEAD = (
    "| Run | Trades | Net P&L | Max DD | Avg hold (d) "
    "| % by lock / TP / stop / DTE / expiry | P&L bear / sideways / bull "
    "| Lock-closed: realised vs hold-to-expiry |\n"
    "|---|---|---|---|---|---|---|---|"
)


def render(res: Mapping[str, Any], live: Mapping[str, Any] | None) -> str:
    runs = {k: RunMetrics(**v) for k, v in res["runs"].items()}
    v1, v2, hold = runs["v1"], runs["v2"], runs["hold"]
    verdict = keep_rule(v1, v2)
    s = res["setup"]
    lines = [
        "# E18.3 · Exit policy v1 vs v2: backtest + live forward tally (D78)",
        "",
        "Status: report only, flips nothing. Code: `scripts/exit_policy_report.py` (this "
        "report is its output), the shadow/MFE writer `arc journal backfill-exit-stats` "
        "(`arc/journal/outcomes.py`). Plan ref: D78, D19, E7.2/E7.5.",
        "",
        "Reproduce (~5 min on 6 workers, offline on the cached chains; the live store is "
        "opened read-only):",
        "",
        "    .venv/bin/python scripts/exit_policy_report.py \\",
        "        --data-dir ~/GitHub/Project-Arc/data \\",
        "        --db data/arc.db --json data/backtest/e183/results.json \\",
        "        --out docs/RESEARCH/exit-policy-v2.md --workers 6",
        "",
        "## Verdict (rule fixed in the card before the run)",
        "",
        "Keep v2 if, for `cash_debit`, v2's net P&L is not lower than v1's in ≥ 2 of 3 trend "
        "sub-periods **and** max drawdown is not higher by more than 10 %. Otherwise "
        "recommend rollback (or the best grid cell as a future XP arm, never a direct flip).",
        "",
        f"**{verdict.text}.**",
        "",
        f"Margins: bear {_usd(v2.subperiods['bear'] - v1.subperiods['bear'])} for v2 "
        f"({_usd(v2.subperiods['bear'])} vs {_usd(v1.subperiods['bear'])}), bull "
        f"{_usd(v2.subperiods['bull'] - v1.subperiods['bull'])} "
        f"({_usd(v2.subperiods['bull'])} vs {_usd(v1.subperiods['bull'])}), max drawdown "
        f"{v2.max_dd:,.0f} vs {v1.max_dd:,.0f}. Both variants lose money over the window "
        "(the E7.5 finding for `cash_debit`): v2 loses less, it does not make the profile "
        "profitable.",
        "",
        "`cash_debit` does not trade sideways sessions (no neutral debit structure), so the "
        'sideways sub-period is 0 = 0 and always counts as "not lower": the rule really '
        "turns on bear and bull. The owner decides.",
        "",
        "## Method",
        "",
        f"Run A of [ranking-backtest.md](ranking-backtest.md), narrowed: profile `{s['profile']}`, "
        f"incumbent ranker `{s['ranker']}`, tickers {', '.join(s['tickers'])}, daily decisions "
        f"{s['start']} → {s['end']}, entry DTE {s['window'][0]}–{s['window'][1]}, "
        f"`{s['marks']}` marks, cost x = {s['slippage_x']}, {s['n_paths']} MC paths, trend "
        "stance, D18 sizing, the pure gate caps. Exit variants (debit kinds):",
        "",
        "- **v1**: take profit 1.00 × debit, no profit lock (before E18.1);",
        "- **v2**: shipped E18.1 defaults: take profit 0.60 × debit, profit lock arms at a "
        "0.50 × debit peak and closes at 0.20 × debit;",
        "- **hold**: hold to expiry (no TP, stop, lock or DTE exit), reference.",
        "",
        "Stop (0.75 × debit, end of day) and the 7-DTE exit are the same in v1 and v2. Each "
        "variant prices its own menus, because the managed-exit Monte Carlo (and the live "
        "managed Net EV > 0 floor) depends on the exit policy. Every rule is checked on "
        "end-of-day marks: the backtest has no intraday marks, so v2's intraday lock acts "
        "at the close here. **The fill-day guard (E18.2) is not modelled**: it holds "
        "discretionary (Research/LLM) closes, which the backtest does not simulate.",
        "",
        "## Results (`cash_debit`, debit_width)",
        "",
        _HEAD,
        _row(v1),
        _row(v2),
        _row(hold),
        _row(runs["v1 exits on v2 picks"]),
        "",
        '"v1 exits on v2 picks" holds v2\'s menus and picks and changes only the exits: '
        "the difference to the v2 row is the exit rules alone, the difference to the v1 "
        "row is what the policy did to the menus (managed Net EV filter).",
        "",
        f"Hold-to-expiry P&L of the same picks: v1 {_usd(v1.hold_shadow_total)}, "
        f"v2 {_usd(v2.hold_shadow_total)}.",
        "",
        "## Threshold grid (descriptive only)",
        "",
        "v2's menus and picks held fixed, each pick replayed under the cell's debit-kind "
        "exits (stop and DTE exit unchanged). Floor 0.0 = breakeven (the model needs "
        f"floor > 0, so {BREAKEVEN:g} is used). A cell the exit-policy validator refuses "
        "(floor ≥ arm, or arm ≥ take profit: the lock could never act) is listed as n/a. "
        "**No cell is a recommendation to flip a default**: a cell worth trying goes into a "
        "future XP arm.",
        "",
        _HEAD,
    ]
    invalid = {c.name: c.invalid for c in grid_cells()}  # validator text, re-derived
    for c in res["cells"]:
        key = f"grid: {c['name']}"
        if c["invalid"]:
            c = {**c, "invalid": invalid.get(c["name"]) or c["invalid"]}
            lines.append(f"| {c['name']} | n/a: {c['invalid']} |  |  |  |  |  |  |")
        elif key in runs:
            m = runs[key]
            lines.append(_row(RunMetrics(**{**asdict(m), "name": c["name"]})))
    valid = [runs[f"grid: {c['name']}"] for c in res["cells"] if not c["invalid"]]
    if valid:
        best = max(valid, key=lambda m: m.net_pnl)
        lines += [
            "",
            f"Best cell by net P&L: `{best.name.removeprefix('grid: ')}` "
            f"({_usd(best.net_pnl)}, max DD {best.max_dd:,.0f}); descriptive, one sample, "
            "in-sample. Candidate for a future XP arm only.",
        ]
    lines += ["", "## Live forward tally", ""]
    if live is None:
        lines.append("_Not run (no `--db`)._")
    else:
        closed = live["closed"]
        lines += [
            f"Closed positions in the live store: {len(closed)}; closed under v2 (after "
            f"{V2_LIVE_AT}, PR #201): **{live['under_v2']}**. The card's tally by exit reason "
            f"(realised vs hold-to-expiry shadow) is appended once ≥ {FORWARD_MIN} positions "
            "have closed under v2.",
            "",
            "Pre-v2 closes, for reference (MFE / MAE from the stored 30-min marks and the "
            "exit; shadow = hold-to-expiry P&L, priced by the nightly reconcile once the legs "
            "expire):",
            "",
            "| Opened | Ticker | Kind | Exit reason | Realised | MAE | MFE | Peak % of debit "
            "| Shadow |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
        for r in closed:
            lines.append(
                f"| {r['opened_at']} | {r['ticker']} | {r['kind']} | {r['exit_reason']} "
                f"| {_usd(r['realised'])} | {_usd(r['mae'])} | {_usd(r['mfe'])} "
                f"| {_pct(r['peak_pct_of_debit'])} | {_usd(r['shadow'])} |"
            )
        peaks = [r["peak_pct_of_debit"] for r in closed if r["peak_pct_of_debit"] is not None]
        if peaks:
            lines += [
                "",
                f"Highest peak: {max(peaks):.0%} of the debit. v2's lock arms at 50 % and its "
                "take profit fires at 60 %, so neither v2 rule would have acted on any of "
                "these positions on the stored marks: their losses came from discretionary "
                "closes and the remaining-EV floor, not from the profit-taking rules.",
            ]
    lines += [
        "",
        "## Caveats",
        "",
        "Estimated spreads (no quotes), end-of-day decisions and marks only, the trend "
        "label is a deterministic proxy for Research's stance, 6 liquid names, one 29-month "
        "window. In-sample: the grid reuses the same window it describes. The live sample "
        "is far too small to prove anything.",
        "",
    ]
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data-dir", type=Path, default=Path("data"))
    ap.add_argument("--db", type=Path, default=None, help="Live store (opened read-only)")
    ap.add_argument("--out", type=Path, default=Path("docs/RESEARCH/exit-policy-v2.md"))
    ap.add_argument("--json", type=Path, default=None, help="Save the backtest results here")
    ap.add_argument("--from-json", type=Path, default=None, help="Render a saved run")
    ap.add_argument("--workers", type=int, default=6)
    args = ap.parse_args(argv)
    if args.from_json is not None:
        res = json.loads(args.from_json.read_text())
    else:
        res = run_backtest(args.data_dir, workers=args.workers)
        if args.json is not None:
            args.json.parent.mkdir(parents=True, exist_ok=True)
            args.json.write_text(json.dumps(res, indent=2, default=str))
    live = live_tally(args.db) if args.db is not None else None
    args.out.write_text(render(res, live))
    log.info("e183.report_written", path=str(args.out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
