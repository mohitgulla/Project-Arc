"""options_fast source (E13.6, D56): the Scalp's 30-minute Cboe options tape.

Three parts, each a typed context kind (never raw docs, never a gate input):

1. :func:`fetch_index_vols`: Cboe delayed index quotes (``_VIX``, ``_VIX9D``, ``_VXN``,
   ``_VIX1D``, ``_VIX3M``, ``_VVIX``; ~15 minutes delayed) -> ``index_vols`` with the
   VIX9D/VIX and VIX/VIX3M ratios and the term-structure / level flags.
2. :func:`parse_delayed_chain` + :func:`snapshot_ticker`: one delayed chain per ticker
   (``delayed_quotes/options/<T>.json``) -> ``chain_snapshot``: session-to-date
   call/put volume and the top-of-book of the 3 strikes nearest spot on the nearest
   expiry inside the entry DTE window.
3. :func:`parse_symbol_data_csv` + :func:`aggregate_by_underlying`: Cboe's exchange
   ``symbol_data`` CSV (per-contract volume on Cboe's own book) summed per underlying ->
   ``exchange_volume`` (active list + top 10 others; Tower/Ops visibility only, never a
   Scout or discovery input, D56 owner decision 1).

:func:`build_tape` renders the code-built options tape block for the Scalp prompt
(wired in E13.10). It is pure: same inputs, same text.

Not called: the BZX book (``/json/bzx/book/<T>``) and ``futures/VX.json`` answer 403;
depth is the per-contract top of book from the delayed chain. Cboe's delayed data is
for personal, non-redistributed use; Arc stores it for its own decisions and shows it
on internal surfaces only (docs/OPS.md 5.29).

Seeded from the archived E4.13 WIP (``options_tape.py``: ``parse_cboe_quote``,
``fetch_vix_complex``, ``detect_flips``, ``vix_line``).
"""

from __future__ import annotations

import argparse
import csv
import datetime as _dt
import io
import json
import re
import sys
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from arc.context.kinds import (
    BookLevel,
    ChainSnapshotPayload,
    ExchangeVolumePayload,
    ExchangeVolumeRow,
    IndexVol,
    IndexVolsPayload,
)
from arc.utils.calendar import ET

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence

__all__ = [
    "CBOE_CHAIN_URL",
    "CBOE_QUOTE_URL",
    "INDEX_VOL_SYMBOLS",
    "SYMBOL_DATA_URL",
    "TAPE_SOURCE",
    "CboeQuoteError",
    "ChainRow",
    "DelayedChain",
    "ScalpTape",
    "SnapshotSkipError",
    "SymbolDataRow",
    "TapeTicker",
    "aggregate_by_underlying",
    "build_tape",
    "detect_flips",
    "fetch_index_vols",
    "index_vol_flags",
    "parse_cboe_quote",
    "parse_delayed_chain",
    "parse_symbol_data_csv",
    "scalp_tape",
    "scalp_tape_from_store",
    "select_exchange_rows",
    "snapshot_ticker",
    "tape_corroborates",
    "tape_direction",
    "vix_line",
]

CBOE_QUOTE_URL = "https://cdn.cboe.com/api/global/delayed_quotes/quotes/_{sym}.json"
CBOE_CHAIN_URL = "https://cdn.cboe.com/api/global/delayed_quotes/options/{ticker}.json"
SYMBOL_DATA_URL = "https://www.cboe.com/us/options/market_statistics/symbol_data/csv/?mkt={mkt}"
UA = "Mozilla/5.0 (Project Arc)"  # the cdn rejects non-browser agents

# Fetch order = display order on the tape's VIX line.
INDEX_VOL_SYMBOLS: tuple[str, ...] = ("VIX1D", "VIX9D", "VIX", "VIX3M", "VVIX", "VXN")
MAX_STRIKES = 3  # BookLevel list is capped at 6 (3 strikes x call/put)
TOP_OTHER_UNDERLYINGS = 10
MAX_TAPE_CHARS = 1500

