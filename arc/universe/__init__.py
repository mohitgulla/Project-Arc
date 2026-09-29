"""Open universe (PLAN D9 revised, D28; card E5.7).

``settings.universe`` is a **seed/watch list** (``ARC_UNIVERSE_MODE=seed``, the
default): seed tickers are always scanned and always accepted. Any other
US-listed ticker the sources surface may become a Scout candidate if it is in the
local :mod:`symbol master <arc.universe.master>` and passes the deterministic
:mod:`liquidity screen <arc.universe.screen>`. ``ARC_UNIVERSE_MODE=strict``
restores the allow-list. Knobs live in ``config/universe.yaml``.

The risk gate never imports this package: every PLAN §5 cap applies per
underlying, whatever the universe.
"""

from __future__ import annotations

from arc.universe.config import UniverseConfig, UniverseMode, load_universe_config
from arc.universe.extract import extract_tickers
from arc.universe.guard import UniverseGuard
from arc.universe.master import SymbolInfo, SymbolMaster, load_symbol_master
from arc.universe.screen import (
    LiquidityMetrics,
    LiquidityThresholds,
    ScreenResult,
    measure_liquidity,
    screen_liquidity,
)

__all__ = [
    "LiquidityMetrics",
    "LiquidityThresholds",
    "ScreenResult",
    "SymbolInfo",
    "SymbolMaster",
    "UniverseConfig",
    "UniverseGuard",
    "UniverseMode",
    "extract_tickers",
    "load_symbol_master",
    "load_universe_config",
    "measure_liquidity",
    "screen_liquidity",
]
