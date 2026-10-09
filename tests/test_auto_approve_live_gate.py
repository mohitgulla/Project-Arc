"""E11.3 / D70: live evidence is live-only (scorecard gate, auto-approve, size cap).

Reuses the E7.5a fixture (one gate-passed SPY iron condor proposal) and its
closed-trade history builder from ``tests/test_auto_approve_scorecard_gate.py``.
"""

from __future__ import annotations

import json
import sqlite3
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from arc.approvals.auto import auto_status, live_gate_block
from arc.approvals.service import LIVE_GATE_ACTOR, ApprovalService, LogCardPoster
from arc.config import ArcSettings
from arc.control.effective import effective_settings
from arc.control.service import LOCAL_ACTOR, ControlService
from arc.journal.scorecard import env_readiness, live_gate_met
from arc.store.identity import write_store_env
from tests.test_auto_approve_scorecard_gate import (
    NOW,
    _phash,
    add_closed_trade,
    gated_rows,
    history,
)
from tests.test_auto_approve_scorecard_gate import _pipeline_db as _pipeline_db  # noqa: F401
from tests.test_auto_approve_scorecard_gate import conn as _conn_fixture  # noqa: F401

_FAKE_LIVE = Path(__file__)


@pytest.fixture(autouse=True)
def _no_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # Live settings are re-validated inside the control service; the live-env
    # guard file is faked for the whole test (Phase 1 has no ~/.arc/live.env).
    monkeypatch.setattr("arc.config._LIVE_ENV_PATH", _FAKE_LIVE)
    for k in (
        "ARC_AUTO_APPROVE",
        "ARC_AUTO_APPROVE_SCORECARD_GATE",
        "ARC_AUTO_APPROVE_MIN_CLOSED_TRADES",
        "ARC_AUTO_APPROVE_LIVE_MIN_CLOSED_TRADES",
        "ARC_LIVE_GATE_MET",
        "ARC_DB_PATH",
    ):
        monkeypatch.delenv(k, raising=False)


@pytest.fixture
def conn(_conn_fixture: sqlite3.Connection) -> sqlite3.Connection:  # noqa: F811
    """The E7.5a fixture store, unstamped (fixture_run stamps it paper) so each test
    stamps the env it needs."""
    from arc.store.migrate import MIGRATIONS_DIR

    c = _conn_fixture
    c.execute("DROP TABLE store_identity")  # drops its triggers too
    c.executescript((MIGRATIONS_DIR / "030_store_identity.sql").read_text())
    return c


def live(**kw: Any) -> ArcSettings:
    """Live settings with auto_approve.live on the way the store turns it on."""
    with mock.patch("arc.config._LIVE_ENV_PATH", _FAKE_LIVE):
        s = ArcSettings(  # type: ignore[call-arg]
            _env_file=None, env="live", account_profile="margin", **kw
        )
    return s.model_copy(update={"auto_approve": True})


def _live_base(**kw: Any) -> ArcSettings:
    with mock.patch("arc.config._LIVE_ENV_PATH", _FAKE_LIVE):
        return ArcSettings(  # type: ignore[call-arg]
            _env_file=None, env="live", account_profile="margin", **kw
        )


def eff_live(conn: sqlite3.Connection) -> ArcSettings:
    """``effective_settings`` for a live process (re-validation needs the live guard)."""
    with mock.patch("arc.config._LIVE_ENV_PATH", _FAKE_LIVE):
        return effective_settings(conn, base=_live_base())


def paper(**kw: Any) -> ArcSettings:
    return ArcSettings(  # type: ignore[call-arg]
        _env_file=None, account_profile="margin", auto_approve=True, **kw
    )


def stamp(conn: sqlite3.Connection, env: str) -> None:
    write_store_env(conn, env, NOW)


def publish(conn: sqlite3.Connection, s: ArcSettings) -> tuple[Any, LogCardPoster, Any]:
    poster = LogCardPoster()
    svc = ApprovalService(conn, s, poster)
    return svc.publish_pending(NOW), poster, svc


