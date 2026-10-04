"""Finnhub per-ticker context (E4.8 / D46): parsers, ticker scope and the fetch loop.

Four free-tier data sets, each stored as a typed context kind keyed by ticker
(never ``raw_doc_ref``, so they never use the Scout's D30 doc budget):

=====================  ==============================  ===================
kind                   endpoint                        cadence (routines)
=====================  ==============================  ===================
``earnings_history``   ``/stock/earnings``             Mon + morning after a report
``insider_activity``   ``/stock/insider-transactions`` trading days
``analyst_recs``       ``/stock/recommendation``       Mon
``fundamentals``       ``/stock/metric?metric=all``    Mon
=====================  ==============================  ===================

The parsers are pure (unit-tested on recorded payloads under
``tests/fixtures/finnhub/``). :func:`fetch_per_ticker` runs one data set over the
scope on the shared :class:`~arc.ingest.finnhub.FinnhubClient` and maps errors to
the E4.1d outcomes: a 403 or 429 aborts the run (raised), any other per-ticker error
is recorded in ``failed_tickers``.

Context only: the gate never reads these (no import from ``arc.gate``).
"""

from __future__ import annotations

import datetime as _dt
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

import structlog

from arc.context.kinds import (
    AnalystRecCounts,
    AnalystRecsPayload,
    EarningsHistoryPayload,
    EarningsQuarter,
    FundamentalsPayload,
    InsiderActivityPayload,
)
from arc.ingest.finnhub import FinnhubError, FinnhubForbidden, FinnhubRateLimited, redact
from arc.universe.master import normalize_symbol

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable, Iterable, Mapping, Sequence

    from pydantic import BaseModel

    from arc.ingest.finnhub import FinnhubClient
    from arc.universe.master import SymbolMaster

log = structlog.get_logger()

DataSet = Literal["earnings_history", "insider_activity", "analyst_recs", "fundamentals"]

ENDPOINTS: Mapping[str, str] = {
    "earnings_history": "/stock/earnings",
    "insider_activity": "/stock/insider-transactions",
    "analyst_recs": "/stock/recommendation",
    "fundamentals": "/stock/metric",
}

MAX_QUARTERS = 8

# SEC Form 4 transaction codes counted as insider activity: open-market (or private)
# purchases and sales only. Everything else is excluded on purpose, e.g.
# A (grant/award), M (option exercise/conversion), F (tax withholding),
# G (gift), D (disposition to the issuer), C (conversion), X (in-the-money exercise),
# J (other), W (will/inheritance), I (discretionary plan).
OPEN_MARKET_BUY = "P"
OPEN_MARKET_SELL = "S"
OPEN_MARKET_CODES: frozenset[str] = frozenset({OPEN_MARKET_BUY, OPEN_MARKET_SELL})

# D46 trimmed basic-financials field set: Finnhub key -> payload field.
FUNDAMENTAL_FIELDS: Mapping[str, str] = {
    "beta": "beta",
    "52WeekHigh": "high_52w",
    "52WeekHighDate": "high_52w_date",
    "52WeekLow": "low_52w",
    "52WeekLowDate": "low_52w_date",
    "marketCapitalization": "market_cap_musd",
    "priceRelativeToS&P5004Week": "rel_sp500_4w",
    "priceRelativeToS&P50013Week": "rel_sp500_13w",
    "priceRelativeToS&P50026Week": "rel_sp500_26w",
    "priceRelativeToS&P50052Week": "rel_sp500_52w",
    "5DayPriceReturnDaily": "return_5d_pct",
    "yearToDatePriceReturnDaily": "return_ytd_pct",
    "forwardPE": "forward_pe",
    "epsGrowthTTMYoy": "eps_growth_ttm_yoy",
    "revenueGrowthTTMYoy": "revenue_growth_ttm_yoy",
}
_DATE_FIELDS = frozenset({"high_52w_date", "low_52w_date"})

__all__ = [
    "ENDPOINTS",
    "FUNDAMENTAL_FIELDS",
    "OPEN_MARKET_CODES",
    "InsiderRules",
    "PerTickerRun",
    "TickerScope",
    "fetch_per_ticker",
    "parse_basic_financials",
    "parse_earnings_surprises",
    "parse_insider_transactions",
    "parse_recommendation_trends",
    "recent_reporters",
    "ticker_scope",
]


