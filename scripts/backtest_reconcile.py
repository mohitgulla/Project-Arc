"""E7.14 study 8: backtest <-> live reconciliation.

Replays every closed live trade (read-only ``open_structures`` + ``executions``) through
the backtest's fill model and exit rules on its actual legs and entry day, using the
backtest's own marked chains (``HistoryView``, ``config/ranking.yaml`` backtest data
settings) and ``config/exits.yaml``:

* **entry:** predicted EOD entry net = Σ side·(mid + side·x·spread) on the entry
  session (x = ``costs.yaml`` slippage_frac), vs the live fill;
* **exit at the live exit day:** predicted EOD close net = Σ side·(mid − side·x·spread)
  on the session the live trade closed, vs the live close fill;
* **P&L:** predicted (entry → live exit day, the fill model) vs live realised;
* **exit rule:** the first rule the backtest would fire on EOD marks after entry (stop →
  lock → TP → DTE) up to the last cached session, vs the live exit reason.

Trades whose entry session or legs are outside the cached data are listed as skipped.
Aggregates and per-trade prices of our own fills only (no vendor rows).

    .venv/bin/python scripts/backtest_reconcile.py --db data/arc.db \
        --db data/archive/<dir>/arc.db --out <dir>
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sqlite3
import statistics
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from arc.backtest.costs import load_cost_model  # noqa: E402
from arc.backtest.data import HistoryView  # noqa: E402
from arc.backtest.ranking import load_ranking_file  # noqa: E402
from arc.exits.policy import check_rules, load_exit_config, resolve_rules  # noqa: E402
from arc.models import Structure  # noqa: E402
from arc.utils.calendar import ET  # noqa: E402

MULT = 100.0


def _day(ts: str) -> dt.date:
    t = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    return t.astimezone(ET).date()


def closed_trades(db: Path) -> list[dict[str, Any]]:
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    out = []
    for r in c.execute(
        "select * from open_structures where status='closed' order by opened_at, id"
    ).fetchall():
        o = c.execute(
            "select o.realised_pnl, o.exit_reason from outcomes o where o.proposal_hash=?"
            " order by o.id desc limit 1",
            (r["open_proposal_hash"],),
        ).fetchone()
        ex = {
            e["kind"]: e
            for e in c.execute(
                "select kind, fill_price, filled_qty, finished_at from executions"
                " where (proposal_hash=? or structure_id=?) and filled_qty > 0"
                " order by started_at",
                (r["open_proposal_hash"], r["id"]),
            )
        }
        out.append(
            {
                "store": db.parent.name,
                "id": r["id"],
                "ticker": r["ticker"],
                "structure": json.loads(r["structure_json"]),
                "contracts": int(r["contracts"]),
                "entry_fill": float(r["entry_net"]),
                "close_fill": float(r["close_net"]) if r["close_net"] is not None else None,
                "entry_day": _day(r["opened_at"]),
                "exit_day": dt.date.fromisoformat(r["exit_day"])
                if r["exit_day"]
                else _day(r["closed_at"]),
                "exit_reason": r["exit_reason"],
                "realised_pnl": float(o["realised_pnl"]) if o and o["realised_pnl"] else None,
                "executions": sorted(ex),
            }
        )
    return out


def _marks(chain: Any, symbols: list[str]) -> dict[str, tuple[float, float]] | None:
    rows = chain[chain["symbol"].isin(symbols)]
    m = {str(x.symbol): (float(x.mid), float(x.spread)) for x in rows.itertuples()}
    return m if len(m) == len(set(symbols)) else None


def replay(t: dict[str, Any], view: HistoryView, x: float) -> dict[str, Any]:
    st = t["structure"]
    legs = [
        (lg["occ_symbol"], 1 if lg["side"] == "long" else -1, int(lg.get("ratio") or 1))
        for lg in st["legs"]
    ]
    syms = [s for s, _, _ in legs]
    h = view.load(t["ticker"])
    chains = h.chains
    row: dict[str, Any] = {
        k: t[k]
        for k in ("store", "id", "ticker", "contracts", "entry_day", "exit_day", "exit_reason")
    }
    row["kind"] = st["kind"]
    row["entry_live"] = t["entry_fill"]
    row["close_live"] = -t["close_fill"] if t["close_fill"] is not None else None
    row["pnl_live"] = t["realised_pnl"]
    e = chains.get(t["entry_day"])
    m0 = None if e is None else _marks(e, syms)
    if m0 is None:
        row["skip"] = "entry session or legs not in the cached chains"
        return row
    mid0 = sum(s * r * m0[sym][0] for sym, s, r in legs)
    spr0 = sum(r * m0[sym][1] for sym, s, r in legs)
    row["entry_mid"] = round(mid0, 4)
    row["entry_spread"] = round(spr0, 4)
    row["entry_pred"] = round(mid0 + x * spr0, 4)
    xe = chains.get(t["exit_day"])
    m1 = None if xe is None else _marks(xe, syms)
    if m1 is not None:
        mid1 = sum(s * r * m1[sym][0] for sym, s, r in legs)
        spr1 = sum(r * m1[sym][1] for sym, s, r in legs)
        row["close_mid"] = round(mid1, 4)
        row["close_pred"] = round(mid1 - x * spr1, 4)
        row["pnl_pred"] = round((row["close_pred"] - row["entry_pred"]) * MULT * t["contracts"], 2)
        row["pnl_pred_at_live_entry"] = round(
            (row["close_pred"] - t["entry_fill"]) * MULT * t["contracts"], 2
        )
    structure = Structure.model_validate(st)
    exits = load_exit_config()
    rules = resolve_rules(structure, exits.policy_for(structure.kind), entry_net=row["entry_pred"])
    expiry = min(dt.date(2000 + int(s[-15:-13]), int(s[-13:-11]), int(s[-11:-9])) for s in syms)
    peak = None
    row["sim_exit_reason"] = None
    for d in sorted(chains):
        if d <= t["entry_day"]:
            continue
        if d >= expiry:
            row["sim_exit_reason"], row["sim_exit_day"] = "expiry", expiry
            break
        m = _marks(chains[d], syms)
        if m is None:
            continue
        pnl = sum(s * r * m[sym][0] for sym, s, r in legs) - rules.entry_net
        fired = check_rules(rules, pnl=pnl, dte=(expiry - d).days, peak_pnl=peak)
        peak = pnl if peak is None else max(peak, pnl)
        if fired is not None:
            row["sim_exit_reason"], row["sim_exit_day"] = str(fired.value), d
            break
    if row["sim_exit_reason"] is None:
        row["sim_exit_reason"] = f"open at data end {max(chains)}"
    return row


def summarise(rows: list[dict[str, Any]]) -> dict[str, Any]:
    ok = [r for r in rows if "skip" not in r]

    def stats(vals: list[float]) -> dict[str, float | int | None]:
        if not vals:
            return {"n": 0, "mean": None, "median": None, "rmse": None}
        return {
            "n": len(vals),
            "mean": round(statistics.fmean(vals), 4),
            "median": round(statistics.median(vals), 4),
            "rmse": round((sum(v * v for v in vals) / len(vals)) ** 0.5, 4),
        }

    entry_err = [r["entry_live"] - r["entry_pred"] for r in ok]
    entry_vs_mid = [
        (r["entry_live"] - r["entry_mid"]) / r["entry_spread"] for r in ok if r["entry_spread"] > 0
    ]
    close = [r for r in ok if "close_pred" in r and r["close_live"] is not None]
    close_err = [r["close_live"] - r["close_pred"] for r in close]
    close_vs_mid = [
        (r["close_mid"] - r["close_live"]) / max(r["entry_spread"], 1e-9) for r in close
    ]
    pnl_err = [r["pnl_live"] - r["pnl_pred"] for r in close if r["pnl_live"] is not None]
    # Same-session round trips cancel the intraday-vs-EOD move: live entry − close is
    # pure cost (spread paid twice + any intraday drift), the model's is 2·x·spread.
    same = [r for r in close if r["entry_day"] == r["exit_day"]]
    rt_live = [r["entry_live"] - r["close_live"] for r in same]
    rt_pred = [r["entry_pred"] - r["close_pred"] for r in same]
    return {
        "trades": len(rows),
        "replayed": len(ok),
        "skipped": len(rows) - len(ok),
        "same_session_round_trips": len(same),
        "round_trip_cost_live_per_share": stats(rt_live),
        "round_trip_cost_pred_per_share": stats(rt_pred),
        "entry_live_minus_pred_per_share": stats(entry_err),
        "entry_realised_x": stats(entry_vs_mid),
        "close_live_minus_pred_per_share": stats(close_err),
        "close_realised_x_approx": stats(close_vs_mid),
        "pnl_live_minus_pred_usd": stats(pnl_err),
        "exit_reason_match": sum(
            1 for r in ok if str(r.get("sim_exit_reason")) == str(r["exit_reason"])
        ),
    }


def live_slippage_x(dbs: list[Path]) -> dict[str, Any]:
    """The E7.3 scorecard's realised entry slippage x per structure kind (all fills)."""
    from arc.journal.scorecard import _filled_executions, _slippage_row, slippage_by_kind

    rows = []
    for db in dbs:
        c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        c.row_factory = sqlite3.Row
        rows += [x for r in _filled_executions(c) if (x := _slippage_row(c, r)) is not None]
    by = slippage_by_kind(rows)
    out: dict[str, Any] = {}
    for k, v in by.items():
        x = v.realised_usd / v.spread_usd if v.spread_usd else None
        out[k] = {
            "fills": v.fills,
            "realised_usd": round(v.realised_usd, 2),
            "spread_usd": round(v.spread_usd, 2),
            "x": None if x is None else round(x, 3),
        }
    tot_r = sum(v.realised_usd for v in by.values())
    tot_s = sum(v.spread_usd for v in by.values())
    out["all"] = {
        "fills": sum(v.fills for v in by.values()),
        "x": round(tot_r / tot_s, 3) if tot_s else None,
    }
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--db", type=Path, action="append", required=True, help="Read-only stores")
    ap.add_argument("--data-dir", type=Path, default=ROOT / "data")
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args(argv)
    trades = [t for db in a.db for t in closed_trades(db)]
    cfg = load_ranking_file()
    cost = load_cost_model()
    lo = min(t["entry_day"] for t in trades) - dt.timedelta(days=400)
    hi = max(t["exit_day"] for t in trades)
    view = HistoryView(
        a.data_dir,
        settings=cfg.backtest.data,
        universe="c500",
        start=lo,
        end=hi,
        cost=cost,
        flat_rate=cfg.backtest.risk_free_rate,
        min_iv_obs=20,
    )
    rows = []
    for t in trades:
        try:
            rows.append(replay(t, view, cost.slippage_frac))
        except Exception as exc:  # noqa: BLE001 - report every trade, never abort
            skip = f"{type(exc).__name__}: {exc}"
            rows.append({"id": t["id"], "ticker": t["ticker"], "skip": skip})
    res = {
        "slippage_x": cost.slippage_frac,
        "live_entry_slippage_x": live_slippage_x(a.db),
        "summary": summarise(rows),
        "trades": rows,
    }
    a.out.mkdir(parents=True, exist_ok=True)
    (a.out / "backtest_reconcile.json").write_text(json.dumps(res, indent=1, default=str) + "\n")
    print(json.dumps(res, indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
