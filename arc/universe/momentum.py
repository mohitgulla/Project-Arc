"""Momentum tier source (D51, card E12.2): top S&P 500 Momentum names, monthly.

S&P does not publish the index constituents for free, so the holdings of Invesco
**SPMO** (which tracks the index, weighted by momentum score x cap) are the proxy:

* ``stockanalysis`` (primary): the holdings page embeds its top rows as SvelteKit
  data, ``holdings:[{no:1,n:"Micron Technology, Inc.",s:"$MU",as:"9.48%",…},…]``,
  plus ``lastUpdated:"Oct 2, 2026"``. The embedded array is parsed, never the
  rendered table.
* ``schwab`` (fallback): the first 20 rows are server-rendered (``<td … tsraw="MU">``
  cells, weight in ``tsraw="9.71"``) with ``gHoldingsAsOfDate = '10/01/2026'``.
  Used only when the primary fails or parses fewer than ``min_rows`` rows; the
  list is then marked ``partial``.

Selection (pure, :func:`select_members`): normalise symbols, collapse share classes
(``share_class_aliases``, GOOG -> GOOGL, weights summed), drop funds / cash lines /
non-optionable names (symbol master), rank by weight, keep the first ``size``.

No LLM. The network is reached only through :func:`fetch_source` (injected ``get``
in tests).
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import re
import sqlite3
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import structlog

from arc.universe.master import normalize_symbol

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping, Sequence

    from arc.universe.config import MomentumConfig
    from arc.universe.master import SymbolMaster
    from arc.universe.tiers import UniverseTierPayload

log = structlog.get_logger(__name__)

__all__ = [
    "SOURCES",
    "HoldingRow",
    "MomentumError",
    "MomentumFetch",
    "MomentumPick",
    "build_payload",
    "fetch_momentum",
    "is_stale",
    "notice_line",
    "parse_schwab",
    "parse_stockanalysis",
    "previous_members",
    "select_members",
    "tier_diff",
]

SOURCES = ("stockanalysis", "schwab")

# A plain listed ticker (BRK.B allowed); cash lines, CUSIPs and fund codes fail it.
_TICKER = re.compile(r"^[A-Z]{1,5}(\.[A-Z])?$")
# Holdings that are funds or cash, by name (money-market sweeps, other ETFs).
_FUND_NAME = re.compile(
    r"\b(ETF|Fund|Money Market|Treasury Portfolio|Government & Agency)\b|^Cash\b|^USD\b", re.I
)
# Money-market fund tickers end in XX (e.g. AGPXX).
_MONEY_MARKET = re.compile(r"^[A-Z]{3}XX$")


class MomentumError(RuntimeError):
    """Every configured source failed: nothing is written, the previous entry stays valid."""


@dataclass(frozen=True)
class HoldingRow:
    """One parsed holdings row, as the source listed it (rank 1 = largest weight)."""

    rank: int
    symbol: str
    name: str
    weight: float  # percent of the fund, e.g. 9.48


@dataclass(frozen=True)
class MomentumPick:
    """One selected member: the collapsed weight and the source rows it came from."""

    symbol: str
    name: str
    weight: float
    source_ranks: tuple[int, ...]
    merged: tuple[str, ...] = ()  # share classes folded into this name (e.g. GOOG)


@dataclass
class MomentumFetch:
    """What one run fetched and chose."""

    source: str
    url: str
    as_of: _dt.date | None
    rows: list[HoldingRow]
    picks: list[MomentumPick]
    dropped: list[tuple[str, str]]  # (symbol, reason)
    digest: str  # sha256 of the raw page bytes
    partial: bool
    size: int
    errors: dict[str, str] = field(default_factory=dict)  # sources tried before this one

    @property
    def tickers(self) -> list[str]:
        return [p.symbol for p in self.picks]


# ---------------------------------------------------------------------------
# Parsers (pure)
# ---------------------------------------------------------------------------

_SA_ROW = re.compile(
    r'\{no:(\d+),n:"((?:[^"\\]|\\.)*)",s:"\$?([^"]*)",as:"(-?[\d.]+)%"',
)
_SA_DATE = re.compile(r'(?:lastUpdated|Updated|date):"([A-Z][a-z]{2} \d{1,2}, \d{4})"')


def _sa_date(text: str) -> _dt.date | None:
    m = _SA_DATE.search(text)
    if not m:
        return None
    try:
        return _dt.datetime.strptime(m.group(1), "%b %d, %Y").date()  # noqa: DTZ007 - a date
    except ValueError:
        return None


def parse_stockanalysis(html: str) -> tuple[list[HoldingRow], _dt.date | None]:
    """Rows of the embedded ``holdings:[…]`` array and the page's as-of date."""
    start = html.find("holdings:[")
    if start < 0:
        return [], None
    end = html.find("}]", start)
    block = html[start : end + 2 if end >= 0 else len(html)]
    rows = [
        HoldingRow(
            rank=int(m.group(1)),
            symbol=m.group(3),
            name=m.group(2).replace('\\"', '"'),
            weight=float(m.group(4)),
        )
        for m in _SA_ROW.finditer(block)
    ]
    return rows, _sa_date(html[start:]) or _sa_date(html)


