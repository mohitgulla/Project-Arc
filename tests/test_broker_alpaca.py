"""Unit tests for AlpacaPaperBroker — mocked, no network."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from alpaca.trading.enums import (
    AssetClass,
    AssetExchange,
    OrderClass,
    OrderSide,
    OrderStatus,
    PositionSide,
)
from alpaca.trading.models import Position

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


def _alpaca_position(symbol: str, qty: str, side: object, asset_class: object) -> MagicMock:
    pos = MagicMock()
    pos.symbol = symbol
    pos.qty = qty
    pos.side = side
    pos.market_value = "700.00"
    pos.avg_entry_price = "3.50"
    pos.unrealized_pl = "50.00"
    pos.asset_class = asset_class
    return pos


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
        mock_acct.non_marginable_buying_power = "40000.00"
        mock_acct.options_approved_level = 3
        mock_acct.last_equity = "99000.00"
        mock_client.get_account.return_value = mock_acct

        broker = _make_broker(mock_client)
        info = broker.account()

        assert info.account_id == "PA12345"
        assert info.equity == Decimal("100000.00")
        assert info.last_equity == Decimal("99000.00")
        assert info.options_approved_level == 3

    @pytest.mark.parametrize(
        ("asset_class", "expected"),
        [
            (AssetClass.US_OPTION, "us_option"),
            (AssetClass.US_EQUITY, "us_equity"),
            (AssetClass.CRYPTO, "crypto"),
            ("us_option", "us_option"),  # plain str passes through unchanged
            (None, "us_option"),  # missing keeps the historical default
        ],
    )
    def test_positions_asset_class_is_plain_wire_value(
        self, asset_class: object, expected: str
    ) -> None:
        """E6.3a: ``str(AssetClass.US_OPTION)`` is ``"AssetClass.US_OPTION"``; map via .value."""
        mock_client = MagicMock()
        mock_client.get_all_positions.return_value = [
            _alpaca_position("SPY261016C00450000", "-2", PositionSide.SHORT, asset_class)
        ]
        (pos,) = _make_broker(mock_client).positions()

        assert pos.asset_class == expected
        assert type(pos.asset_class) is str and type(pos.side) is str
        assert pos.side == "short"
        assert pos.symbol == "SPY261016C00450000"
        assert pos.qty == Decimal("-2")

    def test_positions_carry_intraday_marks(self) -> None:
        """E5.3a: current_price / lastday_price / change_today mapped when present."""
        pos = _alpaca_position("SPY261016C00450000", "1", PositionSide.LONG, "us_option")
        pos.current_price = "3.75"
        pos.lastday_price = "3.40"
        pos.change_today = "0.1029"
        bare = _alpaca_position("SPY261016C00455000", "1", PositionSide.LONG, "us_option")
        bare.current_price = None
        bare.lastday_price = ""
        bare.change_today = "n/a"
        mock_client = MagicMock()
        mock_client.get_all_positions.return_value = [pos, bare]
        got, missing = _make_broker(mock_client).positions()
        assert (got.current_price, got.lastday_price, got.change_today) == (
            Decimal("3.75"), Decimal("3.40"), Decimal("0.1029"),
        )  # fmt: skip
        assert (missing.current_price, missing.lastday_price, missing.change_today) == (
            None, None, None,
        )  # fmt: skip

    def test_positions_from_real_alpaca_model(self) -> None:
        """Positions built by alpaca-py's own model parse to plain values."""
        raw = Position(
            asset_id=uuid4(), symbol="SPY261030C00736000", exchange=AssetExchange.EMPTY,
            asset_class=AssetClass.US_OPTION, avg_entry_price="5.10", qty="1",
            side=PositionSide.LONG, cost_basis="510", unrealized_pl="12.5",
            market_value="522.5", current_price="5.225", lastday_price="5.00",
            change_today="0.045",
        )  # fmt: skip
        mock_client = MagicMock()
        mock_client.get_all_positions.return_value = [raw]
        (pos,) = _make_broker(mock_client).positions()
        assert (pos.asset_class, pos.side) == ("us_option", "long")
        assert pos.unrealized_pl == Decimal("12.5")
        assert pos.current_price == Decimal("5.225") and pos.change_today == Decimal("0.045")

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
        mock_client = MagicMock()
        mock_order = MagicMock()
        mock_order.id = uuid4()
        mock_order.client_order_id = "arc-test-001"
        mock_order.status = OrderStatus.NEW
        mock_order.filled_qty = "0"
        mock_order.filled_avg_price = None
        mock_order.legs = None
        mock_order.created_at = dt.datetime(2026, 10, 1, 10, 0, tzinfo=ET)
        mock_order.updated_at = dt.datetime(2026, 10, 1, 10, 0, tzinfo=ET)
        mock_client.get_order_by_id.return_value = mock_order

        broker = _make_broker(mock_client)
        status = broker.order_status(str(mock_order.id))

        assert status.status == "new" and type(status.status) is str
        assert status.client_order_id == "arc-test-001"

    def test_order_status_carries_one_leg_side(self) -> None:
        """E6.2f: a simple one-leg order's side signs its unsigned average."""

        class _Order:  # duck-typed one-leg Alpaca order
            id = uuid4()
            client_order_id = "arc1.x.s1"
            status = OrderStatus.FILLED
            side = OrderSide.SELL
            filled_qty = "1"
            filled_avg_price = "43.45"
            legs = None
            created_at = updated_at = dt.datetime(2026, 10, 8, 10, 0, tzinfo=ET)

        mock_client = MagicMock()
        mock_client.get_order_by_id.return_value = _Order()
        status = _make_broker(mock_client).order_status("x")
        assert status.side == "sell" and type(status.side) is str
        assert status.filled_avg_price == Decimal("43.45")

        _Order.side = OrderSide.BUY
        assert _make_broker(mock_client).order_status("x").side == "buy"
        _Order.side = None  # mleg parents carry no side
        assert _make_broker(mock_client).order_status("x").side is None
        _Order.side = "sell_short"  # anything else is unknown, not guessed
        assert _make_broker(mock_client).order_status("x").side is None

    def test_order_status_with_legs(self) -> None:
        mock_client = MagicMock()
        mock_leg = MagicMock()
        mock_leg.symbol = "SPY261016C00450000"
        mock_leg.side = OrderSide.BUY
        mock_leg.qty = "1"
        mock_leg.filled_qty = "0"
        mock_leg.status = OrderStatus.NEW

        mock_order = MagicMock()
        mock_order.id = uuid4()
        mock_order.client_order_id = "arc-test-002"
        mock_order.status = OrderStatus.NEW
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
        # plain wire values, never "OrderSide.BUY" / "OrderStatus.NEW"
        assert (status.legs[0]["side"], status.legs[0]["status"]) == ("buy", "new")
        assert type(status.legs[0]["side"]) is str and type(status.legs[0]["status"]) is str

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
        mock_order.side = OrderSide.BUY
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
        assert fills[0].side == "buy" and type(fills[0].side) is str

    def test_fills_mleg_legs_use_plain_side(self) -> None:
        filled = dt.datetime(2026, 10, 1, 11, 0, tzinfo=ET)
        legs = []
        for sym, side in (
            ("SPY261016C00450000", OrderSide.BUY),
            ("SPY261016C00460000", OrderSide.SELL),
        ):
            leg = MagicMock()
            leg.symbol, leg.side, leg.filled_at = sym, side, filled
            leg.filled_qty, leg.filled_avg_price = "1", "2.00"
            legs.append(leg)
        order = MagicMock()
        order.id, order.filled_at, order.legs = uuid4(), filled, legs
        mock_client = MagicMock()
        mock_client.get_orders.return_value = [order]

        fills = _make_broker(mock_client).fills(dt.datetime(2026, 10, 1, tzinfo=ET))

        assert [(f.symbol, f.side) for f in fills] == [
            ("SPY261016C00450000", "buy"),
            ("SPY261016C00460000", "sell"),
        ]
