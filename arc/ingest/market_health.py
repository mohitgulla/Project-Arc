"""Market-health inputs (E16.4, D76): Cboe VIX/VVIX history and the put/call history.

Network + store side of :mod:`arc.features.market_health` (which stays pure):

* :func:`fetch_index_history` — Cboe's free ``<INDEX>_History.csv`` (the same files
  ``vol_term`` reads) -> an :class:`IndexHistoryPayload` with the trailing closes.
* Put/call history: Cboe's historical put/call CSVs stop in October 2019, so the
  series is built from our own ``options_daily`` entries plus a one-off, resumable
  backfill (:func:`backfill_pc_history`) that walks Cboe's dated daily-statistics JSON
  (``<YYYY-MM-DD>_daily_options``, published back to 2023) one session at a time.
  Both land in one ``pc_history`` context entry (subject ``market``).
* :func:`gather_inputs` reads everything the read needs from the store (read-only).

Context only; never a gate input (the gate may not import ``arc.context.kinds``).
"""

from __future__ import annotations

import datetime as _dt
import json
import time
from typing import TYPE_CHECKING, Any

import structlog

from arc.features.market_health import (
    HISTORY_KEEP,
    IndexClose,
    IndexHistoryPayload,
    MarketTerm,
    PcHistoryPayload,
    PcPoint,
    merge_pc_points,
)
from arc.ingest.options_data import BROWSER_UA, CBOE_HISTORY_URL, http_get, parse_cboe_history
from arc.utils.calendar import ET, is_session, sessions_between

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable, Iterable, Sequence

    from arc.context.kinds import OptionsDailyPayload

log = structlog.get_logger(__name__)

__all__ = [
    "HEALTH_INDICES",
    "PC_SUBJECT",
    "BackfillResult",
    "HealthInputs",
    "backfill_pc_history",
    "fetch_index_history",
    "gather_inputs",
    "index_history_from_csv",
    "latest_payload",
    "pc_points_from_options_daily",
    "session_lag",
]

HEALTH_INDICES = ("VIX", "VVIX")
PC_SUBJECT = "market"


def session_lag(day: _dt.date, as_of: _dt.date) -> int:
    """Trading sessions in ``(day, as_of]`` (0 when *day* >= *as_of*)."""
    if day >= as_of:
        return 0
    n = len(sessions_between(day, as_of))
    return n - (1 if is_session(day) else 0)


# ---------------------------------------------------------------------------
# VIX / VVIX history
# ---------------------------------------------------------------------------


def index_history_from_csv(
    index: str, text: str, *, keep: int = HISTORY_KEEP, url: str | None = None
) -> IndexHistoryPayload | None:
    """Cboe history CSV -> the trailing *keep* closes; ``None`` when it has none."""
    closes = parse_cboe_history(text)
    if not closes:
        return None
    days = sorted(closes)[-keep:]
    return IndexHistoryPayload(
        index=index,
        as_of=days[-1],
        closes=[IndexClose(day=d, close=closes[d]) for d in days],
        url=url or CBOE_HISTORY_URL.format(index=index),
    )


def fetch_index_history(
    index: str, *, get: Callable[[str, str], bytes] | None = None, keep: int = HISTORY_KEEP
) -> IndexHistoryPayload | None:
    url = CBOE_HISTORY_URL.format(index=index)
    body = (get or http_get)(url, BROWSER_UA)
    return index_history_from_csv(index, body.decode("utf-8", "replace"), keep=keep, url=url)


# ---------------------------------------------------------------------------
# Put/call history
# ---------------------------------------------------------------------------


def _pc_point(p: OptionsDailyPayload | dict[str, Any]) -> PcPoint | None:
    data = p if isinstance(p, dict) else p.model_dump(mode="json")
    ratios = {r.get("segment"): r.get("ratio") for r in data.get("ratios") or []}
    if ratios.get("equity") is None and ratios.get("total") is None:
        return None
    return PcPoint(
        day=_dt.date.fromisoformat(str(data["as_of"])),
        equity=ratios.get("equity"),
        total=ratios.get("total"),
    )


def pc_points_from_options_daily(payloads: Iterable[Any]) -> list[PcPoint]:
    """``options_daily`` payloads (models or dumps) -> put/call points, by day."""
    return merge_pc_points(p for raw in payloads if (p := _pc_point(raw)) is not None)


def latest_payload(conn: sqlite3.Connection, kind: str, subject: str) -> dict[str, Any] | None:
    """Newest entry of *kind*/*subject* in any status (histories outlive their TTL)."""
    row = conn.execute(
        "SELECT payload FROM context_entries WHERE kind = ? AND subject = ? "
        "ORDER BY valid_from DESC, created_at DESC, rowid DESC LIMIT 1",
        (kind, subject),
    ).fetchone()
    return json.loads(row[0]) if row else None


def _stored_pc(conn: sqlite3.Connection) -> list[PcPoint]:
    raw = latest_payload(conn, "pc_history", PC_SUBJECT)
    return PcHistoryPayload.model_validate(raw).points if raw else []


def _options_daily_points(conn: sqlite3.Connection, as_of: _dt.date) -> list[PcPoint]:
    rows = conn.execute(
        "SELECT payload FROM context_entries WHERE kind = 'options_daily' "
        "AND json_extract(payload, '$.as_of') <= ? ORDER BY valid_from, rowid",
        (as_of.isoformat(),),
    ).fetchall()
    return pc_points_from_options_daily(json.loads(r[0]) for r in rows)


