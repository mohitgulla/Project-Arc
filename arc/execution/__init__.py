"""Order state machine, submit(), the D24 price-band ladder, fills and exits.

``submit()`` is the only path to ``BrokerAdapter.submit_mleg`` (PLAN §2.1);
``execute()`` works an approved proposal through its band by calling it.
"""

from arc.execution.guard import TradingHaltedError, require_trading_allowed
from arc.execution.submission import (
    RefusalCode,
    SubmitRefused,
    attempt_order_id,
    build_order,
    submit,
)

__all__ = [
    "RefusalCode",
    "SubmitRefused",
    "TradingHaltedError",
    "attempt_order_id",
    "build_order",
    "require_trading_allowed",
    "submit",
]
