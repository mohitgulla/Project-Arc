"""``arc iv backfill`` (E4.12, D55): 30-DTE IV rebuilt from Alpaca option daily bars.

For each session day of a ticker:

1. the underlying's daily close ``S``;
2. the two listed expiries bracketing 30 calendar DTE (7..75 DTE window), the
   one just at/below and the one just at/above 30;
3. per expiry, the call and the put at the strike nearest ``S`` (a strike listed
   for both); each leg needs a daily bar with ``volume > 0`` on that day;
4. Black-Scholes inversion of each close (``arc.pricing.bs.implied_volatility``,
   ``t = DTE/365``, ``r = scanner_risk_free_rate``, ``q`` from
   ``iv_dividend_yields`` for ETFs, 0 for stocks); call and put are averaged;
5. total-variance interpolation to 30 DTE, exactly as
   :func:`arc.features.vol.atm_iv_from_chain` (:func:`constant_maturity_iv`).

A day with no usable bracket (an untraded leg, a failed inversion) is recorded in
``iv_skips`` with its reason. Re-runs skip days already stored or skipped (resumable).

Known bias: option and stock closes are not simultaneous, and daily-bar closes are
last trades, not mids, so single days are noisy (about +/-2 vol pts). IV rank /
percentile over 252 days are robust to that.
"""

from __future__ import annotations

import datetime as _dt
import math
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

import structlog

from arc.features.vol import TARGET_DTE, constant_maturity_iv
from arc.iv.record import CHAIN_DTE_MAX, CHAIN_DTE_MIN
from arc.iv.store import BACKFILL, IvRow, IvStore

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable, Iterable, Mapping, Sequence

log = structlog.get_logger(__name__)

YEAR_DAYS = 365.0
#: Underlying-close band (as a share of the period's min/max close) for contract lookups.
STRIKE_BAND = 0.12
#: Expiry window per contracts lookup (keeps each listing request small).
LISTING_CHUNK_DAYS = 31
#: Symbols per option-bars request.
BARS_BATCH = 100
#: Expiries tried per side of the 30-DTE bracket, nearest first. SPY/QQQ list daily
#: (Mon-Thu) expiries that barely trade until close to expiry, so the nearest one or
#: three are often untraded; six reaches the next standard Friday weekly.
FALLBACK_EXPIRIES = 6
#: Strikes tried per expiry, nearest the close first, within ATM_BAND of it. The
#: contracts list holds every strike ever listed, so the nearest one may have been
#: added after *day* (e.g. AAPL 257.5 on 2025-10-03) and has no bar that day.
ATM_STRIKES = 4
ATM_BAND = 0.03


@dataclass(frozen=True)
class ListedContract:
    symbol: str
    expiration: _dt.date
    strike: float
    kind: str  # "c" | "p"


@dataclass(frozen=True)
class DailyBar:
    close: float
    volume: float


class OptionHistory(Protocol):
    """Historical option data for the backfill (Alpaca in production, fakes in tests)."""

    def underlying_closes(
        self, ticker: str, start: _dt.date, end: _dt.date
    ) -> dict[_dt.date, float]:
        """Daily closes of the underlying in [start, end]."""
        ...

    def contracts(
        self,
        ticker: str,
        exp_start: _dt.date,
        exp_end: _dt.date,
        strike_lo: float,
        strike_hi: float,
    ) -> list[ListedContract]:
        """Listed contracts (active and expired) in the expiry/strike window."""
        ...

    def daily_bars(
        self, symbols: Sequence[str], start: _dt.date, end: _dt.date
    ) -> dict[str, dict[_dt.date, DailyBar]]:
        """Daily bars per option symbol in [start, end] (absent = never traded)."""
        ...


@dataclass
class TickerBackfill:
    ticker: str
    stored: int = 0
    skipped: Counter[str] = field(default_factory=Counter)
    already: int = 0
    first: _dt.date | None = None
    last: _dt.date | None = None


@dataclass
class BackfillReport:
    since: _dt.date
    until: _dt.date
    tickers: list[TickerBackfill] = field(default_factory=list)
    wall_s: float = 0.0
    #: E16.1: tickers not started because ``max_runtime_s`` ran out (next run resumes).
    deferred: list[str] = field(default_factory=list)


