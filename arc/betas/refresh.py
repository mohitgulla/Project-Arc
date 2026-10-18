"""The ``betas`` routine / ``arc betas refresh`` (E3.6, D62; E28.4, D131).

For each ticker (active list + open underlyings + SPY/QQQ/IWM): about
:data:`LOOKBACK_DAYS` calendar days of daily closes from
:meth:`MarketDataProvider.history_bars`, beta vs SPY on aligned dates
(:func:`arc.features.beta.beta_vs`), one ``betas`` row per (ticker, day). Every
request first takes a slot from the shared Alpaca data budget
(``routine_state[alpaca_data:calls]``, as :mod:`arc.iv.alpaca_history`).

E28.4 (D131): the same closes also give every pair's 60-session log-return
correlation (:func:`arc.betas.corr_store.compute_rows`), stored in
``correlations`` (that day's rows replaced), so the cluster cap needs no second
bars pull. The live provider reads split/dividend-adjusted bars
(:data:`BARS_ADJUSTMENT`), so a split is not a one-day -50% return.
"""

from __future__ import annotations

import datetime as _dt
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import structlog

from arc.betas.corr import CorrMatrix
from arc.betas.corr_store import CorrRow, compute_rows, replace_day
from arc.betas.store import BetaRow, row_from_result, upsert
from arc.features.beta import BENCHMARK, MIN_DAYS, WINDOW, beta_vs
from arc.features.correlation import MIN_OBS as CORR_MIN_OBS
from arc.features.correlation import WINDOW as CORR_WINDOW
from arc.utils.calendar import ET

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable, Sequence

    from arc.data.base import MarketDataProvider

log = structlog.get_logger(__name__)

#: ~400 calendar days cover 252 aligned sessions plus holidays and a margin.
LOOKBACK_DAYS = 400
#: D56 market reference: always refreshed (SPY is also the benchmark).
REFERENCE = ("SPY", "QQQ", "IWM")
#: E28.4: the live bars read is split/dividend-adjusted (``AlpacaMarketData``'s
#: ``bars_adjustment``), for both the betas and the correlations.
BARS_ADJUSTMENT = "all"


@dataclass
class RefreshResult:
    day: _dt.date
    rows: list[BetaRow] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)
    calls: int = 0
    corr_rows: list[CorrRow] = field(default_factory=list)
    corr_seconds: float = 0.0

    def top_pairs(self, n: int = 10) -> list[tuple[str, str, float]]:
        """The *n* most correlated stored pairs (descending)."""
        pairs = {(r.a, r.b): r.corr for r in self.corr_rows}
        return CorrMatrix(day=self.day, pairs=pairs).top(n)

    def metrics(self) -> dict[str, Any]:
        return {
            "rows": len(self.rows),
            "errors": len(self.errors),
            "too_few_days": sorted(r.ticker for r in self.rows if r.beta is None),
            "alpaca_calls": self.calls,
            "betas": {r.ticker: r.beta for r in self.rows},
            "correlations": len(self.corr_rows),
            "corr_seconds": round(self.corr_seconds, 3),
            "top_pairs": [[a, b, c] for a, b, c in self.top_pairs(5)],
        }


def refresh_tickers(tickers: Sequence[str]) -> list[str]:
    """*tickers* upper-cased and deduplicated, then SPY/QQQ/IWM (input order kept)."""
    return list(dict.fromkeys([*(t.upper() for t in tickers), *REFERENCE]))


def refresh_betas(
    conn: sqlite3.Connection,
    market: MarketDataProvider,
    tickers: Sequence[str],
    day: _dt.date,
    *,
    now: _dt.datetime,
    take: Callable[[], object] | None = None,
    window: int = WINDOW,
    min_days: int = MIN_DAYS,
    corr_window: int = CORR_WINDOW,
    corr_min_obs: int = CORR_MIN_OBS,
    clock: Callable[[], float] = time.perf_counter,
) -> RefreshResult:
    """Compute and store *day*'s beta vs SPY and pair correlations for *tickers*.

    Closes are taken strictly before *day* (the routine runs pre-market, so *day*'s
    bar does not exist yet; a by-hand run mid-session must not use a partial bar).
    A SPY fetch failure aborts the run (no benchmark); one ticker's failure is
    recorded in ``errors`` and the rest continue (that ticker gets no beta row and
    no pairs). *clock* only times the correlation step (``corr_seconds``).
    """
    out = RefreshResult(day=day)
    start, end = day - _dt.timedelta(days=LOOKBACK_DAYS), day - _dt.timedelta(days=1)

    def closes(t: str) -> dict[_dt.date, float]:
        if take is not None:
            take()
        out.calls += 1
        bars = market.history_bars(t, start, end)
        return {b.timestamp.astimezone(ET).date(): float(b.close) for b in bars}

    bench = closes(BENCHMARK)
    if not bench:
        msg = f"no {BENCHMARK} closes {start}..{end}: cannot compute betas"
        raise LookupError(msg)
    series: dict[str, dict[_dt.date, float]] = {}
    for t in refresh_tickers(tickers):
        try:
            s = bench if t == BENCHMARK else closes(t)
        except Exception as exc:  # noqa: BLE001 - one ticker must not sink the rest
            out.errors[t] = str(exc)[:300]
            log.warning("betas.fetch_failed", ticker=t, error=str(exc))
            continue
        series[t] = s
        out.rows.append(row_from_result(t, day, beta_vs(s, bench, window, min_days)))
    upsert(conn, out.rows, now=now)
    t0 = clock()
    cut = {t: {d: c for d, c in s.items() if d < day} for t, s in series.items()}
    out.corr_rows = compute_rows(cut, day, window=corr_window, min_obs=corr_min_obs)
    replace_day(conn, day, out.corr_rows, now=now)
    out.corr_seconds = clock() - t0
    log.info(
        "betas.refreshed",
        day=str(day),
        rows=len(out.rows),
        errors=len(out.errors),
        correlations=len(out.corr_rows),
    )
    return out
