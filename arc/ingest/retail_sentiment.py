"""``retail_sentiment`` source (E14.6, D60): Stocktwits per-ticker bull/bear counts.

One request per ticker in scope to the Stocktwits per-symbol stream (free, no key),
which returns the latest 30 messages. About a third carry a user-tagged
``entities.sentiment.basic`` of ``Bullish`` or ``Bearish``. Code counts them into one
:class:`~arc.context.kinds.RetailSentimentPayload` per ticker:

- untagged messages count in ``messages`` only;
- ``bull_ratio = bullish / tagged``, ``None`` when ``tagged < min_tagged`` ("too few
  tags"), never a 0% or 100% reading off one or two tags;
- ``window_minutes`` = newest minus oldest message time (a chatter-intensity proxy);
- ``pages: 2`` reads one older page (``max`` cursor) for a ticker under ``min_tagged``.

Stocktwits throttles unauthenticated clients. A 429 stops the run at once (``partial``)
and keeps what was fetched; a 404 (unknown symbol) or any other per-ticker error skips
that ticker only. Context only: never a gate input, never a tier-ranking input.
"""

from __future__ import annotations

import datetime as _dt
import json
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import structlog

from arc.context.kinds import RetailSentimentPayload

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from arc.ingest.retail_sentiment_config import RetailSentimentConfig

log = structlog.get_logger(__name__)

__all__ = [
    "RateLimitedError",
    "SentimentFetch",
    "StreamPage",
    "fetch_retail_sentiment",
    "http_getter",
    "parse_stream",
    "sentiment_payload",
]

BULLISH = "Bullish"
BEARISH = "Bearish"


class RateLimitedError(RuntimeError):
    """The stream answered 429: stop the run (``partial``), keep what was fetched."""


def http_getter(user_agent: str) -> Callable[[str, float, int], bytes]:  # pragma: no cover - live
    """HTTP GET (connection errors / timeouts / 5xx retried, never 4xx); 429 ->
    :class:`RateLimitedError`."""
    import requests

    from arc.ingest.options_data import http_get

    def get(url: str, timeout_s: float, retries: int) -> bytes:
        try:
            return http_get(url, user_agent, timeout=timeout_s, retries=retries)
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 429:
                raise RateLimitedError(str(exc)[:200]) from exc
            raise

    return get


@dataclass(frozen=True)
class StreamMessage:
    id: int
    created_at: _dt.datetime | None
    sentiment: str | None  # "Bullish" | "Bearish" | None (untagged)


@dataclass(frozen=True)
class StreamPage:
    """One parsed stream page."""

    messages: list[StreamMessage]
    more: bool = False
    max_cursor: int | None = None
    watchlist_count: int | None = None


def _ts(raw: Any) -> _dt.datetime | None:
    if not raw:
        return None
    try:
        t = _dt.datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo is not None else t.replace(tzinfo=_dt.UTC)


def _sentiment(msg: dict[str, Any]) -> str | None:
    ent = msg.get("entities")
    if not isinstance(ent, dict):
        return None
    s = ent.get("sentiment")
    basic = s.get("basic") if isinstance(s, dict) else None
    return basic if basic in (BULLISH, BEARISH) else None


def _int(v: Any) -> int | None:
    try:
        return int(v) if v is not None and v != "" else None
    except (TypeError, ValueError):
        return None


def parse_stream(data: Any) -> StreamPage:
    """A stream page (``{"symbol", "cursor", "messages"}``) -> :class:`StreamPage`.

    Missing ``entities`` / ``sentiment`` = untagged; any value other than ``Bullish`` /
    ``Bearish`` is untagged. Raises ``ValueError`` on a body without ``messages``.
    """
    if not isinstance(data, dict) or not isinstance(data.get("messages"), list):
        msg = "stocktwits stream: no messages list"
        raise ValueError(msg)
    out: list[StreamMessage] = []
    for m in data["messages"]:
        if not isinstance(m, dict):
            continue
        mid = _int(m.get("id"))
        if mid is None:
            continue
        out.append(
            StreamMessage(id=mid, created_at=_ts(m.get("created_at")), sentiment=_sentiment(m))
        )
    raw_cursor = data.get("cursor")
    raw_sym = data.get("symbol")
    cursor: dict[str, Any] = raw_cursor if isinstance(raw_cursor, dict) else {}
    sym: dict[str, Any] = raw_sym if isinstance(raw_sym, dict) else {}
    return StreamPage(
        messages=out,
        more=bool(cursor.get("more")),
        max_cursor=_int(cursor.get("max")),
        watchlist_count=_int(sym.get("watchlist_count")),
    )


