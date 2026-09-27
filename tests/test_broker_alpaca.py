"""Unit tests for AlpacaPaperBroker — mocked, no network."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from alpaca.trading.enums import OrderClass

from arc.broker.alpaca_paper import AlpacaPaperBroker, _require_paper, build_mleg_request
from arc.broker.base import MlegLeg, MlegOrder
from arc.utils.calendar import ET

# ---------------------------------------------------------------------------
# _require_paper
# ---------------------------------------------------------------------------


class TestRequirePaper:
    def test_paper_ok(self) -> None:
        """Default ARC_ENV=paper should not raise."""
        _require_paper()

    @patch.dict("os.environ", {"ARC_ENV": "live"})
    def test_live_rejected(self) -> None:
        with pytest.raises(RuntimeError, match="paper-only"):
            _require_paper()


# ---------------------------------------------------------------------------
# AlpacaPaperBroker with mocked client
# ---------------------------------------------------------------------------


def _make_broker(mock_client: MagicMock | None = None) -> AlpacaPaperBroker:
    """Build a broker with a mock TradingClient."""
    with patch.dict("os.environ", {"ALPACA_API_KEY": "test", "ALPACA_SECRET_KEY": "test"}):
        return AlpacaPaperBroker(client=mock_client or MagicMock())


class TestAlpacaPaperBrokerMocked:
    def test_account(self) -> None:
        mock_client = MagicMock()
        mock_acct = MagicMock()
        mock_acct.account_number = "PA12345"
        mock_acct.equity = "100000.00"
        mock_acct.buying_power = "200000.00"
        mock_acct.cash = "50000.00"
        mock_acct.currency = "USD"
        mock_acct.options_buying_power = "100000.00"
        mock_acct.options_approved_level = 3
        mock_client.get_account.return_value = mock_acct

        broker = _make_broker(mock_client)
        info = broker.account()

        assert info.account_id == "PA12345"
        assert info.equity == Decimal("100000.00")
        assert info.options_approved_level == 3

    def test_positions(self) -> None:
        mock_client = MagicMock()
        mock_pos = MagicMock()
        mock_pos.symbol = "SPY261016C00450000"
        mock_pos.qty = "2"
        mock_pos.side = MagicMock(value="long")
        mock_pos.market_value = "700.00"
        mock_pos.avg_entry_price = "3.50"
        mock_pos.unrealized_pl = "50.00"
        mock_pos.asset_class = MagicMock(__str__=lambda self: "us_option")
        mock_client.get_all_positions.return_value = [mock_pos]

        broker = _make_broker(mock_client)
        positions = broker.positions()

        assert len(positions) == 1
        assert positions[0].symbol == "SPY261016C00450000"
        assert positions[0].qty == Decimal("2")

    def test_submit_mleg(self) -> None:
        mock_client = MagicMock()
        mock_order = MagicMock()
        mock_order.id = uuid4()
        mock_client.submit_order.return_value = mock_order

        broker = _make_broker(mock_client)
        order = MlegOrder(
            legs=[
                MlegLeg(symbol="SPY261016C00450000", side="buy", ratio_qty=1),
                MlegLeg(symbol="SPY261016C00460000", side="sell", ratio_qty=1),
            ],
            limit_price=Decimal("2.50"),
            client_order_id="arc-test-001",
        )
        broker_id = broker.submit_mleg(order)

        assert broker_id == str(mock_order.id)
        mock_client.submit_order.assert_called_once()
        req = mock_client.submit_order.call_args.args[0]
        assert req.symbol is None
        assert req.order_class == OrderClass.MLEG

    def test_build_mleg_request_has_no_top_level_symbol(self) -> None:
        order = MlegOrder(
            legs=[
                MlegLeg(symbol="SPY261016C00450000", side="buy", ratio_qty=1),
                MlegLeg(symbol="SPY261016C00460000", side="sell", ratio_qty=2),
            ],
            qty=3,
            limit_price=Decimal("2.50"),
            time_in_force="gtc",
        )
        req = build_mleg_request(order)
        payload = req.to_request_fields()

        # Alpaca 422s on any top-level symbol for mleg orders.
        assert "symbol" not in payload
        assert payload["order_class"] == "mleg"
        assert payload["type"] == "limit"
        assert payload["time_in_force"] == "gtc"
        assert payload["qty"] == 3
        assert payload["limit_price"] == 2.5
        assert payload["client_order_id"].startswith("arc-")
        assert payload["legs"] == [
            {"symbol": "SPY261016C00450000", "ratio_qty": 1, "side": "buy"},
            {"symbol": "SPY261016C00460000", "ratio_qty": 2, "side": "sell"},
        ]

    def test_build_mleg_request_defaults(self) -> None:
        order = MlegOrder(
            legs=[
                MlegLeg(symbol="SPY261016C00450000", side="buy", ratio_qty=1),
                MlegLeg(symbol="SPY261016C00460000", side="sell", ratio_qty=1),
            ],
            limit_price=Decimal("-1.00"),
            time_in_force="bogus",
            client_order_id="arc-fixed",
        )
        payload = build_mleg_request(order).to_request_fields()
        assert payload["qty"] == 1
        assert payload["time_in_force"] == "day"
        assert payload["client_order_id"] == "arc-fixed"
        assert payload["limit_price"] == -1.0  # negative = net credit

    def test_cancel(self) -> None:
        mock_client = MagicMock()
        broker = _make_broker(mock_client)
        broker.cancel("order-123")
        mock_client.cancel_order_by_id.assert_called_once_with("order-123")

    def test_order_status(self) -> None:
        from alpaca.trading.enums import OrderStatus as AlpacaOrderStatus

        mock_client = MagicMock()
        mock_order = MagicMock()
        mock_order.id = uuid4()
        mock_order.client_order_id = "arc-test-001"
        mock_order.status = AlpacaOrderStatus.NEW
        mock_order.filled_qty = "0"
        mock_order.filled_avg_price = None
        mock_order.legs = None
        mock_order.created_at = dt.datetime(2026, 10, 1, 10, 0, tzinfo=ET)
        mock_order.updated_at = dt.datetime(2026, 10, 1, 10, 0, tzinfo=ET)
        mock_client.get_order_by_id.return_value = mock_order

        broker = _make_broker(mock_client)
        status = broker.order_status(str(mock_order.id))

        assert status.status == "new"
        assert status.client_order_id == "arc-test-001"

    def test_order_status_with_legs(self) -> None:
        from alpaca.trading.enums import OrderSide
        from alpaca.trading.enums import OrderStatus as AlpacaOrderStatus

        mock_client = MagicMock()
        mock_leg = MagicMock()
        mock_leg.symbol = "SPY261016C00450000"
        mock_leg.side = OrderSide.BUY
        mock_leg.qty = "1"
        mock_leg.filled_qty = "0"
        mock_leg.status = AlpacaOrderStatus.NEW

        mock_order = MagicMock()
        mock_order.id = uuid4()
        mock_order.client_order_id = "arc-test-002"
        mock_order.status = AlpacaOrderStatus.NEW
        mock_order.filled_qty = "0"
        mock_order.filled_avg_price = None
        mock_order.legs = [mock_leg]
        mock_order.created_at = dt.datetime(2026, 10, 1, 10, 0, tzinfo=ET)
        mock_order.updated_at = dt.datetime(2026, 10, 1, 10, 0, tzinfo=ET)
        mock_client.get_order_by_id.return_value = mock_order

        broker = _make_broker(mock_client)
        status = broker.order_status(str(mock_order.id))

        assert status.legs is not None
        assert len(status.legs) == 1
        assert status.legs[0]["symbol"] == "SPY261016C00450000"

    def test_fills_empty(self) -> None:
        mock_client = MagicMock()
        mock_client.get_orders.return_value = []

        broker = _make_broker(mock_client)
        fills = broker.fills(dt.datetime(2026, 10, 1, tzinfo=ET))

        assert fills == []

    def test_fills_with_results(self) -> None:
        mock_client = MagicMock()
        mock_order = MagicMock()
        mock_order.id = uuid4()
        mock_order.symbol = "SPY261016C00450000"
        mock_order.side = MagicMock(value="buy")
        mock_order.filled_at = dt.datetime(2026, 10, 1, 11, 0, tzinfo=ET)
        mock_order.filled_qty = "1"
        mock_order.filled_avg_price = "3.50"
        mock_order.legs = None
        # Make isinstance check work
        from alpaca.trading.models import Order as AlpacaOrder

        mock_order.__class__ = AlpacaOrder
        mock_client.get_orders.return_value = [mock_order]

        broker = _make_broker(mock_client)
        fills = broker.fills(dt.datetime(2026, 10, 1, tzinfo=ET))

        assert len(fills) == 1
        assert fills[0].price == Decimal("3.50")