_SW_ROW = re.compile(
    r'<td class="symbol[^"]*" tsraw="([^"]*)">.*?'
    r'<td class="description[^"]*" tsraw="([^"]*)">.*?'
    r'<td class="data[^"]*" tsraw="(-?[\d.]+)">',
    re.S,
)
_SW_DATE = re.compile(r"gHoldingsAsOfDate\s*=\s*'(\d{2}/\d{2}/\d{4})'")


def parse_schwab(html: str) -> tuple[list[HoldingRow], _dt.date | None]:
    """Server-rendered holdings rows (first page) and ``gHoldingsAsOfDate``."""
    body = html
    t0 = html.find('id="tthHoldingsTbody"')
    if t0 >= 0:
        body = html[t0 : html.find("</tbody>", t0)]
    rows = [
        HoldingRow(rank=i, symbol=m.group(1), name=m.group(2), weight=float(m.group(3)))
        for i, m in enumerate(_SW_ROW.finditer(body), 1)
    ]
    as_of = None
    if m := _SW_DATE.search(html):
        try:
            as_of = _dt.datetime.strptime(m.group(1), "%m/%d/%Y").date()  # noqa: DTZ007 - a date
        except ValueError:
            as_of = None
    return rows, as_of


PARSERS: Mapping[str, Callable[[str], tuple[list[HoldingRow], _dt.date | None]]] = {
    "stockanalysis": parse_stockanalysis,
    "schwab": parse_schwab,
}


# ---------------------------------------------------------------------------
# Selection (pure)
# ---------------------------------------------------------------------------


def _fund_reason(
    symbol: str, name: str, master: SymbolMaster | None, etfs: frozenset[str]
) -> str | None:
    if symbol in etfs or _FUND_NAME.search(name) or _MONEY_MARKET.match(symbol):
        return "fund"
    info = master.get(symbol) if master is not None else None
    if info is not None and info.sources and "sec" not in info.sources:
        return "fund"  # Alpaca-only master row: an ETF/fund (no SEC company filing)
    return None


@dataclass
class _Acc:
    name: str
    weight: float
    ranks: list[int]
    merged: list[str] = field(default_factory=list)


def select_members(
    rows: Sequence[HoldingRow],
    *,
    size: int,
    aliases: Mapping[str, str],
    master: SymbolMaster | None = None,
    etfs: Iterable[str] = (),
) -> tuple[list[MomentumPick], list[tuple[str, str]]]:
    """Collapse share classes, drop funds / non-tickers / non-optionable, rank, cut.

    Deterministic: weights of collapsed classes are summed; ties break on the best
    source rank, then the symbol. Returns ``(picks, dropped)``; ``dropped`` lists
    every row not taken, with its reason (a collapsed share class is recorded in its
    pick's ``merged``, not dropped).
    """
    alias = {normalize_symbol(k): normalize_symbol(v) for k, v in aliases.items()}
    etf_set = frozenset(normalize_symbol(e) for e in etfs)
    acc: dict[str, _Acc] = {}
    dropped: list[tuple[str, str]] = []
    for row in sorted(rows, key=lambda r: r.rank):
        raw = normalize_symbol(row.symbol)
        if not _TICKER.match(raw):
            dropped.append((raw or row.name, "not_a_ticker"))
            continue
        sym = alias.get(raw, raw)
        if reason := _fund_reason(sym, row.name, master, etf_set):
            dropped.append((raw, reason))
            continue
        if master is not None and (why := master.not_optionable(sym)):
            dropped.append((raw, f"not_optionable: {why}"))
            continue
        cur = acc.get(sym)
        if cur is None:
            acc[sym] = _Acc(name=row.name, weight=row.weight, ranks=[row.rank])
            if raw != sym:
                acc[sym].merged.append(raw)
            continue
        cur.weight += row.weight
        cur.ranks.append(row.rank)
        if raw != sym:
            cur.merged.append(raw)
    ranked = sorted(acc.items(), key=lambda kv: (-round(kv[1].weight, 6), min(kv[1].ranks), kv[0]))
    picks: list[MomentumPick] = []
    for sym, a in ranked:
        if len(picks) >= size:
            dropped.append((sym, "over_size"))
            continue
        picks.append(
            MomentumPick(
                symbol=sym,
                name=a.name,
                weight=round(a.weight, 4),
                source_ranks=tuple(a.ranks),
                merged=tuple(a.merged),
            )
        )
    return picks, dropped


def tier_diff(previous: Sequence[str], current: Sequence[str]) -> tuple[list[str], list[str]]:
    """``(added, removed)`` between two member lists, each in its list's order."""
    prev, cur = set(previous), set(current)
    return [t for t in current if t not in prev], [t for t in previous if t not in cur]


