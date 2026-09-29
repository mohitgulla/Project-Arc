"""E6.1: proposal card, Approve/Reject, ApprovalRecord, TTL, approver ids, logging."""

from __future__ import annotations

import datetime as _dt
import importlib.util
import json
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

import pytest
import structlog
from hypothesis import given, settings
from hypothesis import strategies as st

from arc.approvals import ACTION_APPROVE, ACTION_REJECT, render_card
from arc.approvals.card import strategy_name, title
from arc.approvals.service import (
    AUTO_APPROVER,
    TTL_ACTOR,
    ApprovalService,
    LogCardPoster,
    Outcome,
    PostedCard,
    RequestStatus,
    approval_record,
)
from arc.approvals.trail import load_trail
from arc.config import ArcSettings
from arc.gate.rules import proposal_hash
from arc.models import ApprovalDecision, GateDecision, Proposal, StructureKind
from arc.pipeline.env import FIXTURE_NOW
from arc.pipeline.runner import fixture_run
from arc.routines.config import load_routines
from arc.routines.runs import RoutineEventRepo

OWNER = "U0C5KUMH28G"
STRANGER = "U0STRANGER"
TTL = _dt.timedelta(seconds=1200)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def arc_settings(monkeypatch: pytest.MonkeyPatch) -> ArcSettings:
    for var in ("ARC_AUTO_APPROVE", "ARC_APPROVER_SLACK_USER_IDS", "ARC_APPROVAL_TTL_SECONDS"):
        monkeypatch.delenv(var, raising=False)
    return ArcSettings(_env_file=None, account_profile="margin")  # type: ignore[call-arg]


@pytest.fixture(scope="module")
def _pipeline_db() -> bytes:
    """One offline `arc propose --fixtures` run, serialised so each test gets a copy."""
    conn, report = fixture_run(
        ArcSettings(_env_file=None, account_profile="margin"), load_routines()
    )  # type: ignore[call-arg]
    assert len(report.proposals) == 1
    return conn.serialize()


def _db(raw: bytes, *, token: str | None = "tok-fixture") -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.deserialize(raw)
    conn.execute("PRAGMA foreign_keys = ON")
    # Fixture runs never mint tokens; a live paper run would. Simulate that here.
    conn.execute("UPDATE gate_decisions SET token = ?", (token,))
    conn.commit()
    return conn


@pytest.fixture
def conn(_pipeline_db: bytes) -> sqlite3.Connection:
    return _db(_pipeline_db)


class RecordingPoster(LogCardPoster):
    """Like Slack: returns a channel + ts so cards can be updated."""

    def post(self, day: _dt.date, view: Any) -> PostedCard:
        super().post(day, view)
        return PostedCard(channel="C_INV", thread_ts="100.1", message_ts=f"200.{len(self.posted)}")


@pytest.fixture
def poster() -> RecordingPoster:
    return RecordingPoster()


@pytest.fixture
def svc(conn: sqlite3.Connection, arc_settings: ArcSettings, poster: RecordingPoster):  # noqa: ANN201
    return ApprovalService(conn, arc_settings, poster)


def _phash(conn: sqlite3.Connection) -> str:
    return str(conn.execute("SELECT proposal_hash FROM proposals").fetchone()[0])


def _proposal(conn: sqlite3.Connection) -> Proposal:
    raw = conn.execute("SELECT payload FROM context_entries WHERE kind = 'proposal'").fetchone()[0]
    return Proposal.model_validate_json(raw)


def _status(conn: sqlite3.Connection, phash: str) -> str:
    return str(
        conn.execute(
            "SELECT status FROM approval_requests WHERE proposal_hash = ?", (phash,)
        ).fetchone()[0]
    )


