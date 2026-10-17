"""History adapter: :class:`FeatureInputs` from the backtest data store (E27.1).

Reads (all under ``data/``, gitignored, read-only):

* ``underlying_ohlc/<T>.parquet``: split-adjusted daily OHLC;
* ``option_stats/daily/underlying=<T>.parquet``: the E7.16 per-session option stats;
* ``option_stats/earnings_events.parquet``: detected earnings reaction days;
* ``backtest_universe/<version>.membership.parquet``: the PIT universe per quarter
  (``in_top``), plus the pull list (ETF flags, checkpoint symbols);
* ``config/backtest/macro_calendar_2020_2026.yaml``.

History has no forward earnings calendar, so ``upcoming`` stays empty and
``sess_to_earn`` is always projected (``earn_src = 1``).
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import TYPE_CHECKING

import pandas as pd
import yaml

from arc.ml.features.compute import OHLC_INPUT_COLS, OPTION_INPUT_COLS, FeatureInputs
from arc.utils.calendar import last_known_session, sessions_between

if TYPE_CHECKING:
    from collections.abc import Iterable

REPO = Path(__file__).resolve().parents[4]
MACRO_FILE = REPO / "config" / "backtest" / "macro_calendar_2020_2026.yaml"
#: Sessions of history read before the first requested date (longest window: the
#: 252-row HAR fit + its 21-session target + the 22-session regressors, and the v2 vol
#: percentile's 252 values of rv20).
WARMUP_SESSIONS = 300
#: Calendar days of exchange sessions kept after the last date (sessions to events).
FORWARD_DAYS = 400


def as_dates(s: Iterable[object]) -> list[dt.date]:
    return [x if isinstance(x, dt.date) else pd.Timestamp(x).date() for x in s]


def load_ohlc(data_dir: Path, ticker: str, start: dt.date, end: dt.date) -> pd.DataFrame | None:
    p = data_dir / "underlying_ohlc" / f"{ticker}.parquet"
    if not p.is_file():
        return None
    df = pd.read_parquet(p, columns=["date", *OHLC_INPUT_COLS])
    df["date"] = as_dates(df["date"])
    df = df.dropna(subset=["close"]).drop_duplicates("date").set_index("date").sort_index()
    return df[(df.index >= start) & (df.index <= end)]


def load_option_daily(
    data_dir: Path, ticker: str, start: dt.date, end: dt.date
) -> pd.DataFrame | None:
    p = data_dir / "option_stats" / "daily" / f"underlying={ticker}.parquet"
    if not p.is_file():
        return None
    import pyarrow.parquet as pq

    have = set(pq.read_schema(p).names)
    df = pd.read_parquet(p, columns=["date", *(c for c in OPTION_INPUT_COLS if c in have)])
    df = df.reindex(columns=["date", *OPTION_INPUT_COLS])
    df["date"] = as_dates(df["date"])
    df = df.drop_duplicates("date").set_index("date").sort_index()
    return df[(df.index >= start) & (df.index <= end)]


def load_events(data_dir: Path, tickers: Iterable[str], end: dt.date) -> pd.DataFrame:
    p = data_dir / "option_stats" / "earnings_events.parquet"
    cols = ["ticker", "event_date", "realised_over_implied"]
    if not p.is_file():
        return pd.DataFrame(columns=cols)
    ev = pd.read_parquet(p, columns=cols)
    ev["event_date"] = as_dates(ev["event_date"])
    ev = ev[ev["ticker"].isin(set(tickers)) & (ev["event_date"] <= end)]
    return ev.sort_values(["ticker", "event_date"]).reset_index(drop=True)


def load_macro(path: Path = MACRO_FILE) -> dict[str, list[dt.date]]:
    raw = yaml.safe_load(path.read_text())
    return {k: sorted(as_dates(v)) for k, v in raw.items()}


def load_membership(data_dir: Path, version: str | None = None) -> dict[dt.date, frozenset[str]]:
    """PIT universe (``in_top``) per quarter start of the latest (or *version*) build."""
    from arc.backtest.universe import load_universe

    _, _, m = load_universe(data_dir, version)
    m = m[m["in_top"].astype(bool)]
    out: dict[dt.date, frozenset[str]] = {}
    for q, g in m.groupby("quarter"):
        out[as_dates([q])[0]] = frozenset(g["symbol"].astype(str))
    return out


def universe_symbols(
    data_dir: Path, checkpoint: str = "c500", version: str | None = None
) -> tuple[list[str], frozenset[str]]:
    """(checkpoint symbols, ETF symbols of the pull list)."""
    from arc.backtest.universe import checkpoint_symbols, load_universe

    _, rows, _ = load_universe(data_dir, version)
    etfs = frozenset(r.symbol for r in rows if r.asset_type == "etf")
    return checkpoint_symbols(rows, checkpoint), etfs


def session_calendar(lo: dt.date, end: dt.date) -> list[dt.date]:
    """Sessions from *lo* to ~:data:`FORWARD_DAYS` after *end* (clamped to the calendar)."""
    hi = min(end + dt.timedelta(days=FORWARD_DAYS), last_known_session())
    return sessions_between(lo, hi)


def warmup_start(start: dt.date, warmup_sessions: int) -> dt.date:
    """The session *warmup_sessions* sessions before *start* (the first input date read)."""
    back = sessions_between(start - dt.timedelta(days=warmup_sessions * 2), start)
    return back[-warmup_sessions - 1] if len(back) > warmup_sessions else back[0]


def history_inputs(
    data_dir: Path | str,
    tickers: Iterable[str],
    start: dt.date,
    end: dt.date,
    *,
    members: dict[dt.date, frozenset[str]] | None = None,
    etfs: frozenset[str] = frozenset(),
    market_ref: str = "SPY",
    macro_file: Path = MACRO_FILE,
    warmup_sessions: int = WARMUP_SESSIONS,
) -> FeatureInputs:
    """Inputs for ``compute_panel(dates in [start, end])`` from the history store.

    Reads every series from ``start - warmup_sessions`` to *end* (nothing after *end*).
    *members* defaults to the PIT universe of the latest build when one exists.
    """
    data_dir = Path(data_dir)
    lo = warmup_start(start, warmup_sessions)
    names = sorted({*tickers, market_ref})
    if members is None:
        try:
            members = load_membership(data_dir)
        except (FileNotFoundError, ValueError):
            members = {}
    breadth = sorted({t for s in members.values() for t in s})
    ohlc: dict[str, pd.DataFrame] = {}
    for t in sorted({*names, *breadth}):
        df = load_ohlc(data_dir, t, lo, end)
        if df is not None and len(df):
            ohlc[t] = df
    opt: dict[str, pd.DataFrame] = {}
    for t in names:
        df = load_option_daily(data_dir, t, lo, end)
        if df is not None and len(df):
            opt[t] = df
    return FeatureInputs(
        ohlc=ohlc,
        opt=opt,
        events=load_events(data_dir, names, end),
        macro=load_macro(macro_file),
        members=members,
        etfs=etfs,
        sessions=session_calendar(lo, end),
        market_ref=market_ref,
    )
