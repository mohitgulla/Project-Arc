"""E7.17 report: tables for docs/RESEARCH/loss-attribution.md (aggregates only).

Called by ``scripts/loss_attribution.py report``. Reads the attribution runs under
``data/backtest/runs/attribution/``, the E7.14 BT-2/BT-3/BT-8 run dirs, the raw quotes
(sanity re-pricing only; no vendor rows are written) and, read-only, the live stores.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.loss_attribution import DATA, RUN_SPECS, RUNS  # noqa: E402
from scripts.loss_attribution_sanity import (  # noqa: E402
    load_equity,
    load_trades,
    reprice,
    sample,
)

PROFILES = ("margin", "cash_debit")
BT = DATA / "backtest" / "runs"
BT3 = BT / "BT-3" / "20261015T224942-dev-c125-6bd6ea4a"
BT2 = BT / "BT-2" / "20261016T004447-dev-c125-a5bc5634"
BT8 = BT / "BT-8" / "20261015T142415-dev-c125-bd19b741"
EQUITY0 = 25_000.0
BULL = {"bull_put", "bull_call", "long_call"}
BEAR = {"bear_call", "bear_put", "long_put"}
PERIODS = [
    ("2020 crash", dt.date(2020, 1, 1), dt.date(2020, 4, 30)),
    ("2020 rebound", dt.date(2020, 5, 1), dt.date(2020, 12, 31)),
    ("2021 bull", dt.date(2021, 1, 1), dt.date(2021, 12, 31)),
    ("2022 bear", dt.date(2022, 1, 1), dt.date(2022, 12, 31)),
    ("2023", dt.date(2023, 1, 1), dt.date(2023, 12, 31)),
]
LIQ_BINS = [0, 5e3, 5e4, 5e5, np.inf]
LIQ_LABELS = ["<5k", "5k-50k", "50k-500k", ">=500k"]


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def run_dir(run: Path, profile: str, arm: str | None = None) -> Path:
    if arm is not None:
        return run / profile / arm
    got = sorted(p.parent for p in (run / profile).glob("*/trades.csv"))
    if len(got) != 1:
        msg = f"{run / profile}: expected one ranker dir, got {got}"
        raise FileNotFoundError(msg)
    return got[0]


def book(run: Path, profile: str, arm: str | None = None) -> tuple[pd.DataFrame, pd.Series]:
    d = run_dir(run, profile, arm)
    t = load_trades(d / "trades.csv")
    t["exit_day"] = [
        x if x is not None else e for x, e in zip(t["exit_date"], t["expiration"], strict=True)
    ]
    t["risk"] = t["contracts"] * t["max_loss_unit"]
    t["slippage"] = t["pnl_mid"] - t["pnl"] - t["fees"]
    t["win"] = t["pnl"] > 0
    t["year"] = [d.year for d in t["entry_date"]]
    t["period"] = [_period(d) for d in t["entry_date"]]
    t["liq_bucket"] = pd.cut(t["liquidity"], LIQ_BINS, labels=LIQ_LABELS, right=False).astype(str)
    t["direction"] = [
        "bullish" if k in BULL else "bearish" if k in BEAR else "neutral" for k in t["kind"]
    ]
    t["class"] = np.where(t["etf"].astype(bool), "ETFs", "Stocks")
    return t, load_equity(d / "equity.csv")


def _period(d: dt.date) -> str:
    for name, a, b in PERIODS:
        if a <= d <= b:
            return name
    return "other"


def metrics(t: pd.DataFrame, e: pd.Series) -> dict[str, Any]:
    peak = e.cummax()
    dd = ((peak - e) / peak).max() if len(e) else 0.0
    return {
        "trades": len(t),
        "net_pnl": round(float(t["pnl"].sum()), 2),
        "end_equity": round(float(e.iloc[-1]), 2) if len(e) else EQUITY0,
        "return_pct": round((float(e.iloc[-1]) / EQUITY0 - 1) * 100, 2) if len(e) else 0.0,
        "max_dd_pct": round(float(dd) * 100, 2),
        "win_rate": round(float(t["win"].mean()), 3) if len(t) else None,
        "pnl_per_risk": round(float(t["pnl"].sum() / t["risk"].sum()), 4) if len(t) else None,
    }


# ---------------------------------------------------------------------------
# 1. Sanity
# ---------------------------------------------------------------------------


def reconcile(t: pd.DataFrame, e: pd.Series) -> dict[str, Any]:
    """Σ pnl vs Δequity at the end (all trades closed by data end), min equity."""
    return {
        "sum_pnl": round(float(t["pnl"].sum()), 2),
        "delta_equity": round(float(e.iloc[-1] - EQUITY0), 2),
        "diff": round(float(t["pnl"].sum() - (e.iloc[-1] - EQUITY0)), 4),
        "min_equity": round(float(e.min()), 2),
        "min_equity_day": str(e.idxmin()),
        "open_at_end": int(sum(1 for d in t["exit_day"] if d > e.index[-1])),
    }


def sanity(run: Path, arm: str | None = None) -> pd.DataFrame:
    out = []
    for p in PROFILES:
        t, e = book(run, p, arm)
        s = sample(t)
        r = reprice(s, DATA, e)
        out.append(r)
    return pd.concat(out, ignore_index=True)


# ---------------------------------------------------------------------------
# 2. Decomposition
# ---------------------------------------------------------------------------

CUTS = {
    "kind": "kind",
    "spec": "spec",
    "trend": "trend",
    "vol": "vol",
    "year": "year",
    "period": "period",
    "tier": "tier",
    "liquidity": "liq_bucket",
    "earnings_in_hold": "earnings_in_hold",
    "exit_reason": "exit_reason",
    "capacity_clipped": "capacity_clipped",
}


def decompose(t: pd.DataFrame, col: str) -> pd.DataFrame:
    total = float(t["pnl"].sum())
    rows = []
    for cls, g0 in [("All", t), *((c, t[t["class"] == c]) for c in ("Stocks", "ETFs"))]:
        for k, g in g0.groupby(col, dropna=False):
            rows.append(
                {
                    "class": cls,
                    "bucket": k,
                    "n": len(g),
                    "net": round(float(g["pnl"].sum()), 0),
                    "mean": round(float(g["pnl"].mean()), 1),
                    "median": round(float(g["pnl"].median()), 1),
                    "win": round(float(g["win"].mean()), 2),
                    "pnl_per_risk": round(float(g["pnl"].sum() / g["risk"].sum()), 3),
                    "share_of_net": round(float(g["pnl"].sum()) / abs(total), 3) if total else None,
                }
            )
    return pd.DataFrame(rows)


def earnings_events(tickers: list[str]) -> dict[str, list[dt.date]]:
    """Detected earnings reaction days per stock: the E7.12 ``earnings_events.parquet``.

    Same detector (``arc.backtest.earnings_hist.detect_events``) the simulator's
    earnings rules use, precomputed over the whole quote history.
    """
    ev = pd.read_parquet(DATA / "option_stats" / "earnings_events.parquet")
    ev = ev[ev["ticker"].isin(tickers)]
    out: dict[str, list[dt.date]] = {t: [] for t in tickers}
    for t, d in zip(ev["ticker"], ev["event_date"], strict=True):
        out[t].append(d if isinstance(d, dt.date) else dt.date.fromisoformat(str(d)))
    return out


def tag_earnings(t: pd.DataFrame, events: dict[str, list[dt.date]]) -> pd.DataFrame:
    def hit(r: Any) -> str:
        if r.etf:
            return "etf"
        ev = events.get(r.underlying, [])
        return "yes" if any(r.entry_date < d <= r.exit_day for d in ev) else "no"

    t = t.copy()
    t["earnings_in_hold"] = [hit(r) for r in t.itertuples()]
    return t


# ---------------------------------------------------------------------------
# 3. Costs
# ---------------------------------------------------------------------------


def costs(t: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for k, g in [("all", t), *t.groupby("kind")]:
        gross = float(g["pnl_mid"].sum())
        slip = float(g["slippage"].sum())
        fees = float(g["fees"].sum())
        rows.append(
            {
                "kind": k,
                "n": len(g),
                "gross_at_mid": round(gross, 0),
                "slippage_cost": round(slip, 0),
                "fees": round(fees, 0),
                "net": round(float(g["pnl"].sum()), 0),
                "cost_share_of_gross": round((slip + fees) / abs(gross), 2) if gross else None,
                # entry slippage = x * sum(leg spreads); x = 0.25 (quoted fill model)
                "median_entry_spread_pct_of_premium": round(
                    float(
                        (
                            (g["entry_net"] - g["entry_net_mid"]).abs()
                            / 0.25
                            / g["entry_net_mid"].abs()
                        ).median()
                    )
                    * 100,
                    1,
                ),
            }
        )
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# 4. Calibration
# ---------------------------------------------------------------------------


def calibration(t: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    rows = []
    for k, g in [("all", t), *t.groupby("kind")]:
        g = g.dropna(subset=["managed_net_ev", "managed_pop"])
        if not len(g):
            continue
        brier = float(((g["managed_pop"] - g["win"].astype(float)) ** 2).mean())
        base = float(((g["win"].mean() - g["win"].astype(float)) ** 2).mean())
        rows.append(
            {
                "kind": k,
                "n": len(g),
                "pred_ev_per_trade": round(float(g["managed_net_ev"].mean()), 1),
                "real_pnl_per_trade": round(float(g["pnl"].mean()), 1),
                "pred_pop": round(float(g["managed_pop"].mean()), 3),
                "real_win": round(float(g["win"].mean()), 3),
                "brier": round(brier, 4),
                "brier_base_rate": round(base, 4),
                "corr_ev_pnl_unit": round(float(g["managed_net_ev_unit"].corr(g["pnl_unit"])), 3),
            }
        )
    by = t.dropna(subset=["managed_net_ev_unit"]).copy()
    by["decile"] = pd.qcut(by["managed_net_ev_unit"].rank(method="first"), 10, labels=False) + 1
    dec = (
        by.groupby("decile")
        .agg(
            n=("pnl", "size"),
            pred_ev_unit=("managed_net_ev_unit", "mean"),
            real_pnl_unit=("pnl_unit", "mean"),
            pred_pop=("managed_pop", "mean"),
            real_win=("win", "mean"),
        )
        .round(3)
        .reset_index()
    )
    return pd.DataFrame(rows), dec


# ---------------------------------------------------------------------------
# 5. Tail and sizing
# ---------------------------------------------------------------------------


def concentration(t: pd.DataFrame) -> dict[str, float]:
    losses = (-t.loc[t["pnl"] < 0, "pnl"]).sort_values(ascending=False)
    total_loss = float(losses.sum())
    out: dict[str, float] = {"losers": len(losses), "gross_loss": round(total_loss, 0)}
    for q in (0.01, 0.05, 0.10):
        k = max(1, round(len(t) * q))
        out[f"top_{int(q * 100)}pct_trades_share_of_loss"] = round(
            float(losses.iloc[:k].sum()) / total_loss, 3
        )
    out["gross_win"] = round(float(t.loc[t["pnl"] > 0, "pnl"].sum()), 0)
    return out


def drawdown_path(t: pd.DataFrame, e: pd.Series) -> dict[str, Any]:
    peak = e.cummax()
    dd = (peak - e) / peak
    trough = dd.idxmax()
    pk = e[:trough].idxmax()
    first_half = next((d for d, v in e.items() if v < EQUITY0 / 2), None)
    return {
        "peak_day": str(pk),
        "peak_equity": round(float(e[pk]), 0),
        "trough_day": str(trough),
        "trough_equity": round(float(e[trough]), 0),
        "first_below_half": str(first_half) if first_half else None,
    }


def open_book(t: pd.DataFrame, day: dt.date) -> pd.DataFrame:
    return t[(t["entry_date"] <= day) & (t["exit_day"] > day)]


def worst_days(t: pd.DataFrame, e: pd.Series, n: int = 8) -> pd.DataFrame:
    de = e.diff().dropna().sort_values().head(n)
    rows = []
    for d, v in de.items():
        ob = open_book(t, d)
        eq = float(e.shift(1)[d])
        rows.append(
            {
                "day": str(d),
                "equity_change": round(float(v), 0),
                "pct_of_equity": round(float(v) / eq * 100, 1),
                "open": len(ob),
                "bullish": int((ob["direction"] == "bullish").sum()),
                "bearish": int((ob["direction"] == "bearish").sum()),
                "neutral": int((ob["direction"] == "neutral").sum()),
                "risk_pct_equity": round(float(ob["risk"].sum()) / eq * 100, 0),
                "names": ",".join(sorted(set(ob["underlying"]))[:12]),
            }
        )
    return pd.DataFrame(rows)


def exposure_profile(t: pd.DataFrame, e: pd.Series) -> dict[str, float]:
    """Average open risk / equity and same-direction share over all sessions."""
    r, same = [], []
    for d in e.index:
        ob = open_book(t, d)
        if not len(ob):
            continue
        r.append(float(ob["risk"].sum()) / float(e[d]))
        same.append(float(ob["direction"].value_counts().iloc[0]) / len(ob))
    return {
        "mean_open_risk_pct": round(float(np.mean(r)) * 100, 1),
        "p90_open_risk_pct": round(float(np.percentile(r, 90)) * 100, 1),
        "mean_same_direction_share": round(float(np.mean(same)), 2),
    }


# ---------------------------------------------------------------------------
# 7. Live
# ---------------------------------------------------------------------------


def live_trades() -> pd.DataFrame:
    """Closed live trades from the production store + the pre-D80 archive (read-only copies)."""
    stores = [DATA / "arc.db", *sorted((DATA / "archive").glob("*/arc.db"))]
    rows = []
    with tempfile.TemporaryDirectory() as tmp:
        for i, db in enumerate(stores):
            if not db.exists():
                continue
            cp = Path(tmp) / f"s{i}.db"
            src = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            dst = sqlite3.connect(cp)
            src.backup(dst)
            src.close()
            c = dst
            c.row_factory = sqlite3.Row
            for r in c.execute(
                "select s.ticker, s.structure_json, s.contracts, s.opened_at, s.closed_at,"
                " s.exit_reason, (select o.realised_pnl from outcomes o where"
                " o.proposal_hash = s.open_proposal_hash order by o.id desc limit 1) pnl"
                " from open_structures s where s.status='closed'"
            ):
                st = json.loads(r["structure_json"])
                rows.append(
                    {
                        "store": db.parent.name,
                        "ticker": r["ticker"],
                        "kind": st.get("kind"),
                        "legs": len(st.get("legs", [])),
                        "contracts": r["contracts"],
                        "opened": str(r["opened_at"])[:10],
                        "closed": str(r["closed_at"])[:10],
                        "exit_reason": r["exit_reason"],
                        "pnl": float(r["pnl"]) if r["pnl"] not in (None, "") else None,
                    }
                )
            c.close()
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Writer
# ---------------------------------------------------------------------------


def md_table(df: pd.DataFrame) -> str:
    """A GitHub markdown table (no tabulate dependency)."""
    if not len(df):
        return "_(none)_"

    def cell(v: object) -> str:
        if isinstance(v, float):
            return "" if v != v else f"{v:,.4g}" if abs(v) < 1000 else f"{v:,.0f}"
        return str(v).replace("|", "/")

    head = "| " + " | ".join(map(str, df.columns)) + " |"
    sep = "|" + "|".join("---" for _ in df.columns) + "|"
    body = ["| " + " | ".join(cell(v) for v in r) + " |" for r in df.itertuples(index=False)]
    return "\n".join([head, sep, *body])


def collect() -> dict[str, Any]:
    """Every number the report states, as frames/dicts (also written as CSVs)."""
    res: dict[str, Any] = {"runs": {}, "books": {}}
    for name in RUN_SPECS:
        run = RUNS / name
        if not (run / "report.md").exists():
            continue
        for p in PROFILES:
            try:
                t, e = book(run, p)
            except FileNotFoundError:
                continue
            res["runs"][(name, p)] = metrics(t, e)
            res["books"][(name, p)] = (t, e)
    for p in PROFILES:
        t, e = book(BT3, p, "control")
        res["runs"][("BT-3 control (E7.14)", p)] = metrics(t, e)
        res["books"][("bt3", p)] = (t, e)
        for arm in ("control", "t1"):
            t2, e2 = book(BT2, p, arm)
            res["runs"][(f"BT-2 {'hold' if arm == 'control' else 'managed'} (E7.14)", p)] = metrics(
                t2, e2
            )
    return res


def write_report(out: Path) -> None:  # pragma: no cover - driven by the CLI
    from scripts.loss_attribution_text import render

    render(out)