def _approvals(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM approvals").fetchall()


NOW = FIXTURE_NOW  # proposal created at 16:00 ET, expires 16:20 ET


# ---------------------------------------------------------------------------
# Card rendering
# ---------------------------------------------------------------------------


def _text(blocks: list[dict[str, Any]]) -> str:
    return json.dumps(blocks, ensure_ascii=False)


class TestTitle:
    """One title format for every strategy: ticker • expiry (DTE) • strategy."""

    @pytest.mark.parametrize(
        ("kind", "name"),
        [
            (StructureKind.IRON_CONDOR, "Iron Condor"),
            (StructureKind.VERTICAL_CREDIT, "Put Credit Spread"),
            (StructureKind.VERTICAL_DEBIT, "Put Debit Spread"),
            (StructureKind.LONG_PUT, "Long Put"),
            (StructureKind.LONG_CALL, "Long Call"),
            (StructureKind.OTHER, "Custom"),
            (None, "Custom"),
        ],
    )
    def test_strategy_names(self, conn: sqlite3.Connection, kind: object, name: str) -> None:
        p = _proposal(conn)
        p = p.model_copy(update={"structure": p.structure.model_copy(update={"kind": kind})})
        assert strategy_name(p) == name
        assert title(p) == f"[Quant] Proposal: SPY • Oct 30 (35 DTE) • {name}"

    def test_call_side_from_first_leg(self, conn: sqlite3.Connection) -> None:
        p = _proposal(conn)
        calls = [leg for leg in p.structure.legs if leg.occ_symbol[-9] == "C"]
        st = p.structure.model_copy(update={"kind": StructureKind.VERTICAL_CREDIT, "legs": calls})
        assert strategy_name(p.model_copy(update={"structure": st})) == "Call Credit Spread"


class TestLayout:
    """Every card follows one grammar: title, summary line, legs, fact grid, reasoning, ids."""

    def test_section_order(self, conn: sqlite3.Connection) -> None:
        p = _proposal(conn)
        ph = proposal_hash(p)
        d = GateDecision(proposal_hash=ph, passed=True, token="t")
        trail = load_trail(conn, _phash(conn), "SPY")
        blocks = render_card(p, d, proposal_hash=ph, actionable=True, trail=trail).blocks
        kinds = [b["type"] for b in blocks]
        assert kinds[:4] == ["header", "context", "divider", "section"]
        assert kinds[-2:] == ["context", "actions"]
        legs = blocks[3]["text"]["text"]
        assert legs.startswith("*Legs*\n") and "```" not in legs  # item 2: plain lines
        summary = blocks[1]["elements"][0]["text"]
        assert summary == (  # item 1: sentence case, Net EV, x14, gate
            "Credit 1.66 · Max gain $165.55 · Max loss $334.45 · PoP 62% · "
            "Net EV $9.22 managed / $32.36 hold · x14 · Account margin · "
            ":white_check_mark: Gate PASS"
        )

    def test_trail_attributes_each_persona(self, conn: sqlite3.Connection) -> None:
        p = _proposal(conn)
        ph = proposal_hash(p)
        trail = load_trail(conn, _phash(conn), "SPY")
        assert trail.chain_run_id and trail.director and trail.quant and trail.risk
        text = _text(render_card(p, None, proposal_hash=ph, actionable=False, trail=trail).blocks)
        assert (
            "*[Director] Thesis (rank 1 of 3, neutral, confidence 70%)*" in text
        )  # E5.7: SPY, NVDA, XOM
        assert "Regime: Fixture: low realised vol" in text
        assert "*[Quant] Structure choice (confidence 70%)*" in text
        assert "*[Risk] Review*" in text and "Rating *moderate*" in text
        assert "Suggested 20 → sized 14 (5% equity cap)" in text
        assert "Calendar: Fixture: FOMC Oct 28" in text
        assert "Regime *sideways*" in text and "Director market read *risk_on*" in text
        assert f"chain `{trail.chain_run_id}`" in text

    def test_missing_trail_still_renders(self, conn: sqlite3.Connection) -> None:
        p = _proposal(conn)
        ph = proposal_hash(p)
        assert load_trail(conn, "no-such-hash", "SPY").chain_run_id is None
        text = _text(render_card(p, None, proposal_hash=ph, actionable=False).blocks)
        assert "*[Director] Thesis*" in text and "*[Risk] Review*" in text

    def test_published_card_carries_trail(
        self, svc: ApprovalService, poster: RecordingPoster
    ) -> None:
        svc.publish_pending(NOW)
        assert "[Quant] Structure choice" in _text(poster.posted[0][1].blocks)


class TestCard:
    def test_has_every_required_field(self, conn: sqlite3.Connection) -> None:
        p = _proposal(conn)
        ph = proposal_hash(p)
        d = GateDecision(proposal_hash=ph, passed=True, token="t")
        view = render_card(p, d, proposal_hash=ph, actionable=True)
        text = _text(view.blocks)
        expected = "[Quant] Proposal: SPY • Oct 30 (35 DTE) • Iron Condor"
        assert view.blocks[0]["text"]["text"] == expected
        assert view.text == f"{expected} • x14 • gate PASS"
        for leg in p.structure.legs:  # legs: every strike shown
            assert f"{leg.occ_symbol[-8:-3].lstrip('0')}" in text
        assert "*Entry*" in text and "Credit 1.66" in text and "Limit credit 1.65" in text
        assert "*Payoff (per contract)*" in text and "$165.55" in text and "$334.45" in text
        assert "Risk/Reward 2.02 : 1" in text and "Reward/risk" not in text  # item 4
        assert "*Position (x14)*" in text and "$2,317.70" in text and "$4,682.30" in text
        assert "*Breakevens*" in text and "BE 743.34" in text and "BE 799.66" in text
        assert "*Net Greeks (position)*" in text
        assert "PoP 62%" in text and "Quant EV -$18.74" in text
        assert "*Gate*" in text and "PASS" in text and "Token issued" in text
        # D18/D24: sizing is at the band's worst price (max loss $4,760, not $4,682.30 at mid)
        assert "14 contract(s)" in text and "4.76% of equity" in text
        assert "Expires 16:20 ET" in text

    def test_buttons_carry_the_proposal_hash(self, conn: sqlite3.Connection) -> None:
        p = _proposal(conn)
        ph = proposal_hash(p)
        view = render_card(p, None, proposal_hash=ph, actionable=True)
        actions = next(b for b in view.blocks if b["type"] == "actions")
        ids = {e["action_id"]: e["value"] for e in actions["elements"]}
        assert ids == {ACTION_APPROVE: ph, ACTION_REJECT: ph}

    def test_informational_card_has_no_buttons(self, conn: sqlite3.Connection) -> None:
        p = _proposal(conn)
        ph = proposal_hash(p)
        d = GateDecision(
            proposal_hash=ph, passed=False, violations=["max_loss: too big"], token=None
        )
        view = render_card(p, d, proposal_hash=ph, actionable=False, note="gate failed")
        assert all(b["type"] != "actions" for b in view.blocks)
        text = _text(view.blocks)
        assert "*Gate violations*" in text and "• max_loss: too big" in text
        assert "gate failed" in text
        assert "gate FAIL" in view.text

    def test_persona_text_is_escaped(self, conn: sqlite3.Connection) -> None:
        p = _proposal(conn).model_copy(update={"thesis": "buy <!channel> & <http://x|y>"})
        view = render_card(p, None, proposal_hash="0" * 64, actionable=False)
        text = _text(view.blocks)
        assert "<!channel>" not in text and "&lt;!channel&gt; &amp;" in text

    def test_long_text_clipped_under_slack_limit(self, conn: sqlite3.Connection) -> None:
        p = _proposal(conn).model_copy(update={"thesis": "x" * 5000, "risk_narrative": "y" * 5000})
        view = render_card(p, None, proposal_hash="0" * 64, actionable=False)
        for b in view.blocks:
            if b["type"] == "section" and "text" in b:
                assert len(b["text"]["text"]) <= 3000


# ---------------------------------------------------------------------------
# Publish
# ---------------------------------------------------------------------------


class TestPublish:
    def test_posts_actionable_card_in_day_thread(
        self, svc: ApprovalService, conn: sqlite3.Connection, poster: RecordingPoster
    ) -> None:
        rep = svc.publish_pending(NOW)
        ph = _phash(conn)
        assert rep.published == [ph]
        day, view = poster.posted[0]
        assert day == _dt.date(2026, 9, 25)
        assert any(b["type"] == "actions" for b in view.blocks)
        row = conn.execute("SELECT * FROM approval_requests").fetchone()
        assert (row["status"], row["channel"], row["thread_ts"], row["message_ts"]) == (
            "pending",
            "C_INV",
            "100.1",
            "200.1",
        )
        assert _approvals(conn) == []

    def test_publish_is_idempotent(self, svc: ApprovalService, poster: RecordingPoster) -> None:
        svc.publish_pending(NOW)
        assert svc.publish_pending(NOW).published == []
        assert len(poster.posted) == 1

    def test_no_token_is_informational(
        self, _pipeline_db: bytes, arc_settings: ArcSettings, poster: RecordingPoster
    ) -> None:
        conn = _db(_pipeline_db, token=None)
        ApprovalService(conn, arc_settings, poster).publish_pending(NOW)
        ph = _phash(conn)
        assert _status(conn, ph) == "not_actionable"
        assert all(b["type"] != "actions" for b in poster.posted[0][1].blocks)
        res = ApprovalService(conn, arc_settings, poster).decide(
            ph, user=OWNER, approve=True, now=NOW
        )
        assert res.outcome is Outcome.NOT_ACTIONABLE
        assert _approvals(conn) == []

    @pytest.mark.parametrize(
        ("live", "note"),
        [(False, "dry run / fixtures"), (True, "ARC_GATE_SECRET missing")],
    )
    def test_no_token_note_says_why(
        self,
        _pipeline_db: bytes,
        arc_settings: ArcSettings,
        poster: RecordingPoster,
        live: bool,
        note: str,
    ) -> None:
        """E5.2b: a live sweep never labels a token-less PASS as a dry run."""
        conn = _db(_pipeline_db, token=None)
        ApprovalService(conn, arc_settings, poster, live=live).publish_pending(NOW)
        row = conn.execute("SELECT reason FROM approval_requests").fetchone()
        assert note in row[0]
        other = "ARC_GATE_SECRET" if not live else "dry run"
        assert other not in row[0]

    def test_gate_fail_is_informational_and_logged(
        self, conn: sqlite3.Connection, svc: ApprovalService
    ) -> None:
        conn.execute(
            "UPDATE gate_decisions SET passed = 0, token = NULL, violations_json = ?",
            (json.dumps(["halt: trading halted"]),),
        )
        conn.commit()
        with structlog.testing.capture_logs() as logs:
            svc.publish_pending(NOW)
        ph = _phash(conn)
        row = conn.execute("SELECT * FROM approval_requests").fetchone()
        assert row["status"] == "not_actionable"
        assert row["reason"] == "gate failed: halt: trading halted"
        rejected = [e for e in logs if e["event"] == "approvals.rejected"]
        assert rejected and rejected[0]["proposal_hash"] == ph
        assert "halt" in rejected[0]["reason"]

    def test_missing_gate_decision(self, conn: sqlite3.Connection, svc: ApprovalService) -> None:
        conn.execute("DELETE FROM gate_decisions")
        conn.commit()
        svc.publish_pending(NOW)
        assert _status(conn, _phash(conn)) == "not_actionable"

    def test_already_expired_before_post(self, svc: ApprovalService, conn) -> None:  # noqa: ANN001
        svc.publish_pending(NOW + TTL)
        assert _status(conn, _phash(conn)) == "not_actionable"

    def test_tampered_proposal_is_not_published(
        self, conn: sqlite3.Connection, svc: ApprovalService, poster: RecordingPoster
    ) -> None:
        raw = json.loads(
            conn.execute("SELECT payload FROM context_entries WHERE kind='proposal'").fetchone()[0]
        )
        raw["sizing"]["contracts"] = 99
        conn.execute("DROP TRIGGER IF EXISTS context_entries_no_update")
        for (name,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='context_entries'"
        ).fetchall():
            conn.execute(f"DROP TRIGGER {name}")
        conn.execute(
            "UPDATE context_entries SET payload = ? WHERE kind = 'proposal'", (json.dumps(raw),)
        )
        conn.commit()
        with structlog.testing.capture_logs() as logs:
            rep = svc.publish_pending(NOW)
        assert rep.published == [] and poster.posted == []
        assert any(e["event"] == "approvals.unpublishable" for e in logs)

    def test_slack_failure_keeps_request(
        self, conn: sqlite3.Connection, arc_settings: ArcSettings
    ) -> None:
        class Broken(LogCardPoster):
            def post(self, day: _dt.date, view: Any) -> PostedCard:
                raise RuntimeError("slack down")

        rep = ApprovalService(conn, arc_settings, Broken()).publish_pending(NOW)
        assert rep.published == [_phash(conn)]
        assert _status(conn, _phash(conn)) == "pending"  # the TTL still applies

    def test_day_filter(self, svc: ApprovalService) -> None:
        assert svc.publish_pending(NOW, day="2026-01-01").published == []
        assert len(svc.publish_pending(NOW, day="2026-09-25").published) == 1


# ---------------------------------------------------------------------------
# Decide
# ---------------------------------------------------------------------------


class TestDecide:
    def test_owner_approves(
        self, svc: ApprovalService, conn: sqlite3.Connection, poster: RecordingPoster
    ) -> None:
        svc.publish_pending(NOW)
        ph = _phash(conn)
        at = NOW + _dt.timedelta(minutes=5)
        with structlog.testing.capture_logs() as logs:
            res = svc.decide(ph, user=OWNER, approve=True, now=at, slack_ts="200.1")
        assert res.outcome is Outcome.APPROVED and res.accepted
        rec = approval_record(conn, ph)
        assert rec is not None
        assert (rec.proposal_hash, rec.slack_user, rec.slack_ts, rec.decision) == (
            ph,
            OWNER,
            "200.1",
            ApprovalDecision.APPROVED,
        )
        assert rec.at == at
        assert _status(conn, ph) == "approved"
        # E6.2 hand-off: an `approval` routine event for the Investor trigger.
        events = RoutineEventRepo(conn).pending(until=at)
        assert [(e.name, e.payload["proposal_hash"]) for e in events] == [("approval", ph)]
        # The card was rewritten without buttons.
        ch, ts, view = poster.updated[-1]
        assert (ch, ts) == ("C_INV", "200.1")
        assert all(b["type"] != "actions" for b in view.blocks)
        assert "Approved* by <@U0C5KUMH28G>" in _text(view.blocks)
        assert any(e["event"] == "approvals.approved" for e in logs)

    def test_owner_rejects_logged_with_reason(
        self, svc: ApprovalService, conn: sqlite3.Connection
    ) -> None:
        svc.publish_pending(NOW)
        ph = _phash(conn)
        with structlog.testing.capture_logs() as logs:
            res = svc.decide(ph, user=OWNER, approve=False, now=NOW)
        assert res.outcome is Outcome.REJECTED
        rec = approval_record(conn, ph)
        assert rec is not None and rec.decision is ApprovalDecision.REJECTED
        row = conn.execute("SELECT * FROM approval_requests").fetchone()
        assert row["reason"] == f"rejected by <@{OWNER}>"
        ev = next(e for e in logs if e["event"] == "approvals.rejected")
        assert ev["reason"] == f"rejected by <@{OWNER}>" and ev["by"] == OWNER
        assert RoutineEventRepo(conn).pending(until=NOW + TTL) == []

    @pytest.mark.parametrize("user", [STRANGER, ""])
    def test_only_allowed_approvers(
        self, svc: ApprovalService, conn: sqlite3.Connection, poster: RecordingPoster, user: str
    ) -> None:
        svc.publish_pending(NOW)
        ph = _phash(conn)
        with structlog.testing.capture_logs() as logs:
            res = svc.decide(ph, user=user, approve=True, now=NOW)
        assert res.outcome is Outcome.UNAUTHORIZED and not res.accepted
        assert _approvals(conn) == [] and _status(conn, ph) == "pending"
        assert any(e["event"] == "approvals.unauthorized" for e in logs)
        assert [u for u, _ in poster.notices] == ([user] if user else [])
        # The owner can still decide afterwards.
        assert svc.decide(ph, user=OWNER, approve=True, now=NOW).outcome is Outcome.APPROVED

    def test_configured_approver_list(
        self, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ARC_APPROVER_SLACK_USER_IDS", f"{STRANGER}, U0OTHER")
        s = ArcSettings(_env_file=None, account_profile="margin")  # type: ignore[call-arg]
        assert s.approver_slack_user_ids == [STRANGER, "U0OTHER"]
        svc = ApprovalService(conn, s, RecordingPoster())
        svc.publish_pending(NOW)
        ph = _phash(conn)
        assert svc.decide(ph, user=OWNER, approve=True, now=NOW).outcome is Outcome.UNAUTHORIZED
        assert svc.decide(ph, user=STRANGER, approve=True, now=NOW).outcome is Outcome.APPROVED

    def test_resolves_once(self, svc: ApprovalService, conn: sqlite3.Connection) -> None:
        svc.publish_pending(NOW)
        ph = _phash(conn)
        assert svc.decide(ph, user=OWNER, approve=True, now=NOW).outcome is Outcome.APPROVED
        again = svc.decide(ph, user=OWNER, approve=False, now=NOW)
        assert again.outcome is Outcome.ALREADY_DECIDED
        assert again.status is RequestStatus.APPROVED
        assert len(_approvals(conn)) == 1

    def test_unique_index_backs_resolve_once(self, svc: ApprovalService, conn) -> None:  # noqa: ANN001
        svc.publish_pending(NOW)
        ph = _phash(conn)
        conn.execute(
            """INSERT INTO approvals (id, proposal_hash, slack_user, slack_ts, decision,
               decided_at) VALUES ('x', ?, 'U', '', 'approved', '2026-09-25T20:00:00.000000Z')""",
            (ph,),
        )
        conn.commit()
        res = svc.decide(ph, user=OWNER, approve=False, now=NOW)
        assert res.outcome is Outcome.ALREADY_DECIDED
        assert _status(conn, ph) == "pending"  # the failed transaction rolled back

    def test_late_click_expires(
        self, svc: ApprovalService, conn: sqlite3.Connection, poster: RecordingPoster
    ) -> None:
        svc.publish_pending(NOW)
        ph = _phash(conn)
        res = svc.decide(ph, user=OWNER, approve=True, now=NOW + TTL)
        assert res.outcome is Outcome.EXPIRED and not res.accepted
        rec = approval_record(conn, ph)
        assert rec is not None and rec.decision is ApprovalDecision.EXPIRED
        assert rec.slack_user == TTL_ACTOR
        assert poster.notices and "expired" in poster.notices[-1][1]

    def test_unknown_proposal(self, svc: ApprovalService) -> None:
        assert svc.decide("f" * 64, user=OWNER, approve=True, now=NOW).outcome is Outcome.UNKNOWN

    def test_naive_now_rejected(self, svc: ApprovalService) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            svc.decide("f" * 64, user=OWNER, approve=True, now=_dt.datetime(2026, 9, 25, 16))

    def test_card_update_failure_keeps_decision(
        self, conn: sqlite3.Connection, arc_settings: ArcSettings
    ) -> None:
        class NoUpdate(RecordingPoster):
            def update(self, channel: str, message_ts: str, view: Any) -> None:
                raise RuntimeError("slack down")

            def notify_user(
                self, channel: str, user: str, text: str, thread_ts: str | None
            ) -> None:
                raise RuntimeError("slack down")

        svc = ApprovalService(conn, arc_settings, NoUpdate())
        svc.publish_pending(NOW)
        ph = _phash(conn)
        assert svc.decide(ph, user=OWNER, approve=True, now=NOW).outcome is Outcome.APPROVED
        assert svc.decide(ph, user=STRANGER, approve=True, now=NOW).outcome is Outcome.UNAUTHORIZED


# ---------------------------------------------------------------------------
# TTL
# ---------------------------------------------------------------------------


class TestTTL:
    def test_expire_due(
        self, svc: ApprovalService, conn: sqlite3.Connection, poster: RecordingPoster
    ) -> None:
        svc.publish_pending(NOW)
        ph = _phash(conn)
        assert svc.expire_due(NOW + TTL - _dt.timedelta(seconds=1)) == []
        with structlog.testing.capture_logs() as logs:
            assert svc.expire_due(NOW + TTL) == [ph]
        rec = approval_record(conn, ph)
        assert rec is not None and rec.decision is ApprovalDecision.EXPIRED
        row = conn.execute("SELECT * FROM approval_requests").fetchone()
        assert row["status"] == "expired" and row["decided_by"] == TTL_ACTOR
        assert row["reason"] == "TTL expired at 16:20 ET with no decision"
        ev = next(e for e in logs if e["event"] == "approvals.expired")
        assert ev["reason"] == row["reason"]
        assert "Expired" in _text(poster.updated[-1][2].blocks)
        assert svc.expire_due(NOW + 2 * TTL) == []  # once
        assert svc.decide(ph, user=OWNER, approve=True, now=NOW).outcome is (
            Outcome.ALREADY_DECIDED
        )

    def test_ttl_comes_from_config(self, _pipeline_db: bytes, monkeypatch) -> None:  # noqa: ANN001
        monkeypatch.setenv("ARC_APPROVAL_TTL_SECONDS", "300")
        s = ArcSettings(_env_file=None, account_profile="margin")  # type: ignore[call-arg]
        conn, _ = fixture_run(s, load_routines())
        conn.execute("UPDATE gate_decisions SET token = 'tok'")
        conn.commit()
        svc = ApprovalService(conn, s, RecordingPoster())
        svc.publish_pending(FIXTURE_NOW)
        ph = _phash(conn)
        assert svc.expire_due(FIXTURE_NOW + _dt.timedelta(minutes=4, seconds=59)) == []
        assert svc.expire_due(FIXTURE_NOW + _dt.timedelta(minutes=5)) == [ph]

    def test_sweep_publishes_then_expires(self, svc: ApprovalService, conn) -> None:  # noqa: ANN001
        rep = svc.sweep(NOW)
        assert len(rep.published) == 1 and rep.expired == []
        rep = svc.sweep(NOW + TTL)
        assert rep.published == [] and rep.expired == [_phash(conn)]

    @settings(max_examples=40, deadline=None)
    @given(offset=st.integers(min_value=-60, max_value=3 * 1200))
    def test_click_accepted_iff_before_expiry(self, _pipeline_db: bytes, offset: int) -> None:
        conn = _db(_pipeline_db)
        s = ArcSettings(_env_file=None, account_profile="margin")  # type: ignore[call-arg]
        svc = ApprovalService(conn, s, LogCardPoster())
        svc.publish_pending(NOW)
        ph = _phash(conn)
        at = NOW + _dt.timedelta(seconds=offset)
        res = svc.decide(ph, user=OWNER, approve=True, now=at)
        rec = approval_record(conn, ph)
        assert rec is not None
        if at < NOW + TTL:
            assert res.outcome is Outcome.APPROVED and rec.decision is ApprovalDecision.APPROVED
            assert rec.at < _proposal(conn).expires_at  # what submit() requires
        else:
            assert res.outcome is Outcome.EXPIRED and rec.decision is ApprovalDecision.EXPIRED


# ---------------------------------------------------------------------------
# Auto-approve (D10) and the execution hand-off
# ---------------------------------------------------------------------------


class TestAutoApprove:
    def test_paper_auto_approve(
        self, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ARC_AUTO_APPROVE", "true")
        s = ArcSettings(_env_file=None, account_profile="margin")  # type: ignore[call-arg]
        rep = ApprovalService(conn, s, RecordingPoster()).publish_pending(NOW)
        ph = _phash(conn)
        assert rep.auto_approved == [ph]
        rec = approval_record(conn, ph)
        assert rec is not None and rec.decision is ApprovalDecision.APPROVED
        assert rec.slack_user == AUTO_APPROVER

    def test_not_for_informational_cards(self, _pipeline_db: bytes, monkeypatch) -> None:  # noqa: ANN001
        monkeypatch.setenv("ARC_AUTO_APPROVE", "true")
        conn = _db(_pipeline_db, token=None)
        s = ArcSettings(_env_file=None, account_profile="margin")  # type: ignore[call-arg]
        assert ApprovalService(conn, s, RecordingPoster()).publish_pending(NOW).auto_approved == []
        assert _approvals(conn) == []

    def test_off_by_default(self, svc: ApprovalService, conn: sqlite3.Connection) -> None:
        assert svc.publish_pending(NOW).auto_approved == []
        assert _approvals(conn) == []


def test_approval_record_satisfies_submit(
    svc: ApprovalService, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ApprovalRecord written here is what `arc.execution.submit()` accepts (E6.2)."""
    from arc.execution.submission import RefusalCode, SubmitRefused, _check
    from arc.gate.token import issue_token

    secret = "s" * 40
    monkeypatch.setenv("ARC_GATE_SECRET", secret)
    s = ArcSettings(_env_file=None, account_profile="margin")  # type: ignore[call-arg]
    p = _proposal(conn)
    ph = proposal_hash(p)
    decision = issue_token(
        GateDecision(proposal_hash=ph, passed=True, token=None), p, secret=secret.encode(), now=NOW
    )
    tok = decision.token
    assert tok
    svc.publish_pending(NOW)
    at = NOW + _dt.timedelta(minutes=2)
    svc.decide(ph, user=OWNER, approve=True, now=at)
    from arc.gate.halt import HaltSwitch
    from arc.store.repos import HaltRepo

    kw = {"halt": HaltSwitch(HaltRepo(conn)), "step": 0, "limit_price": None}
    order = _check(p, decision, approval_record(conn, ph), s, at, **kw)
    assert order.client_order_id == tok

    conn2_ph = ph  # a rejection must be refused by submit()
    conn.execute("UPDATE approvals SET decision = 'rejected' WHERE proposal_hash = ?", (conn2_ph,))
    with pytest.raises(SubmitRefused) as exc:
        _check(p, decision, approval_record(conn, ph), s, at, **kw)
    assert exc.value.code is RefusalCode.NOT_APPROVED


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


class TestCLI:
    def _file_db(self, raw: bytes, path: Path, now: _dt.datetime) -> str:
        conn = _db(raw)
        disk = sqlite3.connect(path)
        conn.backup(disk)
        disk.close()
        conn.close()
        return str(path)

    def test_sweep_list_decide(
        self,
        _pipeline_db: bytes,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        from arc.cli import main

        db = self._file_db(_pipeline_db, tmp_path / "arc.db", NOW)
        monkeypatch.setattr("arc.utils.calendar.now_et", lambda: NOW)
        monkeypatch.setattr(
            "arc.approvals.cli.make_service",
            _patched_make_service(),
        )
        assert main(["approve", "sweep", "--db", db]) == 0
        out = json.loads(capsys.readouterr().out)
        assert len(out["published"]) == 1
        ph = out["published"][0]

        assert main(["approve", "list", "--db", db]) == 0
        rows = json.loads(capsys.readouterr().out)
        assert rows[0]["status"] == "pending" and "proposal_json" not in rows[0]

        rc = main(["approve", "decide", "--db", db, "--proposal", ph, "--user", STRANGER,
                   "--approve"])  # fmt: skip
        assert rc == 1
        assert json.loads(capsys.readouterr().out)["outcome"] == "unauthorized"

        rc = main(["approve", "decide", "--db", db, "--proposal", ph, "--user", OWNER,
                   "--approve", "--slack-ts", "9.9"])  # fmt: skip
        assert rc == 0
        assert json.loads(capsys.readouterr().out)["outcome"] == "approved"
        conn = sqlite3.connect(db)
        assert conn.execute("SELECT decision, slack_ts FROM approvals").fetchall() == [
            ("approved", "9.9")
        ]

    def test_sweep_no_slack_only_expires(
        self,
        _pipeline_db: bytes,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        from arc.cli import main

        db = self._file_db(_pipeline_db, tmp_path / "arc.db", NOW)
        monkeypatch.setattr("arc.utils.calendar.now_et", lambda: NOW)
        assert main(["approve", "sweep", "--no-slack", "--db", db]) == 0
        assert json.loads(capsys.readouterr().out)["published"] == []

    def test_propose_fixtures_logs_card(self, capsys: pytest.CaptureFixture[str]) -> None:
        from arc.cli import main

        with structlog.testing.capture_logs() as logs:
            assert main(["propose", "--fixtures", "--json", "--profile", "margin"]) == 0
        out = json.loads(capsys.readouterr().out)
        assert len(out["approvals"]["published"]) == 1
        pub = next(e for e in logs if e["event"] == "approvals.published")
        assert pub["status"] == "not_actionable"  # fixtures mint no token: never actionable
        assert any(e["event"] == "approvals.card" for e in logs)

    def test_help_lists_approve(self) -> None:
        from arc.cli import main

        with pytest.raises(SystemExit) as exc:
            main(["approve", "--help"])
        assert exc.value.code == 0


def _patched_make_service():  # noqa: ANN202
    from arc.approvals import cli as approvals_cli

    real = approvals_cli.make_service

    def make(conn, settings, *, slack, poster=None):  # noqa: ANN001, ANN202
        return real(conn, settings, slack=False, poster=RecordingPoster())

    return make


def test_routines_tick_runs_sweep(
    _pipeline_db: bytes, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from arc.routines import cli as rcli

    conn = _db(_pipeline_db)
    calls: list[str] = []

    class Svc:
        def sweep(self, now: _dt.datetime):  # noqa: ANN202
            calls.append("sweep")
            from arc.approvals.service import SweepReport

            return SweepReport(["a"], [], [])

        def expire_due(self, now: _dt.datetime) -> list[str]:
            calls.append("expire")
            return []

    monkeypatch.setattr("arc.approvals.cli.make_service", lambda *a, **k: Svc())
    import argparse

    assert rcli._approval_sweep(argparse.Namespace(no_slack=False), conn, NOW) == {
        "published": ["a"],
        "auto_approved": [],
        "expired": [],
    }
    assert rcli._approval_sweep(argparse.Namespace(no_slack=True), conn, NOW) == {
        "published": [],
        "auto_approved": [],
        "expired": [],
    }
    assert calls == ["sweep", "expire"]

    def boom(*a: Any, **k: Any) -> None:
        raise RuntimeError("db locked")

    monkeypatch.setattr("arc.approvals.cli.make_service", boom)
    assert rcli._approval_sweep(argparse.Namespace(no_slack=False), conn, NOW) is None


# ---------------------------------------------------------------------------
# Slack poster (no network: fake WebClient)
# ---------------------------------------------------------------------------


class FakeWeb:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def chat_postMessage(self, **kw: Any) -> dict[str, Any]:  # noqa: N802
        self.calls.append(("post", kw))
        return {"ts": f"{len(self.calls)}.0", "channel": kw["channel"]}

    def chat_update(self, **kw: Any) -> dict[str, Any]:  # noqa: N802
        self.calls.append(("update", kw))
        return {"ok": True}

    def chat_postEphemeral(self, **kw: Any) -> dict[str, Any]:  # noqa: N802
        self.calls.append(("ephemeral", kw))
        return {"ok": True}


def test_slack_poster_uses_shared_day_thread(
    conn: sqlite3.Connection, arc_settings: ArcSettings
) -> None:
    from arc.approvals.slack import SlackCardPoster
    from arc.routines.heartbeat import SlackDayThreadNotifier
    from arc.slack.client import CHANNEL_ARC_INVESTOR, ArcSlackClient

    web = FakeWeb()
    client = ArcSlackClient(client=web)  # type: ignore[arg-type]
    SlackDayThreadNotifier(conn, client).post(_dt.date(2026, 9, 25), "[Scout] hello")
    svc = ApprovalService(conn, arc_settings, SlackCardPoster(conn, client))
    svc.publish_pending(NOW)
    kinds = [k for k, _ in web.calls]
    assert kinds == ["post", "post", "post"]  # day root, heartbeat, card — one root only
    root_ts = "1.0"
    card = web.calls[2][1]
    assert card["channel"] == CHANNEL_ARC_INVESTOR and card["thread_ts"] == root_ts
    assert any(b["type"] == "actions" for b in card["blocks"])

    ph = _phash(conn)
    svc.decide(ph, user=STRANGER, approve=True, now=NOW)
    assert web.calls[-1][0] == "ephemeral" and web.calls[-1][1]["user"] == STRANGER
    svc.decide(ph, user=OWNER, approve=False, now=NOW)
    kind, kw = web.calls[-1]
    assert kind == "update" and kw["ts"] == "3.0"
    assert "Rejected" in kw["text"]


# ---------------------------------------------------------------------------
# Hermes plugin
# ---------------------------------------------------------------------------

PLUGIN = Path(__file__).resolve().parent.parent / "hermes/plugins/arc-approvals/__init__.py"


def _load_plugin() -> Any:
    spec = importlib.util.spec_from_file_location("arc_approvals_plugin", PLUGIN)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


plugin = _load_plugin()
H = "a" * 64


def _body(user: str = OWNER) -> dict[str, Any]:
    return {"user": {"id": user, "name": "mohit"}, "container": {"message_ts": "5.5"}}


@pytest.mark.parametrize(
    ("body", "action", "expected"),
    [
        (_body(), {"action_id": "arc_approve", "value": H}, (H, True, OWNER, "5.5")),
        (_body(), {"action_id": "arc_reject", "value": H}, (H, False, OWNER, "5.5")),
        (_body(), {"action_id": "hermes_approve_once", "value": H}, None),
        (_body(), {"action_id": "arc_approve", "value": "not-a-hash"}, None),
        ({"user": {}}, {"action_id": "arc_approve", "value": H}, None),
        ({}, {}, None),
    ],
)
def test_plugin_parse_click(body: dict, action: dict, expected: tuple | None) -> None:
    assert plugin.parse_click(body, action) == expected


def test_plugin_registers_both_actions() -> None:
    got: list[str] = []

    class Ctx:
        def register_slack_action_handler(self, action_id: str, cb: Any) -> None:
            got.append(action_id)

    plugin.register(Ctx())
    assert got == ["arc_approve", "arc_reject"]


def test_plugin_runs_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []

    def fake_run(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        seen.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout='{"outcome": "approved"}', stderr="")

    monkeypatch.setattr(plugin.subprocess, "run", fake_run)
    assert plugin.run_decide(H, True, OWNER, "5.5") == {"outcome": "approved"}
    assert seen[0][1:] == [
        "approve", "decide", "--proposal", H, "--user", OWNER, "--approve", "--slack-ts", "5.5",
    ]  # fmt: skip
    plugin.run_decide(H, False, OWNER, "")
    assert "--reject" in seen[1] and "--slack-ts" not in seen[1]


def test_plugin_cli_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    def bad_json(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 2, stdout="", stderr="boom")

    monkeypatch.setattr(plugin.subprocess, "run", bad_json)
    assert plugin.run_decide(H, True, OWNER, "")["outcome"] == "error"

    def timeout(cmd: list[str], **kw: Any) -> None:
        raise subprocess.TimeoutExpired(cmd, 30)

    monkeypatch.setattr(plugin.subprocess, "run", timeout)
    assert plugin.run_decide(H, True, OWNER, "")["outcome"] == "error"


def test_plugin_on_action_acks_and_dispatches(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    acks: list[int] = []
    runs: list[tuple] = []

    async def ack() -> None:
        acks.append(1)

    monkeypatch.setattr(plugin, "run_decide", lambda *a: runs.append(a) or {})
    asyncio.run(plugin.on_action(ack, _body(), {"action_id": "arc_approve", "value": H}))
    asyncio.run(plugin.on_action(ack, _body(), {"action_id": "arc_approve", "value": "x"}))
    assert acks == [1, 1]
    assert runs == [(H, True, OWNER, "5.5")]
