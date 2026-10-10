"""Proposal analytics: the numbers a trader reads before approving (E6.1a, D22/D23).

:class:`ProposalAnalytics` is computed once by the propose step from the same
chain snapshot the proposal was priced with, and stored as part of E7.4's
:class:`~arc.journal.models.MarketContext` (``market_contexts`` row, one copy).
The proposal card reads it back through :mod:`arc.approvals.trail` and only
formats it: nothing on the card is recomputed from a live quote.

It is deliberately **not** part of :class:`~arc.models.Proposal`, so the gate's
proposal hash is unchanged.

The math helpers here are pure and deterministic (no I/O, no LLM):

- moneyness ``(K − S) / S``
- σ-distance ``ln(K / S) / (IV · √t)`` with ``t = DTE / 365``
- 1σ expected move to expiry ``S · IV · √t``
"""

from __future__ import annotations

import datetime as _dt  # noqa: TC003 - pydantic fields
import math
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field

from arc.backtest.costs import CostModel, FeeBreakdown  # noqa: TC001 - pydantic fields
from arc.exits.model import ExitModelResult  # noqa: TC001 - pydantic fields

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = [
    "ANALYTICS_VERSION",
    "BreakevenStat",
    "LegAnalytics",
    "ProposalAnalytics",
    "VolStats",
    "be_atr_multiple",
    "debit_direction",
    "directional_breakeven",
    "expected_move",
    "moneyness_pct",
    "otm",
    "sigma_distance",
    "sigma_t",
]

ANALYTICS_VERSION = 1
_FORBID = ConfigDict(extra="forbid", frozen=True)


def sigma_t(iv: float | None, dte: int) -> float | None:
    """IV × √(DTE/365): one standard deviation of log-return to expiry."""
    if iv is None or not math.isfinite(iv) or iv <= 0 or dte <= 0:
        return None
    return iv * math.sqrt(dte / 365.0)


def moneyness_pct(strike: float, spot: float) -> float:
    """Signed distance of *strike* from *spot*: ``(K − S) / S``."""
    if spot <= 0:
        msg = "spot must be positive"
        raise ValueError(msg)
    return (strike - spot) / spot


def sigma_distance(level: float, spot: float, iv: float | None, dte: int) -> float | None:
    """Signed distance of *level* from *spot* in σ to expiry: ``ln(K/S) / (IV·√t)``."""
    s = sigma_t(iv, dte)
    if s is None or level <= 0 or spot <= 0:
        return None
    return math.log(level / spot) / s


def expected_move(spot: float, iv: float | None, dte: int) -> float | None:
    """±1σ move of the underlying to expiry, in dollars: ``S · IV · √t``."""
    s = sigma_t(iv, dte)
    return None if s is None else spot * s


def otm(kind: Literal["call", "put"], strike: float, spot: float) -> bool:
    """True when the option is out of the money at *spot* (ATM counts as OTM)."""
    return strike >= spot if kind == "call" else strike <= spot


def be_atr_multiple(breakeven: float, spot: float, atr14: float | None, dte: int) -> float | None:
    """E16.5 (D76): ``|BE − S| / (ATR14 · √DTE)``, the breakeven in realised-move terms.

    ATR14 is the stock's average daily true range ($); √DTE scales it to the days
    left (calendar DTE, the same days the σ-distance uses). Example: spot 100, BE
    105, ATR 2, 25 DTE → 5 / (2 · 5) = 0.5. ``None`` without a positive ATR or DTE.
    """
    if atr14 is None or not math.isfinite(atr14) or atr14 <= 0 or dte <= 0 or spot <= 0:
        return None
    return abs(breakeven - spot) / (atr14 * math.sqrt(dte))


def directional_breakeven(breakevens: Sequence[float], direction: int) -> float | None:
    """E16.5: the breakeven a debit structure must cross in its own direction.

    *direction* ``+1`` (long call, bull call debit) → the highest breakeven; ``-1``
    (long put, bear put debit) → the lowest; ``0`` (credit / neutral) → ``None``.
    """
    if not breakevens or direction == 0:
        return None
    return max(breakevens) if direction > 0 else min(breakevens)


