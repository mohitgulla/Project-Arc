"""Ops & pipeline page (E8.7d, D35): is the machine running, what did each run do,
what did it cost.

Every loader is a read-only SELECT over the audit store plus the effective config
(``routines.yaml`` + D26 overrides). Nothing here calls the broker, market data, an
LLM or Slack (``arc.tower`` import-linter contract).

========================  ==========================================================
Section                   Source
========================  ==========================================================
Session timeline          ``config/routines.yaml`` slots for the ET day
                          (:func:`arc.routines.schedule.slots_between`) joined to
                          ``routine_runs``; the trading loop (``loop.job``) is its
                          own row, ``no_change`` runs flagged from the summary
Health strip              ``heartbeats`` (``tick`` / ``health``: its ``checks``)
Alerts                    ``ops_alerts`` (open first, resolved in the last 7 days)
Halts                     ``halts`` (active first) + proposals raised in the window
Runs / run detail         ``routine_runs`` + :func:`arc.context.trace.trace_runs`
                          (the same serializer as ``arc context trace``)
Order budget (D32)        ``orders`` / ``order_events`` / ``executions`` via
                          :func:`arc.budget.orders.count_orders` (no broker)
Context store             ``context_entries`` (active by kind, expiry, latest)
Sources (D30)             :class:`arc.ingest.sources.SourceRegistry` + ``raw_docs`` +
                          source ``routine_runs`` + caption backoff state
LLM usage                 ``persona_calls`` + ``scalp_batches`` (tokens, cost)
Effective config (D26)    :class:`arc.control.service.ControlService` (read-only)
========================  ==========================================================
"""

from __future__ import annotations

import datetime as _dt
import json
import sqlite3
from collections import Counter, defaultdict
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from arc.context.trace import trace_runs
from arc.context.ttl import to_db
from arc.journal import legacy
from arc.tower.data import _has_table, _json, parse_ts
from arc.utils.calendar import ET

if TYPE_CHECKING:
    from collections.abc import Mapping

    from arc.config import ArcSettings
    from arc.routines.config import JobSpec, RoutinesConfig

__all__ = [
    "ALERT_LOOKBACK",
    "RUN_STATUSES",
    "TIMELINE_END",
    "TIMELINE_START",
    "AlertsResponse",
    "BudgetResponse",
    "ConfigResponse",
    "ContextResponse",
    "HaltsResponse",
    "HealthStripResponse",
    "LlmResponse",
    "RunDetailResponse",
    "RunListResponse",
    "SessionResponse",
    "SourcesResponse",
    "load_alerts",
    "load_budget",
    "load_config",
    "load_context",
    "load_halts",
    "load_health",
    "load_llm",
    "job_labels",
    "load_run",
    "load_runs",
    "load_session",
    "load_sources",
    "resolve_day",
]

_STRICT = ConfigDict(extra="forbid", frozen=True)

#: The session timeline's visible span (ET), per the card.
TIMELINE_START = _dt.time(2, 0)  # E8.8d: the pre-market jobs (YouTube briefs 02:00) show
TIMELINE_END = _dt.time(22, 0)
ALERT_LOOKBACK = _dt.timedelta(days=7)
EXPIRED_LOOKBACK = _dt.timedelta(hours=24)
#: A source is late once it has not fetched within this many cadences.
SOURCE_LATE_FACTOR = 2
RUN_STATUSES: tuple[str, ...] = ("ok", "failed", "skipped", "running", "no_change")
MAX_PAGE_SIZE = 200
LOG_TAIL = 200

SlotStatus = Literal["done", "running", "failed", "skipped", "no_change", "missed", "future"]


def _day_bounds(day: _dt.date) -> tuple[_dt.datetime, _dt.datetime]:
    start = _dt.datetime.combine(day, _dt.time(0), tzinfo=ET)
    return start, start + _dt.timedelta(days=1)


def resolve_day(day: str | None, now: _dt.datetime) -> _dt.date:
    """``today`` | ``yesterday`` | ``YYYY-MM-DD`` (ET). Raises ValueError."""
    today = now.astimezone(ET).date()
    if day in (None, "", "today"):
        return today
    if day == "yesterday":
        return today - _dt.timedelta(days=1)
    try:
        return _dt.date.fromisoformat(str(day))
    except ValueError as exc:
        msg = f"day must be today, yesterday or YYYY-MM-DD, got {day!r}"
        raise ValueError(msg) from exc


def _is_no_change(summary: str | None) -> bool:
    return bool(summary) and str(summary).startswith("no_change")


def _duration_ms(a: _dt.datetime | None, b: _dt.datetime | None) -> int | None:
    if a is None or b is None:
        return None
    return int((b - a).total_seconds() * 1000)


# ---------------------------------------------------------------------------
# Runs (shared row model)
# ---------------------------------------------------------------------------


class RunRow(BaseModel):
    model_config = _STRICT

    run_id: str
    job: str
    status: str = Field(description="ok | failed | skipped | running")
    no_change: bool = Field(description="D31 loop run that skipped the LLM (inputs unchanged)")
    reason: str
    scheduled_for: _dt.datetime | None
    started_at: _dt.datetime | None
    finished_at: _dt.datetime | None
    duration_ms: int | None
    summary: str | None
    error: str | None
    chain_run_id: str | None
    step_index: int
    attempts: int
    route: str


def _run_row(r: sqlite3.Row, cut: Mapping[str, _dt.datetime] | None = None) -> RunRow:
    started, finished = parse_ts(r["started_at"]), parse_ts(r["finished_at"])
    scheduled = parse_ts(r["scheduled_for"])
    return RunRow(
        run_id=r["run_id"],
        # D54/D56: a pre-rename run reads under its current name (arc.journal.legacy).
        job=legacy.job_name(r["job"], scheduled, cut or {}),
        status=r["status"],
        no_change=_is_no_change(r["summary"]),
        reason=r["reason"],
        scheduled_for=scheduled,
        started_at=started,
        finished_at=finished,
        duration_ms=_duration_ms(started, finished),
        summary=r["summary"],
        error=r["error"],
        chain_run_id=r["chain_run_id"],
        step_index=int(r["step_index"] or 0),
        attempts=int(r["attempts"] or 1),
        route=f"/ops/runs/{r['run_id']}",
    )


# ---------------------------------------------------------------------------
# 1. Session timeline
# ---------------------------------------------------------------------------


class Slot(BaseModel):
    model_config = _STRICT

    job: str
    at: _dt.datetime = Field(description="Scheduled slot (ET)")
    status: SlotStatus
    run: RunRow | None = None
    chain_steps: int = Field(default=0, description="Chain step runs recorded under this root run")


class TimelineRow(BaseModel):
    model_config = _STRICT

    job: str
    label: str
    kind: Literal["source", "persona", "loop"]
    cadence: str
    slots: list[Slot]
    # E8.8d: config-driven band + explanation (routines.yaml label/group/persona/about).
    group: str = Field(default="other", description="Timeline group (TIMELINE_GROUPS key)")
    band: str = Field(
        default="other", description="Band key: the group, or `sources.<category>` for a source"
    )
    persona: str | None = Field(default=None, description="Persona chip (none for sources)")
    about: str | None = Field(default=None, description="One line on what the job does")
    window: str | None = Field(default=None, description="Intraday window (ET), when any")
    llm: bool = Field(default=False, description="Holds the LLM lock (calls a model)")
    writes: list[str] = Field(default_factory=list, description="Declared context kinds")
    categories: list[str] = Field(
        default_factory=list, description="D47 categories the source job feeds (all of them)"
    )


class TimelineBand(BaseModel):
    """E8.8d: one Session Timeline band, in display order."""

    model_config = _STRICT

    key: str = Field(description="`sources.<category>` or a TIMELINE_GROUPS key")
    label: str
    group: str
    group_label: str
    jobs: list[str] = Field(description="Job names in this band, in row order")


class SessionResponse(BaseModel):
    model_config = _STRICT

    as_of: _dt.datetime
    day: _dt.date
    start: _dt.datetime
    end: _dt.datetime
    loop_job: str = Field(description="The D31 trading loop's job (second row)")
    loop: TimelineRow | None
    rows: list[TimelineRow]
    bands: list[TimelineBand] = Field(
        default_factory=list, description="E8.8d: bands in display order (non-empty only)"
    )
    counts: dict[str, int] = Field(description="Slots by status")
    unscheduled: list[RunRow] = Field(
        default_factory=list, description="Root runs on the day with no slot (manual / event)"
    )


def _slot_status(run: RunRow | None, at: _dt.datetime, now: _dt.datetime) -> SlotStatus:
    if run is None:
        return "future" if at > now else "missed"
    if run.status == "ok":
        return "no_change" if run.no_change else "done"
    if run.status == "running":
        return "running"
    if run.status == "failed":
        return "failed"
    return "skipped"


