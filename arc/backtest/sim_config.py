"""``backtest.sim`` settings for the live-shaped portfolio simulator (card E7.11, D84).

Kept apart from :mod:`arc.backtest.sim` so :class:`arc.backtest.ranking.BacktestSettings`
can carry it without an import cycle. Backtest-only knobs: none of them reaches the
live loop, the gate or a strategy default.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from arc.config import ArcSettings

__all__ = ["SimGateOverrides", "SimSettings"]


class SimGateOverrides(BaseModel):
    """``backtest.sim.gate`` (E28.6, D131): the D131 gate rules' knobs for one study arm.

    Every field defaults to ``None`` = the walk uses the live ``ArcSettings`` value
    (the shipped default, plus any registry override the caller's settings carry), so
    the base config is the shipped behaviour and a study arm is one overlay line, e.g.
    ``ranking: {backtest: {sim: {gate: {max_open_risk_pct: 0.30}}}}``. Bounds match the
    ``ArcSettings`` fields they replace. ``regime_vix_pct_above`` / ``regime_vix_combine``
    are sim-only (no live rule reads them): the E28.6 regime variant.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    # E28.2: short_premium_regime
    regime_gate_enabled: bool | None = Field(None, description="None = ArcSettings")
    regime_gate_ma: Literal[50, 100, 200] | None = Field(None, description="SPY SMA length")
    regime_gate_buffer_pct: float | None = Field(None, ge=-0.05, le=0.05)
    regime_gate_blocked_kinds: Literal["bull_put,iron_condor", "bull_put", "iron_condor"] | None = (
        None
    )
    regime_vix_pct_above: float | None = Field(
        None,
        ge=0.0,
        lt=1.0,
        description="Sim-only VIX condition on the regime gate: VIX's 1-year percentile "
        "(the market_health vix_pct_1y rule: share of the prior <= 252 closes strictly "
        "below the session's close, >= 120 prior obs) is strictly above this. Needs "
        "<data-dir>/market_index/VIX.parquet (scripts/fetch_vix_history.py). None = no "
        "VIX condition (the live rule)",
    )
    regime_vix_combine: Literal["and", "or"] = Field(
        "and",
        description="and = a blocked kind is refused only when SPY < SMA *and* VIX is high; "
        "or = when either holds. A session without a VIX percentile counts as VIX high "
        "(fail closed, like the live rule's missing input)",
    )
    # E28.3: open_risk_cap
    max_open_risk_pct: float | None = Field(None, ge=0.10, le=1.0, description="1.0 = off")
    # E28.5: cluster_cap
    cluster_cap_enabled: bool | None = None
    cluster_cap_corr_threshold: float | None = Field(None, ge=0.5, le=0.95)
    cluster_cap_max_names: int | None = Field(None, ge=1, le=10)

    def settings_update(self) -> dict[str, object]:
        """The set ``ArcSettings`` fields (sim-only VIX knobs excluded)."""
        skip = {"regime_vix_pct_above", "regime_vix_combine"}
        return {k: v for k, v in self.model_dump(exclude=skip).items() if v is not None}

    def apply(self, settings: ArcSettings) -> ArcSettings:
        """*settings* with the set fields replaced (the same object when none is set)."""
        upd = self.settings_update()
        return settings.model_copy(update=upd) if upd else settings


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
    # E7.19 (D125, D128): the live-shaped walk mirrors the live entry filters.
    entry_filters: Literal["none", "live"] = Field(
        "live",
        description="live = the D78 anti-chase rule (backtest.anti_chase thresholds = "
        "strategy.yaml anti_chase:, daily bars <= the decision day) runs on every "
        "cross_name menu as the live pipeline runs it since D128: on a stretched stance "
        "the debit structures leave the menu and the profile's credit structures stay "
        "(a debit-only profile loses the idea). A run with backtest.entry_filter set "
        "(E16.3) keeps that filter instead. The live range-edge rule (D98) is not "
        "replayed: it checks bullish/bearish ideas in a sideways regime, and the trend "
        "stance proxy never gives one. none = no entry filter (pre-E7.19 walk)",
    )
    margin_buying_power: bool = Field(
        True,
        description="D129 on margin profiles: an open needs max loss + open fees <= "
        "options buying power, modelled as equity minus the max loss of the held book "
        "(Alpaca reduces options buying power by a defined-risk spread's max loss: "
        "credit (width - credit) x 100, debit the debit); sizing fits that headroom as "
        "the live quant.open does. Skip reason buying_power. False = no BP rule",
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
    # E28.6 (D131): the D131 rule knobs for study arms + a sim-only VRP cap.
    gate: SimGateOverrides = Field(
        default_factory=lambda: SimGateOverrides(),
        description="D131 gate-rule overrides for the walk (None fields = ArcSettings)",
    )
    vrp_max: float | None = Field(
        None,
        ge=0.0,
        le=1.0,
        description="Sim-only (owner D131 #12: backtest arm first, no live default): skip a "
        "short-premium (credit) entry when the name's IV30 - HV20 at the decision close is "
        "above this, in vol points as a fraction (0.05 = 5 pts; IV30 = the E7.10 "
        "constant-maturity ATM IV, HV20 = 20-session close-to-close vol of the "
        "split-adjusted closes). A name-day with either input unknown is not skipped. "
        "Skip reason vrp_cap. None = off",
    )