def gate_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT actor, new FROM config_changes WHERE key = 'live.gate_met' ORDER BY id"
    ).fetchall()


# ---------------------------------------------------------------------------
# Readiness: paper closes never count for live
# ---------------------------------------------------------------------------


def test_paper_closes_never_count_for_live(conn: sqlite3.Connection) -> None:
    paper_store = conn
    stamp(paper_store, "paper")
    history(paper_store, 40, pnl_per_contract=50.0)
    assert env_readiness(paper_store, paper(), now=NOW).ok  # paper qualifies paper
    # The same 40 paper closes, read by a live process (it could only get here by a
    # bug: open_store refuses the store first), never qualify live.
    r = env_readiness(paper_store, live(), now=NOW)
    assert not r.ok and "store_env_mismatch" in r.failing and not live_gate_met(r)

    live_store = sqlite3.connect(":memory:")
    live_store.row_factory = sqlite3.Row
    from arc.store.migrate import migrate

    migrate(live_store)
    stamp(live_store, "live")
    r = env_readiness(live_store, live(), now=NOW)
    assert r.failing == ["min_closed_trades"] and r.env == "live"
    assert r.closed_trades == 0 and r.min_closed_trades == 30
    assert live_gate_met(r) is False


def test_live_uses_its_own_threshold(conn: sqlite3.Connection) -> None:
    stamp(conn, "live")
    history(conn, 12, pnl_per_contract=50.0)
    s = live(auto_approve_min_closed_trades=10)  # the paper key never reaches live
    r = env_readiness(conn, s, now=NOW)
    assert r.min_closed_trades == 30 and r.failing == ["min_closed_trades"]
    r = env_readiness(conn, live(auto_approve_live_min_closed_trades=10), now=NOW)
    assert r.ok and r.env == "live"


def test_readiness_carries_store_env(conn: sqlite3.Connection) -> None:
    assert env_readiness(conn, paper(), now=NOW).env is None  # unstamped fixture store
    stamp(conn, "paper")
    assert env_readiness(conn, paper(), now=NOW).env == "paper"


# ---------------------------------------------------------------------------
# Auto-approve: unconditional live gate, no live auto-approve before the gate
# ---------------------------------------------------------------------------


def test_live_ignores_scorecard_gate_off(conn: sqlite3.Connection) -> None:
    stamp(conn, "live")
    s = live(auto_approve_scorecard_gate=False)
    assert s.auto_approve_scorecard_gate is True  # forced on at validation
    # Even a post-validation copy with the opt-out (a stale cached settings) is ignored.
    s = s.model_copy(update={"auto_approve_scorecard_gate": False})
    rep, poster, _ = publish(conn, s)
    ph = _phash(conn)
    assert rep.auto_approved == [] and rep.auto_gated == [ph]
    (row,) = gated_rows(conn)
    assert row["payload"]["env"] == "live" and "min_closed_trades" in row["payload"]["failing"]
    status = conn.execute(
        "SELECT status FROM approval_requests WHERE proposal_hash = ?", (ph,)
    ).fetchone()[0]
    assert status == "pending"
    text = json.dumps(poster.posted[0][1].blocks)
    assert "Approve" in text and "Auto-approved" not in text


def test_live_store_override_off_cannot_disable_gate(conn: sqlite3.Connection) -> None:
    stamp(conn, "live")
    svc = ControlService(conn, base=paper())  # owner turned the paper opt-out off
    assert svc.set("auto_approve.scorecard_gate", "off", actor=LOCAL_ACTOR, source="cli").outcome
    eff = eff_live(conn)
    assert eff.auto_approve_scorecard_gate is True


