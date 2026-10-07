"""E13.14 fixture rows: the D56 exit path on the Positions page (Research chain).

``add_exits(conn, now, sids)`` writes one Research ``exit_watchlist``, Quant ``exit_case`` entries,
a Risk ``risk_exit_review`` and the ``position_review`` rows the cases answer, for the
fixture's open structures (``sids``: tag -> structure id):

* SPY — watch ``review`` (thesis weakened), exit case ``close``, Risk ``close``.
* QQQ — watch ``hold`` (intact), exit case ``hold`` (profit target), Risk ``hold``.
* NVDA — a mandatory ``stop`` signal on its latest review (no case: mandatory exits
  stay deterministic).
* The QQQ close proposal (``exit-qqq``) gets a run id and its ``proposal`` context
  entry carries a Risk ``exit_review`` (what ``quant.propose`` writes on a close).

Also used by ``scripts/tower_fixture_db.py --exits`` and the Playwright run.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import TYPE_CHECKING, Any

from arc.context.kinds import (
    ExitCasePayload,
    ExitWatchlistPayload,
    PositionReviewPayload,
    RiskExitReviewPayload,
)
from arc.context.store import ContextStore
from arc.context.ttl import Ttl
from arc.models import Structure
from arc.personas.schemas import ExitWatchItem, RiskExitVerdict
from arc.positions.evaluate import ExitSignal, SignalKind
from arc.positions.exit_case import ExitCaseFacts, ExitTrigger

if TYPE_CHECKING:
    import sqlite3

__all__ = ["add_exits"]

_TTL = Ttl(duration=dt.timedelta(hours=8))


def _review(
    conn: sqlite3.Connection, sid: str, ticker: str, now: dt.datetime, signals: list[ExitSignal]
) -> Any:
    row = conn.execute("SELECT structure_json FROM open_structures WHERE id = ?", (sid,)).fetchone()
    return PositionReviewPayload(
        structure=Structure.model_validate_json(row[0]),
        structure_id=sid, ticker=ticker, kind="debit_vertical", credit=False, contracts=2,
        as_of=now.date(), dte=17, entry_net=3.30, current_value=4.10, pnl=80.0,
        pnl_total=160.0, pct_of_max_gain=0.6, pct_of_debit=0.24, take_profit_pct=0.5,
        buying_power=330.0, close_now_net=74.0, remaining_ev=-6.5, remaining_pop=0.58,
        entry_managed_net_ev=21.0, signals=signals,
    ).model_dump(mode="json")  # fmt: skip


def _facts(close_now: float, ev_hold: float, ev_managed: float) -> ExitCaseFacts:
    return ExitCaseFacts(
        dte=17, contracts=2, credit=False, pnl_total=160.0, pct_of_max_gain=0.6,
        stop_state="armed_eod", close_now_net=close_now, remaining_ev_hold=ev_hold,
        remaining_ev_managed=ev_managed, thesis_status="weakened",
    )  # fmt: skip


def add_exits(conn: sqlite3.Connection, now: dt.datetime, sids: dict[str, str]) -> None:
    """Write the Research exit chain for the fixture's open SPY/QQQ/NVDA structures."""
    store = ContextStore(conn)
    spy, qqq, nvda = sids["pos-spy"], sids["pos-qqq"], sids["pos-nvda"]
    at = now - dt.timedelta(minutes=40)

    def write(kind: str, subject: str, payload: Any, when: dt.datetime = at) -> None:
        store.write(
            kind=kind, subject=subject, payload=payload, produced_by="research",
            ttl=_TTL, valid_from=when, now=when,
        )  # fmt: skip

    old = now - dt.timedelta(days=1)  # an older review per name: the latest must win
    write("position_review", spy, _review(conn, spy, "SPY", old, []), old)
    write("position_review", spy, _review(conn, spy, "SPY", now, []))
    write("position_review", qqq, _review(conn, qqq, "QQQ", now, [
        ExitSignal(kind=SignalKind.PROFIT_TARGET, detail="60% of max gain >= 50% target"),
    ]))  # fmt: skip
    write("position_review", nvda, _review(conn, nvda, "NVDA", now, [
        ExitSignal(kind=SignalKind.STOP, detail="loss 52% of debit >= 50% stop (EOD marks)"),
    ]))  # fmt: skip
    write("exit_watchlist", "session", ExitWatchlistPayload(
        as_of=at.isoformat(), positions_seen=3, items=[
            ExitWatchItem(structure_id=spy, ticker="SPY", action="review",
                          thesis_status="weakened",
                          evidence=["story:st-1 breadth fading into CPI", "iv_rank 0.71"],
                          reason="Breadth is fading and CPI is two sessions out"),
            ExitWatchItem(structure_id=qqq, ticker="QQQ", action="hold",
                          thesis_status="intact", evidence=["scout:yt-2 bearish tech"],
                          reason="Bear thesis intact"),
        ],
    ).model_dump(mode="json"))  # fmt: skip
    write("exit_case", spy, ExitCasePayload(
        structure_id=spy, ticker="SPY", kind="debit_vertical",
        triggers=[ExitTrigger(kind="research_review",
                              detail="Research: thesis weakened: breadth fading")],
        facts=_facts(74.0, -6.5, 21.0), recommendation="close",
        rationale="Remaining EV is negative and the thesis weakened",
    ).model_dump(mode="json"))  # fmt: skip
    write("exit_case", qqq, ExitCasePayload(
        structure_id=qqq, ticker="QQQ", kind="debit_vertical",
        triggers=[ExitTrigger(kind="profit_target", detail="60% of max gain >= 50% target")],
        facts=_facts(98.0, 12.0, 21.0), recommendation="hold",
        rationale="EV remaining beats closing now",
    ).model_dump(mode="json"))  # fmt: skip
    write("risk_exit_review", "session", RiskExitReviewPayload(
        as_of=at.isoformat(), case_ids=[], verdicts=[
            RiskExitVerdict(structure_id=spy, verdict="close", reason_code="thesis_broken",
                            reason="Negative remaining EV into CPI"),
            RiskExitVerdict(structure_id=qqq, verdict="hold", reason_code="ev_remaining",
                            reason="EV remaining beats closing now"),
        ],
    ).model_dump(mode="json"))  # fmt: skip

    # The close proposal's context entry, as quant.propose's close branch writes it.
    run_id = "run-fx-quant-propose"
    row = conn.execute("SELECT * FROM proposals WHERE kind = 'close' AND ticker = 'QQQ'").fetchone()
    conn.execute("UPDATE proposals SET run_id = ? WHERE id = ?", (run_id, row["id"]))
    payload = {
        "candidate_id": row["candidate_id"] or "cand-exit-qqq",
        "structure": json.loads(row["structure_json"]),
        "thesis": row["thesis"],
        "quant": json.loads(row["quant_json"]),
        "risk_narrative": row["risk_narrative"] or "",
        "sizing": json.loads(row["sizing_json"]),
        "expires_at": row["expires_at"],
        "exit_review": RiskExitVerdict(
            structure_id=qqq, verdict="close", reason_code="ev_exhausted",
            reason="Take the 60% gain before CPI",
        ).model_dump(mode="json"),
    }  # fmt: skip
    store.write(
        kind="proposal", subject="QQQ", payload=payload, produced_by="quant.propose",
        ttl=_TTL, run_id=run_id, valid_from=at, now=at,
    )  # fmt: skip
