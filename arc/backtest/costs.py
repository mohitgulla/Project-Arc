"""The one transaction-cost model (PLAN §6 item 6, §7 QFX lesson, D23).

Shared by the backtester, the scanner's ``ev_proxy``, the E2.4 exit model, the
Investor's live exits and the proposal card's Net EV. Its values come from
``config/costs.yaml`` (:func:`load_cost_model`); the class defaults keep the
historical backtest assumptions (commission only, no pass-through fees) so an
explicit ``CostModel()`` in a test or sensitivity grid means what it always did.

Fill model
----------
Every leg fills at ``mid ± x · spread`` where ``x`` = :attr:`CostModel.slippage_frac`:
buys pay ``mid + x·spread``, sells receive ``max(mid − x·spread, 0)``.
``x = 0`` is a fill at mid, ``x = 0.5`` is crossing to the far touch. The
difference to mid is the **spread & slippage** cost.

Spread source
-------------
* When the row carries a closing NBBO (ThetaData EOD) or a live quote, the
  quoted ``ask − bid`` is used and ``mid = (bid + ask) / 2``.
* Alpaca history has **no historical quotes** (E7.1 finding), only trade
  closes. Then ``mid`` is the trade close and the spread is *estimated* as
  ``max(spread_min, spread_pct · mid)``. This estimate is an explicit,
  reported assumption; the baseline report runs a sensitivity grid over it.

Fees (per contract, per trade)
------------------------------
* ``commission_per_contract`` — broker commission, both sides.
* ``orf_per_contract`` — Options Regulatory Fee, both sides.
* ``occ_per_contract`` — OCC clearing fee, both sides.
* ``cat_per_share`` — FINRA CAT fee per executed equivalent share (×100 per
  contract), both sides.
* ``taf_per_contract_sell`` — FINRA TAF, **sells only**.
* ``sec_rate_sell`` — SEC Section 31 fee × trade value, **sells only**.

A trade's side is the order's side for that leg: opening a long leg buys,
opening a short leg sells; closing reverses it. An option that expires is
settled without a trade: an OTM leg pays nothing, an ITM leg is charged the
closing-trade fees (commission and pass-through fees at intrinsic value).
"""

from __future__ import annotations

import math
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "DEFAULT_COSTS_PATH",
    "MULTIPLIER",
    "CostModel",
    "FeeBreakdown",
    "LegSide",
    "load_cost_model",
]

LegSide = int  # +1 buy (long), -1 sell (short)

MULTIPLIER = 100  # shares per equity option contract
DEFAULT_COSTS_PATH = Path(__file__).resolve().parent.parent.parent / "config" / "costs.yaml"


