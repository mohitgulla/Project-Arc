"""D65: a punchy one-sentence bold-italic headline under each loop root and per day recap.

Built only from what the chain journaled (``decisions`` / ``proposals`` /
``executions``), so these tests seed those rows in the shapes the live loop
writes (Oct 8 chains: a Net-EV-floor HOLD, a HOOD open fill, an MRVL close, a
GS close stuck on wide quotes) and assert the sentences.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from typing import TYPE_CHECKING, Any

from arc.pipeline.runner import open_db
from arc.routines.headline import (
    HEADLINE_CHARS,
    day_recap,
    first_sentence,
    headline_sentence,
    loop_headline,
)
from arc.routines.loop import loop_root_from_db
from arc.utils.calendar import ET

if TYPE_CHECKING:
    import sqlite3

CHAIN = "chain-d65"
SLOT = dt.datetime(2026, 10, 8, 15, 40, tzinfo=ET)
STAMP = SLOT.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")  # the store's format


def _conn() -> sqlite3.Connection:
    conn = open_db(":memory:", copy=False)
    conn.execute("PRAGMA foreign_keys = OFF")  # only the rows the headline reads
    return conn


def _run(
    conn: sqlite3.Connection,
    job: str,
    step: int,
    status: str = "ok",
    *,
    chain: str = CHAIN,
    summary: str = "",
    at: str = STAMP,
) -> str:
    run_id = f"run-{job}-{uuid.uuid4().hex[:6]}"
    conn.execute(
        """INSERT INTO routine_runs (run_id, job, chain_run_id, step_index, reason,
               scheduled_for, status, summary) VALUES (?, ?, ?, ?, 'schedule', ?, ?, ?)""",
        (run_id, job, chain, step, at, status, summary),
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
    text: str = "",
) -> None:
    conn.execute(
        """INSERT INTO decisions (id, chain_run_id, run_id, persona, stage, subject, choice,
               reason_code, payload, at, reason_text)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
            STAMP,
            text,
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
            (phash, kind, execution, qty, qty, price, STAMP),
        )
    return phash


def _ideas(conn: sqlite3.Connection, n: int) -> None:
    for i in range(n):
        _dec(conn, "scalp", "candidate", f"T{i}", "selected", "scalp_candidate")


def _ranked(conn: sqlite3.Connection, *picks: tuple[str, str, str, str]) -> None:
    """(ticker, stance, portfolio_fit, thesis) per shortlisted name."""
    for t, stance, fit, thesis in picks:
        _dec(
            conn,
            "research",
            "shortlist",
            t,
            "selected",
            "shortlisted",
            {"stance": stance, "portfolio_fit": fit, "thesis": thesis},
        )


def _market(conn: sqlite3.Connection, text: str) -> None:
    _dec(conn, "research", "shortlist", "market", "noted", "market_read", text=text)


def _open_structure(conn: sqlite3.Connection, sid: str, ticker: str) -> None:
    conn.execute(
        """INSERT INTO open_structures (id, ticker, open_proposal_hash, candidate_id,
               structure_json, contracts, entry_net, opened_at)
           VALUES (?, ?, ?, 'c', '{}', 1, '1', ?)""",
        (sid, ticker, f"p-{sid}", STAMP),
    )


def _full_chain(conn: sqlite3.Connection) -> dict[str, str]:
    jobs = ["research", "exits.mandatory", "quant.exit", "risk.exit", "quant.open", "risk.open"]
    runs = {j: _run(conn, j, i) for i, j in enumerate(jobs)}
    runs["quant.propose"] = _run(conn, "quant.propose", 7)
    runs["broker"] = _run(conn, "broker", 9)
    return runs


HOOD_THESIS = (
    "Bearish put debit vertical on Robinhood. HOOD is in a sticky bear regime with a "
    "20-day drawdown. A capped-cost vertical keeps the loss small."
)


def _close_review(conn: sqlite3.Connection, sid: str, code: str, reason: str) -> None:
    verdict = {"verdict": "close", "reason_code": code, "reason": reason}
    _dec(conn, "risk", "exit", sid, "noted", "exit:research_review", {"verdict": verdict})


