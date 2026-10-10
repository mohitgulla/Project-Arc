"""Ladder liveness and re-attach (E11.2, D72).

A Broker ladder runs in a detached process (D34). If that process dies after a
submit, its DAY order keeps working at the venue with no owner: the ladder would
have cancelled it after one step, but nothing else looks at it until the 16:30
reconcile. :func:`reattach` (job ``broker.reattach``, every tick in RTH) finds
every ``working`` execution whose owner is gone and finishes it *where it
stands*, exactly as the ladder would have:

- owner liveness: the run's flock (``run-<run id>``, and ``<job>:<event id>``
  for an event run) is held **and** its ``heartbeat_at`` is fresher than
  ``execution_reattach_stale_s``. Either failing makes it an orphan. A held lock
  with a stale heartbeat is *wedged*: alert only; after
  ``execution_reattach_kill_after_s`` the recorded pid gets one SIGTERM (never
  SIGKILL); adoption waits for the next tick, once the lock is free.
- adoption: ``executions.adopted_by_run_id`` is set first (the fence the ladder
  checks before every write), then each non-terminal order is resolved at the
  broker (by id, else by ``client_order_id``), a working one is cancelled and
  the cancel confirmed, fills go through :mod:`arc.execution.fills` (the
  ladder's own position-model code), the execution is finished, the dead run is
  failed with the cause and its event consumed.
- it **never submits** and never walks to the next step. Cancelling and
  recording a fill that already exists both reduce risk, so it runs under a
  halt and checks no gate.

Deterministic: DB + the injected broker, lock manager, clock, sleep and kill;
no network, no LLM.
"""

from __future__ import annotations

import datetime as _dt  # noqa: TC003 - pydantic field types
import os
import re
import signal
import sqlite3  # noqa: TC003 - kept beside the DB helpers
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Literal

import structlog
from pydantic import BaseModel, ConfigDict, Field

from arc.context.ttl import from_db, to_db
from arc.execution.fills import apply_fill
from arc.execution.ladder import (
    AttemptRecord,
    ExecutionAdoptedError,
    LadderContext,
    resume_attempt,
)
from arc.execution.resolve import resolve_unknown_submit
from arc.journal.reasons import Choice, ReasonCode
from arc.models import OrderState, Structure
from arc.routines.locks import LockBusyError, NullLocks
from arc.routines.runs import RoutineEventRepo, RoutineRunRepo, owner_lock, pid_alive
from arc.store.execution import ExecutionRepo
from arc.structures import parse_occ

if TYPE_CHECKING:
    from collections.abc import Callable

    from arc.broker.base import BrokerAdapter, BrokerOrderStatus
    from arc.config import ArcSettings
    from arc.routines.locks import LockManager

__all__ = [
    "ACTOR",
    "AdoptedExecution",
    "Orphan",
    "OrphanOrder",
    "ReattachReport",
    "RunLiveness",
    "find_orphans",
    "reattach",
]

log = structlog.get_logger(__name__)

ACTOR = "arc:reattach"
_OPEN_STATES = (
    OrderState.PROPOSED,
    OrderState.GATED,
    OrderState.APPROVED,
    OrderState.SUBMITTED,
    OrderState.PARTIALLY_FILLED,
)
_STEP = re.compile(r"\.s(\d+)$")
_LIMIT = re.compile(r"limit ([+-]?\d+(?:\.\d+)?)")


class RunLiveness(BaseModel):
    """Is the process that owns a ladder run still working it?"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str | None
    run_status: str | None
    pid: int | None
    pid_alive: bool | None  # None when the pid is unknown
    heartbeat_at: _dt.datetime | None
    heartbeat_age_s: float | None
    lock_held: bool | None  # None = unknowable (dry run)
    alive: bool


class OrphanOrder(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    order_id: str
    client_order_id: str
    broker_order_id: str | None
    state: OrderState
    step: int
    limit_price: Decimal


class Orphan(BaseModel):
    """A ``working`` execution whose owner is gone (or, in a dry run, may be)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    proposal_hash: str
    kind: Literal["open", "close"]
    ticker: str
    run_id: str | None
    event_id: str | None
    job: str | None
    orders: list[OrphanOrder]
    liveness: RunLiveness


