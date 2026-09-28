"""``arc propose``: run Scout → Director → Quant → Risk → propose (+ gate) once, now.

The steps are the D16 routine chain: the Scout job, then the ``director`` job
with its ``chain:`` from ``config/routines.yaml``. They go through the same
:class:`~arc.routines.dispatcher.Dispatcher` the cron tick uses, so every run
is recorded in ``routine_runs`` with its input snapshot ids and output context
entries. A failed chain is resumed from its failed step by the next
``arc propose`` on the same day.

Modes:

* default: live market data, the paper account (read-only), Hermes personas,
  heartbeats into the day's #arc-investor thread, and writes to ``data/arc.db``.
* ``--dry-run``: never touches Slack or the broker (fixture account). Without
  ``--db`` it works on an **in-memory copy** of ``data/arc.db``, so it reads
  today's candidates but persists nothing.
* ``--fixtures``: fully offline. It uses the recorded SPY chain, fixture raw
  docs and canned Scout/Director/Quant/Risk replies, in an in-memory DB unless
  ``--db`` is given. It implies no Slack and no broker.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

from arc.pipeline.env import FIXTURE_NOW, PipelineEnv
from arc.pipeline.steps import pipeline_handlers
from arc.pipeline.store import proposals_for_day
from arc.utils.calendar import ET

if TYPE_CHECKING:
    import datetime as _dt

    from arc.config import ArcSettings
    from arc.routines.config import RoutinesConfig
    from arc.routines.dispatcher import Outcome
    from arc.routines.heartbeat import Notifier

log = structlog.get_logger(__name__)

__all__ = ["ProposeReport", "open_db", "run_propose"]

ROOT_JOB = "director"
SCOUT_JOB = "scout"


@dataclass
class ProposeReport:
    now: _dt.datetime
    day: str
    mode: str
    outcomes: list[Outcome] = field(default_factory=list)
    proposals: list[dict[str, Any]] = field(default_factory=list)

    @property
    def failed(self) -> bool:
        return any(o.status == "failed" for o in self.outcomes)

    def as_json(self) -> dict[str, Any]:
        return {
            "now": self.now.isoformat(),
            "day": self.day,
            "mode": self.mode,
            "steps": [
                {
                    "job": o.job,
                    "status": o.status,
                    "run_id": o.run_id,
                    "chain_run_id": o.chain_run_id,
                    "summary": o.summary,
                }
                for o in self.outcomes
            ],
            "proposals": [
                {
                    "ticker": p["ticker"],
                    "proposal_hash": p["proposal_hash"],
                    "run_id": p["run_id"],
                    "sizing": p["sizing_json"],
                    "gate_passed": bool(p["gate_passed"]),
                    "gate_violations": p["gate_violations"],
                    "gate_token": bool(p["gate_token"]),
                }
                for p in self.proposals
            ],
        }

    def lines(self) -> list[str]:
        out = [f"arc propose {self.day} ({self.mode}) at {self.now:%H:%M %Z}"]
        for o in self.outcomes:
            rid = f" {o.run_id}" if o.run_id else ""
            out.append(f"  {o.job:<9} {o.status:<9}{rid}  {o.summary or o.reason}")
        out.append(f"proposals for {self.day}: {len(self.proposals)}")
        for p in self.proposals:
            verdict = "PASS" if p["gate_passed"] else "FAIL"
            token = " token" if p["gate_token"] else ""
            out.append(f"  {p['ticker']:<6} gate {verdict}{token}  {p['proposal_hash'][:12]}")
        return out


def open_db(db: str | Path | None, *, copy: bool) -> sqlite3.Connection:
    """Open (and migrate) the audit DB. ``copy=True`` returns an in-memory copy of it."""
    from arc.store.db import DEFAULT_DB_PATH, connect
    from arc.store.migrate import migrate

    if not copy:
        conn = connect(db)
    else:
        conn = connect(":memory:")
        src_path = Path(db) if db else DEFAULT_DB_PATH
        if src_path.is_file():
            src = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True)
            try:
                src.backup(conn)
            finally:
                src.close()
    migrate(conn)
    return conn


def run_propose(
    conn: sqlite3.Connection,
    settings: ArcSettings,
    routines: RoutinesConfig,
    env: PipelineEnv,
    *,
    now: _dt.datetime,
    notifier: Notifier,
    scout: bool = True,
    locks: Any = None,
    mode: str = "live",
) -> ProposeReport:
    """Run the Scout job, then the Director chain, through the routine dispatcher."""
    from arc.routines.dispatcher import Dispatcher

    now = now.astimezone(ET)
    disp = Dispatcher(
        conn,
        routines,
        handlers=pipeline_handlers(env),
        locks=locks,
        notifier=notifier,
        settings_factory=lambda: settings,
    )
    report = ProposeReport(now=now, day=now.date().isoformat(), mode=mode)
    if routines.job(ROOT_JOB) is None:
        msg = f"routines config has no {ROOT_JOB!r} persona"
        raise KeyError(msg)
    if scout and routines.job(SCOUT_JOB) is not None:
        # Triggers may start the Director chain (scout.completed during RTH).
        report.outcomes += disp.run_job(SCOUT_JOB, now, reason="manual:propose", now=now)
    if not any(o.job == ROOT_JOB for o in report.outcomes):
        report.outcomes += disp.run_manual(ROOT_JOB, now=now, chain=True)
    report.proposals = proposals_for_day(conn, report.day)
    log.info(
        "pipeline.propose.done",
        mode=mode,
        day=report.day,
        steps=[(o.job, o.status, o.run_id) for o in report.outcomes],
        proposals=len(report.proposals),
    )
    return report


def fixture_run(
    settings: ArcSettings,
    routines: RoutinesConfig,
    *,
    db: str | Path | None = None,
    now: _dt.datetime | None = None,
    fixture_set: str = "neutral",
) -> tuple[sqlite3.Connection, ProposeReport]:
    """``arc propose --fixtures``: offline end to end (seeded raw docs, canned personas).

    ``fixture_set`` picks the canned Director/Quant/Risk replies
    (:data:`~arc.pipeline.env.FIXTURE_SETS`): ``neutral`` (SPY iron condor) or
    ``bullish`` (SPY bull call debit, D25).
    """
    from arc.ingest.scout import load_fixture_docs
    from arc.pipeline.env import FIXTURE_SETS
    from arc.routines.heartbeat import LogNotifier

    conn = open_db(db or ":memory:", copy=False)
    load_fixture_docs(conn)
    env = PipelineEnv.fixtures(FIXTURE_SETS[fixture_set])
    report = run_propose(
        conn,
        settings,
        routines,
        env,
        now=now or FIXTURE_NOW,
        notifier=LogNotifier(),
        mode="fixtures",
    )
    return conn, report