class TestLoopHeadline:
    def test_open_fill_leads_with_the_bet_then_the_thesis(self) -> None:
        conn = _conn()
        runs = _full_chain(conn)
        _ideas(conn, 20)
        _ranked(conn, ("HOOD", "bearish", "hedges", HOOD_THESIS), ("XOM", "bullish", "", ""))
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
        # one sentence, "<main point>; <why>." (the structure-only first thesis
        # sentence is skipped for the one with the why)
        assert root.headline == [
            "HOOD bear put debit spread x2 filled at 6.75; "
            "HOOD is in a sticky bear regime with a 20-day drawdown."
        ]
        status, line = root.text().split("\n")
        assert status.endswith("• BUY: HOOD")
        assert line == f"_*{root.headline[0]}*_"

    def test_close_leads_with_why_then_risks_reason(self) -> None:
        conn = _conn()
        runs = _full_chain(conn)
        _ideas(conn, 21)
        _ranked(conn, ("ORCL", "bearish", "", ""))
        _dec(conn, "quant", "propose", "ORCL", "no_trade", "net_ev_floor")
        _open_structure(conn, "os-mrvl", "MRVL")
        _close_review(conn, "os-mrvl", "concentration", "Third semis line in a 70% tech book.")
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
        assert loop_headline(conn, CHAIN) == [
            "Closed MRVL to cut concentration; third semis line in a 70% tech book."
        ]

    def test_open_and_close_in_one_slot(self) -> None:
        conn = _conn()
        runs = _full_chain(conn)
        _ranked(conn, ("TSM", "bullish", "adds_concentration", "TSM has the best idea."))
        _proposal(
            conn,
            runs["quant.propose"],
            "TSM",
            "open",
            struct="vertical_debit",
            occ="TSM261120C00200000",
            execution="filled",
            price="10.75",
        )
        _open_structure(conn, "os-meta", "META")
        _dec(conn, "system", "exit", "os-meta", "selected", "exit:stop")
        _proposal(
            conn,
            runs["exits.mandatory"],
            "META",
            "close",
            struct="long_call",
            occ="META261120C00700000",
            execution="filled",
        )
        assert loop_headline(conn, CHAIN) == [
            "TSM bull call debit spread x1 filled at 10.75; closed META on its stop."
        ]

    def test_no_trade_names_the_main_blocker_and_the_market_read(self) -> None:
        conn = _conn()
        _full_chain(conn)
        _ideas(conn, 21)
        _ranked(
            conn,
            *((t, "bullish", "", "") for t in ("ORCL", "GOOGL", "XOM", "PLTR")),
        )
        for t in ("ORCL", "GOOGL", "XOM"):
            _dec(conn, "quant", "propose", t, "no_trade", "net_ev_floor")
        _dec(conn, "quant", "structure", "PLTR", "no_trade", "quant_skipped")
        _market(conn, "Indexes are trending up on narrow leadership. Breadth is weak.")
        assert loop_headline(conn, CHAIN) == [
            "No trade: ORCL, GOOGL and XOM below the Net EV floor; "
            "indexes are trending up on narrow leadership."
        ]

    def test_stuck_close_outranks_the_market_read(self) -> None:
        conn = _conn()
        _full_chain(conn)
        _ranked(conn, ("ORCL", "bearish", "", ""))
        _dec(conn, "quant", "propose", "ORCL", "no_trade", "net_ev_floor")
        _market(conn, "Indexes are up.")
        _open_structure(conn, "os-gs", "GS")
        _close_review(conn, "os-gs", "ev_exhausted", "Remaining EV is -$32/unit.")
        _dec(conn, "quant", "exit", "os-gs", "no_trade", "exit:quote_unusable")
        assert loop_headline(conn, CHAIN) == [
            "No trade: ORCL below the Net EV floor; GS exit stuck on wide quotes."
        ]

    def test_missed_fill(self) -> None:
        conn = _conn()
        runs = _full_chain(conn)
        _ranked(conn, ("IREN", "bearish", "", ""))
        _proposal(
            conn,
            runs["quant.propose"],
            "IREN",
            "open",
            struct="long_put",
            occ="IREN261120P00040000",
            execution="cancelled",
        )
        assert loop_headline(conn, CHAIN)[0] == (
            "IREN long put missed, no fill inside the price band."
        )

    def test_loop_stopped_after_research(self) -> None:
        conn = _conn()
        _run(conn, "research", 0)
        _ranked(conn, ("ORCL", "bearish", "", ""), ("XOM", "bullish", "", ""))
        assert loop_headline(conn, CHAIN) == ["No trade: loop stopped before pricing ORCL and XOM."]

    def test_nothing_ranked(self) -> None:
        conn = _conn()
        _run(conn, "research", 0)
        _ideas(conn, 5)
        _dec(conn, "research", "shortlist", "session", "no_trade", "market_unclear")
        assert loop_headline(conn, CHAIN) == [
            "No trade: Research passed on all 5 ideas (market unclear)."
        ]

    def test_position_manager_close_awaiting_approval(self) -> None:
        conn = _conn()
        run = _run(conn, "exits.mandatory", 0)
        _open_structure(conn, "os-iwm", "IWM")
        _dec(conn, "system", "exit", "os-iwm", "selected", "exit:stop")
        _proposal(
            conn,
            run,
            "IWM",
            "close",
            struct="vertical_debit",
            occ="IWM261120C00200000",
            execution=None,
            approval="pending",
        )
        assert loop_headline(conn, CHAIN) == ["IWM close awaits your approval."]

    def test_no_change_root_skips_the_journal(self) -> None:
        conn = _conn()
        root = loop_root_from_db(conn, CHAIN, SLOT, no_change=True)
        assert root.headline == []
        status, line = root.text().split("\n")
        assert status.endswith("• HOLD (skip)")
        assert line == "_*Nothing new since the last look; open orders stay.*_"


