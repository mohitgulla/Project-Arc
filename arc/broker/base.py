"""BrokerAdapter protocol — venue-agnostic broker interface.

See PLAN.md §2.2 (arc/broker/) and §4 (E1.4).
Only ``arc.execution.submit()`` may call ``submit_mleg``; personas never
call the broker directly.
"""

from __future__ import annotations

import datetime as dt  # noqa: TC003 — used at runtime in Protocol signatures
from decimal import Decimal
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Data contracts returned by the adapter
# ---------------------------------------------------------------------------


class AccountInfo(BaseModel):
    """Snapshot of the brokerage account."""

    account_id: str
    equity: Decimal
    buying_power: Decimal
    cash: Decimal
    currency: str = "USD"
    options_buying_power: Decimal | None = None
    non_marginable_buying_power: Decimal | None = None
    options_approved_level: int | None = None
    last_equity: Decimal | None = Field(
        None, description="Equity at the previous session close (gate daily-loss basis)"
    )


class BrokerPosition(BaseModel):
    """A single position as reported by the broker."""

    symbol: str
    qty: Decimal
    side: str  # "long" | "short"
    market_value: Decimal | None = None
    avg_entry_price: Decimal | None = None
    unrealized_pl: Decimal | None = None
    asset_class: str = "us_option"
    # E5.3a: intraday marks the monitor heartbeat carries for the tower.
    current_price: Decimal | None = Field(None, description="Broker's latest mark per share")
    lastday_price: Decimal | None = Field(None, description="Previous session close per share")
    change_today: Decimal | None = Field(
        None, description="Fractional change vs lastday_price (0.05 = +5%)"
    )


class MlegLeg(BaseModel):
    """One leg of a multi-leg order request."""

    symbol: str = Field(..., description="OCC option symbol")
    side: str = Field(..., description="buy | sell")
    ratio_qty: int = Field(1, ge=1)


class MlegOrder(BaseModel):
    """A multi-leg order to submit to the broker."""

    legs: list[MlegLeg]
    qty: int = Field(1, ge=1, description="Number of spread units (legs scale by ratio_qty)")
    limit_price: Decimal
    time_in_force: str = "day"
    client_order_id: str | None = None


class BrokerOrderStatus(BaseModel):
    """Status of an order as reported by the broker."""

    broker_order_id: str
    client_order_id: str | None = None
    status: str  # new, partially_filled, filled, canceled, expired, rejected, etc.
    filled_qty: Decimal = Decimal("0")
    filled_avg_price: Decimal | None = None
    legs: list[dict] | None = None
    created_at: dt.datetime | None = None
    updated_at: dt.datetime | None = None


class BrokerOrderRef(BaseModel):
    """One broker order as listed for the day (D32 order-budget cross-check)."""

    broker_order_id: str
    client_order_id: str | None = None
    status: str = ""
    asset_class: str = ""  # us_option for single-leg option orders; "" on mleg parents
    mleg: bool = False
    submitted_at: dt.datetime | None = None


class Fill(BaseModel):
    """A fill event from the broker."""

    broker_order_id: str
    symbol: str
    side: str
    qty: Decimal
    price: Decimal
    filled_at: dt.datetime


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class BrokerAdapter(Protocol):
    """Venue-agnostic broker interface.

    Methods
    -------
    account()
        Current account snapshot.
    positions()
        All open positions.
    submit_mleg(order)
        Submit a multi-leg options order; returns the broker order id.
    cancel(broker_order_id)
        Cancel an order by broker id.
    order_status(broker_order_id)
        Poll the current status of an order.
    fills(since)
        Return fills since *since*.

    Optional (duck-typed, not part of the protocol so fakes stay small):

    option_orders_since(since) -> list[BrokerOrderRef]
        Every option order (single-leg ``us_option`` or mleg) the broker lists
        since *since*, any status. The D32 order budget cross-checks the local
        count against it; adapters without it are counted locally only.
    """

    def account(self) -> AccountInfo: ...

    def positions(self) -> list[BrokerPosition]: ...

    def submit_mleg(self, order: MlegOrder) -> str: ...

    def cancel(self, broker_order_id: str) -> None: ...

    def order_status(self, broker_order_id: str) -> BrokerOrderStatus: ...

    def fills(self, since: dt.datetime) -> list[Fill]: ...
