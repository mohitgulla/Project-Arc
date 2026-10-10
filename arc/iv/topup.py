"""Nightly ``iv.backfill`` top-up (E16.1, D76): keep IV rank populated for new names.

The active list churns daily (Discovery / Trending), so a one-off backfill goes stale.
Each trading evening, after ``iv.record``, this picks the names that are still short
of ``iv_min_obs_rank`` usable observations in the 252-session lookback and runs the
E4.12 backfill (:func:`arc.iv.backfill.backfill`) on them, capped per run and by wall
time. It never writes a second IV series: it only fills ``alpaca_backfill`` rows.

Selection (deterministic):

* candidates: open underlyings (sorted), then today's active list in its order, then
  SPY / QQQ / IWM; deduplicated, first position wins;
* *short*: fewer than ``min_obs`` days in :meth:`IvStore.series` (both sources) over
  the lookback sessions;
* *exhausted*: every lookback session missing from the series already has an
  ``alpaca_backfill`` row in ``iv_skips`` (the backfill tried and could not rebuild
  it), so a re-run cannot add anything: not selected;
* capped at ``max_tickers``.
"""

from __future__ import annotations

import time
from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from arc.iv.backfill import BackfillReport, backfill
from arc.iv.store import BACKFILL, IvStore

if TYPE_CHECKING:
    import datetime as _dt
    import sqlite3
    from collections.abc import Callable, Mapping, Sequence

    from arc.iv.backfill import OptionHistory

#: Market-reference ETFs always kept full (D56 market_reference).
MARKET_REFERENCE: tuple[str, ...] = ("SPY", "QQQ", "IWM")


@dataclass(frozen=True)
class Coverage:
    """One ticker's IV coverage over the lookback sessions."""

    ticker: str
    usable: int
    missing: int
    unskipped: int  # missing days with no backfill skip recorded (what a run can still try)

    def short(self, min_obs: int) -> bool:
        return self.usable < min_obs


def candidates(open_unds: Sequence[str], active: Sequence[str]) -> list[str]:
    """Open underlyings first, then the active list, then SPY/QQQ/IWM (deduplicated)."""
    names = [*sorted(open_unds), *active, *MARKET_REFERENCE]
    return list(dict.fromkeys(t.strip().upper() for t in names if t and t.strip()))


def coverage(store: IvStore, ticker: str, lookback: Sequence[_dt.date]) -> Coverage:
    """Usable series days and missing days (with/without a recorded skip) in *lookback*."""
    want = set(lookback)
    have = want & set(store.series(ticker, until=max(lookback)))
    missing = want - have
    skipped = store.days(ticker, BACKFILL, include_skips=True) - store.days(ticker, BACKFILL)
    return Coverage(ticker.upper(), len(have), len(missing), len(missing - skipped))


def select(
    store: IvStore,
    names: Sequence[str],
    lookback: Sequence[_dt.date],
    *,
    min_obs: int,
    max_tickers: int,
) -> tuple[list[str], list[Coverage], list[Coverage]]:
    """``(picked, short, exhausted)``: short names in *names* order, capped at *max_tickers*.

    ``short`` lists every short name with something left to try (picked or not);
    ``exhausted`` the short names whose every missing day is a recorded skip.
    """
    short: list[Coverage] = []
    exhausted: list[Coverage] = []
    for t in names:
        c = coverage(store, t, lookback)
        if not c.short(min_obs):
            continue
        (short if c.unskipped else exhausted).append(c)
    return [c.ticker for c in short[:max_tickers]], short, exhausted


@dataclass
class TopupResult:
    picked: list[str]
    short: list[Coverage]
    exhausted: list[Coverage]
    report: BackfillReport | None
    still_short: list[str] = field(default_factory=list)

    @property
    def filled(self) -> int:
        """Tickers that gained at least one stored day this run."""
        return sum(1 for t in self.report.tickers if t.stored) if self.report else 0

    @property
    def days_added(self) -> int:
        return sum(t.stored for t in self.report.tickers) if self.report else 0

    @property
    def skip_reasons(self) -> Counter[str]:
        out: Counter[str] = Counter()
        for t in self.report.tickers if self.report else []:
            out.update(t.skipped)
        return out

    def summary(self) -> str:
        """``n tickers filled, n days added, n skipped (reasons), n still short``."""
        if not self.picked:
            return "no short names: IV rank history complete" + (
                f" · {len(self.exhausted)} exhausted (all gaps skipped)" if self.exhausted else ""
            )
        reasons = self.skip_reasons
        why = ", ".join(f"{k} x{v}" for k, v in reasons.most_common(3))
        deferred = self.report.deferred if self.report else []
        return (
            f"{self.filled} tickers filled, {self.days_added} days added, "
            f"{sum(reasons.values())} skipped"
            + (f" ({why})" if why else "")
            + f", {len(self.still_short)} still short"
            + (f" ({', '.join(self.still_short[:8])})" if self.still_short else "")
            + (f" · {len(deferred)} deferred by max_runtime_s" if deferred else "")
            + (f" · {len(self.exhausted)} exhausted" if self.exhausted else "")
        )

    def metrics(self) -> dict[str, Any]:
        rep = self.report
        return {
            "picked": self.picked,
            "short": len(self.short),
            "exhausted": [c.ticker for c in self.exhausted],
            "filled": self.filled,
            "days_added": self.days_added,
            "skipped": dict(self.skip_reasons),
            "still_short": self.still_short,
            "deferred": rep.deferred if rep else [],
            "per_ticker": (
                {
                    t.ticker: {"stored": t.stored, "skipped": sum(t.skipped.values())}
                    for t in rep.tickers
                }
                if rep
                else {}
            ),  # fmt: skip
            "wall_s": round(rep.wall_s, 1) if rep else 0.0,
        }


def run_topup(
    conn: sqlite3.Connection,
    history_factory: Callable[[], OptionHistory],
    names: Sequence[str],
    *,
    sessions: Sequence[_dt.date],
    lookback: Sequence[_dt.date],
    now: _dt.datetime,
    min_obs: int,
    max_tickers: int,
    max_runtime_s: float,
    r: float,
    dividend_yields: Mapping[str, float],
    clock: Callable[[], float] = time.monotonic,
) -> TopupResult:
    """Select the short names and backfill them over *sessions* (``since`` .. yesterday).

    *history_factory* is only called when something is picked (no Alpaca client, and
    no key needed, on a quiet night).
    """
    store = IvStore(conn)
    picked, short, exhausted = select(
        store, names, lookback, min_obs=min_obs, max_tickers=max_tickers
    )
    if not picked:
        return TopupResult(picked, short, exhausted, None)
    rep = backfill(
        conn,
        history_factory(),
        picked,
        sessions,
        now=now,
        r=r,
        dividend_yields=dividend_yields,
        clock=clock,
        max_runtime_s=max_runtime_s,
    )
    still = [t for t in picked if coverage(store, t, lookback).short(min_obs)]
    return TopupResult(picked, short, exhausted, rep, still_short=still)
