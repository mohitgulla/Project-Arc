"""``config/universe.yaml``: core tier, tier layout (D51), symbol master, liquidity screen
and extraction knobs (D28)."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from arc.config import UniverseMode

__all__ = [
    "DEFAULT_UNIVERSE_CONFIG",
    "REPO_ROOT",
    "EarningsConfig",
    "ExtractionConfig",
    "LiquidityThresholds",
    "SymbolMasterConfig",
    "TiersConfig",
    "UniverseConfig",
    "UniverseMode",
    "load_universe_config",
]

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_UNIVERSE_CONFIG = REPO_ROOT / "config" / "universe.yaml"

_FORBID = ConfigDict(extra="forbid", frozen=True)


class SymbolMasterConfig(BaseModel):
    model_config = _FORBID

    cache: Path = Path("data/symbol_master.json")
    refresh_days: int = Field(7, ge=1)
    sec_url: str = "https://www.sec.gov/files/company_tickers_exchange.json"
    listed_exchanges: list[str] = Field(default_factory=lambda: ["Nasdaq", "NYSE", "CBOE"])

    def cache_path(self) -> Path:
        """The cache path, resolved against the repo root when relative."""
        return self.cache if self.cache.is_absolute() else REPO_ROOT / self.cache


class LiquidityThresholds(BaseModel):
    """Thresholds of the deterministic liquidity screen (all must pass)."""

    model_config = _FORBID

    min_price: float = Field(10.0, ge=0.0)
    min_adv_shares: float = Field(1_000_000, ge=0.0)
    adv_days: int = Field(20, ge=1)
    adv_feed: Literal["iex", "sip", "delayed_sip"] = "sip"
    min_atm_open_interest: int = Field(500, ge=0)
    atm_strikes: int = Field(3, ge=1, le=11, description="strikes nearest spot counted as near-ATM")
    max_atm_spread_pct: float = Field(0.10, ge=0.0)


class ExtractionConfig(BaseModel):
    model_config = _FORBID

    min_symbol_len: int = Field(2, ge=1)
    stop_words: list[str] = Field(default_factory=list)


class EarningsConfig(BaseModel):
    model_config = _FORBID

    scout: Literal["seed", "all"] = "seed"


class TiersConfig(BaseModel):
    """D51 tier layout. Sizes and the active cap are runtime tunables (ArcSettings
    ``universe_*``); this block documents the order and holds the market reference."""

    model_config = _FORBID

    order: list[Literal["core", "momentum", "trending", "discovery"]] = Field(
        default_factory=lambda: ["core", "momentum", "trending", "discovery"]
    )
    market_reference: list[str] = Field(default_factory=lambda: ["SPY", "QQQ"])

    @field_validator("order")
    @classmethod
    def _fixed_order(cls, v: list[str]) -> list[str]:
        if v != ["core", "momentum", "trending", "discovery"]:
            msg = "tiers.order is fixed (D51): core, momentum, trending, discovery"
            raise ValueError(msg)
        return v


class UniverseConfig(BaseModel):
    model_config = _FORBID

    core: list[str] = Field(default_factory=list, max_length=30)
    tiers: TiersConfig = Field(default_factory=TiersConfig)
    symbol_master: SymbolMasterConfig = Field(default_factory=SymbolMasterConfig)
    liquidity_screen: LiquidityThresholds = Field(default_factory=LiquidityThresholds)
    extraction: ExtractionConfig = Field(default_factory=ExtractionConfig)
    earnings: EarningsConfig = Field(default_factory=EarningsConfig)


def load_universe_config(path: Path | str | None = None) -> UniverseConfig:
    """Load and validate ``config/universe.yaml`` (or *path*)."""
    p = Path(path) if path is not None else DEFAULT_UNIVERSE_CONFIG
    data = yaml.safe_load(p.read_text()) or {}
    return UniverseConfig.model_validate(data)
