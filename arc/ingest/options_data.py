"""Options-trading data sources (E4.5, D30 §5). Free, no API key (D3).

Each source is a parser (pure, unit-tested on recorded payloads) plus a thin
fetcher. Handlers in :mod:`arc.routines.handlers` write the typed payloads as
context entries (kinds in :mod:`arc.context.kinds`); nothing here calls an LLM.

* :func:`fetch_vol_term` — Cboe daily closes for VIX9D / VIX / VIX3M / VVIX
  (``cdn.cboe.com`` history CSVs). ``structure`` is ``contango`` when
  VIX3M/VIX >= 1 + ``band``, ``backwardation`` when <= 1 - ``band``, else flat.
* :func:`fetch_macro_calendar` — FOMC decision days (federalreserve.gov calendar
  page) and BLS release dates for CPI / PPI / Employment Situation / JOLTS / ECI
  (the BLS release-schedule ICS), plus BEA GDP estimates and Personal Income and
  Outlays (PCE) from the BEA release-schedule ICS.
* :func:`fetch_ex_dividends` — next cash dividend ex-date per ticker (Alpaca
  corporate actions): early-assignment risk on short calls.
"""

from __future__ import annotations

import csv
import datetime as _dt
import io
import re
import time
from typing import TYPE_CHECKING, Any

import requests
import structlog

from arc.context.kinds import (
    ExDividendPayload,
    MacroCalendarPayload,
    MacroEvent,
    VolTermPayload,
)
from arc.utils.calendar import ET

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping, Sequence


log = structlog.get_logger()

__all__ = [
    "BROWSER_UA",
    "BLS_UA",
    "fetch_ex_dividends",
    "fetch_macro_calendar",
    "fetch_vol_term",
    "parse_bea_ics",
    "parse_bls_ics",
    "parse_cboe_history",
    "parse_fomc_calendar",
    "vol_term_from_closes",
]

# Cboe and the Fed reset generic clients. BLS 403s any agent without a contact
# email (like SEC EDGAR), so callers pass ``settings.edgar_user_agent``; this is
# the fallback with the same shape.
BROWSER_UA = "Mozilla/5.0 (compatible; ProjectArc/0.1)"
BLS_UA = "ProjectArc/0.1 (arc@example.com)"

CBOE_HISTORY_URL = "https://cdn.cboe.com/api/global/us_indices/daily_prices/{index}_History.csv"
CBOE_DAILY_URL = "https://cdn.cboe.com/data/us/options/market_statistics/daily/{day}_daily_options"
FOMC_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
BLS_ICS_URL = "https://www.bls.gov/schedule/news_release/bls.ics"
BEA_ICS_URL = "https://www.bea.gov/news/schedule/ics/online-calendar-subscription.ics"

VOL_INDICES = ("VIX9D", "VIX", "VIX3M", "VVIX")


def http_get(
    url: str,
    user_agent: str,
    *,
    timeout: float = 20.0,
    retries: int = 0,
    backoff_s: float = 2.0,
    sleep: Callable[[float], None] = time.sleep,
    max_bytes: int | None = None,
) -> bytes:
    """GET *url* with *user_agent*; raises on HTTP errors.

    ``retries`` (default 0) re-tries connection errors, timeouts and 5xx responses
    with a linear backoff (``backoff_s`` × attempt). A 4xx is never retried.
    ``max_bytes`` (E13.6): a body larger than this raises ``ValueError`` (size guard).
    """
    attempt = 0
    while True:
        try:
            resp = requests.get(url, headers={"User-Agent": user_agent}, timeout=timeout)
            resp.raise_for_status()
            if max_bytes is not None and len(resp.content) > max_bytes:
                msg = f"{url}: body {len(resp.content)} bytes > max_bytes {max_bytes}"
                raise ValueError(msg)
            return resp.content
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else 0
            if status < 500 or attempt >= retries:
                raise
        except (requests.ConnectionError, requests.Timeout):
            if attempt >= retries:
                raise
        attempt += 1
        sleep(backoff_s * attempt)


