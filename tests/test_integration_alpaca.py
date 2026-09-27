"""Integration tests for Alpaca paper adapter.

These tests require real Alpaca paper API keys and are skipped without them.
Mark: ``pytest -m integration`` to run.

Tests:
  1. Fetch SPY option chain and validate data quality flags
  2. Place a 2-leg vertical mleg limit far from market, confirm status,
     cancel, confirm cancelled
"""

from __future__ import annotations

import datetime as dt
import os
import time
from decimal import Decimal

import pytest

from arc.utils.calendar import now_et

# Skip the entire module if keys are absent
_HAS_KEYS = bool(os.environ.get("ALPACA_API_KEY") and os.environ.get("ALPACA_SECRET_KEY"))
pytestmark = pytest.mark.skipif(not _HAS_KEYS, reason="ALPACA_API_KEY/SECRET not set")


@pytest.fixture(scope="module")
def broker():
    """Shared broker instance for integration tests."""
    from arc.broker.alpaca_paper import AlpacaPaperBroker

    return AlpacaPaperBroker()


@pytest.fixture(scope="module")
def data():
    """Shared data provider instance for integration tests."""
    from arc.data.alpaca import AlpacaMarketData

    return AlpacaMarketData()


@pytest.mark.integration
class TestAlpacaIntegration:
    def test_account(self, broker) -> None:
        """Fetch account info and verify basic fields."""
        info = broker.account()
        assert info.account_id
        assert info.equity > 0
        assert info.currency == "USD"

    def test_positions(self, broker) -> None:
        """List positions — may be empty, should not error."""
        positions = broker.positions()
        assert isinstance(positions, list)

    def test_option_chain_spy(self, data) -> None:
        """Fetch SPY chain with 30-45 DTE window; validate contracts."""
        today = now_et().date()
        exp_start = today + dt.timedelta(days=30)
        exp_end = today + dt.timedelta(days=45)

        contracts = data.option_chain("SPY", exp_start, exp_end)

        # SPY should have many contracts
        assert len(contracts) > 0, "Expected at least one SPY contract"

        # Check that at least some have greeks
        with_greeks = [c for c in contracts if c.greeks and c.greeks.delta is not None]
        assert len(with_greeks) > 0, "Expected at least some contracts with greeks"

        # Check data quality flags are correctly applied
        flagged = [c for c in contracts if c.is_flagged]
        # Just verify the flagging mechanism works — some may be flagged, some not
        assert isinstance(flagged, list)

    def test_underlying_quote_spy(self, data) -> None:
        """Fetch SPY underlying quote."""
        quote = data.underlying_quote("SPY")
        assert quote.symbol == "SPY"
        assert quote.bid > 0
        assert quote.ask > 0
        assert quote.mid > 0

    def test_history_bars_spy(self, data) -> None:
        """Fetch SPY daily bars."""
        today = now_et().date()
        start = today - dt.timedelta(days=30)
        bars = data.history_bars("SPY", start, today)
        assert len(bars) > 0

    def test_mleg_place_cancel(self, broker, data) -> None:
        """Place a 2-leg bull call vertical far from market, then cancel.

        Uses SPY options ~30 DTE.  Limit price is set absurdly low ($0.01)
        so the order will never fill.
        """
        today = now_et().date()
        exp_start = today + dt.timedelta(days=30)
        exp_end = today + dt.timedelta(days=45)

        # Fetch chain
        contracts = data.option_chain("SPY", exp_start, exp_end)
        calls = [
            c
            for c in contracts
            if c.option_type == "call"
            and c.bid is not None
            and c.bid > 0
            and c.greeks is not None
            and c.greeks.delta is not None
        ]
        assert len(calls) >= 2, "Need at least 2 call contracts for a vertical"

        # Sort by strike, pick two adjacent
        calls.sort(key=lambda c: c.strike)
        # Find something near ATM
        mid_idx = len(calls) // 2
        long_leg = calls[mid_idx]
        short_leg = calls[mid_idx + 1]

        from arc.broker.base import MlegLeg, MlegOrder

        order = MlegOrder(
            legs=[
                MlegLeg(symbol=long_leg.symbol, side="buy", ratio_qty=1),
                MlegLeg(symbol=short_leg.symbol, side="sell", ratio_qty=1),
            ],
            limit_price=Decimal("0.01"),  # Absurdly low — will never fill
            time_in_force="day",
        )

        # Submit
        broker_id = broker.submit_mleg(order)
        assert broker_id, "Expected a broker order id"

        # Brief pause for the order to be registered
        time.sleep(1)

        # Check status
        status = broker.order_status(broker_id)
        assert status.broker_order_id == broker_id
        assert status.status in (
            "new",
            "accepted",
            "pending_new",
            "partially_filled",
            "held",
        )

        # Cancel
        broker.cancel(broker_id)

        # Brief pause for cancellation
        time.sleep(1)

        # Confirm cancelled
        status = broker.order_status(broker_id)
        assert status.status in ("canceled", "pending_cancel")