def implied_vol(
    price: float, s: float, k: float, dte: int, r: float, q: float, kind: str
) -> float | None:
    """BS implied vol of one option close (``None`` when it can't be inverted)."""
    from arc.pricing.bs import implied_volatility

    if price <= 0 or dte <= 0:
        return None
    try:
        v = implied_volatility(price, s, k, dte / YEAR_DAYS, r, q, kind)
    except ValueError:
        return None
    return v if math.isfinite(v) and 0.01 <= v <= 5.0 else None


def bracket_expiries(
    expiries: Iterable[_dt.date], day: _dt.date, *, target_dte: int = TARGET_DTE
) -> tuple[list[_dt.date], list[_dt.date]]:
    """``(below, above)``: expiries in the 7..75 DTE window at/below and at/above
    *target_dte*, each nearest first (later ones are fallbacks when a leg didn't trade).
    """
    window = sorted(e for e in set(expiries) if CHAIN_DTE_MIN <= (e - day).days <= CHAIN_DTE_MAX)
    below = [e for e in window if (e - day).days <= target_dte][::-1]
    above = [e for e in window if (e - day).days >= target_dte]
    return below, above


def _expiry_iv(
    by_exp: Mapping[tuple[float, str], ListedContract],
    bars: Mapping[str, Mapping[_dt.date, DailyBar]],
    day: _dt.date,
    exp: _dt.date,
    s: float,
    r: float,
    q: float,
) -> tuple[float | None, str, dict[str, object]]:
    """ATM IV of one expiry on *day*: the nearest strike to *s* whose call and put
    both traded (closes averaged), trying up to ``ATM_STRIKES`` within ``ATM_BAND``."""
    dte = (exp - day).days
    strikes = _atm_strikes(by_exp, s)
    if not strikes:
        return None, "no strike listed with both a call and a put near the close", {}
    why = ""
    legs: dict[str, object] = {}
    for k in strikes:
        ivs: list[float] = []
        legs = {"strike": k, "dte": dte}
        for kind in ("c", "p"):
            c = by_exp[(k, kind)]
            bar = bars.get(c.symbol, {}).get(day)
            if bar is None or bar.volume <= 0:
                why = f"no traded {('call', 'put')[kind == 'p']} bar near the ATM strike"
                break
            iv = implied_vol(bar.close, s, k, dte, r, q, kind)
            if iv is None:
                why = "IV inversion failed (close below intrinsic or out of range)"
                break
            legs[kind] = {
                "symbol": c.symbol,
                "close": bar.close,
                "volume": bar.volume,
                "iv": round(iv, 6),
            }
            ivs.append(iv)
        if len(ivs) == 2:
            return sum(ivs) / 2, "", legs
    return None, why, legs


def _atm_strikes(by_exp: Mapping[tuple[float, str], ListedContract], s: float) -> list[float]:
    both = {k for (k, _) in by_exp if (k, "c") in by_exp and (k, "p") in by_exp}
    near = sorted(both, key=lambda x: (abs(x - s), x))
    band = [k for k in near if abs(k - s) <= ATM_BAND * s] or near[:1]
    return band[:ATM_STRIKES]


