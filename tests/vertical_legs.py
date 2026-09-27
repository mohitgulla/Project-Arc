"""Pure leg-selection helper for the mleg integration test (no network).

Picks a bull call debit vertical from an option chain that may span several
expirations. Both legs always come from a single expiration; pairing across
expirations can produce a short leg that expires before the long leg, which
Alpaca rejects as uncovered (403 40310000).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import datetime as dt
    from collections.abc import Iterable

    from arc.data.base import OptionContract


def _usable_call(c: OptionContract) -> bool:
    return (
        c.option_type == "call"
        and c.bid is not None
        and c.bid > 0
        and c.greeks is not None
        and c.greeks.delta is not None
    )


def select_bull_call_vertical(
    contracts: Iterable[OptionContract],
) -> tuple[OptionContract, OptionContract] | None:
    """Return ``(long_leg, short_leg)`` for a single-expiry bull call vertical.

    Uses the nearest expiration that has at least two usable calls with
    distinct strikes. Within that expiration, picks two adjacent strikes near
    the middle of the chain: long the lower strike, short the higher strike.
    Returns ``None`` if no expiration qualifies.
    """
    by_expiry: dict[dt.date, dict[float, OptionContract]] = {}
    for c in contracts:
        if not _usable_call(c):
            continue
        # One contract per strike per expiry (first seen wins).
        by_expiry.setdefault(c.expiration, {}).setdefault(c.strike, c)

    for expiry in sorted(by_expiry):
        strikes = sorted(by_expiry[expiry])
        if len(strikes) < 2:
            continue
        mid_idx = (len(strikes) - 1) // 2
        by_strike = by_expiry[expiry]
        return by_strike[strikes[mid_idx]], by_strike[strikes[mid_idx + 1]]
    return None
