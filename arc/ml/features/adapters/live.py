"""Live adapter: :class:`FeatureInputs` from the live store (E27.1).

Builds the same frames as :mod:`arc.ml.features.adapters.history`, column for column,
from what the live loop stores (``data/arc.db``, opened read-only by the caller):

* **OHLC**: daily bars. Default source ``market_tape`` (the loop's recorded
  ``history_bars(..., "1Day", ...)`` payloads; per date a bar recorded on the history
  feed (SIP) beats any other payload, then the newest payload wins: E27.1a). The
  tape is a pruned cache (days), so a caller with no tape for a ticker passes
  ``bars=`` (e.g. :func:`provider_bars` over ``AlpacaMarketData``), the same call the
  pipeline makes. Alpaca bars are unadjusted: a split inside the window makes price
  features differ from the split-adjusted history store until the window rolls past it.
* **IV30**: the ``iv_daily`` series (``IvStore.series``: forward ``alpaca_cm30`` then
  backfills, the configured precedence). IV rank/percentile are then computed by the same
  :func:`~arc.ml.features.compute.iv_rank_pct` as history.
* **Chain liquidity**: the last ``chain_snapshot`` of each ET session (Cboe delayed):
  ``atm_spread_pct`` and the session-to-date put/call volumes.
* **Forward earnings calendar**: ``raw_docs`` source ``earnings`` (Finnhub); the reaction
  session is the report day for ``bmo``/``dmh`` and the next session otherwise
  (``amc`` or unknown), and a row counts from its ``ingested_at`` ET date.
* **Past earnings events**: the history store's ``earnings_events.parquet`` (the same
  detected events the training rows saw), when *data_dir* has one.

Not available live (registry ``available_live=False``; NaN here): IV60, front IV, term
slope, 25-delta skew/wings, two-sided %, OI (and the OI flag), and the PIT-universe
cross-section (breadth, dispersion, average correlation).
"""

from __future__ import annotations

import datetime as dt
import json
import re
import zlib
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np
import pandas as pd

from arc.iv.store import IvStore
from arc.ml.features.adapters.history import (
    MACRO_FILE,
    WARMUP_SESSIONS,
    load_events,
    load_macro,
    session_calendar,
    warmup_start,
)
from arc.ml.features.compute import OHLC_INPUT_COLS, OPTION_INPUT_COLS, FeatureInputs
from arc.utils.calendar import ET, next_session, sessions_between

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable, Iterable

    from arc.data.base import MarketDataProvider

_DAILY_CALL = re.compile(
    r'^history_bars\(\["(?P<t>[^"]+)", "[^"]+", "[^"]+", "1Day"(?:, "(?P<feed>[^"]+)")?\]\)$'
)
#: The feed whose tape bars win per date (``data.alpaca_history_feed`` default, D98).
HISTORY_FEED = "sip"
_EARN_TEXT = re.compile(r"Earnings report for (?P<t>\S+) on (?P<d>\d{4}-\d{2}-\d{2})\.")
_EARN_HOUR = re.compile(r"Reporting: (?P<h>\w+)")


def _et_date(ts: str) -> dt.date:
    return pd.Timestamp(ts).tz_convert(ET).date()


def _bars_frame(rows: Iterable[dict[str, Any]]) -> pd.DataFrame:
    recs = [
        {"date": _et_date(r["timestamp"]), **{k: float(r[k]) for k in OHLC_INPUT_COLS}}
        for r in rows
    ]
    if not recs:
        return pd.DataFrame(columns=list(OHLC_INPUT_COLS))
    df = pd.DataFrame(recs).drop_duplicates("date", keep="last").set_index("date")
    return df.sort_index()


def tape_bars(
    conn: sqlite3.Connection, tickers: Iterable[str], *, feed: str = HISTORY_FEED
) -> dict[str, pd.DataFrame]:
    """Daily OHLC per ticker from every recorded ``history_bars`` 1Day payload.

    E27.1a: per date, a bar from a payload recorded on *feed* (the history feed, SIP by
    default: the regime technicals' read, the same consolidated bars as the history
    store) beats a bar from any other payload. The others are mostly the feed-less
    10-day spot reads of :func:`arc.data.base.market_spot` on the quote feed (IEX),
    which the loop records just after the SIP read, so "newest wins" alone handed the
    features one-exchange bars (AMD 2026-10-09 close 607.86 IEX vs 608.10 SIP). Within
    a tier the newest recording wins; a date only a non-*feed* payload has keeps it.
    """
    want = set(tickers)
    rows = conn.execute(
        "SELECT call, payload, at FROM market_tape WHERE call LIKE 'history_bars(%\"1Day\"%' "
        "ORDER BY at"
    ).fetchall()
    other: dict[str, list[dict[str, Any]]] = {}
    preferred: dict[str, list[dict[str, Any]]] = {}
    for call, payload, _at in rows:
        m = _DAILY_CALL.match(call)
        if not m or m.group("t") not in want:
            continue
        data = json.loads(zlib.decompress(payload))
        acc = preferred if m.group("feed") == feed else other
        acc.setdefault(m.group("t"), []).extend(data)  # later recordings win per date
    names = sorted(set(other) | set(preferred))
    return {t: _bars_frame([*other.get(t, []), *preferred.get(t, [])]) for t in names}


def provider_bars(provider: MarketDataProvider) -> Callable[[str, dt.date, dt.date], pd.DataFrame]:
    """A ``bars=`` callable over a live market-data provider (network)."""

    def fetch(ticker: str, start: dt.date, end: dt.date) -> pd.DataFrame:
        bars = provider.history_bars(ticker, start, end, "1Day")
        return _bars_frame(b.model_dump(mode="json") for b in bars)

    return fetch