def iv30_for_day(
    listed: Mapping[_dt.date, Mapping[tuple[float, str], ListedContract]],
    bars: Mapping[str, Mapping[_dt.date, DailyBar]],
    day: _dt.date,
    s: float,
    *,
    r: float,
    q: float,
    target_dte: int = TARGET_DTE,
) -> tuple[float | None, str, dict[str, object]]:
    """``(iv30, skip_reason, detail)`` for one day; iv30 ``None`` means skipped.

    Each side of the bracket uses the nearest expiry whose ATM legs both traded,
    walking outward when they didn't. A missing side is a skip unless an expiry sits
    exactly on *target_dte* (no extrapolation from a one-sided bracket).
    """
    below, above = bracket_expiries(listed, day, target_dte=target_dte)
    if not below and not above:
        return None, "no listed expiry in the 7-75 DTE window", {}
    points: list[tuple[int, float]] = []
    detail: dict[str, object] = {}
    reasons: list[str] = []
    for side, exps in (("lo", below), ("hi", above)):
        for exp in exps[:FALLBACK_EXPIRIES]:
            iv, why, legs = _expiry_iv(listed[exp], bars, day, exp, s, r, q)
            if iv is not None:
                points.append(((exp - day).days, iv))
                detail[side] = {"expiration": exp.isoformat(), **legs}
                break
            reasons.append(f"{exp}: {why}")
    dtes = {d for d, _ in points}
    have_lo = any(d <= target_dte for d in dtes)
    have_hi = any(d >= target_dte for d in dtes)
    if not (have_lo and have_hi):
        why = reasons[0] if reasons else "no expiry on one side of 30 DTE"
        return None, f"incomplete 30-DTE bracket ({why})", detail
    return constant_maturity_iv(sorted(set(points)), target_dte=target_dte), "", detail


def _chunks(start: _dt.date, end: _dt.date, days: int) -> list[tuple[_dt.date, _dt.date]]:
    out = []
    cur = start
    while cur <= end:
        nxt = min(cur + _dt.timedelta(days=days - 1), end)
        out.append((cur, nxt))
        cur = nxt + _dt.timedelta(days=1)
    return out


def backfill_ticker(
    conn: sqlite3.Connection,
    history: OptionHistory,
    ticker: str,
    sessions: Sequence[_dt.date],
    *,
    now: _dt.datetime,
    r: float,
    q: float,
    progress: Callable[[str], None] | None = None,
) -> TickerBackfill:
    """Backfill *ticker* over *sessions* (skips days already stored or skipped)."""
    ticker = ticker.upper()
    store = IvStore(conn)
    res = TickerBackfill(ticker)
    done = store.days(ticker, BACKFILL, include_skips=True)
    todo = [d for d in sessions if d not in done]
    res.already = len(sessions) - len(todo)
    if not todo:
        return res
    closes = history.underlying_closes(ticker, todo[0], todo[-1])
    days = [d for d in todo if d in closes and closes[d] > 0]
    for d in todo:
        if d not in closes or closes[d] <= 0:
            store.skip(ticker, d, BACKFILL, "no underlying close", now=now)
            res.skipped["no underlying close"] += 1
    if not days:
        return res

    # Listed contracts, chunked by expiry window, strikes around that window's closes.
    listed: dict[_dt.date, dict[tuple[float, str], ListedContract]] = defaultdict(dict)
    exp_lo = days[0] + _dt.timedelta(days=CHAIN_DTE_MIN)
    exp_hi = days[-1] + _dt.timedelta(days=CHAIN_DTE_MAX)
    for a, b in _chunks(exp_lo, exp_hi, LISTING_CHUNK_DAYS):
        near = [
            closes[d]
            for d in days
            if a - _dt.timedelta(days=CHAIN_DTE_MAX) <= d <= b - _dt.timedelta(days=CHAIN_DTE_MIN)
        ]
        if not near:
            continue
        lo, hi = min(near) * (1 - STRIKE_BAND), max(near) * (1 + STRIKE_BAND)
        for c in history.contracts(ticker, a, b, lo, hi):
            listed[c.expiration][(c.strike, c.kind)] = c

    # Which option symbols each day needs (both bracket sides, plus fallbacks outward).
    need: set[str] = set()
    for d in days:
        below, above = bracket_expiries(listed, d)
        for exps in (below[:FALLBACK_EXPIRIES], above[:FALLBACK_EXPIRIES]):
            for exp in exps:
                book = listed[exp]
                for k in _atm_strikes(book, closes[d]):
                    need.update({book[(k, "c")].symbol, book[(k, "p")].symbol})
    syms = sorted(need)
    bars: dict[str, dict[_dt.date, DailyBar]] = {}
    for i in range(0, len(syms), BARS_BATCH):
        bars.update(history.daily_bars(syms[i : i + BARS_BATCH], days[0], days[-1]))
        if progress:
            progress(f"{ticker}: bars {min(i + BARS_BATCH, len(syms))}/{len(syms)} symbols")

    rows: list[IvRow] = []
    for d in days:
        iv, why, detail = iv30_for_day(listed, bars, d, closes[d], r=r, q=q)
        if iv is None:
            store.skip(ticker, d, BACKFILL, why, now=now)
            key = why.split(" (")[0]
            res.skipped[key] += 1
            continue
        rows.append(
            IvRow(
                ticker=ticker,
                day=d,
                iv30=iv,
                method="bars_bs_cm30",
                source=BACKFILL,
                spot=closes[d],
                spot_basis="last_close",
                n_contracts=2 * sum(1 for s in ("lo", "hi") if s in detail),
                detail={**detail, "r": r, "q": q},
            )
        )
    res.stored = store.upsert(rows, now=now)
    if rows:
        res.first, res.last = rows[0].day, rows[-1].day
    log.info("iv.backfilled", ticker=ticker, stored=res.stored, skipped=sum(res.skipped.values()))
    return res


