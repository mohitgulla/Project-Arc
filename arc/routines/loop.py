"""State of the 5-min trading loop (D31 / D36): digests, thread ids, timeouts.

Everything lives in ``routine_state`` (no migration): the loop's last input
digest and last full-run time (the change-aware skip), the ``ts`` of each
loop's #arc-investor root (``loop_thread:<chain_run_id>``), the per-chain
step-duration summary the ``[Routines]`` reply renders, and the once-a-day
timeout notice. Pure bookkeeping; nothing here calls an LLM or the broker.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Protocol

import structlog
from pydantic import BaseModel, ConfigDict

from arc.routines.manifest import digest as _digest
from arc.routines.runs import RoutineStateRepo
from arc.slack.loop import LoopRoot, slot_stamp

if TYPE_CHECKING:
    import datetime as _dt
    import sqlite3

log = structlog.get_logger(__name__)

_LAST_DIGEST = "loop:last_digest"
_LAST_FULL = "loop:last_full_run"
_THREAD = "loop_thread:{chain}"
_ROOT = "loop_root:{chain}"
_SUMMARY = "loop_summary:{chain}"
_TIMEOUT_DAY = "loop:timeout_alerted"
_PROPOSAL_CHAIN = "loop_proposal:{proposal_hash}"


class LoopInputs(BaseModel):
    """What the Director's input digest is made of (D31 change-aware skip).

    Every field is deterministic and already rounded: candidate ids with their
    context versions, the regime entries, the portfolio view with P&L in
    ``pnl_bucket_pct``-of-equity buckets, pending orders, the order-budget tier
    and the dedupe-suppressed set. Two loops with the same digest would ask the
    Director the same question.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    candidates: list[str]  # "<entry id>@<version>" sorted
    regimes: list[str]  # "<subject>@<version>" sorted
    positions: list[str]  # "<structure id>:<qty>" sorted
    pnl_bucket: int  # day P&L in buckets of pnl_bucket_pct of equity (signed)
    pending_orders: int
    budget_tier: str
    suppressed: list[str]  # dedupe-suppressed idea keys, sorted

    def digest(self) -> str:
        return _digest(self.model_dump(mode="json"))