# ---------------------------------------------------------------------------
# Parsers (pure)
# ---------------------------------------------------------------------------


def _num(v: Any) -> float | None:
    """A finite float, else None (Finnhub sends null, "", or omits the key)."""
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and f not in (float("inf"), float("-inf")) else None


def _date(v: Any) -> str | None:
    text = str(v or "").strip()[:10]
    try:
        return _dt.date.fromisoformat(text).isoformat()
    except ValueError:
        return None


def parse_earnings_surprises(ticker: str, data: Any, *, as_of: str) -> EarningsHistoryPayload:
    """``/stock/earnings`` rows -> the newest (up to 8) quarters, beat/miss counts."""
    rows = data if isinstance(data, list) else []
    quarters: list[EarningsQuarter] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        period = _date(r.get("period"))
        if period is None:
            continue
        actual, estimate = _num(r.get("actual")), _num(r.get("estimate"))
        surprise = _num(r.get("surprise"))
        if surprise is None and actual is not None and estimate is not None:
            surprise = round(actual - estimate, 6)
        quarters.append(
            EarningsQuarter(
                period=period,
                actual=actual,
                estimate=estimate,
                surprise=surprise,
                surprise_pct=_num(r.get("surprisePercent")),
            )
        )
    quarters = sorted({q.period: q for q in quarters}.values(), key=lambda q: q.period)[::-1]
    quarters = quarters[:MAX_QUARTERS]
    known = [q for q in quarters if q.actual is not None and q.estimate is not None]
    return EarningsHistoryPayload(
        ticker=ticker,
        quarters=quarters,
        beat_count=sum(1 for q in known if q.actual > q.estimate),  # type: ignore[operator]
        miss_count=sum(1 for q in known if q.actual < q.estimate),  # type: ignore[operator]
        as_of=as_of,
    )


@dataclass(frozen=True)
class InsiderRules:
    window_days: int = 90
    cluster_buyers: int = 3
    cluster_days: int = 30


def _cluster(buys: Sequence[tuple[_dt.date, str]], buyers: int, days: int) -> bool:
    """True when some *days*-long window holds at least *buyers* distinct buyer names."""
    ordered = sorted(buys)
    for i, (start, _) in enumerate(ordered):
        end = start + _dt.timedelta(days=days - 1)
        names = {n for d, n in ordered[i:] if d <= end}
        if len(names) >= buyers:
            return True
    return False


def parse_insider_transactions(
    ticker: str, data: Any, *, as_of: _dt.date, rules: InsiderRules
) -> InsiderActivityPayload:
    """Open-market buys/sells (codes P/S, non-derivative) in the last ``window_days``."""
    rows = data.get("data") if isinstance(data, dict) else None
    since = as_of - _dt.timedelta(days=rules.window_days)
    buys: list[tuple[_dt.date, str]] = []
    sellers: set[str] = set()
    sells = 0
    net_shares = 0
    net_value = 0.0
    priced = False
    last: _dt.date | None = None
    seen: set[tuple[Any, ...]] = set()
    for r in rows if isinstance(rows, list) else []:
        if not isinstance(r, dict) or r.get("isDerivative"):
            continue
        code = str(r.get("transactionCode") or "").upper()
        if code not in OPEN_MARKET_CODES:
            continue
        day_text = _date(r.get("transactionDate"))
        change = _num(r.get("change"))
        if day_text is None or change is None or change == 0:
            continue
        day = _dt.date.fromisoformat(day_text)
        if not since < day <= as_of:
            continue
        name = str(r.get("name") or "").strip() or "?"
        key = (r.get("id"), name, day_text, code, change, r.get("transactionPrice"))
        if key in seen:  # Finnhub repeats a row per filing amendment
            continue
        seen.add(key)
        shares = int(abs(change))
        signed = shares if code == OPEN_MARKET_BUY else -shares
        if code == OPEN_MARKET_BUY:
            buys.append((day, name))
        else:
            sells += 1
            sellers.add(name)
        net_shares += signed
        price = _num(r.get("transactionPrice"))
        if price is not None and price > 0:
            net_value += signed * price
            priced = True
        last = day if last is None or day > last else last
    return InsiderActivityPayload(
        ticker=ticker,
        window_days=rules.window_days,
        buy_count=len(buys),
        sell_count=sells,
        net_shares=net_shares,
        net_value_usd=round(net_value, 2) if priced else None,
        distinct_insiders_buying=len({n for _, n in buys}),
        distinct_insiders_selling=len(sellers),
        last_txn_date=last.isoformat() if last else None,
        cluster_buy=_cluster(buys, rules.cluster_buyers, rules.cluster_days),
        as_of=as_of.isoformat(),
    )


