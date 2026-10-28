"""The backtest study queue (card E26.2, PLAN D121): ``data/backtest/queue.json``.

The owner's ``approve BT-<n>`` in #arc-analyst runs ``arc backtest study adopt``, which
commits the Analyst's draft spec (auto-merged PR) and appends a ``queued`` entry here. The
nightly study runner (E26.3) runs each entry's ladder, one study at a time:
``c125_dev`` -> ``c500_dev`` -> ``holdout`` (on the last rung, C500).

A JSON file next to the research ledger (gitignored like it), not a DB table: no
migration. Every write takes an exclusive ``flock`` on ``queue.json.lock`` and replaces
the file atomically (temp file in the same directory + ``fsync`` + ``os.replace``), so a
reader never sees half a file and two writers never lose each other's entry.

The ETA is an estimate, in nights: the median measured ``runtime_s`` of recent ledger rows
for each remaining rung x 1.2 (the runner's fit-or-defer factor, E26.3), queued behind
every entry ahead of it, over the 21:30-08:30 ET window (D121).
"""

from __future__ import annotations

import contextlib
import datetime as dt
import fcntl
import json
import math
import os
import re
import statistics
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence

    from arc.backtest.study import LedgerEntry

__all__ = [
    "ACTIVE",
    "APPROVAL_RE",
    "DEFAULT_QUEUE",
    "NIGHT_WINDOW_S",
    "RUNG_RUNS",
    "RUNGS",
    "QueueEntry",
    "QueueError",
    "QueueRun",
    "StudyQueue",
    "enqueue",
    "eta_rows",
    "load_queue",
    "measured_runtime_s",
    "night_of",
    "rung_estimates",
    "save_queue",
    "stop_entry",
    "update_queue",
]

DEFAULT_QUEUE = Path("data/backtest/queue.json")
QUEUE_VERSION = 1
#: D121: the nightly window is 21:30 -> 08:30 ET (11 h). E26.3's runner owns the
#: config; the ETA here only needs the length.
NIGHT_WINDOW_S = 11 * 3600
#: E26.3's fit-or-defer margin on a measured runtime.
RUNTIME_MARGIN = 1.2
#: Recent ledger rows per rung the median is taken over.
RECENT_ROWS = 5

Status = Literal["queued", "running", "concluded", "stopped", "failed"]
#: E27.7 (D133): the queue carries two job kinds; ``bt`` is the default, so a queue
#: written before ML studies existed loads unchanged.
Kind = Literal["bt", "ml"]
Rung = Literal["c125_dev", "c500_dev", "holdout", "dev_2022", "dev_2023", "verdict"]
RUNGS: tuple[Rung, ...] = ("c125_dev", "c500_dev", "holdout")
#: E27.7: an ``ML-<n>`` study's ladder: the harness's two dev test folds, then the
#: verdict (best variant on inner folds, final fit, register; never the holdout).
ML_RUNGS: tuple[Rung, ...] = ("dev_2022", "dev_2023", "verdict")
KIND_RUNGS: dict[str, tuple[Rung, ...]] = {"bt": RUNGS, "ml": ML_RUNGS}
#: D121 + D133: owner approvals per ISO week (ET), BT and ML counted together.
WEEKLY_APPROVAL_CAP = 2
#: rung -> (ledger phase, universe) the run uses (the holdout runs on C500, D121).
RUNG_RUNS: dict[str, tuple[str, str]] = {
    "c125_dev": ("dev", "c125"),
    "c500_dev": ("dev", "c500"),
    "holdout": ("holdout", "c500"),
}
ACTIVE: tuple[str, ...] = ("queued", "running")
TERMINAL: tuple[str, ...] = ("concluded", "stopped", "failed")
APPROVAL_RE = re.compile(r"^slack:\d{9,11}\.\d{1,6}$")
_ID_RE = re.compile(r"^BT-[1-9]\d*$")
_ML_ID_RE = re.compile(r"^ML-[1-9]\d*$")
_KIND_ID_RE: dict[str, re.Pattern[str]] = {"bt": _ID_RE, "ml": _ML_ID_RE}
_FORBID = ConfigDict(extra="forbid")


