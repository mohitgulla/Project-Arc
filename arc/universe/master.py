"""Local symbol master (D28): SEC listed tickers + Alpaca optionable US equities.

Sources (both free, D3):

* SEC ``company_tickers_exchange.json``: every SEC-registered company with its
  ticker and exchange. OTC / unlisted rows are left out (``listed_exchanges``).
* Alpaca ``/v2/assets?status=active&asset_class=us_equity&attributes=has_options``:
  active US equities with listed options, with their ``tradable`` flag. This also
  adds the ETFs (QQQ, XLE, ...) that the SEC company file does not list.

The merged master is cached as JSON (``config/universe.yaml`` ``symbol_master.cache``,
default ``data/symbol_master.json``) and refreshed weekly by the ``symbols``
source job (or ``arc universe refresh``). Loading never needs the network when a
cache exists: a cache older than ``refresh_days`` is still used and logged
``universe.symbol_master.stale`` (stale-file fallback). The ingest connectors and
the Scalp load with ``fetch_if_missing=False``: no cache means the seed list only
(non-seed names fail closed), never a network stall mid-run.

Symbols are normalised to the Alpaca/OCC form: ``BRK-B`` (SEC) → ``BRK.B``.
"""

from __future__ import annotations

import datetime as _dt  # noqa: TC003 - pydantic resolves the field types at runtime
import json
import os
from typing import TYPE_CHECKING, Any

import structlog
from pydantic import BaseModel, ConfigDict, Field

from arc.utils.calendar import now_et

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable
    from pathlib import Path

    from arc.universe.config import SymbolMasterConfig

log = structlog.get_logger(__name__)

__all__ = [
    "MASTER_VERSION",
    "SymbolInfo",
    "SymbolMaster",
    "fetch_alpaca_optionable",
    "fetch_sec_tickers",
    "load_symbol_master",
    "normalize_symbol",
    "refresh_symbol_master",
]

MASTER_VERSION = 1


def normalize_symbol(raw: str) -> str:
    """Upper-case, strip a ``$`` cashtag, use ``.`` for share classes (``BRK-B`` → ``BRK.B``)."""
    return raw.strip().lstrip("$").strip().upper().replace("-", ".").replace("/", ".")


