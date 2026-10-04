"""Fixture DB for E10.3 experiment evaluation tests (and the PR's fixture report).

Builds an in-store experiment with per-arm EOD ``pnl_snapshots`` (control
``arm_id`` NULL, treatment ``X-<n>:treatment``), control ``positions_snapshots``
marking a legacy structure, executions, outcomes and paired-chain manifests:
the rows the reconcile and the E10.2 arm runner write.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from typing import TYPE_CHECKING

from arc.context.ttl import to_db
from arc.experiments.config import ExperimentDefaults
from arc.experiments.models import ExperimentSpec, RunningDetail, arm_id
from arc.experiments.overlay import fill_defaults
from arc.experiments.store import ExperimentStore
from arc.utils.calendar import ET, next_session, session_close

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Sequence

T0 = dt.datetime(2026, 10, 5, 9, 0, tzinfo=ET)  # Monday, before the open
T0_EQUITY = 100_000.0
CONTROL_SHA = "2187d41aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
TREATMENT_SHA = "2187d41bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
# the experiment paper account's broker equity is never reset to control's (E10.2):
# treatment rows carry broker ``equity`` = virtual equity + this unusable excess
BROKER_EXCESS = -5_000.0  # e.g. a $95k paper account against a $100k control


def spec(eid: str = "X-2", *, kind: str = "ab", **kw: object) -> ExperimentSpec:
    data: dict[str, object] = {
        "id": eid,
        "title": f"{eid} fixture",
        "hypothesis": "take profit at 40% beats 50%",
        "area": "exits" if kind == "ab" else "other",
        "kind": kind,
        "proposed_by": "owner",
    }
    if kind == "ab":
        data |= {
            "arms": {"treatment": {"overlay": {"exits": {"default": {"take_profit_pct": 0.4}}}}},
            "backtest_ref": "docs/RESEARCH/backtests/fixture",
            "non_inferiority_margin": 0.5,
        }
    data |= kw
    return fill_defaults(ExperimentSpec.model_validate(data), ExperimentDefaults())


def sessions(n: int, start: dt.date | None = None) -> list[dt.date]:
    out = [start or T0.date()]
    while len(out) < n:
        out.append(next_session(out[-1]))
    return out


def start(
    conn: sqlite3.Connection,
    sp: ExperimentSpec,
    *,
    legacy: Sequence[str] = (),
    t0: dt.datetime = T0,
    t0_equity: float = T0_EQUITY,
) -> ExperimentStore:
    clock = [t0 - dt.timedelta(minutes=30)]

    def now() -> dt.datetime:
        clock[0] += dt.timedelta(seconds=1)
        return clock[0]

    store = ExperimentStore(conn, now=now)
    store.create(sp, actor="local")
    store.register(sp.id, actor="local")
    store.start(
        sp.id,
        RunningDetail(
            t0=t0, t0_equity=t0_equity, legacy_book=list(legacy), control_sha=CONTROL_SHA
        ),
        actor="local",
        aa_override=True,
    )
    return store


def eod(day: dt.date) -> dt.datetime:
    return session_close(day) + dt.timedelta(minutes=30)


def pnl_row(
    conn: sqlite3.Connection,
    day: dt.date,
    equity: float,
    aid: str | None,
    *,
    virtual: bool = True,
) -> None:
    """One EOD snapshot. Arm rows: *equity* is the virtual equity (``virtual_equity``)
    and the broker ``equity`` is offset by :data:`BROKER_EXCESS`, as on the real
    experiment account; ``virtual=False`` writes the broker equity only."""
    details: dict[str, str] = {"day": day.isoformat(), "equity": str(equity)}
    if aid is not None:
        details["equity"] = str(equity + BROKER_EXCESS)
        if virtual:
            details["virtual_equity"] = str(equity)
    conn.execute(
        """INSERT INTO pnl_snapshots
           (id, snapshot_at, realized, unrealized, total, details_json, arm_id)
           VALUES (?, ?, '0', '0', '0', ?, ?)""",
        (uuid.uuid4().hex, to_db(eod(day)), json.dumps(details), aid),
    )


def equity_curves(
    conn: sqlite3.Connection,
    eid: str,
    ctrl_pnl: Sequence[float],
    treat_pnl: Sequence[float],
    *,
    t0_equity: float = T0_EQUITY,
    prior_close: float | None = T0_EQUITY,
) -> list[dt.date]:
    """EOD equity rows for both arms from daily P&L (control also gets the pre-t0 close)."""
    days = sessions(len(ctrl_pnl))
    t_arm = arm_id(eid, "treatment")
    if prior_close is not None:
        from arc.utils.calendar import previous_session

        pnl_row(conn, previous_session(days[0]), prior_close, None)
    c, t = prior_close if prior_close is not None else t0_equity, t0_equity
    for day, cp, tp in zip(days, ctrl_pnl, treat_pnl, strict=True):
        c += cp
        t += tp
        pnl_row(conn, day, c, None)
        pnl_row(conn, day, t, t_arm)
    conn.commit()
    return days


def legacy_snapshot(
    conn: sqlite3.Connection, day: dt.date, structure_id: str, value: float | None
) -> None:
    """Control's EOD positions snapshot: the legacy structure held at *value* (None = gone)."""
    structures = []
    broker = []
    if value is not None:
        sym = "SPY261120C00600000"
        structures.append(
            {"structure_id": structure_id, "ticker": "SPY", "legs": {sym: 1}, "held": True}
        )
        broker.append({"symbol": sym, "qty": "1", "market_value": str(value)})
    conn.execute(
        """INSERT INTO positions_snapshots (id, snapshot_at, positions_json, arm_id)
           VALUES (?, ?, ?, NULL)""",
        (
            uuid.uuid4().hex,
            to_db(eod(day)),
            json.dumps({"day": day.isoformat(), "structures": structures, "broker": broker}),
        ),
    )


def executions(
    conn: sqlite3.Connection, aid: str | None, n: int, *, attempts: int = 1, day: dt.date
) -> None:
    for _ in range(n):
        h = uuid.uuid4().hex
        conn.execute(
            """INSERT INTO candidates (id, ticker, stance, catalyst_type, confidence, created_at)
               VALUES (?, 'SPY', 'bullish', 'momentum', 0.5, ?)""",
            (h, to_db(eod(day))),
        )
        conn.execute(
            """INSERT INTO proposals (id, candidate_id, proposal_hash, structure_json, thesis,
                   quant_json, sizing_json, expires_at, created_at, arm_id)
               VALUES (?, ?, ?, '{"kind": "vertical_debit"}', 't', '{}', '{}', ?, ?, ?)""",
            (h, h, h, to_db(eod(day)), to_db(eod(day)), aid),
        )
        conn.execute(
            """INSERT INTO executions (proposal_hash, kind, status, token_version, band_lo,
                   band_hi, max_steps, attempts, contracts, filled_qty, fill_price,
                   started_at, finished_at, arm_id)
               VALUES (?, 'open', 'filled', 'arc2', '1', '1.2', 4, ?, 1, 1, '1.1', ?, ?, ?)""",
            (h, attempts, to_db(eod(day) - dt.timedelta(hours=5)), to_db(eod(day)), aid),
        )
    conn.commit()