class QueueError(RuntimeError):
    """A queue write was refused (the file is unchanged)."""


class QueueRun(BaseModel):
    """One runner attempt at a rung (written by the E26.3 runner)."""

    model_config = _FORBID

    rung: Rung
    at: dt.datetime
    run_dir: str | None = None
    #: the ledger row it wrote (``BT-<n>:<spec hash 12>:<at>``); None = no ledger line
    ledger_key: str | None = None
    outcome: str | None = None  # done | interrupted | failed | deferred ... (E26.3)


class QueueEntry(BaseModel):
    """One approved study (``BT-<n>``, or ``ML-<n>`` with ``kind: ml``, E27.7)."""

    model_config = _FORBID

    id: str
    kind: Kind = "bt"
    spec_hash: str = Field(..., min_length=64, max_length=64)
    status: Status = "queued"
    approved_at: dt.datetime
    approval: str
    #: the next rung to run; None once the ladder is done (or stopped early). Omitted,
    #: it is the kind's first rung.
    ladder_state: Rung | None = Field(default_factory=lambda d: KIND_RUNGS[d.get("kind", "bt")][0])
    runs: list[QueueRun] = Field(default_factory=list)
    stop_reason: str | None = None
    pr_url: str | None = None

    @model_validator(mode="after")
    def _kind_fits(self) -> QueueEntry:
        if not _KIND_ID_RE[self.kind].match(self.id):
            prefix = "BT" if self.kind == "bt" else "ML"
            msg = f"study id {self.id!r}: use {prefix}-<n> (kind {self.kind})"
            raise ValueError(msg)
        rungs = KIND_RUNGS[self.kind]
        if self.ladder_state is not None and self.ladder_state not in rungs:
            msg = f"{self.id}: rung {self.ladder_state} is not on the {self.kind} ladder {rungs}"
            raise ValueError(msg)
        return self

    @field_validator("approval")
    @classmethod
    def _approval(cls, v: str) -> str:
        if not APPROVAL_RE.match(v):
            msg = f"approval {v!r}: use slack:<ts> (the owner's approving message, D86)"
            raise ValueError(msg)
        return v

    def nights(self) -> int:
        """Nights the runner has worked on it so far (distinct :func:`night_of` dates)."""
        return len({night_of(r.at) for r in self.runs})


class StudyQueue(BaseModel):
    model_config = _FORBID

    version: int = QUEUE_VERSION
    entries: list[QueueEntry] = Field(default_factory=list)

    def get(self, study_id: str) -> QueueEntry | None:
        return next((e for e in self.entries if e.id == study_id), None)

    def active(self) -> list[QueueEntry]:
        """Queued/running entries in run order (oldest approval first, E26.3)."""
        return sorted(
            (e for e in self.entries if e.status in ACTIVE),
            key=lambda e: (e.approved_at, _num(e.id)),
        )

    def position(self, study_id: str) -> int | None:
        ids = [e.id for e in self.active()]
        return ids.index(study_id) + 1 if study_id in ids else None


def _num(study_id: str) -> int:
    return int(study_id.split("-", 1)[1])


def night_of(at: dt.datetime) -> dt.date:
    """The night a run belongs to: its ET date, shifted back 12 h so a run that starts at
    21:30 and one that resumes at 02:00 the same night count once."""
    from arc.utils.calendar import ET

    return (at.astimezone(ET) - dt.timedelta(hours=12)).date()


# ---------------------------------------------------------------------------
# IO: atomic + locked
# ---------------------------------------------------------------------------


def load_queue(path: Path) -> StudyQueue:
    """The queue (an empty one when the file does not exist yet)."""
    if not path.exists():
        return StudyQueue()
    return StudyQueue.model_validate_json(path.read_text())


