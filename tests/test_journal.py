"""E7.4: decision journal — records, reason codes, append-only, attribution, review, CLI."""

from __future__ import annotations

import asyncio
import datetime as _dt
import importlib.util
import json
import sqlite3
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from arc.approvals.service import ApprovalService, LogCardPoster
from arc.config import ArcSettings
from arc.gate.rules import RuleCode
from arc.ingest.llm import LLMResult, _cost, _int
from arc.journal.attribution import (
    attribute,
    calibration,
    expiry_value,
    max_adverse_excursion,
    realised_pnl,
    slippage,
)
from arc.journal.models import DecisionReview, OutcomeStatus
from arc.journal.reasons import (
    Choice,
    JournalPersona,
    ReasonCode,
    Reviewer,
    ReviewLabel,
    RootCause,
    Stage,
    gate_reason,
)
from arc.journal.report import gaps, replay, show_lines
from arc.journal.store import JournalStore, ReviewCitationError
from arc.models import Leg, LegIntent, Proposal
from arc.pipeline.env import FIXTURE_NOW
from arc.pipeline.runner import fixture_run
from arc.routines.config import load_routines
from arc.sizing import size_contracts
from arc.store.db import connect
from arc.store.migrate import migrate

OWNER = "U0C5KUMH28G"
STRANGER = "U0STRANGER"


@pytest.fixture(scope="module")
def _pipeline_db() -> bytes:
    conn, report = fixture_run(ArcSettings(_env_file=None), load_routines())  # type: ignore[call-arg]
    assert len(report.proposals) == 1
    return conn.serialize()


@pytest.fixture
def conn(_pipeline_db: bytes) -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.deserialize(_pipeline_db)
    c.execute("PRAGMA foreign_keys = ON")
    return c


def _phash(conn: sqlite3.Connection) -> str:
    return str(conn.execute("SELECT proposal_hash FROM proposals").fetchone()[0])


def _proposal(conn: sqlite3.Connection) -> Proposal:
    raw = conn.execute("SELECT payload FROM context_entries WHERE kind = 'proposal'").fetchone()[0]
    return Proposal.model_validate_json(raw)


def _codes(conn: sqlite3.Connection, stage: Stage) -> list[tuple[str, str, str]]:
    return [
        (d.subject, str(d.choice), str(d.reason_code))
        for d in JournalStore(conn).decisions()
        if d.stage is stage
    ]


# ---------------------------------------------------------------------------
# Reason codes
# ---------------------------------------------------------------------------


def test_every_gate_rule_has_a_reason_code() -> None:
    """A new gate rule cannot ship without its journal code."""
    for rc in RuleCode:
        assert gate_reason(f"{rc.value}: detail") is ReasonCode(f"gate:{rc.value}")
    assert gate_reason("brand_new_rule: x") is ReasonCode.GATE_RULE_ERROR


def test_pipeline_drop_reasons_are_reason_codes() -> None:
    from arc.pipeline import steps

    for name in dir(steps):
        if name.startswith("DROP_"):
            ReasonCode(getattr(steps, name))  # raises if a drop reason has no code


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        ({"suggestion": 3}, "ok"),
        ({"suggestion": 50}, "capped"),
        ({"suggestion": 0}, "risk_zero"),
        ({"suggestion": 3, "max_loss_per_contract": None}, "unbounded"),
        ({"suggestion": 3, "max_loss_per_contract": Decimal(-1)}, "invalid_input"),
        ({"suggestion": 3, "max_loss_per_contract": Decimal(100_000)}, "cap_zero"),
    ],
)
def test_sizing_codes(kwargs: dict[str, Any], code: str) -> None:
    base: dict[str, Any] = {
        "max_loss_per_contract": Decimal(300),
        "equity": Decimal(100_000),
        "cap_pct": 0.05,
    }
    res = size_contracts(**(base | kwargs))
    assert res.code == code
    ReasonCode(f"sizing:{code}")


# ---------------------------------------------------------------------------
# Pipeline records (offline fixture run)
# ---------------------------------------------------------------------------


