"""Order state machine, submit(), fill tracking, exit management.

``submit()`` is the only path to ``BrokerAdapter.submit_mleg`` (PLAN §2.1).
"""

from arc.execution.guard import TradingHaltedError, require_trading_allowed
from arc.execution.submission import RefusalCode, SubmitRefused, build_order, submit

__all__ = [
    "RefusalCode",
    "SubmitRefused",
    "TradingHaltedError",
    "build_order",
    "require_trading_allowed",
    "submit",
]
