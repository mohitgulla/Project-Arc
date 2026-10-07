"""``retail_buzz`` source (E13.19, D58): Reddit (ApeWisdom) + Stocktwits, once a day.

Fetches every enabled input of the ``retail_buzz`` job (``config/routines.yaml``
``sources.retail_buzz.inputs``) with its own timeout and retries, parses each into raw
rows (symbol, position, the input's own numbers) and returns one
:class:`~arc.context.kinds.RetailBuzzPayload`. Nothing is scored here: the trending
ranker (:mod:`arc.universe.trending`) scores the rows; the Scout reads them as context.

Each input is independent: one failing (network error, bad JSON, no rows) is recorded
``failed`` with its error and contributes nothing; the others still count.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any

import structlog

from arc.context.kinds import RetailBuzzInput, RetailBuzzPayload, RetailBuzzRow
from arc.universe.master import normalize_symbol

if TYPE_CHECKING:
    import datetime as _dt
    from collections.abc import Callable, Mapping

    from arc.ingest.retail_buzz_config import RetailBuzzConfig, RetailBuzzInputSpec

log = structlog.get_logger(__name__)

__all__ = [
    "fetch_retail_buzz",
    "http_getter",
    "parse_apewisdom",
    "parse_stocktwits",
]

# A getter is ``get(url, timeout_s, retries) -> bytes`` and raises on failure.


def http_getter(user_agent: str) -> Callable[[str, float, int], bytes]:
    """HTTP GET (connection errors / timeouts / 5xx retried, never 4xx)."""
    from arc.ingest.options_data import http_get

    def get(url: str, timeout_s: float, retries: int) -> bytes:
        return http_get(url, user_agent, timeout=timeout_s, retries=retries)

    return get


def _int(v: Any) -> int | None:
    try:
        return int(v) if v is not None and v != "" else None
    except (TypeError, ValueError):
        return None


def _float(v: Any) -> float | None:
    try:
        return float(v) if v is not None and v != "" else None
    except (TypeError, ValueError):
        return None


def parse_apewisdom(pages: list[Any]) -> list[RetailBuzzRow]:
    """ApeWisdom pages (``{"results": [...]}``) -> rows, first listing of a symbol wins."""
    out: list[RetailBuzzRow] = []
    seen: set[str] = set()
    for it in (it for p in pages for it in p["results"]):
        sym = normalize_symbol(str(it.get("ticker") or ""))
        if not sym or sym in seen:
            continue
        seen.add(sym)
        out.append(
            RetailBuzzRow(
                symbol=sym,
                position=len(out) + 1,
                rank=_int(it.get("rank")),
                name=str(it.get("name") or "")[:120],
                mentions=_float(it.get("mentions")),
                rank_24h_ago=_int(it.get("rank_24h_ago")),
            )
        )
    return out


def parse_stocktwits(pages: list[Any]) -> list[RetailBuzzRow]:
    """Stocktwits trending pages (``{"symbols": [...]}``) -> rows (crypto kept here,
    dropped by the ranker), first listing of a symbol wins."""
    out: list[RetailBuzzRow] = []
    seen: set[str] = set()
    for it in (it for p in pages for it in p["symbols"]):
        sym = normalize_symbol(str(it.get("symbol") or ""))
        if not sym or sym in seen:
            continue
        seen.add(sym)
        out.append(
            RetailBuzzRow(
                symbol=sym,
                position=len(out) + 1,
                rank=_int(it.get("rank")),
                name=str(it.get("title") or "")[:120],
                trending_score=_float(it.get("trending_score")),
                exchange=(str(it["exchange"]).upper() if it.get("exchange") else None),
                region=(str(it["region"]).upper() if it.get("region") else None),
            )
        )
    return out


_PARSERS: Mapping[str, Callable[[list[Any]], list[RetailBuzzRow]]] = {
    "apewisdom": parse_apewisdom,
    "stocktwits": parse_stocktwits,
}


def _fetch_one(
    name: str,
    spec: RetailBuzzInputSpec,
    *,
    get: Callable[[str, float, int], bytes],
    now: _dt.datetime,
) -> RetailBuzzInput:
    fetched_at = now.isoformat()
    try:
        blobs = [get(u, spec.timeout_s, spec.retries) for u in spec.urls]
        digest = hashlib.sha256(b"\n".join(blobs)).hexdigest()
        rows = _PARSERS[spec.type]([json.loads(b.decode("utf-8")) for b in blobs])
    except Exception as exc:  # noqa: BLE001 - one input failing contributes nothing
        err = f"{type(exc).__name__}: {exc}"[:300]
        log.warning("retail_buzz.input_failed", input=name, type=spec.type, error=err)
        return RetailBuzzInput(
            type=spec.type,
            label=spec.label,
            status="failed",
            urls=list(spec.urls),
            fetched_at=fetched_at,
            error=err,
        )
    status = "ok" if rows else "failed"
    log.info("retail_buzz.input", input=name, type=spec.type, status=status, rows=len(rows))
    return RetailBuzzInput(
        type=spec.type,
        label=spec.label,
        status=status,
        urls=list(spec.urls),
        fetched_at=fetched_at,
        digest=digest,
        error=None if rows else "no rows",
        rows=rows,
    )


def fetch_retail_buzz(
    cfg: RetailBuzzConfig,
    *,
    now: _dt.datetime,
    get: Callable[[str, float, int], bytes],
) -> RetailBuzzPayload:
    """Fetch every enabled input (each independent) into one payload."""
    from arc.utils.calendar import ET

    local = now.astimezone(ET)
    return RetailBuzzPayload(
        as_of=local.isoformat(),
        session=local.date().isoformat(),
        inputs={n: _fetch_one(n, s, get=get, now=local) for n, s in cfg.enabled.items()},
    )
