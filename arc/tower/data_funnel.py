"""E13.14 (D56, folds E5.14's report half): the idea funnel report.

Per ET day range ``[since, until]``, how many ideas survive each step, from the audit
store only (SELECT only, deterministic for a fixed range and store):

==============  ============================================================
stage           counted from
==============  ============================================================
docs_fresh      ``raw_docs`` ingested in range (by feed + category)
docs_read       of those, read by a persona: ``scalp_status`` ``scouted``
                (Scalp read it) or ``brief_only`` (YouTube → Scout briefs)
stories         ``story`` context entries (by category; pre-D56 names read
                through ``normalize_category``, a removed one as ``other``)
candidates      distinct (day, ticker) ``candidate`` entries (by feed)
pool            distinct (day, ticker) Research saw (ranked + excluded)
shortlist       distinct (day, ticker) inside the Quant budget
structures      distinct (day, ticker) Quant structured
proposals       open proposals created
approved        open approval requests approved
filled          open executions with a fill
==============  ============================================================

Docs stages split ``by_feed`` by the source's feed (D56: market_news, company_data and
options_fast are the Scalp's; options_slow, YouTube and reference data the Scout's) and
``by_category`` by its registry category. Ideas stages: ``by_feed`` attributes a ticker
to the feed(s) whose candidates raised it that day (a name both feeds raised counts
under each). ``top_sources`` counts candidates per source
key: each candidate's source URLs are matched to ``raw_docs`` (registry key), and the
Scout's ``origins`` (``youtube:<slug>``) count as themselves. ``discovery_fill`` is the
Scout's discovery tier size per day (D56).

Shared by ``GET /api/performance/funnel`` and ``arc funnel report``.
"""

from __future__ import annotations

import datetime as _dt
import json
from collections import Counter, defaultdict
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from arc.tower.catalogue import Feed, category_feed
from arc.tower.data import _has_table, parse_ts
from arc.tower.data_universe import discovery_fill_by_day
from arc.utils.calendar import ET, sessions_between

if TYPE_CHECKING:
    import sqlite3

    from arc.routines.config import RoutinesConfig

__all__ = [
    "FUNNEL_RANGES",
    "STAGE_LABELS",
    "FunnelRange",
    "FunnelReport",
    "FunnelStage",
    "funnel_bounds",
    "load_funnel_report",
    "render_funnel_table",
]

_STRICT = ConfigDict(extra="forbid", frozen=True)

StageKey = Literal[
    "docs_fresh",
    "docs_read",
    "stories",
    "candidates",
    "pool",
    "shortlist",
    "structures",
    "proposals",
    "approved",
    "filled",
]
STAGE_LABELS: dict[str, str] = {
    "docs_fresh": "Docs fresh",
    "docs_read": "Docs read",
    "stories": "Stories",
    "candidates": "Candidates",
    "pool": "Idea pool",
    "shortlist": "Shortlist",
    "structures": "Structures",
    "proposals": "Proposals",
    "approved": "Approved",
    "filled": "Filled",
}
FunnelRange = Literal["1D", "1W", "1M", "3M"]
FUNNEL_RANGES: dict[str, int] = {"1D": 1, "1W": 7, "1M": 30, "3M": 91}
TOP_SOURCES = 12
#: ``raw_docs.scalp_status`` values meaning a persona read the doc (D54 kept ``scouted``).
_READ_STATUSES = frozenset({"scouted", "brief_only"})


class FunnelStage(BaseModel):
    model_config = _STRICT

    stage: StageKey
    count: int = Field(ge=0)
    by_feed: dict[Feed, int] = Field(default_factory=dict)
    by_category: dict[str, int] = Field(default_factory=dict, description="docs stages only")


class FunnelReport(BaseModel):
    model_config = _STRICT

    since: str
    until: str
    sessions: int = Field(description="Trading sessions in the range")
    stages: list[FunnelStage]
    top_sources: list[tuple[str, int]] = Field(
        default_factory=list, max_length=TOP_SOURCES, description="source key -> candidates"
    )
    discovery_fill: dict[str, int] = Field(
        default_factory=dict, description="day -> discovery tier members (D56 Scout)"
    )


def funnel_bounds(
    today: _dt.date,
    rng: FunnelRange = "1W",
    *,
    since: _dt.date | None = None,
    until: _dt.date | None = None,
) -> tuple[_dt.date, _dt.date]:
    """``[since, until]`` (inclusive): explicit dates win, else *rng* days ending *today*."""
    last = until or today
    first = since or last - _dt.timedelta(days=FUNNEL_RANGES[rng] - 1)
    if first > last:
        msg = f"since {first} is after until {last}"
        raise ValueError(msg)
    return first, last


def _day(value: Any) -> _dt.date | None:
    at = parse_ts(value) if value else None
    return at.astimezone(ET).date() if at is not None else None