# ---------------------------------------------------------------------------
# VIX term structure
# ---------------------------------------------------------------------------


def parse_cboe_history(text: str) -> dict[_dt.date, float]:
    """Cboe ``*_History.csv`` -> ``{date: close}`` (``CLOSE``, or the index column)."""
    reader = csv.DictReader(io.StringIO(text))
    fields = [f.strip().upper() for f in reader.fieldnames or []]
    value_col = "CLOSE" if "CLOSE" in fields else (fields[-1] if fields else "")
    out: dict[_dt.date, float] = {}
    for row in reader:
        norm = {k.strip().upper(): (v or "").strip() for k, v in row.items() if k}
        try:
            day = _dt.datetime.strptime(norm["DATE"], "%m/%d/%Y").date()  # noqa: DTZ007 - a date
            val = float(norm[value_col])
        except (KeyError, ValueError):
            continue
        if val > 0:
            out[day] = val
    return out


def vol_term_from_closes(
    closes: Mapping[str, Mapping[_dt.date, float]], *, band: float = 0.02
) -> VolTermPayload | None:
    """Term structure on the latest date VIX has a close; ``None`` without VIX."""
    vix = closes.get("VIX") or {}
    if not vix:
        return None
    day = max(vix)

    def at(index: str) -> float | None:
        return (closes.get(index) or {}).get(day)

    v, v9, v3, vv = vix[day], at("VIX9D"), at("VIX3M"), at("VVIX")
    r3 = round(v3 / v, 4) if v3 else None
    r9 = round(v9 / v, 4) if v9 else None
    if r3 is None:
        structure = "flat"
    elif r3 >= 1 + band:
        structure = "contango"
    elif r3 <= 1 - band:
        structure = "backwardation"
    else:
        structure = "flat"
    return VolTermPayload(
        as_of=day.isoformat(),
        vix9d=v9,
        vix=v,
        vix3m=v3,
        vvix=vv,
        ratio_3m_1m=r3,
        ratio_9d_1m=r9,
        structure=structure,  # type: ignore[arg-type]
    )


def fetch_vol_term(
    *, get: Callable[[str, str], bytes] | None = None, band: float = 0.02
) -> VolTermPayload | None:
    closes: dict[str, dict[_dt.date, float]] = {}
    for index in VOL_INDICES:
        try:
            body = (get or http_get)(CBOE_HISTORY_URL.format(index=index), BROWSER_UA)
        except requests.RequestException as exc:
            log.warning("vol_term.fetch_error", index=index, error=str(exc))
            continue
        closes[index] = parse_cboe_history(body.decode("utf-8", "replace"))
    return vol_term_from_closes(closes, band=band)


# ---------------------------------------------------------------------------
# Macro calendar (FOMC + BLS + BEA)
# ---------------------------------------------------------------------------

_MONTHS = {
    m: i
    for i, m in enumerate(
        ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"],
        start=1,
    )
}
_YEAR_HEAD = re.compile(r"(\d{4}) FOMC Meetings")
_MEETING = re.compile(
    r"fomc-meeting__month[^>]*>\s*<strong>([^<]+)</strong>.*?"
    r"fomc-meeting__date[^>]*>([^<]+)</div>",
    re.DOTALL,
)


def parse_fomc_calendar(html_text: str) -> list[MacroEvent]:
    """Scheduled FOMC **decision** days (the last day of each meeting, 14:00 ET).

    Notation votes and unscheduled meetings are skipped; ``*`` (SEP meeting) kept.
    """
    events: list[MacroEvent] = []
    heads = list(_YEAR_HEAD.finditer(html_text))
    for i, head in enumerate(heads):
        year = int(head.group(1))
        end = heads[i + 1].start() if i + 1 < len(heads) else len(html_text)
        for m in _MEETING.finditer(html_text, head.end(), end):
            month_raw = m.group(1).strip().lower()
            dates = m.group(2).strip()
            if "notation" in dates.lower() or "unscheduled" in dates.lower():
                continue
            months = [p.strip()[:3] for p in re.split(r"[/\-]", month_raw)]
            months = [p for p in months if p in _MONTHS]
            days = [int(x) for x in re.findall(r"\d+", dates)]
            if not months or not days:
                continue
            last_day = days[-1]
            month = _MONTHS[months[-1]]
            if len(months) == 1 and len(days) == 2 and days[1] < days[0]:
                month = month % 12 + 1  # "Apr/May 30-1" style without a second month
            yr = year + (1 if month < _MONTHS[months[0]] else 0)
            try:
                day = _dt.date(yr, month, last_day)
            except ValueError:
                continue
            sep = "*" in dates
            events.append(
                MacroEvent(
                    date=day.isoformat(),
                    time="14:00",
                    kind="fomc",
                    name="FOMC rate decision" + (" + projections" if sep else ""),
                    source="federalreserve.gov",
                )
            )
    return sorted(events, key=lambda e: e.date)