def _rec_counts(r: Mapping[str, Any], period: str) -> AnalystRecCounts:
    def n(k: str) -> int:
        v = _num(r.get(k))
        return max(int(v), 0) if v is not None else 0

    return AnalystRecCounts(
        period=period,
        strong_buy=n("strongBuy"),
        buy=n("buy"),
        hold=n("hold"),
        sell=n("sell"),
        strong_sell=n("strongSell"),
    )


def _bull_minus_bear(c: AnalystRecCounts) -> int:
    return c.strong_buy + c.buy - c.sell - c.strong_sell


def parse_recommendation_trends(ticker: str, data: Any, *, as_of: str) -> AnalystRecsPayload | None:
    """Latest month vs the month before; None when Finnhub has no trend for *ticker*."""
    rows = [r for r in (data if isinstance(data, list) else []) if isinstance(r, dict)]
    dated = sorted(
        ((p, r) for r in rows if (p := _date(r.get("period"))) is not None),
        key=lambda pr: pr[0],
        reverse=True,
    )
    if not dated:
        return None
    cur = _rec_counts(dated[0][1], dated[0][0])
    prev = _rec_counts(dated[1][1], dated[1][0]) if len(dated) > 1 else None
    return AnalystRecsPayload(
        ticker=ticker,
        period=cur.period,
        strong_buy=cur.strong_buy,
        buy=cur.buy,
        hold=cur.hold,
        sell=cur.sell,
        strong_sell=cur.strong_sell,
        prev_period=prev,
        net_change=_bull_minus_bear(cur) - _bull_minus_bear(prev) if prev else None,
        as_of=as_of,
    )


def parse_basic_financials(ticker: str, data: Any, *, as_of: str) -> FundamentalsPayload | None:
    """The D46 trimmed field set; unknown or missing -> None (never 0).

    None when Finnhub returned no metric block at all (nothing to store).
    """
    metric = data.get("metric") if isinstance(data, dict) else None
    if not isinstance(metric, dict) or not metric:
        return None
    values: dict[str, Any] = {}
    for src, dst in FUNDAMENTAL_FIELDS.items():
        raw = metric.get(src)
        values[dst] = _date(raw) if dst in _DATE_FIELDS else _num(raw)
    return FundamentalsPayload(ticker=ticker, as_of=as_of, **values)


# ---------------------------------------------------------------------------
# Ticker scope
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TickerScope:
    tickers: list[str]
    dropped: list[str]
    etfs_skipped: list[str]
    sources: dict[str, int]  # seed / candidates / open -> tickers taken from each


def _is_etf(symbol: str, master: SymbolMaster | None, etfs: frozenset[str]) -> bool:
    """A fund: in the known ETF list, or in the master from Alpaca only (no SEC company)."""
    if symbol in etfs:
        return True
    info = master.get(symbol) if master is not None else None
    return info is not None and "sec" not in info.sources


def ticker_scope(
    conn: sqlite3.Connection,
    *,
    seed: Iterable[str],
    now: _dt.datetime,
    max_tickers: int,
    master: SymbolMaster | None = None,
    etfs: frozenset[str] = frozenset(),
) -> TickerScope:
    """Seed ∪ live ``candidate`` subjects ∪ open-structure underlyings, ETFs skipped,
    capped at *max_tickers* in that order (seed first)."""
    from arc.context.store import ContextStore

    candidates = [e.subject for e in ContextStore(conn).query(as_of=now, kinds=["candidate"])]
    open_rows = conn.execute(
        "SELECT DISTINCT ticker FROM open_structures WHERE status = 'open' ORDER BY ticker"
    ).fetchall()
    groups = {
        "seed": list(seed),
        "candidates": candidates,
        "open": [r[0] for r in open_rows],
    }
    ordered: list[str] = []
    origin: dict[str, str] = {}
    etf_skips: list[str] = []
    for label, raw in groups.items():
        for t in raw:
            sym = normalize_symbol(str(t))
            if not sym or sym in origin or sym in etf_skips:
                continue
            if _is_etf(sym, master, etfs):
                etf_skips.append(sym)
                continue
            origin[sym] = label
            ordered.append(sym)
    kept, dropped = ordered[:max_tickers], ordered[max_tickers:]
    counts = {k: sum(1 for t in kept if origin[t] == k) for k in groups}
    if dropped:
        log.info("finnhub.scope_capped", kept=len(kept), dropped=len(dropped), cap=max_tickers)
    return TickerScope(tickers=kept, dropped=dropped, etfs_skipped=etf_skips, sources=counts)


