"""``ticker_news`` source (E14.1, D60): ticker-tagged news scoped to the active list.

Inputs come from ``config/routines.yaml`` → ``sources.ticker_news.inputs`` (see
:mod:`arc.ingest.ticker_news_config`):

* ``alpaca_news`` (primary): Alpaca ``/v1beta1/news`` (Benzinga, free on the existing
  key). Symbols are chunked (≤ 50 per call) and each chunk's ``next_page_token`` walk
  is followed to the end. ``start`` is the input's cursor (``ingest_cursors`` row
  ``ticker_news:<input>``), never older than the category's ``max_age``. The cursor
  advances only after every chunk's full walk succeeded.
* ``finnhub_company_news`` (fallback, ``enabled_when: primary_failed``): Finnhub
  ``/company-news`` per ticker through the shared cross-process Finnhub budget, only
  for the tickers whose primary call failed this run.

One ``raw_docs`` row per article: ``source='ticker_news'``, ``source_key`` =
``ticker_news.<input>`` (one registry source per input, in ``company_data``),
``title`` = headline, ``text`` = headline + summary, ``published_at`` = the article's
``created_at`` and ``tickers_hint`` = the provider's symbols ∩ the scope (active list ∪
open underlyings). The provider tags, never a regex. A URL already stored from any
source (RSS included) is dropped; the content hash stops the same story twice. An
article with no scope ticker after the intersection is stored closed
``scalp_status='filtered'`` (counted, never read).

HTTP outcomes: no key → the input is ``no_api_key``; 401/403 → ``forbidden``; 429 →
one retry after ``Retry-After`` (default ``retry_after_default_s``), then
``rate_limited``. The handler maps those to the run status (never ``ok · 0``).
"""

from __future__ import annotations

import datetime as _dt
import html
import re
import time
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import structlog

from arc.ingest.store import FILTERED_STATUS, IngestCursorRepo, RawDocRepo, content_hash
from arc.models import RawDoc
from arc.universe.master import normalize_symbol

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterable, Sequence

    from arc.ingest.ticker_news_config import TickerNewsConfig, TickerNewsInputSpec

log = structlog.get_logger(__name__)

__all__ = [
    "SOURCE",
    "AlpacaGet",
    "Article",
    "InputRun",
    "TickerNewsFetch",
    "alpaca_getter",
    "cursor_key",
    "fetch_ticker_news",
    "parse_alpaca",
    "parse_finnhub",
    "source_key",
]

SOURCE = "ticker_news"
_TAG_RE = re.compile(r"<[^>]+>")

# ``get(url, params, timeout_s) -> (status, headers, json body)``; raises on network errors.
AlpacaGet = Callable[[str, Mapping[str, str], float], tuple[int, Mapping[str, str], Any]]


class InputError(RuntimeError):
    """One input call failed; ``reason`` is ``forbidden`` | ``rate_limited`` | ``error``."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason


@dataclass(frozen=True)
class Article:
    """One provider article, already normalised."""

    url: str
    headline: str
    summary: str
    created_at: _dt.datetime
    symbols: tuple[str, ...]

    @property
    def text(self) -> str:
        return f"{self.headline}\n\n{self.summary}".strip() if self.summary else self.headline


@dataclass
class InputRun:
    """What one input did this run."""

    name: str
    type: str
    status: str = "ok"  # ok | partial | failed | no_api_key | not_needed
    tickers: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)  # ticker -> reason
    errors: list[str] = field(default_factory=list)
    calls: int = 0
    articles: int = 0
    cursor: str | None = None  # new cursor value (alpaca, full walk only)

    @property
    def reasons(self) -> set[str]:
        return set(self.failed.values())


@dataclass
class TickerNewsFetch:
    """One ``ticker_news`` run: Scalp-readable new docs plus per-input accounting."""

    docs: list[RawDoc] = field(default_factory=list)
    new: Counter[str] = field(default_factory=Counter)  # input -> new readable docs
    filtered: Counter[str] = field(default_factory=Counter)  # input -> stored filtered
    duplicates: int = 0
    stale: int = 0
    per_ticker: Counter[str] = field(default_factory=Counter)  # new readable docs per ticker
    seen_tickers: set[str] = field(default_factory=set)  # scope names any article tagged
    inputs: dict[str, InputRun] = field(default_factory=dict)
    scope: list[str] = field(default_factory=list)

    @property
    def unresolved(self) -> dict[str, str]:
        """Tickers no input answered for: the last reason recorded per ticker."""
        out: dict[str, str] = {}
        ok: set[str] = set()
        for run in self.inputs.values():
            ok |= {t for t in run.tickers if t not in run.failed}
            out.update(run.failed)
        return {t: r for t, r in out.items() if t not in ok}


def source_key(input_name: str) -> str:
    """Registry key of an input's docs (``ticker_news.<input>``, like ``youtube.<slug>``)."""
    return f"{SOURCE}.{input_name}"


