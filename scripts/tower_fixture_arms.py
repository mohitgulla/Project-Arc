"""E15.7 Tower fixture: a running K=3 experiment with three arm stores (``--arms``).

Loaded by ``scripts/tower_fixture_db.py --arms``. Adds to the control store:

- ``XP-12`` (A/A, treatments t1..t3) registered and running since three sessions ago;
- one arm store per treatment next to the control DB (``arc-exp-XP-12-t<n>.db``), each
  with its ``arm_identity`` row (shared account), two EOD ``pnl_snapshots`` and a fresh
  ``monitor`` heartbeat carrying the arm's virtual equity ($25k-ish, so an arm view is
  visibly not Arc Investor's $100k book);
- the ``routine_state experiment_arm:XP-12:t<n>`` rows that point the tower at them.

Test-only data; never point the builder at ``data/arc.db``.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import TYPE_CHECKING

from arc.context.ttl import to_db
from arc.experiments.arms import STATE_ARM, ArmIdentity, write_identity
from arc.experiments.config import ExperimentDefaults
from arc.experiments.models import ExperimentSpec, RunningDetail
from arc.experiments.overlay import fill_defaults
from arc.experiments.store import ExperimentStore
from arc.monitoring.store import HeartbeatRepo
from arc.routines.runs import RoutineStateRepo
from arc.store.db import connect
from arc.store.migrate import migrate
from arc.store.repos import PnlSnapshotRepo
from arc.utils.calendar import previous_session

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path

EXPERIMENT_ID = "XP-12"
TREATMENTS = ("t1", "t2", "t3")
#: Per-arm virtual equity at the latest EOD and the live mark on top of it.
ARM_EQUITY = {"t1": 25_300.0, "t2": 24_810.0, "t3": 25_055.0}
ARM_DAY_PNL = {"t1": 21.5, "t2": -42.0, "t3": 8.25}


def _spec() -> ExperimentSpec:
    spec = ExperimentSpec.model_validate(
        {
            "id": EXPERIMENT_ID,
            "title": "Three-arm A/A (Tower fixture)",
            "hypothesis": "identical arms on one shared account stay within noise",
            "area": "other",
            "kind": "aa",
            "proposed_by": "owner",
            "arms": {"treatments": {t: {} for t in TREATMENTS}},
        }
    )
    return fill_defaults(spec, ExperimentDefaults())


def _arm_store(path: Path, control: Path, arm: str, now: dt.datetime) -> None:
    conn = connect(path)
    migrate(conn)
    write_identity(
        conn,
        ArmIdentity(
            arm_id=f"{EXPERIMENT_ID}:{arm}",
            experiment_id=EXPERIMENT_ID,
            arm=arm,
            spec_arm=arm,
            keys_env="ALPACA_EXP",
            control_db=str(control),
            created_at=now - dt.timedelta(days=3),
            plan={"fork_step": "quant.propose", "arm_personas": [], "arm_jobs": []},
            account_mode="shared",
            arm_code=arm[1:],
        ),
    )
    eq = ARM_EQUITY[arm]
    d1 = previous_session(now.date())
    for day, val in ((previous_session(d1), eq - 120.0), (d1, eq)):
        PnlSnapshotRepo(conn).insert(
            realized="0",
            unrealized="0",
            total="0",
            details_json=json.dumps(
                {
                    "day": day.isoformat(),
                    "equity": str(val),
                    "virtual_equity": str(val),
                    "day_pnl": "0",
                    "clean": True,
                }
            ),
            snapshot_at=to_db(dt.datetime.combine(day, dt.time(16, 30), tzinfo=now.tzinfo)),
        )
    mark = eq + ARM_DAY_PNL[arm]
    HeartbeatRepo(conn).record(
        "monitor",
        "ok",
        at=now - dt.timedelta(minutes=2),
        detail={
            "valued": True,
            "positions": 0,
            "equity": mark,
            "cash": mark,
            "last_equity": eq,
            "prev_close": eq,
            "prev_close_source": "arc_close",
            "day_pnl": ARM_DAY_PNL[arm],
            "legs": [],
        },
    )
    HeartbeatRepo(conn).record(
        "tick", "ok", at=now - dt.timedelta(minutes=1), detail={"counts": {"ok": 1}}
    )
    conn.commit()
    conn.close()


def add_arms(conn: sqlite3.Connection, control_path: Path, now: dt.datetime) -> list[Path]:
    """Register + start XP-12 in *conn* and build its three arm stores beside the DB."""
    clock = [now - dt.timedelta(days=3, minutes=30)]

    def tick() -> dt.datetime:
        clock[0] += dt.timedelta(seconds=1)
        return clock[0]

    store = ExperimentStore(conn, now=tick)
    store.create(_spec(), actor="local", owner_approval="P-1")
    store.register(EXPERIMENT_ID, actor="local", owner_approval="P-1")
    store.start(
        EXPERIMENT_ID,
        RunningDetail(
            t0=now - dt.timedelta(days=3), t0_equity=25_000.0, control_sha="2187d41" + "a" * 33
        ),
        actor="local",
        aa_override=True,
    )
    state = RoutineStateRepo(conn)
    paths: list[Path] = []
    for arm in TREATMENTS:
        p = control_path.with_name(f"arc-exp-{EXPERIMENT_ID}-{arm}.db")
        _arm_store(p, control_path, arm, now)
        state.set(STATE_ARM.format(experiment_id=EXPERIMENT_ID, arm=arm), str(p), now=now)
        paths.append(p)
    conn.commit()
    return paths
