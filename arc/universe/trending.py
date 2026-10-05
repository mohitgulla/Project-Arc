"""Trending tier (D51, card E12.3): daily rules-based list of what is popular now.

No LLM, and **no Alpaca ranking input** (owner rule): Alpaca may only supply the
admission liquidity screen. Each configured input (``config/routines.yaml``
``universe.trending`` → ``trending.inputs``) gives every eligible ticker a score in
``(0, 1]``, rank-normalised within that input (:func:`rank_normalise`). Input types
(adding another input of a known type, or removing one, is a config change only):

* ``news`` — distinct ``source_key`` values mentioning the ticker in ``raw_docs``
  over the last ``lookback_sessions`` sessions. Tickers are re-extracted from each
  document with today's :class:`~arc.universe.ingest.IngestUniverse` rules (so the
  E12.3 extraction fix applies to docs stored before it); an EDGAR filing counts its
  filer only, and every EDGAR filing is one source (``edgar``).
* ``apewisdom`` — Reddit mentions (ApeWisdom, pages from ``urls``): the mean of the
  rank-normalised mentions and 24 h rank gain.
* ``stocktwits`` — Stocktwits trending symbols (``trending_score``); crypto and
  non-US symbols dropped.
* ``scout`` — the Scout's ``candidates`` rows over the last ``lookback_sessions``
  sessions, each weighted by its ``corroboration`` (at least 1).

Rules (:func:`rank_trending`, pure):

* Eligible = in the symbol master, optionable and tradable at the broker. ETFs are
  allowed; the market reference (SPY, QQQ) is not (D51: it left the trade universe).
* ``trend_score`` = the sum of a ticker's input scores divided by the number of
  **enabled** inputs. A ticker absent from an input, or an input with no fresh data
  (failed fetch, nothing within ``max_age``), contributes 0; the other inputs are
  never renormalised (owner rule: no new data = no info).
* A name needs at least ``min_inputs`` (2) inputs; one-source names are rejected
  (``universe:trending_single_input``). Core and momentum names (and the market
  reference) are excluded before the cut, so the tier adds new names.
* Order: ``trend_score`` desc, then more inputs, then ticker. The first ``pool``
  (40) are screened with the tier's profile (``tiers.trending.screen``, relaxed);
  the first ``size`` (``universe_trending_size``, 25) passes are the tier.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

import structlog
from pydantic import BaseModel, ConfigDict, Field

from arc.universe.master import normalize_symbol
from arc.universe.trending_config import (
    INPUT_TYPES,
    TrendingConfig,
    TrendingError,
    TrendingInput,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from arc.universe.master import SymbolMaster
    from arc.universe.screen import ScreenResult
    from arc.universe.tiers import UniverseTierPayload

log = structlog.get_logger(__name__)

__all__ = [
    "INPUT_TYPES",
    "TrendingConfig",
    "TrendingError",
    "TrendingInput",
    "TrendingResult",
    "TrendingRow",
    "InputResult",
    "build_payload",
    "eligible",
    "gather_inputs",
    "journal_decisions",
    "notice_line",
    "previous_members",
    "rank_normalise",
    "rank_trending",
    "run_trending",
    "screen_pool",
    "table",
]

_HINT_SCAN_CHARS = 20_000  # same head-of-document budget as EDGAR hints

# Decision rows (journal) are capped per run: single-input rejects are journaled only
# for names that would have made the screened pool on score alone.
EXCLUDED_CORE = "core"
EXCLUDED_MOMENTUM = "momentum"
EXCLUDED_REFERENCE = "market_reference"


# ---------------------------------------------------------------------------
# Pure scoring
# ---------------------------------------------------------------------------


def rank_normalise(raw: Mapping[str, float]) -> dict[str, float]:
    """Scores in ``(0, 1]`` from raw values (higher = better), by rank.

    The best value scores 1, the worst ``1/n``; tied values share the mean of their
    ranks, so they score equally. Deterministic and scale-free.
    """
    n = len(raw)
    if n == 0:
        return {}
    ordered = sorted(raw.items(), key=lambda kv: (-kv[1], kv[0]))
    out: dict[str, float] = {}
    i = 0
    while i < n:
        j = i
        while j + 1 < n and ordered[j + 1][1] == ordered[i][1]:
            j += 1
        mean_rank = (i + j) / 2 + 1  # 1-based
        score = (n - mean_rank + 1) / n
        for k in range(i, j + 1):
            out[ordered[k][0]] = round(score, 6)
        i = j + 1
    return out


@dataclass
class InputResult:
    """What one input produced: per-ticker scores (eligible names only) + audit data."""

    name: str
    type: str
    status: Literal["ok", "stale", "failed", "empty"]
    raw: dict[str, float] = field(default_factory=dict)
    detail: dict[str, str] = field(default_factory=dict)  # ticker -> reason fragment
    error: str | None = None
    urls: list[str] = field(default_factory=list)
    digest: str = ""
    count: int = 0  # rows/symbols read before eligibility
    newest: _dt.datetime | None = None
    dropped: dict[str, str] = field(default_factory=dict)  # ticker -> why not eligible

    @property
    def live(self) -> bool:
        return self.status == "ok" and bool(self.raw)

    def scores(self) -> dict[str, float]:
        return rank_normalise(self.raw) if self.live else {}


class TrendingRow(BaseModel):
    """One ranked name: its input scores, trend score and outcome."""

    model_config = ConfigDict(extra="forbid")

    ticker: str
    scores: dict[str, float] = Field(default_factory=dict)  # input name -> (0, 1]
    details: dict[str, str] = Field(default_factory=dict)
    trend_score: float
    n_inputs: int
    excluded: str | None = None  # core | momentum | market_reference
    screen_passed: bool | None = None
    screen_detail: str = ""
    admitted: bool = False
    rank: int | None = None  # rank in the tier (admitted only)

    def reason(self, order: Sequence[str]) -> str:
        """``reddit #3 (+41 24h), stocktwits #5, 4 news sources`` in input order."""
        return ", ".join(self.details[n] for n in order if n in self.details)


