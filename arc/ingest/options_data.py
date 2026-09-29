"""Options-trading data sources (E4.5, D30 §5). Free, no API key (D3).

Each source is a parser (pure, unit-tested on recorded payloads) plus a thin
fetcher. Handlers in :mod:`arc.routines.handlers` write the typed payloads as
context entries (kinds in :mod:`arc.context.kinds`); nothing here calls an LLM.

* :func:`fetch_vol_term` — Cboe daily closes for VIX9D / VIX / VIX3M / VVIX
  (``cdn.cboe.com`` history CSVs). ``structure`` is ``contango`` when
  VIX3M/VIX >= 1 + ``band``, ``backwardation`` when <= 1 - ``band``, else flat.
* :func:`fetch_put_call` — Cboe daily market statistics (total / equity / index /
  ETP / SPX / VIX put-call ratios) for the latest session that has data.
* :func:`fetch_macro_calendar` — FOMC decision days (federalreserve.gov calendar
  page) and BLS release dates for CPI / PPI / Employment Situation / JOLTS / ECI
  (the BLS release-schedule ICS).
* :func:`unusual_activity` — per-underlying options volume vs its own 20-session
  average (``options_volume_daily``) and per-contract volume / open interest,
  from the Alpaca chain snapshot the scanner already uses. No new provider.
* :func:`fetch_ex_dividends` — next cash dividend ex-date per ticker (Alpaca
  corporate actions): early-assignment risk on short calls.
"""

from __future__ import annotations

import csv
import datetime as _dt
import io
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import requests
import structlog

from arc.context.kinds import (
    ExDividendPayload,
    MacroCalendarPayload,
    MacroEvent,
    PutCallPayload,
    UnusualContract,
    UnusualOptionsPayload,
    VolTermPayload,
)
from arc.utils.calendar import ET

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable, Iterable, Mapping, Sequence

    from arc.data.base import MarketDataProvider, OptionContract

log = structlog.get_logger()

__all__ = [
    "BROWSER_UA",
    "BLS_UA",
    "fetch_ex_dividends",
    "fetch_macro_calendar",
    "fetch_put_call",
    "fetch_vol_term",
    "parse_bls_ics",
    "parse_cboe_history",
    "parse_fomc_calendar",
    "parse_put_call",
    "unusual_activity",
    "vol_term_from_closes",
]

# Cboe and the Fed reset generic clients; BLS asks for a descriptive agent.
BROWSER_UA = "Mozilla/5.0 (compatible; ProjectArc/0.1)"
BLS_UA = "ProjectArc research (options data; contact via repo owner)"

CBOE_HISTORY_URL = "https://cdn.cboe.com/api/global/us_indices/daily_prices/{index}_History.csv"
CBOE_DAILY_URL = "https://cdn.cboe.com/data/us/options/market_statistics/daily/{day}_daily_options"
FOMC_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
BLS_ICS_URL = "https://www.bls.gov/schedule/news_release/bls.ics"

VOL_INDICES = ("VIX9D", "VIX", "VIX3M", "VVIX")


def http_get(url: str, user_agent: str, *, timeout: float = 20.0) -> bytes:
    resp = requests.get(url, headers={"User-Agent": user_agent}, timeout=timeout)
    resp.raise_for_status()
    return resp.content


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
# Put/call ratios
# ---------------------------------------------------------------------------

_PC_NAMES = {
    "TOTAL PUT/CALL RATIO": "total",
    "EQUITY PUT/CALL RATIO": "equity",
    "INDEX PUT/CALL RATIO": "index",
    "EXCHANGE TRADED PRODUCTS PUT/CALL RATIO": "etp",
    "SPX + SPXW PUT/CALL RATIO": "spx",
    "CBOE VOLATILITY INDEX (VIX) PUT/CALL RATIO": "vix",
}


def parse_put_call(payload: Mapping[str, Any], day: _dt.date) -> PutCallPayload | None:
    """Cboe daily-options JSON -> ratios; ``None`` when the day has no total ratio."""
    vals: dict[str, float] = {}
    for item in payload.get("ratios") or []:
        key = _PC_NAMES.get(str(item.get("name", "")).strip().upper())
        try:
            v = float(str(item.get("value", "")).strip())
        except ValueError:
            continue
        if key is not None and v > 0:
            vals[key] = v
    if "total" not in vals:
        return None
    return PutCallPayload(
        as_of=day.isoformat(),
        total=vals["total"],
        equity=vals.get("equity"),
        index=vals.get("index"),
        etp=vals.get("etp"),
        spx=vals.get("spx"),
        vix=vals.get("vix"),
    )


def fetch_put_call(
    today: _dt.date,
    *,
    get: Callable[[str, str], bytes] | None = None,
    lookback_days: int = 7,
) -> PutCallPayload | None:
    """Latest session (today back to *lookback_days*) with published ratios."""
    import json

    for back in range(lookback_days + 1):
        day = today - _dt.timedelta(days=back)
        if day.weekday() >= 5:
            continue
        try:
            body = (get or http_get)(CBOE_DAILY_URL.format(day=day.isoformat()), BROWSER_UA)
            parsed = parse_put_call(json.loads(body), day)
        except (requests.RequestException, ValueError) as exc:
            log.debug("put_call.miss", day=day.isoformat(), error=str(exc))
            continue
        if parsed is not None:
            return parsed
    return None


# ---------------------------------------------------------------------------
# Macro calendar (FOMC + BLS)
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


