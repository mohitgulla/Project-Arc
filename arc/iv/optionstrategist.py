"""Option Strategist weekly volatility file (E4.12, D55) — ad hoc, internal use only.

``https://www.optionstrategist.com/calculators/free-volatility-data`` (McMillan
Analysis Corp., updated Saturdays) lists about 5,800 underlyings as fixed-width
lines::

    Symbol (option symbols)           hv20  hv50 hv100    DATE   curiv Days/Percentile Close
    SPY                                 11    11    12  261002   11.20   600/ 18%ile  769.65

Owner decision (D55): the file is run by hand (``arc iv import-optionstrategist``),
for backfill validation and as a labelled fallback percentile only. Rows are stored
as ``optionstrategist`` and never mixed into our IV series; nothing here is shown
verbatim outside Arc or redistributed.
"""

from __future__ import annotations

import datetime as _dt
import html
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from arc.iv.store import OPTIONSTRATEGIST, IvRow

if TYPE_CHECKING:
    from collections.abc import Callable

OS_URL = "https://www.optionstrategist.com/calculators/free-volatility-data"

_LINE = re.compile(
    r"^(?P<sym>\S+)(?:\s+\([^)]*\))?\s+(?P<hv20>[\d.]+)\s+(?P<hv50>[\d.]+)\s+(?P<hv100>[\d.]+)\s+"
    r"(?P<date>\d{6})\s+(?P<iv>[\d.]+)\s+(?P<days>\d+)/\s*(?P<pct>\d+)%ile\s*(?P<close>[\d.]+)\s*$"
)
_SPAN = re.compile(r"<span class=['\"]vol-line['\"]>(.*?)</span>", re.S)


@dataclass(frozen=True)
class OsRow:
    ticker: str
    day: _dt.date
    cur_iv: float  # decimal
    hv20: float
    hv50: float
    hv100: float
    days: int
    percentile: float  # 0..1
    close: float

    def to_iv_row(self) -> IvRow:
        return IvRow(
            ticker=self.ticker,
            day=self.day,
            iv30=self.cur_iv,
            method="os_cur_iv",
            source=OPTIONSTRATEGIST,
            spot=self.close,
            spot_basis="last_close",
            hv20=self.hv20,
            hv50=self.hv50,
            hv100=self.hv100,
            ext_days=self.days,
            ext_percentile=self.percentile,
        )


def _lines(text: str) -> list[str]:
    spans = _SPAN.findall(text)
    raw = spans if spans else text.splitlines()
    return [html.unescape(re.sub(r"<[^>]+>", "", s)).rstrip("\n") for s in raw]


def parse(text: str) -> list[OsRow]:
    """Parse the page HTML (or its saved ``<pre>`` text) into rows.

    Lines that don't match the fixed layout (headers, notes, ``@`` serial-option
    rows) are ignored. A ticker listed twice keeps the newest ``DATE``.
    """
    out: dict[str, OsRow] = {}
    for line in _lines(text):
        m = _LINE.match(line)
        if not m or m["sym"].startswith("@"):
            continue
        cur = float(m["iv"])
        if cur <= 0:
            continue
        d = m["date"]
        day = _dt.date(2000 + int(d[:2]), int(d[2:4]), int(d[4:6]))
        sym = m["sym"].upper().replace("/", ".")
        row = OsRow(
            ticker=sym,
            day=day,
            cur_iv=cur / 100.0,
            hv20=float(m["hv20"]) / 100.0,
            hv50=float(m["hv50"]) / 100.0,
            hv100=float(m["hv100"]) / 100.0,
            days=int(m["days"]),
            percentile=min(int(m["pct"]), 100) / 100.0,
            close=float(m["close"]),
        )
        if sym not in out or row.day > out[sym].day:
            out[sym] = row
    return list(out.values())


def fetch(get: Callable[[str], bytes]) -> str:  # pragma: no cover - network
    return get(OS_URL).decode("utf-8", "replace")
