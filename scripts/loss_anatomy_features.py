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
import re
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

from scripts.loss_attribution import DATA  # noqa: E402
from scripts.loss_attribution_report import BEAR, BULL, book  # noqa: E402

RUNS = Path(os.environ.get("ARC_ANATOMY_RUNS") or DATA / "backtest" / "runs" / "anatomy")
MACRO_FILE = ROOT / "config" / "backtest" / "macro_calendar_2020_2026.yaml"
SECTOR_FILE = DATA / "backtest" / "sector_map.parquet"
SECTOR_ETFS = ("XLK", "XLF", "XLE", "XLV", "XLY", "XLP", "XLI", "XLU", "XLB", "XLRE", "XLC")
OPTION_COLS = (
    "iv30",
    "iv_rank_252",
    "iv_pct_252",
    "term_slope",
    "front_over_iv30",
    "skew_25d",
    "rv20",
    "rv60",
    "vrp20",
    "atm_spread_pct",
    "two_sided_pct",
    "oi_near_atm",
    "opt_volume",
    "put_call_oi",
    "put_call_vol",
    "d9_pass",
)
#: Close-at-DTE of config/exits.yaml (planned exit = expiration - 7 calendar days).
PLANNED_EXIT_DTE = 7


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

_ETF_RULES: tuple[tuple[str, str], ...] = (
    (
        r"\b(ultra|2x|3x|leveraged|bull 3x|bear 3x|daily .*bull|daily .*bear|-1x|inverse)",
        "leveraged",
    ),
    (r"\b(vix|volatility)\b", "vol"),
    (r"\b(gold|silver|oil|commodity|natural gas|metals|uranium|copper|agricultur)", "commodity"),
    (
        r"\b(treasury|bond|aggregate|corporate|high yield|municipal|tips|income|ltd duration)",
        "bond",
    ),
    (
        r"\b(emerging|international|china|japan|europe|brazil|india|eafe|developed|"
        r"germany|korea|taiwan|mexico|latin|world ex|ex-us|ex us|frontier)",
        "international",
    ),
    (
        r"\b(select sector|sector|semiconductor|biotech|bank|energy|technology|health|"
        r"financial|real estate|reit|utilities|industrial|materials|consumer|homebuilder|"
        r"retail|aerospace|transport|internet|software|cyber|clean|solar|innovation|"
        r"genomic|metals & mining|oil & gas|regional)",
        "sector",
    ),
)


def etf_class(symbol: str, name: str) -> str:
    """Asset-class tag of an ETF from its fund name (first matching rule wins)."""
    n = name.lower()
    for pat, label in _ETF_RULES:
        if re.search(pat, n):
            return label
    _ = symbol
    return "broad_equity"


def sic_sector(sic: int | None) -> str:
    """A coarse sector from a 4-digit SIC code (SEC Division structure, tech carved out)."""
    if sic is None:
        return "unknown"
    s = int(sic)
    tech = {
        3570,
        3571,
        3572,
        3575,
        3576,
        3577,
        3578,
        3579,
        3661,
        3663,
        3669,
        3670,
        3672,
        3674,
        3675,
        3677,
        3678,
        3679,
        3825,
        3826,
        7370,
        7371,
        7372,
        7373,
        7374,
        7377,
    }
    if s in tech:
        return "technology"
    if 2830 <= s <= 2836 or 3840 <= s <= 3851 or 8000 <= s <= 8099 or s == 5122:
        return "health"
    if s in (4812, 4813, 4822, 4832, 4833, 4841, 4899, 7812, 7819, 7822, 7900, 7990, 2711):
        return "communication"
    if 1000 <= s <= 1499 or 2900 <= s <= 2999 or s in (4610, 4612, 4613, 4922, 4923):
        return "energy_materials"
    if 7300 <= s <= 7399:
        return "business_services"  # payments, platforms, data (7389 NEC)
    if 7800 <= s <= 7849:
        return "communication"
    if 4900 <= s <= 4999:
        return "utilities"
    if 6000 <= s <= 6411 or 6500 <= s <= 6799:
        return "financials_realestate" if s < 6500 or s >= 6700 else "real_estate"
    if 5000 <= s <= 5999 or 7000 <= s <= 7299 or s in (3711, 3714, 3751, 2300, 2320, 3021):
        return "consumer"
    if 2000 <= s <= 2199:
        return "consumer_staples"
    if 1500 <= s <= 1799 or 3400 <= s <= 3569 or 3580 <= s <= 3829 or 4000 <= s <= 4799:
        return "industrials"
    if 2200 <= s <= 3399:
        return "materials_mfg"
    return "other"


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