def iv_frame(conn: sqlite3.Connection, ticker: str, end: dt.date) -> pd.Series:
    s = IvStore(conn).series(ticker, until=end)
    return pd.Series(s, dtype=float)


def snapshot_frame(conn: sqlite3.Connection, ticker: str, end: dt.date) -> pd.DataFrame:
    """Last ``chain_snapshot`` per ET session: atm_spread_pct, opt_volume, put_call_vol."""
    rows = conn.execute(
        "SELECT payload, created_at FROM context_entries WHERE kind = 'chain_snapshot' "
        "AND subject = ? ORDER BY created_at",
        (ticker,),
    ).fetchall()
    out: dict[dt.date, dict[str, float]] = {}
    for payload, created in rows:
        d = _et_date(created)
        if d > end:
            continue
        p = json.loads(payload)
        c, pv = float(p.get("call_volume_td") or 0), float(p.get("put_volume_td") or 0)
        out[d] = {
            "atm_spread_pct": np.nan
            if p.get("atm_spread_pct") is None
            else float(p["atm_spread_pct"]),
            "opt_volume": c + pv,
            "put_call_vol": pv / c if c > 0 else np.nan,
        }
    return pd.DataFrame.from_dict(out, orient="index")


def upcoming_earnings(
    conn: sqlite3.Connection, tickers: Iterable[str], end: dt.date
) -> pd.DataFrame:
    """Forward Finnhub calendar rows ``ticker, reaction_date, known_at`` known by *end*."""
    want = set(tickers)
    rows = conn.execute(
        "SELECT text, ingested_at FROM raw_docs WHERE source = 'earnings' ORDER BY ingested_at"
    ).fetchall()
    recs = []
    for text, ingested in rows:
        m = _EARN_TEXT.search(text or "")
        if not m or m.group("t") not in want:
            continue
        known = _et_date(ingested)
        if known > end:
            continue
        day = dt.date.fromisoformat(m.group("d"))
        h = _EARN_HOUR.search(text)
        hour = h.group("h") if h else None
        before_open = hour in ("bmo", "dmh")
        react = day if before_open and _is_session(day) else next_session(day)
        recs.append({"ticker": m.group("t"), "reaction_date": react, "known_at": known})
    df = pd.DataFrame(recs, columns=["ticker", "reaction_date", "known_at"])
    # one row per (ticker, reaction day): the earliest time it was known
    return (
        df.sort_values("known_at")
        .drop_duplicates(["ticker", "reaction_date"])
        .sort_values(["ticker", "reaction_date"])
        .reset_index(drop=True)
    )


def _is_session(d: dt.date) -> bool:
    return bool(sessions_between(d, d))


def live_inputs(
    conn: sqlite3.Connection,
    tickers: Iterable[str],
    start: dt.date,
    end: dt.date,
    *,
    bars: Callable[[str, dt.date, dt.date], pd.DataFrame] | None = None,
    data_dir: Path | str | None = None,
    etfs: frozenset[str] = frozenset(),
    market_ref: str = "SPY",
    macro_file: Path = MACRO_FILE,
    warmup_sessions: int = WARMUP_SESSIONS,
    history_feed: str = HISTORY_FEED,
) -> FeatureInputs:
    """Inputs for ``compute_panel(dates in [start, end])`` from the live store.

    OHLC comes from the tape; tickers the tape lacks are fetched with *bars* when given.
    """
    lo = warmup_start(start, warmup_sessions)
    names = sorted({*tickers, market_ref})
    ohlc = tape_bars(conn, names, feed=history_feed)
    for t in names:
        have = ohlc.get(t)
        if bars is not None and (have is None or have.empty or have.index.min() > lo):
            ohlc[t] = bars(t, lo, end)
    ohlc = {t: df[(df.index >= lo) & (df.index <= end)] for t, df in ohlc.items() if len(df)}
    opt: dict[str, pd.DataFrame] = {}
    for t in names:
        iv = iv_frame(conn, t, end)
        snap = snapshot_frame(conn, t, end)
        idx = sorted(set(iv.index) | set(snap.index))
        f = pd.DataFrame(index=idx, columns=list(OPTION_INPUT_COLS), dtype=float)
        if len(iv):
            f["iv30"] = iv.reindex(idx)
        for c in snap.columns:
            f[c] = snap[c].reindex(idx)
        opt[t] = f[(f.index >= lo) & (f.index <= end)]
    events = (
        load_events(Path(data_dir), names, end)
        if data_dir is not None
        else pd.DataFrame(columns=["ticker", "event_date", "realised_over_implied"])
    )
    return FeatureInputs(
        ohlc=ohlc,
        opt=opt,
        events=events,
        upcoming=upcoming_earnings(conn, names, end),
        macro=load_macro(macro_file),
        members={},
        etfs=etfs,
        sessions=session_calendar(lo, end),
        market_ref=market_ref,
    )


def live_book(conn: sqlite3.Connection) -> pd.DataFrame:
    """Open structures as the ``book`` of :func:`~arc.ml.features.compute.portfolio_row`.

    ``direction`` is the sign of the structure's net delta at open; ``risk`` = max loss
    x contracts.
    """
    rows = conn.execute(
        "SELECT ticker, structure_json, contracts FROM open_structures WHERE status = 'open'"
    ).fetchall()
    recs = []
    for ticker, sj, contracts in rows:
        s = json.loads(sj)
        delta = float((s.get("greeks") or {}).get("delta") or 0.0)
        direction = "bull" if delta > 1e-9 else "bear" if delta < -1e-9 else "neutral"  # noqa: PLR2004
        recs.append(
            {
                "underlying": ticker,
                "direction": direction,
                "risk": float(s.get("max_loss") or 0.0) * int(contracts),
            }
        )
    return pd.DataFrame(recs, columns=["underlying", "direction", "risk"])