_BLS_KINDS: tuple[tuple[str, str], ...] = (
    ("consumer price index", "cpi"),
    ("producer price index", "ppi"),
    ("employment situation", "nfp"),
    ("job openings and labor turnover survey", "jolts"),
    ("employment cost index", "eci"),
)


def _unfold_ics(text: str) -> list[str]:
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").split("\n"):
        if raw.startswith((" ", "\t")) and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    return lines


def parse_bls_ics(text: str) -> list[MacroEvent]:
    """BLS release schedule (ICS) -> market-moving releases (CPI, PPI, NFP, JOLTS, ECI)."""
    events: list[MacroEvent] = []
    cur: dict[str, str] = {}
    for line in _unfold_ics(text):
        if line == "BEGIN:VEVENT":
            cur = {}
        elif line == "END:VEVENT":
            summary = cur.get("SUMMARY", "").strip()
            low = summary.lower()
            if low.startswith("state ") or low.startswith("metropolitan"):
                continue
            kind = next((k for needle, k in _BLS_KINDS if low.startswith(needle)), None)
            start = cur.get("DTSTART", "")
            m = re.match(r"(\d{8})(?:T(\d{2})(\d{2}))?", start)
            if kind is None or m is None:
                continue
            day = _dt.datetime.strptime(m.group(1), "%Y%m%d").date()  # noqa: DTZ007 - a date
            time = f"{m.group(2)}:{m.group(3)}" if m.group(2) else None
            events.append(
                MacroEvent(
                    date=day.isoformat(),
                    time=time,
                    kind=kind,  # type: ignore[arg-type]
                    name=summary[:120],
                    source="bls.gov",
                )
            )
        elif ":" in line:
            key, _, value = line.partition(":")
            cur[key.split(";", 1)[0].upper()] = value
    return sorted(events, key=lambda e: (e.date, e.kind))


def _ics_unescape(value: str) -> str:
    """RFC 5545 TEXT unescape (``\\,`` ``\\;`` ``\\n`` ``\\\\``)."""
    return re.sub(
        r"\\([\\,;nN])", lambda m: " " if m.group(1) in "nN" else m.group(1), value
    ).strip()


def _ics_vevents(text: str) -> list[dict[str, tuple[str, str]]]:
    """VEVENTs as ``{NAME: (params, value)}`` after unfolding (params keep ``TZID`` etc.)."""
    out: list[dict[str, tuple[str, str]]] = []
    cur: dict[str, tuple[str, str]] | None = None
    for line in _unfold_ics(text):
        if line == "BEGIN:VEVENT":
            cur = {}
        elif line == "END:VEVENT":
            if cur is not None:
                out.append(cur)
            cur = None
        elif cur is not None and ":" in line:
            key, _, value = line.partition(":")
            name, _, params = key.partition(";")
            cur[name.upper()] = (params, value)
    return out