class TestPipelineRecords:
    def test_director_selected_rejected_and_not_ranked(self, conn: sqlite3.Connection) -> None:
        rows = _codes(conn, Stage.SHORTLIST)
        assert ("SPY", "selected", "shortlisted") in rows
        assert ("AAPL", "rejected", "not_a_candidate") in rows
        assert ("PLTR", "rejected", "not_a_candidate") in rows
        assert ("NVDA", "rejected", "not_ranked") in rows
        assert ("XOM", "rejected", "not_ranked") in rows
        assert ("session", "noted", "market_read") in rows

    def test_quant_records_every_menu_alternative(self, conn: sqlite3.Connection) -> None:
        rows = _codes(conn, Stage.STRUCTURE)
        assert rows.count(("SPY", "selected", "chosen_from_menu")) == 1
        assert rows.count(("SPY", "rejected", "menu_not_chosen")) == 4
        assert ("SPY", "rejected", "not_in_menu") in rows
        assert ("AAPL", "rejected", "not_shortlisted") in rows

    def test_risk_sizing_gate_and_approval(self, conn: sqlite3.Connection) -> None:
        assert ("SPY", "assessed", "risk_assessed") in _codes(conn, Stage.RISK_REVIEW)
        assert ("QQQ", "rejected", "unknown_structure") in _codes(conn, Stage.RISK_REVIEW)
        assert _codes(conn, Stage.SIZING) == [("SPY", "sized", "sizing:capped")]
        assert _codes(conn, Stage.GATE) == [("SPY", "passed", "gate:pass")]

    def test_not_actionable_card_is_journaled(
        self, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # fixture runs never mint a token → the card is informational, and journaled so
        _svc(conn, monkeypatch).sweep(FIXTURE_NOW)
        assert _codes(conn, Stage.APPROVAL) == [("SPY", "no_trade", "not_actionable:no_token")]

    def test_ids_link_calls_snapshots_and_chain(self, conn: sqlite3.Connection) -> None:
        j = JournalStore(conn)
        chain = j.chain_for_proposal(_phash(conn))
        assert chain and chain.startswith("chain-")
        decisions = j.decisions(chain_run_id=chain)
        assert all(d.inputs_snapshot_id for d in decisions if d.stage is not Stage.APPROVAL)
        llm = [d for d in decisions if d.persona.value in ("director", "quant", "risk")]
        call_ids = {c["id"] for c in j.persona_calls(chain)}
        assert llm and all(d.persona_call_id in call_ids for d in llm)

    def test_market_context_frozen_with_proposal(self, conn: sqlite3.Connection) -> None:
        mc = JournalStore(conn).market_context(_phash(conn))
        assert mc is not None and mc.subject == "SPY"
        assert len(mc.legs) == 4 and all(q.bid is not None for q in mc.legs)
        assert mc.underlying_last and mc.regime == "sideways"

    def test_persona_calls_record_prompt_inputs_and_latency(self, conn: sqlite3.Connection) -> None:
        rows = conn.execute("SELECT * FROM persona_calls WHERE status = 'ok'").fetchall()
        assert len(rows) == 3
        for r in rows:
            assert r["prompt_text"] and json.loads(r["prompt_inputs"])["rules"]
            assert r["latency_ms"] is not None

    def test_empty_shortlist_is_a_no_trade(self) -> None:
        from arc.pipeline import steps

        class Ctx:
            snapshot = type("S", (), {"id": None, "of_kind": staticmethod(lambda k: [])})()
            now = FIXTURE_NOW
            chain_run_id = "chain-x"
            run_id = "run-x"
            settings = ArcSettings(_env_file=None)  # type: ignore[call-arg]

            def __init__(self) -> None:
                self.conn = connect(":memory:")
                migrate(self.conn)
                self.written: list[str] = []

            def write(self, kind: str, subject: str, payload: Any) -> None:
                self.written.append(kind)
                self.conn.commit()

        ctx = Ctx()
        steps.director(ctx, None)  # type: ignore[arg-type]
        (d,) = JournalStore(ctx.conn).decisions()
        assert (d.choice, d.reason_code) == (Choice.NO_TRADE, ReasonCode.NO_CANDIDATES)


# ---------------------------------------------------------------------------
# Append-only
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE decisions SET reason_code = 'x'",
        "DELETE FROM decisions",
        "UPDATE market_contexts SET payload = '{}'",
        "DELETE FROM market_contexts",
    ],
)
def test_journal_is_append_only(conn: sqlite3.Connection, sql: str) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute(sql)


