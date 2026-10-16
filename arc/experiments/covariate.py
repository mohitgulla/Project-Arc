"""D119 (E25.7): the pre-registered covariate of the experiment primary (SPY's daily return).

``x_t`` = SPY's close-to-close return for session ``t``; on a partial first session
(D117: a spec v3 t0 inside the session) SPY's price at t0 (the open of the 1-min bar
holding t0) to the close. The same ``x_t`` serves every pair of an experiment.

The evaluation stays deterministic and offline-reproducible: every ``x_t`` used is
persisted on the stored report (``series[].spy_ret``); a later evaluation reuses the
stored values for past days and asks the provider only for days it has not stored.
The provider is injected (the routine passes Alpaca's daily bars on the SIP feed,
past its 15-minute window at the 16:40 evaluation); tests pass a fake. A day the
provider cannot price is left out and reported (``covariate_missing``): a
pre-registered adjusted verdict then waits rather than falling back to the
unadjusted CI.

D120 (E25.9): ``vix_chg`` = ΔVIX_t, VIX's close-to-close change in vol points (Cboe's
free ``VIX_History.csv``, the vol_term client), persisted the same way
(``series[].vix_chg``, stored first). It feeds the descriptive 2-covariate check
(:mod:`arc.experiments.covariate_check`) and, only for a spec that pre-registers it,
the primary. The D117 partial first session has no ΔVIX (no intraday VIX on the free
feed): it stays missing and holds nothing.
"""

from __future__ import annotations

import datetime as _dt
import json
import sqlite3
from typing import TYPE_CHECKING, Any, Protocol

import structlog

from arc.utils.calendar import ET, previous_session, session_close, session_open

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping, Sequence

__all__ = [
    "COVARIATE_SYMBOL",
    "VIX_INDEX",
    "BarsProvider",
    "CachedCloses",
    "FeedPinned",
    "cboe_vix_closes",
    "partial_first_day",
    "spy_returns",
    "stored_spy_returns",
    "stored_vix_changes",
    "vix_changes",
]

log = structlog.get_logger(__name__)

COVARIATE_SYMBOL = "SPY"


class Bar(Protocol):
    """The fields of :class:`arc.data.base.HistoryBar` the covariate reads.

    (Structural, so ``arc.experiments`` — imported by the read-only Tower — never
    imports ``arc.data``.)
    """

    @property
    def timestamp(self) -> _dt.datetime: ...
    @property
    def open(self) -> float: ...
    @property
    def close(self) -> float: ...


class BarsProvider(Protocol):
    """The slice of :class:`arc.data.alpaca.AlpacaMarketData` the covariate reads."""

    def history_bars(
        self,
        symbol: str,
        start: _dt.date,
        end: _dt.date,
        timeframe: str = "1Day",
        *,
        feed: Any = None,
    ) -> Sequence[Bar]: ...


class FeedPinned:
    """*market* with every bar read on *feed* (the routine pins ``alpaca_history_feed``)."""

    def __init__(self, market: BarsProvider, *, feed: str) -> None:
        self._market = market
        self._feed = feed

    def history_bars(
        self,
        symbol: str,
        start: _dt.date,
        end: _dt.date,
        timeframe: str = "1Day",
        *,
        feed: Any = None,
    ) -> Sequence[Bar]:
        return self._market.history_bars(
            symbol, start, end, timeframe, feed=self._feed if feed is None else feed
        )


def stored_spy_returns(conn: sqlite3.Connection, experiment_id: str) -> dict[_dt.date, float]:
    """Every ``spy_ret`` the experiment's stored reports carry (latest report wins)."""
    return _stored(conn, experiment_id, "spy_ret")


def stored_vix_changes(conn: sqlite3.Connection, experiment_id: str) -> dict[_dt.date, float]:
    """D120: every ``vix_chg`` the experiment's stored reports carry (latest report wins)."""
    return _stored(conn, experiment_id, "vix_chg")


def _stored(conn: sqlite3.Connection, experiment_id: str, key: str) -> dict[_dt.date, float]:
    out: dict[_dt.date, float] = {}
    try:
        rows = conn.execute(
            "SELECT payload FROM experiment_reports WHERE experiment_id = ? ORDER BY id",
            (experiment_id,),
        ).fetchall()
    except sqlite3.OperationalError:
        return out
    for (payload,) in rows:
        doc = json.loads(payload)
        series = list(doc.get("series") or [])
        for pair in doc.get("pairwise") or []:
            series.extend(pair.get("series") or [])
        for r in series:
            v = r.get(key)
            if v is not None:
                out[_dt.date.fromisoformat(r["day"])] = float(v)
    return out


def _closes(bars: Iterable[Bar]) -> dict[_dt.date, float]:
    return {b.timestamp.astimezone(ET).date(): float(b.close) for b in bars if b.close > 0}