def save_queue(path: Path, queue: StudyQueue) -> None:
    """Write *queue* atomically: temp file in the same dir, fsync, ``os.replace``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(queue.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


@contextlib.contextmanager
def _locked(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with (path.parent / f"{path.name}.lock").open("a") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def update_queue(path: Path, fn: Callable[[StudyQueue], Any]) -> Any:
    """Load, apply *fn* (it mutates the queue and returns a result), save: one locked,
    atomic read-modify-write. An exception from *fn* leaves the file untouched."""
    with _locked(path):
        queue = load_queue(path)
        out = fn(queue)
        save_queue(path, queue)
        return out


# ---------------------------------------------------------------------------
# Mutations
# ---------------------------------------------------------------------------


def enqueue(queue: StudyQueue, entry: QueueEntry) -> tuple[QueueEntry, bool]:
    """Append *entry* (``created`` True), or return the existing one for the same id and
    spec hash (idempotent re-approval: its PR URL is filled in when it had none).
    Refuses a different spec hash under the same id."""
    have = queue.get(entry.id)
    if have is None:
        queue.entries.append(entry)
        return entry, True
    if have.spec_hash != entry.spec_hash:
        msg = (
            f"{entry.id} is already queued with spec {have.spec_hash[:12]} "
            f"({have.status}); this spec is {entry.spec_hash[:12]}: a changed design needs "
            f"a new {entry.id.split('-', 1)[0]} id"
        )
        raise QueueError(msg)
    if entry.pr_url and not have.pr_url:
        have.pr_url = entry.pr_url
    return have, False


def iso_week(when: dt.datetime) -> tuple[int, int]:
    """ISO (year, week) of *when* in America/New_York: the D121/D133 cap's week."""
    from arc.utils.calendar import ET

    y, w, _ = when.astimezone(ET).isocalendar()
    return y, w


def week_approvals(queue: StudyQueue, now: dt.datetime, exclude: str = "") -> list[QueueEntry]:
    """Queue entries (BT and ML, any status: D121 counts approvals, not runs) whose
    approval falls in *now*'s ISO week (ET), oldest first."""
    week = iso_week(now)
    got = [e for e in queue.entries if e.id != exclude and iso_week(e.approved_at) == week]
    return sorted(got, key=lambda e: e.approved_at)


def check_weekly_cap(
    queue: StudyQueue, study_id: str, now: dt.datetime, cap: int = WEEKLY_APPROVAL_CAP
) -> None:
    """D121 + D133: refuse a NEW approval once the week holds *cap* (BT and ML together).
    A retry of an id already queued is not a new approval and always passes."""
    if queue.get(study_id) is not None:
        return
    week = week_approvals(queue, now, exclude=study_id)
    if len(week) >= cap:
        from arc.utils.calendar import ET

        y, w = iso_week(now)
        got = ", ".join(f"{e.id} ({e.approved_at.astimezone(ET):%Y-%m-%d})" for e in week)
        d = now.astimezone(ET).date()
        reset = d + dt.timedelta(days=7 - d.weekday())
        msg = (
            f"{study_id} refused: the weekly cap is {cap} study approvals per ISO week, BT "
            f"and ML together (D121, D133); week {y}-W{w:02d} already has {got}. It resets "
            f"Mon {reset} 00:00 ET"
        )
        raise QueueError(msg)


def stop_entry(queue: StudyQueue, study_id: str, reason: str) -> QueueEntry:
    """Owner ``stop BT-<n> <why>``: a queued/running entry becomes ``stopped``; the runner
    obeys at its next check and never picks it up again (E26.3)."""
    if not reason.strip():
        msg = "stop needs a reason"
        raise QueueError(msg)
    e = queue.get(study_id)
    if e is None:
        msg = f"{study_id} is not in the study queue (a draft is dropped, not stopped)"
        raise QueueError(msg)
    if e.status not in ACTIVE:
        msg = f"{study_id} is already {e.status}" + (f" ({e.stop_reason})" if e.stop_reason else "")
        raise QueueError(msg)
    e.status = "stopped"
    e.stop_reason = f"owner: {reason.strip()}"
    return e


