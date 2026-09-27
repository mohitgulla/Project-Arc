"""Alpaca historical options provider (daily bars since Feb 2024).

Alpaca's options history begins 2024-02-01 and is trade-derived only: daily
OHLCV/VWAP/trade-count bars.  Alpaca does not serve *historical* option quotes
(only latest), so ``bid``/``ask`` are always ``None`` here — the ThetaData EOD
provider supplies the closing NBBO.

Contracts are discovered via the (paper) Trading API ``/v2/options/contracts``
for both active and expired (inactive) contracts, then bars are requested in
symbol batches.
"""

from __future__ import annotations

import datetime as dt
import os
import re
from typing import TYPE_CHECKING, Any, Protocol

import structlog

from arc.data.history.base import OptionEodRow, OptionRight
from arc.utils.calendar import ET

if TYPE_CHECKING:
    from collections.abc import Iterable

log = structlog.get_logger()

ALPACA_OPTIONS_HISTORY_START = dt.date(2024, 2, 1)
_BAR_BATCH = 100  # symbols per bars request (URL length / page size friendly)
_STD_OCC = re.compile(r"^[A-Z]{1,5}\d{6}[CP]\d{8}$")


class _ContractsClient(Protocol):
    def get_option_contracts(self, request: Any) -> Any: ...


class _BarsClient(Protocol):
    def get_option_bars(self, request_params: Any) -> Any: ...


def _get_keys() -> tuple[str, str]:
    api_key = os.environ.get("ALPACA_API_KEY", "")
    secret_key = os.environ.get("ALPACA_SECRET_KEY", "")
    if not api_key or not secret_key:
        msg = "ALPACA_API_KEY and ALPACA_SECRET_KEY must be set (see ~/.hermes/.env)."
        raise RuntimeError(msg)
    return api_key, secret_key


def _default_clients() -> tuple[_ContractsClient, _BarsClient]:  # pragma: no cover - network
    from alpaca.data.historical.option import OptionHistoricalDataClient
    from alpaca.trading.client import TradingClient

    key, secret = _get_keys()
    # paper=True is hard-pinned: contract metadata only, never order routing.
    return (
        TradingClient(api_key=key, secret_key=secret, paper=True),
        OptionHistoricalDataClient(api_key=key, secret_key=secret),
    )


def _enum_value(v: Any) -> str:
    return str(getattr(v, "value", v)).lower()


class AlpacaHistoryProvider:
    """``HistoricalDataProvider`` backed by Alpaca options bars."""

    name = "alpaca"

    def __init__(
        self,
        contracts_client: _ContractsClient | None = None,
        bars_client: _BarsClient | None = None,
        *,
        batch_size: int = _BAR_BATCH,
    ) -> None:
        if contracts_client is None or bars_client is None:
            dc, db = _default_clients()
            contracts_client = contracts_client or dc
            bars_client = bars_client or db
        self._contracts = contracts_client
        self._bars = bars_client
        self._batch = batch_size

    def earliest_date(self) -> dt.date:
        return ALPACA_OPTIONS_HISTORY_START

    # -- contracts -----------------------------------------------------------

    def list_contracts(
        self, underlying: str, exp_start: dt.date, exp_end: dt.date
    ) -> list[dict[str, Any]]:
        """All contracts (active + expired) with expiration in [exp_start, exp_end]."""
        from alpaca.trading.enums import AssetStatus
        from alpaca.trading.requests import GetOptionContractsRequest

        out: dict[str, dict[str, Any]] = {}
        skipped = 0
        root = underlying.upper()
        for status in (AssetStatus.ACTIVE, AssetStatus.INACTIVE):
            token: str | None = None
            while True:
                req = GetOptionContractsRequest(
                    underlying_symbols=[root],
                    status=status,
                    expiration_date_gte=exp_start,
                    expiration_date_lte=exp_end,
                    limit=10000,
                    page_token=token,
                )
                resp = self._contracts.get_option_contracts(req)
                for c in resp.option_contracts or []:
                    # Skip corporate-action-adjusted contracts (e.g. "1SPY…", "SPY1…"):
                    # non-standard deliverables, and the bars API rejects their symbols.
                    if not _STD_OCC.match(c.symbol) or c.symbol[:-15] != root:
                        skipped += 1
                        continue
                    out[c.symbol] = {
                        "symbol": c.symbol,
                        "expiration": c.expiration_date,
                        "strike": float(c.strike_price),
                        "right": OptionRight(_enum_value(c.type)),
                    }
                token = resp.next_page_token
                if not token:
                    break
        log.info("alpaca_history.contracts", underlying=underlying, n=len(out), skipped=skipped)
        return list(out.values())

    # -- bars ----------------------------------------------------------------

    def _fetch_bars(
        self, symbols: list[str], start: dt.date, end: dt.date
    ) -> Iterable[tuple[str, Any]]:
        from alpaca.data.requests import OptionBarsRequest
        from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

        for i in range(0, len(symbols), self._batch):
            batch = symbols[i : i + self._batch]
            req = OptionBarsRequest(
                symbol_or_symbols=batch,
                start=dt.datetime.combine(start, dt.time.min, tzinfo=ET),
                end=dt.datetime.combine(end, dt.time.max, tzinfo=ET),
                timeframe=TimeFrame(1, TimeFrameUnit.Day),
            )
            resp = self._bars.get_option_bars(req)
            data = getattr(resp, "data", resp) or {}
            for sym, bars in data.items():
                for bar in bars:
                    yield sym, bar

    def fetch_option_eod(
        self,
        underlying: str,
        start: dt.date,
        end: dt.date,
        *,
        max_dte: int,
    ) -> list[OptionEodRow]:
        start = max(start, ALPACA_OPTIONS_HISTORY_START)
        if end < start:
            return []
        contracts = self.list_contracts(underlying, start, end + dt.timedelta(days=max_dte))
        meta = {c["symbol"]: c for c in contracts}
        rows: list[OptionEodRow] = []
        for sym, bar in self._fetch_bars(sorted(meta), start, end):
            c = meta.get(sym)
            if c is None:
                continue
            day = bar.timestamp.astimezone(ET).date()
            dte = (c["expiration"] - day).days
            if not (start <= day <= end) or dte < 0 or dte > max_dte:
                continue
            rows.append(
                OptionEodRow(
                    provider=self.name,
                    underlying=underlying,
                    date=day,
                    symbol=sym,
                    expiration=c["expiration"],
                    strike=c["strike"],
                    right=c["right"],
                    open=bar.open,
                    high=bar.high,
                    low=bar.low,
                    close=bar.close,
                    volume=bar.volume,
                    trade_count=bar.trade_count,
                    vwap=bar.vwap,
                )
            )
        log.info(
            "alpaca_history.fetched",
            underlying=underlying,
            start=start.isoformat(),
            end=end.isoformat(),
            rows=len(rows),
        )
        return rows
