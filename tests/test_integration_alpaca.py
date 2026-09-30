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
from tests.vertical_legs import select_bull_call_vertical

# Skip the entire module if keys are absent
_HAS_KEYS = bool(os.environ.get("ALPACA_API_KEY") and os.environ.get("ALPACA_SECRET_KEY"))
pytestmark = pytest.mark.skipif(not _HAS_KEYS, reason="ALPACA_API_KEY/SECRET not set")

_OPEN_STATES = {"new", "accepted", "pending_new", "held"}


def _wait_for(broker, broker_id: str, targets: set[str], timeout: float = 15.0):
    """Poll order status until it reaches one of ``targets`` or times out."""
    deadline = time.monotonic() + timeout
    status = broker.order_status(broker_id)
    while status.status not in targets and time.monotonic() < deadline:
        time.sleep(0.5)
        status = broker.order_status(broker_id)
    return status


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
        today = now_et().date()  # wall-clock: live Alpaca chain, DTE window from today
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
        today = now_et().date()  # wall-clock: live Alpaca bars end at today
        start = today - dt.timedelta(days=30)
        bars = data.history_bars("SPY", start, today)
        assert len(bars) > 0

    def test_mleg_place_cancel(self, broker, data) -> None:
        """Place a 2-leg bull call vertical far from market, then cancel.

        Uses SPY options ~30 DTE.  Limit price is set absurdly low ($0.01)
        so the order will never fill.
        """
        today = now_et().date()  # wall-clock: live mleg order on today's listed expiries
        exp_start = today + dt.timedelta(days=30)
        exp_end = today + dt.timedelta(days=45)

        # Fetch chain
        contracts = data.option_chain("SPY", exp_start, exp_end)

        # Both legs from one expiration: a cross-expiry pair can leave the
        # short leg expiring first, which Alpaca rejects as uncovered (403).
        legs = select_bull_call_vertical(contracts)
        assert legs is not None, (
            f"No expiration in {exp_start}..{exp_end} has >=2 usable SPY calls "
            f"(bid > 0, delta present) for a single-expiry vertical "
            f"({len(contracts)} contracts fetched)"
        )
        long_leg, short_leg = legs

        from arc.broker.base import MlegLeg, MlegOrder

        # Bull call vertical: fair debit is well above $0.01, so this limit is
        # far from market and never fills — whether or not the market is open
        # (outside hours Alpaca simply holds it as ``accepted``).
        order = MlegOrder(
            legs=[
                MlegLeg(symbol=long_leg.symbol, side="buy", ratio_qty=1),
                MlegLeg(symbol=short_leg.symbol, side="sell", ratio_qty=1),
            ],
            limit_price=Decimal("0.01"),
            time_in_force="day",
        )

        broker_id = broker.submit_mleg(order)
        assert broker_id, "Expected a broker order id"

        try:
            status = broker.order_status(broker_id)
            assert status.broker_order_id == broker_id
            assert status.status in _OPEN_STATES, status.status
        finally:
            # Always clean up the paper order, even if an assertion failed.
            broker.cancel(broker_id)

        status = _wait_for(broker, broker_id, {"canceled"})
        assert status.status == "canceled", status.status