def load_session(
    conn: sqlite3.Connection, routines: RoutinesConfig, *, now: _dt.datetime, day: _dt.date
) -> SessionResponse:
    from arc.routines.schedule import slots_between

    start = _dt.datetime.combine(day, TIMELINE_START, tzinfo=ET)
    end = _dt.datetime.combine(day, TIMELINE_END, tzinfo=ET)
    lo, hi = _day_bounds(day)
    rows = conn.execute(
        "SELECT * FROM routine_runs WHERE scheduled_for >= ? AND scheduled_for < ?"
        " ORDER BY scheduled_for, step_index, rowid",
        (to_db(lo), to_db(hi)),
    ).fetchall()
    roots: dict[tuple[str, str], RunRow] = {}
    steps: Counter[str] = Counter()
    unscheduled: list[RunRow] = []
    cut = legacy.cutovers(conn)
    for r in rows:
        if int(r["step_index"] or 0) > 0:
            if r["chain_run_id"]:
                steps[r["chain_run_id"]] += 1
            continue
        run = _run_row(r, cut)
        assert run.scheduled_for is not None
        roots[(run.job, to_db(run.scheduled_for))] = run
    loop_job = routines.loop.job
    out_rows: list[TimelineRow] = []
    loop_row: TimelineRow | None = None
    counts: Counter[str] = Counter()
    used: set[tuple[str, str]] = set()
    for name, (kind, spec) in routines.jobs().items():
        slots: list[Slot] = []
        for at in slots_between(spec, start - _dt.timedelta(microseconds=1), end):
            key = (name, to_db(at))
            run = roots.get(key)
            if run is not None:
                used.add(key)
            status = _slot_status(run, at, now)
            counts[status] += 1
            chain_id = run.chain_run_id if run else None
            slots.append(
                Slot(
                    job=name,
                    at=at,
                    status=status,
                    run=run,
                    chain_steps=steps.get(chain_id, 0) if chain_id else 0,
                )
            )
        if not slots:
            continue
        row = TimelineRow(
            job=name,
            label=_label(name, spec),
            kind="loop" if name == loop_job else kind.value,
            cadence=spec.cadence,
            slots=slots,
            **_display(name, kind.value, spec, routines),
        )
        if name == loop_job:
            loop_row = row
        else:
            out_rows.append(row)
    unscheduled = [run for key, run in roots.items() if key not in used]
    out_rows, bands = _bands(out_rows, loop_row, routines)
    return SessionResponse(
        as_of=now,
        day=day,
        start=start,
        end=end,
        loop_job=loop_job,
        loop=loop_row,
        rows=out_rows,
        bands=bands,
        counts=dict(counts),
        unscheduled=unscheduled,
    )


def _label(name: str, spec: JobSpec) -> str:
    label = spec.options.get("label") if isinstance(spec.options, dict) else None
    return str(label) if label else name


def _source_categories(spec: JobSpec) -> list[str]:
    """D47/D49 categories a source job feeds: its own ``category`` plus its feeds' and
    channels', in display order."""
    from arc.context.categories import CATEGORY_ORDER, normalize_category

    found: set[str] = set()
    opts = spec.options
    for raw in [
        opts.get("category"),
        *[f.get("category") for f in opts.get("feeds") or [] if isinstance(f, dict)],
        *[c.get("category") for c in opts.get("channels") or [] if isinstance(c, dict)],
    ]:
        if raw is None:
            continue
        cat = normalize_category(raw)
        if cat is not None:
            found.add(cat.value)
    return [c.value for c in CATEGORY_ORDER if c.value in found]


def _display(name: str, kind: str, spec: JobSpec, routines: RoutinesConfig) -> dict[str, Any]:
    """E8.8d: band, persona chip and ⓘ facts of a timeline row, from routines.yaml only.

    A source's band is its D47 category (the first in display order when its feeds span
    several); a persona's is its ``group:``. Anything unmapped lands in ``other``.
    """
    from arc.context.categories import REFERENCE

    opts = spec.options
    cats = _source_categories(spec) if kind == "source" else []
    # D56: reference data (no category) gets its own band after the six categories
    band_cats = [REFERENCE] if kind == "source" and not cats and opts.get("reference") else cats
    group = str(opts.get("group") or ("sources" if band_cats else "other"))
    band = f"sources.{band_cats[0]}" if group == "sources" and band_cats else group
    if group == "sources" and not band_cats:
        group = band = "other"
    llm = spec.llm if spec.llm is not None else kind == "persona"
    return {
        "group": group,
        "band": band,
        "persona": opts.get("persona") if kind != "source" else None,
        "about": opts.get("about"),
        "window": str(spec.window) if spec.window else None,
        "llm": bool(llm),
        "writes": list(spec.writes or []),
        "categories": cats,
    }


def _bands(
    rows: list[TimelineRow], loop: TimelineRow | None, routines: RoutinesConfig
) -> tuple[list[TimelineRow], list[TimelineBand]]:
    """Rows sorted into band order (config order inside a band) and the non-empty bands.

    The loop row stays out of ``rows`` (``SessionResponse.loop``) but is a member of its
    band, so the UI can place it under "Trading loop".
    """
    from arc.context.categories import CATEGORY_ORDER, REFERENCE
    from arc.routines.config import TIMELINE_GROUPS

    group_label = dict(TIMELINE_GROUPS)
    order: list[tuple[str, str, str]] = []  # (band key, label, group)
    for g, glabel in TIMELINE_GROUPS:
        if g == "sources":
            order += [
                (f"sources.{c.value}", routines.category_spec(c).label or c.value, g)
                for c in CATEGORY_ORDER
            ]
            order.append((f"sources.{REFERENCE}", REFERENCE_LABEL, g))  # D56
        else:
            order.append((g, glabel, g))
    rank = {key: i for i, (key, _, _) in enumerate(order)}
    every = [*([loop] if loop else []), *rows]
    ordered = sorted(every, key=lambda r: rank.get(r.band, len(order)))
    bands = [
        TimelineBand(
            key=key,
            label=label,
            group=g,
            group_label=group_label[g],
            jobs=[r.job for r in ordered if r.band == key],
        )
        for key, label, g in order
        if any(r.band == key for r in ordered)
    ]
    return [r for r in ordered if r is not loop], bands


# ---------------------------------------------------------------------------
# 2. Health strip
# ---------------------------------------------------------------------------


class HealthItem(BaseModel):
    model_config = _STRICT

    key: str
    label: str
    status: Literal["ok", "degraded", "failed", "unknown"]
    at: _dt.datetime | None = None
    age_s: int | None = None
    value: str
    threshold: str = Field(description="What the status is judged against")
    chip: str = Field(default="", description="E8.8d: compact chip text, e.g. `Tick ok 3m`")


class HealthStripResponse(BaseModel):
    model_config = _STRICT

    as_of: _dt.datetime
    items: list[HealthItem]
    checks: dict[str, dict[str, str]] = Field(
        default_factory=dict, description="The latest health heartbeat's checks: severity, summary"
    )


def _latest_hb(conn: sqlite3.Connection, component: str) -> sqlite3.Row | None:
    if not _has_table(conn, "heartbeats"):
        return None
    row: sqlite3.Row | None = conn.execute(
        "SELECT * FROM heartbeats WHERE component = ? ORDER BY at DESC, rowid DESC LIMIT 1",
        (component,),
    ).fetchone()
    return row


def _sev(value: str | None) -> Literal["ok", "degraded", "failed", "unknown"]:
    if value in ("ok", "degraded", "failed"):
        return value  # type: ignore[return-value]
    return "unknown"


def _age(at: _dt.datetime | None, now: _dt.datetime) -> int | None:
    return None if at is None else max(0, int((now - at).total_seconds()))


def _fmt_age(s: int | None) -> str:
    if s is None:
        return "never"
    if s < 90:
        return f"{s}s ago"
    if s < 90 * 60:
        return f"{s // 60} min ago"
    if s < 48 * 3600:
        return f"{s // 3600} h ago"
    return f"{s // 86400} d ago"


def _fmt_s(s: int) -> str:
    return f"{s // 60} min" if s % 3600 else f"{s // 3600} h"


def load_health(
    conn: sqlite3.Connection,
    *,
    now: _dt.datetime,
    tick_stale_s: int,
    health_every_s: int,
    log_path: Any | None = None,
    log_max_bytes: int | None = None,
) -> HealthStripResponse:
    """Tick / health heartbeat ages vs their thresholds, gateway and remote-access checks,
    and the JSON log's size vs its rotation size."""
    items: list[HealthItem] = []
    tick = _latest_hb(conn, "tick")
    tick_at = parse_ts(tick["at"]) if tick else None
    tick_age = _age(tick_at, now)
    tick_status: Literal["ok", "degraded", "failed", "unknown"]
    if tick_age is None:
        tick_status = "unknown"
    elif tick_age > tick_stale_s:
        tick_status = "failed"
    else:
        tick_status = _sev(tick["status"]) if tick else "unknown"
    items.append(
        HealthItem(
            key="tick",
            label="Tick heartbeat",
            status=tick_status,
            at=tick_at,
            age_s=tick_age,
            value=_fmt_age(tick_age),
            threshold=f"stale after {_fmt_s(tick_stale_s)}",
        )
    )
    health = _latest_hb(conn, "health")
    h_at = parse_ts(health["at"]) if health else None
    h_age = _age(h_at, now)
    h_stale = 3 * health_every_s
    h_status = _sev(health["status"]) if health else "unknown"
    if h_age is not None and h_age > h_stale:
        h_status = "failed"
    items.append(
        HealthItem(
            key="health",
            label="Health check",
            status=h_status,
            at=h_at,
            age_s=h_age,
            value=f"{health['status'] if health else 'none'} · {_fmt_age(h_age)}",
            threshold=f"runs every {_fmt_s(health_every_s)}; stale after {_fmt_s(h_stale)}",
        )
    )
    checks: dict[str, dict[str, str]] = {}
    detail = _json(health["detail"], {}) if health else {}
    for name, c in (detail.get("checks") or {}).items():
        if isinstance(c, dict):
            checks[str(name)] = {
                "severity": str(c.get("severity", "")),
                "summary": str(c.get("summary", "")),
            }
    for key, label, threshold in (
        ("gateway", "Gateway", "`hermes gateway status` ok; cron jobs fire"),
        ("remote_access", "Remote access", "Tailscale dashboard + tower ports answer"),
    ):
        c = checks.get(key)
        items.append(
            HealthItem(
                key=key,
                label=label,
                status=_sev(c["severity"]) if c else "unknown",
                at=h_at if c else None,
                age_s=h_age if c else None,
                value=c["summary"] if c else "not checked",
                threshold=threshold,
            )
        )
    size: int | None = None
    if log_path is not None:
        try:
            size = int(log_path.stat().st_size)
        except OSError:
            size = None
    cap = log_max_bytes or 0
    log_status: Literal["ok", "degraded", "failed", "unknown"] = (
        "unknown" if size is None else ("degraded" if cap and size > cap else "ok")
    )
    items.append(
        HealthItem(
            key="log",
            label="Log size",
            status=log_status,
            value="no log file" if size is None else f"{size / 1_000_000:.1f} MB",
            threshold=f"rotates at {cap / 1_000_000:.1f} MB" if cap else "-",
            chip=_log_chip(size, cap),
        )
    )
    return HealthStripResponse(as_of=now, items=[_with_chip(it) for it in items], checks=checks)