_OCC = re.compile(r"^(?P<root>[A-Z0-9.]+?)(?P<ymd>\d{6})(?P<cp>[CP])(?P<strike>\d{8})$")


class CboeQuoteError(RuntimeError):
    """The VIX quote could not be read (blocked, moved, or unparseable)."""


class SnapshotSkipError(ValueError):
    """A ticker's chain cannot be snapshotted; ``reason`` is the metrics key."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason


# ---------------------------------------------------------------------------
# 1. Index vols
# ---------------------------------------------------------------------------


def parse_cboe_quote(body: bytes | str) -> tuple[float, str | None] | None:
    """``(current_price, last_trade_time)`` from a Cboe delayed quote document."""
    try:
        data = json.loads(body).get("data") or {}
    except (ValueError, AttributeError):
        return None
    price = data.get("current_price")
    if not isinstance(price, int | float) or isinstance(price, bool) or price <= 0:
        return None
    ts = data.get("last_trade_time")
    return float(price), str(ts) if ts else None


def _ratio(num: float | None, den: float | None) -> float | None:
    if num is None or den is None or den <= 0:
        return None
    return round(num / den, 4)


def _as_of(ts: str | None, now: _dt.datetime) -> str:
    """Cboe's ``last_trade_time`` (ET wall clock, sometimes with an offset) as ET ISO."""
    if ts:
        try:
            t = _dt.datetime.fromisoformat(ts)
        except ValueError:
            t = None
        if t is not None:
            t = t.replace(tzinfo=ET) if t.tzinfo is None else t.astimezone(ET)
            return t.replace(microsecond=0).isoformat()
    return now.astimezone(ET).replace(microsecond=0).isoformat()


def index_vol_flags(
    vix: float,
    ratio_9d_30d: float | None,
    ratio_30d_3m: float | None,
    *,
    vix_gt_25: float = 25.0,
    vix_gt_35: float = 35.0,
) -> list[str]:
    """Flags in a fixed order. A ratio exactly 1.0 raises nothing (strictly above)."""
    flags: list[str] = []
    if ratio_9d_30d is not None and ratio_9d_30d > 1.0:
        flags.append("9d_over_30d")
    if ratio_30d_3m is not None and ratio_30d_3m > 1.0:
        flags.append("backwardation_30d_3m")
    if vix > vix_gt_25:
        flags.append("vix_gt_25")
    if vix > vix_gt_35:
        flags.append("vix_gt_35")
    return flags


def fetch_index_vols(
    get: Callable[[str], bytes],
    now: _dt.datetime,
    *,
    vix_gt_25: float = 25.0,
    vix_gt_35: float = 35.0,
    errors: dict[str, str] | None = None,
) -> IndexVolsPayload:
    """Read the six Cboe index quotes; raises :class:`CboeQuoteError` when the VIX
    itself cannot be read (the others are optional; misses land in *errors*)."""
    errors = {} if errors is None else errors
    quotes: dict[str, tuple[float, str | None]] = {}
    for sym in INDEX_VOL_SYMBOLS:
        try:
            q = parse_cboe_quote(get(CBOE_QUOTE_URL.format(sym=sym)))
        except Exception as exc:  # noqa: BLE001 - any one quote may fail; VIX is required
            errors[sym] = f"{type(exc).__name__}: {str(exc)[:120]}"
            continue
        if q is None:
            errors[sym] = "unparseable quote"
        else:
            quotes[sym] = q
    if "VIX" not in quotes:
        msg = f"Cboe VIX delayed quote unavailable ({errors.get('VIX', 'missing')})"
        raise CboeQuoteError(msg)
    px = {k: v[0] for k, v in quotes.items()}
    r9 = _ratio(px.get("VIX9D"), px["VIX"])
    r3 = _ratio(px["VIX"], px.get("VIX3M"))
    return IndexVolsPayload(
        fetched_at=now.astimezone(ET).replace(microsecond=0).isoformat(),
        quotes=[
            IndexVol(symbol=s, value=quotes[s][0], as_of=_as_of(quotes[s][1], now))  # type: ignore[arg-type]
            for s in INDEX_VOL_SYMBOLS
            if s in quotes
        ],
        ratio_9d_30d=r9,
        ratio_30d_3m=r3,
        flags=index_vol_flags(  # type: ignore[arg-type]
            px["VIX"], r9, r3, vix_gt_25=vix_gt_25, vix_gt_35=vix_gt_35
        ),
    )