class SymbolInfo(BaseModel):
    """One master row. ``None`` flags mean the source did not say (fail open to the screen)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    symbol: str
    name: str = ""
    exchange: str | None = None
    cik: int | None = None
    tradable: bool | None = None
    options: bool | None = None
    sources: list[str] = Field(default_factory=list)


class SymbolMaster(BaseModel):
    """The cached master: ``symbols`` keyed by normalised symbol."""

    model_config = ConfigDict(extra="forbid")

    version: int = MASTER_VERSION
    fetched_at: _dt.datetime
    sources: dict[str, int] = Field(default_factory=dict, description="rows per source")
    symbols: dict[str, SymbolInfo]

    def __contains__(self, symbol: object) -> bool:
        return isinstance(symbol, str) and normalize_symbol(symbol) in self.symbols

    def get(self, symbol: str) -> SymbolInfo | None:
        return self.symbols.get(normalize_symbol(symbol))

    def age_days(self, now: _dt.datetime) -> float:
        return (now - self.fetched_at).total_seconds() / 86_400

    def is_stale(self, now: _dt.datetime, refresh_days: int) -> bool:
        return self.age_days(now) > refresh_days

    def not_optionable(self, symbol: str) -> str | None:
        """Why Alpaca says *symbol* cannot be traded with options, else ``None``."""
        info = self.get(symbol)
        if info is None:
            return None
        if info.tradable is False:
            return "not tradable at the broker"
        if info.options is False:
            return "no listed options at the broker"
        return None


# ---------------------------------------------------------------------------
# Fetchers (network; injected in tests)
# ---------------------------------------------------------------------------


def fetch_sec_tickers(cfg: SymbolMasterConfig, user_agent: str) -> list[dict[str, Any]]:
    """SEC ``company_tickers_exchange.json`` rows as dicts (``cik, name, ticker, exchange``)."""
    import urllib.request

    req = urllib.request.Request(cfg.sec_url, headers={"User-Agent": user_agent})
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 - fixed https URL from config
        data = json.loads(resp.read())
    fields = data["fields"]
    return [dict(zip(fields, row, strict=False)) for row in data["data"]]


def fetch_alpaca_optionable() -> list[dict[str, Any]]:
    """Active US-equity assets with listed options, from the Alpaca **paper** trading API."""
    from alpaca.trading.client import TradingClient

    key, secret = os.environ.get("ALPACA_API_KEY", ""), os.environ.get("ALPACA_SECRET_KEY", "")
    if not key or not secret:
        msg = "ALPACA_API_KEY / ALPACA_SECRET_KEY not set"
        raise RuntimeError(msg)
    client = TradingClient(api_key=key, secret_key=secret, paper=True)
    raw = client.get(
        "/assets", {"status": "active", "asset_class": "us_equity", "attributes": "has_options"}
    )
    return list(raw) if isinstance(raw, list) else []


# ---------------------------------------------------------------------------
# Build / cache
# ---------------------------------------------------------------------------


def build_symbol_master(
    sec_rows: Iterable[dict[str, Any]],
    alpaca_rows: Iterable[dict[str, Any]] | None,
    cfg: SymbolMasterConfig,
    *,
    now: _dt.datetime,
) -> SymbolMaster:
    """Merge the two sources (pure). ``alpaca_rows=None`` = Alpaca unavailable (flags unknown)."""
    listed = set(cfg.listed_exchanges)
    rows: dict[str, dict[str, Any]] = {}
    n_sec = n_alp = 0
    for r in sec_rows:
        ticker = str(r.get("ticker") or "").strip()
        if not ticker or r.get("exchange") not in listed:
            continue
        sym = normalize_symbol(ticker)
        n_sec += 1
        rows.setdefault(
            sym,
            {
                "symbol": sym,
                "name": str(r.get("name") or ""),
                "exchange": r.get("exchange"),
                "cik": int(r["cik"]) if r.get("cik") is not None else None,
                "sources": ["sec"],
            },
        )
    if alpaca_rows is not None:
        optionable: dict[str, dict[str, Any]] = {}
        for a in alpaca_rows:
            sym = normalize_symbol(str(a.get("symbol") or ""))
            if not sym or str(a.get("exchange") or "").upper() == "OTC":
                continue
            optionable[sym] = a
        for sym, a in optionable.items():
            n_alp += 1
            row = rows.setdefault(
                sym,
                {
                    "symbol": sym,
                    "name": str(a.get("name") or ""),
                    "exchange": a.get("exchange"),
                    "sources": [],
                },
            )
            row["sources"] = [*row["sources"], "alpaca"]
            row["tradable"] = bool(a.get("tradable"))
            row["options"] = "has_options" in (a.get("attributes") or [])
        for sym, row in rows.items():
            if sym not in optionable:
                row["options"] = False  # Alpaca lists no options for it
    return SymbolMaster(
        fetched_at=now,
        sources={"sec": n_sec, "alpaca": n_alp},
        symbols={s: SymbolInfo.model_validate(r) for s, r in sorted(rows.items())},
    )


def _read_cache(path: Path) -> SymbolMaster | None:
    try:
        return SymbolMaster.model_validate_json(path.read_text())
    except (OSError, ValueError) as exc:
        log.warning("universe.symbol_master.cache_unreadable", path=str(path), error=str(exc))
        return None


def refresh_symbol_master(
    cfg: SymbolMasterConfig,
    *,
    user_agent: str,
    now: _dt.datetime | None = None,
    sec_fetcher: Callable[[], list[dict[str, Any]]] | None = None,
    alpaca_fetcher: Callable[[], list[dict[str, Any]]] | None = None,
) -> SymbolMaster:
    """Fetch both sources, merge, and atomically replace the cache. SEC failure raises.

    An Alpaca failure is logged and the master is built from the SEC file alone
    (broker flags unknown), so a broker outage cannot empty the universe.
    """
    now = now or now_et()
    sec = (sec_fetcher or (lambda: fetch_sec_tickers(cfg, user_agent)))()
    try:
        alpaca: list[dict[str, Any]] | None = (alpaca_fetcher or fetch_alpaca_optionable)()
    except Exception as exc:  # noqa: BLE001 - the SEC file alone is still a valid master
        log.warning("universe.symbol_master.alpaca_failed", error=str(exc)[:300])
        alpaca = None
    master = build_symbol_master(sec, alpaca, cfg, now=now)
    path = cfg.cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(master.model_dump_json())
    tmp.replace(path)
    log.info(
        "universe.symbol_master.refreshed",
        path=str(path),
        symbols=len(master.symbols),
        sources=master.sources,
    )
    return master


def load_symbol_master(
    cfg: SymbolMasterConfig,
    *,
    user_agent: str,
    now: _dt.datetime | None = None,
    fetch_if_missing: bool = True,
    refresher: Callable[[], SymbolMaster] | None = None,
) -> SymbolMaster | None:
    """The cached master; a stale cache is still returned (logged). ``None`` = unavailable.

    With no readable cache and ``fetch_if_missing``, fetches once (``refresher``
    overrides the fetch, for tests). Callers load once per run and pass it down.
    """
    now = now or now_et()
    path = cfg.cache_path()
    if path.is_file():
        master = _read_cache(path)
        if master is not None:
            if master.is_stale(now, cfg.refresh_days):
                log.warning(
                    "universe.symbol_master.stale",
                    path=str(path),
                    age_days=round(master.age_days(now), 1),
                    refresh_days=cfg.refresh_days,
                )
            return master
    if not fetch_if_missing:
        log.warning(
            "universe.symbol_master.missing",
            path=str(path),
            hint="run `arc universe refresh` (the weekly `symbols` job does this)",
        )
        return None
    try:
        return (refresher or (lambda: refresh_symbol_master(cfg, user_agent=user_agent, now=now)))()
    except Exception as exc:  # noqa: BLE001 - callers fail closed on None
        log.warning("universe.symbol_master.unavailable", path=str(path), error=str(exc)[:300])
        return None
