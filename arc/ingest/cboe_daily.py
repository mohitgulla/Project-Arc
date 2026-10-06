"""options_slow source (E13.5, D56): Cboe daily options statistics + CFE VX settlements.

Free Cboe endpoints only (D3), no key. Both payloads are typed context entries
(``options_daily`` / ``vx_curve``, subject ``market``), never raw docs and never gate
inputs. Parsers are pure and unit-tested on recorded payloads
(``arc/ingest/fixtures/cboe/``); fetchers take an injectable ``get`` like
:mod:`arc.ingest.options_data`.

* :func:`fetch_daily_options` — ``cdn.cboe.com`` ``<YYYY-MM-DD>_daily_options`` JSON:
  put/call ratios for six segments (total / index / ETP / equity / VIX / SPX+SPXW)
  with each segment's call/put volume, and open interest per product group.
* :func:`fetch_vx_settlements` — ``cboe.com`` CFE settlement CSV for one date: the VX
  futures term structure (monthlies + weeklies) and its front-month slope/shape.

A day that is not published yet (the CDN answers 403/404, or the CSV is a bare
header) raises :class:`NotPublishedError`; the routine handler decides whether that
is a skip (evening slot) or a failure (morning catch-up). Measured publish times
are in docs/OPS.md §5.28.
"""

from __future__ import annotations

import csv
import datetime as _dt
import io
import json
import re
from typing import TYPE_CHECKING, Any, NamedTuple

import requests
import structlog

from arc.context.kinds import (
    OptionsDailyPayload,
    PcRatio,
    PcSegment,
    ProductOi,
    VxCurvePayload,
    VxPoint,
)
from arc.ingest.options_data import BROWSER_UA, CBOE_DAILY_URL, http_get
from arc.utils.calendar import EOD_STATS_CUTOFF, ET, completed_session

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

log = structlog.get_logger(__name__)

__all__ = [
    "CBOE_DAILY_URL",
    "SESSION_CUTOFF",
    "VX_SETTLEMENT_URL",
    "NotPublishedError",
    "VxShape",
    "curve_shape",
    "daily_segments",
    "fetch_daily_options",
    "fetch_vx_settlements",
    "parse_daily_options",
    "parse_vx_settlements",
    "session_date",
    "vx_curve",
]

VX_SETTLEMENT_URL = "https://www.cboe.com/us/futures/market_statistics/settlement/csv?dt={day}"

#: A run at or after this ET time reads today's session; earlier runs read the
#: previous session (the statistics only exist after the close).
SESSION_CUTOFF = EOD_STATS_CUTOFF
#: CDN answers for a day that has no file yet (S3 says 403 for a missing key).
_NOT_PUBLISHED_STATUS = frozenset({403, 404})

# Ratio name in the JSON -> segment.
_RATIO_NAMES: Mapping[str, PcSegment] = {
    "TOTAL PUT/CALL RATIO": "total",
    "INDEX PUT/CALL RATIO": "index",
    "EXCHANGE TRADED PRODUCTS PUT/CALL RATIO": "etp",
    "EQUITY PUT/CALL RATIO": "equity",
    "CBOE VOLATILITY INDEX (VIX) PUT/CALL RATIO": "vix",
    "SPX + SPXW PUT/CALL RATIO": "spx",
}
# Product section in the JSON -> segment / OI product (``all`` = every product).
_SECTIONS: Mapping[str, str] = {
    "SUM OF ALL PRODUCTS": "all",
    "INDEX OPTIONS": "index",
    "EXCHANGE TRADED PRODUCTS": "etp",
    "EQUITY OPTIONS": "equity",
    "CBOE VOLATILITY INDEX (VIX)": "vix",
    "SPX + SPXW": "spx",
}
_SEGMENT_ORDER: tuple[str, ...] = ("total", "index", "etp", "equity", "vix", "spx")
_OI_ORDER: tuple[str, ...] = ("all", "index", "etp", "equity", "vix", "spx")

# CFE VX symbols: ``VX/V6`` is the monthly (October 2026), ``VX40/V6`` a weekly.
_VX_SYMBOL = re.compile(r"^VX(?P<week>\d{1,2})?/(?P<month>[FGHJKMNQUVXZ])(?P<year>\d{1,2})$")


class NotPublishedError(LookupError):
    """Cboe has not published the data for the requested session (yet)."""


