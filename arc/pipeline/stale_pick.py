"""Stale-pick cooldown (E5.16, PLAN D116): stop Research re-ranking dead picks.

A ``ticker|stance`` (the D33 dedupe key family) is **stale** when Research ranked it
in ``after_loops`` consecutive **full loops** of the session and none of those loops
produced a **tradeable structure** for the ticker. A stale pick leaves the idea pool
for the next ``suppress_loops`` ``research`` runs (any status), then is eligible
again with its streak reset.

* A **full loop** is a ``research`` run that completed its evaluation (journaled
  ``market_read``). ``no_change`` skips, failed runs and guard / budget no-trade runs
  rank nothing: they neither extend nor break a streak.
* **Ranked** = a ``shortlisted`` decision of the run (``subject`` + ``payload.stance``).
* **Tradeable** = in the run's chain, a proposal for the ticker that sized >= 1
  contract (a ``sizing`` decision) **and** passed the gate (``gate:pass``). An open or
  a fill implies both.
* A full loop where the key was suppressed or not ranked breaks its streak.
* The session is the ET calendar day of *now*; streaks never cross sessions.

Everything is computed from the journal (``decisions`` + ``routine_runs``), so the
state is restart-safe and deterministic, and an experiment arm reads its own store.
"""

from __future__ import annotations

import datetime as _dt
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from arc.context.ttl import to_db
from arc.journal.reasons import ReasonCode
from arc.utils.calendar import ET

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Sequence

__all__ = [
    "LoopRecord",
    "StaleState",
    "StaleSuppression",
    "load_loops",
    "pick_key",
    "stale_state",
]


def pick_key(ticker: str, stance: str) -> str:
    """``TICKER|stance`` (the D33 Research-stage dedupe key family)."""
    return f"{ticker.strip().upper()}|{stance.strip().lower()}"


@dataclass(frozen=True)
class LoopRecord:
    """One ``research`` run as the cooldown sees it."""

    run_id: str
    at: _dt.datetime  # scheduled_for (ET)
    full: bool  # completed evaluation (journaled market_read)
    ranked: frozenset[str] = frozenset()  # pick keys Research ranked
    tradeable: frozenset[str] = frozenset()  # tickers with a sized + gate-passed proposal


@dataclass(frozen=True)
class StaleSuppression:
    """A suppression started after the ``trigger`` run (covers the next N runs)."""

    key: str
    streak: int
    trigger_run_id: str
    trigger_at: _dt.datetime
    trigger_index: int
    suppress_loops: int
    back_at: _dt.datetime | None  # estimated first eligible slot (None = no cadence)

    @property
    def ticker(self) -> str:
        return self.key.split("|", 1)[0]

    @property
    def stance(self) -> str:
        return self.key.split("|", 1)[1]

    @property
    def last_index(self) -> int:
        """Index of the last suppressed run."""
        return self.trigger_index + self.suppress_loops

    def covers(self, index: int) -> bool:
        return self.trigger_index < index <= self.last_index

    def reason(self) -> str:
        back = f" (back {self.back_at.astimezone(ET):%H:%M} ET)" if self.back_at else ""
        return f"ranked {self.streak} loops, no tradeable structure{back}"

    def line(self) -> str:
        """The prompt / shortlist ``suppressed`` line."""
        return f"{self.ticker} {self.stance} — stale: {self.reason()}"

    def card_line(self) -> str:
        """The shortlist payload / Slack ``Suppressed`` line (``stale: …``)."""
        return f"stale: {self.ticker} {self.stance} — {self.reason()}"

    def payload(self) -> dict[str, object]:
        return {
            "stance": self.stance,
            "streak": self.streak,
            "trigger_run_id": self.trigger_run_id,
            "trigger_at": self.trigger_at.isoformat(),
            "suppress_loops": self.suppress_loops,
            "back_at": self.back_at.isoformat() if self.back_at else None,
        }


@dataclass(frozen=True)
class StaleState:
    """The cooldown replayed over a session's runs."""

    active: dict[str, StaleSuppression]  # key -> suppression covering the next run
    history: list[StaleSuppression] = field(default_factory=list)  # every start, in order
    streaks: dict[str, int] = field(default_factory=dict)  # open streaks after the last run


