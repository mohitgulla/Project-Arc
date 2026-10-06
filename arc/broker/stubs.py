"""Fail-closed broker stubs for venues Arc does not trade yet (E13.11, D1/D56).

``alpaca/live``, ``robinhood/*`` and ``*/mcp`` are registered in
:mod:`arc.broker.registry` so the venue x env x transport space is explicit,
but none of them can do anything: every method raises
:class:`~arc.broker.registry.BrokerNotAvailable`.

Constructing a stub reads **nothing**: no ``os.environ``, no file, no network.
It only logs ``broker.stub_constructed`` at WARNING. The Robinhood adapter
itself is a separate future initiative (D56 owner decision 6); D1 records its
constraints (no paper mode, single-leg only, localhost-only OAuth).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NoReturn

import structlog

from arc.broker.registry import BrokerInfo, BrokerNotAvailable, BrokerSpec

if TYPE_CHECKING:
    import datetime as dt

    from arc.broker.base import (
        AccountInfo,
        BrokerOrderRef,
        BrokerOrderStatus,
        BrokerPosition,
        Fill,
        MlegOrder,
    )

__all__ = ["AlpacaLiveStub", "McpStub", "RobinhoodStub", "StubBroker"]

log = structlog.get_logger()


class StubBroker:
    """A registered venue that is not enabled: every call raises ``BrokerNotAvailable``."""

    supports_mleg: bool = True
    supports_paper: bool = True

    def __init__(self, spec: BrokerSpec) -> None:
        self._spec = spec
        log.warning("broker.stub_constructed", broker=spec.label, stub=type(self).__name__)

    @property
    def spec(self) -> BrokerSpec:
        return self._spec

    @property
    def venue(self) -> str:
        return self._spec.venue

    @property
    def env(self) -> str:
        return self._spec.env

    def _refuse(self) -> NoReturn:
        raise BrokerNotAvailable(self._spec)

    def info(self) -> BrokerInfo:
        self._refuse()

    def account(self) -> AccountInfo:
        self._refuse()

    def positions(self) -> list[BrokerPosition]:
        self._refuse()

    def submit_mleg(self, order: MlegOrder) -> str:  # noqa: ARG002 - refused unseen
        self._refuse()

    def cancel(self, broker_order_id: str) -> None:  # noqa: ARG002
        self._refuse()

    def order_status(self, broker_order_id: str) -> BrokerOrderStatus:  # noqa: ARG002
        self._refuse()

    def fills(self, since: dt.datetime) -> list[Fill]:  # noqa: ARG002
        self._refuse()

    def option_orders_since(self, since: dt.datetime) -> list[BrokerOrderRef]:  # noqa: ARG002
        self._refuse()


class AlpacaLiveStub(StubBroker):
    """``alpaca/live/rest``: live trading is not enabled (Phase 1 is paper only)."""


class RobinhoodStub(StubBroker):
    """``robinhood/live/*``: interface only (D56); no paper mode, single-leg only (D1)."""

    supports_mleg = False
    supports_paper = False


class McpStub(StubBroker):
    """``*/mcp``: an agent MCP server as the order transport is not enabled.

    Alpaca MCP stays read-only inside persona sessions (PLAN §2.6); orders go
    through ``arc.execution.submit()`` over REST.
    """