def detect_flips(prev: IndexVolsPayload | None, cur: IndexVolsPayload) -> list[str]:
    """Flags that changed since *prev*: ``+flag`` turned on, ``-flag`` turned off.

    No previous entry = no flips (the first tape of the day only states the flags).
    A quote unchanged since *prev* (same VIX ``as_of``) never flips.
    """
    if prev is None:
        return []
    if _vix_as_of(prev) == _vix_as_of(cur):
        return []
    before, now = set(prev.flags), set(cur.flags)
    on = [f"+{f}" for f in cur.flags if f not in before]
    off = [f"-{f}" for f in prev.flags if f not in now]
    return on + off


def _vix_as_of(p: IndexVolsPayload) -> str | None:
    return next((q.as_of for q in p.quotes if q.symbol == "VIX"), None)


# ---------------------------------------------------------------------------
# 2. Delayed chain snapshot
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ChainRow:
    occ_symbol: str
    option_type: str  # call | put
    expiry: _dt.date
    strike: float
    bid: float | None
    ask: float | None
    bid_size: int | None
    ask_size: int | None
    iv: float | None
    open_interest: int | None
    volume: int


@dataclass(frozen=True)
class DelayedChain:
    ticker: str
    spot: float | None
    timestamp: str | None
    rows: list[ChainRow] = field(default_factory=list)
    unparsed: int = 0


def _num(v: Any) -> float | None:
    if isinstance(v, bool) or not isinstance(v, int | float):
        return None
    return float(v)


def _int(v: Any) -> int | None:
    f = _num(v)
    return None if f is None or f < 0 else int(f)


def parse_delayed_chain(body: bytes | str) -> DelayedChain:
    """Cboe ``delayed_quotes/options/<T>.json`` -> rows (OCC symbols parsed).

    Raises ``ValueError`` on a body that is not a chain document. A contract whose
    symbol is not OCC-shaped is counted in ``unparsed`` and dropped.
    """
    doc = json.loads(body)
    data = doc.get("data") if isinstance(doc, dict) else None
    if not isinstance(data, dict) or not isinstance(data.get("options"), list):
        msg = "not a Cboe delayed chain document (no data.options)"
        raise ValueError(msg)
    spot = _num(data.get("current_price"))
    rows: list[ChainRow] = []
    unparsed = 0
    for o in data["options"]:
        m = _OCC.match(str(o.get("option") or "")) if isinstance(o, dict) else None
        if m is None:
            unparsed += 1
            continue
        y = m.group("ymd")
        try:
            expiry = _dt.date(2000 + int(y[:2]), int(y[2:4]), int(y[4:6]))
        except ValueError:
            unparsed += 1
            continue
        bid, ask = _num(o.get("bid")), _num(o.get("ask"))
        iv = _num(o.get("iv"))
        rows.append(
            ChainRow(
                occ_symbol=m.group(0),
                option_type="call" if m.group("cp") == "C" else "put",
                expiry=expiry,
                strike=int(m.group("strike")) / 1000.0,
                bid=bid,
                ask=ask,
                bid_size=_int(o.get("bid_size")),
                ask_size=_int(o.get("ask_size")),
                iv=iv if iv is not None and iv > 0 else None,
                open_interest=_int(o.get("open_interest")),
                volume=_int(o.get("volume")) or 0,
            )
        )
    root = str(data.get("symbol") or doc.get("symbol") or "").lstrip("_").upper()
    return DelayedChain(
        ticker=root,
        spot=spot if spot is not None and spot > 0 else None,
        timestamp=str(doc.get("timestamp")) if doc.get("timestamp") else None,
        rows=rows,
        unparsed=unparsed,
    )


