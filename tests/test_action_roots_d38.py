"""D38: the position manager's closes get the loop's one-line root (``SELL: IWM``).

Before D38 only Research's trading loop opened a root in #arc-investor, so a
close proposed and filled by ``positions.evaluate`` never showed as SELL. Now an
action chain opens the same root once it has a proposal, and posts nothing when
it is quiet.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from typing import TYPE_CHECKING, Any

from arc.pipeline import FIXTURE_NOW
from arc.pipeline.runner import open_db
from arc.routines.config import load_routines
from arc.routines.dispatcher import Dispatcher
from arc.routines.handlers import JobResult
from arc.routines.heartbeat import RecordingNotifier
from arc.routines.loop import LoopState, refresh_loop_root
from arc.slack.loop import slot_stamp
from arc.utils.calendar import ET
from tests.test_e59_research_portfolio import _settings

if TYPE_CHECKING:
    import sqlite3

    from arc.routines.handlers import JobContext

SLOT = FIXTURE_NOW.astimezone(ET)
STEPS = ("positions.evaluate", "exits.mandatory", "broker.execute")  # E13.15 chain


def _conn() -> sqlite3.Connection:
    conn = open_db(":memory:", copy=False)
    with conn:
        conn.execute(
            """INSERT INTO context_entries (id, kind, subject, payload, schema_version,
               produced_by, run_id, chain_run_id, created_at, valid_from, expires_at, status)
               VALUES ('pc1', 'portfolio_context', 'session', ?, 1, 'research', 'r0', 'chain-loop',
                       '2026-09-28T13:00:00Z', '2026-09-28T13:00:00Z', '2099-01-01T00:00:00Z',
                       'active')""",
            (json.dumps({"account": {"equity": 100250.0, "day_pnl": 250.0}}),),
        )
    return conn


def _insert_close(conn: sqlite3.Connection, ctx: JobContext, ticker: str) -> str:
    cid = f"cand-{uuid.uuid4().hex[:8]}"
    phash = uuid.uuid4().hex
    now = ctx.now.isoformat()
    with conn:
        conn.execute(
            "INSERT INTO candidates (id, ticker, stance, catalyst_type, confidence, created_at)"
            " VALUES (?, ?, 'bullish', 't', 0.5, ?)",
            (cid, ticker, now),
        )
        conn.execute(
            """INSERT INTO proposals (id, candidate_id, proposal_hash, structure_json, thesis,
               quant_json, sizing_json, expires_at, created_at, run_id, ticker, kind)
               VALUES (?, ?, ?, '{}', '', '{}', '{}', ?, ?, ?, ?, 'close')""",
            (f"p-{phash[:8]}", cid, phash, now, now, ctx.run_id, ticker),
        )
    return phash


def _disp(
    conn: sqlite3.Connection,
    closes: list[str],
    *,
    fill: bool,
    overrides: dict[Any, Any] | None = None,
) -> tuple[Dispatcher, RecordingNotifier, list[str]]:
    hashes: list[str] = []

    def evaluate(ctx: JobContext) -> JobResult:
        return JobResult(summary=f"{len(closes)} position(s) reviewed")

    def exits(ctx: JobContext) -> JobResult:
        hashes.extend(_insert_close(conn, ctx, t) for t in closes)
        return JobResult(summary=f"{len(closes)} exit(s) proposed")

    def execute(ctx: JobContext) -> JobResult:
        if fill:
            with conn:
                for h in hashes:
                    conn.execute(
                        """INSERT INTO executions (proposal_hash, kind, status, token_version,
                           band_lo, band_hi, max_steps, contracts, filled_qty, started_at)
                           VALUES (?, 'close', 'filled', 'arc2', '0', '1', 1, 1, 1, ?)""",
                        (h, ctx.now.isoformat()),
                    )
        return JobResult(summary="handed off")

    notes = RecordingNotifier()
    settings = _settings()
    disp = Dispatcher(
        conn,
        load_routines(overrides=overrides or {}),
        handlers={
            "positions.evaluate": evaluate,
            "exits.mandatory": exits,
            "broker.execute": execute,
        },
        notifier=notes,
        settings_factory=lambda: settings,
    )
    return disp, notes, hashes


def _run(disp: Dispatcher) -> list[Any]:
    return disp.run_job("positions.evaluate", SLOT, reason="schedule", now=SLOT, chain=True)


def test_filled_closes_post_a_sell_root() -> None:
    conn = _conn()
    disp, notes, _ = _disp(conn, ["IWM", "SPY"], fill=True)
    outs = _run(disp)
    assert [o.job for o in outs] == list(STEPS)
    assert len(notes.roots) == 1
    ((ts, text),) = notes.roots.items()
    assert text == (
        f":white_check_mark: {slot_stamp(SLOT)} • Portfolio: $100,250 • P&L: +$250"
        " • Orders: n/a • SELL: IWM, SPY"
    )
    chain = outs[0].chain_run_id
    assert chain is not None and LoopState(conn).thread_ts(chain) == ts
    # the exit/execute posts after the proposal thread under the root, not the day thread
    assert notes.in_thread(ts)
    assert notes.thread_ts is None


def test_quiet_run_posts_no_root() -> None:
    conn = _conn()
    disp, notes, _ = _disp(conn, [], fill=False)
    _run(disp)
    assert notes.roots == {}


def test_working_close_becomes_sell_after_the_fill() -> None:
    conn = _conn()
    disp, notes, hashes = _disp(conn, ["NFLX"], fill=False)
    outs = _run(disp)
    ((ts, text),) = notes.roots.items()
    assert text.startswith(":heavy_multiplication_x: ") and text.endswith("• HOLD")
    # the Investor fills the close later (its own process) and re-renders the root
    with conn:
        conn.execute(
            """INSERT INTO executions (proposal_hash, kind, status, token_version, band_lo,
               band_hi, max_steps, contracts, filled_qty, started_at)
               VALUES (?, 'close', 'filled', 'arc2', '0', '1', 1, 5, 5, ?)""",
            (hashes[0], SLOT.isoformat()),
        )

    class Editor:
        edits: list[tuple[str, str]] = []

        def update_root(self, ts: str, text: str) -> None:
            self.edits.append((ts, text))

    ed = Editor()
    new = refresh_loop_root(conn, ed, outs[0].chain_run_id)
    assert new is not None and new.endswith("• SELL: NFLX")
    assert new.startswith(":white_check_mark: ") and "Portfolio: $100,250" in new
    assert ed.edits == [(ts, new)]


def test_action_roots_is_config_driven() -> None:
    conn = _conn()
    disp, notes, _ = _disp(conn, ["IWM"], fill=True, overrides={("loop", "action_roots"): []})
    _run(disp)
    assert notes.roots == {}


def test_day_thread_layout_posts_no_action_root() -> None:
    conn = _conn()
    disp, notes, _ = _disp(
        conn, ["IWM"], fill=True, overrides={("loop", "slack_layout"): "day_thread"}
    )
    _run(disp)
    assert notes.roots == {}


def test_manual_run_posts_no_action_root() -> None:
    conn = _conn()
    disp, notes, _ = _disp(conn, ["IWM"], fill=True)
    disp.run_job(
        "positions.evaluate", SLOT, reason="manual", now=SLOT + dt.timedelta(minutes=1), chain=True
    )
    assert notes.roots == {}