class BackfillResult:
    """What one backfill pass did (counts only)."""

    def __init__(self) -> None:
        self.fetched: list[_dt.date] = []
        self.skipped: dict[str, str] = {}  # day -> reason
        self.already: int = 0
        self.writes: int = 0

    def summary(self) -> str:
        return (
            f"{len(self.fetched)} sessions fetched, {self.already} already stored, "
            f"{len(self.skipped)} skipped, {self.writes} pc_history writes"
        )


def backfill_pc_history(
    conn: sqlite3.Connection,
    *,
    start: _dt.date,
    end: _dt.date,
    now: _dt.datetime,
    fetch: Callable[[_dt.date], OptionsDailyPayload],
    pace_s: float = 0.5,
    save_every: int = 20,
    sleep: Callable[[float], None] = time.sleep,
    produced_by: str = "cli:market-health",
) -> BackfillResult:
    """Walk Cboe's dated daily statistics for every session in ``[start, end]`` not yet
    in ``pc_history`` (or ``options_daily``) and store them, *save_every* sessions per
    write, so an interrupted run resumes where it stopped. A session Cboe has not
    published (or answers with an error) is skipped and named in the result; it is
    retried on the next run. Rate-limited by *pace_s* between requests.
    """
    from arc.context.store import ContextStore
    from arc.ingest.cboe_daily import NotPublishedError

    res = BackfillResult()
    have = {p.day: p for p in merge_pc_points(_stored_pc(conn), _options_daily_points(conn, end))}
    pending: list[PcPoint] = []

    def save() -> None:
        nonlocal pending
        if not pending:
            return
        points = merge_pc_points(have.values(), pending, keep=10_000)
        payload = PcHistoryPayload(as_of=points[-1].day, points=points)
        ContextStore(conn).write(
            kind="pc_history",
            subject=PC_SUBJECT,
            payload=payload,
            produced_by=produced_by,
            ttl="7d",
            now=now,
        )
        have.update({p.day: p for p in pending})
        pending = []
        res.writes += 1

    first = True
    for day in sessions_between(start, end):
        if day in have:
            res.already += 1
            continue
        if not first:
            sleep(pace_s)
        first = False
        try:
            point = _pc_point(fetch(day))
        except NotPublishedError as exc:
            res.skipped[day.isoformat()] = f"not published: {exc}"
            continue
        except Exception as exc:  # noqa: BLE001 - one bad day never stops the walk
            res.skipped[day.isoformat()] = f"{type(exc).__name__}: {str(exc)[:120]}"
            continue
        if point is None:
            res.skipped[day.isoformat()] = "no equity/total ratio"
            continue
        pending.append(point)
        res.fetched.append(day)
        if len(pending) >= save_every:
            save()
    save()
    log.info("market_health.pc_backfill", summary=res.summary())
    return res


# ---------------------------------------------------------------------------
# Inputs for one read
# ---------------------------------------------------------------------------


class HealthInputs:
    """Everything :func:`arc.features.market_health.compute_market_health` takes."""

    def __init__(
        self,
        *,
        pc: list[PcPoint],
        term: tuple[_dt.date, MarketTerm] | None,
        technicals: list[dict[str, Any]],
        technicals_as_of: _dt.date | None,
        tickers: Sequence[str],
    ) -> None:
        self.pc = pc
        self.term = term
        self.technicals = technicals
        self.technicals_as_of = technicals_as_of
        self.tickers = list(tickers)


def gather_inputs(
    conn: sqlite3.Connection,
    *,
    as_of: _dt.date,
    tickers: Sequence[str],
    max_lag_sessions: int,
) -> HealthInputs:
    """Read the put/call history, the newest ``vol_term`` and each active ticker's
    newest ``regime.technicals`` (any status: a regime entry expires at the close,
    the read runs after it). Technicals dated more than *max_lag_sessions* before
    *as_of* (or after it) are left out. Read-only."""
    pc = merge_pc_points(_stored_pc(conn), _options_daily_points(conn, as_of), keep=10_000)

    term: tuple[_dt.date, MarketTerm] | None = None
    row = conn.execute(
        "SELECT payload FROM context_entries WHERE kind = 'vol_term' "
        "AND json_extract(payload, '$.as_of') <= ? "
        "ORDER BY json_extract(payload, '$.as_of') DESC, valid_from DESC LIMIT 1",
        (as_of.isoformat(),),
    ).fetchone()
    if row:
        vt = json.loads(row[0])
        term = (
            _dt.date.fromisoformat(vt["as_of"]),
            MarketTerm(
                structure=vt["structure"],
                ratio_9d_1m=vt.get("ratio_9d_1m"),
                ratio_3m_1m=vt.get("ratio_3m_1m"),
            ),
        )

    techs: list[dict[str, Any]] = []
    newest: _dt.date | None = None
    for t in tickers:
        r = conn.execute(
            "SELECT json_extract(payload, '$.technicals') FROM context_entries "
            "WHERE kind = 'regime' AND subject = ? "
            "AND json_extract(payload, '$.technicals') IS NOT NULL "
            "AND json_extract(payload, '$.technicals.as_of') <= ? "
            "ORDER BY json_extract(payload, '$.technicals.as_of') DESC, valid_from DESC LIMIT 1",
            (t, as_of.isoformat()),
        ).fetchone()
        if not r or r[0] is None:
            continue
        tech = json.loads(r[0])
        day = _dt.date.fromisoformat(str(tech.get("as_of")))
        if session_lag(day, as_of) > max_lag_sessions:
            continue
        techs.append(tech)
        newest = day if newest is None or day > newest else newest
    return HealthInputs(
        pc=pc, term=term, technicals=techs, technicals_as_of=newest, tickers=tickers
    )


def now_session_day(now: _dt.datetime) -> _dt.date:
    """The session a read at *now* describes (today from 16:30 ET, else the previous)."""
    from arc.utils.calendar import completed_session

    return completed_session(now.astimezone(ET))
