"""D65: the bold-italic loop headline under the #arc-investor root line.

Built only from what the chain journaled (``decisions`` / ``proposals`` /
``executions``), so these tests seed those rows in the shapes the live loop
writes (Oct 8 chains: a Net-EV-floor HOLD, a HOOD open fill, an MRVL exit fill,
a GS close blocked by wide quotes) and assert the two lines.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from typing import TYPE_CHECKING, Any

from arc.pipeline.runner import open_db
from arc.routines.loop import loop_headline, loop_root_from_db
from arc.utils.calendar import ET

if TYPE_CHECKING:
    import sqlite3

CHAIN = "chain-d65"
SLOT = dt.datetime(2026, 10, 8, 15, 40, tzinfo=ET)


def _conn() -> sqlite3.Connection:
    conn = open_db(":memory:", copy=False)
    conn.execute("PRAGMA foreign_keys = OFF")  # only the rows the headline reads
    return conn


def _run(conn: sqlite3.Connection, job: str, step: int, status: str = "ok") -> str:
    run_id = f"run-{job}-{uuid.uuid4().hex[:6]}"
    conn.execute(
        """INSERT INTO routine_runs (run_id, job, chain_run_id, step_index, reason,
               scheduled_for, status) VALUES (?, ?, ?, ?, 'schedule', ?, ?)""",
        (run_id, job, CHAIN, step, SLOT.isoformat(), status),
    )
    return run_id


def _dec(
    conn: sqlite3.Connection,
    persona: str,
    stage: str,
    subject: str,
    choice: str,
    code: str,
    payload: dict[str, Any] | None = None,
    *,
    chain: str | None = CHAIN,
    run_id: str | None = None,
) -> None:
    conn.execute(
        """INSERT INTO decisions (id, chain_run_id, run_id, persona, stage, subject, choice,
               reason_code, payload, at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            f"dec-{uuid.uuid4().hex[:8]}",
            chain,
            run_id,
            persona,
            stage,
            subject,
            choice,
            code,
            json.dumps(payload or {}),
            SLOT.isoformat(),
        ),
    )


def _proposal(
    conn: sqlite3.Connection,
    run_id: str,
    ticker: str,
    kind: str,
    *,
    struct: str,
    occ: str,
    execution: str | None,
    qty: int = 1,
    price: str = "0",
    approval: str = "approved",
) -> str:
    phash = uuid.uuid4().hex
    structure = {"kind": struct, "legs": [{"occ_symbol": occ}]}
    conn.execute(
        """INSERT INTO proposals (candidate_id, proposal_hash, structure_json, thesis, quant_json,
               sizing_json, expires_at, created_at, run_id, ticker, kind)
           VALUES ('c', ?, ?, '', '{}', '{}', ?, ?, ?, ?, ?)""",
        (phash, json.dumps(structure), SLOT.isoformat(), SLOT.isoformat(), run_id, ticker, kind),
    )
    conn.execute(
        """INSERT INTO approval_requests (proposal_hash, ticker, day, proposal_json, status,
               channel, expires_at, created_at) VALUES (?, ?, '2026-10-08', '{}', ?, 'c', ?, ?)""",
        (phash, ticker, approval, SLOT.isoformat(), SLOT.isoformat()),
    )
    if execution:
        conn.execute(
            """INSERT INTO executions (proposal_hash, kind, status, token_version, band_lo,
                   band_hi, max_steps, contracts, filled_qty, fill_price, started_at)
               VALUES (?, ?, ?, 'arc2', '1', '2', 3, ?, ?, ?, ?)""",
            (phash, kind, execution, qty, qty, price, SLOT.isoformat()),
        )
    return phash


def _ideas(conn: sqlite3.Connection, n: int) -> None:
    for i in range(n):
        _dec(conn, "scalp", "candidate", f"T{i}", "selected", "scalp_candidate")


def _ranked(conn: sqlite3.Connection, *tickers: str) -> None:
    for t in tickers:
        _dec(conn, "research", "shortlist", t, "selected", "shortlisted")


def _open_structure(conn: sqlite3.Connection, sid: str, ticker: str) -> None:
    conn.execute(
        """INSERT INTO open_structures (id, ticker, open_proposal_hash, candidate_id,
               structure_json, contracts, entry_net, opened_at)
           VALUES (?, ?, ?, 'c', '{}', 1, '1', ?)""",
        (sid, ticker, f"p-{sid}", SLOT.isoformat()),
    )


def _full_chain(conn: sqlite3.Connection) -> dict[str, str]:
    jobs = ["research", "exits.mandatory", "quant.exit", "risk.exit", "quant.open", "risk.open"]
    runs = {j: _run(conn, j, i) for i, j in enumerate(jobs)}
    runs["quant.propose"] = _run(conn, "quant.propose", 7)
    runs["broker"] = _run(conn, "broker", 9)
    return runs


