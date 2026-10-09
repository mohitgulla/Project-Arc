"""Alpaca paper-trading BrokerAdapter implementation.

Reads ``ALPACA_API_KEY`` and ``ALPACA_SECRET_KEY`` from the environment
(loaded via ``~/.hermes/.env``) unless the caller passes explicit keys: the
integration tests pass the dedicated *test* paper account's keys (E6.2c), so
they never trade the production paper account. Production code never does.
The base URL is **hard-pinned** to ``https://paper-api.alpaca.markets`` when
``ARC_ENV=paper`` (default).

See PLAN.md §4 (E1.4) and AGENTS.md.
"""

from __future__ import annotations

import datetime as dt  # noqa: TC003 — used at runtime
import os
import uuid
from decimal import Decimal
from typing import TYPE_CHECKING, Any, cast

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
    BrokerActivity,
    BrokerOrderRef,
    BrokerOrderStatus,
    BrokerPosition,
    Fill,
    MlegOrder,
)
from arc.broker.http import install_timeouts

if TYPE_CHECKING:
    from arc.broker.registry import BrokerInfo

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


def _to_activity(raw: object, types: tuple[str, ...]) -> BrokerActivity | None:
    """One Alpaca activity row -> :class:`BrokerActivity` (``None`` = not ours/unparsable)."""
    if not isinstance(raw, dict):
        return None
    kind = str(raw.get("activity_type") or "")
    sym = str(raw.get("symbol") or "")
    if kind not in types or kind not in ("OPASN", "OPEXC", "OPEXP", "OPTRD") or not sym:
        return None
    day_raw = str(raw.get("date") or raw.get("transaction_time") or "")[:10]
    try:
        day = dt.date.fromisoformat(day_raw)
    except ValueError:
        return None
    price = raw.get("price")
    try:
        return BrokerActivity(
            id=str(raw.get("id") or f"{kind}:{sym}:{day_raw}"),
            activity_type=kind,  # type: ignore[arg-type]
            symbol=sym,
            qty=Decimal(str(raw.get("qty") or 0)),
            date=day,
            price=None if price in (None, "") else Decimal(str(price)),
            status=str(raw.get("status") or ""),
            raw={k: v for k, v in raw.items() if k != "account_id"},
        )
    except (ArithmeticError, ValueError):
        return None


def _enum_value(v: object) -> str:
    """Plain Alpaca wire value of an alpaca-py enum *or* a plain str.

    alpaca-py enums are ``str`` subclasses whose ``str()`` is ``"AssetClass.US_OPTION"``,
    not the wire value ``"us_option"``; always normalise through ``.value``.
    """
    return str(getattr(v, "value", v))


def _order_side(v: object) -> str | None:
    """``buy`` / ``sell`` from an order's side, else None (mleg parents carry none)."""
    if v is None:
        return None
    side = _enum_value(v).lower()
    return side if side in ("buy", "sell") else None


def _opt_dec(v: object) -> Decimal | None:
    """Optional Alpaca decimal field (wire string); anything else (None, "") -> ``None``."""
    if isinstance(v, bool) or not isinstance(v, str | int | float | Decimal) or v == "":
        return None
    try:
        return Decimal(str(v))
    except ArithmeticError:  # decimal.InvalidOperation: not a number
        return None


def _require_paper() -> None:
    """Fail-fast if ARC_ENV is not paper."""
    env_val = os.environ.get("ARC_ENV", "paper").lower()
    if env_val != "paper":
        msg = f"AlpacaPaperBroker is paper-only. ARC_ENV={env_val!r} is not allowed."
        raise RuntimeError(msg)


def _default_timeouts() -> tuple[float, float]:
    """``(connect_s, read_s)`` from :class:`arc.config.ArcSettings` (D71 defaults 5 s / 15 s)."""
    from arc.config import get_settings

    s = get_settings()
    return s.execution_broker_connect_timeout_s, s.execution_broker_read_timeout_s


def _make_client(
    api_key: str | None = None,
    secret_key: str | None = None,
    *,
    timeouts: tuple[float, float] | None = None,
) -> TradingClient:
    """Build a TradingClient pointed at the paper endpoint.

    Explicit keys (both or neither) win over ``ALPACA_API_KEY``/``ALPACA_SECRET_KEY``.
    Every request carries ``timeout=timeouts`` (``(connect_s, read_s)``; default from
    settings, D71): alpaca-py has no timeout of its own.
    """
    if (api_key is None) != (secret_key is None):
        msg = "pass both api_key and secret_key, or neither"
        raise ValueError(msg)
    if api_key is None:
        api_key = os.environ.get("ALPACA_API_KEY", "")
        secret_key = os.environ.get("ALPACA_SECRET_KEY", "")
    if not api_key or not secret_key:
        msg = (
            "ALPACA_API_KEY and ALPACA_SECRET_KEY must be set in the "
            "environment (see ~/.hermes/.env)."
        )
        raise RuntimeError(msg)
    client = TradingClient(
        api_key=api_key,
        secret_key=secret_key,
        paper=True,
        url_override=_PAPER_BASE_URL,
    )
    connect_s, read_s = timeouts or _default_timeouts()
    install_timeouts(client, connect_s=connect_s, read_s=read_s)
    return client


