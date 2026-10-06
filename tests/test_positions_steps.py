"""E6.4 chain on the bundled SPY recording: positions.evaluate -> investor.exits -> risk.reallocate.

Checks that every close and every swap open is a proposal with a gate decision
and an approval card, that nothing in the chain submits an order, that a swap's
open is proposed only after its close FILLS (and is cancelled otherwise), and
that Risk vetoes (or an LLM failure) stop a swap.
"""

from __future__ import annotations

import ast
import datetime as dt
import json
from decimal import Decimal as D
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from arc.approvals.service import ApprovalService, LogCardPoster
from arc.broker.base import BrokerPosition
from arc.config import ArcSettings
from arc.context.store import ContextStore
from arc.ingest.llm import FixtureSweepLLM, SweepLLMError
from arc.journal.reasons import Choice, JournalPersona, ReasonCode, Stage
from arc.journal.store import JournalStore
from arc.models import LegIntent
from arc.pipeline.market import price_structure
from arc.positions.steps import evaluate, exits, reallocate
from arc.store.db import connect
from arc.store.execution import ExecutionRepo, OpenStructureRepo
from arc.store.migrate import migrate
from arc.store.repos import CandidateRepo, ProposalRepo
from arc.store.swaps import SwapRepo
from arc.utils.calendar import ET
from tests.test_routines_e53 import _ctx, _env

if TYPE_CHECKING:
    import sqlite3

    from arc.pipeline.env import PipelineEnv

NOW = dt.datetime(2026, 9, 25, 16, 0, 5, tzinfo=ET)  # quotes are 15:59:59 ET (fresh for the gate)
SECRET = "g" * 40
LONG_CALL = [["SPY261030C00775000", "long", 1]]
PUT_DEBIT = [["SPY261030P00760000", "long", 1], ["SPY261030P00755000", "short", 1]]
BULL_PUT = [["SPY261030P00711000", "short", 1], ["SPY261030P00710000", "long", 1]]


def settings(**kw: object) -> ArcSettings:
    base: dict[str, object] = {"_env_file": None, "gate_secret": SECRET, "env": "paper"}
    base.update(kw)
    return ArcSettings(**base)  # type: ignore[arg-type]


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = connect(":memory:")
    migrate(c)
    return c


def _priced(env: PipelineEnv, legs: list[list[Any]]) -> Any:
    return price_structure(
        env.market, [(o, LegIntent(s), n) for o, s, n in legs], as_of=NOW.date(), r=0.04
    )


def _open(conn: sqlite3.Connection, env: PipelineEnv, legs: list[list[Any]], entry: str) -> str:
    st = _priced(env, legs).structure
    cid = CandidateRepo(conn).insert(
        ticker="SPY", stance="bullish", catalyst_type="t", confidence=0.7
    )
    phash = f"{len(OpenStructureRepo(conn).list_open()):064d}"
    ProposalRepo(conn).insert(
        candidate_id=cid, proposal_hash=phash, structure_json=st.model_dump_json(),
        thesis="entry", quant_json="{}", sizing_json="{}", expires_at=NOW.isoformat(),
        ticker="SPY",
    )  # fmt: skip
    return OpenStructureRepo(conn).open(
        ticker="SPY", open_proposal_hash=phash, candidate_id=cid,
        structure_json=st.model_dump_json(), contracts=1, entry_net=D(entry),
        now=NOW - dt.timedelta(days=5),
    )  # fmt: skip


def _held(legs: list[list[Any]]) -> list[BrokerPosition]:
    return [
        BrokerPosition(symbol=o, qty=D(n if s == "long" else -n), side=s, avg_entry_price=D("1"))
        for o, s, n in legs
    ]