def _mb(n: int) -> str:
    """``1.2`` / ``5`` / ``0.002``: MB without a trailing ``.0`` and never a false 0."""
    mb = n / 1_000_000
    if mb >= 0.1:  # noqa: PLR2004 - one decimal from 100 KB up
        return f"{mb:.1f}".removesuffix(".0")
    return f"{mb:.3f}".rstrip("0").rstrip(".") or "0"


def _log_chip(size: int | None, cap: int) -> str:
    if size is None:
        return "Log none"
    return f"Log {_mb(size)} / {_mb(cap)} MB" if cap else f"Log {_mb(size)} MB"


#: E8.8d: short chip labels (the long explanation lives in the chip's ⓘ).
_CHIP_LABEL = {
    "tick": "Tick",
    "health": "Health check",
    "gateway": "Gateway",
    "remote_access": "Remote",
    "log": "Log",
}


def _with_chip(it: HealthItem) -> HealthItem:
    """``Tick ok 3m`` / ``Gateway ok`` / ``Log 1.2 / 5 MB``: one short line per check."""
    if it.chip:
        return it
    name = _CHIP_LABEL.get(it.key, it.label)
    state = "not checked" if it.status == "unknown" else it.status
    if it.age_s is not None and it.key in ("tick", "health"):
        from arc.context.categories import age_text

        chip = f"{name} {state} {age_text(_dt.timedelta(seconds=it.age_s))}"
    else:
        chip = f"{name} {state}"
    return it.model_copy(update={"chip": chip})


# ---------------------------------------------------------------------------
# 3. Alerts
# ---------------------------------------------------------------------------


class AlertRow(BaseModel):
    model_config = _STRICT

    id: str
    kind: str
    key: str
    message: str
    opened_at: _dt.datetime | None
    resolved_at: _dt.datetime | None
    duration_s: int | None = Field(description="Open → resolved (or → now while open)")
    open: bool
    posted_ts: str | None = None


class AlertsResponse(BaseModel):
    model_config = _STRICT

    as_of: _dt.datetime
    open: int
    alerts: list[AlertRow]


def load_alerts(conn: sqlite3.Connection, *, now: _dt.datetime) -> AlertsResponse:
    if not _has_table(conn, "ops_alerts"):
        return AlertsResponse(as_of=now, open=0, alerts=[])
    since = to_db(now - ALERT_LOOKBACK)
    rows = conn.execute(
        """SELECT * FROM ops_alerts WHERE resolved_at IS NULL OR resolved_at >= ?
           ORDER BY (resolved_at IS NULL) DESC, opened_at DESC, rowid DESC""",
        (since,),
    ).fetchall()
    out: list[AlertRow] = []
    for r in rows:
        opened, resolved = parse_ts(r["opened_at"]), parse_ts(r["resolved_at"])
        end = resolved or now
        out.append(
            AlertRow(
                id=r["id"],
                kind=r["kind"],
                key=r["key"],
                message=r["message"],
                opened_at=opened,
                resolved_at=resolved,
                duration_s=None if opened is None else max(0, int((end - opened).total_seconds())),
                open=resolved is None,
                posted_ts=r["posted_ts"],
            )
        )
    return AlertsResponse(as_of=now, open=sum(a.open for a in out), alerts=out)


# ---------------------------------------------------------------------------
# 4. Halts
# ---------------------------------------------------------------------------


class HaltRow(BaseModel):
    model_config = _STRICT

    id: str
    kind: str
    actor: str
    reason: str
    at: _dt.datetime | None
    cleared_at: _dt.datetime | None
    cleared_by: str | None
    active: bool
    run_id: str | None = None
    trades: int = Field(description="Proposals created while the halt was in force")
    trades_route: str = Field(description="Trades page filtered to the halt window")


class HaltsResponse(BaseModel):
    model_config = _STRICT

    as_of: _dt.datetime
    active: int
    halts: list[HaltRow]


