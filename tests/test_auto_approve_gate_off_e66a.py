"""E6.6a: auto-approve with the scorecard gate off is explicit, bounded and auditable.

- every AUTO_APPROVE journal row carries the readiness snapshot, gate on or off;
- the weekly scorecard header / funnel row, the tower Ops payload and
  ``arc approve auto status`` show the same gate line for the same ``now``;
- the card marker reads ``Auto-approved (paper, gate off)``;
- the approvals sweep turns the gate back on exactly once at n closed trades.

Fixtures (closed trade history, the fixture pipeline DB) are shared with the E7.5a
tests in :mod:`tests.test_auto_approve_scorecard_gate`.
"""

from __future__ import annotations

import datetime as _dt
import json
import sqlite3
from typing import Any

import pytest

from arc.approvals.auto import scorecard_readiness
from arc.approvals.service import GATE_REENABLE_ACTOR, ApprovalService, LogCardPoster
from arc.control.effective import effective_settings
from arc.control.service import LOCAL_ACTOR, ControlService
from arc.journal.scorecard import (
    auto_approve_gate,
    auto_approve_readiness,
    build_scorecard,
    render_markdown,
)
from arc.journal.views import explain, explain_lines
from arc.slack.scorecard import scorecard_card
from tests import test_auto_approve_scorecard_gate as e75a
from tests.test_auto_approve_scorecard_gate import (
    NOW,
    _phash,
    add_closed_trade,
    history,
    settings,
)

KEY = "auto_approve.scorecard_gate"

# Shared E7.5a fixtures: the fixture-pipeline DB (module scope), a fresh copy per test,
# and no ARC_AUTO_APPROVE* env leaking in.
_pipeline_db = pytest.fixture(scope="module")(e75a._pipeline_db.__wrapped__)
conn = pytest.fixture(e75a.conn.__wrapped__)
_no_env = pytest.fixture(autouse=True)(e75a._no_env.__wrapped__)


def auto_rows(c: sqlite3.Connection) -> list[dict[str, Any]]:
    return [
        {**dict(r), "payload": json.loads(r["payload"])}
        for r in c.execute("SELECT * FROM decisions WHERE reason_code = 'auto_approve'")
    ]


def gate_changes(c: sqlite3.Connection) -> list[sqlite3.Row]:
    return c.execute("SELECT * FROM config_changes WHERE key = ? ORDER BY id", (KEY,)).fetchall()


def gate_off_store(c: sqlite3.Connection) -> ControlService:
    """The owner's change #2: gate turned off through the control service (confirmed)."""
    svc = ControlService(c, base=settings(), now=lambda: NOW)
    res = svc.set(KEY, "off", actor=LOCAL_ACTOR, source="cli", reason="collection phase")
    assert res.outcome == "pending" and res.pending is not None
    assert svc.confirm(res.pending.code, actor=LOCAL_ACTOR, source="cli").outcome == "applied"
    return svc


def sweep(c: sqlite3.Connection) -> tuple[Any, LogCardPoster, ApprovalService]:
    poster = LogCardPoster()
    svc = ApprovalService(c, effective_settings(c, base=settings()), poster)
    return svc.publish_pending(NOW), poster, svc


# ---------------------------------------------------------------------------
# 2. readiness journaled on every auto-approval
# ---------------------------------------------------------------------------