def spread_pct(bid: float | None, ask: float | None) -> float | None:
    """``(ask - bid) / mid`` for a two-sided quote (bid > 0, ask >= bid), else None."""
    if bid is None or ask is None or bid <= 0 or ask < bid:
        return None
    return round((ask - bid) / ((ask + bid) / 2), 4)


def _book_level(r: ChainRow) -> BookLevel:
    return BookLevel(
        occ_symbol=r.occ_symbol,
        option_type=r.option_type,  # type: ignore[arg-type]
        strike=r.strike,
        expiry=r.expiry.isoformat(),
        bid=r.bid,
        ask=r.ask,
        bid_size=r.bid_size,
        ask_size=r.ask_size,
        spread_pct=spread_pct(r.bid, r.ask),
        iv=r.iv,
        open_interest=r.open_interest,
        volume=r.volume,
    )


def snapshot_ticker(
    chain: DelayedChain,
    *,
    ticker: str,
    today: _dt.date,
    dte_window: tuple[int, int],
    fetched_at: str,
    strikes: int = MAX_STRIKES,
) -> ChainSnapshotPayload:
    """The ticker's ``chain_snapshot``; raises :class:`SnapshotSkipError`
    (``spot_missing`` / ``no_expiry_in_window``) when it cannot be built.

    Expiry: the nearest one with ``dte_min <= DTE <= dte_max``. Book: the *strikes*
    strikes nearest spot on it (ties -> lower strike), call + put each, by strike.
    """
    if not 1 <= strikes <= MAX_STRIKES:
        msg = f"strikes must be 1-{MAX_STRIKES}, got {strikes}"
        raise ValueError(msg)
    if chain.spot is None:
        raise SnapshotSkipError("spot_missing", ticker)
    lo, hi = dte_window
    expiries = sorted({r.expiry for r in chain.rows if lo <= (r.expiry - today).days <= hi})
    if not expiries:
        raise SnapshotSkipError("no_expiry_in_window", f"{ticker} {lo}-{hi} DTE")
    expiry = expiries[0]
    on_exp = [r for r in chain.rows if r.expiry == expiry]
    spot = chain.spot
    nearest = sorted({r.strike for r in on_exp}, key=lambda k: (abs(k - spot), k))[:strikes]
    picked = sorted(nearest)
    by_key = {(r.strike, r.option_type): r for r in on_exp}
    book = [
        _book_level(by_key[(k, t)]) for k in picked for t in ("call", "put") if (k, t) in by_key
    ]
    atm = nearest[0]
    atm_levels = [b for b in book if b.strike == atm]
    spreads = [b.spread_pct for b in atm_levels if b.spread_pct is not None]
    ois = [b.open_interest for b in atm_levels if b.open_interest is not None]
    calls = sum(r.volume for r in chain.rows if r.option_type == "call")
    puts = sum(r.volume for r in chain.rows if r.option_type == "put")
    return ChainSnapshotPayload(
        ticker=ticker,
        fetched_at=fetched_at,
        spot=spot,
        expiry=expiry.isoformat(),
        call_volume_td=calls,
        put_volume_td=puts,
        put_call_volume=round(puts / calls, 3) if calls > 0 else None,
        atm_spread_pct=round(sum(spreads) / len(spreads), 4) if spreads else None,
        atm_oi=sum(ois) if ois else None,
        book=book,
    )


# ---------------------------------------------------------------------------
# 3. Exchange symbol_data volume
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SymbolDataRow:
    underlying: str
    volume: int
    matched: int | None
    routed: int | None


def _csv_int(v: str | None) -> int | None:
    try:
        return int(float((v or "").strip()))
    except ValueError:
        return None