def session_date(now: _dt.datetime) -> _dt.date:
    """The session whose statistics a run at *now* reads.

    Today (ET) when *now* is at/after :data:`SESSION_CUTOFF` (16:30) on a trading
    session, else the previous trading session (holiday-aware).
    """
    return completed_session(now, SESSION_CUTOFF)


def _get_or_unpublished(url: str, day: _dt.date, get: Callable[[str, str], bytes] | None) -> bytes:
    try:
        return (get or http_get)(url, BROWSER_UA)
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else 0
        if status in _NOT_PUBLISHED_STATUS:
            msg = f"Cboe has no data for {day.isoformat()} yet (HTTP {status})"
            raise NotPublishedError(msg) from exc
        raise


# ---------------------------------------------------------------------------
# Daily options market statistics
# ---------------------------------------------------------------------------


def _int(v: Any) -> int | None:
    try:
        n = int(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    return n if n >= 0 else None


def _section_rows(payload: Mapping[str, Any], name: str) -> dict[str, Mapping[str, Any]]:
    """``{"VOLUME": {...}, "OPEN INTEREST": {...}}`` of one product section."""
    rows = payload.get(name) or []
    out: dict[str, Mapping[str, Any]] = {}
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, dict):
            out[str(row.get("name", "")).strip().upper()] = row
    return out


def daily_segments(payload: Mapping[str, Any]) -> tuple[list[PcRatio], list[ProductOi]]:
    """Cboe daily-options JSON -> (ratios, open interest), in a fixed segment order.

    A ratio needs a parseable, non-negative value; its call/put volume comes from the
    matching product section (``SUM OF ALL PRODUCTS`` for ``total``). An OI row needs
    call, put and total open interest.
    """
    sections = {
        key: _section_rows(payload, name) for name, key in _SECTIONS.items() if name in payload
    }
    ratios: dict[str, PcRatio] = {}
    for item in payload.get("ratios") or []:
        if not isinstance(item, dict):
            continue
        seg = _RATIO_NAMES.get(str(item.get("name", "")).strip().upper())
        try:
            value = float(str(item.get("value", "")).strip())
        except ValueError:
            continue
        if seg is None or value < 0:
            continue
        vol = sections.get("all" if seg == "total" else seg, {}).get("VOLUME") or {}
        ratios[seg] = PcRatio(
            segment=seg,
            ratio=value,
            call_volume=_int(vol.get("call")),
            put_volume=_int(vol.get("put")),
        )
    oi: dict[str, ProductOi] = {}
    for product, rows in sections.items():
        row = rows.get("OPEN INTEREST") or {}
        call, put, total = _int(row.get("call")), _int(row.get("put")), _int(row.get("total"))
        if call is None or put is None or total is None:
            continue
        oi[product] = ProductOi(
            product=product,  # type: ignore[arg-type]
            call_oi=call,
            put_oi=put,
            total_oi=total,
            volume=_int((rows.get("VOLUME") or {}).get("total")),
        )
    return (
        [ratios[s] for s in _SEGMENT_ORDER if s in ratios],
        [oi[p] for p in _OI_ORDER if p in oi],
    )


def parse_daily_options(
    payload: Mapping[str, Any],
    day: _dt.date,
    *,
    fetched_at: _dt.datetime,
    url: str | None = None,
) -> OptionsDailyPayload | None:
    """Cboe daily-options JSON -> :class:`OptionsDailyPayload`; ``None`` without a total ratio."""
    ratios, oi = daily_segments(payload)
    if not any(r.segment == "total" for r in ratios):
        return None
    return OptionsDailyPayload(
        as_of=day.isoformat(),
        fetched_at=fetched_at.astimezone(ET).isoformat(),
        ratios=ratios,
        open_interest=oi,
        url=url or CBOE_DAILY_URL.format(day=day.isoformat()),
    )


def fetch_daily_options(
    day: _dt.date,
    get: Callable[[str, str], bytes] | None = None,
    *,
    now: _dt.datetime,
) -> OptionsDailyPayload:
    """The statistics for session *day*; :class:`NotPublishedError` when not out yet.

    An empty body or a payload without a total put/call ratio counts as not
    published; any other HTTP error or malformed JSON raises.
    """
    url = CBOE_DAILY_URL.format(day=day.isoformat())
    body = _get_or_unpublished(url, day, get)
    if not body.strip():
        msg = f"Cboe daily options for {day.isoformat()}: empty response"
        raise NotPublishedError(msg)
    data = json.loads(body)
    if not isinstance(data, dict):
        msg = f"Cboe daily options for {day.isoformat()}: expected a JSON object"
        raise ValueError(msg)  # noqa: TRY004 - a malformed payload, not a caller type error
    parsed = parse_daily_options(data, day, fetched_at=now, url=url)
    if parsed is None:
        msg = f"Cboe daily options for {day.isoformat()}: no total put/call ratio"
        raise NotPublishedError(msg)
    return parsed


