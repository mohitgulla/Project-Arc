"""Open interest for the scanner: Cboe's delayed chain first, Alpaca as fallback (E2.5, D116).

On 2026-10-14 Alpaca's contracts API dated every contract's open interest two
sessions back, and contracts listed since had none, so the ``low_open_interest``
filter dropped new weeklies that traded (MU liquid contracts 362 with Alpaca OI vs
419 with Cboe OI). The exchange's delayed chain (``cdn.cboe.com .../options/<T>.json``,
already read by the options_fast source) carries the current OI for every listed
contract.

:func:`merge_open_interest` is the pure rule, per OCC symbol:

1. **Cboe** when the symbol is in the Cboe chain with an OI value (primary);
2. else **Alpaca** when its ``open_interest_date`` is on or after the session before
   the scan's session;
3. else **unknown** (``None``): the filter fails it, as before.

:class:`CboeOpenInterestMarket` wraps the live market-data provider for one run
(the pipeline's ``PipelineEnv.live``, ``arc chains``): one HTTP GET of the Cboe chain
per ticker (D114 read retry, :mod:`arc.utils.http_retry`), cached for the run. A
failed fetch or parse falls back to Alpaca's OI for that ticker and logs
``scanner.oi_source`` = ``alpaca_fallback``; it never fails the scan. The scanner
counts each chain's ``oi_source`` (``scanner.scan`` log), and the pipeline raises the
informational ``data:alpaca_oi_stale`` ops alert from the Alpaca OI dates
(:func:`stale_alpaca_oi`).
"""

from __future__ import annotations

from collections import Counter
from typing import TYPE_CHECKING, Any, Literal

import structlog
from pydantic import BaseModel

from arc.utils.calendar import is_session, previous_session

if TYPE_CHECKING:
    import datetime as _dt
    from collections.abc import Callable, Iterable, Mapping

    from arc.data.base import HistoryBar, MarketDataProvider, OptionContract, UnderlyingQuote

__all__ = [
    "CboeOpenInterestMarket",
    "OiCounts",
    "OiSource",
    "alpaca_oi_cutoff",
    "apply_open_interest",
    "cboe_get",
    "merge_open_interest",
    "modal_date",
    "stale_alpaca_oi",
]

log = structlog.get_logger(__name__)

OiSource = Literal["cboe", "alpaca"]

CBOE_TIMEOUT_S = 20.0
CBOE_USER_AGENT = "Mozilla/5.0 (Project Arc)"  # the cdn rejects non-browser agents


def _key(symbol: str) -> str:
    """Compact OCC key (Alpaca and Cboe both send compact symbols; padding is dropped)."""
    return symbol.replace(" ", "").upper()


def alpaca_oi_cutoff(as_of: _dt.date) -> _dt.date:
    """Oldest Alpaca ``open_interest_date`` still fresh for a scan on *as_of*.

    The session before the scan's session (*as_of* itself when it is a session,
    else the last session before it).
    """
    session = as_of if is_session(as_of) else previous_session(as_of)
    return previous_session(session)


def merge_open_interest(
    alpaca: Mapping[str, tuple[int | None, _dt.date | None]],
    cboe: Mapping[str, int | None] | None,
    *,
    cutoff: _dt.date,
) -> dict[str, tuple[int | None, OiSource | None]]:
    """OCC symbol → ``(open interest, source)`` for every symbol in *alpaca*.

    *alpaca* maps each chain symbol to Alpaca's ``(open_interest, open_interest_date)``;
    *cboe* maps Cboe chain symbols to their OI (``None``: the fetch failed). Alpaca's
    value counts only when dated on or after *cutoff* (:func:`alpaca_oi_cutoff`).
    """
    cb = {_key(k): v for k, v in (cboe or {}).items()}
    out: dict[str, tuple[int | None, OiSource | None]] = {}
    for symbol, (oi, oi_date) in alpaca.items():
        c = cb.get(_key(symbol))
        if c is not None:
            out[symbol] = (c, "cboe")
        elif oi is not None and oi_date is not None and oi_date >= cutoff:
            out[symbol] = (oi, "alpaca")
        else:
            out[symbol] = (None, None)
    return out


class OiCounts(BaseModel):
    """How a chain's open interest was sourced (the ``scanner.scan`` log fields)."""

    oi_cboe: int = 0
    oi_alpaca: int = 0
    oi_unknown: int = 0
    cboe_ok: bool = True


def apply_open_interest(
    chain: Iterable[OptionContract],
    cboe: Mapping[str, int | None] | None,
    *,
    as_of: _dt.date,
) -> tuple[list[OptionContract], OiCounts]:
    """*chain* with ``open_interest`` / ``oi_source`` replaced by the merged values."""
    contracts = list(chain)
    merged = merge_open_interest(
        {c.symbol: (c.open_interest, c.open_interest_date) for c in contracts},
        cboe,
        cutoff=alpaca_oi_cutoff(as_of),
    )
    counts = OiCounts(cboe_ok=cboe is not None)
    out: list[OptionContract] = []
    for c in contracts:
        oi, src = merged[c.symbol]
        if src == "cboe":
            counts.oi_cboe += 1
        elif src == "alpaca":
            counts.oi_alpaca += 1
        else:
            counts.oi_unknown += 1
        out.append(c.model_copy(update={"open_interest": oi, "oi_source": src}))
    return out, counts