def recent_reporters(
    conn: sqlite3.Connection, tickers: Iterable[str], today: _dt.date, lo: int, hi: int
) -> list[str]:
    """Tickers in *tickers* whose earnings date (calendar raw docs) was *lo*..*hi* days ago."""
    wanted = list(tickers)
    start = (today - _dt.timedelta(days=hi)).isoformat()
    end = (today - _dt.timedelta(days=lo)).isoformat()
    rows = conn.execute(
        "SELECT tickers_hint FROM raw_docs WHERE source = 'earnings'"
        " AND substr(published_at, 1, 10) BETWEEN ? AND ?",
        (start, end),
    ).fetchall()
    reported: set[str] = set()
    for (hint,) in rows:
        try:
            reported.update(normalize_symbol(str(t)) for t in json.loads(hint or "[]"))
        except (TypeError, ValueError):
            continue
    return [t for t in wanted if t in reported]


# ---------------------------------------------------------------------------
# Fetch loop
# ---------------------------------------------------------------------------


@dataclass
class PerTickerRun:
    kind: str
    tickers: int = 0
    written: int = 0
    empty: list[str] = field(default_factory=list)  # Finnhub had nothing for the ticker
    failed: dict[str, str] = field(default_factory=dict)  # ticker -> redacted error


def _request(
    kind: DataSet, ticker: str, today: _dt.date, rules: InsiderRules
) -> tuple[str, dict[str, Any]]:
    path = ENDPOINTS[kind]
    if kind == "insider_activity":
        start = today - _dt.timedelta(days=rules.window_days)
        return path, {"symbol": ticker, "from": start.isoformat(), "to": today.isoformat()}
    if kind == "fundamentals":
        return path, {"symbol": ticker, "metric": "all"}
    return path, {"symbol": ticker}


def _parse(
    kind: DataSet, ticker: str, data: Any, today: _dt.date, rules: InsiderRules
) -> BaseModel | None:
    as_of = today.isoformat()
    if kind == "earnings_history":
        p = parse_earnings_surprises(ticker, data, as_of=as_of)
        return p if p.quarters else None
    if kind == "insider_activity":
        return parse_insider_transactions(ticker, data, as_of=today, rules=rules)
    if kind == "analyst_recs":
        return parse_recommendation_trends(ticker, data, as_of=as_of)
    return parse_basic_financials(ticker, data, as_of=as_of)


def fetch_per_ticker(
    client: FinnhubClient,
    kind: DataSet,
    tickers: Sequence[str],
    *,
    today: _dt.date,
    write: Callable[[str, BaseModel], object],
    rules: InsiderRules | None = None,
) -> PerTickerRun:
    """One call per ticker; ``write(ticker, payload)`` for each parsed result.

    403 (:class:`FinnhubForbidden`) and 429 (:class:`FinnhubRateLimited`) abort the
    run (raised); any other per-ticker error lands in ``failed``.
    """
    rules = rules or InsiderRules()
    run = PerTickerRun(kind=kind, tickers=len(tickers))
    for ticker in tickers:
        path, params = _request(kind, ticker, today, rules)
        try:
            data = client.get(path, params)
            payload = _parse(kind, ticker, data, today, rules)
        except (FinnhubForbidden, FinnhubRateLimited):
            raise
        except (FinnhubError, ValueError, TypeError) as exc:
            run.failed[ticker] = redact(str(exc))[:200]
            log.warning("finnhub.ticker_failed", kind=kind, ticker=ticker, error=run.failed[ticker])
            continue
        if payload is None:
            run.empty.append(ticker)
            continue
        write(ticker, payload)
        run.written += 1
    return run