def test_correction_is_a_new_row(conn: sqlite3.Connection) -> None:
    j = JournalStore(conn)
    first = j.decisions(proposal_hash=_phash(conn))[0]
    fix = j.record(
        persona=JournalPersona.SYSTEM,
        stage=first.stage,
        subject=first.subject,
        choice=first.choice,
        reason_code=first.reason_code,
        reason_text="corrected",
        at=FIXTURE_NOW,
        proposal_hash=first.proposal_hash,
        supersedes_id=first.id,
    )
    conn.commit()
    assert j.get(first.id) == first
    assert j.get(fix.id).supersedes_id == first.id  # type: ignore[union-attr]


def test_decisions_roll_back_with_the_step(conn: sqlite3.Connection) -> None:
    j = JournalStore(conn)
    before = len(j.decisions())
    j.record(
        persona="system",
        stage="propose",
        subject="SPY",
        choice="no_trade",
        reason_code="reprice_failed",
        at=FIXTURE_NOW,
    )
    conn.rollback()
    assert len(j.decisions()) == before


def test_unknown_reason_code_is_refused(conn: sqlite3.Connection) -> None:
    with pytest.raises(ValueError):
        JournalStore(conn).record(
            persona="system",
            stage="propose",
            subject="SPY",
            choice="no_trade",
            reason_code="because",
            at=FIXTURE_NOW,
        )


# ---------------------------------------------------------------------------
# Approvals
# ---------------------------------------------------------------------------