def _ics_start_et(params: str, value: str) -> tuple[_dt.date, str | None] | None:
    """DTSTART -> (ET date, ``HH:MM`` ET or ``None`` for an all-day event).

    ``...Z`` is UTC and converted to ET; a floating/``TZID`` time is taken as ET
    (the BEA calendar declares ``America/New_York``).
    """
    m = re.fullmatch(r"(\d{8})(?:T(\d{2})(\d{2})(\d{2})?(Z)?)?", value.strip())
    if m is None:
        return None
    day = _dt.datetime.strptime(m.group(1), "%Y%m%d").date()  # noqa: DTZ007 - a date
    if m.group(2) is None:
        return day, None
    naive = _dt.datetime.combine(day, _dt.time(int(m.group(2)), int(m.group(3))))
    if m.group(5):
        at = naive.replace(tzinfo=_dt.UTC).astimezone(ET)
    else:
        tz = re.search(r"TZID=([^;:]+)", params)
        if tz and tz.group(1) not in ("America/New_York", "US/Eastern"):
            return None  # never guess another zone's offset
        at = naive.replace(tzinfo=ET)
    return at.date(), at.strftime("%H:%M")


# Whitelisted BEA headline releases. ``GDP (`` / ``Gross Domestic Product,`` are the
# national accounts (short and pre-2026 long title); "GDP by County", "Gross Domestic
# Product by State / for Puerto Rico" and "Real Personal Consumption Expenditures by
# State" fail these prefixes on purpose.
_BEA_GDP = re.compile(r"^(?:GDP \(|Gross Domestic Product, )")
_BEA_PCE = re.compile(r"^Personal Income and Outlays, (.+)$")
_BEA_QUARTER = re.compile(r"(\d)(?:st|nd|rd|th) Quarter(?: and Year)? (\d{4})", re.IGNORECASE)
_BEA_ESTIMATE = re.compile(r"\((\w+) Estimate\)", re.IGNORECASE)


def _bea_kind_name(summary: str) -> tuple[str, str] | None:
    if _BEA_GDP.match(summary):
        q = _BEA_QUARTER.search(summary)
        est = _BEA_ESTIMATE.search(summary)
        name = f"GDP Q{q.group(1)} {q.group(2)}" if q else "GDP"
        if est:
            name += f" ({est.group(1).lower()})"
        return "gdp", name
    pce = _BEA_PCE.match(summary)
    if pce:
        return "pce", f"PCE / Personal Income {pce.group(1).strip()}"[:120]
    return None


def parse_bea_ics(text: str) -> list[MacroEvent]:
    """BEA release schedule (ICS) -> GDP estimates and Personal Income and Outlays (PCE).

    Only the whitelisted headline releases are kept; every other BEA release
    (trade, international transactions, regional GDP/PCE, satellite accounts) is
    dropped, never typed ``other``. Times are converted from UTC to ET.
    """
    events: list[MacroEvent] = []
    for ev in _ics_vevents(text):
        summary = _ics_unescape(ev.get("SUMMARY", ("", ""))[1])
        typed = _bea_kind_name(summary)
        start = _ics_start_et(*ev.get("DTSTART", ("", "")))
        if typed is None or start is None:
            continue
        day, time = start
        events.append(
            MacroEvent(
                date=day.isoformat(),
                time=time,
                kind=typed[0],  # type: ignore[arg-type]
                name=typed[1],
                source="bea.gov",
            )
        )
    return sorted(events, key=lambda e: (e.date, e.kind))


def macro_calendar(
    events: Iterable[MacroEvent], today: _dt.date, horizon_days: int
) -> MacroCalendarPayload:
    """Events in ``[today, today + horizon_days]``, deduped on (date, kind, name)."""
    until = today + _dt.timedelta(days=horizon_days)
    seen: set[tuple[str, str, str]] = set()
    kept: list[MacroEvent] = []
    for e in sorted(events, key=lambda e: (e.date, e.kind)):
        d = _dt.date.fromisoformat(e.date)
        key = (e.date, e.kind, e.name)
        if today <= d <= until and key not in seen:
            seen.add(key)
            kept.append(e)
    return MacroCalendarPayload(as_of=today.isoformat(), horizon_days=horizon_days, events=kept)


