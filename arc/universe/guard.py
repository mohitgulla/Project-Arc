"""The Scout's universe check (D28): seed list, symbol master, new-ticker cap, liquidity screen.

One :class:`UniverseGuard` per Scout run. It answers two questions:

* :meth:`UniverseGuard.known` — may the ticker be named at all? ``strict``: it is
  in the active list. ``seed``: it is a seed (D51: core or momentum) or in the
  symbol master.
* :meth:`UniverseGuard.admit` — may this (already schema/confidence/source
  validated) candidate be written to context? Seed tickers always pass. A
  non-seed ticker must be optionable at the broker, fit under the per-run
  ``scout_max_new_tickers`` cap, and pass the deterministic liquidity screen.
  Screen results are cached per ticker for the run, so one ticker is measured
  once however many batches name it.

The guard never touches the gate: gate caps apply per underlying whatever the
universe (PLAN §5).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import structlog

from arc.config import UniverseMode
from arc.universe.config import load_universe_config
from arc.universe.master import load_symbol_master, normalize_symbol
from arc.universe.screen import measure_liquidity, screen_liquidity

if TYPE_CHECKING:
    import datetime as _dt
    import sqlite3
    from collections.abc import Callable

    from arc.config import ArcSettings
    from arc.data.base import MarketDataProvider
    from arc.universe.config import UniverseConfig
    from arc.universe.master import SymbolMaster
    from arc.universe.screen import ScreenResult

log = structlog.get_logger(__name__)

__all__ = [
    "REJECT_ILLIQUID",
    "REJECT_NEW_TICKER_CAP",
    "REJECT_NOT_IN_UNIVERSE",
    "REJECT_UNKNOWN_SYMBOL",
    "UniverseGuard",
]

# Scout rejection keys (stable; stored in scout_batches.rejected, shown on the card).
REJECT_NOT_IN_UNIVERSE = "not_in_universe"  # strict mode: not in the seed list
REJECT_UNKNOWN_SYMBOL = "unknown_symbol"  # seed mode: not in the symbol master
REJECT_ILLIQUID = "illiquid"  # failed the liquidity screen (or no listed options)
REJECT_NEW_TICKER_CAP = "over_new_ticker_cap"  # more non-seed names than scout_max_new_tickers


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
    admitted_new: list[str] = field(default_factory=list)
    screens: dict[str, ScreenResult] = field(default_factory=dict)
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
        active list is the allow-list.
        """
        from arc.universe.tiers import active_tickers, seed_tickers

        cfg = config or load_universe_config(settings.universe_config_file)
        mode = UniverseMode(settings.universe_mode)
        if master is None and mode is UniverseMode.SEED and load_master:
            # Never fetches here (a Scout run must not stall on the SEC file): the
            # weekly `symbols` job / `arc universe refresh` fills the cache. With no
            # cache, non-seed names fail closed as unknown_symbol.
            master = load_symbol_master(
                cfg.symbol_master,
                user_agent=settings.edgar_user_agent,
                now=now,
                fetch_if_missing=False,
            )
        seed = seed_tickers(conn, settings, now)
        if mode is UniverseMode.STRICT:
            seed = [*seed, *active_tickers(conn, settings, now)]
        return cls(
            mode=mode,
            seed=frozenset(normalize_symbol(t) for t in seed),
            config=cfg,
            master=master,
            max_new=settings.scout_max_new_tickers,
            today=now.date(),
            dte_window=settings.entry_dte_window,
            market_factory=market_factory,
            adv_market_factory=adv_market_factory,
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
        return None

    def is_seed(self, ticker: str) -> bool:
        return normalize_symbol(ticker) in self.seed

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

                self._adv_market = AlpacaMarketData(data_feed=self.config.liquidity_screen.adv_feed)
        return self._market, self._adv_market

    def screen(self, ticker: str) -> ScreenResult:
        """Measure + screen *ticker* (cached for the run)."""
        sym = normalize_symbol(ticker)
        if sym not in self.screens:
            thresholds = self.config.liquidity_screen
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
            self.screens[sym] = res
            log.info(
                "universe.screen",
                ticker=sym,
                passed=res.passed,
                detail=res.detail(),
                price=metrics.price,
                adv=metrics.adv_shares,
                atm_oi=metrics.atm_open_interest,
                atm_spread=metrics.atm_spread_pct,
            )
        return self.screens[sym]

    def admit(self, ticker: str) -> str | None:
        """``None`` = write it to context; else the rejection key (detail in :attr:`details`)."""
        sym = normalize_symbol(ticker)
        if sym in self.seed:
            return None
        if (reason := self.known(sym)) is not None:
            return reason
        if sym in self.admitted_new:
            return None
        if self.master is not None and (why := self.master.not_optionable(sym)):
            self.details[sym] = why
            return REJECT_ILLIQUID
        if len(self.admitted_new) >= self.max_new:
            self.details[sym] = f"cap {self.max_new} new tickers per run"
            return REJECT_NEW_TICKER_CAP
        res = self.screen(sym)
        if not res.passed:
            self.details[sym] = res.detail()
            return REJECT_ILLIQUID
        self.admitted_new.append(sym)
        return None