def parse_symbol_data_csv(text: str) -> list[SymbolDataRow]:
    """Cboe ``symbol_data`` CSV (one row per contract) -> rows with a volume.

    ``Symbol`` is the OCC root (adjusted roots such as ``ETHA1`` stay distinct from
    ``ETHA``). Rows without a symbol or a parseable volume are dropped.
    """
    reader = csv.DictReader(io.StringIO(text))
    fields = {(f or "").strip().lower(): f for f in reader.fieldnames or []}
    if "symbol" not in fields or "volume" not in fields:
        msg = f"not a symbol_data CSV (columns {sorted(fields)})"
        raise ValueError(msg)
    out: list[SymbolDataRow] = []
    for row in reader:
        sym = (row.get(fields["symbol"]) or "").strip().upper()
        vol = _csv_int(row.get(fields["volume"]))
        if not sym or vol is None or vol < 0:
            continue
        out.append(
            SymbolDataRow(
                underlying=sym,
                volume=vol,
                matched=_csv_int(row.get(fields.get("matched", ""), None)),
                routed=_csv_int(row.get(fields.get("routed", ""), None)),
            )
        )
    return out


def aggregate_by_underlying(rows: Iterable[SymbolDataRow], market: str) -> list[ExchangeVolumeRow]:
    """Sum volume / matched / routed per underlying; sorted by volume desc, then name.

    ``matched`` / ``routed`` are None for an underlying where any row lacked them.
    """
    acc: dict[str, list[Any]] = {}
    for r in rows:
        a = acc.setdefault(r.underlying, [0, 0, 0, 0])
        a[0] += r.volume
        a[1] = None if a[1] is None or r.matched is None else a[1] + r.matched
        a[2] = None if a[2] is None or r.routed is None else a[2] + r.routed
        a[3] += 1
    out = [
        ExchangeVolumeRow(
            underlying=u,
            market=market,  # type: ignore[arg-type]
            volume=a[0],
            matched=a[1],
            routed=a[2],
            contracts=a[3],
        )
        for u, a in acc.items()
    ]
    return sorted(out, key=lambda r: (-r.volume, r.underlying))


def select_exchange_rows(
    rows: Sequence[ExchangeVolumeRow],
    active: Sequence[str],
    *,
    top_others: int = TOP_OTHER_UNDERLYINGS,
    cap: int = 60,
) -> list[ExchangeVolumeRow]:
    """Active-list rows plus the top *top_others* other underlyings (by volume, per
    the input order), capped at *cap* rows (Tower/Ops visibility only)."""
    wanted = {t.upper() for t in active}
    mine = [r for r in rows if r.underlying in wanted]
    others: list[ExchangeVolumeRow] = []
    seen: set[str] = set()
    for r in rows:
        if r.underlying in wanted or r.underlying in seen:
            continue
        seen.add(r.underlying)
        others.append(r)
        if len(others) >= top_others:
            break
    return (mine + others)[:cap]


# ---------------------------------------------------------------------------
# Tape
# ---------------------------------------------------------------------------

_FLAG_TEXT = {
    "9d_over_30d": "VIX9D > VIX (front-end stress)",
    "backwardation_30d_3m": "VIX > VIX3M (backwardation)",
    "vix_gt_25": "VIX > 25",
    "vix_gt_35": "VIX > 35",
}


def _f(x: float | None, nd: int = 2) -> str:
    return "n/a" if x is None else f"{x:.{nd}f}"


def _k(n: int | None) -> str:
    if n is None:
        return "n/a"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 10_000:
        return f"{n / 1000:.0f}k"
    return f"{n:,}"


def vix_line(v: IndexVolsPayload, flips: Sequence[str] = ()) -> str:
    """One line: every quote, the two ratios, the active flags and any flips."""
    vix = next((q for q in v.quotes if q.symbol == "VIX"), v.quotes[0])
    t = vix.as_of[11:16] if len(vix.as_of) >= 16 else vix.as_of
    vals = " · ".join(f"{q.symbol} {q.value:.2f}" for q in v.quotes)
    line = (
        f"VIX complex (Cboe ~15-min delayed, {t} ET): {vals}"
        f" · 9D/30D {_f(v.ratio_9d_30d)} · 30D/3M {_f(v.ratio_30d_3m)}"
    )
    if v.flags:
        line += " · flags: " + ", ".join(_FLAG_TEXT[f] for f in v.flags)
    if flips:
        parts = [("NEW " if f[0] == "+" else "cleared ") + _FLAG_TEXT[f[1:]] for f in flips]
        line += " · ⚑ " + "; ".join(parts)
    return line