#: E12.3: exchanges a trending name may list on (the symbol master's labels).
LISTED_EXCHANGES: frozenset[str] = frozenset({"NASDAQ", "NYSE", "ARCA", "BATS", "CBOE", "AMEX"})


def eligible(sym: str, master: SymbolMaster) -> str | None:
    """``None`` when *sym* may be a trending name, else why not.

    The relaxed screen's ``min_price`` is checked by the screen itself (admission).
    """
    info = master.get(sym)
    if info is None:
        return "not in the symbol master"
    if info.tradable is False:
        return "not tradable at the broker"
    if info.options is not True:
        return "no listed options at the broker"
    if (info.exchange or "").upper() not in LISTED_EXCHANGES:
        return f"not on a listed exchange ({info.exchange or 'unknown'})"
    return None


def _order_key(r: TrendingRow) -> tuple[float, int, str]:
    return (-r.trend_score, -r.n_inputs, r.ticker)


def rank_trending(
    inputs: Sequence[InputResult],
    *,
    enabled_count: int,
    exclude: Mapping[str, str],
    min_inputs: int,
) -> tuple[list[TrendingRow], list[TrendingRow], list[TrendingRow]]:
    """``(ranked, excluded, single_input)``, each in score order (pure).

    ``ranked`` = eligible names with at least *min_inputs* inputs, not excluded.
    ``trend_score`` = sum of input scores / *enabled_count* (no renormalisation).
    """
    per: dict[str, dict[str, float]] = {}
    details: dict[str, dict[str, str]] = {}
    for res in inputs:
        for sym, score in res.scores().items():
            per.setdefault(sym, {})[res.name] = score
            if sym in res.detail:
                details.setdefault(sym, {})[res.name] = res.detail[sym]
    denom = max(enabled_count, 1)
    rows = [
        TrendingRow(
            ticker=sym,
            scores=s,
            details=details.get(sym, {}),
            trend_score=round(sum(s.values()) / denom, 6),
            n_inputs=len(s),
            excluded=exclude.get(sym),
        )
        for sym, s in per.items()
    ]
    rows.sort(key=_order_key)
    excluded = [r for r in rows if r.excluded]
    single = [r for r in rows if not r.excluded and r.n_inputs < min_inputs]
    ranked = [r for r in rows if not r.excluded and r.n_inputs >= min_inputs]
    return ranked, excluded, single