def modal_date(dates: Counter[_dt.date | None]) -> _dt.date | None:
    """Most common non-null date (latest wins a tie), or None."""
    known = [(n, d) for d, n in dates.items() if d is not None]
    return max(known)[1] if known else None


def cboe_get() -> Callable[[str], bytes]:  # pragma: no cover - live network
    """GET with the D114 transient-read retry (5xx / connection errors, GET only)."""
    from arc.config import get_settings
    from arc.utils.http_retry import ReadRetryPolicy, ReadRetrySession

    s = get_settings()
    session = ReadRetrySession(
        ReadRetryPolicy(
            retries=s.alpaca_read_retries,
            base_s=s.alpaca_read_retry_base_s,
            budget_s=s.alpaca_read_retry_budget_s,
        )
    )
    session.headers["User-Agent"] = CBOE_USER_AGENT

    def get(url: str) -> bytes:
        resp = session.get(url, timeout=CBOE_TIMEOUT_S)
        resp.raise_for_status()
        return resp.content

    return get


class CboeOpenInterestMarket:
    """A :class:`~arc.data.base.MarketDataProvider` whose chains carry merged OI.

    Wraps the live provider (module doc): ``option_chain`` replaces each contract's
    ``open_interest`` with :func:`merge_open_interest`'s value and sets
    ``oi_source``. Every other call goes straight to *inner*. Built once per run
    (``PipelineEnv.live``, ``arc chains``), so its cache is the in-run cache: each
    ticker's Cboe chain is fetched at most once, and a failure is cached too (a down
    cdn costs one request per ticker per run). It sits *inside* an experiment tape
    recorder, so a paired arm replays the merged chain control priced against.

    *get* fetches a URL body (tests inject one; default :func:`cboe_get`); *today*
    gives the session date the Alpaca freshness cutoff is measured from (tests
    inject it; default today ET).
    """

    def __init__(
        self,
        inner: MarketDataProvider,
        *,
        get: Callable[[str], bytes] | None = None,
        today: Callable[[], _dt.date] | None = None,
    ) -> None:
        self.inner = inner
        self._get = get
        self._today = today
        self._cache: dict[str, dict[str, int | None] | None] = {}
        #: Alpaca ``open_interest_date`` per contract seen this run (stale-OI alert)
        self.alpaca_dates: Counter[_dt.date | None] = Counter()

    def __getattr__(self, name: str) -> Any:
        if name == "inner":  # not yet set (copy/unpickle): no recursion
            raise AttributeError(name)
        return getattr(self.inner, name)

    def cboe(self, ticker: str) -> dict[str, int | None] | None:
        """OCC symbol → Cboe OI for *ticker*'s whole listed chain, or None on failure."""
        from arc.ingest.cboe_fast import CBOE_CHAIN_URL, parse_delayed_chain

        ticker = ticker.upper()
        if ticker in self._cache:
            return self._cache[ticker]
        if self._get is None:
            self._get = cboe_get()
        got: dict[str, int | None] | None
        try:
            chain = parse_delayed_chain(self._get(CBOE_CHAIN_URL.format(ticker=ticker)))
            if not chain.rows:
                msg = "empty Cboe chain"
                raise ValueError(msg)
            got = {r.occ_symbol: r.open_interest for r in chain.rows}
        except Exception as exc:  # noqa: BLE001 - OI enrichment never fails the scan
            log.warning(
                "scanner.oi_source",
                ticker=ticker,
                oi_source="alpaca_fallback",
                error=f"{type(exc).__name__}: {str(exc)[:200]}",
            )
            got = None
        self._cache[ticker] = got
        return got

    def option_chain(
        self, underlying: str, exp_start: _dt.date, exp_end: _dt.date
    ) -> list[OptionContract]:
        contracts = self.inner.option_chain(underlying, exp_start, exp_end)
        self.alpaca_dates.update(c.open_interest_date for c in contracts)
        today = self._today() if self._today is not None else _today_et()
        merged, _ = apply_open_interest(contracts, self.cboe(underlying), as_of=today)
        return merged

    def underlying_quote(self, symbol: str) -> UnderlyingQuote:
        return self.inner.underlying_quote(symbol)

    def history_bars(
        self,
        symbol: str,
        start: _dt.date,
        end: _dt.date,
        timeframe: str = "1Day",
        *,
        feed: str | None = None,
    ) -> list[HistoryBar]:
        if feed is None:
            return self.inner.history_bars(symbol, start, end, timeframe)
        return self.inner.history_bars(symbol, start, end, timeframe, feed=feed)


def _today_et() -> _dt.date:  # pragma: no cover - live default; tests inject *today*
    from arc.utils.calendar import now_et

    return now_et().date()


def stale_alpaca_oi(dates: Counter[_dt.date | None], as_of: _dt.date) -> _dt.date | None:
    """Alpaca's modal OI date when older than the freshness cutoff, else None."""
    mode = modal_date(dates)
    if mode is None or mode >= alpaca_oi_cutoff(as_of):
        return None
    return mode