def _to_status(order: object) -> BrokerOrderStatus:
    """An alpaca-py ``Order`` (or its raw dict) as a :class:`BrokerOrderStatus`."""
    if isinstance(order, dict):
        return BrokerOrderStatus(
            broker_order_id=str(order.get("id", "")),
            client_order_id=order.get("client_order_id"),
            status=str(order.get("status", "unknown")),
        )
    o = cast("Any", order)
    return BrokerOrderStatus(
        broker_order_id=str(o.id),
        client_order_id=o.client_order_id,
        status=_enum_value(o.status) if o.status else "unknown",
        filled_qty=Decimal(str(o.filled_qty or "0")),
        filled_avg_price=(Decimal(str(o.filled_avg_price)) if o.filled_avg_price else None),
        side=_order_side(getattr(o, "side", None)),
        legs=(
            [
                {
                    "symbol": leg.symbol,
                    "side": _enum_value(leg.side) if leg.side else None,
                    "qty": str(leg.qty),
                    "filled_qty": str(leg.filled_qty or "0"),
                    "status": _enum_value(leg.status) if leg.status else None,
                    "filled_avg_price": (
                        str(leg.filled_avg_price) if leg.filled_avg_price else None
                    ),
                }
                for leg in o.legs
            ]
            if o.legs
            else None
        ),
        created_at=o.created_at,
        updated_at=o.updated_at,
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
    # D66: Alpaca rejects a limit with 3 or more decimal places. Never send one.
    if order.limit_price != order.limit_price.quantize(Decimal("0.01")):
        msg = f"limit {order.limit_price} has more than 2 decimal places"
        raise ValueError(msg)
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

    def __init__(
        self,
        client: TradingClient | None = None,
        *,
        api_key: str | None = None,
        secret_key: str | None = None,
        account_label: str = "paper",
        timeouts: tuple[float, float] | None = None,
    ) -> None:
        _require_paper()
        self._client = client or _make_client(api_key, secret_key, timeouts=timeouts)
        self._account_label = account_label

    # -- identity (E13.11: registered as alpaca/paper/rest) -------------------

    venue = "alpaca"
    env = "paper"
    supports_mleg = True

    def info(self) -> BrokerInfo:
        """Venue/env/transport and an account label (never the account number)."""
        from arc.broker.registry import BrokerInfo, BrokerSpec

        return BrokerInfo(
            spec=BrokerSpec(venue="alpaca", env="paper", transport="rest"),
            account_label=self._account_label,
            supports_mleg=True,
            supports_paper=True,
        )

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
                    side=_enum_value(pos.side) if pos.side else "long",
                    market_value=Decimal(pos.market_value) if pos.market_value else None,
                    avg_entry_price=Decimal(pos.avg_entry_price) if pos.avg_entry_price else None,
                    unrealized_pl=Decimal(pos.unrealized_pl) if pos.unrealized_pl else None,
                    asset_class=_enum_value(pos.asset_class) if pos.asset_class else "us_option",
                    current_price=_opt_dec(getattr(pos, "current_price", None)),
                    lastday_price=_opt_dec(getattr(pos, "lastday_price", None)),
                    change_today=_opt_dec(getattr(pos, "change_today", None)),
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
        return _to_status(self._client.get_order_by_id(broker_order_id))

    # -- by client_order_id (E11.1, D71: resolve an unknown submit) -----------

    def order_status_by_client_id(self, client_order_id: str) -> BrokerOrderStatus | None:
        """``GET /v2/orders:by_client_order_id``; ``None`` on 404 (no such order).

        Any other API error, and every transport error, propagates: the caller
        cannot tell "absent" from "unreadable" then.
        """
        from alpaca.common.exceptions import APIError

        try:
            order = self._client.get_order_by_client_id(client_order_id)
        except APIError as exc:
            if exc.status_code == 404:
                return None
            raise
        return _to_status(order)

    def cancel_by_client_id(self, client_order_id: str) -> None:
        """Cancel the order held under *client_order_id*; a no-op when there is none."""
        st = self.order_status_by_client_id(client_order_id)
        if st is None:
            log.info("cancel_by_client_id_absent", client_order_id=client_order_id)
            return
        self.cancel(st.broker_order_id)

    # -- option non-trade activities / DNE (E11.4, D73) ------------------------

    def activities(
        self,
        since: dt.date,
        *,
        types: tuple[str, ...] = ("OPASN", "OPEXC", "OPEXP", "OPTRD"),
    ) -> list[BrokerActivity]:
        """``GET /v2/account/activities?activity_types=...&after=<since - 1 day>``, paged.

        alpaca-py's ``TradingClient`` has no activities method, so this uses its raw
        REST ``get`` (base URL stays the paper endpoint). Rows of other types and
        rows without an OCC ``symbol`` are dropped. Read-only.
        """
        out: list[BrokerActivity] = []
        params: dict[str, Any] = {
            "activity_types": ",".join(types),
            "after": (since - dt.timedelta(days=1)).isoformat(),
            "direction": "asc",
            "page_size": 100,
        }
        seen: set[str] = set()
        while True:
            page = self._client.get("/account/activities", params)
            rows = page if isinstance(page, list) else []
            for raw in rows:
                act = _to_activity(raw, types)
                if act is not None and act.id not in seen and act.date >= since:
                    seen.add(act.id)
                    out.append(act)
            if len(rows) < params["page_size"]:
                return out
            last = str(rows[-1].get("id") or "")
            if not last or last == params.get("page_token"):
                return out
            params["page_token"] = last

    def do_not_exercise(self, occ: str) -> None:
        """``POST /v2/positions/{occ}/do-not-exercise`` (BETA; expiry day only).

        Raises the broker's ``APIError`` on a rejection (e.g. not the expiration
        day, after Alpaca's cutoff). Only ``arc.execution.instructions`` calls it.
        """
        self._client.post(f"/positions/{occ}/do-not-exercise")
        log.info("do_not_exercise_sent", occ=occ)

    # -- order list (D32 order budget cross-check) ----------------------------

    def option_orders_since(self, since: dt.datetime) -> list[BrokerOrderRef]:
        """Option orders (single-leg ``us_option`` or mleg) of any status since *since*.

        ``GET /v2/orders?status=all&after=<since>`` in pages of 500 (Alpaca's
        maximum); equity and crypto orders are dropped. Dashboard-placed orders
        are included, which is the point.
        """
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        out: list[BrokerOrderRef] = []
        after = since
        while True:
            req = GetOrdersRequest(
                status=QueryOrderStatus.ALL, after=after, limit=500, direction="asc"
            )
            page = [o for o in self._client.get_orders(req) if not isinstance(o, dict)]
            for o in page:
                mleg = bool(o.legs) or _enum_value(getattr(o, "order_class", "")) == "mleg"
                asset_class = _enum_value(o.asset_class) if o.asset_class is not None else ""
                if not mleg and asset_class != "us_option":
                    continue
                out.append(
                    BrokerOrderRef(
                        broker_order_id=str(o.id),
                        client_order_id=o.client_order_id,
                        status=_enum_value(o.status) if o.status is not None else "",
                        asset_class=asset_class,
                        mleg=mleg,
                        submitted_at=o.submitted_at or o.created_at,
                    )
                )
            if len(page) < 500:
                return out
            last = page[-1].submitted_at or page[-1].created_at
            if last is None or last <= after:
                return out
            after = last

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
            coid = order.client_order_id if isinstance(order.client_order_id, str) else None
            # For mleg orders, report fills per leg
            if order.legs:
                for leg in order.legs:
                    if leg.filled_at and leg.filled_avg_price and leg.filled_qty:
                        result.append(
                            Fill(
                                broker_order_id=str(order.id),
                                symbol=leg.symbol or "",
                                side=_enum_value(leg.side) if leg.side else "buy",
                                qty=Decimal(str(leg.filled_qty)),
                                price=Decimal(str(leg.filled_avg_price)),
                                filled_at=leg.filled_at,
                                client_order_id=coid,
                            )
                        )
            elif order.filled_avg_price and order.filled_qty:
                result.append(
                    Fill(
                        broker_order_id=str(order.id),
                        symbol=order.symbol or "",
                        side=_enum_value(order.side) if order.side else "buy",
                        qty=Decimal(str(order.filled_qty)),
                        price=Decimal(str(order.filled_avg_price)),
                        filled_at=order.filled_at,
                        client_order_id=coid,
                    )
                )
        return result
