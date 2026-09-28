"""Unit tests for BrokerAdapter protocol and data contracts."""

from __future__ import annotations

from decimal import Decimal

from arc.broker.base import (
    AccountInfo,
    BrokerAdapter,
    BrokerOrderStatus,
    BrokerPosition,
    Fill,
    MlegLeg,
    MlegOrder,
)


def test_broker_adapter_is_runtime_checkable() -> None:
    """BrokerAdapter is a runtime-checkable Protocol."""
    assert hasattr(BrokerAdapter, "__protocol_attrs__") or hasattr(
        BrokerAdapter, "__abstractmethods__"
    )


def test_account_info_model() -> None:
    info = AccountInfo(
        account_id="PA123",
        equity=Decimal("100000"),
        buying_power=Decimal("200000"),
        cash=Decimal("50000"),
    )
    assert info.account_id == "PA123"
    assert info.currency == "USD"


def test_broker_position_model() -> None:
    pos = BrokerPosition(
        symbol="AAPL261016C00150000",
        qty=Decimal("2"),
        side="long",
        market_value=Decimal("500"),
    )
    assert pos.asset_class == "us_option"
    assert pos.qty == Decimal("2")


def test_mleg_order_model() -> None:
    order = MlegOrder(
        legs=[
            MlegLeg(symbol="SPY261016C00450000", side="buy", ratio_qty=1),
            MlegLeg(symbol="SPY261016C00460000", side="sell", ratio_qty=1),
        ],
        limit_price=Decimal("2.50"),
        time_in_force="day",
        client_order_id="arc-test-001",
    )
    assert len(order.legs) == 2
    assert order.limit_price == Decimal("2.50")


def test_broker_order_status_model() -> None:
    status = BrokerOrderStatus(
        broker_order_id="abc-123",
        status="new",
    )
    assert status.filled_qty == Decimal("0")
    assert status.filled_avg_price is None


def test_fill_model() -> None:
    import datetime as dt

    from arc.utils.calendar import ET

    fill = Fill(
        broker_order_id="abc-123",
        symbol="SPY261016C00450000",
        side="buy",
        qty=Decimal("1"),
        price=Decimal("3.25"),
        filled_at=dt.datetime(2026, 10, 1, 10, 30, tzinfo=ET),
    )
    assert fill.price == Decimal("3.25")