def pnl_bucket(day_pnl: float | None, equity: float | None, bucket_pct: float) -> int:
    """Signed bucket index of *day_pnl* in steps of ``bucket_pct``% of *equity*.

    Unknown P&L or a non-positive equity gives bucket 0, so a loop without
    account data does not thrash the digest.
    """
    if day_pnl is None or not equity or equity <= 0 or bucket_pct <= 0:
        return 0
    step = equity * bucket_pct / 100.0
    return int(day_pnl // step) if step > 0 else 0


class LoopState:
    """``routine_state`` accessors for the loop (one instance per dispatcher)."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._state = RoutineStateRepo(conn)

    # -- change-aware skip ----------------------------------------------------

    def last_digest(self) -> str | None:
        return self._state.get(_LAST_DIGEST)

    def last_full_run(self) -> _dt.datetime | None:
        return self._state.get_time(_LAST_FULL)

    def should_skip(self, new_digest: str, now: _dt.datetime, max_idle: _dt.timedelta) -> bool:
        """True when the inputs match the last loop and a full run is not yet due."""
        if self.last_digest() != new_digest:
            return False
        last = self.last_full_run()
        return last is not None and now - last < max_idle

    def record_digest(self, new_digest: str, now: _dt.datetime, *, full_run: bool) -> None:
        self._state.set(_LAST_DIGEST, new_digest, now=now)
        if full_run:
            self._state.set_time(_LAST_FULL, now)

    # -- Slack root per loop (D36) -------------------------------------------

    def thread_ts(self, chain_run_id: str) -> str | None:
        return self._state.get(_THREAD.format(chain=chain_run_id))

    def set_thread_ts(self, chain_run_id: str, ts: str, *, now: _dt.datetime | None = None) -> None:
        self._state.set(_THREAD.format(chain=chain_run_id), ts, now=now)

    def root(self, chain_run_id: str) -> dict[str, Any] | None:
        """The stored fields of a loop's root line (rendered again on updates)."""
        raw = self._state.get(_ROOT.format(chain=chain_run_id))
        return json.loads(raw) if raw else None

    def set_root(self, chain_run_id: str, fields: dict[str, Any]) -> None:
        self._state.set(_ROOT.format(chain=chain_run_id), json.dumps(fields, default=str))

    def chain_for_proposal(self, proposal_hash: str) -> str | None:
        return self._state.get(_PROPOSAL_CHAIN.format(proposal_hash=proposal_hash))

    def link_proposal(self, proposal_hash: str, chain_run_id: str) -> None:
        self._state.set(_PROPOSAL_CHAIN.format(proposal_hash=proposal_hash), chain_run_id)

    # -- per-chain summary ([Routines] reply) --------------------------------

    def chain_summary(self, chain_run_id: str) -> dict[str, Any] | None:
        raw = self._state.get(_SUMMARY.format(chain=chain_run_id))
        return json.loads(raw) if raw else None

    def set_chain_summary(self, chain_run_id: str, summary: dict[str, Any]) -> None:
        self._state.set(_SUMMARY.format(chain=chain_run_id), json.dumps(summary, default=str))

    # -- once-a-day timeout notice --------------------------------------------

    def first_timeout_today(self, day: _dt.date) -> bool:
        """True (and remembers it) the first time a loop times out on *day*."""
        if self._state.get(_TIMEOUT_DAY) == day.isoformat():
            return False
        self._state.set(_TIMEOUT_DAY, day.isoformat())
        return True


# ---------------------------------------------------------------------------
# D36: the root line per loop, computed from the audit DB
# ---------------------------------------------------------------------------


def loop_root_from_db(
    conn: sqlite3.Connection,
    chain_run_id: str,
    slot: _dt.datetime,
    *,
    no_change: bool = False,
    timeout: bool = False,
    skipped: str | None = None,
    fallback: LoopRoot | None = None,
) -> LoopRoot:
    """Build the :class:`LoopRoot` for *chain_run_id* from what the chain wrote.

    *fallback* fills equity / day P&L / the order budget when the chain recorded
    none (D38: the position manager writes no ``portfolio_context``).

    Deterministic and re-runnable: an approval, a fill or an expiry later just
    recomputes it. Equity / day P&L come from the chain's ``portfolio_context``
    entry, the order budget from the root run's manifest, the action lists from
    the chain's proposals joined to their approval request and execution.
    """

    equity = day_pnl = None
    row = conn.execute(
        """SELECT payload FROM context_entries
           WHERE kind = 'portfolio_context' AND chain_run_id = ?
           ORDER BY rowid DESC LIMIT 1""",
        (chain_run_id,),
    ).fetchone()
    if row is not None:
        acct = json.loads(row["payload"]).get("account") or {}
        equity, day_pnl = acct.get("equity"), acct.get("day_pnl")
    used = limit = None
    row = conn.execute(
        """SELECT m.payload FROM run_manifests m
           JOIN routine_runs r ON r.run_id = m.run_id
           WHERE r.chain_run_id = ? AND r.step_index = 0
           ORDER BY m.rowid DESC LIMIT 1""",
        (chain_run_id,),
    ).fetchone()
    if row is not None:
        budget = json.loads(row["payload"]).get("order_budget") or {}
        used, limit = budget.get("used"), budget.get("limit")
    buys: list[str] = []
    sells: list[str] = []
    pending: list[str] = []
    working: list[str] = []
    rows = conn.execute(
        """SELECT p.ticker, p.kind, a.status AS approval, e.status AS execution
           FROM proposals p
           JOIN routine_runs r ON r.run_id = p.run_id
           LEFT JOIN approval_requests a ON a.proposal_hash = p.proposal_hash
           LEFT JOIN executions e ON e.proposal_hash = p.proposal_hash
           WHERE r.chain_run_id = ? ORDER BY p.rowid""",
        (chain_run_id,),
    ).fetchall()
    for r in rows:
        ticker, kind = str(r["ticker"]), str(r["kind"])
        execution, approval = r["execution"], r["approval"]
        if execution in ("filled", "partially_filled"):
            (sells if kind == "close" else buys).append(ticker)
        elif approval == "pending":
            pending.append(ticker)
        elif approval == "approved" and execution in (None, "working", "unconfirmed"):
            working.append(ticker)
    if fallback is not None:
        if equity is None:
            equity, day_pnl = fallback.equity, fallback.day_pnl
        if used is None:
            used, limit = fallback.orders_used, fallback.orders_limit
    return LoopRoot(
        slot=slot,
        equity=equity,
        day_pnl=day_pnl,
        orders_used=used,
        orders_limit=limit,
        buys=_dedupe(buys),
        sells=_dedupe(sells),
        pending=_dedupe(pending),
        working=_dedupe(working),
        no_change=no_change,
        timeout=timeout,
        skipped=skipped,
    )


def latest_account_facts(conn: sqlite3.Connection, slot: _dt.datetime) -> LoopRoot:
    """Equity, day P&L and order budget as last recorded (any chain / run).

    D38: the position manager's root shows the same facts as the trading loop's,
    taken from the newest ``portfolio_context`` entry and run manifest.
    """
    equity = day_pnl = None
    row = conn.execute(
        """SELECT payload FROM context_entries WHERE kind = 'portfolio_context'
           ORDER BY rowid DESC LIMIT 1"""
    ).fetchone()
    if row is not None:
        acct = json.loads(row["payload"]).get("account") or {}
        equity, day_pnl = acct.get("equity"), acct.get("day_pnl")
    used = limit = None
    row = conn.execute(
        """SELECT json_extract(payload, '$.order_budget') AS ob FROM run_manifests
           WHERE json_extract(payload, '$.order_budget') IS NOT NULL
           ORDER BY rowid DESC LIMIT 1"""
    ).fetchone()
    if row is not None and row["ob"]:
        budget = json.loads(row["ob"])
        used, limit = budget.get("used"), budget.get("limit")
    return LoopRoot(slot=slot, equity=equity, day_pnl=day_pnl, orders_used=used, orders_limit=limit)


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    return [x for x in items if not (x in seen or seen.add(x))]


class RootEditor(Protocol):
    """Whoever can edit an #arc-investor root line (a Notifier, a card poster)."""

    def update_root(self, ts: str, text: str) -> None: ...


def refresh_loop_root(
    conn: sqlite3.Connection, editor: RootEditor, chain_run_id: str | None
) -> str | None:
    """Recompute and edit the root line of *chain_run_id* (approval, fill, expiry).

    No-op when the chain has no root (day-thread layout, dry runs, a proposal
    from a manual ``arc propose``). Returns the new text.
    """
    if not chain_run_id:
        return None
    state = LoopState(conn)
    ts = state.thread_ts(chain_run_id)
    stored = state.root(chain_run_id)
    if ts is None or stored is None:
        return None

    prev = LoopRoot.model_validate(stored)
    root = loop_root_from_db(
        conn,
        chain_run_id,
        prev.slot,
        no_change=prev.no_change,
        timeout=prev.timeout,
        skipped=prev.skipped,
        fallback=prev,  # D38: an action chain keeps the facts its root opened with
    )
    text = root.text()
    if text == prev.text():
        return text
    state.set_root(chain_run_id, root.model_dump(mode="json"))
    try:
        editor.update_root(ts, text)
    except Exception as exc:  # noqa: BLE001 - the decision/fill is committed; the edit is best-effort
        log.warning("routines.loop_root_update_failed", chain_run_id=chain_run_id, error=str(exc))
    else:
        log.info("routines.loop_root_updated", chain_run_id=chain_run_id, text=text)
    return text


__all__ = [
    "LoopInputs",
    "LoopRoot",
    "LoopState",
    "RootEditor",
    "latest_account_facts",
    "loop_root_from_db",
    "pnl_bucket",
    "refresh_loop_root",
    "slot_stamp",
]
