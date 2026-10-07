"""``config/universe.yaml``: core tier, tier layout (D56), symbol master, liquidity screen
and extraction knobs (D28)."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import structlog
import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from arc.config import UniverseMode

if TYPE_CHECKING:
    from collections.abc import Mapping

    from arc.config import ArcSettings

log = structlog.get_logger()

__all__ = [
    "D56_ORDER",
    "DEFAULT_UNIVERSE_CONFIG",
    "REPO_ROOT",
    "SCREEN_PROFILES",
    "EarningsConfig",
    "ExtractionConfig",
    "LiquidityScreens",
    "LiquidityThresholds",
    "MomentumConfig",
    "PolicyTier",
    "ScreenProfile",
    "SymbolMasterConfig",
    "TierPolicy",
    "TierScreenName",
    "TiersConfig",
    "UniverseConfig",
    "UniverseMode",
    "load_universe_config",
    "universe_config",
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


#: D56 (E13.15): two profiles, ``standard`` (momentum) and ``loose`` (discovery, trending).
ScreenProfile = Literal["standard", "loose"]
SCREEN_PROFILES: tuple[ScreenProfile, ...] = ("standard", "loose")
#: A tier's screen may also be ``none`` (core: never screened).
TierScreenName = Literal["none", "standard", "loose"]
#: D58: trending is the 4th tier, after discovery.
D56_ORDER: tuple[str, ...] = ("core", "momentum", "discovery", "trending")
D56_MARKET_REFERENCE: tuple[str, ...] = ("SPY", "QQQ", "IWM")
PolicyTier = Literal["core", "momentum", "discovery", "trending"]
#: E13.15: profiles removed at the D56 cutover; a block for one still loads (ignored, logged).
RETIRED_SCREEN_PROFILES: tuple[str, ...] = ("strict", "relaxed")
#: E13.15: ``tiers:`` keys of the pre-D56 layouts, accepted (ignored, logged) so an old
#: local universe.yaml or a stored override still loads. (D58's trending screen lives
#: under ``tiers.policy.trending``, not a top-level ``tiers.trending`` block.)
RETIRED_TIER_KEYS: tuple[str, ...] = ("model", "trending", "discovery")


def _standard_default() -> LiquidityThresholds:
    """D56 standard screen (momentum tier)."""
    return LiquidityThresholds(
        min_price=7.5, min_adv_shares=750_000, min_atm_open_interest=250, max_atm_spread_pct=0.15
    )


def _loose_default() -> LiquidityThresholds:
    """D56 loose screen (discovery tier)."""
    return LiquidityThresholds(
        min_price=3.0, min_adv_shares=300_000, min_atm_open_interest=100, max_atm_spread_pct=0.25
    )


class LiquidityScreens(BaseModel):
    """Profile-keyed liquidity screen (D56): ``standard`` (momentum) and ``loose``
    (discovery). Core is never screened.

    E13.15: the pre-D56 ``strict`` / ``relaxed`` profiles (and a pre-D51 flat block of
    threshold keys) still load, ignored with a ``universe.screen_profile_retired`` log.
    """

    model_config = _FORBID

    standard: LiquidityThresholds = Field(default_factory=_standard_default)
    loose: LiquidityThresholds = Field(default_factory=_loose_default)

    @model_validator(mode="before")
    @classmethod
    def _retired_profiles(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        drop = [
            k for k in data if k in RETIRED_SCREEN_PROFILES or k in LiquidityThresholds.model_fields
        ]
        if not drop:
            return data
        log.warning("universe.screen_profile_retired", keys=sorted(drop))
        return {k: v for k, v in data.items() if k not in drop}

    def profile(self, name: ScreenProfile) -> LiquidityThresholds:
        if name not in SCREEN_PROFILES:
            msg = f"unknown liquidity screen profile {name!r} (one of {SCREEN_PROFILES})"
            raise ValueError(msg)
        th: LiquidityThresholds = getattr(self, name)
        return th

    @property
    def adv_feed(self) -> Literal["iex", "sip", "delayed_sip"]:
        """ADV bars feed (from ``standard``; one ADV market per run)."""
        return self.standard.adv_feed


class ExtractionConfig(BaseModel):
    model_config = _FORBID

    min_symbol_len: int = Field(2, ge=1)
    # E12.3: a bare upper-case word shorter than this counts only for core + momentum
    # names; cashtags and labelled forms ("(SYM)", "ticker symbol SYM") match any length.
    bare_min_len: int = Field(4, ge=1)
    stop_words: list[str] = Field(default_factory=list)
    # E12.3: also never matched as "(SYM)" / "ticker symbol SYM" ("(AI)", "(COLA)").
    # Cashtags always match.
    paren_stop_words: list[str] = Field(default_factory=list)


class EarningsConfig(BaseModel):
    model_config = _FORBID

    scalp: Literal["seed", "all"] = "seed"

    @model_validator(mode="before")
    @classmethod
    def _legacy_scout_key(cls, data: object) -> object:
        """``earnings.scout`` (D54) and ``earnings.sweep`` (D56) load as ``earnings.scalp``."""
        if isinstance(data, dict):
            for old_key in ("sweep", "scout"):
                if old_key not in data:
                    continue
                data = dict(data)
                old = data.pop(old_key)
                if "scalp" not in data:
                    data["scalp"] = old
                log.warning(
                    "universe.config_alias", old=f"earnings.{old_key}", new="earnings.scalp"
                )
        return data


class MomentumConfig(BaseModel):
    """E12.2 momentum tier source (S&P 500 Momentum via Invesco SPMO holdings).

    Which sources run, in what order, and how many names the tier writes are job
    options in ``config/routines.yaml`` (``universe.momentum``); this block holds the
    source URLs and the deterministic selection rules.
    """

    model_config = _FORBID

    urls: dict[Literal["stockanalysis", "schwab"], str] = Field(
        default_factory=lambda: {
            "stockanalysis": "https://stockanalysis.com/etf/spmo/holdings/",
            "schwab": (
                "https://www.schwab.wallst.com/schwab/Prospect/research/etfs/schwabETF/"
                "index.asp?type=holdings&symbol=SPMO"
            ),
        }
    )
    # Share classes collapsed to one name before ranking (the first-listed class keeps
    # its rank; later classes are dropped as duplicates).
    share_class_aliases: dict[str, str] = Field(default_factory=lambda: {"GOOG": "GOOGL"})
    # A source with fewer parsed rows than this falls through to the next source.
    min_rows: int = Field(20, ge=1)
    # The page's as-of date older than this -> keep the list, raise coverage:universe.momentum.
    stale_after_days: int = Field(40, ge=1)
    timeout_s: float = Field(20.0, gt=0)
    retries: int = Field(2, ge=0, le=5)


class TierPolicy(BaseModel):
    """D56 admission screen of one tier. The tier's Scalp confidence floor is the
    runtime tunable ``universe_floor_<tier>`` (ArcSettings), one source of truth."""

    model_config = _FORBID

    screen: TierScreenName


def _default_policy() -> dict[PolicyTier, TierPolicy]:
    """D56 (owner 2026-10-05): core none, momentum standard, discovery loose.
    D58: trending loose (default, owner to confirm)."""
    return {
        "core": TierPolicy(screen="none"),
        "momentum": TierPolicy(screen="standard"),
        "discovery": TierPolicy(screen="loose"),
        "trending": TierPolicy(screen="loose"),
    }


class TiersConfig(BaseModel):
    """Tier layout (D56 + D58): core / momentum / discovery / trending, a screen per
    tier (``policy``) and the market reference.

    Sizes, floors and the active cap are runtime tunables (ArcSettings ``universe_*``).
    E13.15: the D56 cutover removed the layout switch; a ``model:`` key (and the pre-D56
    ``trending:`` / ``discovery:`` screen blocks) still load and are ignored with a
    ``universe.tiers_key_retired`` log.
    """

    model_config = _FORBID

    order: list[Literal["core", "momentum", "discovery", "trending"]] = Field(default_factory=list)
    # None = SPY QQQ IWM (D56); see `reference()`.
    market_reference: list[str] | None = None
    policy: dict[PolicyTier, TierPolicy] = Field(default_factory=_default_policy)

    @model_validator(mode="before")
    @classmethod
    def _retired_keys(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        drop = {k: data[k] for k in RETIRED_TIER_KEYS if k in data}
        if not drop:
            return data
        log.warning("universe.tiers_key_retired", keys=sorted(drop))
        return {k: v for k, v in data.items() if k not in drop}

    def reference(self) -> list[str]:
        """Market reference symbols: the configured list, else SPY QQQ IWM."""
        if self.market_reference is not None:
            return list(self.market_reference)
        return list(D56_MARKET_REFERENCE)

    @field_validator("policy", mode="after")
    @classmethod
    def _policy_complete(cls, v: dict[PolicyTier, TierPolicy]) -> dict[PolicyTier, TierPolicy]:
        # a partial block (e.g. only `discovery:`) keeps the D56 defaults for the rest
        return {**_default_policy(), **v}

    @model_validator(mode="after")
    def _fixed_order(self) -> TiersConfig:
        want = list(D56_ORDER)
        if not self.order:
            object.__setattr__(self, "order", want)
        elif self.order != want:
            msg = f"tiers.order is fixed: {', '.join(want)}"
            raise ValueError(msg)
        return self


class UniverseConfig(BaseModel):
    model_config = _FORBID

    core: list[str] = Field(default_factory=list, max_length=25)  # D58 MAX_CORE
    tiers: TiersConfig = Field(default_factory=TiersConfig)
    momentum: MomentumConfig = Field(default_factory=MomentumConfig)
    symbol_master: SymbolMasterConfig = Field(default_factory=SymbolMasterConfig)
    liquidity_screen: LiquidityScreens = Field(default_factory=LiquidityScreens)
    extraction: ExtractionConfig = Field(default_factory=ExtractionConfig)
    earnings: EarningsConfig = Field(default_factory=EarningsConfig)

    def screen_for(self, tier: Literal["momentum", "discovery", "trending"]) -> LiquidityThresholds:
        """Thresholds of the profile ``tiers.policy`` sets for *tier*; a tier screened
        ``none`` (core) raises ``ValueError``."""
        name = self.tier_screen(tier)
        if name == "none":
            msg = f"tier {tier!r} is not screened"
            raise ValueError(msg)
        return self.liquidity_screen.profile(name)

    def tier_screen(self, tier: str) -> TierScreenName:
        """The screen profile name of *tier* (``none`` = no screen, or not a tier)."""
        pol = self.tiers.policy.get(tier)  # type: ignore[call-overload]
        return "none" if pol is None else pol.screen


def load_universe_config(
    path: Path | str | None = None, *, overrides: Mapping[tuple[str, ...], object] | None = None
) -> UniverseConfig:
    """Load and validate ``config/universe.yaml`` (or *path*).

    *overrides* (D26 control panel, ``path -> value`` from the file root, e.g.
    ``("liquidity_screen", "loose", "min_price")``) patch the YAML first.
    """
    from arc.utils.yamlpatch import apply_overrides

    p = Path(path) if path is not None else DEFAULT_UNIVERSE_CONFIG
    data = apply_overrides(yaml.safe_load(p.read_text()) or {}, overrides)
    return UniverseConfig.model_validate(data)


def universe_config(settings: ArcSettings) -> UniverseConfig:
    """The effective ``config/universe.yaml``: the file plus the D26 overrides on *settings*."""
    return load_universe_config(
        settings.universe_config_file, overrides=settings.yaml_overrides("universe") or None
    )