def backfill(
    conn: sqlite3.Connection,
    history: OptionHistory,
    tickers: Sequence[str],
    sessions: Sequence[_dt.date],
    *,
    now: _dt.datetime,
    r: float,
    dividend_yields: Mapping[str, float],
    progress: Callable[[str], None] | None = None,
    clock: Callable[[], float] = time.monotonic,
    max_runtime_s: float | None = None,
) -> BackfillReport:
    """Backfill each ticker over *sessions* (days already stored or skipped are not refetched).

    E16.1: with *max_runtime_s*, a ticker is not started once that many seconds have
    passed; it lands in :attr:`BackfillReport.deferred` (each day is stored as it is
    done, so the next run resumes where this one stopped).
    """
    t0 = clock()
    rep = BackfillReport(since=sessions[0], until=sessions[-1]) if sessions else None
    if rep is None:
        msg = "no sessions in the requested range"
        raise ValueError(msg)
    for i, t in enumerate(tickers):
        if max_runtime_s is not None and clock() - t0 >= max_runtime_s:
            rep.deferred = [x.upper() for x in tickers[i:]]
            log.info("iv.backfill_deferred", tickers=rep.deferred, max_runtime_s=max_runtime_s)
            break
        q = float(dividend_yields.get(t.upper(), 0.0))
        try:
            rep.tickers.append(
                backfill_ticker(conn, history, t, sessions, now=now, r=r, q=q, progress=progress)
            )
        except Exception as exc:  # noqa: BLE001 - one ticker's data error must not stop the rest
            log.warning("iv.backfill_failed", ticker=t, error=str(exc))
            tb = TickerBackfill(t.upper())
            tb.skipped[f"error: {str(exc)[:120]}"] += 1
            rep.tickers.append(tb)
        if progress:
            last = rep.tickers[-1]
            progress(f"{last.ticker}: {last.stored} stored, {sum(last.skipped.values())} skipped")
    rep.wall_s = clock() - t0
    return rep


def format_report(rep: BackfillReport) -> str:
    lines = [
        f"IV backfill {rep.since} .. {rep.until} (alpaca_backfill, 30-DTE constant maturity)",
        f"{'ticker':<8}{'stored':>8}{'skipped':>9}{'already':>9}  first .. last   top skip reasons",
    ]
    for t in rep.tickers:
        reasons = ", ".join(f"{k} x{v}" for k, v in t.skipped.most_common(2)) or "-"
        span = f"{t.first} .. {t.last}" if t.first else "-"
        lines.append(
            f"{t.ticker:<8}{t.stored:>8}{sum(t.skipped.values()):>9}{t.already:>9}  "
            f"{span}  {reasons}"
        )
    lines.append(
        f"total stored {sum(t.stored for t in rep.tickers)} · "
        f"skipped {sum(sum(t.skipped.values()) for t in rep.tickers)} · wall {rep.wall_s:.0f} s"
    )
    lines.append(
        "note: option and stock closes are not simultaneous and bar closes are last "
        "trades, not mids: expect about +/-2 vol pts of daily noise (rank/percentile "
        "over 252 days is robust to it)."
    )
    return "\n".join(lines)
