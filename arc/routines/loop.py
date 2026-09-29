"""State of the 5-min trading loop (D31 / D36): digests, thread ids, timeouts.

Everything lives in ``routine_state`` (no migration): the loop's last input
digest and last full-run time (the change-aware skip), the ``ts`` of each
loop's #arc-investor root (``loop_thread:<chain_run_id>``), the per-chain
step-duration summary the ``[Routines]`` reply renders, and the once-a-day
timeout notice. Pure bookkeeping; nothing here calls an LLM or the broker.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict

from arc.routines.manifest import digest as _digest
from arc.routines.runs import RoutineStateRepo
from arc.utils.calendar import ET

if TYPE_CHECKING:
    import datetime as _dt
    import sqlite3

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


def slot_stamp(slot: _dt.datetime) -> str:
    """``YYYY-MM-DD HH:MMET`` (owner's format, no space before ET), DST-correct."""
    return f"{slot.astimezone(ET):%Y-%m-%d %H:%M}ET"


__all__ = ["LoopInputs", "LoopState", "pnl_bucket", "slot_stamp"]