class AdoptedExecution(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    proposal_hash: str
    ticker: str
    kind: Literal["open", "close"]
    final_status: Literal["filled", "partially_filled", "cancelled", "unconfirmed"]
    filled_qty: int
    contracts: int
    fill_price: Decimal | None
    cancelled_broker_order_ids: list[str]
    structure_id: str | None
    detail: str
    alert: str


class ReattachReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    checked: int = 0
    dry_run: bool = False
    enabled: bool = True
    orphans: list[Orphan] = Field(default_factory=list)
    adopted: list[AdoptedExecution] = Field(default_factory=list)
    skipped_alive: list[str] = Field(default_factory=list)
    wedged: list[str] = Field(default_factory=list)
    terminated: list[int] = Field(default_factory=list)  # pids sent SIGTERM
    errors: list[str] = Field(default_factory=list)

    @property
    def unconfirmed(self) -> bool:
        return any(a.final_status == "unconfirmed" for a in self.adopted)

    def summary(self) -> str:
        parts = [f"{self.checked} working"]
        if self.adopted:
            parts.append(f"{len(self.adopted)} adopted")
        if self.orphans and not self.adopted:
            parts.append(f"{len(self.orphans)} orphan(s)")
        if self.skipped_alive:
            parts.append(f"{len(self.skipped_alive)} alive")
        if self.wedged:
            parts.append(f"{len(self.wedged)} wedged")
        if self.errors:
            parts.append(f"{len(self.errors)} error(s)")
        if self.dry_run:
            parts.append("dry run")
        elif not self.enabled:
            parts.append("execution_reattach off: listed only")
        return ", ".join(parts)


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


def _candidates(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT e.proposal_hash, e.kind, e.structure_id, e.contracts, e.max_steps,
                  e.started_at, e.run_id, e.adopted_by_run_id,
                  r.status AS run_status, r.job, r.event_id, r.pid, r.heartbeat_at,
                  r.started_at AS run_started_at,
                  p.ticker, p.structure_json, p.candidate_id
           FROM executions e
           LEFT JOIN routine_runs r ON r.run_id = e.run_id
           LEFT JOIN proposals p ON p.proposal_hash = e.proposal_hash
           WHERE e.status = 'working'
           ORDER BY e.started_at""",
    ).fetchall()
    return [dict(r) for r in rows]


def _open_orders(conn: sqlite3.Connection, phash: str) -> list[OrphanOrder]:
    out: list[OrphanOrder] = []
    rows = conn.execute(
        "SELECT * FROM orders WHERE proposal_hash = ? ORDER BY created_at, rowid", (phash,)
    ).fetchall()
    for r in rows:
        state = OrderState(r["state"])
        if state not in _OPEN_STATES:
            continue
        coid = str(r["client_order_id"])
        m = _STEP.search(coid)
        out.append(
            OrphanOrder(
                order_id=r["id"],
                client_order_id=coid,
                broker_order_id=r["broker_order_id"],
                state=state,
                step=int(m.group(1)) if m else 0,
                limit_price=_limit_of(conn, r["id"]),
            )
        )
    return out


def _limit_of(conn: sqlite3.Connection, order_id: str) -> Decimal:
    """The attempt's limit, from the ladder's ``approved; step k limit X`` event."""
    row = conn.execute(
        """SELECT detail FROM order_events WHERE order_id = ? AND to_state = 'approved'
           ORDER BY id LIMIT 1""",
        (order_id,),
    ).fetchone()
    m = _LIMIT.search(str(row[0])) if row is not None else None
    return Decimal(m.group(1)) if m else Decimal(0)


def _ladder_bound_s(row: dict[str, Any], settings: ArcSettings) -> float:
    """Longest a healthy ladder can work one execution (every step + its cancel)."""
    per_step = settings.execution_step_seconds + settings.execution_cancel_confirm_seconds
    return (int(row["max_steps"] or 0) + 1) * per_step + settings.execution_reattach_stale_s


def _lock_names(row: dict[str, Any]) -> list[str]:
    names = [owner_lock(str(row["run_id"]))]
    if row.get("event_id") and row.get("job"):
        names.append(f"{row['job']}:{row['event_id']}")  # run_event's own lock (D34)
    return names


def _probe(locks: LockManager, names: list[str]) -> bool:
    """True when any of *names* is held by another process."""
    for name in names:
        try:
            with locks.hold(name):
                pass
        except LockBusyError:
            return True
    return False


def _liveness(
    row: dict[str, Any], *, lock_held: bool | None, now: _dt.datetime, settings: ArcSettings
) -> RunLiveness:
    beat = from_db(row["heartbeat_at"]) if row.get("heartbeat_at") else None
    since = beat or (from_db(row["run_started_at"]) if row.get("run_started_at") else None)
    age = (now - since).total_seconds() if since is not None else None
    fresh = age is not None and age <= settings.execution_reattach_stale_s
    running = row.get("run_status") == "running"
    if row.get("run_id") is None:
        # A manual `arc execute` (no run row): alive while a ladder could still be working.
        started = from_db(row["started_at"])
        alive = (now - started).total_seconds() <= _ladder_bound_s(row, settings)
    else:
        # lock_held None (dry run): unknowable, the heartbeat alone decides.
        alive = running and lock_held is not False and fresh
    return RunLiveness(
        run_id=row.get("run_id"),
        run_status=row.get("run_status"),
        pid=row.get("pid"),
        pid_alive=pid_alive(row.get("pid")),
        heartbeat_at=beat,
        heartbeat_age_s=round(age, 1) if age is not None else None,
        lock_held=lock_held,
        alive=alive,
    )


def _orphan(conn: sqlite3.Connection, row: dict[str, Any], liveness: RunLiveness) -> Orphan:
    return Orphan(
        proposal_hash=row["proposal_hash"],
        kind=row["kind"],
        ticker=_ticker(row),
        run_id=row.get("run_id"),
        event_id=row.get("event_id"),
        job=row.get("job"),
        orders=_open_orders(conn, row["proposal_hash"]),
        liveness=liveness,
    )


def _ticker(row: dict[str, Any]) -> str:
    if row.get("ticker"):
        return str(row["ticker"])
    try:
        s = Structure.model_validate_json(row["structure_json"])
        return parse_occ(s.legs[0].occ_symbol).root
    except Exception:  # noqa: BLE001 - a label only
        return "?"


def find_orphans(
    conn: sqlite3.Connection,
    locks: LockManager,
    *,
    now: _dt.datetime,
    settings: ArcSettings,
) -> list[Orphan]:
    """Every ``working`` execution whose owner is not alive (read-only; never adopts).

    With :class:`NullLocks` (dry run) the lock is unknowable, so an execution
    with a running run is listed when its heartbeat is stale, flagged
    ``lock_held=None``.
    """
    dry = isinstance(locks, NullLocks)
    out: list[Orphan] = []
    for row in _candidates(conn):
        held: bool | None = None
        if not dry and row.get("run_id") is not None and row.get("run_status") == "running":
            held = _probe(locks, _lock_names(row))
        live = _liveness(row, lock_held=held, now=now, settings=settings)
        if not live.alive:
            out.append(_orphan(conn, row, live))
    return out


# ---------------------------------------------------------------------------
# Adoption
# ---------------------------------------------------------------------------


def _fence(conn: sqlite3.Connection, phash: str, run_id: str, now: _dt.datetime) -> bool:
    """Take the execution over (the ladder's fence); False when another run holds it."""
    with conn:
        cur = conn.execute(
            """UPDATE executions SET adopted_by_run_id = ?, adopted_at = ?
               WHERE proposal_hash = ? AND status = 'working'
                 AND (adopted_by_run_id IS NULL OR adopted_by_run_id = ?
                      OR adopted_by_run_id NOT IN
                         (SELECT run_id FROM routine_runs WHERE status = 'running'))""",
            (run_id, to_db(now), phash, run_id),
        )
    return cur.rowcount == 1


def _status(broker: BrokerAdapter, bid: str) -> BrokerOrderStatus | None:
    try:
        return broker.order_status(bid)
    except Exception as exc:  # noqa: BLE001 - unreadable: the cancel path decides
        log.warning("reattach.status_error", broker_order_id=bid, error=str(exc))
        return None


def _resolve_order(c: LadderContext, o: OrphanOrder) -> tuple[str, AttemptRecord]:
    """Finish one non-terminal order; returns the ladder verdict and its record."""
    a = AttemptRecord(
        step=o.step,
        limit_price=o.limit_price,
        client_order_id=o.client_order_id,
        order_id=o.order_id,
        broker_order_id=o.broker_order_id,
        status=o.state.value,
    )
    if o.state in (OrderState.PROPOSED, OrderState.GATED):
        c.move(o.order_id, OrderState.CANCELLED, "ladder died before approval; never sent")
        a.status = "cancelled"
        return "next", a
    status: BrokerOrderStatus | None = None
    if o.broker_order_id is None:
        res = resolve_unknown_submit(
            c.broker,
            client_order_id=o.client_order_id,
            attempts=c.config.execution_unknown_submit_lookups,
            sleep=c.sleep,
            wait_seconds=c.config.execution_poll_seconds,
        )
        if res.outcome == "absent":
            a.status, a.detail = "cancelled", "never reached the broker"
            c.move(o.order_id, OrderState.CANCELLED, f"ladder died; {a.detail}")
            c.journal(
                Choice.FAILED,
                ReasonCode.ORDER_SUBMIT_FAILED,
                f"re-attach: step {o.step} {a.detail} ({o.client_order_id[-4:]})",
                step=o.step,
                client_order_id=o.client_order_id,
            )
            return "next", a
        if res.outcome == "unknown" or res.broker_order_id is None:
            a.status = "unconfirmed"
            a.detail = (
                f"order {o.client_order_id[-4:]} unknown at the broker after {res.lookups} "
                f"lookup(s); cancel by client id {'sent' if res.cancel_sent else 'not sent'}"
            )
            c.journal(
                Choice.FAILED,
                ReasonCode.ORDER_UNCONFIRMED,
                a.detail,
                step=o.step,
                client_order_id=o.client_order_id,
            )
            return "unconfirmed", a
        a.broker_order_id = res.broker_order_id
        a.adopted = True
        status = res.status
        c.fence()
        c.orders.set_broker_order_id(o.order_id, res.broker_order_id)
        c.move(o.order_id, OrderState.SUBMITTED, f"re-attach found broker {res.broker_order_id}")
    else:
        status = _status(c.broker, o.broker_order_id)
    return resume_attempt(c, a, status), a


def _fills_of(conn: sqlite3.Connection, phash: str) -> tuple[int, Decimal | None, str | None]:
    """Total filled qty, qty-weighted net price and the filling order of *phash*."""
    rows = conn.execute(
        """SELECT f.order_id, f.qty, f.price FROM fills f JOIN orders o ON o.id = f.order_id
           WHERE o.proposal_hash = ? ORDER BY f.rowid""",
        (phash,),
    ).fetchall()
    qty = sum(int(r[1]) for r in rows)
    if not qty:
        return 0, None, None
    net = sum((Decimal(str(r[2])) * int(r[1]) for r in rows), Decimal(0)) / qty
    return qty, net.quantize(Decimal("0.0001")), str(rows[-1][0])


def _already_applied(conn: sqlite3.Connection, row: dict[str, Any]) -> str | None:
    """The structure id when the fill already reached the position model (crash after it)."""
    phash = row["proposal_hash"]
    if row["kind"] == "open":
        hit = conn.execute(
            "SELECT id FROM open_structures WHERE open_proposal_hash = ?", (phash,)
        ).fetchone()
        return str(hit[0]) if hit else None
    hit = conn.execute(
        "SELECT 1 FROM decisions WHERE proposal_hash = ? AND reason_code = ?",
        (phash, ReasonCode.EXIT_CLOSED.value),
    ).fetchone()
    return str(row["structure_id"]) if hit else None


def _adopt(
    conn: sqlite3.Connection,
    broker: BrokerAdapter,
    row: dict[str, Any],
    orphan: Orphan,
    *,
    settings: ArcSettings,
    clock: Callable[[], _dt.datetime],
    sleep: Callable[[float], None],
    run_id: str,
) -> AdoptedExecution | None:
    phash = row["proposal_hash"]
    if not _fence(conn, phash, run_id, clock()):
        return None
    c = LadderContext(
        conn=conn,
        broker=broker,
        config=settings,
        clock=clock,
        sleep=sleep,
        run_id=run_id,
        phash=phash,
        ticker=orphan.ticker,
        actor=ACTOR,
    )
    verdicts: list[tuple[str, AttemptRecord]] = [_resolve_order(c, o) for o in orphan.orders]
    cancelled = [
        a.broker_order_id
        for v, a in verdicts
        if a.broker_order_id and v in ("next", "partial") and a.status != "filled"
    ]
    qty, price, fill_order = _fills_of(conn, phash)
    contracts = int(row["contracts"])
    final: Literal["filled", "partially_filled", "cancelled", "unconfirmed"]
    if any(v == "unconfirmed" for v, _ in verdicts):
        final = "unconfirmed"
    elif qty and qty >= contracts:
        final = "filled"
    elif qty:
        final = "partially_filled"
    else:
        final = "cancelled"
    structure_id = row.get("structure_id")
    if qty and price is not None and fill_order is not None:
        done = _already_applied(conn, row)
        if done is not None:
            structure_id = done
        else:
            structure = Structure.model_validate_json(row["structure_json"])
            structure_id = apply_fill(
                conn,
                kind=row["kind"],
                phash=phash,
                ticker=orphan.ticker,
                candidate_id=str(row["candidate_id"]),
                structure=structure,
                order_id=fill_order,
                filled_qty=qty,
                fill_price=price,
                structure_id=row.get("structure_id"),
                now=clock(),
                run_id=run_id,
            )
    step = max((o.step for o in orphan.orders), default=0)
    broker_word = ", ".join(sorted({a.status for _, a in verdicts})) or "no open order"
    live = orphan.liveness
    pid = f"pid {live.pid}" if live.pid is not None else "no pid"
    detail = (
        f"re-attached: ladder {pid} died at step {step} "
        f"(heartbeat age {live.heartbeat_age_s}s, run {live.run_id or 'none'}); {final}"
    )
    steps_used = next((a.step for v, a in verdicts if v in ("filled", "partial")), None)
    ExecutionRepo(conn).finish(
        phash,
        status=final,
        now=clock(),
        filled_qty=qty,
        fill_price=price,
        steps_used=steps_used,
        structure_id=structure_id,
        detail=detail,
    )
    if qty:
        c.journal(
            Choice.FILLED,
            ReasonCode.ORDER_FILLED if final == "filled" else ReasonCode.ORDER_PARTIAL,
            f"{detail}; filled {qty} @ {price:+}",
            filled_qty=qty,
            fill_price=str(price),
            adopted_from_run=live.run_id,
        )
    if final == "unconfirmed" and settings.execution_intraday_reconcile:
        from arc.reconcile.intraday import queue_intraday_reconcile

        queue_intraday_reconcile(conn, phash, reason=detail, now=clock())
    _finish_dead_run(conn, row, live, run_id=run_id, now=clock())
    alert = (
        f"Re-attached {orphan.ticker} {row['kind']}: ladder {pid} died at step {step}; "
        f"broker {broker_word}; {final} ({qty}/{contracts}); "
        f"cancelled [{', '.join(cancelled)}]"
    )
    log.warning("reattach.adopted", proposal_hash=phash, final=final, alert=alert)
    return AdoptedExecution(
        proposal_hash=phash,
        ticker=orphan.ticker,
        kind=row["kind"],
        final_status=final,
        filled_qty=qty,
        contracts=contracts,
        fill_price=price,
        cancelled_broker_order_ids=cancelled,
        structure_id=structure_id,
        detail=detail,
        alert=alert,
    )


def _finish_dead_run(
    conn: sqlite3.Connection,
    row: dict[str, Any],
    live: RunLiveness,
    *,
    run_id: str,
    now: _dt.datetime,
) -> None:
    dead = row.get("run_id")
    if dead is None:
        return
    error = (
        f"ladder process died (pid {live.pid}, heartbeat age {live.heartbeat_age_s} s); "
        f"adopted by reattach run {run_id}"
    )
    RoutineRunRepo(conn).fail_if_running(str(dead), error=error, now=now)
    if row.get("event_id"):
        RoutineEventRepo(conn).consume(str(row["event_id"]), [run_id], now=now)


def _default_kill(pid: int, sig: int) -> None:
    os.kill(pid, sig)


def reattach(
    conn: sqlite3.Connection,
    broker: BrokerAdapter | None,
    locks: LockManager,
    *,
    now: _dt.datetime,
    settings: ArcSettings,
    run_id: str,
    clock: Callable[[], _dt.datetime] | None = None,
    sleep: Callable[[float], None] | None = None,
    kill: Callable[[int, int], None] = _default_kill,
    dry_run: bool = False,
) -> ReattachReport:
    """Adopt every orphaned ``working`` execution (see the module docstring).

    ``dry_run`` (or :class:`NullLocks`) lists orphans and touches nothing; so
    does ``execution_reattach`` off. *broker* may be None only then.
    """
    dry = dry_run or isinstance(locks, NullLocks)
    enabled = bool(settings.execution_reattach)
    report = ReattachReport(dry_run=dry, enabled=enabled)
    clk = clock or (lambda: now)
    nap = sleep or (lambda _s: None)
    for row in _candidates(conn):
        report.checked += 1
        phash = row["proposal_hash"]
        if dry:
            live = _liveness(row, lock_held=None, now=now, settings=settings)
            if not live.alive:
                report.orphans.append(_orphan(conn, row, live))
            else:
                report.skipped_alive.append(phash)
            continue
        names = _lock_names(row) if row.get("run_id") is not None else []
        try:
            with locks.hold(*names):
                live = _liveness(row, lock_held=False, now=now, settings=settings)
                if live.alive:  # only a run-less (manual) ladder still inside its bound
                    report.skipped_alive.append(phash)
                    continue
                orphan = _orphan(conn, row, live)
                report.orphans.append(orphan)
                if not enabled:
                    continue
                if broker is None:
                    report.errors.append(f"{phash[:12]}: no broker to adopt with")
                    continue
                try:
                    adopted = _adopt(
                        conn, broker, row, orphan,
                        settings=settings, clock=clk, sleep=nap, run_id=run_id,
                    )  # fmt: skip
                except ExecutionAdoptedError as exc:
                    report.errors.append(f"{phash[:12]}: adopted by {exc.adopted_by}")
                    continue
                except Exception as exc:  # noqa: BLE001 - one bad orphan never stops the rest
                    log.exception("reattach.adopt_failed", proposal_hash=phash)
                    report.errors.append(f"{phash[:12]}: {type(exc).__name__}: {exc}")
                    continue
                if adopted is not None:
                    report.adopted.append(adopted)
        except LockBusyError:
            live = _liveness(row, lock_held=True, now=now, settings=settings)
            if live.alive:
                report.skipped_alive.append(phash)
                continue
            report.wedged.append(phash)
            age = live.heartbeat_age_s
            log.warning("reattach.wedged", proposal_hash=phash, pid=live.pid, heartbeat_age_s=age)
            if (
                enabled
                and age is not None
                and age > settings.execution_reattach_kill_after_s
                and live.pid is not None
                and live.pid != os.getpid()
                and live.pid_alive
            ):
                try:
                    kill(live.pid, signal.SIGTERM)
                    report.terminated.append(live.pid)
                    log.warning("reattach.sigterm", proposal_hash=phash, pid=live.pid)
                except OSError as exc:
                    report.errors.append(f"{phash[:12]}: SIGTERM {live.pid}: {exc}")
    return report