def screen_pool(
    ranked: Sequence[TrendingRow],
    *,
    pool: int,
    size: int,
    screen: Callable[[str], ScreenResult] | None,
) -> list[TrendingRow]:
    """Screen the first *pool* names in order; the first *size* passes are admitted.

    *screen* ``None`` = no screen (dry runs): the first *size* are admitted unscreened.
    Returns the pool rows (updated copies) in score order.
    """
    out: list[TrendingRow] = []
    admitted = 0
    for row in ranked[:pool]:
        upd: dict[str, Any] = {}
        if screen is not None:
            res = screen(row.ticker)
            upd |= {"screen_passed": res.passed, "screen_detail": res.detail()}
            ok = res.passed
        else:
            ok = True
        if ok and admitted < size:
            admitted += 1
            upd |= {"admitted": True, "rank": admitted}
        out.append(row.model_copy(update=upd))
    return out


# ---------------------------------------------------------------------------
# Inputs (I/O)
# ---------------------------------------------------------------------------


def _sessions_back(today: _dt.date, n: int) -> list[_dt.date]:
    """The last *n* sessions ending at *today* (when a session) or the one before it."""
    from arc.utils.calendar import is_session, previous_session

    cur = today if is_session(today) else previous_session(today)
    out = [cur]
    while len(out) < n:
        cur = previous_session(cur)
        out.append(cur)
    return out


def _window_start(today: _dt.date, n: int) -> _dt.datetime:
    from arc.utils.calendar import ET

    first = _sessions_back(today, n)[-1]
    return _dt.datetime(first.year, first.month, first.day, tzinfo=ET)


def _parse_ts(raw: str | None) -> _dt.datetime | None:
    if not raw:
        return None
    try:
        ts = _dt.datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if ts.tzinfo is None:
        from arc.utils.calendar import ET

        ts = ts.replace(tzinfo=ET)
    return ts


def _stale(res: InputResult, spec: TrendingInput, now: _dt.datetime) -> InputResult:
    if (
        res.status == "ok"
        and spec.max_age is not None
        and (res.newest is None or now - res.newest > spec.max_age)
    ):
        newest = res.newest.isoformat() if res.newest else "none"
        res.status = "stale"
        res.error = f"newest row {newest} older than {spec.max_age}"
    if res.status == "ok" and not res.raw:
        res.status = "empty"
    return res


def _eligible_filter(res: InputResult, master: SymbolMaster) -> None:
    for sym in list(res.raw):
        why = eligible(sym, master)
        if why is not None:
            res.dropped[sym] = why
            res.raw.pop(sym)
            res.detail.pop(sym, None)


def news_input(
    name: str,
    spec: TrendingInput,
    *,
    conn: sqlite3.Connection,
    now: _dt.datetime,
    tickers_in: Callable[[str], list[str]],
    key_for: Callable[[Mapping[str, Any]], str],
) -> InputResult:
    """Distinct registry sources per ticker over the lookback (EDGAR = one source)."""
    from arc.utils.calendar import ET

    start = _window_start(now.astimezone(ET).date(), spec.lookback_sessions)
    lo = (start - _dt.timedelta(days=1)).astimezone(_dt.UTC).isoformat()
    excl = list(spec.exclude_sources)
    marks = ",".join("?" * len(excl))
    sql = (
        "SELECT source, source_key, url, channel_id, text, tickers_hint, published_at "
        "FROM raw_docs WHERE published_at >= ?" + (f" AND source NOT IN ({marks})" if excl else "")
    )
    res = InputResult(name=name, type="news", status="ok")
    try:
        rows = conn.execute(sql, (lo, *excl)).fetchall()
    except sqlite3.OperationalError as exc:
        return InputResult(name=name, type="news", status="failed", error=str(exc))
    keys: dict[str, set[str]] = {}
    for r in rows:
        ts = _parse_ts(r[6])
        if ts is None or ts < start or ts > now:
            continue
        res.count += 1
        res.newest = ts if res.newest is None or ts > res.newest else res.newest
        row = {"source": r[0], "source_key": r[1], "url": r[2], "channel_id": r[3]}
        if r[0] == "edgar":
            key = "edgar"
            hints = json.loads(r[5] or "[]")
            syms = hints[:1]  # the filer only; filing boilerplate is not a mention
        else:
            key = key_for(row)
            syms = tickers_in(str(r[4] or "")[:_HINT_SCAN_CHARS])
        for sym in syms:
            keys.setdefault(normalize_symbol(sym), set()).add(key)
    res.raw = {s: float(len(k)) for s, k in keys.items()}
    res.detail = {s: f"{len(k)} news source" + ("s" if len(k) > 1 else "") for s, k in keys.items()}
    return _stale(res, spec, now)