def cursor_key(input_name: str) -> str:
    return f"{SOURCE}:{input_name}"


def _ts(raw: Any) -> _dt.datetime | None:
    if raw is None or raw == "":
        return None
    if isinstance(raw, (int, float)):
        return _dt.datetime.fromtimestamp(float(raw), tz=_dt.UTC)
    try:
        ts = _dt.datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=_dt.UTC)


def _clean(text: Any) -> str:
    return " ".join(html.unescape(_TAG_RE.sub(" ", str(text or ""))).split())


def _symbols(raw: Iterable[Any]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(s for s in (normalize_symbol(str(x)) for x in raw) if s))


def parse_alpaca(body: Mapping[str, Any]) -> list[Article]:
    """Alpaca ``/v1beta1/news`` page → articles (no URL or no time = dropped)."""
    out: list[Article] = []
    for it in body.get("news") or []:
        url = str(it.get("url") or "").strip()
        at = _ts(it.get("created_at"))
        if not url or at is None:
            continue
        out.append(
            Article(
                url=url,
                headline=_clean(it.get("headline")),
                summary=_clean(it.get("summary")),
                created_at=at,
                symbols=_symbols(it.get("symbols") or []),
            )
        )
    return out


def parse_finnhub(rows: Sequence[Mapping[str, Any]], ticker: str) -> list[Article]:
    """Finnhub ``/company-news`` rows → articles; ``related`` tags (else *ticker*)."""
    out: list[Article] = []
    for it in rows:
        url = str(it.get("url") or "").strip()
        at = _ts(it.get("datetime"))
        if not url or at is None:
            continue
        related = [r for r in str(it.get("related") or "").split(",") if r.strip()]
        out.append(
            Article(
                url=url,
                headline=_clean(it.get("headline")),
                summary=_clean(it.get("summary")),
                created_at=at,
                symbols=_symbols(related or [ticker]),
            )
        )
    return out


def alpaca_getter(api_key: str, secret_key: str) -> AlpacaGet:  # pragma: no cover - live
    """HTTP GET with the Alpaca key headers (never logged)."""
    import requests

    headers = {"APCA-API-KEY-ID": api_key, "APCA-API-SECRET-KEY": secret_key}

    def get(
        url: str, params: Mapping[str, str], timeout: float
    ) -> tuple[int, Mapping[str, str], Any]:
        resp = requests.get(url, params=dict(params), headers=headers, timeout=timeout)
        body: Any = None
        if resp.status_code < 400:  # noqa: PLR2004
            body = resp.json()
        return resp.status_code, dict(resp.headers), body

    return get


def _retry_after(headers: Mapping[str, str], default: float) -> float:
    raw = next((v for k, v in headers.items() if k.lower() == "retry-after"), None)
    try:
        return max(0.0, float(raw)) if raw is not None else default
    except ValueError:
        return default


