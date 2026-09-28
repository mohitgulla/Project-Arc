"""Configuration via pydantic-settings: ARC_ENV, limits, universe.

See PLAN.md §5 for risk defaults and §9.2 (D9) for the default universe.
"""

from __future__ import annotations

import enum
from pathlib import Path
from typing import Annotated

import structlog
from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

log = structlog.get_logger()

# ---------------------------------------------------------------------------
# ARC_ENV enum
# ---------------------------------------------------------------------------

_LIVE_ENV_PATH = Path.home() / ".arc" / "live.env"


class ArcEnv(enum.StrEnum):
    """Execution environment: paper (default) or live."""

    PAPER = "paper"
    LIVE = "live"


# ---------------------------------------------------------------------------
# Structure whitelist
# ---------------------------------------------------------------------------


class StructureKind(enum.StrEnum):
    """Allowed option structure types (D4)."""

    VERTICAL = "vertical"
    IRON_CONDOR = "iron_condor"
    LONG_CALL = "long_call"
    LONG_PUT = "long_put"


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

# StockedUp (@StockedUp) posts a next-session market outlook almost every trading
# day. The channel id is used rather than the handle so a rename can't break it.
DEFAULT_YOUTUBE_CHANNELS: list[str] = [
    "https://www.youtube.com/channel/UC-m6zNItyoDk5lSykDlhE4Q/videos",
]


# ---------------------------------------------------------------------------
# Default universe (D9)
# ---------------------------------------------------------------------------

DEFAULT_UNIVERSE: list[str] = [
    "SPY",
    "QQQ",
    "IWM",
    "DIA",
    "XLF",
    "XLE",
    "XLK",
    "AAPL",
    "MSFT",
    "NVDA",
    "AMZN",
    "GOOGL",
    "META",
    "TSLA",
    "AMD",
    "JPM",
    "BAC",
    "XOM",
    "UNH",
    "HD",
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
    structure_whitelist: list[StructureKind] = Field(
        default=[
            StructureKind.VERTICAL,
            StructureKind.IRON_CONDOR,
            StructureKind.LONG_CALL,
            StructureKind.LONG_PUT,
        ],
        description="Allowed structure types (D4).",
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
    auto_exit_defined_risk: bool = Field(
        default=False,
        description=(
            "D24: when true, fired exits on defined-risk positions skip the Slack approval. "
            "Default false: every exit is a proposal that needs an approval."
        ),
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
        description="Auto-approve proposals in paper mode (D10). Ignored when env=live.",
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
    edgar_user_agent: str = Field(
        default="ProjectArc/0.1 (arc@example.com)",
        description="User-Agent header for SEC EDGAR requests (required by EDGAR).",
    )
    finnhub_api_key: str = Field(
        default="",
        description="Finnhub API key for earnings calendar (free tier).",
    )
    ingest_youtube_channels: list[str] = Field(
        default_factory=lambda: list(DEFAULT_YOUTUBE_CHANNELS),
        description=(
            "YouTube channel/playlist URLs for transcript ingestion. "
            "Default: StockedUp (daily next-session market outlook)."
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
        description="Max RawDocs summarised per Scout LLM call.",
    )
    scout_max_doc_chars: Annotated[int, Field(ge=200)] = Field(
        default=4000,
        description="Per-document text budget in the Scout prompt (truncated beyond).",
    )

    # -- Pipeline runner (E5.2) -----------------------------------------------
    persona_timeout_seconds: Annotated[int, Field(ge=10)] = Field(
        default=600,
        description="Timeout for a single Director/Quant/Risk LLM call.",
    )
    pipeline_max_shortlist: Annotated[int, Field(ge=1, le=20)] = Field(
        default=3,
        description="Max tickers the Director shortlist may carry into Quant.",
    )
    pipeline_scan_top: Annotated[int, Field(ge=1, le=20)] = Field(
        default=5,
        description="Scanner candidates per ticker offered to Quant (Quant picks among them).",
    )

    # -- Universe (D9) -------------------------------------------------------
    universe: list[str] = Field(
        default_factory=lambda: list(DEFAULT_UNIVERSE),
        description="Ticker universe for scanning.",
    )

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
    def _auto_approve_paper_only(self) -> ArcSettings:
        """auto_approve is forced off when env=live."""
        if self.env is ArcEnv.LIVE and self.auto_approve:
            log.warning("auto_approve forced off in live mode")
            self.auto_approve = False
        return self


def get_settings(**overrides: object) -> ArcSettings:
    """Convenience factory — use in application code and tests."""
    return ArcSettings(**overrides)  # type: ignore[arg-type]