def scout_input(
    name: str, spec: TrendingInput, *, conn: sqlite3.Connection, now: _dt.datetime
) -> InputResult:
    """Scout candidates over the lookback, each row weighted by its corroboration."""
    from arc.utils.calendar import ET

    days = [
        d.isoformat() for d in _sessions_back(now.astimezone(ET).date(), spec.lookback_sessions)
    ]
    res = InputResult(name=name, type="scout", status="ok")
    try:
        rows = conn.execute(
            "SELECT ticker, corroboration, COALESCE(updated_at, created_at) FROM candidates "
            f"WHERE day IN ({','.join('?' * len(days))})",
            days,
        ).fetchall()
    except sqlite3.OperationalError as exc:
        return InputResult(name=name, type="scout", status="failed", error=str(exc))
    weight: dict[str, float] = {}
    n_rows: dict[str, int] = {}
    for r in rows:
        ts = _parse_ts(r[2])
        if ts is not None and ts > now:
            continue
        res.count += 1
        if ts is not None and (res.newest is None or ts > res.newest):
            res.newest = ts
        sym = normalize_symbol(str(r[0]))
        weight[sym] = weight.get(sym, 0.0) + max(1, int(r[1] or 0))
        n_rows[sym] = n_rows.get(sym, 0) + 1
    res.raw = weight
    res.detail = {s: f"scout {n_rows[s]}d (corr {int(w)})" for s, w in weight.items()}
    return _stale(res, spec, now)


def _fetch_pages(spec: TrendingInput, get: Callable[[str], bytes]) -> tuple[list[Any], str]:
    blobs = [get(u) for u in spec.urls]
    digest = hashlib.sha256(b"\n".join(blobs)).hexdigest()
    return [json.loads(b.decode("utf-8")) for b in blobs], digest


def apewisdom_input(
    name: str, spec: TrendingInput, *, get: Callable[[str], bytes], now: _dt.datetime
) -> InputResult:
    """Reddit (ApeWisdom): mean of rank-normalised mentions and 24 h rank gain."""
    res = InputResult(name=name, type="apewisdom", status="ok", urls=list(spec.urls))
    try:
        pages, res.digest = _fetch_pages(spec, get)
        items = [it for p in pages for it in p["results"]]
    except Exception as exc:  # noqa: BLE001 - one input failing contributes nothing
        res.status, res.error = "failed", f"{type(exc).__name__}: {exc}"[:300]
        return res
    res.count = len(items)
    res.newest = now
    best: dict[str, dict[str, Any]] = {}
    for it in items:
        sym = normalize_symbol(str(it.get("ticker") or ""))
        if not sym or sym in best:
            continue
        best[sym] = it
    n = len(best)
    mentions = {s: float(it.get("mentions") or 0) for s, it in best.items()}

    def gain(it: Mapping[str, Any]) -> float:
        rank = int(it.get("rank") or n)
        prev = it.get("rank_24h_ago")
        return float((int(prev) if prev else n + 1) - rank)

    gains = {s: gain(it) for s, it in best.items()}
    m_norm, g_norm = rank_normalise(mentions), rank_normalise(gains)
    res.raw = {s: (m_norm[s] + g_norm[s]) / 2 for s in best}
    for s, it in best.items():
        g = int(gains[s])
        res.detail[s] = f"reddit #{it.get('rank')} ({g:+d} 24h)"
    return res


