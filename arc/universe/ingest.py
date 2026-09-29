"""Ingest-side view of the open universe (D28): which tickers a document mentions.

Every ingest connector (rss, youtube, edgar, earnings, channel briefs) uses
:class:`IngestUniverse` instead of reading ``settings.universe`` directly:

* seed tickers keep the pre-D28 match (case-insensitive whole word, optional ``$``);
* in ``seed`` mode, cashtags / ``(SYM)`` / exact upper-case symbols are also
  matched and validated against the symbol master
  (:func:`arc.universe.extract.extract_tickers`);
* in ``strict`` mode only the seed list matches, as before.

The result is only a *hint* (``raw_docs.tickers_hint``) for the Scout; the Scout's
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

    @classmethod
    def from_settings(
        cls,
        settings: ArcSettings,
        *,
        master: SymbolMaster | None = None,
        now: _dt.datetime | None = None,
    ) -> IngestUniverse:
        """Load once per connector run. Seed mode loads the symbol master (cache first)."""
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
        seed = tuple(dict.fromkeys(normalize_symbol(t) for t in settings.universe))
        return cls(mode=mode, seed=seed, config=cfg, master=master)

    @property
    def open(self) -> bool:
        return self.mode is UniverseMode.SEED and self.master is not None

    def accepted(self) -> frozenset[str]:
        if not self.open or self.master is None:
            return frozenset(self.seed)
        return frozenset(self.seed) | frozenset(self.master.symbols)

    def is_seed(self, symbol: str) -> bool:
        return normalize_symbol(symbol) in self.seed

    def known(self, symbol: str) -> bool:
        """Seed symbol, or (open universe) a symbol-master symbol."""
        sym = normalize_symbol(symbol)
        return sym in self.seed or (self.open and self.master is not None and sym in self.master)

    def tickers_in(self, text: str) -> list[str]:
        """Seed matches (pre-D28 rule) first, then validated open-universe matches."""
        upper = text.upper()
        found = [
            t
            for t in self.seed
            if re.search(rf"(?:^|[\s\[($])\$?({re.escape(t)})(?:[\s\]).,;:!?]|$)", upper)
        ]
        if self.open:
            for sym in extract_tickers(text, self.accepted(), self.config.extraction):
                if sym not in found:
                    found.append(sym)
        return found

    def mention_universe(self, text: str) -> list[str]:
        """Seed list plus the open-universe symbols found in *text* (channel briefs pass
        this as their ``universe``, so ``tickers_mentioned`` keeps its own match rules)."""
        extra = extract_tickers(text, self.accepted(), self.config.extraction) if self.open else []
        return list(dict.fromkeys([*self.seed, *extra]))

    def cik(self, symbol: str) -> str | None:
        """Zero-padded CIK from the symbol master, if known."""
        info = self.master.get(symbol) if self.master is not None else None
        return str(info.cik).zfill(10) if info is not None and info.cik is not None else None
