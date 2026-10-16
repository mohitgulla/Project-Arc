"""``theta_backfill`` IV rows from the cached ThetaData history (E7.13, D84).

Lives in :mod:`arc.backtest` (not :mod:`arc.iv`): it reads the backtest cache through
the backtest code, and ``arc.iv`` may not import that layer (import-linter).

Seeds live IV rank for any of the C500 names from day one. Two inputs, one method (the
live cm30 code via :mod:`arc.backtest.ivhist`, the E7.16 path):

1. ``data/option_stats/daily/underlying=<T>.parquet`` (E7.16): its ``iv30`` per session
   (computed from the quoted NBBO chains with the live ``atm_term_points`` + cm30).
   Reused as-is rather than recomputed (card comment, E7.16 PR #233).
2. Cached ThetaData sessions **after** that file's last date (the nightly free-tier
   top-up appends them): IV30 computed now with
   :func:`arc.backtest.option_stats.ticker_stats` over just those sessions (same code
   path, so the number matches what E7.16 would write).

Append-only and idempotent per ``(ticker, day, 'theta_backfill')``: a day already
stored (or recorded in ``iv_skips`` as not computable) is never rewritten. A cached
session with a close and option rows but no IV30 (no quoted expiry in 7–75 DTE) is
recorded as a skip; one whose underlying close or option rows aren't cached (yet) is
left for the next run.
No network, no LLM; the caller owns the connection.
"""

from __future__ import annotations

import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pandas as pd
import structlog

from arc.iv.store import THETA, IvRow, IvStore

if TYPE_CHECKING:
    import datetime as _dt
    import sqlite3
    from collections.abc import Callable, Sequence

    from arc.backtest.costs import CostModel
    from arc.backtest.option_stats import StatsConfig

log = structlog.get_logger(__name__)

PROVIDER = "thetadata"
METHOD_STATS = "theta_quoted_cm30"  # from option_stats/daily (E7.16)
METHOD_TOPUP = "theta_quoted_cm30_topup"  # computed here for sessions after that file


@dataclass
class TickerSeed:
    ticker: str
    from_stats: int = 0
    computed: int = 0
    skipped: Counter[str] = field(default_factory=Counter)
    error: str | None = None

    @property
    def stored(self) -> int:
        return self.from_stats + self.computed


@dataclass
class SeedReport:
    tickers: list[TickerSeed] = field(default_factory=list)
    deferred: list[str] = field(default_factory=list)
    wall_s: float = 0.0

    @property
    def stored(self) -> int:
        return sum(t.stored for t in self.tickers)

    @property
    def errors(self) -> list[TickerSeed]:
        return [t for t in self.tickers if t.error]

    def metrics(self) -> dict[str, Any]:
        skips: Counter[str] = Counter()
        for t in self.tickers:
            skips.update(t.skipped)
        return {
            "tickers": len(self.tickers),
            "rows": self.stored,
            "from_stats": sum(t.from_stats for t in self.tickers),
            "computed": sum(t.computed for t in self.tickers),
            "skipped": dict(skips),
            "errors": {t.ticker: t.error for t in self.errors},
            "deferred": len(self.deferred),
            "wall_s": round(self.wall_s, 1),
        }

    def summary(self) -> str:
        m = self.metrics()
        out = (
            f"theta_backfill: {m['rows']} rows ({m['from_stats']} from option_stats, "
            f"{m['computed']} computed) for {m['tickers']} tickers"
        )
        if m["skipped"]:
            out += f", {sum(m['skipped'].values())} skipped"
        if m["errors"]:
            out += f", {len(m['errors'])} errors"
        if self.deferred:
            out += f", {len(self.deferred)} deferred by max_runtime_s"
        return out


def stats_config(ranking_path: Path | str | None = None) -> tuple[StatsConfig, CostModel]:
    """The E7.16 :class:`StatsConfig` + cost model (the ``arc history stats`` settings)."""
    from arc.backtest.costs import load_cost_model
    from arc.backtest.option_stats import StatsConfig
    from arc.backtest.ranking import load_ranking_file
    from arc.config import ArcSettings

    cfg = load_ranking_file(ranking_path)
    stats = StatsConfig(
        provider=PROVIDER,
        data=cfg.backtest.data,
        stage2=cfg.backtest.universe.stage2,
        flat_rate=cfg.backtest.risk_free_rate,
        min_iv_obs=ArcSettings.model_fields["iv_min_obs_rank"].default,
    )
    return stats, load_cost_model()


def _f(v: object) -> float | None:
    try:
        x = float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return x if pd.notna(x) else None


def _why_missing(r: Any) -> str:
    """Why a session has no IV30: ``no_close`` / ``no_options`` (both retried later, no
    ``iv_skips`` row: the input may still arrive) or ``no_iv30`` (final: a skip)."""
    if _f(getattr(r, "spot", None)) is None:
        return "no_close"
    if not (_f(getattr(r, "rows", None)) or 0):
        return "no_options"
    return "no_iv30"