def ticker_line(s: ChainSnapshotPayload, exch: ExchangeVolumeRow | None = None) -> str:
    total = s.call_volume_td + s.put_volume_td
    parts = [
        f"{s.ticker} {_f(s.spot)}",
        f"vol {_k(total)} P/C {_f(s.put_call_volume)}",
        f"ATM {s.expiry[5:]} spread "
        + ("n/a" if s.atm_spread_pct is None else f"{s.atm_spread_pct * 100:.1f}%"),
        f"OI {_k(s.atm_oi)}",
    ]
    if exch is not None:
        parts.append(f"Cboe book {_k(exch.volume)}")
    return " · ".join(parts)


def _fresh(fetched_at: str, now: _dt.datetime, max_age: _dt.timedelta) -> bool:
    try:
        at = _dt.datetime.fromisoformat(fetched_at)
    except ValueError:
        return False
    if at.tzinfo is None:
        at = at.replace(tzinfo=ET)
    return _dt.timedelta(0) <= now - at <= max_age


NO_FRESH_TAPE = "Options tape: no fresh options tape (Cboe options_fast older than {age})."


def build_tape(
    index_vols: IndexVolsPayload | None,
    snapshots: Sequence[ChainSnapshotPayload],
    exchange_volume: ExchangeVolumePayload | None,
    previous: IndexVolsPayload | None,
    *,
    now: _dt.datetime,
    max_age: _dt.timedelta,
    max_chars: int = MAX_TAPE_CHARS,
) -> str:
    """The options tape block (at most *max_chars* characters).

    One VIX-complex line (flips vs *previous*), then one line per fresh ticker
    snapshot by session volume (desc). A part older than *max_age* at *now* is left
    out; nothing fresh at all -> the single ``no fresh options tape`` line (never a
    failure). Lines that do not fit are dropped and counted on a last line.
    """
    age = f"{int(max_age.total_seconds() // 60)}m"
    vix = index_vols if index_vols and _fresh(index_vols.fetched_at, now, max_age) else None
    snaps = [s for s in snapshots if _fresh(s.fetched_at, now, max_age)]
    exch_ok = exchange_volume is not None and _fresh(exchange_volume.fetched_at, now, max_age)
    exch = {r.underlying: r for r in exchange_volume.rows} if exch_ok and exchange_volume else {}
    if vix is None and not snaps:
        return NO_FRESH_TAPE.format(age=age)
    lines: list[str] = []
    if vix is not None:
        lines.append(vix_line(vix, detect_flips(previous, vix)))
    else:
        lines.append(f"VIX complex: no fresh quote (older than {age})")
    ordered = sorted(snaps, key=lambda s: (-(s.call_volume_td + s.put_volume_td), s.ticker))
    body = [ticker_line(s, exch.get(s.ticker)) for s in ordered]
    out = list(lines)
    used = sum(len(x) + 1 for x in out)
    for i, line in enumerate(body):
        left = len(body) - i
        tail = len(f"… +{left} more tickers") + 1
        if used + len(line) + 1 + (tail if left > 1 else 0) > max_chars:
            out.append(f"… +{left} more tickers")
            break
        out.append(line)
        used += len(line) + 1
    text = "\n".join(out)
    return text[:max_chars]


# ---------------------------------------------------------------------------
# CLI: `python -m arc.ingest.cboe_fast --tape --db <path> [--now ISO]`
# ---------------------------------------------------------------------------


def tape_from_store(conn: Any, now: _dt.datetime, max_age: _dt.timedelta) -> str:
    """:func:`build_tape` over the newest stored options_fast entries at *now*."""
    iv, snaps, ex, prev = _latest_parts(conn, now)
    return build_tape(iv, snaps, ex, prev, now=now, max_age=max_age)


# ---------------------------------------------------------------------------
# E13.10: the Scalp's tape input (always on since E13.15)
# ---------------------------------------------------------------------------