def _payload(raw: Any) -> dict[str, Any]:
    try:
        out = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return out if isinstance(out, dict) else {}


def _entries(
    conn: sqlite3.Connection, kind: str, first: _dt.date, last: _dt.date
) -> list[tuple[_dt.date, str, dict[str, Any]]]:
    """(ET day, subject, payload) of every *kind* entry whose ``valid_from`` is in range."""
    if not _has_table(conn, "context_entries"):
        return []
    out = []
    for r in conn.execute(
        "SELECT subject, payload, valid_from FROM context_entries WHERE kind = ?"
        " ORDER BY valid_from, created_at, rowid",
        (kind,),
    ):
        d = _day(r["valid_from"])
        if d is not None and first <= d <= last:
            out.append((d, str(r["subject"]), _payload(r["payload"])))
    return out


def _docs(
    conn: sqlite3.Connection, routines: RoutinesConfig | None, first: _dt.date, last: _dt.date
) -> tuple[FunnelStage, FunnelStage, dict[str, str]]:
    """docs_fresh, docs_read and the url -> source key map of the range's docs."""
    fresh_feed: Counter[str] = Counter()
    fresh_cat: Counter[str] = Counter()
    read_feed: Counter[str] = Counter()
    read_cat: Counter[str] = Counter()
    urls: dict[str, str] = {}
    if not _has_table(conn, "raw_docs"):
        empty = FunnelStage(stage="docs_fresh", count=0)
        return empty, FunnelStage(stage="docs_read", count=0), urls
    reg = None
    if routines is not None:
        from arc.ingest.sources import SourceRegistry

        reg = SourceRegistry.from_routines(routines)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(raw_docs)")}
    extra = [c for c in ("source_key", "scalp_status", "channel_id") if c in cols]
    sel = ", ".join(["source", "url", "ingested_at", *extra])
    n_fresh = n_read = 0
    for r in conn.execute(f"SELECT {sel} FROM raw_docs"):  # noqa: S608 - fixed columns
        d = _day(r["ingested_at"])
        if d is None or not first <= d <= last:
            continue
        row = dict(r)
        key = reg.key_for(row) if reg is not None else str(row.get("source_key") or row["source"])
        urls.setdefault(str(row["url"]), key)
        if reg is not None:
            spec = reg.spec_for(key)
            cat = spec.category_key
            feed = category_feed(spec.category) if spec.category is not None else "scout"
            if spec.category is not None and spec.feed == "scout":
                feed = "scout"  # D54: a feed: scout source (earnings calendar) is slow
        else:
            cat, feed = "unknown", "scalp"
        n_fresh += 1
        fresh_feed[feed] += 1
        fresh_cat[cat] += 1
        status = row.get("scalp_status")
        if status in _READ_STATUSES:
            n_read += 1
            read_feed[feed] += 1
            read_cat[cat] += 1
    return (
        FunnelStage(
            stage="docs_fresh",
            count=n_fresh,
            by_feed=dict(sorted(fresh_feed.items())),  # type: ignore[arg-type]
            by_category=dict(sorted(fresh_cat.items())),
        ),
        FunnelStage(
            stage="docs_read",
            count=n_read,
            by_feed=dict(sorted(read_feed.items())),  # type: ignore[arg-type]
            by_category=dict(sorted(read_cat.items())),
        ),
        urls,
    )


def _by_feed(
    keys: set[tuple[_dt.date, str]], feeds: dict[tuple[_dt.date, str], set[str]]
) -> dict[Feed, int]:
    out: Counter[str] = Counter()
    for k in keys:
        for f in feeds.get(k, ()):
            out[f] += 1
    return dict(sorted(out.items()))  # type: ignore[arg-type]


def _tickers(items: Any) -> list[str]:
    return [
        str(i["ticker"]).upper()
        for i in items or []
        if isinstance(i, dict) and isinstance(i.get("ticker"), str)
    ]


def _budgeted(p: dict[str, Any]) -> list[str]:
    ranked = sorted(
        (i for i in p.get("shortlist") or [] if isinstance(i, dict)),
        key=lambda i: int(i.get("rank") or 0),
    )
    budget = p.get("budget")
    if isinstance(budget, int):
        ranked = ranked[:budget]
    return _tickers(ranked)


def _approved_filled(
    conn: sqlite3.Connection, first: _dt.date, last: _dt.date
) -> tuple[int, int, int]:
    proposals = approved = filled = 0
    if _has_table(conn, "proposals"):
        for r in conn.execute("SELECT created_at FROM proposals WHERE kind = 'open'"):
            d = _day(r["created_at"])
            proposals += d is not None and first <= d <= last
    if _has_table(conn, "approval_requests") and _has_table(conn, "proposals"):
        for r in conn.execute(
            "SELECT a.decided_at, a.created_at FROM approval_requests a"
            " JOIN proposals p ON p.proposal_hash = a.proposal_hash"
            " WHERE a.status = 'approved' AND p.kind = 'open'"
        ):
            d = _day(r["decided_at"] or r["created_at"])
            approved += d is not None and first <= d <= last
    if _has_table(conn, "executions"):
        for r in conn.execute(
            "SELECT started_at FROM executions WHERE kind = 'open' AND filled_qty > 0"
        ):
            d = _day(r["started_at"])
            filled += d is not None and first <= d <= last
    return proposals, approved, filled