# ---------------------------------------------------------------------------
# CFE VX settlement curve
# ---------------------------------------------------------------------------


def parse_vx_settlements(csv_text: str) -> list[VxPoint]:
    """CFE settlement CSV -> VX points (product ``VX`` only; VXM, VA, ... dropped).

    Monthlies first by expiry, then weeklies by expiry. Rows with an unknown symbol
    shape, a bad date or a non-positive price are skipped.
    """
    reader = csv.DictReader(io.StringIO(csv_text.lstrip("\ufeff")))
    monthly: list[VxPoint] = []
    weekly: list[VxPoint] = []
    for row in reader:
        norm = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
        if norm.get("product", "").upper() != "VX":
            continue
        symbol = norm.get("symbol", "").upper()
        m = _VX_SYMBOL.match(symbol)
        try:
            expiry = _dt.date.fromisoformat(norm.get("expiration date", ""))
            settle = float(norm.get("price", ""))
        except ValueError:
            continue
        if m is None or settle <= 0:
            continue
        point = VxPoint(
            symbol=symbol,
            expiry=expiry.isoformat(),
            settle=settle,
            weekly=m.group("week") is not None,
        )
        (weekly if point.weekly else monthly).append(point)
    return sorted(monthly, key=lambda p: p.expiry) + sorted(weekly, key=lambda p: p.expiry)


class VxShape(NamedTuple):
    front: float
    second: float
    back: float
    slope_1_2_pct: float
    shape: str


def curve_shape(points: Sequence[VxPoint], *, flat_band: float = 0.5) -> VxShape:
    """Front/second/back monthly settles, the 1→2 slope in % and the curve shape.

    ``flat`` when ``|slope_1_2_pct| < flat_band`` (percent), else ``contango`` for an
    upward slope and ``backwardation`` for a downward one. Weeklies are ignored.
    Raises ``ValueError`` with fewer than two monthlies.
    """
    monthly = sorted((p for p in points if not p.weekly), key=lambda p: p.expiry)
    if len(monthly) < 2:  # noqa: PLR2004 - front and second month
        msg = f"VX curve needs two monthly settlements, got {len(monthly)}"
        raise ValueError(msg)
    front, second, back = monthly[0].settle, monthly[1].settle, monthly[-1].settle
    slope = round((second / front - 1.0) * 100.0, 3)
    if slope == 0 or abs(slope) < flat_band:
        shape = "flat"
    elif slope > 0:
        shape = "contango"
    else:
        shape = "backwardation"
    return VxShape(front, second, back, slope, shape)


def vx_curve(
    points: Sequence[VxPoint],
    *,
    as_of: _dt.date,
    fetched_at: _dt.datetime,
    url: str,
    flat_band: float = 0.5,
) -> VxCurvePayload:
    """Build the ``vx_curve`` payload; ``ValueError`` with fewer than two monthlies."""
    s = curve_shape(points, flat_band=flat_band)
    return VxCurvePayload(
        as_of=as_of.isoformat(),
        fetched_at=fetched_at.astimezone(ET).isoformat(),
        points=list(points),
        front=s.front,
        second=s.second,
        back=s.back,
        slope_1_2_pct=s.slope_1_2_pct,
        shape=s.shape,  # type: ignore[arg-type]
        url=url,
    )


def fetch_vx_settlements(
    day: _dt.date,
    get: Callable[[str, str], bytes] | None = None,
    *,
    now: _dt.datetime,
    flat_band: float = 0.5,
) -> VxCurvePayload:
    """The VX settlement curve for *day*; :class:`NotPublishedError` when not out yet.

    Cboe answers an unpublished date with a bare CSV header (HTTP 200), so no VX
    monthly row counts as not published.
    """
    url = VX_SETTLEMENT_URL.format(day=day.isoformat())
    body = _get_or_unpublished(url, day, get)
    points = parse_vx_settlements(body.decode("utf-8", "replace"))
    if not any(not p.weekly for p in points):
        msg = f"CFE VX settlements for {day.isoformat()}: no VX rows"
        raise NotPublishedError(msg)
    return vx_curve(points, as_of=day, fetched_at=now, url=url, flat_band=flat_band)