#: The ``Candidate.sources`` token (and corroboration source key) the tape adds.
TAPE_SOURCE = "options_fast:tape"

TapeDirection = Literal["bullish", "bearish", "neutral", "unknown"]


class TapeTicker(BaseModel):
    """One ticker's fresh chain snapshot as the Scalp's tape reads it."""

    model_config = ConfigDict(extra="forbid")

    pc_volume: float | None
    atm_spread_pct: float | None
    atm_oi: int | None
    direction: TapeDirection


class ScalpTape(BaseModel):
    """The rendered options tape one Scalp run read (kept on the run manifest)."""

    model_config = ConfigDict(extra="forbid")

    as_of: str
    vix: float | None
    vix9d: float | None
    vxn: float | None
    flags: list[str]
    tickers: dict[str, TapeTicker]
    text: str = Field(max_length=MAX_TAPE_CHARS)

    @property
    def present(self) -> bool:
        """A fresh tape was shown (the index vols were within ``max_age``)."""
        return self.vix is not None


def tape_direction(
    pc_volume: float | None, *, bull: float = 0.7, bear: float = 1.3
) -> TapeDirection:
    """Session P/C volume -> direction: ``<= bull`` bullish, ``>= bear`` bearish."""
    if pc_volume is None:
        return "unknown"
    if pc_volume <= bull:
        return "bullish"
    if pc_volume >= bear:
        return "bearish"
    return "neutral"


def tape_corroborates(stance: str, direction: TapeDirection | None) -> bool:
    """A ticker's tape corroborates a candidate only in the candidate's direction."""
    return stance in ("bullish", "bearish") and direction == stance