def _svc(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> ApprovalService:
    monkeypatch.delenv("ARC_AUTO_APPROVE", raising=False)
    monkeypatch.setenv("ARC_APPROVER_SLACK_USER_IDS", OWNER)
    return ApprovalService(conn, ArcSettings(_env_file=None), LogCardPoster())  # type: ignore[call-arg]


def _actionable(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM approval_requests")
    conn.execute("UPDATE gate_decisions SET token = 'tok-fixture'")
    conn.commit()


class TestApprovalJournal:
    def test_reject_then_optional_reason(
        self, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _actionable(conn)
        svc = _svc(conn, monkeypatch)
        svc.sweep(FIXTURE_NOW)
        ph = _phash(conn)
        assert svc.decide(ph, user=OWNER, approve=False, now=FIXTURE_NOW).accepted
        rows = [
            d for d in JournalStore(conn).decisions(proposal_hash=ph) if d.stage is Stage.APPROVAL
        ]
        click = rows[-1]
        assert (click.persona, click.choice, click.reason_code) == (
            JournalPersona.OWNER,
            Choice.REJECTED,
            ReasonCode.OWNER_REJECT,
        )
        assert svc.record_reason(ph, user=OWNER, text="  ", now=FIXTURE_NOW).outcome == "blank"
        assert svc.record_reason(ph, user=STRANGER, text="x", now=FIXTURE_NOW).outcome == (
            "unauthorized"
        )
        res = svc.record_reason(ph, user=OWNER, text="IV too low for a condor", now=FIXTURE_NOW)
        assert res.outcome == "recorded"
        follow = JournalStore(conn).get(res.decision_id or "")
        assert follow is not None and follow.supersedes_id == click.id
        assert follow.reason_text == "IV too low for a condor"
        assert "IV too low" in conn.execute("SELECT reason FROM approval_requests").fetchone()[0]

    def test_reason_needs_a_rejection(
        self, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _actionable(conn)
        svc = _svc(conn, monkeypatch)
        svc.sweep(FIXTURE_NOW)
        ph = _phash(conn)
        svc.decide(ph, user=OWNER, approve=True, now=FIXTURE_NOW)
        assert svc.record_reason(ph, user=OWNER, text="x", now=FIXTURE_NOW).outcome == (
            "not_rejected"
        )
        codes = [str(d.reason_code) for d in JournalStore(conn).decisions(proposal_hash=ph)]
        assert "owner_approve" in codes

    def test_ttl_expiry_is_journaled(
        self, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _actionable(conn)
        svc = _svc(conn, monkeypatch)
        svc.sweep(FIXTURE_NOW)
        svc.expire_due(FIXTURE_NOW + _dt.timedelta(hours=2))
        last = JournalStore(conn).decisions(proposal_hash=_phash(conn))[-1]
        assert (last.persona, last.choice, last.reason_code) == (
            JournalPersona.SYSTEM,
            Choice.EXPIRED,
            ReasonCode.TTL_EXPIRED,
        )


# ---------------------------------------------------------------------------
# Attribution
# ---------------------------------------------------------------------------


def test_condor_pnl_example() -> None:
    # sold for 1.66 credit, bought back for 0.40: +$126 per contract
    assert realised_pnl(entry=Decimal("-1.66"), exit_=Decimal("-0.40"), contracts=1) == 126


def test_slippage_sign_and_bps() -> None:
    usd, bps = slippage(
        limit=Decimal("-1.65"),
        fill=Decimal("-1.60"),
        contracts=2,
        max_loss_per_contract=Decimal(334),
    )
    assert usd == Decimal("10.00") and bps == pytest.approx(10 / 668 * 10_000)
    assert (
        slippage(limit=Decimal(1), fill=Decimal(1), contracts=1, max_loss_per_contract=None)[1]
        is None
    )


_px = st.decimals(min_value=Decimal("-50"), max_value=Decimal("50"), places=2)


@given(entry=_px, marks=st.lists(_px, max_size=10), n=st.integers(1, 50))
def test_mae_is_never_positive_and_bounds_every_mark(
    entry: Decimal, marks: list[Decimal], n: int
) -> None:
    mae = max_adverse_excursion(entry=entry, marks=marks, contracts=n)
    assert mae <= 0
    for m in marks:
        assert realised_pnl(entry=entry, exit_=m, contracts=n) >= mae


@given(settle=st.decimals(min_value=Decimal(0), max_value=Decimal(2000), places=2))
def test_condor_expiry_value_bounded_by_wings(settle: Decimal) -> None:
    legs = [
        Leg(occ_symbol="SPY261030P00740000", side=LegIntent.LONG, ratio=1),
        Leg(occ_symbol="SPY261030P00745000", side=LegIntent.SHORT, ratio=1),
        Leg(occ_symbol="SPY261030C00798000", side=LegIntent.SHORT, ratio=1),
        Leg(occ_symbol="SPY261030C00803000", side=LegIntent.LONG, ratio=1),
    ]
    v = expiry_value(legs, settle)
    assert Decimal(-5) <= v <= 0
    if Decimal(745) <= settle <= Decimal(798):
        assert v == 0


def test_attribute_lifecycle(conn: sqlite3.Connection) -> None:
    p = _proposal(conn)
    ph = _phash(conn)
    kw: dict[str, Any] = {"proposal_hash": ph, "at": FIXTURE_NOW}
    assert attribute(p, traded=False, **kw).status is OutcomeStatus.NOT_TRADED
    assert attribute(p, traded=True, **kw).status is OutcomeStatus.NEVER_FILLED
    fill = Decimal("-1.60")
    op = attribute(p, traded=True, entry_fill=fill, marks=[Decimal("-2.10")], **kw)
    assert op.status is OutcomeStatus.OPEN and op.max_adverse_excursion == Decimal(-50) * 14
    closed = attribute(
        p,
        traded=True,
        entry_fill=fill,
        exit_fill=Decimal("-0.40"),
        settlement=Decimal(770),
        opened_at=_dt.date(2026, 9, 28),
        closed_at=_dt.date(2026, 10, 9),
        exit_reason="profit_take_50",
        **kw,
    )
    assert closed.status is OutcomeStatus.CLOSED
    assert closed.realised_pnl == Decimal(120) * 14
    assert closed.hold_to_expiry_shadow_pnl == Decimal(160) * 14  # worthless at 770
    assert closed.days_held == 11
    worthless = attribute(
        p, traded=True, entry_fill=fill, settlement=Decimal(770), expired=True, **kw
    )
    assert worthless.status is OutcomeStatus.EXPIRED_WORTHLESS
    JournalStore(conn).record_outcome(closed)
    conn.commit()
    assert JournalStore(conn).outcome(ph).realised_pnl == closed.realised_pnl  # type: ignore[union-attr]


def test_calibration_buckets() -> None:
    pts = [("quant", 0.62, True), ("quant", 0.65, False), ("director", 0.9, False)]
    by = {(b.persona, b.lo): b for b in calibration(pts)}
    q = by[("quant", 0.6)]
    assert q.n == 2 and q.hit_rate == 0.5 and q.gap == pytest.approx(0.5 - 0.635)
    assert by[("director", 0.8)].gap == pytest.approx(-0.9)
    with pytest.raises(ValueError):
        calibration(pts, buckets=0)


# ---------------------------------------------------------------------------
# Reviews
# ---------------------------------------------------------------------------


def test_review_must_cite_existing_decisions(conn: sqlite3.Connection) -> None:
    j = JournalStore(conn)
    ph = _phash(conn)
    base = {
        "proposal_hash": ph,
        "label": ReviewLabel.GOOD_DECISION_BAD_OUTCOME,
        "root_cause": RootCause.TIMING,
        "reviewer": Reviewer.AUDITOR,
        "at": FIXTURE_NOW,
    }
    with pytest.raises(ReviewCitationError, match="at least one"):
        j.add_review(DecisionReview(**base))
    with pytest.raises(ReviewCitationError, match="unknown decision"):
        j.add_review(DecisionReview(**base, cites=["dec-invented"]))
    cite = j.decisions(proposal_hash=ph)[0].id
    with pytest.raises(ReviewCitationError, match="unknown proposal"):
        j.add_review(DecisionReview(**(base | {"proposal_hash": "f" * 64}), cites=[cite]))
    rid = j.add_review(DecisionReview(**base, cites=[cite]))
    (r,) = j.reviews(proposal_hash=ph)
    assert r.id == rid and r.cites == [cite]
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM decision_reviews")


# ---------------------------------------------------------------------------
# Report / CLI
# ---------------------------------------------------------------------------


def test_show_tree_orders_stages(conn: sqlite3.Connection) -> None:
    lines = show_lines(conn, _phash(conn)[:10])
    heads = [ln.split(" (")[0] for ln in lines if ln.startswith("── ")]
    assert heads[:8] == [
        "── candidate",
        "── shortlist",
        "── structure",
        "── risk_review",
        "── propose",
        "── sizing",
        "── gate",
        "── persona calls",
    ]
    text = "\n".join(lines)
    assert "[Quant] SPY • rejected • menu_not_chosen" in text
    assert "x14 (Risk 20, cap 14)" in text


def test_replay_matches_recorded_prompts(conn: sqlite3.Connection) -> None:
    res = replay(conn, _phash(conn))
    assert [r.persona for r in res] == ["director", "quant", "risk"]
    assert all(r.ok for r in res), res


def test_replay_detects_a_changed_snapshot_input(conn: sqlite3.Connection) -> None:
    conn.execute("DROP TRIGGER IF EXISTS persona_calls_no_update")
    conn.execute(
        "UPDATE persona_calls SET prompt_inputs = json_set(prompt_inputs, '$.scan_date', "
        "'1999-01-01') WHERE persona = 'director'"
    )
    res = {r.persona: r for r in replay(conn, _phash(conn))}
    assert not res["director"].ok and "MISMATCH" in res["director"].detail


def test_gaps_without_history(conn: sqlite3.Connection, tmp_path: Path) -> None:
    from arc.journal.report import ShadowPricer

    rep = gaps(conn, pricer=ShadowPricer(tmp_path))
    assert rep.rejected["structure:menu_not_chosen"] == 4
    assert rep.rejected["shortlist:not_ranked"] == 2
    assert rep.shadow.startswith("n/a")
    assert all(a.better_by is None for a in rep.alternatives)
    text = "\n".join(rep.lines())
    assert "calibration" in text and "n/a: no closed trades yet" in text


def test_cli_show_and_review(conn: sqlite3.Connection, tmp_path: Path, capsys) -> None:  # noqa: ANN001
    from arc.cli import main

    db = tmp_path / "arc.db"
    disk = sqlite3.connect(db)
    conn.backup(disk)
    disk.close()
    ph = _phash(conn)
    assert main(["journal", "show", ph[:12], "--db", str(db)]) == 0
    assert "decision journal · chain chain-" in capsys.readouterr().out
    cite = JournalStore(conn).decisions(proposal_hash=ph)[0].id
    rc = main(
        [
            "journal", "review", ph[:12], "--label", "bad_decision_good_outcome",
            "--root-cause", "sizing", "--cite", cite, "--db", str(db),
        ]
    )  # fmt: skip
    assert rc == 0 and json.loads(capsys.readouterr().out)["proposal_hash"] == ph
    assert main(["journal", "show", "nope", "--db", str(db)]) == 2
    assert main(["journal", "replay", "nope", "--db", str(db)]) == 2


# ---------------------------------------------------------------------------
# LLM usage + plugin modal
# ---------------------------------------------------------------------------


def test_usage_parsing() -> None:
    assert _int(12) == 12 and _int(True) is None and _int("3") is None
    assert _cost({"cost_status": "included", "estimated_cost_usd": 1.2}) == 0.0
    assert _cost({"estimated_cost_usd": "0.0123"}) == pytest.approx(0.0123)
    assert _cost({}) is None
    assert LLMResult(text="", model="m").input_tokens is None


PLUGIN = Path(__file__).resolve().parent.parent / "hermes/plugins/arc-approvals/__init__.py"


def _plugin() -> Any:
    spec = importlib.util.spec_from_file_location("arc_approvals_plugin_e74", PLUGIN)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


H = "b" * 64


def test_plugin_reason_modal_round_trip() -> None:
    plugin = _plugin()
    view = plugin.build_reason_modal(H)
    assert view["private_metadata"] == H and view["blocks"][1]["optional"] is True
    view["state"] = {"values": {"arc_reason": {"reason": {"value": "too close to FOMC"}}}}
    assert plugin.parse_submission({"user": {"id": OWNER}}, view) == (H, OWNER, "too close to FOMC")
    view["state"] = {"values": {}}
    assert plugin.parse_submission({"user": {"id": OWNER}}, view) == (H, OWNER, "")
    assert plugin.parse_submission({"user": {"id": OWNER}}, {"callback_id": "other"}) is None
    bad = plugin.build_reason_modal("nothex")
    assert plugin.parse_submission({"user": {"id": OWNER}}, bad) is None


def test_plugin_reject_opens_modal_then_decides(monkeypatch: pytest.MonkeyPatch) -> None:
    plugin = _plugin()
    calls: list[Any] = []

    class Client:
        async def views_open(self, **kw: Any) -> None:
            calls.append(("open", kw["trigger_id"], kw["view"]["private_metadata"]))

    class App:
        client = Client()

        def view(self, cb_id: str) -> Any:
            calls.append(("view", cb_id))
            return lambda fn: fn

    plugin.slack_handlers(App())
    monkeypatch.setattr(plugin, "run_decide", lambda *a: calls.append(("decide", *a)))
    monkeypatch.setattr(plugin, "run_reason", lambda *a: calls.append(("reason", *a)))

    async def ack() -> None:
        return None

    body = {"user": {"id": OWNER}, "container": {"message_ts": "1.1"}, "trigger_id": "T1"}
    asyncio.run(plugin.on_action(ack, body, {"action_id": "arc_reject", "value": H}))
    asyncio.run(plugin.on_action(ack, body, {"action_id": "arc_approve", "value": H}))
    view = plugin.build_reason_modal(H)
    asyncio.run(plugin.on_reason_submit(ack, {"user": {"id": OWNER}}, view))
    assert calls == [
        ("view", "arc_reject_reason"),
        ("open", "T1", H),
        ("decide", H, False, OWNER, "1.1"),
        ("decide", H, True, OWNER, "1.1"),
        ("reason", H, OWNER, ""),
    ]


def test_plugin_modal_failure_does_not_block_reject(monkeypatch: pytest.MonkeyPatch) -> None:
    plugin = _plugin()

    class Client:
        async def views_open(self, **kw: Any) -> None:
            raise RuntimeError("expired_trigger_id")

    plugin.SLACK.client = Client()
    seen: list[Any] = []
    monkeypatch.setattr(plugin, "run_decide", lambda *a: seen.append(a))

    async def ack() -> None:
        return None

    body = {"user": {"id": OWNER}, "trigger_id": "T"}
    asyncio.run(plugin.on_action(ack, body, {"action_id": "arc_reject", "value": H}))
    assert seen == [(H, False, OWNER, "")]


def test_plugin_registers_view_handler_factory() -> None:
    plugin = _plugin()
    got: list[Any] = []

    class Ctx:
        def register_slack_action_handler(self, action_id: str, cb: Any) -> None:
            got.append(action_id)

        def register_platform_handler(self, platform: str, factory: Any) -> None:
            got.append((platform, factory.__name__))

    plugin.register(Ctx())
    assert got == ["arc_approve", "arc_reject", ("slack", "slack_handlers")]