def _budget_exhausted(conn: sqlite3.Connection, env: PipelineEnv) -> str:
    """What E5.2 propose records when existing SPY exposure used the budget up."""
    from arc.pipeline.steps import _proposal_exit_model

    priced = _priced(env, PUT_DEBIT)
    from arc.exits import load_exit_config

    model = _proposal_exit_model(priced, load_exit_config(), 0.04, None, None)
    assert model is not None
    cid = CandidateRepo(conn).insert(
        ticker="SPY", stance="bearish", catalyst_type="t", confidence=0.6
    )
    with conn:
        rec = JournalStore(conn).record(
            persona=JournalPersona.SIZING, stage=Stage.SIZING, subject="SPY",
            choice=Choice.NO_TRADE, reason_code=ReasonCode.SIZING_BUDGET_EXHAUSTED,
            reason_text="existing max loss leaves nothing", at=NOW - dt.timedelta(hours=1),
            payload={"realloc_source": {
                "candidate_id": cid, "thesis": "SPY pullback into Oct",
                "quant": {"pop": 0.55, "ev": "12", "cost_bps": 30.0},
                "risk_narrative": "fine", "suggestion": 2, "kind": "vertical_debit",
                "legs": PUT_DEBIT, "net_ev": model.managed.net_ev, "pop": model.managed.pop,
                "buying_power": float(priced.structure.max_loss),
            }},
        )  # fmt: skip
    return rec.id


SPECS: dict[str, dict[str, object]] = {
    "positions.evaluate": {
        "every": "30m",
        "reads": ["regime"],
        "writes": ["position_review"],
        "llm": False,
    },  # fmt: skip
    "investor.exits": {
        "every": "30m",
        "reads": ["position_review"],
        "writes": ["proposal"],
        "llm": False,
    },  # fmt: skip
    "risk.reallocate": {"every": "30m", "reads": ["position_review"], "writes": ["proposal"]},
}


def _run(conn: sqlite3.Connection, env: PipelineEnv, job: str, now: dt.datetime = NOW) -> Any:
    fn = {"positions.evaluate": evaluate, "investor.exits": exits, "risk.reallocate": reallocate}
    return fn[job](_ctx(conn, job, SPECS[job], now, settings()), env)


def _env_with(positions: list[BrokerPosition], risk_reply: str | None = None) -> PipelineEnv:
    env = _env(positions)
    info = env.account()
    cash = D("100000")  # settled cash for the D25 cash_debit profile
    env.account = lambda: info.model_copy(update={"cash": cash, "buying_power": cash})
    env.mint_tokens = True  # a live paper run: PASS mints an arc2 token
    if risk_reply is not None:
        env.llms["risk"] = FixtureSweepLLM([risk_reply])
    return env


def _approve(conn: sqlite3.Connection, phash: str, *, approve: bool = True) -> None:
    svc = ApprovalService(conn, settings(), LogCardPoster())
    svc.publish_pending(NOW)
    res = svc.decide(phash, user="U0C5KUMH28G", approve=approve, now=NOW + dt.timedelta(seconds=30))
    assert res.outcome.value == ("approved" if approve else "rejected"), res


def _fill(conn: sqlite3.Connection, phash: str, status: str = "filled") -> None:
    ex = ExecutionRepo(conn)
    assert ex.start(
        proposal_hash=phash, kind="close", token_version="arc2", band_lo=D("0"),
        band_hi=D("1"), max_steps=1, contracts=1, now=NOW,
    )  # fmt: skip
    ex.finish(phash, status=status, now=NOW, filled_qty=1 if status == "filled" else 0)


def _approve_swap(verdict: bool = True, swap_key: str = "sg1") -> str:
    return json.dumps(
        {
            "verdicts": [{"swap_id": swap_key, "approve": verdict, "narrative": "clear edge"}],
            "advisory_notes": "",
        }  # fmt: skip
    )


# ---------------------------------------------------------------------------


def test_evaluate_writes_reviews_and_exits_propose_through_gate_and_approval(
    conn: sqlite3.Connection,
) -> None:
    env = _env_with(_held(BULL_PUT))
    sid = _open(conn, env, BULL_PUT, "-0.90")  # ~98% of max gain captured
    ev = _run(conn, env, "positions.evaluate")
    assert ev.metrics["reviewed"] == 1 and ev.metrics["signal_profit_target"] == 1
    reviews = ContextStore(conn).snapshot(NOW).of_kind("position_review")
    assert [e.subject for e in reviews] == [sid]
    assert reviews[0].payload["remaining_pop"] is not None

    out = _run(conn, env, "investor.exits")
    assert out.metrics == {"signals": 1, "proposed": 1, "gate_passed": 1, "quote_blocked": 0}
    assert "+98% of max gain at 35 DTE" in out.summary
    row = conn.execute("SELECT * FROM proposals WHERE kind = 'close'").fetchone()
    gate = conn.execute(
        "SELECT passed, token FROM gate_decisions WHERE proposal_hash = ?", (row["proposal_hash"],)
    ).fetchone()
    assert gate["passed"] == 1 and gate["token"]  # gate PASS + arc2 token
    rec = conn.execute(
        "SELECT reason_code FROM decisions WHERE proposal_hash = ?", (row["proposal_hash"],)
    ).fetchall()
    assert ReasonCode.EXIT_TAKE_PROFIT.value in {r["reason_code"] for r in rec}
    # the approval card is the only way forward; nothing was executed
    rep = ApprovalService(conn, settings(), LogCardPoster()).publish_pending(NOW)
    assert row["proposal_hash"] in rep.published
    assert conn.execute("SELECT COUNT(*) FROM executions").fetchone()[0] == 0
    # one exit per structure: a rerun while it is pending proposes nothing
    assert _run(conn, env, "investor.exits").metrics["proposed"] == 0