def stocktwits_input(
    name: str, spec: TrendingInput, *, get: Callable[[str], bytes], now: _dt.datetime
) -> InputResult:
    """Stocktwits trending symbols by ``trending_score`` (crypto and non-US dropped)."""
    res = InputResult(name=name, type="stocktwits", status="ok", urls=list(spec.urls))
    try:
        pages, res.digest = _fetch_pages(spec, get)
        items = [it for p in pages for it in p["symbols"]]
    except Exception as exc:  # noqa: BLE001 - one input failing contributes nothing
        res.status, res.error = "failed", f"{type(exc).__name__}: {exc}"[:300]
        return res
    res.count = len(items)
    res.newest = now
    for pos, it in enumerate(items, 1):
        sym = normalize_symbol(str(it.get("symbol") or ""))
        if not sym or sym in res.raw:
            continue
        exch = str(it.get("exchange") or "").upper()
        region = str(it.get("region") or "US").upper()
        if exch == "CRYPTO" or region != "US":
            res.dropped[sym] = f"{exch or region} symbol"
            continue
        score = it.get("trending_score")
        res.raw[sym] = float(score) if score is not None else float(len(items) - pos + 1)
        res.detail[sym] = f"stocktwits #{it.get('rank') or pos}"
    return res


def gather_inputs(
    cfg: TrendingConfig,
    *,
    conn: sqlite3.Connection,
    now: _dt.datetime,
    master: SymbolMaster,
    get: Callable[[str], bytes],
    tickers_in: Callable[[str], list[str]],
    key_for: Callable[[Mapping[str, Any]], str],
) -> list[InputResult]:
    """Run every enabled input and keep eligible names only (each input independent)."""
    out: list[InputResult] = []
    for name, spec in cfg.enabled.items():
        if spec.type == "news":
            res = news_input(name, spec, conn=conn, now=now, tickers_in=tickers_in, key_for=key_for)
        elif spec.type == "scout":
            res = scout_input(name, spec, conn=conn, now=now)
        elif spec.type == "apewisdom":
            res = apewisdom_input(name, spec, get=get, now=now)
        else:
            res = stocktwits_input(name, spec, get=get, now=now)
        _eligible_filter(res, master)
        if res.status == "ok" and not res.raw:
            res.status = "empty"
        log.info(
            "universe.trending.input",
            input=name,
            type=spec.type,
            status=res.status,
            names=len(res.raw),
            read=res.count,
            error=res.error,
        )
        out.append(res)
    return out


# ---------------------------------------------------------------------------
# One run
# ---------------------------------------------------------------------------


@dataclass
class TrendingResult:
    """Everything one run computed (the payload, the table, the journal rows)."""

    as_of: _dt.date
    inputs: list[InputResult]
    order: list[str]  # enabled input names, config order
    pool: list[TrendingRow]  # screened (or unscreened, dry run) pool, score order
    ranked: list[TrendingRow]
    excluded: list[TrendingRow]
    single_input: list[TrendingRow]
    size: int
    pool_size: int
    screened: bool

    @property
    def members(self) -> list[TrendingRow]:
        return sorted((r for r in self.pool if r.admitted), key=lambda r: r.rank or 0)

    @property
    def tickers(self) -> list[str]:
        return [r.ticker for r in self.members]

    @property
    def failed_inputs(self) -> dict[str, str]:
        return {i.name: f"{i.status}: {i.error or 'no names'}" for i in self.inputs if not i.live}

    @property
    def single_input_journaled(self) -> list[TrendingRow]:
        """Single-input rejects that would have made the pool on score alone."""
        if not self.pool:
            return self.single_input[: self.pool_size]
        floor = self.pool[-1].trend_score if len(self.pool) >= self.pool_size else 0.0
        return [r for r in self.single_input if r.trend_score >= floor][: self.pool_size]

    def digest(self) -> str:
        return hashlib.sha256(
            "|".join(f"{i.name}:{i.digest or i.count}" for i in self.inputs).encode()
        ).hexdigest()