def test_no_open_groups_the_reasons() -> None:
    conn = _conn()
    _full_chain(conn)
    _ideas(conn, 21)
    _ranked(conn, "ORCL", "GOOGL", "XOM", "PLTR")
    for t in ("ORCL", "GOOGL"):
        _dec(conn, "quant", "propose", t, "no_trade", "net_ev_floor")
    _dec(conn, "risk", "risk_review", "XOM", "rejected", "risk_reject")
    _dec(conn, "quant", "structure", "PLTR", "no_trade", "quant_skipped")
    assert loop_headline(conn, CHAIN) == [
        "21 ideas → 4 ranked (ORCL, GOOGL, XOM, PLTR) → no open: ORCL, GOOGL below the "
        "Net EV floor; XOM rejected by Risk; PLTR no viable structure."
    ]


def test_open_fill_names_structure_and_price() -> None:
    conn = _conn()
    runs = _full_chain(conn)
    _ideas(conn, 20)
    _ranked(conn, "HOOD", "XOM")
    _dec(conn, "risk", "risk_review", "XOM", "rejected", "risk_reject")
    _proposal(
        conn,
        runs["quant.propose"],
        "HOOD",
        "open",
        struct="vertical_debit",
        occ="HOOD261120P00110000",
        execution="filled",
        qty=2,
        price="6.7500",
    )
    root = loop_root_from_db(conn, CHAIN, SLOT)
    assert root.buys == ["HOOD"]
    assert root.headline == [
        "20 ideas → 2 ranked → bought HOOD Put Debit Spread x2 @ 6.75. "
        "Skipped: XOM rejected by Risk."
    ]
    status, line = root.text().split("\n")
    assert status.endswith("• BUY: HOOD")
    assert line.startswith("_*20 ideas") and line.endswith("*_")


def test_exit_fill_with_realized_pnl_and_blocked_close() -> None:
    conn = _conn()
    runs = _full_chain(conn)
    _ideas(conn, 21)
    _ranked(conn, "ORCL")
    _dec(conn, "quant", "propose", "ORCL", "no_trade", "net_ev_floor")
    for sid, t, code in (
        ("os-mrvl", "MRVL", "exit:watch_review"),
        ("os-gs", "GS", "exit:watch_review"),
        ("os-mu", "MU", "exit:watch_hold"),
    ):
        _open_structure(conn, sid, t)
        _dec(conn, "research", "exit", sid, "noted", code)
    _dec(
        conn,
        "risk",
        "exit",
        "os-mrvl",
        "noted",
        "exit:research_review",
        {"verdict": {"verdict": "close", "reason_code": "concentration"}},
    )
    _dec(
        conn,
        "risk",
        "exit",
        "os-gs",
        "noted",
        "exit:research_review",
        {"verdict": {"verdict": "close", "reason_code": "ev_exhausted"}},
    )
    _dec(
        conn,
        "quant",
        "exit",
        "GS",
        "no_trade",
        "exit:quote_unusable",
        chain=None,
        run_id=runs["quant.propose"],
    )
    _proposal(
        conn,
        runs["quant.propose"],
        "MRVL",
        "close",
        struct="vertical_credit",
        occ="MRVL261120C00100000",
        execution="filled",
        price="-7.2000",
    )
    # the Broker's close journal row has no chain id; it is found through its run
    _dec(
        conn,
        "broker",
        "exit",
        "MRVL",
        "filled",
        "exit:closed",
        {"realized_pnl": "-410.00"},
        chain=None,
        run_id=runs["broker"],
    )
    assert loop_headline(conn, CHAIN) == [
        "21 ideas → 1 ranked (ORCL) → no open: ORCL below the Net EV floor.",
        "Exits: sold MRVL x1 @ 7.20 (concentration), realized -$410; "
        "GS close (EV exhausted) blocked: quotes too wide; 1 held.",
    ]


def test_loop_stopped_after_research() -> None:
    conn = _conn()
    _run(conn, "research", 0)
    _ideas(conn, 21)
    _ranked(conn, "ORCL", "XOM")
    assert loop_headline(conn, CHAIN) == [
        "21 ideas → 2 ranked (ORCL, XOM) → no open: ORCL, XOM not structured "
        "(loop stopped after Research)."
    ]


def test_nothing_ranked() -> None:
    conn = _conn()
    _run(conn, "research", 0)
    _ideas(conn, 5)
    _dec(conn, "research", "shortlist", "session", "no_trade", "market_unclear")
    assert loop_headline(conn, CHAIN) == [
        "5 ideas → 0 ranked: Research opened nothing (market unclear)."
    ]


def test_position_manager_chain_has_only_the_exit_line() -> None:
    conn = _conn()
    runs = {"exits.mandatory": _run(conn, "exits.mandatory", 0)}
    _open_structure(conn, "os-iwm", "IWM")
    _dec(conn, "system", "exit", "os-iwm", "selected", "exit:stop")
    _proposal(
        conn,
        runs["exits.mandatory"],
        "IWM",
        "close",
        struct="vertical_debit",
        occ="IWM261120C00200000",
        execution=None,
        approval="pending",
    )
    assert loop_headline(conn, CHAIN) == ["Exits: close IWM awaiting approval (stop)."]


def test_no_change_root_skips_the_journal() -> None:
    conn = _conn()
    root = loop_root_from_db(conn, CHAIN, SLOT, no_change=True)
    assert root.headline == []
    assert root.text().split("\n")[0].endswith("• HOLD (skip)")
