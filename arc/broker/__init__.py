"""BrokerAdapter protocol and venue-specific implementations.

Usage::

    from arc.broker.base import BrokerAdapter, MlegOrder
    from arc.broker.registry import resolve_broker

    broker = resolve_broker(settings)  # alpaca/paper/rest; anything else refuses
"""

from arc.broker.base import (
    AccountInfo,
    BrokerAdapter,
    BrokerOrderRef,
    BrokerOrderStatus,
    BrokerPosition,
    Fill,
    MlegLeg,
    MlegOrder,
)

__all__ = [
    "AccountInfo",
    "BrokerAdapter",
    "BrokerOrderRef",
    "BrokerOrderStatus",
    "BrokerPosition",
    "Fill",
    "MlegLeg",
    "MlegOrder",
]