def test_control_panel_exit_override_reaches_position_manager(conn: sqlite3.Connection) -> None:
    """D26: `!arc set exits.vertical_credit.take_profit` must reach positions.evaluate."""
    from arc.control.service import ControlService

    env = _env_with(_held(BULL_PUT))
    _open(conn, env, BULL_PUT, "-0.90")  # ~98% of max gain: fires at the 50% default
    svc = ControlService(
        conn,
        base=settings(approver_slack_user_ids=["U0C5KUMH28G"]),
        now=lambda: NOW,
        is_halted=lambda: False,
    )
    r = svc.set("exits.vertical_credit.take_profit", "100%", actor="U0C5KUMH28G", source="slack")
    assert r.pending is not None  # holding longer is the riskier direction
    svc.confirm(r.pending.code, actor="U0C5KUMH28G", source="slack")
    spec = SPECS["positions.evaluate"]
    ev = evaluate(_ctx(conn, "positions.evaluate", spec, NOW, svc.settings()), env)
    assert ev.metrics["reviewed"] == 1
    assert ev.metrics.get("signal_profit_target", 0) == 0


def test_exits_halted_proposes_nothing(conn: sqlite3.Connection) -> None:
    from arc.gate.halt import HaltSwitch
    from arc.store.repos import HaltRepo

    env = _env_with(_held(BULL_PUT))
    _open(conn, env, BULL_PUT, "-0.90")
    _run(conn, env, "positions.evaluate")
    HaltSwitch(HaltRepo(conn)).halt(reason="test", actor="owner", now=NOW)
    out = _run(conn, env, "investor.exits")
    assert out.metrics["halted"] and out.metrics["proposed"] == 0
    assert conn.execute("SELECT COUNT(*) FROM proposals WHERE kind = 'close'").fetchone()[0] == 0


def _swap_setup(conn: sqlite3.Connection, risk_reply: str | None) -> tuple[PipelineEnv, str, str]:
    env = _env_with(_held(LONG_CALL), risk_reply)
    sid = _open(conn, env, LONG_CALL, "11.415")  # flat long call: holds, no exit signal
    ref = _budget_exhausted(conn, env)
    ev = _run(conn, env, "positions.evaluate")
    assert ev.metrics["signals"] == 0
    return env, sid, ref