def _age_text(fetched_at: str | None, now: _dt.datetime) -> str:
    if not fetched_at:
        return "none stored"
    try:
        at = _dt.datetime.fromisoformat(fetched_at)
    except ValueError:
        return "unreadable"
    at = at.replace(tzinfo=ET) if at.tzinfo is None else at
    mins = int((now - at).total_seconds() // 60)
    if mins < 0:
        return "in the future"
    return f"{mins // 60}h{mins % 60:02d}m" if mins >= 60 else f"{mins}m"


def scalp_tape(
    index_vols: IndexVolsPayload | None,
    snapshots: Sequence[ChainSnapshotPayload],
    exchange_volume: ExchangeVolumePayload | None,
    previous: IndexVolsPayload | None,
    *,
    now: _dt.datetime,
    max_age: _dt.timedelta,
    max_chars: int = MAX_TAPE_CHARS,
    pc_bull: float = 0.7,
    pc_bear: float = 1.3,
) -> ScalpTape:
    """The Scalp's tape: :func:`build_tape` text plus the per-ticker directions.

    Shown only when ``index_vols`` is within *max_age* of *now*; otherwise the one
    line ``Options tape: no fresh info (age …)`` and no tickers (no corroboration).
    """
    if index_vols is None or not _fresh(index_vols.fetched_at, now, max_age):
        age = _age_text(index_vols.fetched_at if index_vols else None, now)
        return ScalpTape(
            as_of=index_vols.fetched_at if index_vols else "",
            vix=None,
            vix9d=None,
            vxn=None,
            flags=[],
            tickers={},
            text=f"Options tape: no fresh info (age {age})",
        )
    px = {q.symbol: q.value for q in index_vols.quotes}
    fresh = [s for s in snapshots if _fresh(s.fetched_at, now, max_age)]
    return ScalpTape(
        as_of=index_vols.fetched_at,
        vix=px.get("VIX"),
        vix9d=px.get("VIX9D"),
        vxn=px.get("VXN"),
        flags=list(index_vols.flags),
        tickers={
            s.ticker: TapeTicker(
                pc_volume=s.put_call_volume,
                atm_spread_pct=s.atm_spread_pct,
                atm_oi=s.atm_oi,
                direction=tape_direction(s.put_call_volume, bull=pc_bull, bear=pc_bear),
            )
            for s in sorted(fresh, key=lambda s: s.ticker)
        },
        text=build_tape(
            index_vols,
            fresh,
            exchange_volume,
            previous,
            now=now,
            max_age=max_age,
            max_chars=max_chars,
        ),
    )


def _latest_parts(
    conn: Any, now: _dt.datetime
) -> tuple[
    IndexVolsPayload | None,
    list[ChainSnapshotPayload],
    ExchangeVolumePayload | None,
    IndexVolsPayload | None,
]:
    """The newest stored options_fast entries visible at *now* (+ the previous VIX)."""
    from arc.context.store import ContextStore
    from arc.context.ttl import to_db

    entries = ContextStore(conn).query(
        as_of=now, kinds=["index_vols", "chain_snapshot", "exchange_volume"]
    )
    latest: dict[tuple[str, str], Any] = {}
    for e in entries:
        latest[(e.kind, e.subject)] = e
    iv = latest.get(("index_vols", "market"))
    ex = latest.get(("exchange_volume", "market"))
    prev = None
    if iv is not None:
        row = conn.execute(
            "SELECT payload FROM context_entries WHERE kind = 'index_vols' AND id != ? "
            "AND created_at <= ? ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (iv.id, to_db(iv.created_at)),
        ).fetchone()
        prev = IndexVolsPayload.model_validate(json.loads(row[0])) if row else None
    snaps = [
        ChainSnapshotPayload.model_validate(e.payload)
        for (k, _), e in sorted(latest.items())
        if k == "chain_snapshot"
    ]
    return (
        IndexVolsPayload.model_validate(iv.payload) if iv else None,
        snaps,
        ExchangeVolumePayload.model_validate(ex.payload) if ex else None,
        prev,
    )


def scalp_tape_from_store(
    conn: Any,
    now: _dt.datetime,
    max_age: _dt.timedelta,
    *,
    max_chars: int = MAX_TAPE_CHARS,
    pc_bull: float = 0.7,
    pc_bear: float = 1.3,
) -> ScalpTape:
    """:func:`scalp_tape` over the newest stored options_fast entries at *now*.

    *now* is the Scalp tick's real time, never a stored ``fetched_at``.
    """
    iv, snaps, ex, prev = _latest_parts(conn, now)
    return scalp_tape(
        iv,
        snaps,
        ex,
        prev,
        now=now,
        max_age=max_age,
        max_chars=max_chars,
        pc_bull=pc_bull,
        pc_bear=pc_bear,
    )


def main(argv: Sequence[str] | None = None) -> int:
    from arc.context.categories import SourceCategory
    from arc.routines.config import load_routines
    from arc.store.db import connect_ro

    ap = argparse.ArgumentParser(prog="python -m arc.ingest.cboe_fast")
    ap.add_argument("--tape", action="store_true", help="print the options tape block")
    ap.add_argument("--db", default=None, help="SQLite path (default: data/arc.db)")
    ap.add_argument("--now", default=None, help="ISO time (default: the newest index_vols write)")
    ap.add_argument("--config", default=None, help="routines YAML (category max_age)")
    args = ap.parse_args(argv)
    if not args.tape:
        ap.error("nothing to do (pass --tape)")
    max_age = load_routines(args.config).category_spec(SourceCategory.OPTIONS_FAST).max_age
    td = max_age.duration or _dt.timedelta(minutes=30)
    conn = connect_ro(args.db)  # read-only: the tape CLI never writes
    try:
        if args.now:
            now = _dt.datetime.fromisoformat(args.now).astimezone(ET)
        else:
            # The newest index_vols write time (``valid_from`` = the writing tick's
            # sub-second ``now``), not its ``fetched_at``: that one is truncated to the
            # second, so querying at it hides every entry written later in that same
            # second (E13.10).
            from arc.context.ttl import from_db

            row = conn.execute(
                "SELECT max(valid_from) FROM context_entries WHERE kind = 'index_vols'"
            ).fetchone()
            now = from_db(row[0]) if row and row[0] else _dt.datetime.now(ET)
        sys.stdout.write(tape_from_store(conn, now, td) + "\n")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