class FeeBreakdown(BaseModel):
    """Dollar fees of one or more trades, by type (all ≥ 0)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    commission: float = 0.0
    orf: float = 0.0
    occ: float = 0.0
    cat: float = 0.0
    taf: float = 0.0
    sec: float = 0.0

    @property
    def regulatory(self) -> float:
        """Exchange/regulatory pass-through fees (everything except commission)."""
        return self.orf + self.occ + self.cat + self.taf + self.sec

    @property
    def total(self) -> float:
        return self.commission + self.regulatory

    def __add__(self, other: FeeBreakdown) -> FeeBreakdown:
        return FeeBreakdown(
            commission=self.commission + other.commission,
            orf=self.orf + other.orf,
            occ=self.occ + other.occ,
            cat=self.cat + other.cat,
            taf=self.taf + other.taf,
            sec=self.sec + other.sec,
        )

    def scaled(self, k: float) -> FeeBreakdown:
        return FeeBreakdown(
            commission=self.commission * k,
            orf=self.orf * k,
            occ=self.occ * k,
            cat=self.cat * k,
            taf=self.taf * k,
            sec=self.sec * k,
        )


class CostModel(BaseModel):
    """Slippage + commission + pass-through fee assumptions. Prices are per share."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    slippage_frac: float = Field(
        0.25, ge=0.0, le=1.0, description="x: fill at mid ± x·spread (0 = mid, 0.5 = touch)"
    )
    commission_per_contract: float = Field(
        0.65, ge=0.0, description="$ per contract per side (opening and ITM closing trades)"
    )
    spread_pct: float = Field(
        0.04, ge=0.0, description="Estimated spread as a fraction of mid when no quote exists"
    )
    spread_min: float = Field(
        0.03, ge=0.0, description="Estimated spread floor ($/share) when no quote exists"
    )
    orf_per_contract: float = Field(0.0, ge=0.0, description="Options Regulatory Fee, both sides")
    occ_per_contract: float = Field(0.0, ge=0.0, description="OCC clearing fee, both sides")
    cat_per_share: float = Field(
        0.0, ge=0.0, description="FINRA CAT fee per executed equivalent share, both sides"
    )
    taf_per_contract_sell: float = Field(0.0, ge=0.0, description="FINRA TAF, sells only")
    sec_rate_sell: float = Field(0.0, ge=0.0, description="SEC fee × trade value, sells only")

    def spread(self, mid: float, bid: float | None = None, ask: float | None = None) -> float:
        """Quoted spread if a valid NBBO is present, else the estimate."""
        if _valid_quote(bid, ask):
            assert bid is not None and ask is not None  # narrowed by _valid_quote
            return ask - bid
        return max(self.spread_min, self.spread_pct * max(mid, 0.0))

    def mid(self, close: float | None, bid: float | None = None, ask: float | None = None) -> float:
        """Mid from a valid NBBO, else the trade close."""
        if _valid_quote(bid, ask):
            assert bid is not None and ask is not None
            return (bid + ask) / 2.0
        if close is None or not math.isfinite(close) or close < 0:
            msg = "row has neither a valid quote nor a close price"
            raise ValueError(msg)
        return close

    def fill(self, mid: float, spread: float, side: LegSide) -> float:
        """Per-share fill for one leg: buys pay up, sells give up, never below 0."""
        if side not in (1, -1):
            msg = "side must be +1 (buy) or -1 (sell)"
            raise ValueError(msg)
        return max(mid + side * self.slippage_frac * spread, 0.0)

    @property
    def per_contract_both_sides(self) -> float:
        """$ per contract charged on every trade regardless of side."""
        return (
            self.commission_per_contract
            + self.orf_per_contract
            + self.occ_per_contract
            + self.cat_per_share * MULTIPLIER
        )

    def fees(self, contracts: int) -> float:
        """Side-independent fees for *contracts* contracts (commission, ORF, OCC, CAT).

        Sell-side fees depend on the side and price: use :meth:`trade_fees`.
        """
        return self.per_contract_both_sides * contracts

    def sell_fee_per_contract(self, price: float) -> float:
        """Sell-only fees ($) per contract at *price* per share (TAF + SEC).

        Plain arithmetic, so the exit model may pass a numpy array of prices.
        """
        return self.taf_per_contract_sell + self.sec_rate_sell * price * MULTIPLIER

    def trade_fees(self, contracts: float, side: LegSide, price: float) -> float:
        """All fees ($) for trading *contracts* contracts on one side at *price* per share."""
        return self.fee_breakdown(contracts, side, price).total

    def fee_breakdown(self, contracts: float, side: LegSide, price: float) -> FeeBreakdown:
        """:meth:`trade_fees` itemised by fee type."""
        sell = side < 0
        return FeeBreakdown(
            commission=self.commission_per_contract * contracts,
            orf=self.orf_per_contract * contracts,
            occ=self.occ_per_contract * contracts,
            cat=self.cat_per_share * MULTIPLIER * contracts,
            taf=self.taf_per_contract_sell * contracts if sell else 0.0,
            sec=self.sec_rate_sell * max(price, 0.0) * MULTIPLIER * contracts if sell else 0.0,
        )


def load_cost_model(path: Path | str | None = None) -> CostModel:
    """Load and validate the cost config (default: ``config/costs.yaml``)."""
    p = Path(path) if path is not None else DEFAULT_COSTS_PATH
    data = yaml.safe_load(p.read_text()) or {}
    return CostModel.model_validate(data.get("costs", data))


def _valid_quote(bid: float | None, ask: float | None) -> bool:
    if bid is None or ask is None:
        return False
    if not (math.isfinite(bid) and math.isfinite(ask)):
        return False
    return 0.0 <= bid <= ask and ask > 0.0