def stale_state(
    loops: Sequence[LoopRecord],
    *,
    after_loops: int,
    suppress_loops: int,
    every: _dt.timedelta | None = None,
) -> StaleState:
    """Replay *loops* (oldest first) and return what is suppressed at the next run.

    The next run has index ``len(loops)``. ``after_loops <= 0`` = off (nothing).
    *every* is the loop cadence, used only for the ``back HH:MM`` estimate.
    """
    if after_loops <= 0:
        return StaleState(active={})
    streak: dict[str, int] = {}
    history: list[StaleSuppression] = []
    current: dict[str, StaleSuppression] = {}
    for i, loop in enumerate(loops):
        current = {k: s for k, s in current.items() if s.covers(i)}
        if not loop.full:
            continue
        for key in list(streak):
            if key not in loop.ranked or key in current:
                del streak[key]  # not ranked / suppressed: streak broken
        for key in sorted(loop.ranked):
            if key in current:
                continue
            if key.split("|", 1)[0] in loop.tradeable:
                streak.pop(key, None)
                continue
            streak[key] = streak.get(key, 0) + 1
            if streak[key] >= after_loops:
                sup = StaleSuppression(
                    key=key,
                    streak=streak.pop(key),
                    trigger_run_id=loop.run_id,
                    trigger_at=loop.at,
                    trigger_index=i,
                    suppress_loops=suppress_loops,
                    back_at=loop.at + every * (suppress_loops + 1) if every else None,
                )
                current[key] = sup
                history.append(sup)
    nxt = len(loops)
    active = {k: s for k, s in current.items() if s.covers(nxt)}
    return StaleState(active=active, history=history, streaks=dict(streak))


# ---------------------------------------------------------------------------
# Journal reads
# ---------------------------------------------------------------------------


def _session_bounds(now: _dt.datetime) -> tuple[str, str]:
    day = now.astimezone(ET).date()
    start = _dt.datetime.combine(day, _dt.time(0, 0), tzinfo=ET)
    return to_db(start), to_db(start + _dt.timedelta(days=1))


def _parse(text: str) -> _dt.datetime:
    d = _dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    return (d if d.tzinfo else d.replace(tzinfo=_dt.UTC)).astimezone(ET)


def load_loops(
    conn: sqlite3.Connection,
    *,
    job: str,
    now: _dt.datetime,
    exclude_run_id: str | None = None,
) -> list[LoopRecord]:
    """The session's ``job`` runs scheduled before *now*, oldest first (read-only)."""
    lo, _ = _session_bounds(now)
    rows = conn.execute(
        "SELECT run_id, chain_run_id, scheduled_for FROM routine_runs "
        "WHERE job = ? AND scheduled_for >= ? AND scheduled_for <= ? "
        "ORDER BY scheduled_for, COALESCE(started_at, ''), run_id",
        (job, lo, to_db(now)),
    ).fetchall()
    out: list[LoopRecord] = []
    for run_id, chain_run_id, scheduled_for in rows:
        if run_id == exclude_run_id:
            continue
        full = (
            conn.execute(
                "SELECT 1 FROM decisions WHERE run_id = ? AND reason_code = ? LIMIT 1",
                (run_id, ReasonCode.MARKET_READ.value),
            ).fetchone()
            is not None
        )
        ranked: set[str] = set()
        tradeable: set[str] = set()
        if full:
            for subject, payload in conn.execute(
                "SELECT subject, payload FROM decisions WHERE run_id = ? AND reason_code = ?",
                (run_id, ReasonCode.SHORTLISTED.value),
            ):
                stance = str(json.loads(payload or "{}").get("stance") or "")
                if stance:
                    ranked.add(pick_key(str(subject), stance))
            if chain_run_id:
                tradeable = _tradeable(conn, str(chain_run_id))
        out.append(
            LoopRecord(
                run_id=str(run_id),
                at=_parse(str(scheduled_for)),
                full=full,
                ranked=frozenset(ranked),
                tradeable=frozenset(tradeable),
            )
        )
    return out


def _tradeable(conn: sqlite3.Connection, chain_run_id: str) -> set[str]:
    """Tickers with a proposal in the chain sized >= 1 contract that passed the gate."""
    rows = conn.execute(
        "SELECT g.subject, s.payload FROM decisions g "
        "JOIN decisions s ON s.proposal_hash = g.proposal_hash AND s.stage = 'sizing' "
        "WHERE g.chain_run_id = ? AND g.reason_code = ? AND g.proposal_hash IS NOT NULL",
        (chain_run_id, ReasonCode.GATE_PASS.value),
    ).fetchall()
    out: set[str] = set()
    for subject, payload in rows:
        try:
            contracts = int(json.loads(payload or "{}").get("contracts") or 0)
        except (TypeError, ValueError):
            contracts = 0
        if contracts >= 1:
            out.add(str(subject).strip().upper())
    return out


def already_journaled(conn: sqlite3.Connection, sup: StaleSuppression) -> bool:
    """True when this suppression's start row exists (idempotent per trigger run)."""
    row = conn.execute(
        "SELECT 1 FROM decisions WHERE reason_code = ? AND subject = ? "
        "AND json_extract(payload, '$.trigger_run_id') = ? "
        "AND json_extract(payload, '$.stance') = ? LIMIT 1",
        (ReasonCode.STALE_PICK_SUPPRESSED.value, sup.ticker, sup.trigger_run_id, sup.stance),
    ).fetchone()
    return row is not None
