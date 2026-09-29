"""RecordedMarketData — an offline MarketDataProvider backed by a recorded JSON chain.

Used by tests and ``arc chains --fixture`` so the scanner runs with no network.
A recording holds one underlying: its quote, a daily close history and the
option chain snapshot, all serialised from the :mod:`arc.data.base` models.

Recording format (JSON object)::

    {
      "underlying": "SPY",
      "recorded_at": "<ISO datetime>",
      "quote": {UnderlyingQuote},
      "closes": [{"date": "YYYY-MM-DD", "close": float}, ...],
      "contracts": [{OptionContract}, ...]
    }
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from pydantic import BaseModel, Field

from arc.data.base import HistoryBar, OptionContract, UnderlyingQuote
from arc.utils.calendar import ET

__all__ = [
    "FIXTURES_DIR",
    "MULTI_NAME_FIXTURES",
    "SPY_CHAIN_FIXTURE",
    "ChainRecording",
    "DailyClose",
    "RecordedMarketData",
]

FIXTURES_DIR = Path(__file__).parent / "fixtures"
SPY_CHAIN_FIXTURE = FIXTURES_DIR / "spy_chain.json"
# E5.7 multi-name set (same offline clock as SPY): NVDA/XOM seed names, PLTR a
# non-seed name that passes the D28 liquidity screen, UFPT one that fails it.
MULTI_NAME_FIXTURES: tuple[Path, ...] = tuple(
    FIXTURES_DIR / f"{t}_chain.json" for t in ("spy", "nvda", "xom", "pltr", "ufpt")
)


class DailyClose(BaseModel):
    """One daily close in a recording."""

    date: dt.date
    close: float = Field(..., gt=0)
    volume: float | None = Field(None, ge=0, description="Daily share volume (E5.7 recordings).")


class ChainRecording(BaseModel):
    """A recorded chain snapshot for one underlying."""

    underlying: str
    recorded_at: dt.datetime
    source: str = ""
    note: str = ""
    quote: UnderlyingQuote
    closes: list[DailyClose] = Field(default_factory=list)
    contracts: list[OptionContract]

    @property
    def as_of(self) -> dt.date:
        """Recording date in ET."""
        return self.recorded_at.astimezone(ET).date()


class RecordedMarketData:
    """MarketDataProvider serving one or more recordings, keyed by underlying."""

    def __init__(self, *recordings: ChainRecording) -> None:
        if not recordings:
            msg = "RecordedMarketData needs at least one recording"
            raise ValueError(msg)
        self._by_symbol = {r.underlying.upper(): r for r in recordings}

    @classmethod
    def from_files(cls, *paths: Path | str) -> RecordedMarketData:
        """Load recordings from JSON files."""
        return cls(*(load_recording(p) for p in paths))

    def recording(self, symbol: str) -> ChainRecording:
        """Return the recording for *symbol* (KeyError if absent)."""
        try:
            return self._by_symbol[symbol.upper()]
        except KeyError:
            msg = f"no recording for {symbol!r}; have {sorted(self._by_symbol)}"
            raise KeyError(msg) from None

    # -- MarketDataProvider --------------------------------------------------

    def option_chain(
        self,
        underlying: str,
        exp_start: dt.date,
        exp_end: dt.date,
    ) -> list[OptionContract]:
        rec = self.recording(underlying)
        return [
            c.model_copy(deep=True) for c in rec.contracts if exp_start <= c.expiration <= exp_end
        ]

    def underlying_quote(self, symbol: str) -> UnderlyingQuote:
        return self.recording(symbol).quote.model_copy()

    def history_bars(
        self,
        symbol: str,
        start: dt.date,
        end: dt.date,
        timeframe: str = "1Day",
    ) -> list[HistoryBar]:
        if timeframe.lower().replace(" ", "") != "1day":
            msg = f"recordings only hold daily closes, not {timeframe!r}"
            raise ValueError(msg)
        rec = self.recording(symbol)
        return [
            HistoryBar(
                timestamp=dt.datetime.combine(c.date, dt.time(16, 0), tzinfo=ET),
                open=c.close,
                high=c.close,
                low=c.close,
                close=c.close,
                volume=c.volume or 0.0,
            )
            for c in rec.closes
            if start <= c.date <= end
        ]


def load_recording(path: Path | str) -> ChainRecording:
    """Parse a recording file."""
    return ChainRecording.model_validate(json.loads(Path(path).read_text()))
