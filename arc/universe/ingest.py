"""Ingest-side view of the open universe (D28): which tickers a document mentions.

Every ingest connector (rss, youtube, edgar, earnings, channel briefs) uses
:class:`IngestUniverse` instead of reading ``settings.universe`` directly. Its seed
list is today's D51 active list (:func:`arc.universe.tiers.active_tickers`):

* seed tickers keep the pre-D28 match (case-insensitive whole word, optional ``$``);
* in ``seed`` mode, cashtags / ``(SYM)`` / exact upper-case symbols are also
  matched and validated against the symbol master
  (:func:`arc.universe.extract.extract_tickers`);
* in ``strict`` mode only the seed list matches, as before.

The result is only a *hint* (``raw_docs.tickers_hint``) for the Sweep; the Sweep's
own :class:`~arc.universe.guard.UniverseGuard` decides what becomes a candidate.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from arc.config import UniverseMode
from arc.universe.config import load_universe_config
from arc.universe.extract import extract_tickers
from arc.universe.master import load_symbol_master, normalize_symbol

if TYPE_CHECKING:
    import datetime as _dt
    import sqlite3

    from arc.config import ArcSettings
    from arc.universe.config import UniverseConfig
    from arc.universe.master import SymbolMaster

__all__ = ["IngestUniverse"]


@dataclass(frozen=True)
class IngestUniverse:
    mode: UniverseMode
    seed: tuple[str, ...]
    config: UniverseConfig
    master: SymbolMaster | None
    #: D51 market reference (SPY, QQQ): tagged as mentions (regime/macro context),
    #: never a trade name, so not in :attr:`seed`.
    reference: tuple[str, ...] = ()
    #: E12.3: core + momentum names. In the open universe only these (and the
    #: reference) keep the legacy case-insensitive seed match, and only they may match
    #: as a bare word shorter than ``extraction.bare_min_len``. Other active names
    #: (trending, discoveries) go through :func:`extract_tickers`' rules, so a short
    #: trending name such as NOW cannot match every "now" and feed its own news score.
    #: ``None`` = every seed name (pre-E12.3 behaviour, used by direct constructors).
    bare_allow: tuple[str, ...] | None = None

    @classmethod
    def from_settings(
        cls,
        settings: ArcSettings,
        *,
        master: SymbolMaster | None = None,
        now: _dt.datetime | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> IngestUniverse:
        """Load once per connector run. Seed mode loads the symbol master (cache first).

        D51: the seed tuple is today's active list read from *conn* (the core list
        without a store or before the first resolve of the day).
        """
        from arc.universe.tiers import SEED_TIERS, active_tickers, market_reference, tier_membership
        from arc.utils.calendar import now_et

        cfg = load_universe_config(settings.universe_config_file)
        mode = UniverseMode(settings.universe_mode)
        if master is None and mode is UniverseMode.SEED:
            # Never fetches: the weekly `symbols` job (or `arc universe refresh`) fills the
            # cache. No cache = seed list only (logged), i.e. the pre-D28 behaviour.
            master = load_symbol_master(
                cfg.symbol_master,
                user_agent=settings.edgar_user_agent,
                now=now,
                fetch_if_missing=False,
            )
        at = now or now_et()
        active = active_tickers(conn, settings, at)
        seed = tuple(dict.fromkeys(normalize_symbol(t) for t in active))
        reference = tuple(
            t
            for t in dict.fromkeys(normalize_symbol(t) for t in market_reference(settings))
            if t not in seed
        )
        bare_allow = tuple(
            normalize_symbol(t)
            for t, tier in tier_membership(conn, settings, at).items()
            if tier in SEED_TIERS
        )
        return cls(
            mode=mode,
            seed=seed,
            config=cfg,
            master=master,
            reference=reference,
            bare_allow=bare_allow,
        )

    @property
    def open(self) -> bool:
        return self.mode is UniverseMode.SEED and self.master is not None

    def _legacy_seed(self) -> tuple[str, ...]:
        """Names matched by the pre-D28 case-insensitive rule (see :attr:`bare_allow`)."""
        if not self.open or self.bare_allow is None:
            return self.seed
        allow = set(self.bare_allow)
        return tuple(t for t in self.seed if t in allow)

    def _allow(self) -> tuple[str, ...]:
        return (*(self.seed if self.bare_allow is None else self.bare_allow), *self.reference)

    def accepted(self) -> frozenset[str]:
        base = frozenset(self.seed) | frozenset(self.reference)
        if not self.open or self.master is None:
            return base
        return base | frozenset(self.master.symbols)

    def is_seed(self, symbol: str) -> bool:
        return normalize_symbol(symbol) in self.seed

    def known(self, symbol: str) -> bool:
        """Seed symbol, or (open universe) a symbol-master symbol."""
        sym = normalize_symbol(symbol)
        return sym in self.seed or (self.open and self.master is not None and sym in self.master)

    def _extract(self, text: str) -> list[str]:
        return extract_tickers(
            text, self.accepted(), self.config.extraction, bare_allow=self._allow()
        )

    def tickers_in(self, text: str) -> list[str]:
        """Legacy seed + market-reference matches (pre-D28 rule) first, then validated
        open-universe matches."""
        upper = text.upper()
        found = [
            t
            for t in (*self._legacy_seed(), *self.reference)
            if re.search(rf"(?:^|[\s\[($])\$?({re.escape(t)})(?:[\s\]).,;:!?]|$)", upper)
        ]
        if self.open:
            for sym in self._extract(text):
                if sym not in found:
                    found.append(sym)
        return found

    def mention_universe(self, text: str) -> list[str]:
        """Seed list, market reference and the open-universe symbols found in *text*
        (channel briefs pass this as their ``universe``, so ``tickers_mentioned`` keeps
        its own match rules). E12.3: in the open universe, active names outside core +
        momentum are listed only when :func:`extract_tickers` found them."""
        if not self.open:
            return list(dict.fromkeys([*self.seed, *self.reference]))
        return list(dict.fromkeys([*self._legacy_seed(), *self.reference, *self._extract(text)]))

    def cik(self, symbol: str) -> str | None:
        """Zero-padded CIK from the symbol master, if known."""
        info = self.master.get(symbol) if self.master is not None else None
        return str(info.cik).zfill(10) if info is not None and info.cik is not None else None
