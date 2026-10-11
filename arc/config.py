"""Configuration via pydantic-settings: ARC_ENV, limits, universe.

See PLAN.md §5 for risk defaults and §9.2 (D9) for the default universe.
"""

from __future__ import annotations

import enum
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Literal

import structlog
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    SecretStr,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from arc.account_profiles import DEFAULT_ACCOUNT_PROFILE, AccountProfile, load_account_profiles

log = structlog.get_logger()

# ---------------------------------------------------------------------------
# ARC_ENV enum
# ---------------------------------------------------------------------------

_LIVE_ENV_PATH = Path.home() / ".arc" / "live.env"


class UniverseMode(enum.StrEnum):
    """D28: ``seed`` = ``universe`` is a watch list (open universe); ``strict`` = allow-list."""

    SEED = "seed"
    STRICT = "strict"


class ArcEnv(enum.StrEnum):
    """Execution environment: paper (default) or live."""

    PAPER = "paper"
    LIVE = "live"


# ---------------------------------------------------------------------------
# Market data feeds (D7: Alpaca Basic, free tier)
# ---------------------------------------------------------------------------


class AlpacaDataFeed(enum.StrEnum):
    """Alpaca stock data feed. Free/paper tier cannot query recent SIP data."""

    IEX = "iex"
    SIP = "sip"
    DELAYED_SIP = "delayed_sip"


class AlpacaOptionsFeed(enum.StrEnum):
    """Alpaca options data feed. Free tier is ``indicative``; OPRA is paid."""

    INDICATIVE = "indicative"
    OPRA = "opra"


# ---------------------------------------------------------------------------
# Default YouTube sources (D13)
# ---------------------------------------------------------------------------

# D13/D45 (E4.6), D60: the channels behind the daily 02:00 ET ``youtube.briefs`` job
# (config/routines.yaml is the source of truth; this list is the CLI default).
# Channel ids, never @handles, so a rename can't break them.
DEFAULT_YOUTUBE_CHANNELS: list[str] = [
    "https://www.youtube.com/channel/UC-m6zNItyoDk5lSykDlhE4Q/videos",  # StockedUp
    "https://www.youtube.com/channel/UCvJZEG5x-DVYZKTz--pS39w/videos",  # FX Evolution
    "https://www.youtube.com/channel/UCYKtr6GfycBqQJf32tbQSbQ/videos",  # Trade Brigade
    "https://www.youtube.com/channel/UCTeFsS-bP0XEt3NBMjfW2cA/videos",  # Arete Trading
    "https://www.youtube.com/channel/UCOHxDwCcOzBaLkeTazanwcw/videos",  # Bravos
    "https://www.youtube.com/channel/UCBayuhgYpKNbhJxfExYkPfA/videos",  # Warrior Trading (D60)
    "https://www.youtube.com/channel/UC5fZv7bPcF5j2RsfO-9OiLA/videos",  # IBD (D60)
    "https://www.youtube.com/channel/UC5fZv7bPcF5j2RsfO-9OiLA/streams",  # IBD daily show (D60)
]


# ---------------------------------------------------------------------------
# Default core universe (D51; was the D9 flat seed list)
# ---------------------------------------------------------------------------

# D56 core tier: 20 stocks, no ETFs (D51's 25 minus SMCI, MARA, SOFI, UBER, BAC). Must
# equal config/universe.yaml `core:` (tests/test_universe_tiers.py pins it). SPY/QQQ/IWM
# are the market reference (config/universe.yaml `tiers.market_reference`), not trade names.
DEFAULT_UNIVERSE: list[str] = [
    "NVDA",
    "AAPL",
    "MSFT",
    "AMZN",
    "GOOGL",
    "META",
    "TSLA",
    "AMD",
    "AVGO",
    "MU",
    "JPM",
    "XOM",
    "UNH",
    "BA",
    "ORCL",
    "COIN",
    "PLTR",
    "HOOD",
    "INTC",
    "NFLX",
]


# ---------------------------------------------------------------------------
# Exchange price increments (D66, E6.2h)
# ---------------------------------------------------------------------------

_CENT = Decimal("0.01")