# ---------------------------------------------------------------------------
# Price / market state (causal series, looked up at the entry day)
# ---------------------------------------------------------------------------


def price_state(px: pd.DataFrame) -> pd.DataFrame:
    """Per-date causal price features of one ticker."""
    c = px["close"].astype(float)
    r = np.log(c / c.shift(1))
    sd20 = r.rolling(20).std()
    out = pd.DataFrame(index=px.index)
    for n in (5, 20, 60):
        out[f"ret_{n}d"] = c / c.shift(n) - 1.0
    for n in (20, 50, 200):
        out[f"dist_ma{n}"] = c / c.rolling(n).mean() - 1.0
    rv20 = sd20 * np.sqrt(252)
    out["rv20_pctile"] = rv20.rolling(252, min_periods=60).rank(pct=True)
    o = px["open"].astype(float) if "open" in px else pd.Series(np.nan, index=px.index)
    out["gap_pct"] = o / c.shift(1) - 1.0
    big = (r.abs() > 2.0 * sd20.shift(1)).to_numpy()
    since, last = [], None
    for i, b in enumerate(big):
        if b:
            last = i
        since.append(np.nan if last is None else float(i - last))
    out["days_since_2sd"] = since
    out["dd_from_high"] = c / c.cummax() - 1.0
    return out


def market_state(
    prices: Mapping[str, pd.DataFrame],
    opt: Mapping[str, pd.DataFrame],
    breadth_names: Iterable[str],
) -> pd.DataFrame:
    """One row per session: SPY trend, VIX proxy (SPY iv30), credit, breadth, dispersion."""
    spy = prices["SPY"]["close"].astype(float)
    m = pd.DataFrame(index=spy.index)
    for n in (20, 50, 200):
        m[f"spy_dist_ma{n}"] = spy / spy.rolling(n).mean() - 1.0
    m["spy_ret_20d"] = spy / spy.shift(20) - 1.0
    m["spy_dd_from_high"] = spy / spy.cummax() - 1.0
    so = opt["SPY"].reindex(m.index)
    iv = so["iv30"].astype(float)
    m["vix_proxy"] = iv
    m["vix_proxy_pctile"] = iv.rolling(252, min_periods=60).rank(pct=True)
    m["vix_proxy_chg_5d"] = iv - iv.shift(5)
    m["spy_term_slope"] = so["term_slope"].astype(float)
    r = np.log(spy / spy.shift(1))
    m["spy_rv_minus_iv"] = r.rolling(20).std() * np.sqrt(252) - iv
    if "HYG" in prices and "TLT" in prices:
        ratio = (prices["HYG"]["close"] / prices["TLT"]["close"]).reindex(m.index)
        m["credit_hyg_tlt_20d"] = ratio / ratio.shift(20) - 1.0
    above, total = pd.Series(0.0, index=m.index), pd.Series(0.0, index=m.index)
    for t in breadth_names:
        if t not in prices:
            continue
        c = prices[t]["close"].astype(float)
        ma = c.rolling(50).mean()
        ok = ma.notna()
        above = above.add(((c > ma) & ok).astype(float).reindex(m.index), fill_value=0.0)
        total = total.add(ok.astype(float).reindex(m.index), fill_value=0.0)
    m["breadth_above_ma50"] = above / total.replace(0.0, np.nan)
    secs = [
        (prices[t]["close"] / prices[t]["close"].shift(20) - 1.0).reindex(m.index).rename(t)
        for t in SECTOR_ETFS
        if t in prices
    ]
    if secs:
        m["sector_dispersion_20d"] = pd.concat(secs, axis=1).std(axis=1)
    # SPY daily history starts 2019-09-03, so the 200-day MA is undefined before
    # mid-June 2020; there the label falls back to the 50-day MA alone (else the
    # Feb-Mar 2020 crash would read "mixed").
    d50, d200 = m["spy_dist_ma50"], m["spy_dist_ma200"]
    m["spy_trend"] = np.where(
        d50.isna(),
        "unknown",
        np.where(
            d200.isna(),
            np.where(d50 > 0, "up", "down"),
            np.where(
                (d50 > 0) & (d200 > 0),
                "up",
                np.where((d50 < 0) & (d200 < 0), "down", "mixed"),
            ),
        ),
    )
    m["vix_bucket"] = pd.cut(
        m["vix_proxy"],
        [0, 0.15, 0.20, 0.30, np.inf],
        labels=["<15", "15-20", "20-30", ">=30"],
        right=False,
    ).astype(str)
    return m


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