def run_trending(
    cfg: TrendingConfig,
    *,
    conn: sqlite3.Connection,
    now: _dt.datetime,
    master: SymbolMaster | None,
    size: int,
    exclude: Mapping[str, str],
    get: Callable[[str], bytes],
    tickers_in: Callable[[str], list[str]],
    key_for: Callable[[Mapping[str, Any]], str],
    screen: Callable[[str], ScreenResult] | None,
) -> TrendingResult:
    """Gather, rank, exclude, screen. Raises :class:`TrendingError` when the tier
    cannot be built (no symbol master, or fewer live inputs than ``min_inputs``)."""
    from arc.utils.calendar import ET

    if master is None:
        msg = "symbol master unavailable (run `arc universe refresh`); trending fails closed"
        raise TrendingError(msg)
    enabled = cfg.enabled
    inputs = gather_inputs(
        cfg,
        conn=conn,
        now=now,
        master=master,
        get=get,
        tickers_in=tickers_in,
        key_for=key_for,
    )
    live = [i for i in inputs if i.live]
    if len(live) < cfg.min_inputs:
        detail = "; ".join(f"{i.name}: {i.status} ({i.error or 'no names'})" for i in inputs)
        msg = f"only {len(live)} live trending inputs (< {cfg.min_inputs}): {detail}"
        raise TrendingError(msg)
    ranked, excluded, single = rank_trending(
        inputs, enabled_count=len(enabled), exclude=exclude, min_inputs=cfg.min_inputs
    )
    pool = screen_pool(ranked, pool=cfg.pool, size=size, screen=screen)
    log.info(
        "universe.trending.excluded",
        overlaps={r.ticker: r.excluded for r in excluded},
        single_input=len(single),
        ranked=len(ranked),
    )
    return TrendingResult(
        as_of=now.astimezone(ET).date(),
        inputs=inputs,
        order=list(enabled),
        pool=pool,
        ranked=ranked,
        excluded=excluded,
        single_input=single,
        size=size,
        pool_size=cfg.pool,
        screened=screen is not None,
    )


def build_payload(res: TrendingResult, *, now: _dt.datetime) -> UniverseTierPayload:
    """The ``universe_tier`` (subject ``trending``) entry for *res*."""
    from arc.universe.tiers import Tier, TierMember, UniverseTierPayload

    members = [
        TierMember(
            ticker=r.ticker,
            tier=Tier.TRENDING,
            rank=r.rank or i,
            source="+".join(n for n in res.order if n in r.scores),
            reason=f"{r.reason(res.order)} · score {r.trend_score:.2f}",
            as_of=res.as_of,
        )
        for i, r in enumerate(res.members, 1)
    ]
    return UniverseTierPayload(
        tier=Tier.TRENDING,
        members=members,
        fetched_at=now,
        source="rules:" + "+".join(i.name for i in res.inputs if i.live),
        source_as_of=res.as_of,
        digest=res.digest(),
        url=" ".join(u for i in res.inputs for u in i.urls),
        # an enabled input contributed nothing today: the list is built on fewer inputs
        partial=bool(res.failed_inputs),
    )


def notice_line(res: TrendingResult, previous: Sequence[str] | None) -> str:
    """``Trending tier: 25 names (+SPCX +LULU −RIVN) · inputs news, reddit, … · …``."""
    if previous is None:
        change = "first list"
    else:
        prev, cur = set(previous), set(res.tickers)
        moves = [f"+{t}" for t in res.tickers if t not in prev] + [
            f"\u2212{t}" for t in previous if t not in cur
        ]
        change = " ".join(moves[:12]) + (" …" if len(moves) > 12 else "") if moves else "no change"
    live = [i.name for i in res.inputs if i.live]
    parts = [f"Trending tier: {len(res.members)} names ({change})", f"inputs {', '.join(live)}"]
    if res.failed_inputs:
        parts.append("no data: " + ", ".join(res.failed_inputs))
    fails = sum(1 for r in res.pool if r.screen_passed is False)
    if fails:
        parts.append(f"{fails} failed the screen")
    return " · ".join(parts)


