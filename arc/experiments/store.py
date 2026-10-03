"""Experiment registry store (PLAN D44, card E10.1): append-only, deterministic.

``experiments`` holds one row per spec revision; ``experiment_events`` is the
status log (the latest event is the status). Every lifecycle step also writes a
journal decision (stage ``experiment``) with a ``reason_code``, in the same
transaction.

Rules enforced here (and, for the spec lock, by a DB trigger too):

- **Pre-registration lock.** ``register`` stores the canonical-JSON SHA-256 of
  the spec; any later revision of that experiment id is refused
  (:class:`SpecLockedError`): a changed experiment needs a new id. ``verify``
  recomputes the hash of the stored spec.
- **One per area.** At most one ``registered``/``running`` experiment per
  ``area``; a registration into a busy area becomes ``queued`` and is promoted
  to ``registered`` when the area frees up (:meth:`ExperimentStore.stop`).
- **A/A first.** An ``ab`` experiment may not start until an ``aa`` experiment has
  stopped with a recorded sigma (E10.4). The owner can override; the override is
  journaled (``experiment:aa_override``) and recorded on the running event.

The caller injects ``now``; nothing here reads the wall clock.
"""

from __future__ import annotations

import json
import sqlite3
from typing import TYPE_CHECKING, Any

import structlog

from arc.context.ttl import from_db, to_db
from arc.experiments.models import (
    ACTIVE_STATUSES,
    TRANSITIONS,
    ExperimentEvent,
    ExperimentKind,
    ExperimentSpec,
    ExperimentState,
    ExperimentStatus,
    RunningDetail,
    StopDetail,
    StopReason,
    canonical_json,
    spec_hash,
)
from arc.journal.reasons import Choice, JournalPersona, ReasonCode, Stage
from arc.journal.store import JournalStore

if TYPE_CHECKING:
    import datetime as _dt
    from collections.abc import Callable

__all__ = [
    "AaRequiredError",
    "ExperimentError",
    "ExperimentStore",
    "SpecLockedError",
    "TransitionError",
    "Verification",
]

log = structlog.get_logger(__name__)


class ExperimentError(ValueError):
    """A refused experiment operation (unknown id, bad input)."""


class SpecLockedError(ExperimentError):
    """The spec is hash-locked by registration; a change needs a new experiment id."""


class TransitionError(ExperimentError):
    """The status change is not allowed from the current status."""


class AaRequiredError(ExperimentError):
    """An ab experiment may not start before an A/A has recorded sigma."""


class Verification(dict[str, Any]):
    """``{experiment_id, ok, registered_hash, stored_hash, recomputed_hash, revision}``."""


_STATUS_REASON: dict[ExperimentStatus, ReasonCode] = {
    ExperimentStatus.DRAFT: ReasonCode.EXPERIMENT_DRAFTED,
    ExperimentStatus.REGISTERED: ReasonCode.EXPERIMENT_REGISTERED,
    ExperimentStatus.QUEUED: ReasonCode.EXPERIMENT_QUEUED,
    ExperimentStatus.RUNNING: ReasonCode.EXPERIMENT_STARTED,
    ExperimentStatus.STOPPED: ReasonCode.EXPERIMENT_STOPPED,
    ExperimentStatus.PROMOTED: ReasonCode.EXPERIMENT_PROMOTED,
    ExperimentStatus.REJECTED: ReasonCode.EXPERIMENT_REJECTED,
}
_STATUS_CHOICE: dict[ExperimentStatus, Choice] = {
    ExperimentStatus.DRAFT: Choice.NOTED,
    ExperimentStatus.REGISTERED: Choice.SELECTED,
    ExperimentStatus.QUEUED: Choice.NOTED,
    ExperimentStatus.RUNNING: Choice.SELECTED,
    ExperimentStatus.STOPPED: Choice.NOTED,
    ExperimentStatus.PROMOTED: Choice.APPROVED,
    ExperimentStatus.REJECTED: Choice.REJECTED,
}


def _persona(actor: str) -> JournalPersona:
    return JournalPersona.SYSTEM if actor.startswith("arc") else JournalPersona.OWNER