def sentiment_payload(
    pages: Sequence[StreamPage], *, min_tagged: int, as_of: str
) -> RetailSentimentPayload:
    """Count the pages' messages (deduped by id) into one payload (pure)."""
    seen: dict[int, StreamMessage] = {}
    for p in pages:
        for m in p.messages:
            seen.setdefault(m.id, m)
    msgs = list(seen.values())
    bull = sum(1 for m in msgs if m.sentiment == BULLISH)
    bear = sum(1 for m in msgs if m.sentiment == BEARISH)
    tagged = bull + bear
    times = [m.created_at for m in msgs if m.created_at is not None]
    window = round((max(times) - min(times)).total_seconds() / 60, 1) if len(times) >= 2 else None
    watch = next((p.watchlist_count for p in pages if p.watchlist_count is not None), None)
    return RetailSentimentPayload(
        as_of=as_of,
        messages=len(msgs),
        tagged=tagged,
        bullish=bull,
        bearish=bear,
        bull_ratio=round(bull / tagged, 4) if tagged >= min_tagged else None,
        min_tagged=min_tagged,
        window_minutes=window,
        newest_at=max(times).astimezone(_dt.UTC).isoformat() if times else None,
        watchlist_count=watch,
        pages=max(1, len(pages)),
    )


@dataclass
class SentimentFetch:
    """What one run fetched (the handler writes ``readings``)."""

    tickers: list[str]
    readings: dict[str, RetailSentimentPayload] = field(default_factory=dict)
    skipped: dict[str, str] = field(default_factory=dict)  # ticker -> reason
    requests: int = 0
    rate_limited: bool = False
    capped: bool = False  # stopped at max_requests
    not_reached: list[str] = field(default_factory=list)
    wall_s: float = 0.0

    @property
    def partial(self) -> bool:
        return self.rate_limited or self.capped

    @property
    def with_ratio(self) -> list[str]:
        return [t for t, r in self.readings.items() if r.bull_ratio is not None]


def fetch_retail_sentiment(
    cfg: RetailSentimentConfig,
    tickers: Sequence[str],
    *,
    now: _dt.datetime,
    get: Callable[[str, float, int], bytes],
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> SentimentFetch:
    """Fetch the stream for each ticker (serial, ``pace_s`` apart), stop on a 429."""
    from arc.utils.calendar import ET

    as_of = now.astimezone(ET).replace(microsecond=0).isoformat()
    res = SentimentFetch(tickers=list(tickers))
    t0 = clock()

    def request(url: str) -> bytes:
        if res.requests:
            sleep(cfg.pace_s)
        res.requests += 1
        return get(url, cfg.timeout_s, cfg.retries)

    for i, ticker in enumerate(tickers):
        if res.requests >= cfg.max_requests:
            res.capped = True
            res.not_reached = list(tickers[i:])
            break
        base = cfg.url.format(ticker=ticker)
        pages: list[StreamPage] = []
        try:
            pages.append(parse_stream(json.loads(request(base).decode("utf-8"))))
            last = pages[-1]
            while (
                len(pages) < cfg.pages
                and last.more
                and last.max_cursor is not None
                and res.requests < cfg.max_requests
                and sentiment_payload(pages, min_tagged=cfg.min_tagged, as_of=as_of).tagged
                < cfg.min_tagged
            ):
                sep = "&" if "?" in base else "?"
                url = f"{base}{sep}max={last.max_cursor}"
                pages.append(parse_stream(json.loads(request(url).decode("utf-8"))))
                last = pages[-1]
        except RateLimitedError:
            log.warning("retail_sentiment.rate_limited", ticker=ticker, requests=res.requests)
            res.rate_limited = True
            # a 429 on page 2 keeps this ticker's page 1; on page 1 the ticker is not reached
            res.not_reached = list(tickers[i + 1 :] if pages else tickers[i:])
        except Exception as exc:  # noqa: BLE001 - one ticker never fails the run
            res.skipped[ticker] = f"{type(exc).__name__}: {str(exc)[:120]}"
            continue
        if pages:
            res.readings[ticker] = sentiment_payload(pages, min_tagged=cfg.min_tagged, as_of=as_of)
        if res.rate_limited:
            break
    res.wall_s = round(clock() - t0, 1)
    return res
