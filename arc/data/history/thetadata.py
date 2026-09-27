"""ThetaData historical options provider (free EOD tier via local Theta Terminal v3).

Requires the Theta Terminal v3 running locally (default
``http://127.0.0.1:25503``); credentials live in the terminal's own config,
never in this repo.  Uses ``GET /v3/option/history/eod`` with
``expiration=*`` and ``max_dte`` to pull every contract's daily EOD report
(OHLC + volume/count + closing NBBO) for an underlying.

The free tier is limited to roughly the last year of history
(:data:`THETA_FREE_LOOKBACK_DAYS`) and is request-rate limited; requests are
issued per ``chunk_days`` window with a minimum interval between calls.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import time
from typing import TYPE_CHECKING, Any, Protocol

import structlog

from arc.data.history.base import OptionEodRow, OptionRight, occ_symbol
from arc.utils.calendar import now_et

if TYPE_CHECKING:
    from collections.abc import Callable

log = structlog.get_logger()

THETA_DEFAULT_URL = "http://127.0.0.1:25503"
THETA_FREE_LOOKBACK_DAYS = 365
_EOD_PATH = "/v3/option/history/eod"


class ThetaTerminalError(RuntimeError):
    """The Theta Terminal is unreachable or returned an error."""


class _HttpResponse(Protocol):
    status_code: int
    text: str


class _HttpSession(Protocol):
    def get(self, url: str, params: dict[str, Any], timeout: float) -> _HttpResponse: ...


_RIGHTS = {
    "C": OptionRight.CALL,
    "CALL": OptionRight.CALL,
    "P": OptionRight.PUT,
    "PUT": OptionRight.PUT,
}


def _f(v: str | None) -> float | None:
    if v is None or v.strip() == "":
        return None
    return float(v)


def _date(v: str) -> dt.date:
    """Parse ``YYYY-MM-DD``, ``YYYYMMDD`` or an ISO datetime to a date."""
    v = v.strip()
    if len(v) == 8 and v.isdigit():
        return dt.date(int(v[:4]), int(v[4:6]), int(v[6:]))
    return dt.date.fromisoformat(v[:10])


def parse_eod_csv(text: str, underlying: str, provider: str = "thetadata") -> list[OptionEodRow]:
    """Parse a Theta v3 ``option/history/eod`` CSV body into rows.

    The session date is taken from ``created`` (report generation time, 17:15 ET).
    Rows with an unknown right or non-positive strike are skipped.
    """
    rows: list[OptionEodRow] = []
    reader = csv.DictReader(io.StringIO(text))
    for rec in reader:
        right = _RIGHTS.get((rec.get("right") or "").strip().upper())
        strike = _f(rec.get("strike"))
        if right is None or strike is None or strike <= 0:
            log.warning("thetadata.skip_row", reason="bad right/strike", rec=rec)
            continue
        expiration = _date(rec["expiration"])
        root = (rec.get("symbol") or underlying).strip().upper()
        rows.append(
            OptionEodRow(
                provider=provider,
                underlying=underlying,
                date=_date(rec["created"]),
                symbol=occ_symbol(root, expiration, right, strike),
                expiration=expiration,
                strike=strike,
                right=right,
                open=_f(rec.get("open")),
                high=_f(rec.get("high")),
                low=_f(rec.get("low")),
                close=_f(rec.get("close")),
                volume=_f(rec.get("volume")),
                trade_count=_f(rec.get("count")),
                bid=_f(rec.get("bid")),
                ask=_f(rec.get("ask")),
                bid_size=_f(rec.get("bid_size")),
                ask_size=_f(rec.get("ask_size")),
            )
        )
    return rows


def _default_session() -> _HttpSession:  # pragma: no cover - trivial
    import requests

    return requests.Session()  # type: ignore[return-value]


class ThetaDataEodProvider:
    """``HistoricalDataProvider`` backed by ThetaData's EOD endpoint."""

    name = "thetadata"

    def __init__(
        self,
        base_url: str = THETA_DEFAULT_URL,
        session: _HttpSession | None = None,
        *,
        lookback_days: int = THETA_FREE_LOOKBACK_DAYS,
        chunk_days: int = 7,
        min_interval_s: float = 0.0,
        timeout_s: float = 120.0,
        today: Callable[[], dt.date] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if chunk_days < 1:
            msg = "chunk_days must be >= 1"
            raise ValueError(msg)
        self._url = base_url.rstrip("/") + _EOD_PATH
        self._session = session or _default_session()
        self._lookback = lookback_days
        self._chunk = chunk_days
        self._min_interval = min_interval_s
        self._timeout = timeout_s
        self._today = today or (lambda: now_et().date())
        self._sleep = sleep
        self._last_call: float | None = None

    def earliest_date(self) -> dt.date:
        return self._today() - dt.timedelta(days=self._lookback)

    def _get(self, params: dict[str, Any]) -> str:
        if self._min_interval and self._last_call is not None:
            wait = self._min_interval - (time.monotonic() - self._last_call)
            if wait > 0:
                self._sleep(wait)
        try:
            resp = self._session.get(self._url, params=params, timeout=self._timeout)
        except Exception as exc:  # connection refused etc.
            msg = f"Theta Terminal unreachable at {self._url}: {exc} (is the v3 terminal running?)"
            raise ThetaTerminalError(msg) from exc
        finally:
            self._last_call = time.monotonic()
        if resp.status_code == 472 or (resp.status_code == 200 and not resp.text.strip()):
            return ""  # 472 = no data for request
        if resp.status_code != 200:
            msg = f"Theta Terminal HTTP {resp.status_code}: {resp.text[:300]}"
            raise ThetaTerminalError(msg)
        return resp.text

    def fetch_option_eod(
        self,
        underlying: str,
        start: dt.date,
        end: dt.date,
        *,
        max_dte: int,
    ) -> list[OptionEodRow]:
        start = max(start, self.earliest_date())
        rows: list[OptionEodRow] = []
        cur = start
        while cur <= end:
            chunk_end = min(end, cur + dt.timedelta(days=self._chunk - 1))
            text = self._get(
                {
                    "symbol": underlying.upper(),
                    "expiration": "*",
                    "start_date": cur.strftime("%Y%m%d"),
                    "end_date": chunk_end.strftime("%Y%m%d"),
                    "max_dte": max_dte,
                    "format": "csv",
                }
            )
            if text:
                rows.extend(
                    r for r in parse_eod_csv(text, underlying, self.name) if start <= r.date <= end
                )
            cur = chunk_end + dt.timedelta(days=1)
        log.info(
            "thetadata.fetched",
            underlying=underlying,
            start=start.isoformat(),
            end=end.isoformat(),
            rows=len(rows),
        )
        return rows