def test_live_auto_approve_after_30_live_closes(conn: sqlite3.Connection) -> None:
    stamp(conn, "live")
    history(conn, 30, pnl_per_contract=50.0)
    rep, poster, svc = publish(conn, live())
    ph = _phash(conn)
    assert rep.auto_approved == [ph] and rep.auto_gated == []
    assert "Auto-approved (LIVE)" in json.dumps(poster.posted[0][1].blocks)
    rows = gate_rows(conn)
    assert [(r["actor"], json.loads(r["new"])) for r in rows] == [(LIVE_GATE_ACTOR, True)]
    assert svc.settings.live_gate_met is True
    # The sticky flag is applied from the store; a second sweep flips nothing.
    eff = eff_live(conn)
    assert eff.live_gate_met is True
    assert ApprovalService(conn, eff, LogCardPoster()).update_live_gate(NOW) is False
    assert len(gate_rows(conn)) == 1


def test_live_gate_flip_posted_once(conn: sqlite3.Connection) -> None:
    stamp(conn, "live")
    history(conn, 30, pnl_per_contract=50.0)
    notices: list[str] = []
    with mock.patch(
        "arc.approvals.auto.post_day_notice", side_effect=lambda c, t, n: notices.append(t)
    ):
        svc = ApprovalService(conn, live(), LogCardPoster(), live=True)
        assert svc.update_live_gate(NOW) is True
        svc2 = ApprovalService(conn, live(), LogCardPoster(), live=True)
        assert svc2.update_live_gate(NOW) is False  # same actor already flipped it once
    assert len(notices) == 1 and "Live gate: MET" in notices[0]


def test_live_gate_not_flipped_below_threshold(conn: sqlite3.Connection) -> None:
    stamp(conn, "live")
    history(conn, 29, pnl_per_contract=50.0)
    rep, _, svc = publish(conn, live())
    assert rep.auto_approved == [] and gate_rows(conn) == []
    assert svc.settings.live_gate_met is False


def test_owner_cannot_turn_live_gate_on(conn: sqlite3.Connection) -> None:
    svc = ControlService(conn, base=live())
    res = svc.set("live.gate_met", "on", actor=LOCAL_ACTOR, source="cli")
    assert res.outcome == "refused" and "arc:live-gate" in res.message
    other = svc.set_system("live.gate_met", "on", actor="arc:scorecard-gate", reason="x")
    assert other.outcome == "refused"
    assert gate_rows(conn) == []


def test_owner_can_turn_live_gate_off(conn: sqlite3.Connection) -> None:
    stamp(conn, "live")
    history(conn, 30, pnl_per_contract=50.0)
    ApprovalService(conn, live(), LogCardPoster()).update_live_gate(NOW)
    svc = ControlService(conn, base=live())
    assert svc.set("live.gate_met", "off", actor=LOCAL_ACTOR, source="cli").outcome == "applied"
    assert eff_live(conn).live_gate_met is False


