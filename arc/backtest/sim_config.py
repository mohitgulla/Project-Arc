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
    # E7.17 (loss attribution): sim correctness fixes, on by default.
    exit_marks: Literal["all_quotes", "entry_chain"] = Field(
        "all_quotes",
        description="all_quotes = an open position is marked (and exits) on every two-sided "
        "closing quote of its legs, as live marks it; entry_chain = only on contracts the "
        "entry chain keeps (the E7.11 behaviour: a leg wider than max_quoted_spread_pct "
        "made the position unmarkable, so stops waited and losers rode to expiry)",
    )
    splits: Literal["adjust", "ignore"] = Field(
        "adjust",
        description="adjust = a whole-number forward split inside the hold follows the "
        "re-struck contract (strike / r, value x r) and settles on the adjusted close; a "
        "non-standard adjustment (reverse split, 3:2, spin-off) leaves the candidate "
        "unsettled. Trend labels and momentum read split-adjusted closes. ignore = E7.11",
    )
    # E7.17 counterfactual knobs (attribution runs only; defaults = no change).
    fill_spread: Literal["quoted", "estimated"] = Field(
        "quoted",
        description="estimated = fills (entry and exit) use the cost model's estimated "
        "spread max(spread_min, spread_pct * mid) instead of the quoted one; selection, "
        "the spread rule and marks stay quoted",
    )
    size_scale: float = Field(
        1.0, gt=0.0, le=1.0, description="Contracts x scale (floored; < 1 contract = skip)"
    )
    max_new_risk_pct_day: float | None = Field(
        None, gt=0.0, le=1.0, description="Cap on the day's new max loss / equity (None = off)"
    )
    exclude_kinds: tuple[str, ...] = Field(
        (), description="Strategy kinds never opened (structure counterfactuals)"
    )
    # E7.19 (D125): sim alignment with the live entry-DTE sleeves.
    intraday_proxy: Literal["none", "range"] = Field(
        "range",
        description="Intraday-flagged exit rules (stop_eod_only: false, profit_lock.eod_only: "
        "false) on EOD data: range = also check them at the session's underlying high and "
        "low, every leg re-priced at its EOD IV (sticky strike); a rule that fires there "
        "fills at that proxy price minus the spread cost (stop_intraday_proxy / "
        "lock_intraday_proxy). Stop-first: the proxy is checked before the EOD rules, so a "
        "day touching both a stop and a take profit books the stop. Over-states stop hits "
        "on whipsaw days (high/low order unknown). none = EOD marks only",
    )