def _iso_z(at: _dt.datetime) -> str:
    return at.astimezone(_dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _alpaca_chunk(
    symbols: Sequence[str],
    start: _dt.datetime,
    end: _dt.datetime,
    spec: TickerNewsInputSpec,
    *,
    get: AlpacaGet,
    sleep: Callable[[float], None],
    run: InputRun,
) -> list[Article]:
    """Walk every page of one symbol chunk; raises :class:`InputError` on any failure."""
    out: list[Article] = []
    token: str | None = None
    pages = 0
    while True:
        params = {
            "symbols": ",".join(symbols),
            "start": _iso_z(start),
            "end": _iso_z(end),  # the run's clock: replays and cursors never run ahead
            "limit": str(spec.page_limit),
            "sort": "asc",
            "include_content": "false",
        }
        if token:
            params["page_token"] = token
        retried = False
        while True:
            run.calls += 1
            try:
                status, headers, body = get(spec.url, params, spec.timeout_s)
            except Exception as exc:  # noqa: BLE001 - network error: this chunk failed
                raise InputError("error", f"{type(exc).__name__}: {exc}"[:300]) from None
            if status == 429 and not retried:  # noqa: PLR2004
                retried = True
                delay = _retry_after(headers, spec.retry_after_default_s)
                log.warning("ticker_news.rate_limited", input=run.name, retry_in_s=delay)
                sleep(delay)
                continue
            break
        if status == 429:  # noqa: PLR2004
            raise InputError("rate_limited", "HTTP 429 twice")
        if status in (401, 403):
            raise InputError("forbidden", f"HTTP {status}")
        if status >= 400 or not isinstance(body, dict):  # noqa: PLR2004
            raise InputError("error", f"HTTP {status}")
        out.extend(parse_alpaca(body))
        pages += 1
        token = body.get("next_page_token") or None
        if not token:
            return out
        if pages >= spec.max_pages:
            msg = f"page walk not finished after {pages} pages"
            raise InputError("error", msg)


def _run_alpaca(
    name: str,
    spec: TickerNewsInputSpec,
    tickers: Sequence[str],
    start: _dt.datetime,
    end: _dt.datetime,
    *,
    get: AlpacaGet | None,
    sleep: Callable[[float], None],
) -> tuple[InputRun, list[Article]]:
    run = InputRun(name=name, type=spec.type, tickers=list(tickers))
    if get is None:
        run.status = "no_api_key"
        run.failed = dict.fromkeys(tickers, "no_api_key")
        return run, []
    arts: list[Article] = []
    for i in range(0, len(tickers), spec.chunk_size):
        chunk = list(tickers[i : i + spec.chunk_size])
        try:
            arts.extend(_alpaca_chunk(chunk, start, end, spec, get=get, sleep=sleep, run=run))
        except InputError as exc:
            log.warning("ticker_news.chunk_failed", input=name, reason=exc.reason, error=str(exc))
            run.errors.append(f"{exc.reason}: {exc}")
            run.failed.update(dict.fromkeys(chunk, exc.reason))
    run.articles = len(arts)
    if not run.failed:  # full walk of every chunk: the cursor may advance
        newest = max((a.created_at for a in arts), default=None)
        run.cursor = _iso_z(newest) if newest is not None else None
    run.status = (
        "ok" if not run.failed else ("failed" if len(run.failed) == len(tickers) else "partial")
    )
    return run, arts


def _run_finnhub(
    name: str,
    spec: TickerNewsInputSpec,
    tickers: Sequence[str],
    start: _dt.datetime,
    now: _dt.datetime,
    *,
    client: Any,
) -> tuple[InputRun, list[Article]]:
    from arc.ingest.finnhub import FinnhubError, FinnhubForbidden, FinnhubRateLimited
    from arc.utils.calendar import ET

    run = InputRun(name=name, type=spec.type, tickers=list(tickers))
    if client is None:
        run.status = "no_api_key"
        run.failed = dict.fromkeys(tickers, "no_api_key")
        return run, []
    lo = start.astimezone(ET).date().isoformat()
    hi = now.astimezone(ET).date().isoformat()
    arts: list[Article] = []
    for t in tickers:
        before = client.calls
        try:
            rows = client.get("/company-news", {"symbol": t, "from": lo, "to": hi})
        except FinnhubForbidden as exc:
            run.failed[t] = "forbidden"
            run.errors.append(f"{t}: {exc}")
        except FinnhubRateLimited as exc:
            run.failed[t] = "rate_limited"
            run.errors.append(f"{t}: {exc}")
        except FinnhubError as exc:
            run.failed[t] = "error"
            run.errors.append(f"{t}: {exc}")
        else:
            arts.extend(a for a in parse_finnhub(rows or [], t) if a.created_at >= start)
        finally:
            run.calls += client.calls - before
    run.articles = len(arts)
    run.status = (
        "ok" if not run.failed else ("failed" if len(run.failed) == len(tickers) else "partial")
    )
    return run, arts


def _start_for(
    cursors: IngestCursorRepo, name: str, now: _dt.datetime, max_age: _dt.timedelta
) -> _dt.datetime:
    floor = now - max_age
    cur = _ts(cursors.get(cursor_key(name)))
    return max(cur, floor) if cur is not None else floor


def fetch_ticker_news(  # noqa: PLR0913 - injectable network edges
    conn: sqlite3.Connection,
    cfg: TickerNewsConfig,
    scope: Sequence[str],
    *,
    now: _dt.datetime,
    max_age: _dt.timedelta,
    alpaca_get: AlpacaGet | None,
    finnhub_client: Any = None,
    sleep: Callable[[float], None] = time.sleep,
    run_id: str | None = None,
) -> TickerNewsFetch:
    """Run every enabled input over *scope* and store the new articles.

    *alpaca_get* / *finnhub_client* are ``None`` when that provider has no key (the
    input is recorded ``no_api_key``). Fallback inputs run only on the tickers no
    primary answered for, in config order.
    """
    out = TickerNewsFetch(scope=list(dict.fromkeys(normalize_symbol(t) for t in scope if t)))
    scope_set = set(out.scope)
    cursors = IngestCursorRepo(conn)
    docs = RawDocRepo(conn)
    pending_cursor: dict[str, str] = {}
    gathered: list[tuple[str, Article]] = []

    def run_input(name: str, spec: TickerNewsInputSpec, tickers: list[str]) -> None:
        start = _start_for(cursors, name, now, max_age)
        if spec.type == "alpaca_news":
            run, arts = _run_alpaca(name, spec, tickers, start, now, get=alpaca_get, sleep=sleep)
        else:
            run, arts = _run_finnhub(name, spec, tickers, start, now, client=finnhub_client)
        out.inputs[name] = run
        if run.cursor is not None:
            pending_cursor[name] = run.cursor
        gathered.extend((name, a) for a in arts)
        log.info(
            "ticker_news.input",
            input=name,
            type=spec.type,
            status=run.status,
            tickers=len(tickers),
            articles=run.articles,
            failed=len(run.failed),
            calls=run.calls,
        )

    for name, spec in cfg.primaries.items():
        run_input(name, spec, out.scope)
    for name, spec in cfg.fallbacks.items():
        need = sorted(out.unresolved)
        if not need:
            out.inputs[name] = InputRun(name=name, type=spec.type, status="not_needed")
            continue
        run_input(name, spec, [t for t in out.scope if t in set(need)])

    # Store: newest-known URL dedupe across every source (RSS included), then the hash.
    urls = list(dict.fromkeys(a.url for _, a in gathered))
    stored = set(docs.source_keys_for_urls(urls)) if urls else set()
    for name, art in gathered:
        if art.url in stored:
            out.duplicates += 1
            continue
        if art.created_at + max_age <= now:
            out.stale += 1  # D47: past the category window; never stored
            continue
        hint = [s for s in art.symbols if s in scope_set]
        out.seen_tickers.update(hint)
        h = content_hash(SOURCE, art.url)
        doc_id = docs.insert(
            source=SOURCE,
            url=art.url,
            published_at=art.created_at.isoformat(),
            text=art.text,
            tickers_hint=hint,
            hash_val=h,
            run_id=run_id,
            title=art.headline or None,
            source_key=source_key(name),
            closed_status=None if hint else FILTERED_STATUS,
        )
        stored.add(art.url)
        if doc_id is None:
            out.duplicates += 1
            continue
        if not hint:
            out.filtered[name] += 1
            continue
        out.new[name] += 1
        out.per_ticker.update(hint)
        out.docs.append(
            RawDoc(
                source=SOURCE,
                url=art.url,
                published_at=art.created_at,
                text=art.text,
                tickers_hint=hint,
                content_hash=h,
                title=art.headline,
            )
        )
    # The cursor advances only after the input's full successful page walk (stored first).
    for name, val in pending_cursor.items():
        cursors.set(cursor_key(name), val)
    log.info(
        "ticker_news.done",
        new=sum(out.new.values()),
        filtered=sum(out.filtered.values()),
        duplicates=out.duplicates,
        stale=out.stale,
        unresolved=len(out.unresolved),
    )
    return out