def test_env_var_cannot_set_live_gate_met(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARC_LIVE_GATE_MET", "true")
    assert live().live_gate_met is False
    assert paper().live_gate_met is False


def test_reenable_scorecard_gate_is_noop_in_live(conn: sqlite3.Connection) -> None:
    stamp(conn, "live")
    history(conn, 30, pnl_per_contract=50.0)
    s = live().model_copy(update={"auto_approve_scorecard_gate": False})
    svc = ApprovalService(conn, s, LogCardPoster())
    assert svc.reenable_scorecard_gate(NOW) is False
    assert (
        conn.execute(
            "SELECT count(*) FROM config_changes WHERE key = 'auto_approve.scorecard_gate'"
        ).fetchone()[0]
        == 0
    )


def test_paper_behaviour_unchanged(conn: sqlite3.Connection) -> None:
    """Paper opt-out still auto-approves with 0 closes; no live-gate row is written."""
    stamp(conn, "paper")
    rep, poster, _ = publish(conn, paper(auto_approve_scorecard_gate=False))
    assert rep.auto_approved == [_phash(conn)]
    assert "Auto-approved (paper, gate off)" in json.dumps(poster.posted[0][1].blocks)
    assert gate_rows(conn) == []


def test_closes_not_gated_in_live(conn: sqlite3.Connection) -> None:
    stamp(conn, "live")
    conn.execute("UPDATE proposals SET kind = 'close' WHERE day IS NOT NULL")
    conn.commit()
    rep, _, _ = publish(conn, live())
    assert rep.auto_approved == [_phash(conn)] and rep.auto_gated == []


# ---------------------------------------------------------------------------
# Status surfaces
# ---------------------------------------------------------------------------


def test_auto_status_live_block(conn: sqlite3.Connection) -> None:
    stamp(conn, "live")
    history(conn, 3, pnl_per_contract=50.0)
    blk = live_gate_block(conn, live(), NOW)
    assert blk["gate_met"] is False and blk["size_cap"] == 1 and blk["live_closed_trades"] == 3
    assert blk["line"] == "live gate: NOT MET (3/30 live closes) — size cap 1 — auto-approve off"
    assert live_gate_block(conn, paper(), NOW) == {
        "env": "paper",
        "line": "live gate: n/a (paper)",
    }
    st = auto_status(ControlService(conn, base=live()), live())
    assert st["effective"] is False and st["live_gate_met"] is False


def test_auto_status_live_block_met(conn: sqlite3.Connection) -> None:
    stamp(conn, "live")
    history(conn, 30, pnl_per_contract=50.0)
    blk = live_gate_block(conn, live(), NOW)
    assert blk["gate_met"] is True and blk["size_cap"] is None
    assert blk["auto_approve_effective"] is True
    assert blk["line"].startswith("live gate: MET (30/30 live closes) — no size cap")


# ---------------------------------------------------------------------------
# Sizing: the clamp under D18, and the snapshot flag the gate reads
# ---------------------------------------------------------------------------


def test_propose_clamps_to_live_cap() -> None:
    from arc.sizing import apply_live_cap, size_contracts

    d18 = size_contracts(
        suggestion=3,
        max_loss_per_contract=Decimal(415),
        equity=Decimal(100_000),
        cap_pct=0.05,
    )
    assert d18.contracts == 3
    capped = apply_live_cap(d18, 1, Decimal(100_000))
    assert capped.contracts == 1 and capped.code == "live_capped"
    assert capped.max_loss_total == Decimal(415) and capped.pct_equity == pytest.approx(0.00415)
    assert apply_live_cap(d18, None, Decimal(100_000)) is d18  # paper / gate met
    assert apply_live_cap(d18, 5, Decimal(100_000)) is d18  # never raises the count
    none = size_contracts(
        suggestion=0, max_loss_per_contract=Decimal(415), equity=Decimal(1), cap_pct=0.05
    )
    assert apply_live_cap(none, 1, Decimal(1)) is none


def test_live_size_cap_and_gate_status(conn: sqlite3.Connection) -> None:
    from arc.gate.inputs import AccountSnapshot
    from arc.pipeline.market import live_gate_status, live_size_cap

    a = AccountSnapshot(equity=Decimal(1), last_equity=Decimal(1), as_of=NOW)
    assert live_size_cap(paper(), a) is None
    assert live_size_cap(live(), a) == 1  # unknown -> capped
    assert live_size_cap(live(), a.model_copy(update={"live_gate_met": True})) is None
    assert live_gate_status(conn, paper(), now=NOW) is None
    stamp(conn, "live")
    assert live_gate_status(conn, live(), now=NOW) is False
    assert live_gate_status(conn, live().model_copy(update={"live_gate_met": True}), now=NOW)
    for i in range(30):
        add_closed_trade(conn, i, pnl_per_contract=50.0)
    assert live_gate_status(conn, live(), now=NOW) is True


def test_live_gate_status_fails_closed(conn: sqlite3.Connection) -> None:
    from arc.pipeline.market import live_gate_status

    with mock.patch("arc.journal.scorecard.env_readiness", side_effect=RuntimeError("boom")):
        assert live_gate_status(conn, live(), now=NOW) is False