def _sessions_between(sessions: list[dt.date], a: dt.date, d: dt.date) -> int:
    """Sessions strictly after *a* up to and including *d* (signed)."""
    import bisect

    i, j = bisect.bisect_right(sessions, a), bisect.bisect_right(sessions, d)
    return j - i


def event_features(
    t: pd.DataFrame,
    earnings: pd.DataFrame,
    macro: Mapping[str, list[dt.date]],
    sessions: list[dt.date],
) -> pd.DataFrame:
    """Earnings and macro columns per trade (entry-time + ``post_*`` hold descriptors)."""
    by_t: dict[str, pd.DataFrame] = {
        k: g.sort_values("event_date") for k, g in earnings.groupby("ticker")
    }
    rows = []
    for r in t.itertuples():
        e0: dt.date = r.entry_date
        planned = r.expiration - dt.timedelta(days=PLANNED_EXIT_DTE)
        f: dict[str, object] = {}
        g = by_t.get(r.underlying)
        if g is not None and not r.etf:
            past = g[g["event_date"] <= e0]
            last = past["event_date"].iloc[-1] if len(past) else None
            f["days_since_earn"] = (e0 - last).days if last else np.nan
            f["days_to_earn_proj"] = ((last + dt.timedelta(days=91)) - e0).days if last else np.nan
            f["earn_proj_in_window"] = (
                bool(last and e0 < last + dt.timedelta(days=91) <= planned) if last else None
            )
            inside = g[(g["event_date"] > e0) & (g["event_date"] <= r.exit_day)]
            f["post_earn_in_hold"] = bool(len(inside))
            if len(inside):
                f["post_earn_implied_move"] = float(inside["implied_move"].iloc[0])
                f["post_earn_realised_move"] = float(inside["realised_move"].iloc[0])
        for k in ("fomc", "cpi", "nfp"):
            ds = macro.get(k, [])
            f[f"{k}_in_window"] = any(e0 < d <= planned for d in ds)
            f[f"{k}_within_2s"] = any(
                0 < _sessions_between(sessions, e0, d) <= 2 for d in ds if d > e0
            )
            f[f"post_{k}_in_hold"] = any(e0 < d <= r.exit_day for d in ds)
        rows.append(f)
    return pd.DataFrame(rows, index=t.index)


# ---------------------------------------------------------------------------
# Portfolio state at entry
# ---------------------------------------------------------------------------


def portfolio_state(
    t: pd.DataFrame,
    equity: pd.Series,
    prices: Mapping[str, pd.DataFrame],
    groups: Mapping[str, str],
    corr_cut: float = 0.7,
) -> pd.DataFrame:
    """Open book at each entry (positions with entry < day < exit, plus same-day opens).

    ``same_cluster_open``: open names whose 60-session log-return correlation with the
    entry name (window ending at the entry day) exceeds *corr_cut*.
    """
    rets = {k: np.log(v["close"] / v["close"].shift(1)) for k, v in prices.items()}
    rows = []
    eq_prev = equity.shift(1).fillna(equity.iloc[0])
    for r in t.itertuples():
        d = r.entry_date
        ob = t[(t["entry_date"] < d) & (t["exit_day"] > d)]
        today = t[(t["entry_date"] == d) & (t.index != r.Index)]
        eq = float(eq_prev.get(d, equity.iloc[0]))
        same_dir = int((ob["direction"] == r.direction).sum()) if r.direction != "neutral" else 0
        grp = groups.get(r.underlying, "unknown")
        names = sorted(set(ob["underlying"]) - {r.underlying})
        clus = 0
        if r.underlying in rets and names:
            a = rets[r.underlying]
            a = a[a.index <= d].tail(60)
            for n in names:
                if n not in rets:
                    continue
                b = rets[n].reindex(a.index)
                ok = a.notna() & b.notna()
                if ok.sum() >= 40 and float(np.corrcoef(a[ok], b[ok])[0, 1]) > corr_cut:
                    clus += 1
        rows.append(
            {
                "open_positions": len(ob),
                "open_risk_pct": float(ob["risk"].sum()) / eq if eq else np.nan,
                "open_same_dir_share": same_dir / len(ob) if len(ob) else 0.0,
                "same_name_open": int((ob["underlying"] == r.underlying).sum()),
                "same_group_open": int(sum(groups.get(n, "?") == grp for n in ob["underlying"])),
                "same_cluster_open": clus,
                "new_risk_today_pct": float(today["risk"].sum() + r.risk) / eq if eq else np.nan,
                "risk_pct_equity": float(r.risk) / eq if eq else np.nan,
                "drawdown_at_entry": float(eq / equity[equity.index < d].cummax().iloc[-1] - 1.0)
                if (equity.index < d).any()
                else 0.0,
            }
        )
    return pd.DataFrame(rows, index=t.index)


