"""Alpaca implementation of :class:`arc.iv.backfill.OptionHistory` (network; E4.12).

Contracts come from the paper Trading API contracts list (active *and* inactive,
so expired series are found); option daily bars from the historical options data
API, batched; underlying closes from :meth:`AlpacaMarketData.history_bars`. Every
request first takes a slot from a shared cross-process budget
(``routine_state[alpaca_data:calls]``) so a backfill and the routines never exceed
the data plan's per-minute limit together.
"""

from __future__ import annotations

import datetime as _dt
from typing import TYPE_CHECKING, Any

import structlog

from arc.iv.backfill import DailyBar, ListedContract
from arc.utils.calendar import ET

if TYPE_CHECKING:
    from collections.abc import Sequence

    from arc.ingest.finnhub import RateLimiter

log = structlog.get_logger(__name__)

RATE_STATE_KEY = "alpaca_data:calls"
PAGE_ROWS = 10_000  # rows per Alpaca market-data page


class AlpacaOptionHistory:  # pragma: no cover - live network (exercised by the CLI live run)
    def __init__(self, limiter: RateLimiter, *, page_limit: int = 10_000) -> None:
        import os

        from alpaca.data.historical.option import OptionHistoricalDataClient
        from alpaca.trading.client import TradingClient

        from arc.data.alpaca import AlpacaMarketData, _get_keys

        key, secret = _get_keys(
            os.environ.get("ALPACA_API_KEY"), os.environ.get("ALPACA_SECRET_KEY")
        )
        self._trading = TradingClient(api_key=key, secret_key=secret, paper=True)
        self._options = OptionHistoricalDataClient(api_key=key, secret_key=secret)
        self._market = AlpacaMarketData(api_key=key, secret_key=secret)
        self._limiter = limiter
        self._page_limit = page_limit
        self.calls = 0

    def _take(self) -> None:
        self._limiter.acquire()
        self.calls += 1

    def underlying_closes(
        self, ticker: str, start: _dt.date, end: _dt.date
    ) -> dict[_dt.date, float]:
        self._take()
        bars = self._market.history_bars(ticker, start, end)
        return {b.timestamp.astimezone(ET).date(): float(b.close) for b in bars}

    def contracts(
        self,
        ticker: str,
        exp_start: _dt.date,
        exp_end: _dt.date,
        strike_lo: float,
        strike_hi: float,
    ) -> list[ListedContract]:
        from alpaca.trading.enums import AssetStatus
        from alpaca.trading.requests import GetOptionContractsRequest

        out: list[ListedContract] = []
        for status in (AssetStatus.INACTIVE, AssetStatus.ACTIVE):
            token: str | None = None
            while True:
                self._take()
                req = GetOptionContractsRequest(
                    underlying_symbols=[ticker],
                    status=status,
                    expiration_date_gte=exp_start,
                    expiration_date_lte=exp_end,
                    strike_price_gte=f"{strike_lo:.2f}",
                    strike_price_lte=f"{strike_hi:.2f}",
                    limit=self._page_limit,
                    page_token=token,
                )
                resp: Any = self._trading.get_option_contracts(req)
                for c in resp.option_contracts or []:
                    out.append(
                        ListedContract(
                            symbol=str(c.symbol),
                            expiration=c.expiration_date,
                            strike=float(c.strike_price),
                            kind="c" if str(getattr(c.type, "value", c.type)) == "call" else "p",
                        )
                    )
                token = resp.next_page_token
                if not token:
                    break
        return out

    def daily_bars(
        self, symbols: Sequence[str], start: _dt.date, end: _dt.date
    ) -> dict[str, dict[_dt.date, DailyBar]]:
        from alpaca.data.requests import OptionBarsRequest
        from alpaca.data.timeframe import TimeFrame

        # No ``limit``: alpaca-py treats it as a cap on the TOTAL rows across pages
        # (a 10k limit silently truncated 100 symbols x a year of bars). Without it the
        # client follows ``next_page_token`` itself; the extra pages are charged to
        # the shared budget afterwards (one per 10k bars).
        self._take()
        req = OptionBarsRequest(
            symbol_or_symbols=list(symbols),
            timeframe=TimeFrame.Day,
            start=_dt.datetime.combine(start, _dt.time.min, tzinfo=ET),
            end=_dt.datetime.combine(end, _dt.time.max, tzinfo=ET),
        )
        resp: Any = self._options.get_option_bars(req)
        out: dict[str, dict[_dt.date, DailyBar]] = {}
        n = 0
        for sym, bars in (getattr(resp, "data", None) or {}).items():
            day_map = out.setdefault(str(sym), {})
            for b in bars:
                n += 1
                day_map[b.timestamp.astimezone(ET).date()] = DailyBar(
                    close=float(b.close), volume=float(b.volume or 0)
                )
        for _ in range(n // PAGE_ROWS):
            self._take()
        return out