def load_halts(conn: sqlite3.Connection, *, now: _dt.datetime, limit: int = 50) -> HaltsResponse:
    if not _has_table(conn, "halts"):
        return HaltsResponse(as_of=now, active=0, halts=[])
    rows = conn.execute(
        """SELECT * FROM halts ORDER BY (cleared_at IS NULL) DESC, at DESC, rowid DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    out: list[HaltRow] = []
    for r in rows:
        at, cleared = parse_ts(r["at"]), parse_ts(r["cleared_at"])
        trades = 0
        if at is not None and _has_table(conn, "proposals"):
            trades = int(
                conn.execute(
                    "SELECT COUNT(*) FROM proposals WHERE created_at >= ? AND created_at < ?",
                    (to_db(at), to_db(cleared or now)),
                ).fetchone()[0]
            )
        lo = (at or now).astimezone(ET).date()
        hi = (cleared or now).astimezone(ET).date()
        out.append(
            HaltRow(
                id=r["id"],
                kind=r["kind"],
                actor=r["actor"],
                reason=r["reason"],
                at=at,
                cleared_at=cleared,
                cleared_by=r["cleared_by"],
                active=cleared is None,
                run_id=r["run_id"],
                trades=trades,
                trades_route=f"/trades?date=custom&date_from={lo.isoformat()}&date_to={hi.isoformat()}",
            )
        )
    return HaltsResponse(as_of=now, active=sum(h.active for h in out), halts=out)


# ---------------------------------------------------------------------------
# 5. Runs list
# ---------------------------------------------------------------------------


class RunFilterOptions(BaseModel):
    model_config = _STRICT

    jobs: list[str]
    statuses: list[str] = Field(default_factory=lambda: list(RUN_STATUSES))


class RunListResponse(BaseModel):
    model_config = _STRICT

    as_of: _dt.datetime
    total: int
    page: int
    size: int
    rows: list[RunRow]
    options: RunFilterOptions


def load_runs(  # noqa: PLR0913 - one argument per filter
    conn: sqlite3.Connection,
    *,
    now: _dt.datetime,
    jobs: list[str] | None = None,
    statuses: list[str] | None = None,
    day: _dt.date | None = None,
    chain: str | None = None,
    page: int = 1,
    size: int = 50,
) -> RunListResponse:
    where: list[str] = []
    args: list[Any] = []
    cut = legacy.cutovers(conn)
    if jobs:
        jclause = f"job IN ({','.join('?' * len(jobs))})"
        args += jobs
        # D54/D56: a 'scalp*' filter also matches the pre-rename 'sweep*' / 'scout*'
        # runs, and 'research' the 'director' runs (each before its own cutover).
        for j in jobs:
            for old, key in legacy.legacy_names(j):
                jclause += " OR (job = ?"
                args.append(old)
                if key in cut:
                    jclause += " AND scheduled_for < ?"
                    args.append(to_db(cut[key]))
                jclause += ")"
        where.append(f"({jclause})")
    if statuses:
        parts: list[str] = []
        plain = [s for s in statuses if s != "no_change"]
        if plain:
            parts.append(
                f"(status IN ({','.join('?' * len(plain))})"
                " AND NOT (status = 'ok' AND COALESCE(summary, '') LIKE 'no_change%'))"
            )
            args += plain
        if "no_change" in statuses:
            parts.append("(status = 'ok' AND COALESCE(summary, '') LIKE 'no_change%')")
        where.append("(" + " OR ".join(parts) + ")")
    if day is not None:
        lo, hi = _day_bounds(day)
        where.append("scheduled_for >= ? AND scheduled_for < ?")
        args += [to_db(lo), to_db(hi)]
    if chain:
        where.append("(chain_run_id = ? OR run_id = ?)")
        args += [chain, chain]
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    size = max(1, min(size, MAX_PAGE_SIZE))
    page = max(1, page)
    total = int(
        conn.execute(f"SELECT COUNT(*) FROM routine_runs {clause}", args).fetchone()[0]  # noqa: S608 - placeholders only
    )
    rows = conn.execute(
        f"SELECT * FROM routine_runs {clause}"  # noqa: S608 - placeholders only
        " ORDER BY scheduled_for DESC, step_index DESC, rowid DESC LIMIT ? OFFSET ?",
        [*args, size, (page - 1) * size],
    ).fetchall()
    all_jobs = sorted(
        {
            legacy.job_name(r[0], None if r[1] is None else parse_ts(r[1]), cut)
            for r in conn.execute("SELECT job, MIN(scheduled_for) FROM routine_runs GROUP BY job")
        }
    )
    return RunListResponse(
        as_of=now,
        total=total,
        page=page,
        size=size,
        rows=[_run_row(r, cut) for r in rows],
        options=RunFilterOptions(jobs=all_jobs),
    )


# ---------------------------------------------------------------------------
# 6. Run detail
# ---------------------------------------------------------------------------


class EntryRef(BaseModel):
    model_config = _STRICT

    id: str
    kind: str
    subject: str
    produced_by: str | None = None
    undeclared: bool = Field(default=False, description="Kind outside the declared contract")


class PersonaCallRow(BaseModel):
    model_config = _STRICT

    id: str
    persona: str
    model: str
    status: str
    prompt_sha256: str
    created_at: _dt.datetime | None
    input_tokens: int | None = None
    output_tokens: int | None = None
    latency_ms: int | None = None
    cost_usd: float | None = None


class EventRow(BaseModel):
    model_config = _STRICT

    id: str
    name: str
    role: str
    created_at: _dt.datetime | None
    dispatched_at: _dt.datetime | None = None
    dispatched_by: str | None = None
    consumed_at: _dt.datetime | None = None
    consumed_by: list[str] = Field(default_factory=list)


class ContractView(BaseModel):
    model_config = _STRICT

    declared_reads: list[str] | None
    declared_writes: list[str] | None
    actual_reads: list[str]
    actual_writes: list[str]
    undeclared_reads: list[str]
    undeclared_writes: list[str]
    ok: bool


class LinkRef(BaseModel):
    model_config = _STRICT

    id: str
    label: str
    route: str | None = None


class StepView(BaseModel):
    """One run of a trace: the ``arc context trace`` element, typed."""

    model_config = _STRICT

    run: RunRow
    contract: ContractView
    read: list[EntryRef]
    wrote: list[EntryRef]
    outputs: dict[str, list[LinkRef]] = Field(
        description="Manifest output ids by kind, with links (context entries / proposals)"
    )
    events: list[EventRow]
    persona_calls: list[PersonaCallRow]
    proposals: list[LinkRef]
    decisions: list[str]
    gate_decisions: list[str]
    slack_posts: list[LinkRef]
    manifest: dict[str, Any] | None = Field(description="The stored D27 RunManifest (JSON)")
    label: str | None = Field(None, description="E8.8e: routines.yaml `label` of the job")
    persona: str | None = Field(None, description="E8.8e: routines.yaml `persona` (chip)")


class LogLine(BaseModel):
    model_config = _STRICT

    ts: str | None
    level: str
    event: str
    fields: dict[str, Any]


class RunDetailResponse(BaseModel):
    model_config = _STRICT

    as_of: _dt.datetime
    run_id: str
    chain_run_id: str | None
    step: StepView = Field(description="The requested run")
    chain: list[StepView] = Field(description="Every step of its chain (step order)")
    log: list[LogLine] = Field(description="JSON log lines mentioning the run (tail)")
    log_available: bool


def _entry_route(entry_id: str) -> str:
    return f"/ops/context/{entry_id}"


def _permalink(channel: str | None, ts: str) -> str | None:
    if not channel or not ts:
        return None
    return f"https://slack.com/archives/{channel}/p{ts.replace('.', '')}"


def job_labels(routines: RoutinesConfig) -> dict[str, tuple[str, str | None]]:
    """E8.8e: job -> (label, persona) from routines.yaml, for the run detail header."""
    out: dict[str, tuple[str, str | None]] = {}
    for name, (kind, spec) in routines.jobs().items():
        persona = spec.options.get("persona") if kind.value != "source" else None
        out[name] = (_label(name, spec), str(persona) if persona else None)
    return out


def _step(
    conn: sqlite3.Connection,
    t: dict[str, Any],
    slack_channel: str | None,
    labels: dict[str, tuple[str, str | None]] | None = None,
) -> StepView:
    row = conn.execute("SELECT * FROM routine_runs WHERE run_id = ?", (t["run_id"],)).fetchone()
    contract = t["contract"]
    un_r, un_w = set(contract["undeclared_reads"]), set(contract["undeclared_writes"])
    m = t["manifest"] or {}
    outputs: dict[str, list[LinkRef]] = {}
    for kind, ids in (m.get("output_ids") or {}).items():
        refs = []
        for i in ids or []:
            route = f"/trades/{i}" if kind == "proposals" else _entry_route(i)
            refs.append(LinkRef(id=str(i), label=str(i), route=route))
        outputs[str(kind)] = refs
    hashes = [str(h) for h in m.get("proposal_hashes") or []]
    run = _run_row(row, legacy.cutovers(conn))
    return StepView(
        run=run,
        contract=ContractView(
            declared_reads=t["declared"]["reads"],
            declared_writes=t["declared"]["writes"],
            actual_reads=sorted({e["kind"] for e in t["read"]}),
            actual_writes=sorted({e["kind"] for e in t["wrote"]}),
            undeclared_reads=sorted(un_r),
            undeclared_writes=sorted(un_w),
            ok=not (un_r or un_w),
        ),
        read=[
            EntryRef(
                id=e["id"],
                kind=e["kind"],
                subject=e["subject"],
                produced_by=e.get("produced_by"),
                undeclared=e["kind"] in un_r,
            )
            for e in t["read"]
        ],
        wrote=[
            EntryRef(id=e["id"], kind=e["kind"], subject=e["subject"], undeclared=e["kind"] in un_w)
            for e in t["wrote"]
        ],
        outputs=outputs,
        events=[
            EventRow(
                id=e["id"],
                name=e["name"],
                role=e["role"],
                created_at=parse_ts(e["created_at"]),
                dispatched_at=parse_ts(e["dispatched_at"]),
                dispatched_by=e["dispatched_by"],
                consumed_at=parse_ts(e["consumed_at"]),
                consumed_by=[str(x) for x in e["consumed_by"]],
            )
            for e in t["events"]
        ],
        persona_calls=[
            PersonaCallRow(
                id=c["id"],
                persona=c["persona"],
                model=c["model"],
                status=c["status"],
                prompt_sha256=c["prompt_sha256"],
                created_at=parse_ts(c["created_at"]),
                input_tokens=c["input_tokens"],
                output_tokens=c["output_tokens"],
                latency_ms=c["latency_ms"],
                cost_usd=c["cost_usd"],
            )
            for c in t["persona_calls"]
        ],
        proposals=[LinkRef(id=h, label=h[:12], route=f"/trades/{h}") for h in hashes],
        decisions=[str(x) for x in m.get("decision_ids") or []],
        gate_decisions=[str(x) for x in m.get("gate_decision_ids") or []],
        slack_posts=[
            LinkRef(id=str(ts), label=str(ts), route=_permalink(slack_channel, str(ts)))
            for ts in m.get("notifications") or []
        ],
        manifest=t["manifest"],
        label=(labels or {}).get(run.job, (None, None))[0],
        persona=(labels or {}).get(run.job, (None, None))[1],
    )


def _log_tail(log_path: Any | None, needle: str) -> tuple[list[LogLine], bool]:
    """JSON log lines (current + rotated) mentioning *needle*, oldest first."""
    if log_path is None:
        return [], False
    files = sorted(log_path.parent.glob(log_path.name + ".*"), reverse=True) + [log_path]
    found = False
    out: list[LogLine] = []
    for f in files:
        if not f.is_file():
            continue
        found = True
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            if needle not in line:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if not isinstance(d, dict):
                continue
            ts = d.pop("ts", None) or d.pop("timestamp", None)
            level = str(d.pop("level", "info"))
            event = str(d.pop("event", ""))
            out.append(
                LogLine(ts=None if ts is None else str(ts), level=level, event=event, fields=d)
            )
    return out[-LOG_TAIL:], found


def load_run(
    conn: sqlite3.Connection,
    run_id: str,
    *,
    now: _dt.datetime,
    log_path: Any | None = None,
    slack_channel: str | None = None,
    labels: dict[str, tuple[str, str | None]] | None = None,
) -> RunDetailResponse:
    """The run's trace plus its chain's. Raises LookupError for an unknown run id.

    *labels* (:func:`job_labels`) adds each step's routines.yaml label and persona."""
    row = conn.execute(
        "SELECT run_id, chain_run_id FROM routine_runs WHERE run_id = ?", (run_id,)
    ).fetchone()
    if row is None:
        msg = f"no routine run {run_id!r}"
        raise LookupError(msg)
    own = _step(conn, trace_runs(conn, run_id)[0], slack_channel, labels)
    chain_id = row["chain_run_id"]
    chain: list[StepView] = []
    if chain_id:
        chain = [_step(conn, t, slack_channel, labels) for t in trace_runs(conn, chain_id)]
    log, available = _log_tail(log_path, run_id)
    return RunDetailResponse(
        as_of=now,
        run_id=run_id,
        chain_run_id=chain_id,
        step=own,
        chain=chain,
        log=log,
        log_available=available,
    )


# ---------------------------------------------------------------------------
# 7. Order budget (D32)
# ---------------------------------------------------------------------------


class OrderRow(BaseModel):
    model_config = _STRICT

    id: str
    proposal_hash: str
    ticker: str | None
    kind: str | None
    state: str
    created_at: _dt.datetime | None
    route: str


class BudgetResponse(BaseModel):
    model_config = _STRICT

    as_of: _dt.datetime
    day: _dt.date
    used: int
    local: int
    reserved: int
    limit: int
    restrict_at: int
    open_limit: int
    close_reserve: int
    tier: str
    remaining_opens: int
    by_state: dict[str, int]
    orders: list[OrderRow]
    broker_checked: bool = Field(
        False, description="Always false: the tower never calls the broker (local count only)"
    )


def load_budget(
    conn: sqlite3.Connection, settings: ArcSettings, *, now: _dt.datetime
) -> BudgetResponse | None:
    """Today's D32 budget from the local store; ``None`` without the execution tables."""
    if not (_has_table(conn, "orders") and _has_table(conn, "executions")):
        return None
    from arc.budget.orders import OrderBudgetConfig, budget_state, count_orders

    day = now.astimezone(ET).date()
    cfg = OrderBudgetConfig.from_settings(settings)
    count = count_orders(conn, None, day)
    b = budget_state(count.used, cfg, day=day, count=count)
    lo, hi = _day_bounds(day)
    rows = conn.execute(
        """SELECT o.id, o.proposal_hash, o.state, o.created_at, e.kind,
                  (SELECT c.ticker FROM proposals p JOIN candidates c ON c.id = p.candidate_id
                    WHERE p.proposal_hash = o.proposal_hash) AS ticker
             FROM orders o LEFT JOIN executions e ON e.proposal_hash = o.proposal_hash
            WHERE o.created_at >= ? AND o.created_at < ?
            ORDER BY o.created_at DESC, o.rowid DESC""",
        (to_db(lo), to_db(hi)),
    ).fetchall()
    orders = [
        OrderRow(
            id=r["id"],
            proposal_hash=r["proposal_hash"],
            ticker=r["ticker"],
            kind=r["kind"],
            state=r["state"],
            created_at=parse_ts(r["created_at"]),
            route=f"/trades/{r['proposal_hash']}",
        )
        for r in rows
    ]
    return BudgetResponse(
        as_of=now,
        day=day,
        used=b.used,
        local=count.local,
        reserved=count.reserved,
        limit=b.limit,
        restrict_at=b.restrict_at,
        open_limit=b.open_limit,
        close_reserve=b.close_reserve,
        tier=b.tier.value,
        remaining_opens=b.remaining_opens,
        by_state=dict(Counter(o.state for o in orders)),
        orders=orders,
    )


# ---------------------------------------------------------------------------
# 8. Context store
# ---------------------------------------------------------------------------


class ContextKindRow(BaseModel):
    model_config = _STRICT

    kind: str
    active: int
    ttl: str | None = Field(description="Configured TTL (context_ttl in routines.yaml)")
    next_expiry: _dt.datetime | None
    remaining_fraction: float | None = Field(
        description="Latest entry's TTL left (0..1); None when it never expires"
    )
    latest_id: str | None
    latest_subject: str | None
    latest_at: _dt.datetime | None
    latest_by: str | None
    expired_24h: int


class ContextResponse(BaseModel):
    model_config = _STRICT

    as_of: _dt.datetime
    total_active: int
    expired_24h: int
    kinds: list[ContextKindRow]


def load_context(
    conn: sqlite3.Connection, routines: RoutinesConfig, *, now: _dt.datetime
) -> ContextResponse:
    """Entries valid at *now* per kind (``status`` is only flipped lazily, so validity is
    judged on ``expires_at``), every registered kind listed, even with 0 active."""
    from arc.context.kinds import KINDS

    nowdb = to_db(now)
    rows = conn.execute(
        """SELECT kind, COUNT(*) AS n, MIN(expires_at) AS next_exp FROM context_entries
            WHERE status = 'active' AND valid_from <= ?
              AND (expires_at IS NULL OR expires_at > ?)
            GROUP BY kind""",
        (nowdb, nowdb),
    ).fetchall()
    active = {r["kind"]: (int(r["n"]), r["next_exp"]) for r in rows}
    expired = {
        r[0]: int(r[1])
        for r in conn.execute(
            "SELECT kind, COUNT(*) FROM context_entries WHERE expires_at > ? AND expires_at <= ?"
            " GROUP BY kind",
            (to_db(now - EXPIRED_LOOKBACK), nowdb),
        )
    }
    kinds = list(KINDS) + sorted(k for k in set(active) | set(expired) if k not in KINDS)
    out: list[ContextKindRow] = []
    for kind in kinds:
        latest = conn.execute(
            "SELECT id, subject, created_at, produced_by, valid_from, expires_at"
            " FROM context_entries WHERE kind = ? ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (kind,),
        ).fetchone()
        frac: float | None = None
        if latest is not None and latest["expires_at"]:
            vf, ex = parse_ts(latest["valid_from"]), parse_ts(latest["expires_at"])
            if vf and ex and ex > vf:
                frac = max(0.0, min(1.0, (ex - now).total_seconds() / (ex - vf).total_seconds()))
        policy = routines.context_ttl.get(kind)
        n, nxt = active.get(kind, (0, None))
        out.append(
            ContextKindRow(
                kind=kind,
                active=n,
                ttl=str(policy.ttl) if policy and policy.ttl else None,
                next_expiry=parse_ts(nxt),
                remaining_fraction=frac,
                latest_id=latest["id"] if latest else None,
                latest_subject=latest["subject"] if latest else None,
                latest_at=parse_ts(latest["created_at"]) if latest else None,
                latest_by=(
                    legacy.job_name(
                        latest["produced_by"], parse_ts(latest["created_at"]), legacy.cutovers(conn)
                    )
                    if latest
                    else None
                ),
                expired_24h=expired.get(kind, 0),
            )
        )
    return ContextResponse(
        as_of=now,
        total_active=sum(k.active for k in out),
        expired_24h=sum(k.expired_24h for k in out),
        kinds=out,
    )


class ContextEntryResponse(BaseModel):
    model_config = _STRICT

    as_of: _dt.datetime
    id: str
    kind: str
    subject: str
    status: str
    produced_by: str
    run_id: str | None
    chain_run_id: str | None
    created_at: _dt.datetime | None
    valid_from: _dt.datetime | None
    expires_at: _dt.datetime | None
    supersedes_id: str | None
    schema_version: int
    payload: Any


def load_context_entry(
    conn: sqlite3.Connection, entry_id: str, *, now: _dt.datetime
) -> ContextEntryResponse:
    r = conn.execute("SELECT * FROM context_entries WHERE id = ?", (entry_id,)).fetchone()
    if r is None:
        msg = f"no context entry {entry_id!r}"
        raise LookupError(msg)
    return ContextEntryResponse(
        as_of=now,
        id=r["id"],
        kind=r["kind"],
        subject=r["subject"],
        status=r["status"],
        produced_by=legacy.job_name(
            r["produced_by"], parse_ts(r["created_at"]), legacy.cutovers(conn)
        ),
        run_id=r["run_id"],
        chain_run_id=r["chain_run_id"],
        created_at=parse_ts(r["created_at"]),
        valid_from=parse_ts(r["valid_from"]),
        expires_at=parse_ts(r["expires_at"]),
        supersedes_id=r["supersedes_id"],
        schema_version=int(r["schema_version"]),
        payload=_json(r["payload"], None),
    )


# ---------------------------------------------------------------------------
# 9. Sources (D30)
# ---------------------------------------------------------------------------


SourceStatus = Literal["ok", "idle", "pending", "backoff", "late", "failed"]
#: Worst-wins order for the category rollup (E8.8d).
SOURCE_STATUS_RANK: dict[str, int] = {
    "ok": 0,
    "idle": 1,
    "pending": 2,
    "backoff": 3,
    "late": 4,
    "failed": 5,
}


class ChannelBriefState(BaseModel):
    """E4.6: a YouTube channel's brief outcome in the latest ``youtube.briefs`` run today."""

    model_config = _STRICT

    outcome: Literal["ok", "pending", "no_video", "error", "not_run"]
    text: str = Field(description="`brief ok` / `pending: <reason>` / `no video in 24h` / …")
    video_title: str | None = None
    run_id: str | None = None


class SourceRow(BaseModel):
    model_config = _STRICT

    key: str
    label: str
    job: str
    category: str
    feed: Literal["scalp", "scout"] = Field(
        default="scalp",
        description="D54: Research feed (scalp = fast, intraday; scout = slow, daily or slower)",
    )
    weight: float = Field(description="Effective fairness weight (category × source share)")
    share_in_category: float | None = Field(
        default=None,
        description="Share of its category's Scalp budget (registry); null for typed-context "
        "sources (options data, YouTube), which never draw on the doc budget",
    )
    unit: Literal["docs", "entries"] = Field(
        default="docs", description="docs = raw documents; entries = typed context entries"
    )
    cadence: str
    every_s: int | None
    last_fetch: _dt.datetime | None = Field(description="Latest ok run of the source job")
    last_doc_at: _dt.datetime | None
    docs_today: int
    skipped_budget_today: int
    skipped_stale_today: int = Field(
        default=0, description="D47: docs closed skipped_stale today (older than max_age)"
    )
    filtered_today: int = Field(
        default=0,
        description="D55: docs stored today but closed `filtered` by the feed's title filter "
        "(never read by the Scalp)",
    )
    runs_24h: int
    failed_24h: int
    error_rate: float | None
    late: bool = Field(description=f"No fetch within {SOURCE_LATE_FACTOR} x cadence")
    backoff: str | None = Field(default=None, description="Cooldown / backoff state, when any")
    last_run_failed: bool = Field(default=False, description="The latest run of the job failed")
    brief: ChannelBriefState | None = Field(
        default=None, description="E4.6: today's brief status (YouTube channels only)"
    )
    status: SourceStatus = Field(default="ok", description="Row pill (worst condition)")


#: D56: the Sources page group for reference-data sources (not a category).
REFERENCE_LABEL = "Reference data"


class SourceCategoryRow(BaseModel):
    """E8.8d: one D47 category block header."""

    model_config = _STRICT

    key: str
    label: str
    weight: float = Field(description="categories.<c>.weight (config)")
    share: float | None = Field(
        description="Share of the Scalp doc budget (SourceRegistry.category_weights); null "
        "for categories the Scalp never reads (typed context only)"
    )
    max_age: str = Field(description="D47 freshness window")
    newest_doc_at: _dt.datetime | None
    status: SourceStatus = Field(description="Worst status of its sources")
    sources: int


class SourcesResponse(BaseModel):
    model_config = _STRICT

    as_of: _dt.datetime
    categories: list[SourceCategoryRow] = Field(
        default_factory=list, description="E8.8d: D47 categories in display order"
    )
    sources: list[SourceRow]


def _every_s(spec: JobSpec) -> int | None:
    if spec.every is not None:
        return int(spec.every.total_seconds())
    if spec.schedule:
        secs = sorted(t.hour * 3600 + t.minute * 60 for t in spec.schedule)
        gaps = [b - a for a, b in zip(secs, secs[1:], strict=False)]
        gaps.append(86_400 - secs[-1] + secs[0])
        return max(gaps)  # the longest gap: a source is late only after it
    return None


def _in_window(spec: JobSpec, now: _dt.datetime) -> bool:
    """Is *now* inside a period in which the source is expected to have fetched?"""
    from arc.routines.schedule import slots_between

    every = _every_s(spec)
    if every is None:
        return False
    window = _dt.timedelta(seconds=SOURCE_LATE_FACTOR * every)
    return bool(slots_between(spec, now - window, now))


def _caption_backoff(conn: sqlite3.Connection, now: _dt.datetime) -> str | None:
    if not _has_table(conn, "ingest_cursors"):
        return None
    row = conn.execute(
        "SELECT cursor_val FROM ingest_cursors WHERE connector = ?", ("youtube:captions_backoff",)
    ).fetchone()
    if row is None:
        return None
    state = _json(row[0], {})
    until = parse_ts(state.get("cooldown_until"))
    n = int(state.get("consecutive_rate_limits") or 0)
    if until is not None and until > now:
        return f"429 cooldown until {until:%H:%M} ET ({n} in a row)"
    if n:
        return f"{n} rate limit(s) in a row, no active cooldown"
    return None


def _channel_briefs(
    conn: sqlite3.Connection, job: str, lookback: str, day: tuple[_dt.datetime, _dt.datetime]
) -> tuple[dict[str, ChannelBriefState], str | None]:
    """E4.6: per-channel outcome of the latest manifest of *job* scheduled today."""
    if not _has_table(conn, "run_manifests"):
        return {}, None
    row = conn.execute(
        "SELECT m.run_id, m.payload FROM run_manifests m JOIN routine_runs r"
        " ON r.run_id = m.run_id WHERE m.job = ? AND r.scheduled_for >= ?"
        " AND r.scheduled_for < ? ORDER BY r.scheduled_for DESC, m.attempt DESC LIMIT 1",
        (job, to_db(day[0]), to_db(day[1])),
    ).fetchone()
    if row is None:
        return {}, None
    channels = (_json(row["payload"], {}).get("metrics") or {}).get("channels") or {}
    out: dict[str, ChannelBriefState] = {}
    for slug, c in channels.items():
        if not isinstance(c, dict):
            continue
        outcome = str(c.get("outcome") or "")
        title = c.get("title")
        if outcome in ("briefed", "existing"):
            state = ChannelBriefState(outcome="ok", text="brief ok", video_title=title)
        elif outcome == "pending":
            reason = c.get("pending_reason") or "no transcript yet"
            state = ChannelBriefState(
                outcome="pending", text=f"pending: {reason}", video_title=title
            )
        elif outcome == "no_video":
            state = ChannelBriefState(outcome="no_video", text=f"no video in {lookback}")
        else:
            err = c.get("error") or outcome or "unknown"
            state = ChannelBriefState(outcome="error", text=f"error: {err}", video_title=title)
        out[f"youtube.{slug}"] = state.model_copy(update={"run_id": row["run_id"]})
    return out, row["run_id"]


def _row_status(
    *, failed: bool, late: bool, backoff: str | None, brief: ChannelBriefState | None, idle: bool
) -> SourceStatus:
    if failed or (brief is not None and brief.outcome == "error"):
        return "failed"
    if late:
        return "late"
    if backoff:
        return "backoff"
    if brief is not None and brief.outcome == "pending":
        return "pending"
    if idle or (brief is not None and brief.outcome in ("no_video", "not_run")):
        return "idle"
    return "ok"


def load_sources(  # noqa: PLR0912, PLR0915 - one pass over the registry and the typed sources
    conn: sqlite3.Connection, routines: RoutinesConfig, *, now: _dt.datetime
) -> SourcesResponse:
    """Sources by D47 category. Shares come from :class:`SourceRegistry` (never
    recomputed here): ``category_weights()`` for a category, ``effective_weights()``
    for a source. Typed-context sources (options data, macro calendar, Finnhub) are
    listed by job with their context entries as the activity count.

    D56: reference-data sources (``reference: true``: earnings calendar, macro
    calendar, ex-dividend, IV history, Finnhub) are listed last under one
    ``Reference data`` group (key ``reference``, no share, no freshness window)."""
    from arc.context.categories import CATEGORY_ORDER, REFERENCE, normalize_category
    from arc.ingest.sources import SCALP_CATEGORIES, SourceRegistry

    reg = SourceRegistry.from_routines(routines)
    weights = reg.effective_weights()
    cat_w = reg.category_weights()
    day = _day_bounds(now.astimezone(ET).date())
    day_lo, day_hi = day
    since24 = to_db(now - _dt.timedelta(hours=24))
    # per-doc keys: resolve every recent doc through the registry (legacy rows lack source_key)
    docs: dict[str, int] = Counter()
    skipped: dict[str, int] = Counter()
    stale: dict[str, int] = Counter()
    filtered: dict[str, int] = Counter()
    last_doc: dict[str, _dt.datetime] = {}
    has_key = "source_key" in {r[1] for r in conn.execute("PRAGMA table_info(raw_docs)")}
    extra = ", source_key, scalp_status" if has_key else ""
    cols = "source, url, channel_id, ingested_at" + extra
    for r in conn.execute(
        f"SELECT {cols} FROM raw_docs WHERE ingested_at >= ?",  # noqa: S608 - fixed columns
        (to_db(now - _dt.timedelta(days=2)),),
    ):
        key = reg.key_for(dict(r))
        at = parse_ts(r["ingested_at"])
        if at is None:
            continue
        if key not in last_doc or at > last_doc[key]:
            last_doc[key] = at
        if day_lo <= at < day_hi:
            docs[key] += 1
            if has_key and r["scalp_status"] == "skipped_budget":
                skipped[key] += 1
            if has_key and r["scalp_status"] == "skipped_stale":
                stale[key] += 1
            if has_key and r["scalp_status"] == "filtered":
                filtered[key] += 1
    runs = defaultdict(lambda: [0, 0])
    last_ok: dict[str, _dt.datetime] = {}
    last_status: dict[str, str] = {}
    for r in conn.execute(
        "SELECT job, status, finished_at FROM routine_runs WHERE scheduled_for >= ?"
        " AND step_index = 0 ORDER BY scheduled_for, rowid",
        (since24,),
    ):
        runs[r["job"]][0] += 1
        if r["status"] == "failed":
            runs[r["job"]][1] += 1
        if r["status"] in ("ok", "failed"):
            last_status[r["job"]] = r["status"]
    for r in conn.execute(
        "SELECT job, MAX(finished_at) FROM routine_runs WHERE status = 'ok' GROUP BY job"
    ):
        t = parse_ts(r[1])
        if t is not None:
            last_ok[r[0]] = t
    backoff = _caption_backoff(conn, now)

    def _common(job: str) -> dict[str, Any]:
        found = routines.job(job)
        spec = found[1] if found else None
        every = _every_s(spec) if spec else None
        n, failed = runs.get(job, [0, 0])
        last = last_ok.get(job)
        late = False
        if spec is not None and every is not None and _in_window(spec, now):
            late = last is None or (now - last).total_seconds() > SOURCE_LATE_FACTOR * every
        return {
            "cadence": spec.cadence if spec else "-",
            "every_s": every,
            "last_fetch": last,
            "runs_24h": n,
            "failed_24h": failed,
            "error_rate": round(failed / n, 4) if n else None,
            "late": late,
            "last_run_failed": last_status.get(job) == "failed",
        }

    out: list[SourceRow] = []
    briefs_by_job: dict[str, dict[str, ChannelBriefState]] = {}
    for s in reg.sources.values():
        common = _common(s.job)
        brief: ChannelBriefState | None = None
        if s.channel is not None and s.job.startswith("youtube"):
            if s.job not in briefs_by_job:
                found = routines.job(s.job)
                lookback = str((found[1].options.get("lookback") if found else None) or "24h")
                briefs_by_job[s.job] = _channel_briefs(conn, s.job, lookback, day)[0]
            brief = briefs_by_job[s.job].get(s.key) or ChannelBriefState(
                outcome="not_run", text="not run today"
            )
        src_backoff = backoff if s.job.startswith("youtube") else None
        c_share = cat_w.get(s.category)
        share = weights.get(s.key)
        if c_share and share:
            in_cat: float | None = round(share / c_share, 4)
        elif s.channel is not None:  # D49: a channel's split of its YouTube category
            in_cat = round(reg.share_in_category(s.key), 4)
        else:
            in_cat = None
        out.append(
            SourceRow(
                key=s.key,
                label=s.display,
                job=s.job,
                category=s.category_key,
                # D54: video / options data / reference data are never Scalp-read (slow feed)
                feed=s.feed if s.category in SCALP_CATEGORIES else "scout",
                weight=round(weights.get(s.key, 0.0), 4),
                share_in_category=in_cat,
                last_doc_at=last_doc.get(s.key),
                docs_today=docs.get(s.key, 0),
                skipped_budget_today=skipped.get(s.key, 0),
                skipped_stale_today=stale.get(s.key, 0),
                filtered_today=filtered.get(s.key, 0),
                backoff=src_backoff,
                brief=brief,
                status=_row_status(
                    failed=common["last_run_failed"],
                    late=common["late"],
                    backoff=src_backoff,
                    brief=brief,
                    idle=False,
                ),
                **common,
            )
        )
    # Typed-context sources (no raw docs): one row per job, counted by context entries.
    in_registry = {s.job for s in reg.sources.values()}
    entries: dict[str, int] = Counter()
    newest: dict[str, _dt.datetime] = {}
    if _has_table(conn, "context_entries"):
        for r in conn.execute(
            "SELECT produced_by, created_at FROM context_entries WHERE created_at >= ?",
            (to_db(now - _dt.timedelta(days=7)),),
        ):
            at = parse_ts(r["created_at"])
            if at is None:
                continue
            job = r["produced_by"]
            if job not in newest or at > newest[job]:
                newest[job] = at
            if day_lo <= at < day_hi:
                entries[job] += 1
    for job, spec in routines.sources.items():
        if not spec.enabled or job in in_registry:
            continue
        cat = normalize_category(spec.options.get("category"))
        if cat is None and not routines.is_reference(job):
            continue
        common = _common(job)
        out.append(
            SourceRow(
                key=job,
                label=_label(job, spec),
                job=job,
                category=cat.value if cat is not None else REFERENCE,
                feed="scout",  # D54: typed context is never Scalp-read (slow feed)
                weight=0.0,
                share_in_category=None,
                unit="entries",
                last_doc_at=newest.get(job),
                docs_today=entries.get(job, 0),
                skipped_budget_today=0,
                status=_row_status(
                    failed=common["last_run_failed"],
                    late=common["late"],
                    backoff=None,
                    brief=None,
                    idle=common["runs_24h"] == 0 and job not in newest,
                ),
                **common,
            )
        )
    rank = {c.value: i for i, c in enumerate(CATEGORY_ORDER)}
    out.sort(key=lambda r: rank.get(r.category, len(rank)))
    categories: list[SourceCategoryRow] = []
    for c in CATEGORY_ORDER:
        rows = [r for r in out if r.category == c.value]
        if not rows:
            continue
        spec = reg.category_spec(c)
        stamps = [r.last_doc_at for r in rows if r.last_doc_at is not None]
        share = cat_w.get(c)
        categories.append(
            SourceCategoryRow(
                key=c.value,
                label=spec.label,
                weight=spec.weight,
                share=round(share, 4) if share is not None else None,
                max_age=str(spec.max_age),
                newest_doc_at=max(stamps) if stamps else None,
                status=max((r.status for r in rows), key=lambda s: SOURCE_STATUS_RANK[s]),
                sources=len(rows),
            )
        )
    ref_rows = [r for r in out if r.category == REFERENCE]
    if ref_rows:  # D56: reference data is one group after the six categories
        stamps = [r.last_doc_at for r in ref_rows if r.last_doc_at is not None]
        categories.append(
            SourceCategoryRow(
                key=REFERENCE,
                label=REFERENCE_LABEL,
                weight=0.0,
                share=None,
                max_age="-",
                newest_doc_at=max(stamps) if stamps else None,
                status=max((r.status for r in ref_rows), key=lambda s: SOURCE_STATUS_RANK[s]),
                sources=len(ref_rows),
            )
        )
    return SourcesResponse(as_of=now, categories=categories, sources=out)


# ---------------------------------------------------------------------------
# 10. LLM usage
# ---------------------------------------------------------------------------


class LlmDay(BaseModel):
    model_config = _STRICT

    day: _dt.date
    calls: int
    input_tokens: int
    output_tokens: int
    cost_usd: float
    by_persona: dict[str, float] = Field(description="Cost by persona (USD)")
    by_model: dict[str, float] = Field(description="Cost by model (USD)")


class LlmGroup(BaseModel):
    model_config = _STRICT

    persona: str
    model: str
    calls: int
    input_tokens: int
    output_tokens: int
    cost_usd: float
    failed: int


class LlmResponse(BaseModel):
    model_config = _STRICT

    as_of: _dt.datetime
    days: int
    today_cost: float
    yesterday_cost: float
    total_cost: float
    series: list[LlmDay]
    today: list[LlmGroup]
    period: list[LlmGroup]
    personas: list[str]
    models: list[str]


def load_llm(conn: sqlite3.Connection, *, now: _dt.datetime, days: int = 30) -> LlmResponse:
    """``persona_calls`` (Research/Quant/Risk/…) plus ``scalp_batches`` (Scalp, digest stage)
    per ET day; cost is NULL for fixtures, counted as 0."""
    days = max(1, min(days, 365))
    today = now.astimezone(ET).date()
    first = today - _dt.timedelta(days=days - 1)
    lo, _ = _day_bounds(first)
    _, hi = _day_bounds(today)
    calls: list[tuple[_dt.date, str, str, int, int, float, bool]] = []
    cuts = legacy.cutovers(conn)  # D56: pre-rename "director" calls read as Research
    if _has_table(conn, "persona_calls"):
        for r in conn.execute(
            "SELECT persona, model, status, created_at, input_tokens, output_tokens, cost_usd"
            " FROM persona_calls WHERE created_at >= ? AND created_at < ?",
            (to_db(lo), to_db(hi)),
        ):
            at = parse_ts(r["created_at"])
            if at is None:
                continue
            calls.append(
                (
                    at.date(),
                    legacy.persona_key(r["persona"], at, cuts),
                    r["model"],
                    int(r["input_tokens"] or 0),
                    int(r["output_tokens"] or 0),
                    float(r["cost_usd"] or 0.0),
                    r["status"] != "ok",
                )
            )
    if _has_table(conn, "scalp_batches"):
        sb_cols = {c[1] for c in conn.execute("PRAGMA table_info(scalp_batches)")}
        if {"stage", "input_tokens", "cost_usd"} <= sb_cols:
            for r in conn.execute(
                "SELECT stage, model, status, created_at, input_tokens, output_tokens, cost_usd"
                " FROM scalp_batches WHERE created_at >= ? AND created_at < ?",
                (to_db(lo), to_db(hi)),
            ):
                at = parse_ts(r["created_at"])
                if at is None:
                    continue
                persona = "scalp" if r["stage"] == "scalp" else f"scalp.{r['stage']}"
                calls.append(
                    (
                        at.date(),
                        persona,
                        r["model"],
                        int(r["input_tokens"] or 0),
                        int(r["output_tokens"] or 0),
                        float(r["cost_usd"] or 0.0),
                        r["status"] != "ok",
                    )
                )
    series: list[LlmDay] = []
    by_day: dict[_dt.date, list[tuple[_dt.date, str, str, int, int, float, bool]]] = defaultdict(
        list
    )
    for c in calls:
        by_day[c[0]].append(c)
    for i in range(days):
        d = first + _dt.timedelta(days=i)
        cs = by_day.get(d, [])
        per: dict[str, float] = defaultdict(float)
        mod: dict[str, float] = defaultdict(float)
        for c in cs:
            per[c[1]] += c[5]
            mod[c[2]] += c[5]
        series.append(
            LlmDay(
                day=d,
                calls=len(cs),
                input_tokens=sum(c[3] for c in cs),
                output_tokens=sum(c[4] for c in cs),
                cost_usd=round(sum(c[5] for c in cs), 6),
                by_persona={k: round(v, 6) for k, v in sorted(per.items())},
                by_model={k: round(v, 6) for k, v in sorted(mod.items())},
            )
        )

    def groups(rows: list[tuple[_dt.date, str, str, int, int, float, bool]]) -> list[LlmGroup]:
        g: dict[tuple[str, str], list[Any]] = {}
        for c in rows:
            acc = g.setdefault((c[1], c[2]), [0, 0, 0, 0.0, 0])
            acc[0] += 1
            acc[1] += c[3]
            acc[2] += c[4]
            acc[3] += c[5]
            acc[4] += int(c[6])
        return sorted(
            (
                LlmGroup(
                    persona=p,
                    model=m,
                    calls=a[0],
                    input_tokens=a[1],
                    output_tokens=a[2],
                    cost_usd=round(a[3], 6),
                    failed=a[4],
                )
                for (p, m), a in g.items()
            ),
            key=lambda x: (-x.cost_usd, -x.calls, x.persona, x.model),
        )

    yesterday = today - _dt.timedelta(days=1)
    return LlmResponse(
        as_of=now,
        days=days,
        today_cost=round(sum(c[5] for c in by_day.get(today, [])), 6),
        yesterday_cost=round(sum(c[5] for c in by_day.get(yesterday, [])), 6),
        total_cost=round(sum(c[5] for c in calls), 6),
        series=series,
        today=groups(by_day.get(today, [])),
        period=groups(calls),
        personas=sorted({c[1] for c in calls}),
        models=sorted({c[2] for c in calls}),
    )


# ---------------------------------------------------------------------------
# 11. Effective config (D26)
# ---------------------------------------------------------------------------


class ConfigKeyRow(BaseModel):
    model_config = _STRICT

    key: str
    group: str
    value: Any
    value_text: str
    default: Any
    default_text: str
    source: Literal["yaml", "override"]
    bounds: str
    hard_ceiling: Any = None
    risk: str
    description: str
    env: str | None = None
    last_change_id: int | None = None
    last_change_at: _dt.datetime | None = None
    last_change_by: str | None = None
    # E8.8e: the registry entry's shape, for the full-page view (allowed values, list layout)
    value_type: str = Field(
        default="", description="Registry ValueType (float, choice, tickers, …)"
    )
    is_list: bool = Field(default=False, description="A list value (tickers, ids): own full line")
    choices: list[str] = Field(
        default_factory=list, description="Categorical options, safest first"
    )
    min: float | None = None
    max: float | None = None
    unit: str = ""


class ConfigChangeRow(BaseModel):
    model_config = _STRICT

    id: int
    key: str
    old: Any = None
    new: Any = None
    is_default: bool
    actor: str
    reason: str | None
    at: _dt.datetime
    source: str
    status: str = Field(description="applied | reverted")
    supersedes_id: int | None = Field(description="Revert marker: the change this one undid")
    direction: str
    halted: bool
    # E8.8e: registry formatting (null when the key is no longer in the registry)
    group: str | None = None
    is_list: bool = False
    old_text: str | None = None
    new_text: str | None = None


class ConfigGroupRow(BaseModel):
    """E8.8e: one registry group (a page section), in registry order."""

    model_config = _STRICT

    key: str
    label: str
    keys: int


#: E8.8e section labels for the registry groups (Title Case, D48). A group missing here
#: falls back to its key capitalised, so a new registry group needs no tower change.
GROUP_LABELS: dict[str, str] = {
    "account": "Account",
    "universe": "Universe",
    "risk": "Risk",
    "entries": "Entries",
    "exits": "Exits",
    "positions": "Positions",
    "execution": "Execution",
    "costs": "Costs",
    "approvals": "Approvals",
    "routines": "Routines",
    "experiments": "Experiments",
}


class ConfigResponse(BaseModel):
    model_config = _STRICT

    as_of: _dt.datetime
    config_version: int
    env: str
    account_profile: str
    keys: list[ConfigKeyRow]
    changes: list[ConfigChangeRow]
    groups: list[ConfigGroupRow] = Field(
        default_factory=list, description="E8.8e: registry groups in registry order (sections)"
    )
    actor_names: dict[str, str] = Field(
        default_factory=dict,
        description="E8.8e: `tower.actor_names` (Slack id -> display name); unknown ids as is",
    )
    note: str | None = None
    scorecard_gate: str | None = Field(
        None,
        description="E6.6a: scorecard gate line, e.g. 'scorecard gate: OFF (opt-out) — 3 closed "
        "trades < 30 required; …' (null when the store has no trade tables)",
    )
    auto_approve: AutoApproveView | None = Field(
        None, description="E8.8d: the Auto-Approve widget as key/values (null without trade tables)"
    )


class AutoApproveView(BaseModel):
    """E8.8d: paper / live / scorecard gate / last flip, for a KeyValue layout."""

    model_config = _STRICT

    paper: bool
    live: bool
    env: str
    scorecard_gate: Literal["off", "met", "unmet"] = Field(
        description="off = opt-out; unmet = holding opens"
    )
    reason: str = Field(description="What the gate would say (its criteria vs the scorecard)")
    blocks: bool = Field(description="True when the gate is holding opens right now")
    last_flip_key: str | None = None
    last_flip_at: _dt.datetime | None = None
    last_flip_by: str | None = None
    last_flip_to: Any = None


ConfigResponse.model_rebuild()  # resolves the forward reference to AutoApproveView


def _auto_approve_view(
    conn: sqlite3.Connection, settings: ArcSettings, keys: list[ConfigKeyRow], now: _dt.datetime
) -> AutoApproveView | None:
    if not (_has_table(conn, "open_structures") and _has_table(conn, "executions")):
        return None
    from arc.journal.scorecard import auto_approve_gate

    gate = auto_approve_gate(conn, settings, now=now)
    state = gate.readiness.gate_state(gate.scorecard_gate)
    line = gate.line
    reason = line.split(" — ", 1)[1] if " — " in line else line
    by_key = {k.key: k for k in keys}
    flips = [
        by_key[k]
        for k in ("auto_approve.paper", "auto_approve.live", "auto_approve.scorecard_gate")
        if k in by_key and by_key[k].last_change_at is not None
    ]
    last = max(flips, key=lambda k: k.last_change_at or now, default=None)
    paper = by_key.get("auto_approve.paper")
    live = by_key.get("auto_approve.live")
    return AutoApproveView(
        paper=bool(paper.value) if paper else bool(settings.auto_approve),
        live=bool(live.value) if live else False,
        env=settings.env.value,
        scorecard_gate=state,  # type: ignore[arg-type]
        reason=reason,
        blocks=state == "unmet",
        last_flip_key=last.key if last else None,
        last_flip_at=last.last_change_at if last else None,
        last_flip_by=last.last_change_by if last else None,
        last_flip_to=last.value if last else None,
    )


def _scorecard_gate_line(
    conn: sqlite3.Connection, settings: ArcSettings, now: _dt.datetime
) -> str | None:
    """E6.6a: the same line the weekly scorecard and ``arc approve auto status`` show."""
    if not (_has_table(conn, "open_structures") and _has_table(conn, "executions")):
        return None
    from arc.journal.scorecard import auto_approve_gate

    return auto_approve_gate(conn, settings, now=now).line


def load_config(
    conn: sqlite3.Connection,
    base: ArcSettings,
    *,
    now: _dt.datetime,
    history: int = 50,
    actor_names: dict[str, str] | None = None,
) -> ConfigResponse:
    """The D26 effective config via :class:`ControlService` (reads only: ``show``,
    ``history``, ``version``, ``settings``). No override tables: yaml values, version 0.

    *actor_names* is ``tower.actor_names`` (Slack id -> display name), passed through for the
    change log; it never changes a value."""
    from arc.control.service import ControlService

    has = _has_table(conn, "config_changes") and _has_table(conn, "config_pending")
    note: str | None = None
    if has:
        svc = ControlService(conn, base=base, now=lambda: now, is_halted=lambda: False)
        views, version, changes = _config_views(svc, history)
        settings = svc.settings()
    else:  # a store without the D26 tables: nothing can be overridden
        from arc.store.migrate import migrate

        mem = sqlite3.connect(":memory:")
        mem.row_factory = sqlite3.Row
        try:
            migrate(mem)  # a scratch in-memory DB; the audit store is never touched
            svc = ControlService(mem, base=base, now=lambda: now, is_halted=lambda: False)
            views, version, changes = _config_views(svc, history)
            settings = svc.settings()
        finally:
            mem.close()
        note = "No override tables in this store (D26 not migrated): file/env values only."
    keys = [_key_row(v) for v in views]
    return ConfigResponse(
        as_of=now,
        config_version=version,
        env=settings.env.value,
        account_profile=settings.account_profile,
        keys=keys,
        changes=changes,
        groups=_groups(keys),
        actor_names=dict(actor_names or {}),
        note=note,
        scorecard_gate=_scorecard_gate_line(conn, settings, now),
        auto_approve=_auto_approve_view(conn, settings, keys, now),
    )


_LIST_TYPES = frozenset({"tickers", "user_ids"})


def _is_list(vtype: str, *values: Any) -> bool:
    """A list of plain members (tickers, ids): rendered as chips on its own line and diffed
    as ``+A -B``. Take-profit target lists (dicts) stay scalar text."""
    if vtype in _LIST_TYPES:
        return True
    if vtype:
        return False
    lists = [v for v in values if isinstance(v, list)]
    return bool(lists) and all(not isinstance(x, dict) for v in lists for x in v)


def _tunable(key: str) -> Any | None:
    from arc.control.registry import TunableError, lookup

    try:
        return lookup(key)
    except TunableError:
        return None


def _key_row(v: dict[str, Any]) -> ConfigKeyRow:
    t = _tunable(v["key"])
    vtype = t.type.value if t is not None else ""
    return ConfigKeyRow(
        key=v["key"],
        group=v["group"],
        value=v["value"],
        value_text=str(v["value_text"]),
        default=v["default"],
        default_text=str(v["default_text"]),
        source="override" if v["overridden"] else "yaml",
        bounds=str(v["bounds"]),
        hard_ceiling=v["hard_ceiling"],
        risk=str(v["risk"]),
        description=str(v["description"]),
        env=v["env"],
        last_change_id=v["last_change_id"],
        last_change_at=parse_ts(v["last_change_at"]),
        last_change_by=v["last_change_by"],
        value_type=vtype,
        is_list=_is_list(vtype, v["value"]),
        choices=list(t.choices) if t is not None else [],
        min=t.min if t is not None else None,
        max=t.max if t is not None else None,
        unit=t.unit if t is not None else "",
    )


def _groups(keys: list[ConfigKeyRow]) -> list[ConfigGroupRow]:
    """Registry groups in registry (enum) order, then any group the registry does not list."""
    from arc.control.registry import Group

    counts = Counter(k.group for k in keys)
    order = [g.value for g in Group] + sorted(set(counts) - {g.value for g in Group})
    return [
        ConfigGroupRow(
            key=g, label=GROUP_LABELS.get(g, g.replace("_", " ").title()), keys=counts[g]
        )
        for g in order
        if counts[g]
    ]


def _config_views(
    svc: Any, history: int
) -> tuple[list[dict[str, Any]], int, list[ConfigChangeRow]]:
    views = [v.as_json() for v in svc.show()]
    changes = [_change_row(c) for c in svc.history(limit=history)]
    return views, int(svc.version()), changes


def _change_row(c: Any) -> ConfigChangeRow:
    from arc.control.registry import format_value

    t = _tunable(c.key)

    def fmt(v: Any) -> str | None:
        if v is None:
            return None
        try:
            return format_value(t, v) if t is not None else str(v)
        except (TypeError, ValueError, KeyError):  # a stored value the registry no longer parses
            return str(v)

    vtype = t.type.value if t is not None else ""
    return ConfigChangeRow(
        id=c.id,
        key=c.key,
        old=c.old,
        new=c.new,
        is_default=c.is_default,
        actor=c.actor,
        reason=c.reason,
        at=c.at,
        source=c.source,
        status=c.status,
        supersedes_id=c.supersedes_id,
        direction=c.direction,
        halted=c.halted,
        group=t.group.value if t is not None else None,
        is_list=_is_list(vtype, c.old, c.new),
        old_text=fmt(c.old),
        new_text=fmt(c.new),
    )
