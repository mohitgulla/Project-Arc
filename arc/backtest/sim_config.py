"""``backtest.sim`` settings for the live-shaped portfolio simulator (card E7.11, D84).

Kept apart from :mod:`arc.backtest.sim` so :class:`arc.backtest.ranking.BacktestSettings`
can carry it without an import cycle. Backtest-only knobs: none of them reaches the
live loop, the gate or a strategy default.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["SimSettings"]


class SimSettings(BaseModel):
    """``backtest.sim`` in ``config/ranking.yaml``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    selection: Literal["cross_name", "per_ticker"] = Field(
        "cross_name",
        description="cross_name = pool every active name's candidates (live-shaped); "
        "per_ticker = the E7.5 walk (top-1 per ticker, alphabetical), for reproducibility",
    )
    equity: float = Field(25_000.0, gt=0.0, description="D80 account equity (--equity)")
    apply_screen: bool = Field(True, description="Live D9 screen on non-seed names")
    active_cap: int = Field(50, ge=1, description="Active-list cap (live universe_active_max)")
    active_fill: Literal["round_robin", "precedence"] = Field(
        "round_robin", description="universe.active_fill (D67) for the slots after momentum"
    )
    momentum_size: int = Field(20, ge=0, description="Momentum tier size (live 20)")
    momentum_lookback: int = Field(252, ge=20, description="Sessions in the 12-month return")
    momentum_skip: int = Field(21, ge=0, description="Most recent sessions skipped (12-1)")
    regime_model: Literal["v1", "v2"] = Field("v2", description="Stance proxy labels (D77)")
    earnings_blackout: bool = Field(True, description="Pure gate blackout on projected dates")
    slippage_x: float = Field(
        0.25, ge=0.0, le=1.0, description="Fill at mid +/- x * quoted spread (all kinds)"
    )
    spread_rule: Literal["live", "off"] = Field(
        "live", description="live = no fill when a leg's spread > max(pct * mid, abs)"
    )
    capacity: Literal["clip", "skip", "off"] = Field(
        "clip", description="Over-capacity size: clip to the limit, skip the open, or ignore"
    )
    max_pct_oi: float = Field(0.05, gt=0.0, le=1.0, description="Contracts / each leg's OI")
    max_pct_volume: float = Field(
        0.25, gt=0.0, le=1.0, description="Contracts / each leg's session volume"
    )
    per_name_depth: int = Field(3, ge=1, description="Candidates per name in the pool")
    daily_loss_rule: bool = Field(True, description="check_daily_loss blocks the day's opens")
