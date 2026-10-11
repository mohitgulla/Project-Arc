"""Historical options data contracts and the ``HistoricalDataProvider`` protocol.

See PLAN.md §4 (E7.1) and D7: Alpaca options history (Feb 2024 →) and the
ThetaData free EOD tier (last ~1y) feed the backtester (E7.2).

Every provider returns normalised :class:`OptionEodRow` records — one row per
option contract per trading session.  Fields a provider cannot supply are
``None`` (e.g. Alpaca has no historical NBBO, ThetaData free EOD has no VWAP).
"""

from __future__ import annotations

import datetime as dt  # noqa: TC003 — used at runtime in pydantic models
import enum
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator

from arc.utils.calendar import ET


class OptionRight(enum.StrEnum):
    CALL = "call"
    PUT = "put"


class OptionEodRow(BaseModel):
    """One option contract's end-of-day record for one trading session."""

    model_config = ConfigDict(frozen=True)

    provider: str = Field(..., description="Source provider name, e.g. 'alpaca'.")
    underlying: str
    date: dt.date = Field(..., description="Trading session (ET) this row describes.")
    symbol: str = Field(..., description="OCC symbol without padding, e.g. SPY240301C00500000.")
    expiration: dt.date
    strike: float = Field(..., gt=0)
    right: OptionRight

    # Trade-derived OHLCV
    open: float | None = None
    high: float | None = None
    low: float | None = None
    close: float | None = None
    volume: float | None = Field(default=None, ge=0)
    trade_count: float | None = Field(default=None, ge=0)
    vwap: float | None = None

    # Closing NBBO (EOD quote)
    bid: float | None = Field(default=None, ge=0)
    ask: float | None = Field(default=None, ge=0)
    bid_size: float | None = Field(default=None, ge=0)
    ask_size: float | None = Field(default=None, ge=0)

    # E7.6 (D84), schema-additive; None for providers/files that predate them.
    last_trade: dt.datetime | None = Field(
        default=None, description="Time of the session's last trade (ET), ThetaData EOD report."
    )
    created: dt.datetime | None = Field(
        default=None, description="EOD report generation time (ET, ~17:15), ThetaData."
    )
    open_interest: float | None = Field(
        default=None,
        ge=0,
        description=(
            "OPRA open interest reported the morning of `date` (= OI as of the prior "
            "session's close), i.e. what a trader sees during `date`. No look-ahead."
        ),
    )

    @field_validator("underlying")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.upper()

    @field_validator("last_trade", "created")
    @classmethod
    def _et(cls, v: dt.datetime | None) -> dt.datetime | None:
        """Naive timestamps are ET (ThetaData reports wall-clock ET); aware ones convert."""
        if v is None:
            return None
        if v.tzinfo is None:
            return v.replace(tzinfo=ET)
        return v.astimezone(ET)


#: Column order for the on-disk parquet schema (mirrors OptionEodRow fields).
EOD_COLUMNS: tuple[str, ...] = tuple(OptionEodRow.model_fields.keys())


def occ_symbol(underlying: str, expiration: dt.date, right: OptionRight, strike: float) -> str:
    """Build an unpadded OCC symbol (Alpaca style): ROOT + YYMMDD + C/P + strike*1000 (8 digits)."""
    strike_milli = round(strike * 1000)
    if strike_milli <= 0 or strike_milli >= 10**8:
        msg = f"strike {strike} out of OCC range"
        raise ValueError(msg)
    cp = "C" if right is OptionRight.CALL else "P"
    return f"{underlying.upper()}{expiration:%y%m%d}{cp}{strike_milli:08d}"


@runtime_checkable
class HistoricalDataProvider(Protocol):
    """Source of historical daily option data for the backtester.

    Attributes
    ----------
    name
        Short stable identifier; used as the storage partition key.
    """

    name: str

    def earliest_date(self) -> dt.date:
        """First trading date this provider can serve (tier/coverage limit)."""
        ...

    def fetch_option_eod(
        self,
        underlying: str,
        start: dt.date,
        end: dt.date,
        *,
        max_dte: int,
    ) -> list[OptionEodRow]:
        """Daily rows, every *underlying* option with DTE ≤ *max_dte*, for sessions [start, end]."""
        ...