def previous_members(conn: sqlite3.Connection) -> list[str] | None:
    """Tickers of the latest ``universe_tier`` momentum entry written (any status), or
    ``None`` when none was ever written (the diff baseline: last month's list)."""
    try:
        row = conn.execute(
            "SELECT payload FROM context_entries WHERE kind = 'universe_tier' "
            "AND subject = 'momentum' ORDER BY valid_from DESC, rowid DESC LIMIT 1"
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    if row is None:
        return None
    from arc.universe.tiers import UniverseTierPayload

    return [m.ticker for m in UniverseTierPayload.model_validate_json(row[0]).members]


def is_stale(as_of: _dt.date | None, today: _dt.date, stale_after_days: int) -> bool:
    """The source page's as-of date is older than *stale_after_days* (missing = stale)."""
    return as_of is None or (today - as_of).days > stale_after_days


def build_payload(fetch: MomentumFetch, *, now: _dt.datetime) -> UniverseTierPayload:
    """The ``universe_tier`` (subject ``momentum``) entry for *fetch*."""
    from arc.universe.tiers import Tier, TierMember, UniverseTierPayload

    as_of = fetch.as_of or now.date()
    members = [
        TierMember(
            ticker=p.symbol,
            tier=Tier.MOMENTUM,
            rank=i,
            source=fetch.source,
            reason=(
                f"SPMO weight {p.weight:.2f}% (row {', '.join(map(str, p.source_ranks))})"
                + (f", incl. {'+'.join(p.merged)}" if p.merged else "")
            ),
            as_of=as_of,
        )
        for i, p in enumerate(fetch.picks, 1)
    ]
    return UniverseTierPayload(
        tier=Tier.MOMENTUM,
        members=members,
        fetched_at=now,
        source=fetch.source,
        source_as_of=fetch.as_of,
        digest=fetch.digest,
        url=fetch.url,
        partial=fetch.partial,
    )


def notice_line(
    fetch: MomentumFetch,
    previous: Sequence[str] | None,
    *,
    stale: bool,
    primary: str,
) -> str:
    """``Momentum tier: +LITE +GS −NEM · 25 names · as of Oct 2`` (plus flags)."""
    if previous is None:
        change = "first list"
    else:
        added, removed = tier_diff(previous, fetch.tickers)
        change = " ".join([*(f"+{t}" for t in added), *(f"\u2212{t}" for t in removed)])
        change = change or "no change"
    as_of = f"as of {fetch.as_of:%b} {fetch.as_of.day}" if fetch.as_of else "as of unknown"
    parts = [f"Momentum tier: {change}", f"{len(fetch.picks)} names", as_of]
    if fetch.source != primary:
        parts.append(f"fallback {fetch.source}")
    if fetch.partial:
        parts.append(f"partial ({len(fetch.rows)} rows listed)")
    if stale:
        parts.append("STALE source")
    return " · ".join(parts)


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------


def _default_get(cfg: MomentumConfig, user_agent: str) -> Callable[[str], bytes]:
    from arc.ingest.options_data import http_get

    def get(url: str) -> bytes:
        return http_get(url, user_agent, timeout=cfg.timeout_s, retries=cfg.retries)

    return get


def fetch_momentum(
    cfg: MomentumConfig,
    *,
    source_order: Sequence[str],
    size: int,
    user_agent: str,
    master: SymbolMaster | None = None,
    etfs: Iterable[str] = (),
    get: Callable[[str], bytes] | None = None,
) -> MomentumFetch:
    """Try each source in order; the first with at least ``min_rows`` parsed rows wins.

    A list with fewer than *size* names (a short page, or names collapsed/dropped) is
    marked ``partial``. Raises :class:`MomentumError` when every source fails (fetch
    error, parse error, or too few rows); the caller writes nothing.
    """
    fetch = get or _default_get(cfg, user_agent)
    etfs = tuple(etfs)
    errors: dict[str, str] = {}
    for source in source_order:
        if source not in PARSERS:
            errors[source] = "unknown source"
            continue
        url = cfg.urls[source]  # type: ignore[index]
        try:
            raw = fetch(url)
            rows, as_of = PARSERS[source](raw.decode("utf-8", "replace"))
        except Exception as exc:  # noqa: BLE001 - try the next source; reported if all fail
            errors[source] = f"{type(exc).__name__}: {exc}"[:300]
            log.warning("universe.momentum.source_failed", source=source, error=errors[source])
            continue
        if len(rows) < cfg.min_rows:
            errors[source] = f"parsed {len(rows)} rows (< {cfg.min_rows})"
            log.warning("universe.momentum.source_short", source=source, rows=len(rows))
            continue
        picks, dropped = select_members(
            rows, size=size, aliases=cfg.share_class_aliases, master=master, etfs=etfs
        )
        return MomentumFetch(
            source=source,
            url=url,
            as_of=as_of,
            rows=rows,
            picks=picks,
            dropped=dropped,
            digest=hashlib.sha256(raw).hexdigest(),
            partial=len(picks) < size,
            size=size,
            errors=errors,
        )
    detail = "; ".join(f"{s}: {e}" for s, e in errors.items()) or "no sources configured"
    msg = f"every momentum source failed ({detail})"
    raise MomentumError(msg)
