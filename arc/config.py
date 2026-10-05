"""Configuration via pydantic-settings: ARC_ENV, limits, universe.

See PLAN.md §5 for risk defaults and §9.2 (D9) for the default universe.
"""

from __future__ import annotations

import enum
import json
from pathlib import Path
from typing import Annotated

import structlog
from pydantic import Field, PrivateAttr, SecretStr, field_validator, model_validator
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
# Structure whitelist
# ---------------------------------------------------------------------------


class StructureKind(enum.StrEnum):
    """Allowed option structure types (D4; split by D25/E3.4).

    ``vertical`` was split into ``vertical_debit`` and ``vertical_credit``. The old
    value is still accepted in ``ARC_STRUCTURE_WHITELIST`` as a deprecated alias
    that expands to both (see :data:`LEGACY_WHITELIST_ALIASES`).
    """

    VERTICAL_DEBIT = "vertical_debit"
    VERTICAL_CREDIT = "vertical_credit"
    IRON_CONDOR = "iron_condor"
    LONG_CALL = "long_call"
    LONG_PUT = "long_put"


# Deprecated whitelist spellings -> the kinds they stand for.
LEGACY_WHITELIST_ALIASES: dict[str, tuple[StructureKind, ...]] = {
    "vertical": (StructureKind.VERTICAL_DEBIT, StructureKind.VERTICAL_CREDIT),
}


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

# D13/D45 (E4.6): the four channels behind the daily 05:00 ET ``youtube.briefs`` job
# (config/routines.yaml is the source of truth; this list is the CLI default).
# Channel ids, never @handles, so a rename can't break them.
DEFAULT_YOUTUBE_CHANNELS: list[str] = [
    "https://www.youtube.com/channel/UC-m6zNItyoDk5lSykDlhE4Q/videos",  # StockedUp
    "https://www.youtube.com/channel/UCvJZEG5x-DVYZKTz--pS39w/videos",  # FX Evolution
    "https://www.youtube.com/channel/UCYKtr6GfycBqQJf32tbQSbQ/videos",  # Trade Brigade
    "https://www.youtube.com/channel/UCTeFsS-bP0XEt3NBMjfW2cA/videos",  # Arete Trading
]


# ---------------------------------------------------------------------------
# Default core universe (D51; was the D9 flat seed list)
# ---------------------------------------------------------------------------

