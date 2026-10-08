"""``market_movers`` source (E14.3, D60): the tape's movers + most-actives, Scalp context.

Alpaca's free screener on the existing key:

* ``/v1beta1/screener/stocks/movers?top=N`` → ``gainers`` / ``losers`` (symbol, price,
  change, percent_change) and ``last_updated``.
* ``/v1beta1/screener/stocks/most-actives?top=N`` → ``most_actives`` (symbol, volume,
  trade_count). It carries no price, so one ``/v2/stocks/snapshots`` call prices those
  names (last trade; % vs the previous daily close).

Rows are dropped at write time (counted per reason in ``excluded``):

* ``price_below_min``: price under ``min_price`` ($3), and ``no_price`` when the
  snapshot did not price a most-active name (a penny stock never leaks in unpriced);
* ``warrant_unit_right``: dotted suffixes ``.WS`` / ``.WT`` / ``.W`` / ``.U`` / ``.R``
  / ``.RT``, and 5-letter Nasdaq symbols whose 5th letter is ``W`` / ``U`` / ``R``
  (Nasdaq's warrant / unit / rights codes; 4-letter names such as SNOW never match);
* ``leveraged``: the symbol master's name reads as a leveraged / inverse fund (the D58
  trending ranker's :func:`arc.universe.trending.leveraged`).

Context only (D60): the payload is never a ``candidate`` / ``universe_tier`` input and
never a discovery or trending input (D56). The Scalp shows a code-built "Tape movers"
block only with ``personas.scalp_movers_context`` on (:func:`movers_block`).
"""

from __future__ import annotations

import datetime as _dt
import re
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import structlog

from arc.context.kinds import MarketMoversPayload, MoverRow
from arc.universe.master import normalize_symbol
from arc.universe.trending import leveraged
from arc.utils.calendar import ET

log = structlog.get_logger(__name__)

__all__ = [
    "EXCLUDE_LEVERAGED",
    "EXCLUDE_NO_PRICE",
    "EXCLUDE_PRICE",
    "EXCLUDE_WARRANT",
    "MOST_ACTIVES_URL",
    "MOVERS_URL",
    "SNAPSHOTS_URL",
    "MoversFetchError",
    "build_payload",
    "fetch_market_movers",
    "is_warrant_unit_right",
    "movers_block",
]

MOVERS_URL = "https://data.alpaca.markets/v1beta1/screener/stocks/movers"
MOST_ACTIVES_URL = "https://data.alpaca.markets/v1beta1/screener/stocks/most-actives"
SNAPSHOTS_URL = "https://data.alpaca.markets/v2/stocks/snapshots"

EXCLUDE_PRICE = "price_below_min"
EXCLUDE_NO_PRICE = "no_price"
EXCLUDE_WARRANT = "warrant_unit_right"
EXCLUDE_LEVERAGED = "leveraged"

# Dotted class suffixes (NYSE style) for warrants / units / rights.
_DOTTED = re.compile(r"\.(WS|WT|W|U|UN|R|RT)(\.[A-Z])?$")
# Nasdaq 5th-letter codes: W warrant, U unit, R rights (4-letter names never match).
_NASDAQ_5TH = re.compile(r"^[A-Z]{4}[WUR]$")

# ``get(url, params, timeout_s) -> (status, headers, json body)`` (arc.ingest.ticker_news).
AlpacaGet = Callable[[str, Mapping[str, str], float], tuple[int, Mapping[str, str], Any]]