def macro_calendar(
    events: Iterable[MacroEvent], today: _dt.date, horizon_days: int
) -> MacroCalendarPayload:
    until = today + _dt.timedelta(days=horizon_days)
    seen: set[tuple[str, str]] = set()
    kept: list[MacroEvent] = []
    for e in sorted(events, key=lambda e: (e.date, e.kind)):
        d = _dt.date.fromisoformat(e.date)
        if today <= d <= until and (e.date, e.kind) not in seen:
            seen.add((e.date, e.kind))
            kept.append(e)
    return MacroCalendarPayload(as_of=today.isoformat(), horizon_days=horizon_days, events=kept)


def fetch_macro_calendar(
    today: _dt.date,
    horizon_days: int,
    *,
    get: Callable[[str, str], bytes] | None = None,
) -> tuple[MacroCalendarPayload, dict[str, int]]:
    """Merged FOMC + BLS calendar and per-source event counts (0 = fetch failed)."""
    events: list[MacroEvent] = []
    counts: dict[str, int] = {}
    for name, url, ua, parse in (
        ("fomc", FOMC_URL, BROWSER_UA, parse_fomc_calendar),
        ("bls", BLS_ICS_URL, BLS_UA, parse_bls_ics),
    ):
        try:
            got = parse((get or http_get)(url, ua).decode("utf-8", "replace"))
        except requests.RequestException as exc:
            log.warning("macro_calendar.fetch_error", source=name, error=str(exc))
            got = []
        counts[name] = len(got)
        events.extend(got)
    return macro_calendar(events, today, horizon_days), counts


# ---------------------------------------------------------------------------
# Unusual options activity (self-computed)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UoaThresholds:
    min_volume: int = 500
    vol_oi_ratio: float = 2.0
    volume_spike_ratio: float = 2.0
    history_days: int = 20
    max_contracts: int = 5


def record_volume(
    conn: sqlite3.Connection, ticker: str, day: _dt.date, calls: int, puts: int, *, now: str
) -> None:
    conn.execute(
        """INSERT INTO options_volume_daily (ticker, day, call_volume, put_volume, updated_at)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(ticker, day) DO UPDATE SET call_volume = excluded.call_volume,
               put_volume = excluded.put_volume, updated_at = excluded.updated_at""",
        (ticker, day.isoformat(), calls, puts, now),
    )
    conn.commit()


def prior_volumes(conn: sqlite3.Connection, ticker: str, day: _dt.date, n: int) -> list[int]:
    rows = conn.execute(
        """SELECT call_volume + put_volume FROM options_volume_daily
           WHERE ticker = ? AND day < ? ORDER BY day DESC LIMIT ?""",
        (ticker, day.isoformat(), n),
    ).fetchall()
    return [int(r[0]) for r in rows]


def unusual_activity(
    ticker: str,
    contracts: Sequence[OptionContract],
    history: Sequence[int],
    day: _dt.date,
    t: UoaThresholds,
) -> UnusualOptionsPayload:
    """Deterministic UOA read for one underlying (pure; *history* = prior daily totals)."""
    calls = sum(c.volume or 0 for c in contracts if c.option_type == "call")
    puts = sum(c.volume or 0 for c in contracts if c.option_type == "put")
    total = calls + puts
    avg = sum(history) / len(history) if history else None
    ratio = round(total / avg, 3) if avg else None
    flags: list[str] = []
    if ratio is not None and len(history) >= 5 and ratio >= t.volume_spike_ratio:
        flags.append("volume_spike")
    hot: list[UnusualContract] = []
    for c in contracts:
        vol = c.volume or 0
        if vol < t.min_volume:
            continue
        oi = c.open_interest
        voi = round(vol / oi, 3) if oi else None
        if oi is None or oi == 0 or (voi is not None and voi >= t.vol_oi_ratio):
            hot.append(
                UnusualContract(
                    symbol=c.symbol,
                    expiry=c.expiration.isoformat(),
                    strike=c.strike,
                    option_type="call" if c.option_type == "call" else "put",
                    volume=vol,
                    open_interest=oi,
                    vol_oi=voi,
                )
            )
    hot.sort(key=lambda u: (u.vol_oi is None, -(u.vol_oi or 0.0), -u.volume, u.symbol))
    if hot:
        flags.append("vol_oi")
    return UnusualOptionsPayload(
        ticker=ticker,
        as_of=day.isoformat(),
        call_volume=calls,
        put_volume=puts,
        total_volume=total,
        avg_volume=round(avg, 1) if avg is not None else None,
        history_days=len(history),
        volume_ratio=ratio,
        put_call_volume=round(puts / calls, 3) if calls else None,
        flags=flags,  # type: ignore[arg-type]
        contracts=hot[: t.max_contracts],
    )


def scan_unusual(
    conn: sqlite3.Connection,
    market: MarketDataProvider,
    tickers: Iterable[str],
    today: _dt.date,
    t: UoaThresholds,
    *,
    max_dte: int,
    now: str,
) -> tuple[list[UnusualOptionsPayload], dict[str, str]]:
    """Fetch each chain, persist today's volume, return payloads and per-ticker errors."""
    out: list[UnusualOptionsPayload] = []
    errors: dict[str, str] = {}
    for ticker in tickers:
        try:
            chain = market.option_chain(ticker, today, today + _dt.timedelta(days=max_dte))
        except Exception as exc:  # noqa: BLE001 - one bad ticker never stops the scan
            errors[ticker] = str(exc)[:200]
            log.warning("uoa.chain_error", ticker=ticker, error=str(exc)[:200])
            continue
        history = prior_volumes(conn, ticker, today, t.history_days)
        payload = unusual_activity(ticker, chain, history, today, t)
        record_volume(conn, ticker, today, payload.call_volume, payload.put_volume, now=now)
        out.append(payload)
    return out, errors


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