def _t0_price(market: BarsProvider, t0: _dt.datetime) -> float | None:
    """SPY at t0: the open of the first 1-min bar at or after t0 (its session)."""
    day = t0.astimezone(ET).date()
    bars = market.history_bars(COVARIATE_SYMBOL, day, day, "1Min")
    after = sorted(
        (b for b in bars if b.timestamp >= t0.replace(second=0, microsecond=0)),
        key=lambda b: b.timestamp,
    )
    return float(after[0].open) if after and after[0].open > 0 else None


def spy_returns(
    days: list[_dt.date],
    *,
    t0: _dt.datetime,
    partial_first: bool,
    stored: Mapping[_dt.date, float],
    market: BarsProvider | None,
) -> dict[_dt.date, float]:
    """SPY's return for each session in *days* (stored values first, then *market*).

    *partial_first*: the first day is a D117 partial session (t0 inside it). A day the
    provider cannot price (no bar, an error, no provider) is absent from the result.
    """
    out = {d: stored[d] for d in days if d in stored}
    need = [d for d in days if d not in out]
    if not need or market is None:
        return out
    try:
        bars = market.history_bars(COVARIATE_SYMBOL, previous_session(min(need)), max(need))
        closes = _closes(bars)
    except Exception as exc:  # noqa: BLE001 - a fetch failure leaves the days missing
        log.warning("experiments.covariate_fetch_failed", days=len(need), error=str(exc)[:200])
        return out
    for d in need:
        close = closes.get(d)
        if close is None:
            continue
        partial = partial_first and d == days[0] and session_open(d) < t0 < session_close(d)
        if partial:
            try:
                base = _t0_price(market, t0)
            except Exception as exc:  # noqa: BLE001
                log.warning("experiments.covariate_t0_failed", day=str(d), error=str(exc)[:200])
                base = None
        else:
            base = closes.get(previous_session(d))
        if base:
            out[d] = close / base - 1.0
    return out


# ---------------------------------------------------------------------------
# D120 (E25.9): ΔVIX, the descriptive second covariate
# ---------------------------------------------------------------------------

#: D120: the Cboe index whose close-to-close change is ``vix_chg`` (vol points).
VIX_INDEX = "VIX"


def partial_first_day(days: Sequence[_dt.date], *, t0: _dt.datetime, partial_first: bool) -> bool:
    """True when ``days[0]`` is a D117 partial first session (t0 inside it)."""
    if not partial_first or not days:
        return False
    d = days[0]
    return session_open(d) < t0 < session_close(d)


def vix_changes(
    days: list[_dt.date],
    *,
    t0: _dt.datetime,
    partial_first: bool,
    stored: Mapping[_dt.date, float],
    closes: Callable[[], Mapping[_dt.date, float]] | None,
) -> dict[_dt.date, float]:
    """ΔVIX_t = VIX close_t - VIX close of the previous session, for each day in *days*.

    Stored values first; *closes* is called once (at most) for the rest. The D117
    partial first session has no intraday VIX on the free feed, so it is always
    absent (never fetched). A day without both closes, or a fetch failure, is absent.
    """
    skip = days[0] if partial_first_day(days, t0=t0, partial_first=partial_first) else None
    out = {d: stored[d] for d in days if d in stored and d != skip}
    need = [d for d in days if d not in out and d != skip]
    if not need or closes is None:
        return out
    try:
        px = dict(closes())
    except Exception as exc:  # noqa: BLE001 - a fetch failure leaves the days missing
        log.warning("experiments.vix_fetch_failed", days=len(need), error=str(exc)[:200])
        return out
    for d in need:
        c, p = px.get(d), px.get(previous_session(d))
        if c is not None and p is not None:
            out[d] = float(c) - float(p)
    return out


def cboe_vix_closes() -> dict[_dt.date, float]:  # pragma: no cover - live network
    """VIX daily closes from Cboe's free ``VIX_History.csv`` (D3; the vol_term client)."""
    from arc.ingest.options_data import (
        BROWSER_UA,
        CBOE_HISTORY_URL,
        http_get,
        parse_cboe_history,
    )

    body = http_get(CBOE_HISTORY_URL.format(index=VIX_INDEX), BROWSER_UA, retries=2)
    return parse_cboe_history(body.decode("utf-8", "replace"))


class CachedCloses:
    """*fetch* called at most once (one Cboe GET per evaluation, shared by every XP)."""

    def __init__(self, fetch: Callable[[], Mapping[_dt.date, float]]) -> None:
        self._fetch = fetch
        self._value: Mapping[_dt.date, float] | None = None

    def __call__(self) -> Mapping[_dt.date, float]:
        if self._value is None:
            self._value = self._fetch()
        return self._value