def test_readiness_payload_on_auto_approval_with_gate_off(conn: sqlite3.Connection) -> None:
    history(conn, 3, pnl_per_contract=-50.0)
    rep, poster, _ = publish_off(conn)
    ph = _phash(conn)
    assert rep.auto_approved == [ph]  # no behaviour change: still approved
    (row,) = auto_rows(conn)
    p = row["payload"]
    assert p["scorecard_gate"] == "off" and p["scorecard_gate_applies"] is True
    assert p["failing"] == ["min_closed_trades", "negative_realised_ev"]
    assert p["closed_trades"] == 3 and p["min_closed_trades"] == 30
    assert p["realised_net_ev"] == pytest.approx(-50.0 - 2 * 0.18297, abs=1e-3)
    assert {"realised_slippage", "half_spread", "env", "approval_id"} <= set(p)
    # card marker
    assert "Auto-approved (paper, gate off)" in json.dumps(poster.posted[0][1].blocks)
    # `arc journal explain` shows what the gate would have said
    text = "\n".join(explain_lines(explain(conn, ph)))
    assert "scorecard gate off · closed 3/30" in text
    assert "failing min_closed_trades, negative_realised_ev" in text


def publish_off(c: sqlite3.Connection) -> tuple[Any, LogCardPoster, ApprovalService]:
    poster = LogCardPoster()
    svc = ApprovalService(c, settings(auto_approve_scorecard_gate=False), poster)
    return svc.publish_pending(NOW), poster, svc


def test_readiness_payload_on_auto_approval_with_gate_met(conn: sqlite3.Connection) -> None:
    history(conn, 30, pnl_per_contract=50.0)
    poster = LogCardPoster()
    rep = ApprovalService(conn, settings(), poster).publish_pending(NOW)
    assert rep.auto_approved == [_phash(conn)]
    (row,) = auto_rows(conn)
    p = row["payload"]
    assert p["scorecard_gate"] == "met" and p["failing"] == []
    assert p["closed_trades"] == 30 and p["realised_net_ev"] > 0
    text = json.dumps(poster.posted[0][1].blocks)
    assert "Auto-approved (paper)" in text and "gate off" not in text


def test_close_snapshot_says_not_gated(conn: sqlite3.Connection) -> None:
    conn.execute("UPDATE proposals SET kind = 'close' WHERE day IS NOT NULL")
    conn.commit()
    rep, poster, _ = publish_off(conn)
    assert rep.auto_approved == [_phash(conn)]
    (row,) = auto_rows(conn)
    assert row["payload"]["scorecard_gate_applies"] is False
    assert "gate off" not in json.dumps(poster.posted[0][1].blocks)  # closes are never gated


def test_resolved_card_keeps_the_gate_off_marker() -> None:
    from arc.approvals.service import AUTO_APPROVER, RequestStatus, _outcome_text

    assert "(paper, gate off)" in _outcome_text(
        RequestStatus.APPROVED, AUTO_APPROVER, "paper", gate_off=True
    )
    assert "(paper)*" in _outcome_text(RequestStatus.APPROVED, AUTO_APPROVER, "paper")


# ---------------------------------------------------------------------------
# 3. gate line in the scorecard, the tower and `arc approve auto status`
# ---------------------------------------------------------------------------


def test_gate_line_off_and_met() -> None:
    from arc.journal.scorecard import AutoApproveReadiness

    r = AutoApproveReadiness(
        ok=False,
        failing=["min_closed_trades"],
        closed_trades=3,
        min_closed_trades=30,
        window_trades=3,
        realised_net_ev=-154.95,
        slippage_tolerance=1.5,
    )
    assert r.gate_line(False) == (
        "scorecard gate: OFF (opt-out) — 3 closed trades < 30 required; "
        "realised net EV -$154.95/trade over 3 trades"
    )
    assert r.gate_line(True).startswith("scorecard gate: holding opens — 3 closed trades")
    ok = r.model_copy(
        update={
            "ok": True,
            "failing": [],
            "closed_trades": 30,
            "realised_net_ev": 12.0,
            "realised_slippage": 1.0,
            "half_spread": 2.0,
        }
    )
    assert ok.gate_line(True) == (
        "scorecard gate: met — 30 closed trades, net EV +$12.00/trade, slippage +$1.00 <= $3.00"
    )