# ---------------------------------------------------------------------------
# The table
# ---------------------------------------------------------------------------


def _lookup(frame: pd.DataFrame, day: dt.date) -> pd.Series:
    """Last row of *frame* dated <= *day* (point-in-time), or an all-NaN row."""
    sub = frame[frame.index <= day]
    return sub.iloc[-1] if len(sub) else pd.Series(np.nan, index=frame.columns)


def trade_columns(t: pd.DataFrame) -> pd.DataFrame:
    out = t.copy()
    out["R"] = out["pnl_unit"] / out["max_loss_unit"]
    out["R_mid"] = (out["pnl_mid"] / out["contracts"]) / out["max_loss_unit"]
    out["loser_05R"] = out["R"] <= -0.5
    spec = out["spec"].astype(str)
    out["delta"] = [float(m[1]) / 100 if (m := re.search(r"_d(\d+)", s)) else np.nan for s in spec]
    out["width"] = out["credit_width"].fillna(out["debit_width"])
    out["dte_entry"] = [
        (e - d).days for e, d in zip(out["expiration"], out["entry_date"], strict=True)
    ]
    out["spread_pct_premium"] = (
        (out["entry_net"] - out["entry_net_mid"]).abs() / 0.25 / out["entry_net_mid"].abs()
    )
    return out


def build_features(
    t: pd.DataFrame,
    equity: pd.Series,
    *,
    prices: Mapping[str, pd.DataFrame],
    opt: Mapping[str, pd.DataFrame],
    market: pd.DataFrame,
    earnings: pd.DataFrame,
    macro: Mapping[str, list[dt.date]],
    groups: Mapping[str, str],
) -> pd.DataFrame:
    """One row per trade of one book (see the module docstring for the PIT contract)."""
    t = trade_columns(t)
    sessions = sorted(market.index)
    pstate = {k: price_state(v) for k, v in prices.items() if k in set(t["underlying"])}
    rows = []
    for r in t.itertuples():
        d = r.entry_date
        f: dict[str, object] = {}
        if r.underlying in pstate:
            f |= _lookup(pstate[r.underlying], d).add_prefix("px_").to_dict()
        if r.underlying in opt:
            f |= _lookup(opt[r.underlying], d).add_prefix("opt_").to_dict()
        f |= (
            _lookup(market.drop(columns=["spy_trend", "vix_bucket"]), d)
            .add_prefix("mkt_")
            .to_dict()
        )
        mrow = market[market.index <= d]
        f["mkt_spy_trend"] = mrow["spy_trend"].iloc[-1] if len(mrow) else "unknown"
        f["mkt_vix_bucket"] = mrow["vix_bucket"].iloc[-1] if len(mrow) else "nan"
        rows.append(f)
    feat = pd.DataFrame(rows, index=t.index)
    ev = event_features(t, earnings, macro, sessions)
    ps = portfolio_state(t, equity, prices, groups)
    out = pd.concat([t, feat, ev, ps], axis=1)
    out["group"] = [groups.get(u, "unknown") for u in out["underlying"]]
    out["weekday"] = [d.strftime("%a") for d in out["entry_date"]]
    out["month"] = [d.month for d in out["entry_date"]]
    out["half"] = [f"{d.year}H{1 if d.month <= 6 else 2}" for d in out["entry_date"]]
    out["gap_day"] = out["px_gap_pct"].abs() >= 0.02
    return out


def regime_change_soon(market: pd.DataFrame, n: int = 10) -> pd.Series:
    """Descriptive (uses the future label): SPY trend label changes within *n* sessions."""
    lab = market["spy_trend"]
    chg = lab.ne(lab.shift(-1)).astype(int)[::-1].rolling(n, min_periods=1).max()[::-1]
    return chg.astype(bool).rename("post_regime_change_10s")


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
    "build_features",
    "etf_class",
    "event_features",
    "market_state",
    "portfolio_state",
    "price_state",
    "sector_map",
    "sic_sector",
    "write_features",
]