class TickRules(BaseModel):
    """Exchange-valid option price increments (D66). Read by :mod:`arc.gate.ticks`.

    Single-leg orders (one ratio-1 leg, sent to Alpaca as a simple order) trade on
    their class grid by the order's own price level: Penny Program (``ppind``)
    classes on ``penny_below`` under ``boundary`` and ``penny_above`` at or above
    it; every other class, including an unknown ``ppind``, on the wider standard
    grid; ``penny_all_underlyings`` on ``penny_all`` at any price. Multi-leg
    (``mleg``) net prices trade on ``mleg`` (our reading of complex-order rules;
    Alpaca's article is silent, so it is configurable).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    mleg: Decimal = Field(default=Decimal("0.01"), description="Multi-leg net price increment ($).")
    penny_all_underlyings: tuple[str, ...] = Field(
        default=("SPY", "QQQ", "IWM"), description="Underlyings quoted in pennies at any price."
    )
    penny_all: Decimal = Field(
        default=Decimal("0.01"), description="Increment for those underlyings."
    )
    penny_below: Decimal = Field(
        default=Decimal("0.01"), description="Penny Program, below boundary."
    )
    penny_above: Decimal = Field(
        default=Decimal("0.05"), description="Penny Program, at/above boundary."
    )
    standard_below: Decimal = Field(
        default=Decimal("0.05"), description="Standard class, below boundary."
    )
    standard_above: Decimal = Field(
        default=Decimal("0.10"), description="Standard class, at/above it."
    )
    boundary: Decimal = Field(
        default=Decimal("3.00"), description="Price level where the grid widens ($)."
    )

    @field_validator("penny_all_underlyings")
    @classmethod
    def _upper(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(s.strip().upper() for s in v)

    @model_validator(mode="after")
    def _whole_cents(self) -> TickRules:
        ticks = {
            "mleg": self.mleg,
            "penny_all": self.penny_all,
            "penny_below": self.penny_below,
            "penny_above": self.penny_above,
            "standard_below": self.standard_below,
            "standard_above": self.standard_above,
        }
        for name, t in ticks.items():
            if t <= 0 or t % _CENT:
                msg = f"ticks.{name} must be a positive whole-cent increment, got {t}"
                raise ValueError(msg)
        if self.boundary <= 0:
            msg = f"ticks.boundary must be positive, got {self.boundary}"
            raise ValueError(msg)
        for name in ("penny_below", "penny_above", "standard_below", "standard_above"):
            if self.boundary % ticks[name]:
                msg = f"ticks.boundary {self.boundary} is not a multiple of ticks.{name}"
                raise ValueError(msg)
        return self


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


class ArcSettings(BaseSettings):
    """Central configuration for the Arc trading system.

    Values are loaded in pydantic-settings priority order:
      1. Constructor kwargs
      2. Environment variables (prefixed ``ARC_``)
      3. A ``.env`` file (``~/.hermes/.env``)
      4. Field defaults (from PLAN.md §5)
    """

    model_config = SettingsConfigDict(
        env_prefix="ARC_",
        env_file=str(Path.home() / ".hermes" / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # -- Environment ---------------------------------------------------------
    env: ArcEnv = Field(
        default=ArcEnv.PAPER,
        description="Execution environment: paper (default) or live.",
    )
    # E13.11 (D56): the broker registry key is (broker_venue, env, broker_transport).
    # Only alpaca/paper/rest constructs; every other combination is refused by
    # arc.broker.registry with BrokerNotAvailable before any credential read.
    broker_venue: Literal["alpaca", "robinhood"] = Field(
        default="alpaca",
        description="Broker venue (ARC_BROKER_VENUE). Only alpaca is enabled (D1/D56).",
    )
    broker_transport: Literal["rest", "mcp"] = Field(
        default="rest",
        description="Order transport (ARC_BROKER_TRANSPORT). Only rest is enabled (D56).",
    )

    # -- Risk limits (PLAN.md §5) -------------------------------------------
    max_alloc_pct: Annotated[float, Field(gt=0.0, le=1.0)] = Field(
        default=0.10,
        description="Max allocation per underlying as fraction of equity (10%, D85; was 5%).",
    )
    daily_loss_halt_pct: Annotated[float, Field(gt=0.0, le=1.0)] = Field(
        default=0.03,
        description="Daily portfolio loss halt threshold (3%).",
    )
    spread_max_pct: Annotated[float, Field(ge=0.0, le=1.0)] = Field(
        default=0.10,
        description="Max bid-ask spread as fraction of mid (10%).",
    )
    spread_max_abs: Annotated[float, Field(ge=0.0)] = Field(
        default=0.10,
        description="Max bid-ask spread in dollars ($0.10).",
    )
    wash_sale_days: Annotated[int, Field(ge=0)] = Field(
        default=30,
        description="Wash-sale lookback window in calendar days.",
    )
    portfolio_dollar_delta_cap_pct: Annotated[float, Field(ge=0.0, le=2.0)] = Field(
        default=1.00,
        description=(
            "D57/D62: |net dollar delta| cap as a share of equity: post-trade "
            "|Σ (net Δ share-eq × spot)| ≤ 1.00 × equity. Not beta-weighted."
        ),
    )
    portfolio_beta_delta_cap_pct: Annotated[float, Field(ge=0.0, le=4.0)] = Field(
        default=2.00,
        description=(
            "D62: |beta-weighted net dollar delta| cap as a share of equity: post-trade "
            "|Σ (net Δ share-eq × spot × max(β, 1))| ≤ 2.00 × equity; β = 1y daily vs SPY "
            "(betas routine), 1.0 when missing or stale."
        ),
    )
    portfolio_vega_cap_pct: Annotated[float, Field(ge=0.0, le=1.0)] = Field(
        default=0.010,
        description="|ν| cap: 1.0% of equity per vol-point (D57).",
    )
    # -- Account profile (D25, E3.4) -------------------------------------------
    account_profile: str = Field(
        default=DEFAULT_ACCOUNT_PROFILE,
        min_length=1,
        description=(
            "ARC_ACCOUNT_PROFILE: a profile in config/account_profiles.yaml "
            "(margin | cash_debit | cash_long_only). Paper default cash_debit (D25)."
        ),
    )
    account_profiles_file: Path | None = Field(
        default=None,
        description="ARC_ACCOUNT_PROFILES_FILE; None -> config/account_profiles.yaml.",
    )
    account_profile_spec: AccountProfile | None = Field(
        default=None,
        description=(
            "The resolved profile (filled from the file at load time). The gate reads it "
            "and fails closed when it is missing or names a different profile."
        ),
    )
    dte_min: Annotated[int, Field(ge=0)] = Field(
        default=30,
        description="Minimum DTE for new entries.",
    )
    dte_max: Annotated[int, Field(ge=0)] = Field(
        default=45,
        description="Maximum DTE for new entries.",
    )
    earnings_blackout: bool = Field(
        default=True,
        description="Block short premium through earnings unless overridden.",
    )
    max_open_positions: Annotated[int, Field(ge=1)] = Field(
        default=8,
        description="Max simultaneous open positions.",
    )
    approval_ttl_seconds: Annotated[int, Field(ge=1)] = Field(
        default=1200,
        description="Approval TTL in seconds (20 min).",
    )
    # Gate data-quality defaults (E3.1). Not specified in PLAN §5 — proposed
    # defaults, owner to confirm in the E3.1 PR.
    quote_max_age_seconds: Annotated[int, Field(ge=1)] = Field(
        default=60,
        description="Max age of a leg quote at gate time (data freshness).",
    )
    account_max_age_seconds: Annotated[int, Field(ge=1)] = Field(
        default=300,
        description="Max age of the account snapshot at gate time (data freshness).",
    )
    gate_fee_per_leg_contract: Annotated[float, Field(ge=0.0, le=1.0)] = Field(
        default=0.05,
        description=(
            "Fee allowance per leg-contract in the gate's settled-cash check (D25). "
            "Conservative round-up of config/costs.yaml buy-side fees (ORF + OCC + CAT "
            "~= $0.04); the gate cannot read the cost model file (pure, no I/O)."
        ),
    )
    ticks: TickRules = Field(
        default=TickRules(),
        description=(
            "D66 exchange price increments: every limit is on the grid for its order "
            "(multi-leg net, Penny Program / standard single-leg by price level)."
        ),
    )
    # -- Execution: bounded price improvement (D24, E6.2) --------------------
    execution_improvement_steps: Annotated[int, Field(ge=0, le=9)] = Field(
        default=3,
        description="Price-improvement steps after the mid attempt (D24: 3 = 4 attempts).",
    )
    execution_band_reach: Annotated[float, Field(ge=0.0, le=1.0)] = Field(
        default=1.0,
        description=(
            "How far toward the far touch of the combo NBBO the band's worst price may go "
            "(1.0 = far touch). Each step moves 1/N of the band (D24)."
        ),
    )
    execution_step_seconds: Annotated[int, Field(ge=1, le=900)] = Field(
        default=60,
        description="Seconds each attempt may work before it is cancelled (D24: 60 s).",
    )
    execution_poll_seconds: Annotated[float, Field(gt=0.0, le=30.0)] = Field(
        default=2.0,
        description="Order-status poll interval while an attempt works.",
    )
    execution_cancel_confirm_seconds: Annotated[int, Field(ge=1, le=300)] = Field(
        default=30,
        description=(
            "Max seconds to wait for the broker to confirm a cancel before the ladder stops "
            "(D28: never two working orders per structure)."
        ),
    )
    execution_max_quote_age_seconds: Annotated[int, Field(ge=0, le=600)] = Field(
        default=60,
        description=(
            "D34: if more than this passed between the proposal's pricing and the ladder's "
            "first attempt, the Broker re-prices at the current mid. A mid outside the "
            "gate-approved band is not sent (journal reason stale_band)."
        ),
    )
    # -- Unknown submit state (E11.1, D71) ---------------------------------------
    execution_broker_connect_timeout_s: Annotated[float, Field(gt=0.0, le=30.0)] = Field(
        default=5.0,
        description=(
            "D71: TCP connect timeout (s) on every broker REST call. alpaca-py sets none, "
            "so without it a stalled connection blocks forever."
        ),
    )
    execution_broker_read_timeout_s: Annotated[float, Field(gt=0.0, le=60.0)] = Field(
        default=15.0,
        description=(
            "D71: read timeout (s) on every broker REST call. A submit with no answer in "
            "this time is an unknown submit, resolved by client_order_id."
        ),
    )
    execution_unknown_submit_lookups: Annotated[int, Field(ge=1, le=10)] = Field(
        default=3,
        description=(
            "D71: client_order_id lookups after a submit error before the order is "
            "called absent (or unknown when the lookups fail); execution_poll_seconds apart."
        ),
    )
    execution_intraday_reconcile: bool = Field(
        default=True,
        description=(
            "D71: queue a reconcile.intraday event after any unconfirmed execution "
            "(orders/executions only; never halts by itself)."
        ),
    )
    # -- Ladder liveness and re-attach (E11.2, D72) -------------------------------
    execution_reattach: bool = Field(
        default=True,
        description=(
            "D72: the broker.reattach job adopts a working execution whose ladder process "
            "died (fills recorded, remainder cancelled). Off = orphans are only listed."
        ),
    )
    execution_reattach_stale_s: Annotated[int, Field(ge=30, le=900)] = Field(
        default=90,
        description=(
            "D72: a running ladder whose heartbeat is older than this is not alive "
            "(it beats on every poll, execution_poll_seconds apart)."
        ),
    )
    execution_reattach_kill_after_s: Annotated[int, Field(ge=60, le=3600)] = Field(
        default=600,
        description=(
            "D72: a ladder that still holds its run lock but has not beaten for this long "
            "is wedged: SIGTERM its recorded pid (never SIGKILL); adopted on the next tick."
        ),
    )
    # -- Close quote check (E6.2a; PLAN §6.8: data quality fails closed) --------
    close_quote_max_age_seconds: Annotated[int, Field(ge=1, le=600)] = Field(
        default=60,
        description="Max age of each close leg's quote, measured from the quote's own timestamp.",
    )
    close_quote_max_skew_seconds: Annotated[int, Field(ge=0, le=600)] = Field(
        default=30,
        description="Max gap between the close legs' quote timestamps.",
    )
    close_quote_max_spread_pct: Annotated[float, Field(gt=0.0, le=1.0)] = Field(
        default=0.10,
        description="Max close-leg bid-ask spread as a fraction of mid (OR the $ cap below).",
    )
    close_quote_max_spread_abs: Annotated[float, Field(ge=0.0, le=1.0)] = Field(
        default=0.10,
        description="Max close-leg bid-ask spread in $ (a cheap wing passes on this).",
    )
    close_quote_max_curve_dev: Annotated[float, Field(gt=0.0, le=1.0)] = Field(
        default=0.15,
        description=(
            "Max $/share gap between the close's combo mid and the same combo read off the "
            "expiry's strike curve (indicative-feed jitter guard)."
        ),
    )
    close_quote_alert_after: Annotated[int, Field(ge=1, le=100)] = Field(
        default=6,
        description=(
            "Alert #arc-investor after this many consecutive unusable-quote close tries "
            "(6 = 30 min at the 5-min monitor)."
        ),
    )
    auto_exit_defined_risk: bool = Field(
        default=True,
        description=(
            "D24: when true, fired exits on defined-risk positions skip the Slack approval. "
            "Per environment (D34): the paper default is on (D85, was off); a live process "
            "validates it to off and only the config store (`auto_exit_defined_risk.live`, "
            "confirm code) turns it on there. ARC_AUTO_EXIT_DEFINED_RISK sets paper only."
        ),
    )
    # -- Daily options order budget (E6.5, D32) ---------------------------------
    order_budget_daily_max: Annotated[int, Field(ge=1, le=200)] = Field(
        default=200,
        description=(
            "Hard cap on broker option orders per ET day (every ladder attempt, opens and "
            "closes, dashboard orders). Code ceiling 200 (arc.budget.HARD_CEILING)."
        ),
    )
    order_budget_restrict_at: Annotated[int, Field(ge=0, le=200)] = Field(
        default=100,
        description="Orders used at which selection turns restrictive (D32 tier).",
    )
    order_budget_close_reserve: Annotated[int, Field(ge=0, le=199)] = Field(
        default=25,
        description=(
            "Orders kept for closes: opens stop at daily_max - reserve (175), closes may "
            "use the rest up to daily_max."
        ),
    )
    order_budget_restrictive_research_max_shortlist: Annotated[int, Field(ge=0, le=10)] = Field(
        default=1, description="Restrictive tier: Research shortlist cap."
    )
    order_budget_restrictive_max_new_opens_per_loop: Annotated[int, Field(ge=0, le=10)] = Field(
        default=1, description="Restrictive tier: new open proposals per pipeline run."
    )
    order_budget_restrictive_min_net_ev_multiplier: Annotated[float, Field(ge=1.0, le=10.0)] = (
        Field(
            default=1.5,
            description=(
                "Restrictive tier: managed Net EV must clear this x max(base floor, round-trip "
                "cost)."
            ),
        )
    )
    order_budget_restrictive_min_pop_delta_pp: Annotated[float, Field(ge=0.0, le=50.0)] = Field(
        default=5.0,
        description=(
            "Restrictive tier: managed PoP must clear the breakeven PoP by this many points."
        ),
    )
    order_budget_restrictive_max_improvement_steps: Annotated[int, Field(ge=0, le=9)] = Field(
        default=2, description="Restrictive tier: ladder improvement-step cap."
    )
    order_budget_restrictive_dedupe_cooldown_multiplier: Annotated[
        float, Field(ge=1.0, le=10.0)
    ] = Field(default=2.0, description="Restrictive tier: E5.9 dedupe cooldown multiplier.")
    # -- Portfolio-aware Research, dedupe, no-trade guard (E5.9, D33) ----------
    dedupe_executed_cooldown_sessions: Annotated[int, Field(ge=0, le=60)] = Field(
        default=5,
        description=(
            "D33: an idea whose fingerprint was executed (open, or closed within this many "
            "trading sessions) is suppressed. Doubled in the restrictive order-budget tier."
        ),
    )
    dedupe_proposed_cooldown_sessions: Annotated[int, Field(ge=0, le=60)] = Field(
        default=1,
        description=(
            "D33: an idea proposed (not rejected / TTL-expired) within this many sessions "
            "is suppressed. Doubled in the restrictive tier."
        ),
    )
    dedupe_rejected_cooldown_sessions: Annotated[int, Field(ge=0, le=60)] = Field(
        default=1,
        description=(
            "D33: an idea the owner rejected within this many sessions is suppressed. "
            "Doubled in the restrictive tier."
        ),
    )
    dedupe_reprice_move_pct: Annotated[float, Field(ge=0.0, le=1.0)] = Field(
        default=0.03,
        description=(
            "D33 material-change override: a suppressed idea is re-admitted when spot moved "
            "at least this fraction since the last one (or the regime changed)."
        ),
    )
    portfolio_sector_max_pct: Annotated[float, Field(gt=0.0, le=1.0)] = Field(
        default=0.40,
        description=(
            "D33: share of open max loss in one sector above which the book is flagged "
            "over_concentrated_sector and adds_concentration picks in it are dropped."
        ),
    )
    portfolio_stance_max_pct: Annotated[float, Field(gt=0.0, le=1.0)] = Field(
        default=0.75,
        description=(
            "D33: share of open max loss in one stance (bull/bear) above which the book is "
            "flagged stance_skew; adds_concentration picks in that stance are dropped."
        ),
    )
    portfolio_expiry_max_pct: Annotated[float, Field(gt=0.0, le=1.0)] = Field(
        default=0.60,
        description=(
            "D33: share of open max loss in one expiry bucket above which the book is "
            "flagged expiry_cluster."
        ),
    )
    portfolio_greek_near_cap_pct: Annotated[float, Field(gt=0.0, le=1.0)] = Field(
        default=0.80,
        description=(
            "D33: net |dollar delta| / |vega| usage of the PLAN §5 cap above which the "
            "book is flagged delta_near_cap / beta_delta_near_cap / vega_near_cap (D57/D62)."
        ),
    )
    portfolio_context_max_positions: Annotated[int, Field(ge=1, le=50)] = Field(
        default=12,
        description="D33: positions rendered in full in Research prompt (largest first).",
    )
    no_trade_vix_max: Annotated[float, Field(gt=0.0, le=200.0)] = Field(
        default=35.0,
        description=(
            "D33 market-conditions guard: at or above this VIX the loop proposes no new "
            "opens (reason market_unclear). Exits are unaffected."
        ),
    )
    no_trade_on_backwardation: bool = Field(
        default=True,
        description=(
            "D33: a VIX term structure in backwardation (vol_term context) blocks new opens."
        ),
    )
    no_trade_transitional_min_confidence: Annotated[float, Field(ge=0.0, le=1.0)] = Field(
        default=0.55,
        description=(
            "D33 legacy path: when the SPY regime entry has no v2 confirmation fields "
            "(run_length / margin_z), stickiness (P[stay]) below this counts as transitional "
            "and new opens are blocked. v2 entries use regime_guard_min_run / _margin_z (D77)."
        ),
    )
    regime_guard_min_run: Annotated[int, Field(ge=1, le=10)] = Field(
        default=1,
        description=(
            "D77 (E17.2): SPY trend label held for fewer sessions than this = transitional; "
            "the D33 market guard blocks new opens. Default 1 = off (run_length >= 1); "
            "the reference setting for replay / E17.3 is 3."
        ),
    )
    regime_guard_min_margin_z: Annotated[float, Field(ge=0.0, le=1.0)] = Field(
        default=0.0,
        description=(
            "D77 (E17.2): SPY trend z closer than this to a bull/bear threshold = "
            "transitional; the D33 market guard blocks new opens. Default 0.0 = off "
            "(margin_z >= 0); the reference setting for replay / E17.3 is 0.10."
        ),
    )
    # -- Regime model (E17.1, D77) ------------------------------------------------
    # Context and a Research/backtest input only; never a gate input.
    regime_model: Literal["v1", "v2"] = Field(
        default="v2",
        description=(
            "D77: per-ticker regime model. v2 = vol-scaled 20-day z trend + per-ticker vol "
            "state + rolling fit (default); v1 = the old +/-5% trailing return (rollback)."
        ),
    )
    regime_trend_z: Annotated[float, Field(gt=0.0, le=3.0)] = Field(
        default=1.0,
        description=(
            "D77 v2: |z| at or above this labels bull / bear (z = r20 / (sigma60 * sqrt 20))."
        ),
    )
    regime_vol_scale_window: Annotated[int, Field(ge=20, le=250)] = Field(
        default=60,
        description="D77 v2: sessions of daily log returns behind sigma in the trend z.",
    )
    regime_vol_rank_window: Annotated[int, Field(ge=60, le=504)] = Field(
        default=252,
        description="D77 v2: sessions of the ticker's own rv20 history for the vol percentile.",
    )
    regime_fit_window: Annotated[int, Field(ge=20, le=1000)] = Field(
        default=252,
        description="D77 v2: most recent labels the trend and vol chains are fit on.",
    )
    regime_alpha: Annotated[float, Field(ge=0.0, le=5.0)] = Field(
        default=0.5,
        description="D77 v2: Laplace pseudo-count per transition-matrix cell.",
    )
    no_trade_require_vix: bool = Field(
        default=True,
        description=(
            "D33: fail closed for new opens when no VIX reading is available "
            "(reason market_data_missing). Off = the guard skips the VIX checks."
        ),
    )
    # -- Close-to-reallocate (E6.4, D19) ----------------------------------------
    realloc_min_edge: Annotated[float, Field(ge=0.0, le=10.0)] = Field(
        default=0.20,
        description=(
            "D19: a swap is suggested only when its net edge in EV per $ of buying power "
            "(after switching costs) is >= this share of the larger |EV per BP| of the two "
            "positions (0.20 = 20% relative). Env: ARC_REALLOC_MIN_EDGE."
        ),
    )
    realloc_pop_tolerance: Annotated[float, Field(ge=0.0, le=1.0)] = Field(
        default=0.05,
        description="D19: the new trade's PoP must be >= the open's remaining PoP minus this.",
    )
    realloc_max_swaps_per_day: Annotated[int, Field(ge=0, le=20)] = Field(
        default=2, description="D19 churn limit: swaps suggested per ET day, all tickers."
    )
    realloc_max_swaps_per_ticker_per_day: Annotated[int, Field(ge=0, le=5)] = Field(
        default=1, description="D19 churn limit: swaps per ticker (closed or opened) per ET day."
    )
    # -- Gate token (E3.2) ----------------------------------------------------
    gate_secret: SecretStr | None = Field(
        default=None,
        description=(
            "ARC_GATE_SECRET: HMAC key for gate tokens (>= 32 bytes). Lives in "
            "~/.hermes/.env only. Unset = no token can be minted or verified (fail closed)."
        ),
    )
    auto_approve: bool = Field(
        default=True,
        description=(
            "D85: paper default on (was off); a live process validates it to off. "
            "D34: auto-approve gate-passed proposals (approver arc:auto-approve) and execute "
            "them in the same chain run. The effective value is per environment: the "
            "ARC_AUTO_APPROVE env var is a paper-only shortcut (ignored when env=live); the "
            "config store keys `auto_approve.paper` / `auto_approve.live` (arc approve auto, "
            "!arc config) set it for one environment each, and enabling live needs the "
            "one-time confirm code."
        ),
    )
    # -- E7.5a: scorecard gate in front of D34 auto-approve ---------------------
    auto_approve_scorecard_gate: bool = Field(
        default=False,
        description=(
            "E7.5a: D34 auto-approve opens a new position only when the E7.3 scorecard shows "
            ">= auto_approve_min_closed_trades closed trades, realised net EV >= 0 over the "
            "latest that many, and realised entry slippage <= modelled half-spread x "
            "auto_approve_slippage_tolerance. Otherwise the card waits for a manual approval "
            "(journal auto_approve_gated). Off = explicit opt-out (paper as pure "
            "calibration), logged as a warning on every auto-approval. Closes are not gated. "
            "D85: paper default off (was on); a live process always forces it on (D70)."
        ),
    )
    auto_approve_min_closed_trades: Annotated[int, Field(ge=1, le=1000)] = Field(
        default=30,
        description="E7.5a: closed trades the scorecard gate needs before auto-approving opens.",
    )
    # -- E11.3 (D70): live evidence is live-only --------------------------------
    auto_approve_live_min_closed_trades: Annotated[int, Field(ge=1, le=1000)] = Field(
        default=30,
        description=(
            "D70: LIVE closed trades (live store only) the live scorecard gate needs before "
            "live auto-approve is effective and the live size cap lifts. Paper uses "
            "auto_approve_min_closed_trades."
        ),
    )
    live_max_contracts_until_gate: Annotated[int, Field(ge=1, le=100)] = Field(
        default=1,
        description=(
            "D70: contracts per live open while the live scorecard gate is not met "
            "(sizing clamps to it and the gate rule live_size_cap rejects anything above). "
            "Lifts automatically when the gate is met. Never applies to paper or to closes."
        ),
    )
    live_auto_approve_requires_gate: bool = Field(
        default=True,
        description=(
            "D70: live auto-approve (auto_approve.live) is effective only once the live "
            "scorecard gate is met; until then the owner approves every live open. Code "
            "constant (not tunable): the owner decided no live auto-approve before the "
            "live collection phase ends."
        ),
    )
    live_gate_met: bool = Field(
        default=False,
        description=(
            "D70: the live scorecard gate was met once on this live store (sticky; key "
            "`live.gate_met`, set on only by arc:live-gate, D26 change log). Lifts the live "
            "size cap and lets auto_approve.live take effect. Never from an env var: "
            "validated to False and applied from the store only (like auto_approve.live)."
        ),
    )
    auto_approve_slippage_tolerance: Annotated[float, Field(gt=0.0, le=10.0)] = Field(
        default=1.5,
        description=(
            "E7.5a: realised entry slippage may be at most modelled half-spread x this "
            "(over the gate's window of closed trades)."
        ),
    )
    owner_slack_user_id: str = Field(
        default="U0C5KUMH28G",
        min_length=1,
        description="Slack user id of the owner (D10). Only this user may `!resume` (E3.3).",
    )
    # NoDecode: the env value is comma-separated ("U1,U2"), split by _parse_str_list.
    approver_slack_user_ids: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["U0C5KUMH28G"],
        min_length=1,
        description=(
            "ARC_APPROVER_SLACK_USER_IDS (comma-separated): Slack user ids whose Approve / "
            "Reject clicks are honoured (E6.1). D10: owner only."
        ),
    )
    db_path: Path | None = Field(
        default=None,
        description=(
            "Audit store path (ARC_DB_PATH). None = data/arc.db in the repo for paper; a live "
            "process defaults to data/arc-live.db (D70: one store per env)."
        ),
    )

    # -- Market data feeds (D7) ---------------------------------------------
    alpaca_data_feed: AlpacaDataFeed = Field(
        default=AlpacaDataFeed.IEX,
        description="Alpaca stock data feed (ARC_ALPACA_DATA_FEED). Free tier: iex.",
    )
    alpaca_options_feed: AlpacaOptionsFeed = Field(
        default=AlpacaOptionsFeed.INDICATIVE,
        description="Alpaca options data feed (ARC_ALPACA_OPTIONS_FEED). Free tier: indicative.",
    )

    # -- Chain scanner (E2.3) ------------------------------------------------
    # Liquidity: the spread rule reuses spread_max_pct / spread_max_abs (§5).
    scanner_min_open_interest: Annotated[int, Field(ge=0)] = Field(
        default=100,
        description="Min open interest per leg (unknown OI fails the filter).",
    )
    scanner_min_volume: Annotated[int, Field(ge=0)] = Field(
        default=10,
        description="Min daily volume per leg (unknown volume fails the filter).",
    )
    scanner_short_delta_min: Annotated[float, Field(gt=0.0, lt=1.0)] = Field(
        default=0.16,
        description="Lower bound of the short-strike |delta| band (D4: 16-30 delta).",
    )
    scanner_short_delta_max: Annotated[float, Field(gt=0.0, lt=1.0)] = Field(
        default=0.30,
        description="Upper bound of the short-strike |delta| band (D4: 16-30 delta).",
    )
    scanner_target_delta: Annotated[float, Field(gt=0.0, lt=1.0)] = Field(
        default=0.20,
        description="Default target |delta| for short strikes (CLI --delta).",
    )
    scanner_wing_width: Annotated[float, Field(gt=0.0)] = Field(
        default=5.0,
        description="Target wing width in dollars between short and long strikes.",
    )
    # Debit strategies (D25, E3.4): long-leg band and debit-vertical short band.
    scanner_long_delta_min: Annotated[float, Field(gt=0.0, lt=1.0)] = Field(
        default=0.40, description="Lower bound of the long-leg |delta| band (debit strategies)."
    )
    scanner_long_delta_max: Annotated[float, Field(gt=0.0, lt=1.0)] = Field(
        default=0.70, description="Upper bound of the long-leg |delta| band (debit strategies)."
    )
    scanner_long_target_delta: Annotated[float, Field(gt=0.0, lt=1.0)] = Field(
        default=0.55, description="Target |delta| for the long leg of debit strategies."
    )
    scanner_debit_short_delta_min: Annotated[float, Field(gt=0.0, lt=1.0)] = Field(
        default=0.20, description="Lower bound of the debit-vertical short-leg |delta| band."
    )
    scanner_debit_short_delta_max: Annotated[float, Field(gt=0.0, lt=1.0)] = Field(
        default=0.35, description="Upper bound of the debit-vertical short-leg |delta| band."
    )
    scanner_debit_short_target_delta: Annotated[float, Field(gt=0.0, lt=1.0)] = Field(
        default=0.30, description="Target |delta| for the debit-vertical short leg."
    )
    scanner_debit_width: Annotated[float | None, Field(gt=0.0)] = Field(
        default=None,
        description=(
            "Target debit-vertical width in dollars; None = pick the short by its delta "
            "band/target only (the width then follows the underlying's price)."
        ),
    )
    scanner_risk_free_rate: float = Field(
        default=0.04,
        description="Risk-free rate (annualised, continuous) for scanner Greeks / EV proxy.",
    )
    scanner_iv_lookback: Annotated[int, Field(ge=2)] = Field(
        default=252,
        description="IV rank / percentile lookback in observations (~1 trading year).",
    )
    scanner_iv_history_dir: Path = Field(
        default=Path("data/iv_history"),
        description=(
            "E4.12: legacy per-ticker ATM IV CSVs (date,atm_iv), read once by "
            "`arc iv import-csv` into iv_daily; nothing writes here any more."
        ),
    )

    # -- IV history (E4.12, D55) -------------------------------------------------
    iv_min_obs_rank: Annotated[int, Field(ge=2)] = Field(
        default=120,
        description=(
            "IV observations needed before IV rank / percentile are reported (regime, "
            "scanner). A 20-observation rank is noise (D55)."
        ),
    )
    iv_crosscheck_max_pts: Annotated[float, Field(gt=0, le=50)] = Field(
        default=3.0,
        description="iv.record: |our iv30 - Cboe iv30| above this (vol pts) opens an [Ops] alert.",
    )
    iv_crosscheck_max_names: Annotated[int, Field(ge=0, le=20)] = Field(
        default=5,
        description="iv.record: pool names cross-checked against Cboe besides SPY and QQQ.",
    )
    iv_ext_max_age_days: Annotated[int, Field(ge=1, le=31)] = Field(
        default=8,
        description=(
            "An Option Strategist percentile older than this is not shown as "
            "iv_percentile_ext (weekly file, Saturdays)."
        ),
    )
    iv_dividend_yields: dict[str, Annotated[float, Field(ge=0, le=0.2)]] = Field(
        default_factory=lambda: {
            "SPY": 0.012,
            "QQQ": 0.006,
            "IWM": 0.012,
            "DIA": 0.016,
            "XLF": 0.015,
            "XLE": 0.032,
            "XLK": 0.007,
            "XLV": 0.016,
            "XLI": 0.014,
            "XLY": 0.008,
            "XLP": 0.026,
            "XLU": 0.029,
        },
        description=(
            "E4.12 backfill: continuous dividend yield per ETF for the Black-Scholes IV "
            "inversion; any other ticker uses 0."
        ),
    )
    spot_max_spread_pct: Annotated[float, Field(gt=0, le=1)] = Field(
        default=0.05,
        description=(
            "E4.12 market_spot: a two-sided quote wider than this share of its mid gives way "
            "to today's daily close (a zero side always does)."
        ),
    )
    alpaca_data_calls_per_minute: Annotated[int, Field(ge=1, le=200)] = Field(
        default=150,
        description=(
            "E4.12: shared cross-process budget for bulk Alpaca data pulls "
            "(`arc iv backfill`); the free data plan allows 200/min."
        ),
    )

    # -- Ingestion (E4.1) ----------------------------------------------------
    ingest_rss_timeout_seconds: float = Field(
        default=20.0,
        gt=0,
        le=120,
        description="Per-feed HTTP timeout; a slow or dead feed is skipped, never hangs the tick.",
    )
    edgar_user_agent: str = Field(
        default="ProjectArc/0.1 (arc@example.com)",
        description="User-Agent header for SEC EDGAR requests (required by EDGAR).",
    )
    finnhub_api_key: str = Field(
        default="",
        description="Finnhub API key for earnings calendar (free tier).",
    )
    # -- Finnhub per-ticker context (E4.8 / D46) --------------------------------
    finnhub_calls_per_minute: Annotated[int, Field(ge=1, le=60)] = Field(
        default=55,
        description=(
            "D46: Finnhub calls per minute for the whole key, shared across processes by "
            "every Finnhub caller (earnings calendar included). The free key allows 60."
        ),
    )
    finnhub_max_tickers: Annotated[int, Field(ge=1, le=200)] = Field(
        default=50,
        description=(
            "D46/D51: per-ticker Finnhub jobs fetch at most this many tickers per run "
            "(open-position underlyings first, then today's candidates, core, momentum, "
            "discovery)."
        ),
    )
    finnhub_insider_window_days: Annotated[int, Field(ge=7, le=365)] = Field(
        default=90,
        description="D46: insider_activity looks back this many days.",
    )
    finnhub_cluster_buyers: Annotated[int, Field(ge=2, le=20)] = Field(
        default=3,
        description="D46: cluster_buy = at least this many distinct insiders buying ...",
    )
    finnhub_cluster_days: Annotated[int, Field(ge=1, le=180)] = Field(
        default=30,
        description="D46: ... within this many days.",
    )

    # -- YouTube audio-transcription fallback (E4.1b, D15) -------------------
    yt_caption_grace_minutes: Annotated[int, Field(ge=0)] = Field(
        default=30,
        description=(
            "Only transcribe audio for caption-less videos older than this "
            "(ARC_YT_CAPTION_GRACE_MINUTES); younger ones wait for YouTube's captions."
        ),
    )
    yt_max_audio_minutes: Annotated[int, Field(ge=1)] = Field(
        default=60,
        description="Skip audio transcription for longer videos (ARC_YT_MAX_AUDIO_MINUTES).",
    )
    yt_max_audio_per_run: Annotated[int, Field(ge=0)] = Field(
        default=5,  # D60: was 3
        description="Max audio transcriptions per ingest run (ARC_YT_MAX_AUDIO_PER_RUN).",
    )
    yt_max_audio_per_slot: Annotated[int, Field(ge=0)] = Field(
        default=5,  # D60: was 4 (7 channels per run since E14.4)
        description=(
            "Max audio transcriptions across all channels of one daily youtube.briefs "
            "run (E4.6; ARC_YT_MAX_AUDIO_PER_SLOT)."
        ),
    )
    # -- YouTube caption rate-limit backoff (E4.1c, D15) ----------------------
    yt_caption_sleep_seconds: Annotated[float, Field(ge=0)] = Field(
        default=5.0,
        description=(
            "Pause between timedtext (caption) requests within a run "
            "(ARC_YT_CAPTION_SLEEP_SECONDS; yt-dlp guidance: 5-10 s)."
        ),
    )
    yt_caption_cooldown_base_minutes: Annotated[float, Field(gt=0)] = Field(
        default=30.0,
        description=(
            "Caption cooldown after the first 429 in a streak; doubles per consecutive "
            "rate-limited run (ARC_YT_CAPTION_COOLDOWN_BASE_MINUTES)."
        ),
    )
    yt_caption_cooldown_max_minutes: Annotated[float, Field(gt=0)] = Field(
        default=360.0,
        description="Upper bound on the caption cooldown (ARC_YT_CAPTION_COOLDOWN_MAX_MINUTES).",
    )
    yt_caption_cooldown_jitter: Annotated[float, Field(ge=0, lt=1)] = Field(
        default=0.10,
        description=(
            "Relative ± jitter applied to the caption cooldown "
            "(ARC_YT_CAPTION_COOLDOWN_JITTER; 0.10 = ±10%)."
        ),
    )
    whisper_model: str = Field(
        default="mlx-community/whisper-large-v3-turbo",
        description="Local mlx-whisper model repo (ARC_WHISPER_MODEL).",
    )
    ffmpeg_bin: str = Field(
        default="",
        description="ffmpeg path (ARC_FFMPEG_BIN); empty → PATH, then ~/.hermes/tools/ffmpeg-*.",
    )

    # -- Persona model routing (E8.1, PLAN §2.4, D8) --------------------------
    llm_routing_file: Path | None = Field(
        default=None,
        description=(
            "Per-persona model routing YAML (ARC_LLM_ROUTING_FILE); "
            "None → config/llm_routing.yaml. The only place persona/tier models are set."
        ),
    )

    # -- Scalp candidate pipeline (E4.2) -------------------------------------
    scalp_hermes_bin: str = Field(
        default="hermes",
        description="Hermes CLI executable used for one-shot Scalp calls.",
    )
    scalp_timeout_seconds: Annotated[int, Field(ge=10)] = Field(
        default=240,
        description="Timeout for a single Scalp LLM batch call.",
    )
    # -- Scout daily read (E13.7, D56) ---------------------------------------
    scout_video_chars: Annotated[int, Field(ge=1000, le=200_000)] = Field(
        default=12_000,
        description="Channel-brief chars per Scout prompt, split equally between "
        "youtube_macro and youtube_micro, then among the channels present.",
    )
    scout_timeout_seconds: Annotated[int, Field(ge=10)] = Field(
        default=240,
        description="Timeout for the Scout's one daily LLM call.",
    )
    scout_max_calls: Annotated[int, Field(ge=1, le=30)] = Field(
        default=30,
        description="Max ticker calls kept from one Scout reply (schema cap 30).",
    )
    scalp_batch_size: Annotated[int, Field(ge=1, le=50)] = Field(
        default=8,
        description="Max stories digested per stage-1 (digest) LLM call (E4.5).",
    )
    scalp_max_doc_chars: Annotated[int, Field(ge=200)] = Field(
        default=4000,
        description="Per-document text budget in the Scalp prompt (truncated beyond).",
    )
    # -- Source fairness + synthesis (E4.5, D30) --------------------------------
    scalp_doc_budget: Annotated[int, Field(ge=1, le=1000)] = Field(
        default=120,
        description=(
            "D30: docs the Scalp reads per run, shared across sources by weighted "
            "round-robin (config/routines.yaml sources); the rest wait or are "
            "marked skipped_budget when their context TTL runs out."
        ),
    )
    scalp_story_threshold: Annotated[float, Field(gt=0.0, le=1.0)] = Field(
        default=0.5,
        description="D30: token-set Jaccard of normalised headlines to cluster two docs.",
    )
    scalp_story_window_hours: Annotated[float, Field(gt=0, le=168)] = Field(
        default=24.0,
        description="D30: docs cluster into one story only within this many hours.",
    )
    scalp_story_batch_size: Annotated[int, Field(ge=1, le=200)] = Field(
        default=40,
        description="D30: story digests per stage-2 Scalp LLM call.",
    )
    scalp_story_doc_chars: Annotated[int, Field(ge=100)] = Field(
        default=1500,
        description="D30: per-document text in the stage-1 digest prompt (3 docs per story).",
    )
    # E13.10 (D56): the options tape in the Scalp prompt.
    scalp_tape_max_chars: Annotated[int, Field(ge=200, le=1500)] = Field(
        default=1500,
        description="E13.10: the options tape block in the Scalp prompt is cut to this size.",
    )
    scalp_tape_pc_bull: Annotated[float, Field(gt=0.0, lt=1.0)] = Field(
        default=0.7,
        description="E13.10: session put/call volume at or below this reads bullish.",
    )
    scalp_tape_pc_bear: Annotated[float, Field(gt=1.0, le=10.0)] = Field(
        default=1.3,
        description="E13.10: session put/call volume at or above this reads bearish.",
    )

    # -- Pipeline runner (E5.2) -----------------------------------------------
    persona_timeout_seconds: Annotated[int, Field(ge=10)] = Field(
        default=600,
        description="Timeout for a single Research/Quant/Risk LLM call.",
    )
    # E13.8 (D56/D54): the compact Research prompt's budget, of which
    # arc.pipeline.research_pool.EXIT_BLOCK_RESERVE_CHARS (7,200) is kept for E13.17's
    # exit block. Over it -> headlines trimmed, then the pool cut to its top 40.
    research_prompt_max_chars: Annotated[int, Field(ge=20_000, le=400_000)] = Field(
        default=80_000,
        description="Target size of the compact Research prompt (chars).",
    )
    # E13.17 (D56): Research-managed exits.
    exit_block_max_chars_per_position: Annotated[int, Field(ge=200, le=2_000)] = Field(
        default=600,
        description="Research exit-watch block: hard clip per open position (chars).",
    )
    quant_exit_max_cases: Annotated[int, Field(ge=1, le=20)] = Field(
        default=8,
        description="quant.exit: at most this many exit cases per call (most triggers first).",
    )
    quant_exit_case_max_chars: Annotated[int, Field(ge=300, le=3_000)] = Field(
        default=900,
        description="quant.exit prompt: hard clip per exit case (chars).",
    )
    # E13.18 (D56): Risk exit review.
    exit_review_max_consecutive_holds: Annotated[int, Field(ge=1, le=10)] = Field(
        default=3,
        description="Risk `hold` on a case with a deterministic discretionary signal is "
        "honoured for at most this many consecutive reviews; the next one closes.",
    )
    exit_steps_min_remaining_s: Annotated[int, Field(ge=0, le=600)] = Field(
        default=60,
        description="quant.exit / risk.exit: loop budget (s) a step needs to start "
        "(routines.yaml steps.*.min_remaining_s mirrors it).",
    )
    pipeline_max_shortlist: Annotated[int, Field(ge=1, le=20)] = Field(
        default=10,
        description=(
            "Quant/Risk budget (D28): the first N Research-ranked tickers get a structure. "
            "Never shown to Research; ranked items beyond it stay on the card as "
            "'Ranked, not structured'."
        ),
    )
    pipeline_max_context_notes: Annotated[int, Field(ge=0, le=100)] = Field(
        default=20,
        description="Max prior D27 notes (regime view/thesis/observation) shown to Research.",
    )
    pipeline_scan_top: Annotated[int, Field(ge=1, le=20)] = Field(
        default=5,
        description="Scanner candidates per ticker offered to Quant (Quant picks among them).",
    )

    # -- Universe (D9 revised, D28) ------------------------------------------
    universe: list[str] = Field(
        default_factory=lambda: list(DEFAULT_UNIVERSE),
        description=(
            "Core tier (was the D28 seed list): always scanned and always accepted. "
            "Consumers read the active list (arc.universe.tiers.active_tickers), never this "
            "field. A list longer than 25 (D58 MAX_CORE) is a pre-D51 override and is ignored "
            "in favour of "
            "config/universe.yaml core. In strict mode (ARC_UNIVERSE_MODE=strict) the "
            "active list is the allow-list."
        ),
    )
    universe_active_max: Annotated[int, Field(ge=1, le=60)] = Field(
        default=50,
        description="D58/D67: the deduped active list (core, momentum, then discovery ⇄ "
        "trending by round robin) is capped at this many names; the rest are journaled "
        "universe:over_active_cap.",
    )
    # -- D56 (E13.4) / D58 (E13.19): four tiers, per-tier sizes and floors
    universe_momentum_size: Annotated[int, Field(ge=0, le=50)] = Field(
        default=20,
        description="D56: momentum tier size (top N of the momentum feed; the feed itself "
        "keeps its 25 rows, universe.momentum `size`).",
    )
    universe_discovery_size: Annotated[int, Field(ge=0, le=25)] = Field(
        default=25,
        description="D58: discovery tier size (the Scout's YouTube calls, ranked; was 20).",
    )
    universe_trending_size: Annotated[int, Field(ge=0, le=25)] = Field(
        default=25,
        description="D58: trending tier size (the daily retail_buzz ranking, E13.19).",
    )
    universe_floor_core: Annotated[float, Field(ge=0.0, le=1.0)] = Field(
        default=0.4,
        description="D56: Scalp confidence floor for core names.",
    )
    universe_floor_momentum: Annotated[float, Field(ge=0.0, le=1.0)] = Field(
        default=0.5,
        description="D56: Scalp confidence floor for momentum names.",
    )
    universe_floor_discovery: Annotated[float, Field(ge=0.0, le=1.0)] = Field(
        default=0.6,
        description="D56: Scalp confidence floor for discovery names (owner: >= 0.6).",
    )
    universe_floor_trending: Annotated[float, Field(ge=0.0, le=1.0)] = Field(
        default=0.6,
        description="D58: Scalp confidence floor for trending names (default pending owner "
        "confirmation; same as discovery).",
    )
    universe_mode: UniverseMode = Field(
        default=UniverseMode.SEED,
        description=(
            "ARC_UNIVERSE_MODE: seed (default) = any symbol-master ticker that passes the "
            "liquidity screen may become a Scalp candidate; strict = universe only."
        ),
    )
    universe_config_file: Path | None = Field(
        default=None,
        description="ARC_UNIVERSE_CONFIG_FILE; None -> config/universe.yaml.",
    )

    # -- Control panel (D26, E8.5) --------------------------------------------
    config_version: int | None = Field(
        default=None,
        description=(
            "Set by arc.control.effective_settings(): the latest config_changes id these "
            "settings include (0 = no override ever). None = built without the override store. "
            "Recorded on every routine run and run manifest. Never tunable."
        ),
    )
    # YAML-file overrides (target -> {path: value}) applied by arc.control; read them
    # through arc.control.exit_config()/cost_model()/effective_routines().
    _yaml_overrides: dict[str, dict[tuple[str, ...], object]] = PrivateAttr(default_factory=dict)

    def yaml_overrides(self, target: str) -> dict[tuple[str, ...], object]:
        """D26 overrides for one YAML config (``exits``, ``costs``, ...); ``{}`` if none."""
        return dict(self._yaml_overrides.get(target, {}))

    # -- Validators ----------------------------------------------------------

    @field_validator(
        "universe",
        "approver_slack_user_ids",
        mode="before",
    )
    @classmethod
    def _parse_str_list(cls, v: object) -> object:
        """Accept a comma-separated string from env vars."""
        if isinstance(v, str):
            return [s.strip() for s in v.split(",") if s.strip()]
        return v

    @field_validator("dte_max")
    @classmethod
    def _dte_range(cls, v: int, info: object) -> int:
        """Ensure dte_max >= dte_min."""
        # info.data contains already-validated fields in declaration order
        data = getattr(info, "data", {})
        dte_min = data.get("dte_min", 30)
        if v < dte_min:
            msg = f"dte_max ({v}) must be >= dte_min ({dte_min})"
            raise ValueError(msg)
        return v

    @model_validator(mode="after")
    def _enforce_live_env_file(self) -> ArcSettings:
        """If ARC_ENV=live, require ~/.arc/live.env to exist."""
        if self.env is ArcEnv.LIVE and not _LIVE_ENV_PATH.is_file():
            msg = (
                f"ARC_ENV=live requires {_LIVE_ENV_PATH} to exist. "
                "Phase 1 is paper-only; this file must NOT be created yet."
            )
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _resolve_account_profile(self) -> ArcSettings:
        """Load the selected profile (unknown name -> error at startup, not at trade time)."""
        spec = self.account_profile_spec
        if spec is None or spec.name != self.account_profile:
            profiles = load_account_profiles(self.account_profiles_file)
            self.account_profile_spec = profiles.get(self.account_profile)
        return self

    @property
    def profile(self) -> AccountProfile:
        """The active account profile. Raises if it does not match ``account_profile``."""
        spec = self.account_profile_spec
        if spec is None or spec.name != self.account_profile:
            msg = (
                f"account profile {self.account_profile!r} is not resolved; build settings "
                "with ArcSettings(...) / with_profile(), not model_copy()"
            )
            raise ValueError(msg)
        return spec

    def with_profile(self, name: str) -> ArcSettings:
        """A copy of these settings under account profile *name* (re-resolved)."""
        profiles = load_account_profiles(
            self.account_profiles_file, overrides=self.yaml_overrides("account_profiles")
        )
        return self.model_copy(
            update={
                "account_profile": name,
                "account_profile_spec": profiles.get(name),
            }
        )

    @property
    def entry_dte_window(self) -> tuple[int, int]:
        """Entry DTE window: the profile's override when set, else ``dte_min/dte_max``."""
        p = self.profile
        if p.dte_min is not None and p.dte_max is not None:
            return p.dte_min, p.dte_max
        return self.dte_min, self.dte_max

    @model_validator(mode="after")
    def _order_budget_consistent(self) -> ArcSettings:
        """D32: reserve below the cap; the restrictive tier starts at or below the open limit."""
        open_limit = self.order_budget_daily_max - self.order_budget_close_reserve
        if open_limit < 1:
            msg = (
                f"order_budget_close_reserve ({self.order_budget_close_reserve}) must be below "
                f"order_budget_daily_max ({self.order_budget_daily_max})"
            )
            raise ValueError(msg)
        if self.order_budget_restrict_at > open_limit:
            msg = (
                f"order_budget_restrict_at ({self.order_budget_restrict_at}) must not exceed "
                f"the open limit {open_limit} (daily_max - close_reserve)"
            )
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _env_switches_paper_only(self) -> ArcSettings:
        """The env-var shortcuts for the per-env switches never reach live (D34).

        ``ARC_AUTO_APPROVE`` / ``ARC_AUTO_EXIT_DEFINED_RISK`` set the *paper* value
        only: a live process always validates them to off. The only way to enable
        either in live is the config store (``auto_approve.live``,
        ``auto_exit_defined_risk.live``, confirm code required), which
        :func:`arc.control.effective.apply_changes` applies *after* validation.
        """
        if self.env is not ArcEnv.LIVE:
            return self
        for name in sorted(PER_ENV_SWITCHES):
            if getattr(self, name):
                # D85: the paper default is on, so only an explicit value is worth a warning.
                if name in self.model_fields_set:
                    log.warning(
                        "per-env switch forced off in live (env var is paper-only)", key=name
                    )
                setattr(self, name, False)
        return self

    @model_validator(mode="after")
    def _live_evidence_is_live_only(self) -> ArcSettings:
        """D70: the paper opt-out never reaches live; live gets its own store path.

        - ``auto_approve_scorecard_gate`` is forced on in live (the E6.6a opt-out
          is the paper collection phase only). :func:`arc.control.effective.apply_changes`
          validates every store override through this, so a stored ``off`` cannot
          switch it off in live either.
        - ``live_auto_approve_requires_gate`` cannot be turned off (owner decision).
        - ``live_gate_met`` is never set by an env var or kwarg (store only, post-validation).
        - ``db_path`` unset in live defaults to ``data/arc-live.db``.
        """
        if self.live_gate_met:
            log.warning("live_gate_met forced off (set by arc:live-gate in the store only)")
            self.live_gate_met = False
        if self.env is not ArcEnv.LIVE:
            return self
        if not self.auto_approve_scorecard_gate:
            # D85: the paper default is off, so only an explicit opt-out is worth a warning.
            if "auto_approve_scorecard_gate" in self.model_fields_set:
                log.warning(
                    "scorecard gate forced on in live (auto_approve.scorecard_gate is paper-only)",
                    key="auto_approve_scorecard_gate",
                )
            self.auto_approve_scorecard_gate = True
        if not self.live_auto_approve_requires_gate:
            log.warning(
                "live auto-approve always requires the live gate (D70)",
                key="live_auto_approve_requires_gate",
            )
            self.live_auto_approve_requires_gate = True
        if self.db_path is None:
            # data/arc-live.db (== arc.store.identity.DEFAULT_LIVE_DB_PATH; no arc.store
            # import here, arc.config sits under the gate's no-storage contract).
            self.db_path = Path(__file__).resolve().parent.parent / "data" / "arc-live.db"
        return self


# Settings switches with one value per ARC_ENV (D34). The env var sets the paper
# value only; `arc.control` applies the store's `<field>.<env>` key for the running env.
PER_ENV_SWITCHES: frozenset[str] = frozenset({"auto_approve", "auto_exit_defined_risk"})
# D70: live-only fields the validators force off; only the store's live key turns them
# on, applied after validation by arc.control.effective.apply_changes.
STORE_ONLY_LIVE_FIELDS: frozenset[str] = PER_ENV_SWITCHES | {"live_gate_met"}


def get_settings(**overrides: object) -> ArcSettings:
    """Convenience factory — use in application code and tests."""
    return ArcSettings(**overrides)  # type: ignore[arg-type]
