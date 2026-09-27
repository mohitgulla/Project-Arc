"""OCC option symbol parse / format.

The OCC (Options Clearing Corporation) symbology is::

    ROOT (1-6 chars, space-padded to 6) + YYMMDD + C|P + STRIKE (x1000, 8 digits)

e.g. ``"AAPL  260117C00200000"`` = AAPL 2026-01-17 200 call.

Brokers (Alpaca included) usually drop the padding: ``"AAPL260117C00200000"``.
:func:`parse_occ` accepts both forms; :func:`format_occ` emits the compact
form by default and the padded 21-char form with ``padded=True``.
"""

from __future__ import annotations

import datetime as dt
import re
from decimal import Decimal, InvalidOperation

from pydantic import BaseModel, ConfigDict, Field, field_validator

from arc.pricing.bs import OptionKind

__all__ = ["OccSymbol", "format_occ", "parse_occ"]

_ROOT_WIDTH = 6
_STRIKE_SCALE = Decimal(1000)
_MAX_STRIKE = Decimal("99999.999")
_OCC_RE = re.compile(r"^(?P<root>[A-Z0-9]{1,6}) *(?P<date>\d{6})(?P<cp>[CP])(?P<strike>\d{8})$")


class OccSymbol(BaseModel):
    """A parsed OCC option symbol."""

    model_config = ConfigDict(frozen=True)

    root: str = Field(..., min_length=1, max_length=_ROOT_WIDTH, description="Underlying root")
    expiration: dt.date
    kind: OptionKind
    strike: Decimal = Field(..., gt=0, le=_MAX_STRIKE)

    @field_validator("root")
    @classmethod
    def _root_upper(cls, v: str) -> str:
        if not re.fullmatch(r"[A-Z0-9]{1,6}", v):
            msg = f"invalid OCC root {v!r}: 1-6 chars of A-Z, 0-9"
            raise ValueError(msg)
        return v

    @field_validator("strike")
    @classmethod
    def _strike_precision(cls, v: Decimal) -> Decimal:
        if (v * _STRIKE_SCALE) != (v * _STRIKE_SCALE).to_integral_value():
            msg = f"strike {v} has more than 3 decimal places"
            raise ValueError(msg)
        return v

    @field_validator("expiration")
    @classmethod
    def _year_range(cls, v: dt.date) -> dt.date:
        if not 2000 <= v.year <= 2099:
            msg = f"expiration year {v.year} outside OCC YY range 2000-2099"
            raise ValueError(msg)
        return v

    def format(self, *, padded: bool = False) -> str:
        """Render as an OCC string (compact by default)."""
        return format_occ(self.root, self.expiration, self.kind, self.strike, padded=padded)

    def __str__(self) -> str:
        return self.format()


def parse_occ(symbol: str) -> OccSymbol:
    """Parse a padded or compact OCC symbol.

    Raises
    ------
    ValueError
        If *symbol* is not a well-formed OCC symbol.
    """
    m = _OCC_RE.match(symbol.strip())
    if m is None:
        msg = f"not an OCC option symbol: {symbol!r}"
        raise ValueError(msg)
    date_s = m["date"]
    try:
        expiration = dt.date(2000 + int(date_s[:2]), int(date_s[2:4]), int(date_s[4:6]))
    except ValueError as exc:
        msg = f"invalid OCC expiration {date_s!r} in {symbol!r}"
        raise ValueError(msg) from exc
    strike = Decimal(int(m["strike"])) / _STRIKE_SCALE
    kind = OptionKind.CALL if m["cp"] == "C" else OptionKind.PUT
    return OccSymbol(root=m["root"], expiration=expiration, kind=kind, strike=strike)


def format_occ(
    root: str,
    expiration: dt.date,
    kind: OptionKind | str,
    strike: Decimal | float | str,
    *,
    padded: bool = False,
) -> str:
    """Format components into an OCC symbol.

    *kind* accepts :class:`OptionKind`, ``"c"``/``"p"``, ``"call"``/``"put"``.
    """
    k = str(kind).lower()
    if k in ("c", "call"):
        cp = "C"
    elif k in ("p", "put"):
        cp = "P"
    else:
        msg = f"invalid option kind {kind!r}"
        raise ValueError(msg)
    try:
        strike_d = Decimal(str(strike))
    except InvalidOperation as exc:
        msg = f"invalid strike {strike!r}"
        raise ValueError(msg) from exc
    # Validate everything through the model (root charset, strike precision, year).
    occ = OccSymbol(
        root=root,
        expiration=expiration,
        kind=OptionKind.CALL if cp == "C" else OptionKind.PUT,
        strike=strike_d,
    )
    root_part = occ.root.ljust(_ROOT_WIDTH) if padded else occ.root
    strike_part = f"{int(occ.strike * _STRIKE_SCALE):08d}"
    return f"{root_part}{occ.expiration:%y%m%d}{cp}{strike_part}"