@pytest.mark.parametrize("gate_on", [False, True])
def test_scorecard_header_renders_gate_line(conn: sqlite3.Connection, gate_on: bool) -> None:
    history(conn, 30 if gate_on else 3, pnl_per_contract=50.0)
    s = settings(auto_approve_scorecard_gate=gate_on)
    g = auto_approve_gate(conn, s, now=NOW)
    sc = build_scorecard(conn, start=NOW - _dt.timedelta(days=90), end=NOW, now=NOW, auto_approve=g)
    md = render_markdown(sc)
    head = md.split("## Funnel")[0]
    want = "scorecard gate: met —" if gate_on else "scorecard gate: OFF (opt-out) — 3 closed"
    assert want in head
    approved = next(line for line in md.splitlines() if line.startswith("| Approved:"))
    assert want in approved
    card = json.dumps(scorecard_card(sc).blocks)
    assert ("Scorecard gate: met" if gate_on else "Scorecard gate: OFF (opt-out)") in card
    # the model round-trips (the scorecard routine records it as a run input)
    assert sc.model_dump(mode="json")["auto_approve"]["scorecard_gate"] is gate_on


def test_scorecard_without_gate_renders_as_before(conn: sqlite3.Connection) -> None:
    sc = build_scorecard(conn, start=NOW - _dt.timedelta(days=7), end=NOW, now=NOW)
    assert sc.auto_approve is None and "scorecard gate" not in render_markdown(sc)


def test_status_scorecard_and_tower_report_the_same_numbers(conn: sqlite3.Connection) -> None:
    from arc.tower.data_ops import load_config

    history(conn, 5, pnl_per_contract=-20.0)
    gate_off_store(conn)
    eff = effective_settings(conn, base=settings())
    assert eff.auto_approve_scorecard_gate is False
    status = scorecard_readiness(conn, eff, NOW)
    g = auto_approve_gate(conn, eff, now=NOW)
    raw = auto_approve_readiness(conn, now=NOW, min_closed_trades=30, slippage_tolerance=1.5)
    assert status["closed_trades"] == g.readiness.closed_trades == raw.closed_trades == 5
    assert status["realised_net_ev"] == g.readiness.realised_net_ev == raw.realised_net_ev
    assert status["failing"] == g.readiness.failing == raw.failing
    assert status["gate_line"] == g.line
    assert g.line.startswith("scorecard gate: OFF (opt-out) — 5 closed trades < 30")
    tower = load_config(conn, settings(), now=NOW)
    assert tower.scorecard_gate == g.line


def test_journal_scorecard_cli_reads_effective_gate(
    conn: sqlite3.Connection, tmp_path: Any, capsys: pytest.CaptureFixture[str], monkeypatch: Any
) -> None:
    """`arc journal scorecard` builds the header from the D26 effective switch."""
    from arc.cli import main

    gate_off_store(conn)
    db = tmp_path / "s.db"
    disk = sqlite3.connect(db)
    conn.backup(disk)
    disk.close()
    monkeypatch.setenv("ARC_AUTO_APPROVE_SCORECARD_GATE", "true")  # base on; store says off
    assert main(["journal", "scorecard", "--db", str(db)]) == 0
    out = capsys.readouterr().out
    assert "**scorecard gate: OFF (opt-out) — 0 closed trades < 30 required**" in out


# ---------------------------------------------------------------------------
# 4. automatic re-enable at n closed trades
# ---------------------------------------------------------------------------