def previous_members(conn: sqlite3.Connection) -> list[str] | None:
    """Tickers of the latest trending entry written (any status); ``None`` = never."""
    try:
        row = conn.execute(
            "SELECT payload FROM context_entries WHERE kind = 'universe_tier' "
            "AND subject = 'trending' ORDER BY valid_from DESC, rowid DESC LIMIT 1"
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    if row is None:
        return None
    from arc.universe.tiers import UniverseTierPayload

    return [m.ticker for m in UniverseTierPayload.model_validate_json(row[0]).members]


def journal_decisions(
    conn: sqlite3.Connection,
    res: TrendingResult,
    *,
    at: _dt.datetime,
    run_id: str | None = None,
    chain_run_id: str | None = None,
) -> dict[str, int]:
    """One ``candidate``-stage decision per admission / screen fail / single-input reject.

    Idempotent per ET day, reason code and ticker (a manual re-run journals only new
    outcomes). Returns the number of rows written per reason code.
    """
    from arc.context.ttl import to_db
    from arc.journal.reasons import Choice, JournalPersona, ReasonCode, Stage
    from arc.journal.store import JournalStore
    from arc.utils.calendar import ET

    day = res.as_of
    start = _dt.datetime(day.year, day.month, day.day, tzinfo=ET)
    codes = (
        ReasonCode.UNIVERSE_TRENDING_ADMITTED,
        ReasonCode.UNIVERSE_TRENDING_SCREEN_FAIL,
        ReasonCode.UNIVERSE_TRENDING_SINGLE_INPUT,
    )
    done = {
        (r[0], r[1])
        for r in conn.execute(
            f"SELECT reason_code, subject FROM decisions WHERE reason_code IN "
            f"({','.join('?' * len(codes))}) AND at >= ? AND at < ?",
            (*(c.value for c in codes), to_db(start), to_db(start + _dt.timedelta(days=1))),
        ).fetchall()
    }
    rows: list[tuple[ReasonCode, Choice, TrendingRow, str]] = []
    for r in res.pool:
        base = f"{r.reason(res.order)}; trend score {r.trend_score:.2f}"
        if r.admitted:
            rows.append(
                (
                    ReasonCode.UNIVERSE_TRENDING_ADMITTED,
                    Choice.SELECTED,
                    r,
                    f"trending #{r.rank}: {base}",
                )
            )
        elif r.screen_passed is False:
            rows.append(
                (
                    ReasonCode.UNIVERSE_TRENDING_SCREEN_FAIL,
                    Choice.REJECTED,
                    r,
                    f"{base}; screen: {r.screen_detail}",
                )
            )
    for r in res.single_input_journaled:
        rows.append(
            (
                ReasonCode.UNIVERSE_TRENDING_SINGLE_INPUT,
                Choice.REJECTED,
                r,
                f"only 1 input ({r.reason(res.order)}); needs 2",
            )
        )
    store = JournalStore(conn)
    counts = {c.value: 0 for c in codes}
    with conn:
        for code, choice, r, text in rows:
            if (code.value, r.ticker) in done:
                continue
            store.record(
                persona=JournalPersona.SYSTEM,
                stage=Stage.CANDIDATE,
                subject=r.ticker,
                choice=choice,
                reason_code=code,
                reason_text=text[:500],
                confidence=r.trend_score,
                at=at,
                run_id=run_id,
                chain_run_id=chain_run_id,
                payload={
                    "tier": "trending",
                    "as_of": day.isoformat(),
                    "scores": r.scores,
                    "trend_score": r.trend_score,
                    "rank": r.rank,
                    "screen": r.screen_detail or None,
                },
            )
            counts[code.value] += 1
    return counts


def table(res: TrendingResult, *, limit: int | None = None) -> list[dict[str, Any]]:
    """The per-input scores table (pool first, in score order), JSON-friendly."""
    rows = res.pool if limit is None else res.pool[:limit]
    return [
        {
            "ticker": r.ticker,
            **{n: r.scores.get(n) for n in res.order},
            "trend_score": r.trend_score,
            "inputs": r.n_inputs,
            "screen": ("-" if r.screen_passed is None else ("pass" if r.screen_passed else "fail")),
            "screen_detail": r.screen_detail,
            "admitted": r.admitted,
            "rank": r.rank,
            "reason": r.reason(res.order),
        }
        for r in rows
    ]
