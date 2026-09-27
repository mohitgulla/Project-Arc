"""Configuration via pydantic-settings: ARC_ENV, limits, universe.

See PLAN.md §5 for risk defaults and §9.2 (D9) for the default universe.
"""

from __future__ import annotations

import enum
from pathlib import Path
from typing import Annotated

import structlog
from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

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
    auto_approve: bool = Field(
        default=False,
        description="Auto-approve proposals in paper mode (D10). Ignored when env=live.",
    )

    # -- Universe (D9) -------------------------------------------------------
    universe: list[str] = Field(
        default_factory=lambda: list(DEFAULT_UNIVERSE),
        description="Ticker universe for scanning.",
    )

    # -- Validators ----------------------------------------------------------

    @field_validator("universe", mode="before")
    @classmethod
    def _parse_universe(cls, v: object) -> object:
        """Accept a comma-separated string from env vars."""
        if isinstance(v, str):
            return [s.strip().upper() for s in v.split(",") if s.strip()]
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