def test_reenable_flips_exactly_once_at_n(conn: sqlite3.Connection) -> None:
    history(conn, 30, pnl_per_contract=-10.0)  # n reached; EV negative: gate will hold opens
    gate_off_store(conn)
    rep, poster, svc = sweep(conn)
    rows = gate_changes(conn)
    assert len(rows) == 2  # owner off, then the system on
    last = rows[-1]
    assert last["actor"] == GATE_REENABLE_ACTOR and json.loads(last["new"]) is True
    assert last["reason"] == "E7.5a: collection phase complete (n=30)"
    assert last["source"] == "cli" and last["status"] == "applied"
    assert effective_settings(conn, base=settings()).auto_approve_scorecard_gate is True
    # the same sweep already decides under the gate: negative EV holds the open back
    assert rep.auto_approved == [] and rep.auto_gated == [_phash(conn)]
    assert svc.settings.auto_approve_scorecard_gate is True
    # auto_approve.paper / live untouched
    keys = {r[0] for r in conn.execute("SELECT key FROM config_changes")}
    assert keys == {KEY}
    # idempotent: a second sweep and a direct call do nothing
    _, _, svc2 = sweep(conn)
    assert svc2.reenable_scorecard_gate(NOW) is False
    assert len(gate_changes(conn)) == 2


def test_reenable_noop_below_n(conn: sqlite3.Connection) -> None:
    history(conn, 29, pnl_per_contract=50.0)
    gate_off_store(conn)
    rep, _, svc = sweep(conn)
    assert svc.reenable_scorecard_gate(NOW) is False
    assert len(gate_changes(conn)) == 1
    assert rep.auto_approved == [_phash(conn)]  # still collecting
    (row,) = auto_rows(conn)
    assert row["payload"]["scorecard_gate"] == "off"


def test_reenable_noop_when_gate_already_on(conn: sqlite3.Connection) -> None:
    history(conn, 30, pnl_per_contract=50.0)
    _, _, svc = sweep(conn)
    assert svc.reenable_scorecard_gate(NOW) is False
    assert gate_changes(conn) == []


def test_reenable_never_overrides_a_later_owner_opt_out(conn: sqlite3.Connection) -> None:
    """After the one automatic flip, an owner who turns the gate off again keeps it off."""
    history(conn, 30, pnl_per_contract=50.0)
    gate_off_store(conn)
    sweep(conn)
    assert len(gate_changes(conn)) == 2
    gate_off_store(conn)  # owner opts out again, on purpose
    _, _, svc = sweep(conn)
    assert svc.reenable_scorecard_gate(NOW) is False
    assert len(gate_changes(conn)) == 3
    assert effective_settings(conn, base=settings()).auto_approve_scorecard_gate is False


def test_reenable_counts_trades_closed_before_now_only(conn: sqlite3.Connection) -> None:
    for i in range(30):
        add_closed_trade(conn, i, pnl_per_contract=50.0, closed=NOW + _dt.timedelta(hours=1 + i))
    gate_off_store(conn)
    _, _, svc = sweep(conn)
    assert svc.reenable_scorecard_gate(NOW) is False and len(gate_changes(conn)) == 1


def test_reenable_posts_the_flip_when_live(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    posted: list[str] = []
    import arc.approvals.auto as auto_mod

    monkeypatch.setattr(auto_mod, "post_day_notice", lambda _c, text, _now: posted.append(text))
    history(conn, 30, pnl_per_contract=50.0)
    gate_off_store(conn)
    svc = ApprovalService(
        conn, effective_settings(conn, base=settings()), LogCardPoster(), live=True
    )
    assert svc.reenable_scorecard_gate(NOW) is True
    (text,) = posted
    assert text.startswith("Scorecard gate: ON (auto, collection phase complete: 30 closed")
    assert "scorecard gate: met" in text


def test_system_actor_may_only_make_safer_changes(conn: sqlite3.Connection) -> None:
    svc = ControlService(conn, base=settings(), now=lambda: NOW)
    res = svc.set_system(KEY, "off", actor=GATE_REENABLE_ACTOR, reason="x")
    assert res.outcome == "refused" and "safer" in res.message
    assert svc.set_system(KEY, "on", actor="local", reason="x").outcome == "refused"
    assert svc.set_system(KEY, "on", actor=GATE_REENABLE_ACTOR, reason="x").outcome == ("unchanged")
    assert svc.set_system("nope.key", "on", actor=GATE_REENABLE_ACTOR, reason="x").outcome == (
        "refused"
    )
    assert gate_changes(conn) == []