# D51 core tier: 25 stocks, no ETFs; the last 8 fit a $10k-$25k account. Must equal
# config/universe.yaml `core:` (tests/test_universe_tiers.py pins it). SPY/QQQ are the
# market reference (config/universe.yaml `tiers.market_reference`), not trade names.
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
    "SOFI",
    "HOOD",
    "INTC",
    "SMCI",
    "NFLX",
    "UBER",
    "BAC",
    "MARA",
]


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

    # -- Risk limits (PLAN.md §5) -------------------------------------------
    max_alloc_pct: Annotated[float, Field(gt=0.0, le=1.0)] = Field(
        default=0.05,
        description="Max allocation per underlying as fraction of equity (5%).",
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
    portfolio_delta_cap: Annotated[float, Field(ge=0.0)] = Field(
        default=0.30,
        description="|net Δ| cap: 0.30 × equity/100 per dollar.",
    )
    portfolio_vega_cap_pct: Annotated[float, Field(ge=0.0, le=1.0)] = Field(
        default=0.005,
        description="|ν| cap: 0.5% of equity per vol-point.",
    )
    # NoDecode: the env value may be comma-separated or JSON; see _parse_whitelist.
    structure_whitelist: Annotated[list[StructureKind], NoDecode] = Field(
        default=[
            StructureKind.VERTICAL_DEBIT,
            StructureKind.VERTICAL_CREDIT,
            StructureKind.IRON_CONDOR,
            StructureKind.LONG_CALL,
            StructureKind.LONG_PUT,
        ],
        description=(
            "Allowed structure types (D4). The account profile narrows this further (D25). "
            "Deprecated alias 'vertical' = vertical_debit + vertical_credit."
        ),
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
    limit_tick: Annotated[float, Field(gt=0.0)] = Field(
        default=0.01,
        description="Limit price must be a whole multiple of this tick ($).",
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
            "first attempt, the Investor re-prices at the current mid. A mid outside the "
            "gate-approved band is not sent (journal reason stale_band)."
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
        default=False,
        description=(
            "D24: when true, fired exits on defined-risk positions skip the Slack approval. "
            "Default false: every exit is a proposal that needs an approval. Per environment "
            "(D34): ARC_AUTO_EXIT_DEFINED_RISK is the paper shortcut; live is only switched "
            "through the config store (`auto_exit_defined_risk.live`)."
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
    order_budget_restrictive_director_max_shortlist: Annotated[int, Field(ge=0, le=10)] = Field(
        default=1, description="Restrictive tier: Director shortlist cap."
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
    # -- Portfolio-aware Director, dedupe, no-trade guard (E5.9, D33) ----------
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
            "D33: net |delta| / |vega| usage of the PLAN §5 cap above which the book is "
            "flagged delta_near_cap / vega_near_cap."
        ),
    )
    portfolio_context_max_positions: Annotated[int, Field(ge=1, le=50)] = Field(
        default=12,
        description="D33: positions rendered in full in the Director prompt (largest first).",
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
            "D33: when the snapshot holds a SPY regime entry whose stickiness (P[stay]) is "
            "below this, the regime counts as transitional and new opens are blocked."
        ),
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
        default=False,
        description=(
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
        default=True,
        description=(
            "E7.5a: D34 auto-approve opens a new position only when the E7.3 scorecard shows "
            ">= auto_approve_min_closed_trades closed trades, realised net EV >= 0 over the "
            "latest that many, and realised entry slippage <= modelled half-spread x "
            "auto_approve_slippage_tolerance. Otherwise the card waits for a manual approval "
            "(journal auto_approve_gated). Off = explicit opt-out (paper as pure "
            "calibration), logged as a warning on every auto-approval. Closes are not gated."
        ),
    )
    auto_approve_min_closed_trades: Annotated[int, Field(ge=1, le=1000)] = Field(
        default=30,
        description="E7.5a: closed trades the scorecard gate needs before auto-approving opens.",
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
        description="Audit store path (ARC_DB_PATH). None = data/arc.db in the repo.",
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
    scanner_iv_min_obs: Annotated[int, Field(ge=2)] = Field(
        default=20,
        description="Minimum IV observations before IV rank / percentile are reported.",
    )
    scanner_iv_history_dir: Path = Field(
        default=Path("data/iv_history"),
        description="Directory of per-ticker ATM IV history CSVs (date,atm_iv).",
    )

    # -- Ingestion (E4.1) ----------------------------------------------------
    ingest_rss_feeds: list[str] = Field(
        default_factory=list,
        description="RSS feed URLs for the Scout connector.",
    )
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
            "trending)."
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
    ingest_youtube_channels: list[str] = Field(
        default_factory=lambda: list(DEFAULT_YOUTUBE_CHANNELS),
        description=(
            "YouTube channel/playlist URLs for transcript ingestion (`arc ingest`). "
            "Default: the four D45 channels (StockedUp, FX Evolution, Trade Brigade, Arete)."
        ),
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
        default=3,
        description="Max audio transcriptions per ingest run (ARC_YT_MAX_AUDIO_PER_RUN).",
    )
    yt_max_audio_per_slot: Annotated[int, Field(ge=0)] = Field(
        default=4,
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

    # -- Scout candidate pipeline (E4.2) -------------------------------------
    scout_hermes_bin: str = Field(
        default="hermes",
        description="Hermes CLI executable used for one-shot Scout calls.",
    )
    scout_timeout_seconds: Annotated[int, Field(ge=10)] = Field(
        default=240,
        description="Timeout for a single Scout LLM batch call.",
    )
    scout_min_confidence: Annotated[float, Field(ge=0.0, le=1.0)] = Field(
        default=0.6,
        description="Candidates below this Scout confidence are dropped.",
    )
    scout_batch_size: Annotated[int, Field(ge=1, le=50)] = Field(
        default=8,
        description="Max stories digested per stage-1 (digest) LLM call (E4.5).",
    )
    scout_max_doc_chars: Annotated[int, Field(ge=200)] = Field(
        default=4000,
        description="Per-document text budget in the Scout prompt (truncated beyond).",
    )
    # -- Source fairness + synthesis (E4.5, D30) --------------------------------
    scout_doc_budget: Annotated[int, Field(ge=1, le=1000)] = Field(
        default=120,
        description=(
            "D30: docs the Scout reads per run, shared across sources by weighted "
            "round-robin (config/routines.yaml sources); the rest wait or are "
            "marked skipped_budget when their context TTL runs out."
        ),
    )
    scout_story_threshold: Annotated[float, Field(gt=0.0, le=1.0)] = Field(
        default=0.5,
        description="D30: token-set Jaccard of normalised headlines to cluster two docs.",
    )
    scout_story_window_hours: Annotated[float, Field(gt=0, le=168)] = Field(
        default=24.0,
        description="D30: docs cluster into one story only within this many hours.",
    )
    scout_story_batch_size: Annotated[int, Field(ge=1, le=200)] = Field(
        default=40,
        description="D30: story digests per stage-2 Scout LLM call.",
    )
    scout_story_doc_chars: Annotated[int, Field(ge=100)] = Field(
        default=1500,
        description="D30: per-document text in the stage-1 digest prompt (3 docs per story).",
    )
    ingest_macro_horizon_days: Annotated[int, Field(ge=1, le=180)] = Field(
        default=45,
        description="E4.5: macro calendar (FOMC/BLS) looks this many days ahead.",
    )
    uoa_min_volume: Annotated[int, Field(ge=1)] = Field(
        default=500,
        description="E4.5 UOA: a contract needs this daily volume to be flagged vol/OI.",
    )
    uoa_vol_oi_ratio: Annotated[float, Field(gt=0)] = Field(
        default=2.0,
        description="E4.5 UOA: contract volume / open interest at or above this is flagged.",
    )
    uoa_volume_spike_ratio: Annotated[float, Field(gt=0)] = Field(
        default=2.0,
        description="E4.5 UOA: underlying volume / 20-day average at or above this is flagged.",
    )
    uoa_max_dte: Annotated[int, Field(ge=1, le=365)] = Field(
        default=60,
        description="E4.5 UOA: expiries within this many days are scanned.",
    )
    uoa_min_dte: Annotated[int, Field(ge=0, le=30)] = Field(
        default=3,
        description="E4.5 UOA: contracts expiring sooner (0DTE churn) are never flagged.",
    )
    uoa_min_open_interest: Annotated[int, Field(ge=1)] = Field(
        default=100,
        description="E4.5 UOA: vol/OI is only read on lines with at least this open interest.",
    )
    uoa_min_hot_share: Annotated[float, Field(ge=0, le=1)] = Field(
        default=0.02,
        description="E4.5 UOA: hot lines must carry this share of the ticker's option volume.",
    )
    ex_dividend_horizon_days: Annotated[int, Field(ge=1, le=180)] = Field(
        default=45,
        description="E4.5: ex-dividend dates looked up this many days ahead.",
    )

    # -- Pipeline runner (E5.2) -----------------------------------------------
    persona_timeout_seconds: Annotated[int, Field(ge=10)] = Field(
        default=600,
        description="Timeout for a single Director/Quant/Risk LLM call.",
    )
    pipeline_max_shortlist: Annotated[int, Field(ge=1, le=20)] = Field(
        default=10,
        description=(
            "Quant/Risk budget (D28): the first N Director-ranked tickers get a structure. "
            "Never shown to the Director; ranked items beyond it stay on the card as "
            "'Ranked, not structured'."
        ),
    )
    pipeline_max_context_notes: Annotated[int, Field(ge=0, le=100)] = Field(
        default=20,
        description="Max prior D27 notes (regime view/thesis/observation) shown to the Director.",
    )
    pipeline_scan_top: Annotated[int, Field(ge=1, le=20)] = Field(
        default=5,
        description="Scanner candidates per ticker offered to Quant (Quant picks among them).",
    )

    # -- Universe (D9 revised, D28) ------------------------------------------
    universe: list[str] = Field(
        default_factory=lambda: list(DEFAULT_UNIVERSE),
        description=(
            "D51 core tier (was the D28 seed list): always scanned and always accepted. "
            "Consumers read the active list (arc.universe.tiers.active_tickers), never this "
            "field. A list longer than 30 is a pre-D51 override and is ignored in favour of "
            "config/universe.yaml core. In strict mode (ARC_UNIVERSE_MODE=strict) the "
            "active list is the allow-list."
        ),
    )
    universe_active_max: Annotated[int, Field(ge=1, le=60)] = Field(
        default=50,
        description="D51: the deduped active list (core > momentum > trending > discovery) "
        "is capped at this many names; the rest are journaled universe:over_active_cap.",
    )
    universe_momentum_size: Annotated[int, Field(ge=0, le=50)] = Field(
        default=25,
        description="D51: momentum tier size (top N S&P 500 Momentum holdings, monthly, E12.2).",
    )
    universe_trending_size: Annotated[int, Field(ge=0, le=50)] = Field(
        default=25,
        description="D51: trending tier size (daily rules-based list, E12.3).",
    )
    universe_mode: UniverseMode = Field(
        default=UniverseMode.SEED,
        description=(
            "ARC_UNIVERSE_MODE: seed (default) = any symbol-master ticker that passes the "
            "liquidity screen may become a Scout candidate; strict = universe only."
        ),
    )
    universe_config_file: Path | None = Field(
        default=None,
        description="ARC_UNIVERSE_CONFIG_FILE; None -> config/universe.yaml.",
    )
    scout_max_new_tickers: Annotated[int, Field(ge=0, le=50)] = Field(
        default=25,
        description=(
            "Max discoveries (names in no D51 tier) the Scout may accept per run (D28, "
            "D51: 25); extra ones are rejected as over_new_ticker_cap."
        ),
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
        "ingest_rss_feeds",
        "ingest_youtube_channels",
        "approver_slack_user_ids",
        mode="before",
    )
    @classmethod
    def _parse_str_list(cls, v: object) -> object:
        """Accept a comma-separated string from env vars."""
        if isinstance(v, str):
            return [s.strip() for s in v.split(",") if s.strip()]
        return v

    @field_validator("structure_whitelist", mode="before")
    @classmethod
    def _parse_whitelist(cls, v: object) -> object:
        """Comma-separated or JSON list; expand the deprecated ``vertical`` alias."""
        if isinstance(v, str):
            s = v.strip()
            v = json.loads(s) if s.startswith("[") else [p.strip() for p in s.split(",")]
        if not isinstance(v, list | tuple):
            return v
        out: list[object] = []
        for item in v:
            key = str(item).strip().lower() if isinstance(item, str) else item
            alias = LEGACY_WHITELIST_ALIASES.get(key) if isinstance(key, str) else None
            if alias is not None:
                log.warning("config.deprecated_whitelist_alias", alias=key, expands_to=alias)
                out.extend(k for k in alias if k not in out)
            elif key and key not in out:
                out.append(key)
        return out

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
                log.warning("per-env switch forced off in live (env var is paper-only)", key=name)
                setattr(self, name, False)
        return self


# Settings switches with one value per ARC_ENV (D34). The env var sets the paper
# value only; `arc.control` applies the store's `<field>.<env>` key for the running env.
PER_ENV_SWITCHES: frozenset[str] = frozenset({"auto_approve", "auto_exit_defined_risk"})


def get_settings(**overrides: object) -> ArcSettings:
    """Convenience factory — use in application code and tests."""
    return ArcSettings(**overrides)  # type: ignore[arg-type]