def fetch_macro_calendar(
    today: _dt.date,
    horizon_days: int,
    *,
    get: Callable[[str, str], bytes] | None = None,
    contact_ua: str = BLS_UA,
    status: dict[str, str] | None = None,
) -> tuple[MacroCalendarPayload, dict[str, int]]:
    """Merged FOMC + BLS + BEA calendar and per-source event counts (0 = fetch failed).

    One source failing (HTTP error or an unparseable payload) never drops the
    others; it is logged ``macro_calendar.source_failed`` and counted 0.
    *status*, when given, is filled per source with ``ok`` or ``failed:<ErrorClass>``
    (for the run manifest: an empty-but-healthy feed is not a failure).
    *contact_ua*: a User-Agent with a contact email; BLS answers 403 without one
    (sent to BEA too, same courtesy).
    """
    events: list[MacroEvent] = []
    counts: dict[str, int] = {}
    status = {} if status is None else status
    for name, url, ua, parse in (
        ("fomc", FOMC_URL, BROWSER_UA, parse_fomc_calendar),
        ("bls", BLS_ICS_URL, contact_ua, parse_bls_ics),
        ("bea", BEA_ICS_URL, contact_ua, parse_bea_ics),
    ):
        try:
            got = parse((get or http_get)(url, ua).decode("utf-8", "replace"))
            status[name] = "ok"
        except (requests.RequestException, ValueError) as exc:  # ValidationError is a ValueError
            log.warning(
                "macro_calendar.source_failed",
                source=name,
                error_class=type(exc).__name__,
                error=str(exc)[:300],
            )
            status[name] = f"failed:{type(exc).__name__}"
            got = []
        counts[name] = len(got)
        events.extend(got)
    return macro_calendar(events, today, horizon_days), counts


# ---------------------------------------------------------------------------
# Ex-dividend dates (Alpaca corporate actions)
# ---------------------------------------------------------------------------


def next_ex_dividends(
    actions: Iterable[Mapping[str, Any]], today: _dt.date, horizon_days: int
) -> dict[str, ExDividendPayload]:
    """Earliest upcoming cash-dividend ex-date per symbol within the horizon (pure)."""
    until = today + _dt.timedelta(days=horizon_days)
    best: dict[str, ExDividendPayload] = {}
    for a in actions:
        sym = str(a.get("symbol") or "").upper()
        raw = a.get("ex_date")
        if not sym or raw is None:
            continue
        ex = raw if isinstance(raw, _dt.date) else _dt.date.fromisoformat(str(raw)[:10])
        if not (today <= ex <= until):
            continue
        if sym in best and _dt.date.fromisoformat(best[sym].ex_date) <= ex:
            continue

        def iso(v: Any) -> str | None:
            return v.isoformat() if isinstance(v, _dt.date) else (str(v)[:10] if v else None)

        rate = a.get("rate")
        best[sym] = ExDividendPayload(
            ticker=sym,
            ex_date=ex.isoformat(),
            amount=float(rate) if rate is not None else None,
            record_date=iso(a.get("record_date")),
            payable_date=iso(a.get("payable_date")),
        )
    return best


def fetch_ex_dividends(
    tickers: Sequence[str], today: _dt.date, horizon_days: int
) -> dict[str, ExDividendPayload]:  # pragma: no cover - live Alpaca call (integration)
    from alpaca.data.enums import CorporateActionsType
    from alpaca.data.historical.corporate_actions import CorporateActionsClient
    from alpaca.data.requests import CorporateActionsRequest

    from arc.data.alpaca import _get_keys

    key, secret = _get_keys()
    client = CorporateActionsClient(api_key=key, secret_key=secret)
    req = CorporateActionsRequest(
        symbols=list(tickers),
        types=[CorporateActionsType.CASH_DIVIDEND],
        start=today,
        end=today + _dt.timedelta(days=horizon_days),
    )
    resp = client.get_corporate_actions(req)
    data = getattr(resp, "data", {}) or {}
    rows: list[dict[str, Any]] = []
    for items in data.values():
        for item in items:
            rows.append(item.model_dump() if hasattr(item, "model_dump") else dict(item))
    return next_ex_dividends(rows, today, horizon_days)


def today_et(now: _dt.datetime) -> _dt.date:
    return now.astimezone(ET).date()