class MoversFetchError(RuntimeError):
    """A screener call failed; ``reason`` is ``forbidden`` | ``rate_limited`` | ``error``."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason


def is_warrant_unit_right(symbol: str) -> bool:
    """``True`` for a warrant / unit / rights symbol (``DAAQW``, ``ALISU``, ``PEW.WS``)."""
    sym = normalize_symbol(symbol)
    return bool(_DOTTED.search(sym) or _NASDAQ_5TH.match(sym))


def _float(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None  # NaN -> None


def _int(v: Any) -> int | None:
    f = _float(v)
    return int(f) if f is not None and f >= 0 else None


def _ts(raw: Any, fallback: _dt.datetime) -> str:
    """The screener's ``last_updated`` (RFC 3339, ns precision) as ISO ET seconds."""
    text = str(raw or "").strip()
    m = re.match(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(\.\d+)?(Z|[+-]\d{2}:\d{2})?$", text)
    if m:
        tz = m.group(3) or "Z"
        at = _dt.datetime.fromisoformat(m.group(1) + ("+00:00" if tz == "Z" else tz))
    else:
        at = fallback
    return at.astimezone(ET).replace(microsecond=0).isoformat()


def _call(
    get: AlpacaGet, url: str, params: Mapping[str, str], timeout_s: float
) -> Mapping[str, Any]:
    try:
        status, _headers, body = get(url, params, timeout_s)
    except Exception as exc:  # noqa: BLE001 - network errors become a typed failure
        raise MoversFetchError("error", f"{type(exc).__name__}: {str(exc)[:160]}") from exc
    if status in (401, 403):
        raise MoversFetchError("forbidden", f"HTTP {status} {url}")
    if status == 429:  # noqa: PLR2004
        raise MoversFetchError("rate_limited", f"HTTP 429 {url}")
    if status >= 400 or not isinstance(body, Mapping):  # noqa: PLR2004
        raise MoversFetchError("error", f"HTTP {status} {url}")
    return body


@dataclass
class _Raw:
    symbol: str
    price: float | None
    pct: float | None
    volume: int | None = None
    trade_count: int | None = None


def _movers_rows(items: Any) -> list[_Raw]:
    out: list[_Raw] = []
    for it in items if isinstance(items, list) else []:
        if not isinstance(it, Mapping) or not str(it.get("symbol") or "").strip():
            continue
        out.append(
            _Raw(
                symbol=normalize_symbol(str(it["symbol"])),
                price=_float(it.get("price")),
                pct=_float(it.get("percent_change")),
            )
        )
    return out


def _actives_rows(items: Any) -> list[_Raw]:
    out: list[_Raw] = []
    for it in items if isinstance(items, list) else []:
        if not isinstance(it, Mapping) or not str(it.get("symbol") or "").strip():
            continue
        out.append(
            _Raw(
                symbol=normalize_symbol(str(it["symbol"])),
                price=None,
                pct=None,
                volume=_int(it.get("volume")),
                trade_count=_int(it.get("trade_count")),
            )
        )
    return out


def _field(snap: Mapping[str, Any], part: str, key: str) -> float | None:
    sub = snap.get(part)
    return _float(sub.get(key)) if isinstance(sub, Mapping) else None


def price_snapshots(body: Mapping[str, Any]) -> dict[str, tuple[float | None, float | None]]:
    """``/v2/stocks/snapshots`` → symbol -> (last price, % vs the previous close)."""
    out: dict[str, tuple[float | None, float | None]] = {}
    for sym, snap in body.items():
        if not isinstance(snap, Mapping):
            continue
        price = _field(snap, "latestTrade", "p") or _field(snap, "dailyBar", "c")
        close = _field(snap, "prevDailyBar", "c")
        pct = round((price / close - 1) * 100, 2) if price and close else None
        out[normalize_symbol(str(sym))] = (price if price and price > 0 else None, pct)
    return out


def _exclusion(r: _Raw, *, min_price: float, names: Callable[[str], str | None]) -> str | None:
    if is_warrant_unit_right(r.symbol):
        return EXCLUDE_WARRANT
    name = names(r.symbol)
    if name and leveraged(name):
        return EXCLUDE_LEVERAGED
    if r.price is None:
        return EXCLUDE_NO_PRICE
    if r.price < min_price:
        return EXCLUDE_PRICE
    return None


def build_payload(
    movers: Mapping[str, Any],
    actives: Mapping[str, Any],
    prices: Mapping[str, tuple[float | None, float | None]],
    *,
    active: Iterable[str],
    names: Callable[[str], str | None],
    now: _dt.datetime,
    min_price: float = 3.0,
) -> MarketMoversPayload:
    """Parse both screener bodies, price the most-actives, drop excluded rows."""
    active_set = {normalize_symbol(t) for t in active}
    excluded: Counter[str] = Counter()

    def keep(rows: list[_Raw]) -> list[MoverRow]:
        out: list[MoverRow] = []
        seen: set[str] = set()
        for r in rows:
            if r.symbol in seen:
                continue
            seen.add(r.symbol)
            why = _exclusion(r, min_price=min_price, names=names)
            if why is not None:
                excluded[why] += 1
                continue
            out.append(
                MoverRow(
                    symbol=r.symbol,
                    price=r.price,
                    pct=r.pct,
                    volume=r.volume,
                    trade_count=r.trade_count,
                    in_active=r.symbol in active_set,
                )
            )
        return out[:50]

    act = _actives_rows(actives.get("most_actives"))
    for r in act:
        r.price, r.pct = prices.get(r.symbol, (None, None))
    fetched = now.astimezone(ET).replace(microsecond=0)
    return MarketMoversPayload(
        as_of=_ts(movers.get("last_updated") or actives.get("last_updated"), fetched),
        fetched_at=fetched.isoformat(),
        gainers=keep(_movers_rows(movers.get("gainers"))),
        losers=keep(_movers_rows(movers.get("losers"))),
        most_actives=keep(act),
        excluded=dict(sorted(excluded.items())),
    )


def fetch_market_movers(
    get: AlpacaGet,
    *,
    top: int,
    active: Sequence[str],
    names: Callable[[str], str | None],
    now: _dt.datetime,
    min_price: float = 3.0,
    timeout_s: float = 15.0,
) -> tuple[MarketMoversPayload, dict[str, Any]]:
    """Fetch movers + most-actives (+ one snapshot call) and build the payload.

    Either screener call failing raises :class:`MoversFetchError` (nothing is written).
    A failed snapshot call only leaves the most-actives unpriced (dropped ``no_price``)
    and is reported in the returned metrics.
    """
    params = {"top": str(top)}
    movers = _call(get, MOVERS_URL, params, timeout_s)
    actives = _call(get, MOST_ACTIVES_URL, params, timeout_s)
    metrics: dict[str, Any] = {}
    symbols = list(dict.fromkeys(r.symbol for r in _actives_rows(actives.get("most_actives"))))
    prices: dict[str, tuple[float | None, float | None]] = {}
    if symbols:
        try:
            snap = _call(get, SNAPSHOTS_URL, {"symbols": ",".join(symbols)}, timeout_s)
            prices = price_snapshots(snap)
        except MoversFetchError as exc:
            metrics["snapshot_error"] = f"{exc.reason}: {exc}"
            log.warning("market_movers.snapshot_failed", reason=exc.reason, error=str(exc))
    payload = build_payload(
        movers, actives, prices, active=active, names=names, now=now, min_price=min_price
    )
    metrics.update(
        {
            "raw_gainers": len(_movers_rows(movers.get("gainers"))),
            "raw_losers": len(_movers_rows(movers.get("losers"))),
            "raw_most_actives": len(symbols),
            "priced": sum(1 for p, _ in prices.values() if p is not None),
        }
    )
    return payload, metrics


# -- the Scalp prompt block ----------------------------------------------------------


def _age(as_of: str, now: _dt.datetime) -> _dt.timedelta | None:
    try:
        at = _dt.datetime.fromisoformat(as_of)
    except ValueError:
        return None
    at = at.replace(tzinfo=ET) if at.tzinfo is None else at
    return now - at


def _age_text(delta: _dt.timedelta | None) -> str:
    if delta is None:
        return "none stored"
    mins = int(delta.total_seconds() // 60)
    if mins < 0:
        return "in the future"
    return f"{mins // 60}h{mins % 60:02d}m" if mins >= 60 else f"{mins}m"  # noqa: PLR2004


def _fmt_pct(v: float | None) -> str:
    return "n/a" if v is None else f"{v:+.1f}%"


def _fmt_count(v: int | None) -> str:
    if v is None:
        return "n/a"
    if v >= 1_000_000:
        return f"{v / 1_000_000:.1f}M"
    if v >= 1_000:
        return f"{v / 1_000:.0f}k"
    return str(v)


def movers_block(
    payload: MarketMoversPayload | None,
    *,
    now: _dt.datetime,
    max_age: _dt.timedelta,
    active: Iterable[str],
    story_tickers: Iterable[str],
    max_lines: int = 10,
) -> str:
    """The Scalp's "Tape movers" lines (code-built, ``max_lines`` at most).

    Only rows on the active list or named by a story in this run are listed, in
    screener order (gainers, losers, most-actives), one line per symbol. No entry, or
    one older than *max_age* → ``Tape movers: no fresh info (age …)``.
    """
    age = _age(payload.as_of, now) if payload is not None else None
    if payload is None or age is None or age > max_age or age < -_dt.timedelta(minutes=5):
        return f"Tape movers: no fresh info (age {_age_text(age)})"
    active_set = {normalize_symbol(t) for t in active}
    named = {normalize_symbol(t) for t in story_tickers if t}
    wanted = active_set | named
    lines: dict[str, list[str]] = {}
    order: list[str] = []
    for label, rows in (
        ("gainer", payload.gainers),
        ("loser", payload.losers),
        ("most active", payload.most_actives),
    ):
        for rank, r in enumerate(rows, start=1):
            if r.symbol not in wanted:
                continue
            if r.symbol not in lines:
                order.append(r.symbol)
                price = f"${r.price:,.2f}" if r.price is not None else "$n/a"
                tag = "active list" if r.symbol in active_set else "named in a story"
                lines[r.symbol] = [f"{r.symbol} {_fmt_pct(r.pct)} {price} ({tag})"]
            part = f"{label} #{rank}"
            if label == "most active":
                part += f" ({_fmt_count(r.volume)} sh, {_fmt_count(r.trade_count)} trades)"
            lines[r.symbol].append(part)
    at = _dt.datetime.fromisoformat(payload.as_of).astimezone(ET).strftime("%H:%M ET")
    head = (
        f"As of {at}: {len(payload.gainers)} gainers, {len(payload.losers)} losers, "
        f"{len(payload.most_actives)} most-actives after the filters."
    )
    if not order:
        return f"{head}\nNo active-list or story names among them."
    body = [" · ".join(lines[s]) for s in order[: max(0, max_lines)]]
    return "\n".join([head, *body])