# ---------------------------------------------------------------------------
# ETA
# ---------------------------------------------------------------------------


def measured_runtime_s(e: LedgerEntry) -> float | None:
    """Wall seconds of a ledger run: its ``runtime_s`` (E26.2 on), else, for rows written
    before it, the run's ``report.json`` mtime minus the row's start stamp ``at``."""
    if e.runtime_s is not None:
        return e.runtime_s
    report = Path(e.run_dir) / "report.json"
    try:
        sec = report.stat().st_mtime - e.at.timestamp()
    except OSError:
        return None
    return sec if sec > 0 else None


def rung_estimates(entries: Sequence[LedgerEntry]) -> dict[str, dict[str, Any]]:
    """Per rung: the median measured runtime of the last :data:`RECENT_ROWS` ledger rows
    on that phase + universe, x :data:`RUNTIME_MARGIN`; ``seconds`` None without a row."""
    out: dict[str, dict[str, Any]] = {}
    for rung, (phase, uni) in RUNG_RUNS.items():
        rows = sorted(
            (e for e in entries if e.phase == phase and e.universe == uni),
            key=lambda e: e.at,
        )
        secs = [s for e in rows if (s := measured_runtime_s(e)) is not None][-RECENT_ROWS:]
        med = statistics.median(secs) if secs else None
        out[rung] = {
            "seconds": round(med * RUNTIME_MARGIN) if med is not None else None,
            "rows": len(secs),
        }
    return out


def _remaining(e: QueueEntry) -> list[str]:
    if e.ladder_state is None:
        return []
    rungs = KIND_RUNGS[e.kind]
    return list(rungs[rungs.index(e.ladder_state) :])


def eta_rows(
    queue: StudyQueue,
    entries: Sequence[LedgerEntry],
    ml_estimates: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """One row per queue entry: kind, status, next rung, nights so far, position and ETA
    (nights until its ladder is done, behind every active entry ahead of it, BT and ML
    alike: one runner, one queue). *ml_estimates* maps an ``ML-<n>`` id to its rung
    estimates (``{rung: {"seconds": ...}}``, :func:`arc.ml.study.ml_rung_estimates`);
    without them an ML entry's ETA is unknown."""
    bt = rung_estimates(entries)
    ml = ml_estimates or {}
    cum: float | None = 0.0
    rows: list[dict[str, Any]] = []
    active = queue.active()
    for pos, e in enumerate(active, 1):
        est = bt if e.kind == "bt" else ml.get(e.id, {})
        need = [(est.get(r) or {}).get("seconds") for r in _remaining(e)]
        own = None if any(s is None for s in need) else float(sum(need))  # type: ignore[arg-type]
        cum = None if cum is None or own is None else cum + own
        rows.append(
            _row(e, pos, own, None if cum is None else max(1, math.ceil(cum / NIGHT_WINDOW_S)))
        )
    rest = sorted(
        (e for e in queue.entries if e.status not in ACTIVE),
        key=lambda e: (e.approved_at, _num(e.id)),
    )
    rows += [_row(e, None, None, None) for e in rest]
    return rows


def _row(e: QueueEntry, pos: int | None, own: float | None, eta: int | None) -> dict[str, Any]:
    return {
        "id": e.id,
        "kind": e.kind,
        "status": e.status,
        "ladder_state": e.ladder_state,
        "remaining_rungs": _remaining(e) if e.status in ACTIVE else [],
        "nights": e.nights(),
        "queue_position": pos,
        "compute_s": round(own) if own is not None else None,
        "eta_nights": eta,
        "approved_at": e.approved_at.isoformat(),
        "approval": e.approval,
        "spec_hash": e.spec_hash,
        "pr_url": e.pr_url,
        "stop_reason": e.stop_reason,
    }
