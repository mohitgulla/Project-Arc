"""The Scalp's universe check (D28): seed list, symbol master, new-ticker cap, liquidity screen.

One :class:`UniverseGuard` per Scalp run. It answers two questions:

* :meth:`UniverseGuard.known` — may the ticker be named at all? ``strict``: it is
  in the active list. ``seed``: it is a seed (D51: core or momentum) or in the
  symbol master.
* :meth:`UniverseGuard.admit` — may this (already schema/source validated)
  candidate be written to context? Seed tickers (D51: core + momentum) always
  pass. A trending-tier name must be optionable and pass the screen profile of
  ``tiers.trending.screen``. Any other name (a discovery) must be optionable, fit
  under the per-run ``scalp_max_new_tickers`` cap, and pass the
  ``tiers.discovery.screen`` profile (E12.4: both default ``relaxed``). Screen
  results are cached per ticker and profile for the run, so one ticker is
  measured once however many batches name it.
* :meth:`UniverseGuard.skips_confidence_floor` — E12.4: core + momentum names are
  kept below ``scalp_min_confidence``.

D56 (E13.4, ``tiers.model: d56``): three tiers, each with its own screen and floor
(:meth:`UniverseGuard.floor_for`): core unscreened / 0.4, momentum ``standard`` /
0.5, discovery (the Scout's list) ``loose`` / 0.6. A name in no tier is never
admitted: ``not_in_tier``, kept only as a *mention* (:attr:`UniverseGuard.mentions`)
for the Scalp note/card. There is no new-ticker path and no backfill.

The guard never touches the gate: gate caps apply per underlying whatever the
universe (PLAN §5).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

import structlog

from arc.config import UniverseMode
from arc.universe.config import universe_config
from arc.universe.master import load_symbol_master, normalize_symbol
from arc.universe.screen import measure_liquidity, screen_liquidity
from arc.universe.tiers import SEED_TIERS, Tier, tier_floor, unscreened_tiers

if TYPE_CHECKING:
    import datetime as _dt
    import sqlite3
    from collections.abc import Callable

    from arc.config import ArcSettings
    from arc.data.base import MarketDataProvider
    from arc.universe.config import LiquidityThresholds, ScreenProfile, UniverseConfig
    from arc.universe.master import SymbolMaster
    from arc.universe.screen import ScreenResult

log = structlog.get_logger(__name__)

__all__ = [
    "REJECT_ILLIQUID",
    "REJECT_NEW_TICKER_CAP",
    "REJECT_NOT_IN_TIER",
    "REJECT_NOT_IN_UNIVERSE",
    "REJECT_UNKNOWN_SYMBOL",
    "UniverseGuard",
]

# Scalp rejection keys (stable; stored in scalp_batches.rejected, shown on the card).
REJECT_NOT_IN_UNIVERSE = "not_in_universe"  # strict mode: not in the seed list
REJECT_UNKNOWN_SYMBOL = "unknown_symbol"  # seed mode: not in the symbol master
REJECT_ILLIQUID = "illiquid"  # failed the liquidity screen (or no listed options)
REJECT_NEW_TICKER_CAP = "over_new_ticker_cap"  # more non-seed names than scalp_max_new_tickers
REJECT_NOT_IN_TIER = "not_in_tier"  # D56: in no tier -> a mention, never a candidate


@dataclass
class UniverseGuard:
    """Per-run universe policy. Build with :meth:`from_settings`."""

    mode: UniverseMode
    seed: frozenset[str]
    config: UniverseConfig
    master: SymbolMaster | None
    max_new: int
    today: _dt.date
    dte_window: tuple[int, int]
    market_factory: Callable[[], MarketDataProvider] | None = None
    adv_market_factory: Callable[[], MarketDataProvider] | None = None
    #: E12.4: ``ticker -> tier`` for core, momentum and trending (others are discoveries).
    #: D56: core, momentum and discovery (others are in no tier).
    tiers: dict[str, Tier] = field(default_factory=dict)
    #: ``config/universe.yaml`` ``tiers.model`` (E13.4).
    model: str = "d51"
    #: D56 per-tier confidence floors (``universe_floor_<tier>``).
    floors: dict[Tier, float] = field(default_factory=dict)
    admitted_new: list[str] = field(default_factory=list)
    admitted_trending: list[str] = field(default_factory=list)
    admitted_tier: list[str] = field(default_factory=list)  # D56 screened tier names
    mentions: list[str] = field(default_factory=list)  # D56 not_in_tier names, in order
    screens: dict[str, ScreenResult] = field(default_factory=dict)  # last screen per ticker
    _screen_cache: dict[tuple[str, str], ScreenResult] = field(default_factory=dict, repr=False)
    details: dict[str, str] = field(default_factory=dict)  # ticker -> why it was rejected
    _market: MarketDataProvider | None = field(default=None, repr=False)
    _adv_market: MarketDataProvider | None = field(default=None, repr=False)

    @classmethod
    def from_settings(
        cls,
        settings: ArcSettings,
        *,
        now: _dt.datetime,
        master: SymbolMaster | None = None,
        config: UniverseConfig | None = None,
        market_factory: Callable[[], MarketDataProvider] | None = None,
        adv_market_factory: Callable[[], MarketDataProvider] | None = None,
        load_master: bool = True,
        conn: sqlite3.Connection | None = None,
    ) -> UniverseGuard:
        """Guard for one run. In seed mode the symbol master is loaded unless given.

        D51: the seed set (admitted without the screen) is core ∪ the valid momentum
        tier read from *conn* (core only without a store). In strict mode the whole
        active list is the allow-list. E12.4: the trending feed from *conn* picks the
        ``trending`` screen profile; *config* defaults to the effective universe.yaml
        (D26 ``universe_screen_*`` overrides applied).
        """
        from arc.universe.tiers import active_tickers, tier_membership

        cfg = config or universe_config(settings)
        mode = UniverseMode(settings.universe_mode)
        if master is None and mode is UniverseMode.SEED and load_master:
            # Never fetches here (a Scalp run must not stall on the SEC file): the
            # weekly `symbols` job / `arc universe refresh` fills the cache. With no
            # cache, non-seed names fail closed as unknown_symbol.
            master = load_symbol_master(
                cfg.symbol_master,
                user_agent=settings.edgar_user_agent,
                now=now,
                fetch_if_missing=False,
            )
        tiers = tier_membership(conn, settings, now)
        model = cfg.tiers.model
        unscreened = unscreened_tiers(model)
        seed = [t for t, tier in tiers.items() if tier in unscreened]
        if mode is UniverseMode.STRICT:
            seed = [*seed, *active_tickers(conn, settings, now)]
        return cls(
            mode=mode,
            seed=frozenset(normalize_symbol(t) for t in seed),
            config=cfg,
            master=master,
            max_new=settings.scalp_max_new_tickers,
            today=now.date(),
            dte_window=settings.entry_dte_window,
            market_factory=market_factory,
            adv_market_factory=adv_market_factory,
            tiers={normalize_symbol(t): tier for t, tier in tiers.items()},
            model=model,
            floors={
                t: f
                for t in (Tier.CORE, Tier.MOMENTUM, Tier.DISCOVERY)
                if (f := tier_floor(settings, t)) is not None
            },
        )

    # -- membership ----------------------------------------------------------

    def accepted_symbols(self) -> frozenset[str]:
        """Every symbol that may be named (ingest ticker extraction validates against it)."""
        if self.mode is UniverseMode.STRICT or self.master is None:
            return self.seed
        return self.seed | frozenset(self.master.symbols)

    def known(self, ticker: str) -> str | None:
        """``None`` if *ticker* may be named, else the rejection key."""
        sym = normalize_symbol(ticker)
        if sym in self.seed:
            return None
        if self.mode is UniverseMode.STRICT:
            return REJECT_NOT_IN_UNIVERSE
        if self.master is None:
            self.details[sym] = "symbol master unavailable (fails closed for non-seed names)"
            return REJECT_UNKNOWN_SYMBOL
        if sym not in self.master:
            self.details[sym] = "not in the symbol master (SEC listed + Alpaca optionable)"
            return REJECT_UNKNOWN_SYMBOL
        if self.model == "d56" and sym not in self.tiers:
            return self._mention(sym)
        return None

    def _mention(self, sym: str) -> str:
        """D56: a name in no tier is only mentioned (Scout is the one way into discovery)."""
        self.details[sym] = "in no tier (core / momentum / discovery): mentioned, not admitted"
        if sym not in self.mentions:
            self.mentions.append(sym)
        return REJECT_NOT_IN_TIER

    def membership(self, ticker: str) -> Tier | None:
        """The ticker's highest tier, ``None`` when it is in none (D56 reading)."""
        return self.tiers.get(normalize_symbol(ticker))

    def tier_label(self, ticker: str) -> str:
        """Tier name for display: D56 ``none`` for a name in no tier; D51 discovery."""
        sym = normalize_symbol(ticker)
        if self.model == "d56":
            tier = self.membership(sym)
            return "none" if tier is None else tier.value
        return self.tier_of(sym).value

    def floor_for(self, ticker: str) -> float | None:
        """D56: the confidence floor of *ticker*'s tier; ``None`` for a name in no tier."""
        tier = self.membership(ticker)
        return None if tier is None else self.floors.get(tier)

    def tier_floors(self) -> dict[str, float]:
        """D56: ``ticker -> floor`` for every tier name (the scanner's per-name floor)."""
        return {t: self.floors[tier] for t, tier in self.tiers.items() if tier in self.floors}

    def is_seed(self, ticker: str) -> bool:
        return normalize_symbol(ticker) in self.seed

    def tier_of(self, ticker: str) -> Tier:
        """The ticker's highest tier (D51); a name in no tier is a discovery."""
        return self.tiers.get(normalize_symbol(ticker), Tier.DISCOVERY)

    def skips_confidence_floor(self, ticker: str) -> Tier | None:
        """E12.4: the tier (core / momentum) whose names are kept below
        ``scalp_min_confidence``, else ``None`` (the floor applies)."""
        tier = self.tier_of(ticker)
        return tier if tier in SEED_TIERS else None

    def floor_exempt(self) -> frozenset[str]:
        """E12.4: every core + momentum ticker (kept below ``scalp_min_confidence``)."""
        return frozenset(t for t, tier in self.tiers.items() if tier in SEED_TIERS)

    def screen_profile(self, ticker: str) -> ScreenProfile:
        """The screen profile for *ticker*'s tier (``tiers.<tier>.screen``; D56
        ``tiers.policy``). An unscreened D56 tier (core) reads as ``standard`` for a
        what-if screen; :meth:`admit` never screens it."""
        if self.model == "d56":
            tier = self.membership(ticker)
            name = self.config.tier_screen(tier.value) if tier is not None else "loose"
            return "standard" if name == "none" else cast("ScreenProfile", name)
        spec = (
            self.config.tiers.trending
            if self.tier_of(ticker) is Tier.TRENDING
            else self.config.tiers.discovery
        )
        return spec.screen

    # -- admission (cap + screen) -------------------------------------------

    def _markets(self) -> tuple[MarketDataProvider, MarketDataProvider]:
        if self._market is None:
            if self.market_factory is None:
                from arc.data.alpaca import AlpacaMarketData

                self._market = AlpacaMarketData()
            else:
                self._market = self.market_factory()
        if self._adv_market is None:
            if self.adv_market_factory is not None:
                self._adv_market = self.adv_market_factory()
            elif self.market_factory is not None:
                self._adv_market = self._market
            else:
                from arc.data.alpaca import AlpacaMarketData

                feed = self.config.liquidity_screen.adv_feed
                self._adv_market = AlpacaMarketData(data_feed=feed)
        return self._market, self._adv_market

    def screen(self, ticker: str, profile: ScreenProfile | None = None) -> ScreenResult:
        """Measure + screen *ticker* with *profile* (default: its tier's), cached for the run."""
        sym = normalize_symbol(ticker)
        prof: ScreenProfile = profile or self.screen_profile(sym)
        key = (sym, prof)
        if key not in self._screen_cache:
            thresholds: LiquidityThresholds = self.config.liquidity_screen.profile(prof)
            try:
                market, adv_market = self._markets()
            except Exception as exc:  # noqa: BLE001 - no data = fail closed
                from arc.universe.screen import LiquidityMetrics

                metrics = LiquidityMetrics(ticker=sym, as_of=self.today, error=str(exc)[:200])
            else:
                metrics = measure_liquidity(
                    market,
                    sym,
                    today=self.today,
                    dte_window=self.dte_window,
                    thresholds=thresholds,
                    adv_market=adv_market,
                )
            res = screen_liquidity(metrics, thresholds)
            self._screen_cache[key] = res
            log.info(
                "universe.screen",
                ticker=sym,
                profile=prof,
                tier=self.tier_label(sym),
                passed=res.passed,
                detail=res.detail(),
                price=metrics.price,
                adv=metrics.adv_shares,
                atm_oi=metrics.atm_open_interest,
                atm_spread=metrics.atm_spread_pct,
            )
        self.screens[sym] = self._screen_cache[key]
        return self._screen_cache[key]

    def admit(self, ticker: str) -> str | None:
        """``None`` = write it to context; else the rejection key (detail in :attr:`details`)."""
        sym = normalize_symbol(ticker)
        if sym in self.seed:
            return None
        if (reason := self.known(sym)) is not None:
            return reason
        if self.model == "d56":
            return self._admit_d56(sym)
        if sym in self.admitted_new or sym in self.admitted_trending:
            return None
        if self.master is not None and (why := self.master.not_optionable(sym)):
            self.details[sym] = why
            return REJECT_ILLIQUID
        if self.tier_of(sym) is Tier.TRENDING:  # a tier name: screened, never capped
            res = self.screen(sym)
            if not res.passed:
                self.details[sym] = f"{self.screen_profile(sym)} screen: {res.detail()}"
                return REJECT_ILLIQUID
            self.admitted_trending.append(sym)
            return None
        if len(self.admitted_new) >= self.max_new:
            self.details[sym] = f"cap {self.max_new} new tickers per run"
            return REJECT_NEW_TICKER_CAP
        res = self.screen(sym)
        if not res.passed:
            self.details[sym] = f"{self.screen_profile(sym)} screen: {res.detail()}"
            return REJECT_ILLIQUID
        self.admitted_new.append(sym)
        return None

    def _admit_d56(self, sym: str) -> str | None:
        """D56: a tier name passes its tier's screen; any other name is a mention."""
        tier = self.membership(sym)
        if tier is None:
            return self._mention(sym)
        if sym in self.admitted_tier:
            return None
        if self.master is not None and (why := self.master.not_optionable(sym)):
            self.details[sym] = why
            return REJECT_ILLIQUID
        profile = self.config.tier_screen(tier.value)
        if profile != "none":
            res = self.screen(sym, profile)
            if not res.passed:
                self.details[sym] = f"{tier.value} {profile} screen: {res.detail()}"
                return REJECT_ILLIQUID
        self.admitted_tier.append(sym)
        return None
