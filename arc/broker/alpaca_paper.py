"""Alpaca paper-trading BrokerAdapter implementation.

Reads ``ALPACA_API_KEY`` and ``ALPACA_SECRET_KEY`` from the environment
(loaded via ``~/.hermes/.env``).  The base URL is **hard-pinned** to
``https://paper-api.alpaca.markets`` when ``ARC_ENV=paper`` (default).

See PLAN.md §4 (E1.4) and AGENTS.md.
"""

from __future__ import annotations

import datetime as dt  # noqa: TC003 — used at runtime
import os
import uuid
from decimal import Decimal

import structlog
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import (
    OrderClass,
    OrderSide,
    TimeInForce,
)
from alpaca.trading.requests import LimitOrderRequest, OptionLegRequest

from arc.broker.base import (
    AccountInfo,
    BrokerOrderStatus,
    BrokerPosition,
    Fill,
    MlegOrder,
)

log = structlog.get_logger()

# ---------------------------------------------------------------------------
# Time-in-force mapping
# ---------------------------------------------------------------------------

_TIF_MAP: dict[str, TimeInForce] = {
    "day": TimeInForce.DAY,
    "gtc": TimeInForce.GTC,
    "ioc": TimeInForce.IOC,
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PAPER_BASE_URL = "https://paper-api.alpaca.markets"


def _require_paper() -> None:
    """Fail-fast if ARC_ENV is not paper."""
    env_val = os.environ.get("ARC_ENV", "paper").lower()
    if env_val != "paper":
        msg = f"AlpacaPaperBroker is paper-only. ARC_ENV={env_val!r} is not allowed."
        raise RuntimeError(msg)


def _make_client() -> TradingClient:
    """Build a TradingClient pointed at the paper endpoint."""
    api_key = os.environ.get("ALPACA_API_KEY", "")
    secret_key = os.environ.get("ALPACA_SECRET_KEY", "")
    if not api_key or not secret_key:
        msg = (
            "ALPACA_API_KEY and ALPACA_SECRET_KEY must be set in the "
            "environment (see ~/.hermes/.env)."
        )
        raise RuntimeError(msg)
    return TradingClient(
        api_key=api_key,
        secret_key=secret_key,
        paper=True,
        url_override=_PAPER_BASE_URL,
    )


def build_mleg_request(order: MlegOrder) -> LimitOrderRequest:
    """Build an Alpaca multi-leg limit order request.

    Alpaca rejects a top-level ``symbol`` on ``order_class=mleg`` (422
    ``symbol is not allowed for mleg order``); contracts go only in ``legs``.
    A single leg with ratio 1 (long call / long put, D25 cash_debit) is sent as a
    simple limit order on that contract: the limit is then the absolute premium.
    """
    tif = _TIF_MAP.get(order.time_in_force.lower(), TimeInForce.DAY)
    client_order_id = order.client_order_id or f"arc-{uuid.uuid4().hex[:12]}"
    if len(order.legs) == 1 and order.legs[0].ratio_qty == 1:
        (leg,) = order.legs
        return LimitOrderRequest(
            symbol=leg.symbol,
            qty=float(order.qty),
            side=OrderSide.BUY if leg.side == "buy" else OrderSide.SELL,
            time_in_force=tif,
            limit_price=float(abs(order.limit_price)),
            client_order_id=client_order_id,
        )
    legs = [
        OptionLegRequest(
            symbol=leg.symbol,
            ratio_qty=float(leg.ratio_qty),
            side=OrderSide.BUY if leg.side == "buy" else OrderSide.SELL,
        )
        for leg in order.legs
    ]
    # LimitOrderRequest (not the base OrderRequest, which has no limit_price
    # field and silently drops it). Positive limit = net debit, negative = credit.
    return LimitOrderRequest(
        qty=float(order.qty),
        order_class=OrderClass.MLEG,
        time_in_force=tif,
        limit_price=float(order.limit_price),
        legs=legs,
        client_order_id=client_order_id,
    )


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class AlpacaPaperBroker:
    """BrokerAdapter backed by Alpaca's paper-trading API.

    The base URL is unconditionally pinned to ``paper-api.alpaca.markets``.
    """

    def __init__(self, client: TradingClient | None = None) -> None:
        _require_paper()
        self._client = client or _make_client()

    # -- account -------------------------------------------------------------

    def account(self) -> AccountInfo:
        acct = self._client.get_account()
        return AccountInfo(
            account_id=acct.account_number,
            equity=Decimal(acct.equity or "0"),
            buying_power=Decimal(acct.buying_power or "0"),
            cash=Decimal(acct.cash or "0"),
            currency=acct.currency or "USD",
            options_buying_power=(
                Decimal(acct.options_buying_power) if acct.options_buying_power else None
            ),
            non_marginable_buying_power=(
                Decimal(acct.non_marginable_buying_power)
                if acct.non_marginable_buying_power
                else None
            ),
            options_approved_level=acct.options_approved_level,
            last_equity=Decimal(acct.last_equity) if acct.last_equity else None,
        )

    # -- positions -----------------------------------------------------------

    def positions(self) -> list[BrokerPosition]:
        raw = self._client.get_all_positions()
        result: list[BrokerPosition] = []
        for pos in raw:
            result.append(
                BrokerPosition(
                    symbol=pos.symbol,
                    qty=Decimal(pos.qty),
                    side=pos.side.value if pos.side else "long",
                    market_value=Decimal(pos.market_value) if pos.market_value else None,
                    avg_entry_price=Decimal(pos.avg_entry_price) if pos.avg_entry_price else None,
                    unrealized_pl=Decimal(pos.unrealized_pl) if pos.unrealized_pl else None,
                    asset_class=str(pos.asset_class) if pos.asset_class else "us_option",
                )
            )
        return result

    # -- submit_mleg ---------------------------------------------------------

    def submit_mleg(self, order: MlegOrder) -> str:
        req = build_mleg_request(order)
        result = self._client.submit_order(req)
        broker_id = str(result["id"]) if isinstance(result, dict) else str(result.id)
        log.info(
            "mleg_order_submitted",
            broker_order_id=broker_id,
            client_order_id=req.client_order_id,
            legs=len(order.legs),
        )
        return broker_id

    # -- cancel --------------------------------------------------------------

    def cancel(self, broker_order_id: str) -> None:
        self._client.cancel_order_by_id(broker_order_id)
        log.info("order_cancelled", broker_order_id=broker_order_id)

    # -- order_status --------------------------------------------------------

    def order_status(self, broker_order_id: str) -> BrokerOrderStatus:
        order = self._client.get_order_by_id(broker_order_id)
        if isinstance(order, dict):
            # dict fallback
            return BrokerOrderStatus(
                broker_order_id=str(order.get("id", "")),
                status=str(order.get("status", "unknown")),
            )
        # AlpacaOrder (or duck-typed equivalent)
        return BrokerOrderStatus(
            broker_order_id=str(order.id),
            client_order_id=order.client_order_id,
            status=order.status.value if order.status else "unknown",
            filled_qty=Decimal(str(order.filled_qty or "0")),
            filled_avg_price=(
                Decimal(str(order.filled_avg_price)) if order.filled_avg_price else None
            ),
            legs=(
                [
                    {
                        "symbol": leg.symbol,
                        "side": leg.side.value if leg.side else None,
                        "qty": str(leg.qty),
                        "filled_qty": str(leg.filled_qty or "0"),
                        "status": leg.status.value if leg.status else None,
                        "filled_avg_price": (
                            str(leg.filled_avg_price) if leg.filled_avg_price else None
                        ),
                    }
                    for leg in order.legs
                ]
                if order.legs
                else None
            ),
            created_at=order.created_at,
            updated_at=order.updated_at,
        )

    # -- fills ---------------------------------------------------------------

    def fills(self, since: dt.datetime) -> list[Fill]:
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        req = GetOrdersRequest(
            status=QueryOrderStatus.CLOSED,
            after=since,
            limit=500,
        )
        orders = self._client.get_orders(req)
        result: list[Fill] = []
        for order in orders:
            if isinstance(order, dict):
                continue
            if order.filled_at is None:
                continue
            # For mleg orders, report fills per leg
            if order.legs:
                for leg in order.legs:
                    if leg.filled_at and leg.filled_avg_price and leg.filled_qty:
                        result.append(
                            Fill(
                                broker_order_id=str(order.id),
                                symbol=leg.symbol or "",
                                side=leg.side.value if leg.side else "buy",
                                qty=Decimal(str(leg.filled_qty)),
                                price=Decimal(str(leg.filled_avg_price)),
                                filled_at=leg.filled_at,
                            )
                        )
            elif order.filled_avg_price and order.filled_qty:
                result.append(
                    Fill(
                        broker_order_id=str(order.id),
                        symbol=order.symbol or "",
                        side=order.side.value if order.side else "buy",
                        qty=Decimal(str(order.filled_qty)),
                        price=Decimal(str(order.filled_avg_price)),
                        filled_at=order.filled_at,
                    )
                )
        return result