def test_swap_close_first_then_open_after_the_close_fills(conn: sqlite3.Connection) -> None:
    env, sid, ref = _swap_setup(conn, _approve_swap())
    out = _run(conn, env, "risk.reallocate")
    assert out.metrics["suggested"] == 1 and out.metrics["swaps_started"] == 1, out.summary
    (sw,) = SwapRepo(conn).by_status("closing")
    assert sw["close_structure_id"] == sid and sw["source_ref"] == ref
    close = conn.execute(
        "SELECT * FROM proposals WHERE proposal_hash = ?", (sw["close_proposal_hash"],)
    ).fetchone()
    assert close["kind"] == "close" and close["swap_id"] == sw["id"]
    gate = conn.execute(
        "SELECT passed, token FROM gate_decisions WHERE proposal_hash = ?",
        (close["proposal_hash"],),
    ).fetchone()
    assert gate["passed"] == 1 and gate["token"]
    # no open yet: it waits for the close to fill
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM proposals WHERE kind = 'open' AND swap_id IS NOT NULL"
        ).fetchone()[0]
        == 0
    )  # noqa: E501
    codes = {r[0] for r in conn.execute("SELECT reason_code FROM decisions")}
    assert {ReasonCode.REALLOC_SUGGESTED.value, ReasonCode.REALLOC_APPROVED.value,
            ReasonCode.EXIT_REALLOCATE.value} <= codes  # fmt: skip

    # still working: nothing changes
    _approve(conn, close["proposal_hash"])
    assert SwapRepo(conn).by_status("closing")

    # the close fills (Investor ladder); the broker no longer holds the call
    _fill(conn, close["proposal_hash"])
    env.positions = list
    later = NOW + dt.timedelta(seconds=20)  # next tick; the recording's quotes are still fresh
    out = _run(conn, env, "risk.reallocate", later)
    (sw,) = SwapRepo(conn).by_status("open_proposed")
    opened = conn.execute(
        "SELECT * FROM proposals WHERE proposal_hash = ?", (sw["open_proposal_hash"],)
    ).fetchone()
    assert opened["kind"] == "open" and opened["swap_id"] == sw["id"] and opened["ticker"] == "SPY"
    sizing = json.loads(opened["sizing_json"])
    assert 1 <= sizing["contracts"] <= 2  # min(Risk suggestion 2, D18 cap)
    gate = conn.execute(
        "SELECT passed, token, violations_json FROM gate_decisions WHERE proposal_hash = ?",
        (opened["proposal_hash"],),
    ).fetchone()
    assert gate["passed"] == 1 and gate["token"], gate["violations_json"]
    rep = ApprovalService(conn, settings(), LogCardPoster()).publish_pending(later)
    assert opened["proposal_hash"] in rep.published  # a normal approval card
    # the source is consumed: no second swap for it
    assert _run(conn, env, "risk.reallocate", later).metrics.get("candidates", 0) == 0


@pytest.mark.parametrize("how", ["rejected", "cancelled", "unfilled_by_day_end"])
def test_swap_whose_close_never_fills_cancels_its_open(conn: sqlite3.Connection, how: str) -> None:
    env, _, _ = _swap_setup(conn, _approve_swap())
    _run(conn, env, "risk.reallocate")
    (sw,) = SwapRepo(conn).by_status("closing")
    later = NOW + dt.timedelta(minutes=30)
    if how == "rejected":
        _approve(conn, sw["close_proposal_hash"], approve=False)
    elif how == "cancelled":
        _approve(conn, sw["close_proposal_hash"])
        _fill(conn, sw["close_proposal_hash"], status="cancelled")
    else:
        later = NOW + dt.timedelta(days=1)
    _run(conn, env, "risk.reallocate", later)
    got = SwapRepo(conn).get(sw["id"])
    assert got is not None and got["status"] == "cancelled" and got["open_proposal_hash"] is None
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM proposals WHERE swap_id = ? AND kind = 'open'", (sw["id"],)
        ).fetchone()[0]
        == 0
    )
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM decisions WHERE reason_code = ?",
            (ReasonCode.REALLOC_CANCELLED.value,),
        ).fetchone()[0]
        == 1
    )


def test_swap_close_on_unusable_quotes_is_cancelled_before_any_order(
    conn: sqlite3.Connection,
) -> None:
    """E6.2a: the swap's close leg goes through the same quote check; no close, no open."""
    env, _, _ = _swap_setup(conn, _approve_swap())
    stale = NOW - dt.timedelta(minutes=10)
    for c in env.market.recording("SPY").contracts:  # type: ignore[attr-defined]
        if c.symbol == LONG_CALL[0][0]:
            c.quote_timestamp = stale
    out = _run(conn, env, "risk.reallocate")
    assert out.metrics["swaps_started"] == 0, out.summary
    assert "quotes unusable" in out.summary and "stale" in out.summary
    (sw,) = SwapRepo(conn).for_day("2026-09-25")
    assert sw["status"] == "cancelled" and sw["close_proposal_hash"] is None
    assert (
        conn.execute("SELECT COUNT(*) FROM proposals WHERE swap_id IS NOT NULL").fetchone()[0] == 0
    )
    j = conn.execute(
        "SELECT payload FROM decisions WHERE reason_code = ?",
        (ReasonCode.EXIT_QUOTE_UNUSABLE.value,),
    ).fetchone()
    assert j is not None and json.loads(j[0])["swap_id"] == sw["id"]