class TestHeadlineSentence:
    """D65: one sentence, never longer than two lines."""

    def test_second_point_that_overflows_is_dropped_for_one_that_fits(self) -> None:
        long = "x" * 140
        assert headline_sentence("Closed GS", long, "it hedges the book") == (
            "Closed GS; it hedges the book."
        )
        assert headline_sentence("Closed GS", long) == "Closed GS."

    def test_main_point_alone_is_clipped(self) -> None:
        out = headline_sentence("y" * 400)
        assert len(out) <= HEADLINE_CHARS and out.endswith("…")

    def test_every_live_shape_fits(self) -> None:
        for line in (
            "No trade: loop stopped before pricing ORCL, XOM and PLTR",
            "Closed MRVL to cut concentration",
        ):
            assert len(headline_sentence(line, "z" * 200, "indexes are up")) <= HEADLINE_CHARS


class TestFirstSentence:
    def test_label_prefix_and_structure_only_lead(self) -> None:
        assert first_sentence("META: close (ev_exhausted): Remaining EV is -$39. More.") == (
            "close (ev_exhausted): Remaining EV is -$39."
        )
        assert first_sentence(HOOD_THESIS, skip_structure=True).startswith("HOOD is in")
        assert first_sentence("Bullish call vertical on TSM.", skip_structure=True) == (
            "Bullish call vertical on TSM."  # nothing better to fall back to
        )


class TestDayRecap:
    def _day(self, conn: sqlite3.Connection) -> None:
        runs = _full_chain(conn)
        _ranked(conn, ("ORCL", "bearish", "", ""), ("XOM", "bullish", "", ""))
        for t in ("ORCL", "XOM"):
            _dec(conn, "quant", "propose", t, "no_trade", "net_ev_floor")
        _proposal(
            conn,
            runs["quant.propose"],
            "HOOD",
            "open",
            struct="vertical_debit",
            occ="HOOD261120P00110000",
            execution="filled",
        )
        for t, pnl in (("META", "-8765.00"), ("VST", "160.00")):
            _proposal(
                conn,
                runs["quant.propose"],
                t,
                "close",
                struct="long_call",
                occ=f"{t}261120C00100000",
                execution="filled",
            )
            _dec(conn, "broker", "exit", t, "filled", "exit:closed", {"realized_pnl": pnl})
        _run(
            conn,
            "research",
            0,
            chain="chain-skip",
            summary="no_change: inputs unchanged",
            at=STAMP.replace("19:40", "19:50"),
        )

    def test_result_then_main_blocker(self) -> None:
        conn = _conn()
        self._day(conn)
        assert day_recap(conn, SLOT.date(), day_pnl=-1945.57, equity_start=99604.46) == [
            "Red day: -$1,946 (-2.0%) on 1 open and 2 closes; biggest hit META -$8,765."
        ]

    def test_blocker_when_nothing_closed(self) -> None:
        conn = _conn()
        _full_chain(conn)
        _ranked(conn, ("ORCL", "bearish", "", ""), ("XOM", "bullish", "", ""))
        for t in ("ORCL", "XOM"):
            _dec(conn, "quant", "propose", t, "no_trade", "net_ev_floor")
        assert day_recap(conn, SLOT.date(), day_pnl=-120.0) == [
            "Red day: -$120, no trades; Net EV floor blocked 2 of 2 picks."
        ]

    def test_green_day_and_no_pnl(self) -> None:
        conn = _conn()
        self._day(conn)
        assert day_recap(conn, SLOT.date(), day_pnl=500, equity_start=100000)[0].startswith(
            "Green day: +$500 (+0.5%) on 1 open and 2 closes"
        )
        assert day_recap(conn, SLOT.date())[0].startswith("Day done on 1 open and 2 closes")

    def test_quiet_day(self) -> None:
        assert day_recap(_conn(), SLOT.date(), day_pnl=0.0) == ["Flat day: +$0, no trades."]