def load_funnel_report(
    conn: sqlite3.Connection,
    *,
    since: _dt.date,
    until: _dt.date,
    routines: RoutinesConfig | None = None,
) -> FunnelReport:
    """The funnel for ET days ``[since, until]`` (inclusive). *routines* resolves doc
    source keys / categories through the registry (``None``: stored keys only)."""
    first, last = since, until
    fresh, read, urls = _docs(conn, routines, first, last)

    stories_cat: Counter[str] = Counter()
    stories = _entries(conn, "story", first, last)
    from arc.context.categories import normalize_category

    for _, _, p in stories:
        cat = normalize_category(p.get("category"))
        stories_cat[cat.value if cat is not None else "other"] += 1

    feeds: dict[tuple[_dt.date, str], set[str]] = defaultdict(set)
    sources_by_cand: dict[tuple[_dt.date, str], set[str]] = defaultdict(set)
    for d, subject, p in _entries(conn, "candidate", first, last):
        key = (d, str(p.get("ticker") or subject).upper())
        feed = p.get("feed") if p.get("feed") in ("scalp", "scout") else "scalp"
        feeds[key].add(str(feed))
        for u in p.get("sources") or []:
            if str(u) in urls:
                sources_by_cand[key].add(urls[str(u)])
        for o in p.get("origins") or []:
            sources_by_cand[key].add(str(o))
    cands = set(feeds)

    pool: set[tuple[_dt.date, str]] = set()
    short: set[tuple[_dt.date, str]] = set()
    for d, _, p in _entries(conn, "shortlist", first, last):
        pool |= {(d, t) for t in _tickers(p.get("shortlist")) + _tickers(p.get("excluded"))}
        short |= {(d, t) for t in _budgeted(p)}
    structs: set[tuple[_dt.date, str]] = set()
    for d, _, p in _entries(conn, "structures", first, last):
        structs |= {(d, t) for t in _tickers(p.get("structures"))}
    proposals, approved, filled = _approved_filled(conn, first, last)

    top = Counter(k for keys in sources_by_cand.values() for k in keys)
    stages = [
        fresh,
        read,
        FunnelStage(
            stage="stories", count=len(stories), by_category=dict(sorted(stories_cat.items()))
        ),
        FunnelStage(stage="candidates", count=len(cands), by_feed=_by_feed(cands, feeds)),
        FunnelStage(stage="pool", count=len(pool), by_feed=_by_feed(pool, feeds)),
        FunnelStage(stage="shortlist", count=len(short), by_feed=_by_feed(short, feeds)),
        FunnelStage(stage="structures", count=len(structs), by_feed=_by_feed(structs, feeds)),
        FunnelStage(stage="proposals", count=proposals),
        FunnelStage(stage="approved", count=approved),
        FunnelStage(stage="filled", count=filled),
    ]
    return FunnelReport(
        since=first.isoformat(),
        until=last.isoformat(),
        sessions=len(sessions_between(first, last)),
        stages=stages,
        top_sources=sorted(top.items(), key=lambda kv: (-kv[1], kv[0]))[:TOP_SOURCES],
        discovery_fill=discovery_fill_by_day(conn, first, last),
    )


def render_funnel_table(report: FunnelReport) -> str:
    """Plain-text table for ``arc funnel report`` (one line per stage, then sources)."""
    lines = [
        f"Idea funnel {report.since} .. {report.until} ({report.sessions} sessions)",
        f"{'stage':<12} {'count':>7}  {'scalp':>6} {'scout':>6}  detail",
    ]
    for s in report.stages:
        detail = ", ".join(f"{k} {v}" for k, v in s.by_category.items())
        scalp = s.by_feed.get("scalp")
        scout = s.by_feed.get("scout")
        lines.append(
            f"{STAGE_LABELS[s.stage]:<12} {s.count:>7}  "
            f"{'' if scalp is None else scalp:>6} {'' if scout is None else scout:>6}  {detail}"
        )
    if report.top_sources:
        lines.append("")
        lines.append("Top sources (candidates attributed):")
        lines += [f"  {k:<28} {n:>5}" for k, n in report.top_sources]
    lines.append("")
    if report.discovery_fill:
        fill = ", ".join(f"{d} {n}" for d, n in report.discovery_fill.items())
        lines.append(f"Discovery fill (Scout): {fill}")
    else:
        lines.append("Discovery fill (Scout): none in range")
    return "\n".join(lines)