def test_risk_veto_stops_the_swap(conn: sqlite3.Connection) -> None:
    env, _, _ = _swap_setup(conn, _approve_swap(verdict=False))
    out = _run(conn, env, "risk.reallocate")
    assert out.metrics["vetoed"] == 1 and out.metrics["swaps_started"] == 0
    assert [s["status"] for s in SwapRepo(conn).for_day("2026-09-25")] == ["vetoed"]
    assert conn.execute("SELECT COUNT(*) FROM proposals WHERE kind = 'close'").fetchone()[0] == 0
    call = conn.execute("SELECT persona, status FROM persona_calls").fetchone()
    assert (call["persona"], call["status"]) == ("risk_swap", "ok")


def test_missing_verdict_and_llm_failure_fail_closed(conn: sqlite3.Connection) -> None:
    env, _, _ = _swap_setup(conn, _approve_swap(swap_key="sg-unknown"))
    assert _run(conn, env, "risk.reallocate").metrics["vetoed"] == 1

    class Down:
        model = "down"

        def complete(self, prompt: str) -> Any:
            raise SweepLLMError("offline")

    c2 = connect(":memory:")
    migrate(c2)
    env2, _, _ = _swap_setup(c2, None)
    env2.llms["risk"] = Down()  # type: ignore[assignment]
    out = _run(c2, env2, "risk.reallocate")
    assert out.metrics["vetoed"] == 1 and out.metrics["swaps_started"] == 0
    (sw,) = SwapRepo(c2).for_day("2026-09-25")
    assert sw["status"] == "vetoed" and "fail closed" in sw["detail"]


def test_churn_counts_swaps_already_made_today(conn: sqlite3.Connection) -> None:
    env, _, _ = _swap_setup(conn, _approve_swap())
    for i in range(2):  # two earlier swaps today (any status but vetoed counts)
        SwapRepo(conn).create(
            day="2026-09-25", status="cancelled", close_structure_id=f"x{i}", close_ticker="QQQ",
            open_ticker="IWM", source_ref=f"old-{i}", suggestion_json="{}", now=NOW, run_id=None,
        )  # fmt: skip
    out = _run(conn, env, "risk.reallocate")
    assert out.metrics["suggested"] == 0
    codes = [
        r[0] for r in conn.execute("SELECT reason_code FROM decisions WHERE stage = 'reallocate'")
    ]
    assert codes == [ReasonCode.REALLOC_CHURN_DAY.value]


def test_no_direct_submit_path_in_the_position_manager() -> None:
    """Personas never call the broker: arc.positions only writes proposals."""
    root = Path(__file__).resolve().parents[1] / "arc" / "positions"
    for path in root.glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                banned = ("arc.broker", "arc.execution.submission", "arc.execution.ladder")
                assert not node.module.startswith(banned), (path, node.module)
                names = {a.name for a in node.names}
                assert not names & {"submit", "execute", "AlpacaPaperBroker"}, (path, names)
            if isinstance(node, ast.Attribute):
                assert node.attr not in {"submit", "submit_order", "place_order"}, path


def test_live_run_without_gate_secret_fails_before_any_proposal(conn: sqlite3.Connection) -> None:
    """E5.2b: a minting run with no ARC_GATE_SECRET raises; no token-less PASS is stored."""
    from arc.pipeline.steps import GateSecretMissingError

    env = _env_with(_held(BULL_PUT))
    _open(conn, env, BULL_PUT, "-0.90")
    _run(conn, env, "positions.evaluate")
    ctx = _ctx(conn, "investor.exits", SPECS["investor.exits"], NOW, settings(gate_secret=None))
    with pytest.raises(GateSecretMissingError):
        exits(ctx, env)
    assert conn.execute("SELECT COUNT(*) FROM proposals WHERE kind = 'close'").fetchone()[0] == 0


def test_dry_run_never_mints(conn: sqlite3.Connection) -> None:
    env = _env_with(_held(BULL_PUT))
    env.mint_tokens = False  # fixtures / --dry-run
    _open(conn, env, BULL_PUT, "-0.90")
    _run(conn, env, "positions.evaluate")
    assert _run(conn, env, "investor.exits").metrics["gate_passed"] == 1
    (tok,) = conn.execute("SELECT token FROM gate_decisions").fetchone()
    assert tok is None