class TestRecapBroadcast:
    """D65: the recap is a day-thread reply with "Also send to #arc-investor" ticked."""

    def test_dispatcher_posts_the_broadcast_after_the_jobs_own_card(self) -> None:
        import textwrap

        from arc.routines.config import RoutinesConfig
        from arc.routines.dispatcher import Dispatcher
        from arc.routines.handlers import JobResult
        from arc.routines.heartbeat import RecordingNotifier
        from arc.slack.blocks import CardView, header

        conn = _conn()
        cfg = RoutinesConfig.model_validate(
            __import__("yaml").safe_load(
                textwrap.dedent(
                    """
                    personas:
                      broker.reconcile: {schedule: ["16:30"], notify: card}
                    """
                )
            )
        )
        recap = CardView(
            text=":rolled_up_newspaper: *Thu Oct 8 · Day Recap*\n\n_*Red day.*_",
            blocks=[],
            broadcast=True,
        )
        card = CardView(text="🏦 [Broker] Reconcile", blocks=[header("card")])
        notes = RecordingNotifier()
        d = Dispatcher(
            conn,
            cfg,
            handlers={
                "broker.reconcile": lambda ctx: JobResult(
                    summary="clean", card=card, extra_cards=[recap]
                )
            },
            notifier=notes,
            is_halted=lambda: False,
        )
        d.run_manual("broker.reconcile", now=dt.datetime(2026, 10, 8, 16, 30, tzinfo=ET))
        assert [t for _, t in notes.posts][-1] == recap.text
        assert notes.broadcasts == [recap.text]
        assert notes.posts[0][1].startswith("🏦 [Broker]")

    def test_slack_reply_sets_reply_broadcast(self) -> None:
        from unittest.mock import MagicMock

        from arc.slack.client import ArcSlackClient

        fake = MagicMock()
        fake.chat_postMessage.return_value = {"ok": True, "ts": "1.2"}
        client = ArcSlackClient(client=fake)
        client.reply(channel="C1", thread_ts="9.9", text="x", broadcast=True)
        assert fake.chat_postMessage.call_args.kwargs["reply_broadcast"] is True
        client.reply(channel="C1", thread_ts="9.9", text="x")
        assert "reply_broadcast" not in fake.chat_postMessage.call_args.kwargs

    def test_reconcile_builds_the_recap_card(self) -> None:
        from types import SimpleNamespace

        from arc.broker.reconcile_job import _day_recap

        conn = _conn()
        TestDayRecap()._day(conn)
        report = SimpleNamespace(
            day=SLOT.date(),
            day_pnl=-1945.57,
            baseline=SimpleNamespace(value=99604.46),
        )
        view = _day_recap(SimpleNamespace(conn=conn), report)  # type: ignore[arg-type]
        assert view is not None and view.broadcast and view.blocks == []
        assert view.text.split("\n") == [
            ":rolled_up_newspaper: *Thu Oct 8 · Day Recap*",
            "",
            "_*Red day: -$1,946 (-2.0%) on 1 open and 2 closes; biggest hit META -$8,765.*_",
        ]

    def test_recap_failure_never_blocks_the_reconcile(self) -> None:
        from types import SimpleNamespace

        from arc.broker.reconcile_job import _day_recap

        broken = SimpleNamespace(conn=None)  # no store at all
        report = SimpleNamespace(day=SLOT.date(), day_pnl=None, baseline=None)
        assert _day_recap(broken, report) is None  # type: ignore[arg-type]
