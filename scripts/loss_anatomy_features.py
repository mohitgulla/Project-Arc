"""E7.18: the point-in-time entry feature table (one row per trade) and its inputs.

Every entry-time column is computed from data dated on or before the trade's entry
session (rolling windows are causal; percentile ranks are trailing). The columns that
describe the hold itself (``post_*``: earnings/macro inside the actual hold, the
event's realised move) are labelled as such and never enter the classifier.

Inputs (all under ``data/``, gitignored):
* ``underlying_ohlc/<T>.parquet``: split-adjusted daily OHLC (price state, gaps,
  breadth, correlation clusters); ``option_stats/daily`` ``spot_adj`` when absent.
* ``option_stats/daily/underlying=<T>.parquet``: per-ticker option state.
* ``option_stats/earnings_events.parquet``: detected earnings reaction days.
* ``config/backtest/macro_calendar_2020_2026.yaml``: FOMC/CPI/NFP release days.
* ``backtest/sector_map.parquet``: SEC SIC per stock, asset class per ETF (cached by
  :func:`sector_map`; ``data.sec.gov`` at <= 10 req/s with ``edgar_user_agent``).
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
import time
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd
import yaml

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# The formulas live in the feature library (E27.1, "promote, don't copy"); this script
# keeps only the loaders and the CLI. Re-exported for its callers and tests.
from arc.ml.features.compute import (  # noqa: E402
    OPTION_COLS,
    PLANNED_EXIT_DTE,
    SECTOR_ETFS,
    build_features,
    etf_class,
    event_features,
    market_state,
    portfolio_state,
    price_state,
    regime_change_soon,
    sic_sector,
    trade_columns,
)
from scripts.loss_attribution import DATA  # noqa: E402
from scripts.loss_attribution_report import BEAR, BULL, book  # noqa: E402

RUNS = Path(os.environ.get("ARC_ANATOMY_RUNS") or DATA / "backtest" / "runs" / "anatomy")
MACRO_FILE = ROOT / "config" / "backtest" / "macro_calendar_2020_2026.yaml"
SECTOR_FILE = DATA / "backtest" / "sector_map.parquet"


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def _dates(s: Iterable[object]) -> list[dt.date]:
    return [x if isinstance(x, dt.date) else pd.Timestamp(x).date() for x in s]


def load_prices(tickers: Iterable[str], data: Path = DATA) -> dict[str, pd.DataFrame]:
    """Split-adjusted daily OHLC per ticker, indexed by date (sorted)."""
    out: dict[str, pd.DataFrame] = {}
    for t in sorted(set(tickers)):
        p = data / "underlying_ohlc" / f"{t}.parquet"
        if p.is_file():
            df = pd.read_parquet(p, columns=["date", "open", "high", "low", "close"])
        else:
            q = data / "option_stats" / "daily" / f"underlying={t}.parquet"
            if not q.is_file():
                continue
            df = pd.read_parquet(q, columns=["date", "spot_adj"]).rename(
                columns={"spot_adj": "close"}
            )
            df["open"] = np.nan
        df["date"] = _dates(df["date"])
        df = df.dropna(subset=["close"]).drop_duplicates("date").set_index("date").sort_index()
        out[t] = df
    return out


def load_option_stats(tickers: Iterable[str], data: Path = DATA) -> dict[str, pd.DataFrame]:
    out: dict[str, pd.DataFrame] = {}
    for t in sorted(set(tickers)):
        p = data / "option_stats" / "daily" / f"underlying={t}.parquet"
        if not p.is_file():
            continue
        df = pd.read_parquet(p, columns=["date", *OPTION_COLS])
        df["date"] = _dates(df["date"])
        out[t] = df.drop_duplicates("date").set_index("date").sort_index()
    return out


def load_macro(path: Path = MACRO_FILE) -> dict[str, list[dt.date]]:
    raw = yaml.safe_load(path.read_text())
    return {k: sorted(_dates(v)) for k, v in raw.items()}


def load_earnings(data: Path = DATA) -> pd.DataFrame:
    ev = pd.read_parquet(data / "option_stats" / "earnings_events.parquet")
    ev["event_date"] = _dates(ev["event_date"])
    return ev


# ---------------------------------------------------------------------------
# Sector / asset class
# ---------------------------------------------------------------------------


def sector_map(
    tickers: Iterable[str],
    etf: Mapping[str, bool],
    *,
    data: Path = DATA,
    fetch: bool = True,
    rate_per_s: float = 8.0,
) -> pd.DataFrame:
    """``ticker, etf, sic, sic_desc, group`` for *tickers*, cached in sector_map.parquet."""
    cache = pd.read_parquet(SECTOR_FILE) if SECTOR_FILE.is_file() else pd.DataFrame()
    have = set(cache["ticker"]) if len(cache) else set()
    master = json.loads((data / "symbol_master.json").read_text())["symbols"]
    rows: list[dict[str, object]] = []
    ua: str | None = None
    for t in sorted(set(tickers) - have):
        info = master.get(t, {})
        name = str(info.get("name") or "")
        if etf.get(t, False):
            rows.append(
                {
                    "ticker": t,
                    "etf": True,
                    "sic": None,
                    "sic_desc": name,
                    "group": "etf:" + etf_class(t, name),
                }
            )
            continue
        sic, desc = None, ""
        cik = info.get("cik")
        if fetch and cik:
            if ua is None:
                from arc.config import ArcSettings

                ua = ArcSettings().edgar_user_agent
            url = f"https://data.sec.gov/submissions/CIK{int(cik):010d}.json"
            req = urllib.request.Request(url, headers={"User-Agent": ua})
            try:
                with urllib.request.urlopen(req, timeout=20) as r:  # noqa: S310 - fixed host
                    d = json.load(r)
                sic = int(d["sic"]) if d.get("sic") else None
                desc = str(d.get("sicDescription") or "")
            except (OSError, ValueError, KeyError):
                sic = None
            time.sleep(1.0 / rate_per_s)
        rows.append(
            {"ticker": t, "etf": False, "sic": sic, "sic_desc": desc, "group": sic_sector(sic)}
        )
    if rows:
        cache = pd.concat([cache, pd.DataFrame(rows)], ignore_index=True)
        SECTOR_FILE.parent.mkdir(parents=True, exist_ok=True)
        cache.to_parquet(SECTOR_FILE, index=False)
    out = cache[cache["ticker"].isin(sorted(set(tickers)))].reset_index(drop=True)
    stock = ~out["etf"].astype(bool)
    out.loc[stock, "group"] = [
        sic_sector(None if pd.isna(v) else int(v)) for v in out.loc[stock, "sic"]
    ]
    return out


def write_features(name: str) -> Path:  # pragma: no cover - driven by the CLI
    from arc.backtest.universe import checkpoint_symbols, load_universe

    run = RUNS / name
    frames = []
    for p in ("margin", "cash_debit"):
        t, e = book(run, p)
        t["profile"] = p
        frames.append((p, t, e))
    names = sorted(set().union(*(set(t["underlying"]) for _, t, _ in frames)))
    _, rows, _ = load_universe(DATA)
    c500 = checkpoint_symbols(rows, "c500")
    prices = load_prices({*names, *c500, "SPY", "HYG", "TLT", *SECTOR_ETFS})
    opt = load_option_stats({*names, "SPY"})
    market = market_state(prices, opt, c500)
    etf = {}
    for _, t, _ in frames:
        etf |= dict(zip(t["underlying"], t["etf"].astype(bool), strict=True))
    smap = sector_map(names, etf)
    groups = dict(zip(smap["ticker"], smap["group"], strict=True))
    earnings, macro = load_earnings(), load_macro()
    chg = regime_change_soon(market)
    out = []
    for _p, t, e in frames:
        f = build_features(
            t,
            e,
            prices=prices,
            opt=opt,
            market=market,
            earnings=earnings,
            macro=macro,
            groups=groups,
        )
        f["post_regime_change_10s"] = [bool(chg.get(d, False)) for d in f["entry_date"]]
        f["book"] = name
        out.append(f)
    df = pd.concat(out, ignore_index=True)
    dest = run / "features.parquet"
    df.to_parquet(dest, index=False)
    market.to_parquet(run / "market.parquet")
    sys.stdout.write(f"{name}: {len(df)} trades x {df.shape[1]} cols -> {dest}\n")
    return dest


__all__ = [
    "BEAR",
    "BULL",
    "OPTION_COLS",
    "PLANNED_EXIT_DTE",
    "SECTOR_ETFS",
    "build_features",
    "etf_class",
    "event_features",
    "load_earnings",
    "load_macro",
    "load_option_stats",
    "load_prices",
    "market_state",
    "portfolio_state",
    "price_state",
    "regime_change_soon",
    "sector_map",
    "sic_sector",
    "trade_columns",
    "write_features",
]