def debit_direction(net_debit_credit: float, long_kinds: Sequence[str]) -> int:
    """E16.5: ``+1`` / ``-1`` for a debit structure bought on calls / puts, else ``0``.

    *net_debit_credit* is per share (> 0 = a debit); *long_kinds* are the option
    types (``call`` / ``put``) of the bought legs. Credit structures, and a debit
    with long legs of both types (not on any profile today), return 0: no single
    direction, so no directional breakeven.
    """
    kinds = set(long_kinds)
    if net_debit_credit <= 0 or len(kinds) != 1:
        return 0
    return 1 if kinds == {"call"} else -1 if kinds == {"put"} else 0


class LegAnalytics(BaseModel):
    """One leg's quote, liquidity, moneyness and entry cost, frozen at proposal time."""

    model_config = _FORBID

    occ_symbol: str
    side: Literal["long", "short"]
    ratio: int = Field(..., ge=1)
    kind: Literal["call", "put"]
    strike: float
    expiration: _dt.date
    bid: float | None = None
    ask: float | None = None
    mid: float | None = None
    bid_size: float | None = Field(None, description="Top of book; None when the feed omits it")
    ask_size: float | None = None
    open_interest: int | None = None
    volume: int | None = None
    iv: float | None = None
    delta: float | None = None
    quote_ts: _dt.datetime | None = None
    spread: float | None = Field(None, description="ask − bid, $/share")
    spread_pct: float | None = Field(None, description="spread / mid")
    moneyness_pct: float | None = Field(None, description="(K − S) / S")
    sigma_distance: float | None = Field(None, description="ln(K/S) / (ATM IV · √t)")
    otm: bool | None = None
    entry_fill: float | None = Field(None, description="Modelled fill, $/share (mid ± x·spread)")
    entry_slippage: float = Field(0.0, ge=0.0, description="$ per unit: |fill − mid| × ratio × 100")
    entry_fees: FeeBreakdown = Field(default_factory=FeeBreakdown)


class BreakevenStat(BaseModel):
    model_config = _FORBID

    price: float
    pct: float = Field(..., description="(BE − S) / S")
    sigma: float | None = Field(None, description="ln(BE/S) / (ATM IV · √t)")
    atr_multiple: float | None = Field(
        None,
        description="E16.5 (D76): |BE − S| / (ATR14 · √DTE); None without the ticker's ATR14",
    )


class VolStats(BaseModel):
    """Vol context: scan-time ATM IV plus the regime FeatureSnapshot's rank/HV fields."""

    model_config = _FORBID

    atm_iv: float | None = None
    iv_rank: float | None = None
    iv_percentile: float | None = None
    hv20: float | None = None
    hv60: float | None = None
    iv_hv20: float | None = Field(None, description="ATM IV / HV20")
    features_as_of: _dt.date | None = None


class ProposalAnalytics(BaseModel):
    """Everything the v2 proposal card shows beyond the Proposal itself."""

    model_config = _FORBID

    version: Literal[1] = ANALYTICS_VERSION
    spot: float
    spot_as_of: _dt.datetime | None = None
    prev_close: float | None = Field(None, description="Last close in the regime snapshot")
    prev_close_as_of: _dt.date | None = None
    day_change_pct: float | None = Field(None, description="spot / prev_close − 1")
    dte: int = Field(..., ge=0)
    sigma_t: float | None = Field(None, description="ATM IV · √(DTE/365)")
    expected_move: float | None = Field(None, description="±1σ to expiry, $")
    breakevens: list[BreakevenStat] = Field(default_factory=list)
    legs: list[LegAnalytics]
    vol: VolStats = Field(default_factory=lambda: VolStats())
    cost_model: CostModel = Field(..., description="The costs.yaml values this was priced with")
    entry_slippage: float = Field(..., ge=0.0, description="$ per unit, all legs")
    entry_fees: FeeBreakdown = Field(default_factory=FeeBreakdown)
    exit_model: ExitModelResult | None = Field(None, description="E2.4 managed vs static")
    account_profile: str | None = Field(None, description="D25 account profile, when configured")
    depth: Literal["top_of_book"] = Field(
        "top_of_book", description="Alpaca gives top-of-book only; no order-book depth"
    )