class ExperimentStore:
    def __init__(
        self, conn: sqlite3.Connection, *, now: Callable[[], _dt.datetime] | None = None
    ) -> None:
        from arc.utils.calendar import now_et

        self.conn = conn
        self._now = now or now_et

    # -- reads -------------------------------------------------------------------

    def _spec_row(self, experiment_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            """SELECT * FROM experiments WHERE experiment_id = ?
               ORDER BY revision DESC LIMIT 1""",
            (experiment_id,),
        ).fetchone()

    def events(self, experiment_id: str) -> list[ExperimentEvent]:
        rows = self.conn.execute(
            "SELECT * FROM experiment_events WHERE experiment_id = ? ORDER BY id",
            (experiment_id,),
        ).fetchall()
        return [
            ExperimentEvent(
                id=r["id"],
                experiment_id=r["experiment_id"],
                status=ExperimentStatus(r["status"]),
                reason=StopReason(r["reason"]) if r["reason"] else None,
                spec_hash=r["spec_hash"],
                actor=r["actor"],
                detail=json.loads(r["detail"]),
                at=from_db(r["at"]),
            )
            for r in rows
        ]

    def get(self, experiment_id: str) -> ExperimentState | None:
        row = self._spec_row(experiment_id)
        if row is None:
            return None
        events = self.events(experiment_id)
        last = events[-1]
        registered = next(
            (
                e.spec_hash
                for e in events
                if e.status in (ExperimentStatus.REGISTERED, ExperimentStatus.QUEUED)
            ),
            None,
        )
        running = next(
            (RunningDetail.model_validate(e.detail) for e in events if e.status == "running"),
            None,
        )
        stop = next(
            (StopDetail.model_validate(e.detail) for e in events if e.status == "stopped"),
            None,
        )
        return ExperimentState(
            experiment_id=experiment_id,
            status=last.status,
            reason=next((e.reason for e in reversed(events) if e.reason), None),
            revision=row["revision"],
            spec=ExperimentSpec.model_validate_json(row["spec"]),
            spec_hash=row["spec_hash"],
            registered_hash=registered,
            running=running,
            stop=stop,
            events=events,
        )

    def require(self, experiment_id: str) -> ExperimentState:
        st = self.get(experiment_id)
        if st is None:
            msg = f"unknown experiment {experiment_id}"
            raise ExperimentError(msg)
        return st

    def all(self, *, status: ExperimentStatus | None = None) -> list[ExperimentState]:
        """Every experiment (latest revision + status), oldest first; optionally one status."""
        ids = [
            r[0]
            for r in self.conn.execute(
                "SELECT experiment_id FROM experiments GROUP BY experiment_id ORDER BY MIN(id)"
            )
        ]
        out = [s for s in (self.get(i) for i in ids) if s is not None]
        return [s for s in out if status is None or s.status is status]

    def active_in_area(self, area: str, *, exclude: str | None = None) -> list[ExperimentState]:
        """Registered/running experiments in *area* (they hold it)."""
        return [
            s
            for s in self.all()
            if s.spec.area == area and s.status in ACTIVE_STATUSES and s.experiment_id != exclude
        ]

    def aa_sigma(self) -> float | None:
        """Sigma recorded by the latest A/A that stopped with one (E10.4), else None."""
        best: tuple[int, float] | None = None
        for s in self.all():
            if s.kind is not ExperimentKind.AA or s.stop is None or s.stop.sigma is None:
                continue
            stopped_id = max(e.id for e in s.events if e.status == "stopped")
            if best is None or stopped_id > best[0]:
                best = (stopped_id, s.stop.sigma)
        return best[1] if best else None

    # -- writes ------------------------------------------------------------------

    def _event(
        self,
        st_id: str,
        status: ExperimentStatus,
        *,
        hash_: str,
        actor: str,
        reason: StopReason | None = None,
        detail: dict[str, Any] | None = None,
        reason_code: ReasonCode | None = None,
        text: str = "",
    ) -> None:
        at = self._now()
        self.conn.execute(
            """INSERT INTO experiment_events
               (experiment_id, status, reason, spec_hash, actor, detail, at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                st_id,
                status.value,
                reason.value if reason else None,
                hash_,
                actor,
                json.dumps(detail or {}, sort_keys=True, default=str),
                to_db(at),
            ),
        )
        JournalStore(self.conn).record(
            persona=_persona(actor),
            stage=Stage.EXPERIMENT,
            subject=st_id,
            choice=_STATUS_CHOICE[status],
            reason_code=reason_code or _STATUS_REASON[status],
            reason_text=text,
            at=at,
            payload={
                "experiment_id": st_id,
                "status": status.value,
                "reason": reason.value if reason else None,
                "spec_hash": hash_,
                "actor": actor,
                **({"detail": detail} if detail else {}),
            },
        )
        log.info(
            "experiments.event",
            experiment_id=st_id,
            status=status.value,
            reason=reason.value if reason else None,
            actor=actor,
        )

    def _transition(self, st: ExperimentState, to: ExperimentStatus) -> None:
        if to not in TRANSITIONS[st.status]:
            msg = f"{st.experiment_id}: cannot go from {st.status.value} to {to.value}"
            raise TransitionError(msg)

    def create(self, spec: ExperimentSpec, *, actor: str) -> ExperimentState:
        """Store *spec* as a draft (a new revision when the id is still a draft).

        Refused once the experiment left draft: the spec is hash-locked.
        """
        current = self.get(spec.id)
        if current is not None and current.status is not ExperimentStatus.DRAFT:
            msg = (
                f"{spec.id} is {current.status.value}: its spec is locked "
                f"(sha256 {current.registered_hash}); register the change under a new id"
            )
            raise SpecLockedError(msg)
        h = spec_hash(spec)
        if current is not None and current.spec_hash == h:
            return current  # identical re-create: no new revision
        revision = 1 if current is None else current.revision + 1
        with self.conn:
            try:
                self.conn.execute(
                    """INSERT INTO experiments
                       (experiment_id, revision, spec_version, area, kind, spec, spec_hash,
                        actor, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        spec.id,
                        revision,
                        spec.spec_version,
                        spec.area.value,
                        spec.kind.value,
                        canonical_json(spec),
                        h,
                        actor,
                        to_db(self._now()),
                    ),
                )
            except sqlite3.IntegrityError as exc:  # pragma: no cover - guarded above
                raise SpecLockedError(str(exc)) from exc
            self._event(
                spec.id,
                ExperimentStatus.DRAFT,
                hash_=h,
                actor=actor,
                detail={"revision": revision},
                text=f"{spec.title} (revision {revision})",
            )
        return self.require(spec.id)

    def register(self, experiment_id: str, *, actor: str) -> ExperimentState:
        """Lock the spec (sha256) and register it, or queue it when its area is busy."""
        st = self.require(experiment_id)
        if st.status is not ExperimentStatus.DRAFT:
            msg = f"{experiment_id} is {st.status.value}, not draft: already registered"
            raise TransitionError(msg)
        if not st.spec.complete:
            msg = f"{experiment_id}: spec has unset defaults; re-create it with arc experiment"
            raise ExperimentError(msg)
        busy = self.active_in_area(st.spec.area.value, exclude=experiment_id)
        to = ExperimentStatus.QUEUED if busy else ExperimentStatus.REGISTERED
        self._transition(st, to)
        text = f"spec sha256 {st.spec_hash}"
        if busy:
            text += f"; area {st.spec.area.value} held by " + ", ".join(
                f"{b.experiment_id} ({b.status.value})" for b in busy
            )
        with self.conn:
            self._event(
                experiment_id,
                to,
                hash_=st.spec_hash,
                actor=actor,
                detail={"queued_behind": [b.experiment_id for b in busy]} if busy else None,
                text=text,
            )
        return self.require(experiment_id)

    def start(
        self,
        experiment_id: str,
        detail: RunningDetail,
        *,
        actor: str,
        aa_override: bool = False,
    ) -> ExperimentState:
        """``registered`` -> ``running`` with the t0 record (the E10.2 runner calls this).

        An ab experiment needs a recorded A/A sigma unless *aa_override* (owner
        only, journaled).
        """
        st = self.require(experiment_id)
        self._transition(st, ExperimentStatus.RUNNING)
        self._check_lock(st)
        override = False
        if st.kind is ExperimentKind.AB and self.aa_sigma() is None:
            if not aa_override:
                msg = (
                    f"{experiment_id} is an ab experiment and no A/A has recorded sigma yet "
                    "(E10.4); run an aa experiment first or start with the owner override"
                )
                raise AaRequiredError(msg)
            if _persona(actor) is not JournalPersona.OWNER:
                msg = "only the owner may override the A/A requirement"
                raise AaRequiredError(msg)
            override = True
        d = detail.model_copy(update={"aa_override": override})
        with self.conn:
            if override:
                JournalStore(self.conn).record(
                    persona=JournalPersona.OWNER,
                    stage=Stage.EXPERIMENT,
                    subject=experiment_id,
                    choice=Choice.APPROVED,
                    reason_code=ReasonCode.EXPERIMENT_AA_OVERRIDE,
                    reason_text="ab started with no A/A sigma on record",
                    at=self._now(),
                    payload={"experiment_id": experiment_id, "actor": actor},
                )
            self._event(
                experiment_id,
                ExperimentStatus.RUNNING,
                hash_=st.spec_hash,
                actor=actor,
                detail=d.model_dump(mode="json"),
                text=f"t0 equity {d.t0_equity:,.2f}; legacy book {len(d.legacy_book)}",
            )
        return self.require(experiment_id)

    def stop(
        self,
        experiment_id: str,
        reason: StopReason,
        *,
        actor: str,
        detail: StopDetail | None = None,
    ) -> ExperimentState:
        """Stop (from queued/registered/running) and promote the next queued one in the area."""
        st = self.require(experiment_id)
        self._transition(st, ExperimentStatus.STOPPED)
        d = detail or StopDetail()
        with self.conn:
            self._event(
                experiment_id,
                ExperimentStatus.STOPPED,
                hash_=st.spec_hash,
                actor=actor,
                reason=reason,
                detail=d.model_dump(mode="json", exclude_none=True),
                text=f"stopped: {reason.value}" + (f" ({d.note})" if d.note else ""),
            )
            self._promote_queue(st.spec.area.value, actor="arc.experiments")
        return self.require(experiment_id)

    def decide(self, experiment_id: str, *, promote: bool, actor: str) -> ExperimentState:
        """``stopped`` -> ``promoted`` | ``rejected`` (owner)."""
        st = self.require(experiment_id)
        to = ExperimentStatus.PROMOTED if promote else ExperimentStatus.REJECTED
        self._transition(st, to)
        with self.conn:
            self._event(experiment_id, to, hash_=st.spec_hash, actor=actor)
        return self.require(experiment_id)

    def _promote_queue(self, area: str, *, actor: str) -> None:
        """Move the oldest queued experiment of *area* to registered if the area is free."""
        if self.active_in_area(area):
            return
        queued = [s for s in self.all(status=ExperimentStatus.QUEUED) if s.spec.area.value == area]
        if not queued:
            return
        nxt = min(queued, key=lambda s: s.events[-1].id)
        self._event(
            nxt.experiment_id,
            ExperimentStatus.REGISTERED,
            hash_=nxt.spec_hash,
            actor=actor,
            text=f"area {area} free; spec sha256 {nxt.spec_hash}",
        )

    # -- integrity ---------------------------------------------------------------

    def verify(self, experiment_id: str) -> Verification:
        """Recompute the stored spec's hash and compare it with the registered hash."""
        st = self.require(experiment_id)
        row = self._spec_row(experiment_id)
        assert row is not None  # noqa: S101 - require() found it
        recomputed = spec_hash(ExperimentSpec.model_validate_json(row["spec"]))
        stored = row["spec_hash"]
        ok = recomputed == stored and (
            st.registered_hash is None or st.registered_hash == recomputed
        )
        return Verification(
            experiment_id=experiment_id,
            status=st.status.value,
            ok=ok,
            revision=st.revision,
            registered_hash=st.registered_hash,
            stored_hash=stored,
            recomputed_hash=recomputed,
        )

    def _check_lock(self, st: ExperimentState) -> None:
        v = self.verify(st.experiment_id)
        if not v["ok"]:
            msg = f"{st.experiment_id}: spec hash mismatch ({v}); refusing to run it"
            raise SpecLockedError(msg)