def _row(ticker: str, day: _dt.date, r: Any, method: str) -> IvRow | None:
    iv = _f(getattr(r, "iv30", None))
    if iv is None or iv <= 0:
        return None
    n = getattr(r, "iv30_expiries", None)
    return IvRow(
        ticker=ticker,
        day=day,
        iv30=iv,
        method=method,
        source=THETA,
        spot=_f(getattr(r, "spot", None)),
        spot_basis="last_close",
        n_contracts=int(n) if n is not None and pd.notna(n) else None,
        detail={"provider": PROVIDER, "expiries": int(n) if n is not None and pd.notna(n) else 0},
    )


def seed_ticker(
    conn: sqlite3.Connection,
    data_dir: Path,
    ticker: str,
    *,
    now: _dt.datetime,
    since: _dt.date | None = None,
    until: _dt.date | None = None,
    cfg: StatsConfig | None = None,
    cost: CostModel | None = None,
) -> TickerSeed:
    """Store every missing ``theta_backfill`` day of *ticker* in ``[since, until]``."""
    from arc.backtest.option_stats import read_daily, ticker_stats
    from arc.data.history.store import ParquetHistoryStore

    t = ticker.upper()
    out = TickerSeed(t)
    store = IvStore(conn)
    have = store.days(t, THETA, include_skips=True)

    def inside(d: _dt.date) -> bool:
        return (since is None or d >= since) and (until is None or d <= until)

    rows: list[IvRow] = []
    daily = read_daily(data_dir, t)
    last_stats: _dt.date | None = None
    for r in daily.itertuples(index=False):
        d = r.date
        last_stats = d if last_stats is None or d > last_stats else last_stats
        if d in have or not inside(d):
            continue
        row = _row(t, d, r, METHOD_STATS)
        if row is None:
            why = _why_missing(r)
            out.skipped[why] += 1
            if why == "no_iv30":
                store.skip(t, d, THETA, "no quoted expiry in 7-75 DTE (option_stats)", now=now)
            continue
        rows.append(row)
    out.from_stats = store.upsert(rows, now=now) if rows else 0

    cached = ParquetHistoryStore(data_dir).cached_dates(PROVIDER, t)
    todo = sorted(
        d for d in cached if (last_stats is None or d > last_stats) and d not in have and inside(d)
    )
    if not todo:
        return out
    if cfg is None or cost is None:
        cfg, cost = stats_config()
    res = ticker_stats(data_dir, t, cfg=cfg, cost=cost, start=todo[0], end=todo[-1])
    got = {r.date: r for r in res.daily.itertuples(index=False)}
    rows = []
    for d in todo:
        r = got.get(d)
        row = _row(t, d, r, METHOD_TOPUP) if r is not None else None
        if row is None:
            why = "no_close" if r is None else _why_missing(r)
            out.skipped[why] += 1
            if why == "no_iv30":
                store.skip(t, d, THETA, "no quoted expiry in 7-75 DTE", now=now)
            continue
        rows.append(row)
    out.computed = store.upsert(rows, now=now) if rows else 0
    return out


def seed(
    conn: sqlite3.Connection,
    data_dir: Path | str,
    tickers: Sequence[str],
    *,
    now: _dt.datetime,
    since: _dt.date | None = None,
    until: _dt.date | None = None,
    max_runtime_s: float | None = None,
    clock: Callable[[], float] = time.monotonic,
    progress: Callable[[str], None] | None = None,
    ranking_path: Path | str | None = None,
) -> SeedReport:
    """:func:`seed_ticker` for each name in order; no new name starts after the cap."""
    data_dir = Path(data_dir)
    rep = SeedReport()
    t0 = clock()
    cfg: StatsConfig | None = None
    cost: CostModel | None = None
    names = list(dict.fromkeys(t.strip().upper() for t in tickers if t.strip()))
    for i, t in enumerate(names):
        if max_runtime_s is not None and clock() - t0 >= max_runtime_s:
            rep.deferred = names[i:]
            break
        try:
            if cfg is None:  # lazily: a run with nothing to compute never loads it
                cfg, cost = stats_config(ranking_path)
            one = seed_ticker(
                conn, data_dir, t, now=now, since=since, until=until, cfg=cfg, cost=cost
            )
        except Exception as exc:  # noqa: BLE001 - one bad name never stops the rest
            one = TickerSeed(t, error=f"{type(exc).__name__}: {exc}"[:300])
            log.warning("iv.theta_seed_failed", ticker=t, error=one.error)
        rep.tickers.append(one)
        if progress is not None:
            progress(
                f"{i + 1}/{len(names)} {t}: +{one.stored}"
                + (f" ({one.error})" if one.error else "")
            )
    rep.wall_s = clock() - t0
    log.info("iv.theta_seeded", **rep.metrics())
    return rep
