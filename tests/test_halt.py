"""Tests for the kill switch + daily halt state (E3.3).

Covered by ``make test-gate`` (100% branch coverage on ``arc.gate``).
"""

from __future__ import annotations

import datetime as dt
import os
import sqlite3
import subprocess
import sys
from decimal import Decimal as D
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from hypothesis import given
from hypothesis import strategies as st

from arc.config import ArcSettings
from arc.execution import TradingHaltedError, require_trading_allowed
from arc.gate import (
    AccountSnapshot,
    HaltKind,
    HaltSwitch,
    Portfolio,
    ResumeNotAuthorizedError,
    RuleCode,
    daily_loss_breach,
    evaluate_with_halt,
)
from arc.gate import halt as H
from arc.slack.client import CHANNEL_ARC_INVESTOR, ArcSlackClient
from arc.slack.halt import auto_halt_on_daily_loss, handle_command
from arc.store.db import connect
from arc.store.migrate import current_version, migrate
from arc.store.repos import HaltRepo
from arc.utils.calendar import ET

OWNER = "U0C5KUMH28G"
OTHER = "U0OTHER0001"
NOW = dt.datetime(2026, 10, 9, 10, 0, tzinfo=ET)
REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def cfg() -> ArcSettings:
    return ArcSettings(_env_file=None, account_profile="margin", owner_slack_user_id=OWNER)  # type: ignore[call-arg]


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "arc.db"


def _open(path: Path) -> sqlite3.Connection:
    conn = connect(path)
    migrate(conn)
    return conn


@pytest.fixture()
def switch(db_path: Path) -> HaltSwitch:
    return HaltSwitch(HaltRepo(_open(db_path)))


def _acct(equity: str = "100000", last: str = "100000", halted: bool = False) -> AccountSnapshot:
    return AccountSnapshot(equity=D(equity), last_equity=D(last), halted=halted, as_of=NOW)


# ---------------------------------------------------------------------------
# Migration / table shape
# ---------------------------------------------------------------------------


class TestMigration:
    def test_halt_table_columns(self, switch: HaltSwitch, db_path: Path) -> None:
        conn = _open(db_path)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(halts)")}
        assert {"reason", "actor", "at", "cleared_at", "cleared_by", "kind"} <= cols
        assert "halted_at" not in cols and "resumed_at" not in cols
        assert current_version(conn) >= 4  # 004_halt_state applied

    def test_upgrade_preserves_active_v1_halt(self, db_path: Path) -> None:
        """A halt written under schema v1 is still active after migrating to v5 (halts = 004)."""
        conn = connect(db_path)
        v1 = (REPO_ROOT / "arc/store/migrations/001_initial.sql").read_text()
        conn.executescript(v1)
        conn.execute("INSERT INTO schema_version (version) VALUES (1)")
        conn.execute(
            "INSERT INTO halts (id, halted_at, reason, actor) VALUES ('old', "
            "'2026-10-08T14:00:00.000000Z', 'legacy', 'owner')"
        )
        conn.commit()
        applied = migrate(conn)
        assert applied[:3] == [2, 3, 4]  # every later migration applies on top
        state = HaltSwitch(HaltRepo(conn)).state()
        assert state.halted
        (rec,) = state.active
        assert (rec.id, rec.kind, rec.reason, rec.session_date) == (
            "old",
            HaltKind.MANUAL,
            "legacy",
            None,
        )
        assert rec.at == dt.datetime(2026, 10, 8, 10, 0, tzinfo=ET)


# ---------------------------------------------------------------------------
# HaltSwitch
# ---------------------------------------------------------------------------


class TestHaltSwitch:
    def test_not_halted_initially(self, switch: HaltSwitch) -> None:
        state = switch.state()
        assert not state.halted and state.active == [] and state.error is None

    def test_halt_records_reason_actor_at(self, switch: HaltSwitch) -> None:
        rec = switch.halt(actor=OTHER, reason="flash crash", now=NOW)
        assert switch.is_halted()
        (stored,) = switch.state().active
        assert stored == rec
        assert stored.actor == OTHER and stored.reason == "flash crash"
        assert stored.at == NOW and stored.at.tzinfo is not None
        assert stored.kind is HaltKind.MANUAL
        assert stored.session_date == dt.date(2026, 10, 9)
        assert stored.cleared_at is None

    def test_anyone_may_halt(self, switch: HaltSwitch) -> None:
        switch.halt(actor="U_RANDOM", reason="", now=NOW)
        assert switch.is_halted()

    def test_naive_now_rejected(self, switch: HaltSwitch) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            switch.halt(actor=OWNER, reason="x", now=dt.datetime(2026, 10, 9, 10, 0))

    def test_session_date_uses_et(self, switch: HaltSwitch) -> None:
        # 01:30 UTC on Oct 10 is 21:30 ET on Oct 9.
        rec = switch.halt(
            actor=OWNER, reason="x", now=dt.datetime(2026, 10, 10, 1, 30, tzinfo=dt.UTC)
        )
        assert rec.session_date == dt.date(2026, 10, 9)

    def test_owner_resume_clears_all(self, switch: HaltSwitch, cfg: ArcSettings) -> None:
        switch.halt(actor=OTHER, reason="a", now=NOW)
        switch.halt(actor=OWNER, reason="b", now=NOW + dt.timedelta(minutes=1))
        later = NOW + dt.timedelta(hours=1)
        cleared = switch.resume(actor=OWNER, config=cfg, now=later)
        assert [c.reason for c in cleared] == ["a", "b"]
        assert not switch.is_halted()

    def test_resume_persists_cleared_at_and_by(
        self, switch: HaltSwitch, cfg: ArcSettings, db_path: Path
    ) -> None:
        rec = switch.halt(actor=OTHER, reason="a", now=NOW)
        switch.resume(actor=OWNER, config=cfg, now=NOW + dt.timedelta(hours=1))
        row = _open(db_path).execute("SELECT * FROM halts WHERE id = ?", (rec.id,)).fetchone()
        assert row["cleared_by"] == OWNER
        assert H._from_store(row["cleared_at"]) == NOW + dt.timedelta(hours=1)

    def test_non_owner_cannot_resume(self, switch: HaltSwitch, cfg: ArcSettings) -> None:
        switch.halt(actor=OTHER, reason="a", now=NOW)
        with pytest.raises(ResumeNotAuthorizedError):
            switch.resume(actor=OTHER, config=cfg, now=NOW)
        with pytest.raises(ResumeNotAuthorizedError):
            switch.resume(actor="", config=cfg, now=NOW)
        assert switch.is_halted()

    def test_owner_from_config(self, switch: HaltSwitch) -> None:
        cfg = ArcSettings(
            _env_file=None, account_profile="margin", owner_slack_user_id="U_NEW_OWNER"
        )  # type: ignore[call-arg]
        switch.halt(actor=OTHER, reason="a", now=NOW)
        with pytest.raises(ResumeNotAuthorizedError):
            switch.resume(actor=OWNER, config=cfg, now=NOW)
        switch.resume(actor="U_NEW_OWNER", config=cfg, now=NOW)
        assert not switch.is_halted()

    def test_resume_when_not_halted(self, switch: HaltSwitch, cfg: ArcSettings) -> None:
        assert switch.resume(actor=OWNER, config=cfg, now=NOW) == []

    def test_apply_stamps_halted(self, switch: HaltSwitch) -> None:
        acct = _acct()
        assert switch.apply(acct) is acct
        switch.halt(actor=OTHER, reason="a", now=NOW)
        stamped = switch.apply(acct)
        assert stamped.halted and not acct.halted

    def test_apply_never_clears_existing_flag(self, switch: HaltSwitch) -> None:
        acct = _acct(halted=True)
        assert switch.apply(acct) is acct

    def test_unreadable_store_fails_closed(self, db_path: Path) -> None:
        conn = _open(db_path)
        sw = HaltSwitch(HaltRepo(conn))
        conn.close()
        state = sw.state()
        assert state.halted and state.error is not None and "ProgrammingError" in state.error
        assert sw.apply(_acct()).halted

    def test_naive_stored_timestamp_read_as_utc(self, switch: HaltSwitch, db_path: Path) -> None:
        conn = _open(db_path)
        conn.execute(
            "INSERT INTO halts (id, at, reason, actor) VALUES ('n', '2026-10-09T14:00:00', '', '')"
        )
        conn.commit()
        (rec,) = switch.state().active
        assert rec.at == NOW

    def test_corrupt_row_fails_closed(self, switch: HaltSwitch, db_path: Path) -> None:
        conn = _open(db_path)
        conn.execute("INSERT INTO halts (id, at, reason, actor) VALUES ('x', 'not-a-date', '', '')")
        conn.commit()
        state = switch.state()
        assert state.halted and state.error is not None and "ValueError" in state.error


# ---------------------------------------------------------------------------
# Restart survival
# ---------------------------------------------------------------------------


class TestSurvivesRestart:
    def test_halt_survives_reconnect(self, db_path: Path, cfg: ArcSettings) -> None:
        conn = _open(db_path)
        HaltSwitch(HaltRepo(conn)).halt(actor=OTHER, reason="before restart", now=NOW)
        conn.close()

        reopened = HaltSwitch(HaltRepo(_open(db_path)))
        assert reopened.is_halted()
        assert reopened.state().active[0].reason == "before restart"
        with pytest.raises(TradingHaltedError):
            require_trading_allowed(reopened)

    def test_halt_survives_process_restart(self, db_path: Path) -> None:
        """Halt in one Python process, read it from a fresh one (the `arc` CLI)."""
        env = {**os.environ, "ARC_DB_PATH": str(db_path), "ARC_OWNER_SLACK_USER_ID": OWNER}

        def arc(*args: str) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                [sys.executable, "-m", "arc.cli", *args],
                capture_output=True,
                text=True,
                env=env,
                cwd=REPO_ROOT,
                check=False,
            )

        assert arc("halt-status").returncode == 0
        halted = arc("slack-command", "--user", OTHER, "--text", "!halt vol spike")
        assert halted.returncode == 0, halted.stderr
        assert "HALT" in halted.stdout

        status = arc("halt-status")
        assert status.returncode == 1 and "vol spike" in status.stdout
        assert HaltSwitch(HaltRepo(_open(db_path))).is_halted()

        denied = arc("resume", "--actor", OTHER)
        assert denied.returncode == 2 and "DENIED" in denied.stdout
        assert arc("halt-status").returncode == 1

        assert arc("resume", "--actor", OWNER).returncode == 0
        assert arc("halt-status").returncode == 0

    def test_resume_is_persisted(self, db_path: Path, cfg: ArcSettings) -> None:
        conn = _open(db_path)
        sw = HaltSwitch(HaltRepo(conn))
        sw.halt(actor=OTHER, reason="a", now=NOW)
        sw.resume(actor=OWNER, config=cfg, now=NOW)
        conn.close()
        assert not HaltSwitch(HaltRepo(_open(db_path))).is_halted()


# ---------------------------------------------------------------------------
# Gate reads halt state
# ---------------------------------------------------------------------------


class TestGateReadsHalt:
    def test_evaluate_with_halt_blocks(self, switch: HaltSwitch) -> None:
        from tests import test_gate as G  # reuse the E3.1 baseline that passes the gate

        pf, mkt, cfg = Portfolio(), G.mkt(), G.cfg()
        ok = evaluate_with_halt(switch, G.make_proposal(), G.acct(), pf, cfg, market=mkt, now=NOW)
        assert ok.passed, ok.violations

        switch.halt(actor=OTHER, reason="a", now=NOW)
        blocked = evaluate_with_halt(
            switch, G.make_proposal(), G.acct(), pf, cfg, market=mkt, now=NOW
        )
        assert not blocked.passed
        assert [v.split(":", 1)[0] for v in blocked.violations] == [RuleCode.HALTED.value]
        assert blocked.account_snapshot["halted"] is True


# ---------------------------------------------------------------------------
# Execution refuses when halted
# ---------------------------------------------------------------------------


class TestExecutionGuard:
    def test_allows_when_clear(self, switch: HaltSwitch) -> None:
        require_trading_allowed(switch)

    def test_refuses_when_halted(self, switch: HaltSwitch) -> None:
        switch.halt(actor=OTHER, reason="manual stop", now=NOW)
        with pytest.raises(TradingHaltedError, match="manual stop"):
            require_trading_allowed(switch)

    def test_refuses_blank_reason(self, switch: HaltSwitch) -> None:
        switch.halt(actor=OTHER, reason="", now=NOW)
        with pytest.raises(TradingHaltedError, match="no reason"):
            require_trading_allowed(switch)

    def test_refuses_when_unreadable(self, db_path: Path) -> None:
        conn = _open(db_path)
        sw = HaltSwitch(HaltRepo(conn))
        conn.close()
        with pytest.raises(TradingHaltedError, match="unreadable"):
            require_trading_allowed(sw)


# ---------------------------------------------------------------------------
# Daily-loss auto-halt
# ---------------------------------------------------------------------------


class TestDailyLoss:
    def test_breach_threshold(self, cfg: ArcSettings) -> None:
        assert daily_loss_breach(_acct("97001", "100000"), cfg) is None
        assert daily_loss_breach(_acct("97000", "100000"), cfg) is not None  # exactly 3% halts
        assert daily_loss_breach(_acct("100000", "0"), cfg) is not None  # fails closed

    @given(
        last=st.decimals(min_value=1, max_value=10_000_000, places=2),
        loss_bp=st.integers(min_value=-1000, max_value=10_000),
    )
    def test_breach_matches_gate_rule(self, last: D, loss_bp: int) -> None:
        cfg = ArcSettings(_env_file=None, account_profile="margin")  # type: ignore[call-arg]
        equity = last - last * D(loss_bp) / D(10_000)
        acct = _acct(str(equity), str(last))
        expected = (last - equity) / last >= D(str(cfg.daily_loss_halt_pct))
        assert (daily_loss_breach(acct, cfg) is not None) is expected

    def test_auto_halts_once_per_session(self, switch: HaltSwitch, cfg: ArcSettings) -> None:
        assert switch.check_daily_loss(_acct("98000", "100000"), cfg, now=NOW) is None
        rec = switch.check_daily_loss(_acct("96000", "100000"), cfg, now=NOW)
        assert rec is not None and rec.kind is HaltKind.DAILY_LOSS
        assert rec.actor == "arc:daily-loss" and "4.0000%" in rec.reason
        assert switch.is_halted()
        # Repeated checks in the same session do not stack.
        assert switch.check_daily_loss(_acct("95000", "100000"), cfg, now=NOW) is None
        assert len(switch.state().active) == 1

    def test_owner_resume_not_undone_same_session(
        self, switch: HaltSwitch, cfg: ArcSettings
    ) -> None:
        switch.check_daily_loss(_acct("96000", "100000"), cfg, now=NOW)
        switch.resume(actor=OWNER, config=cfg, now=NOW)
        later = NOW + dt.timedelta(minutes=5)
        assert switch.check_daily_loss(_acct("96000", "100000"), cfg, now=later) is None
        assert not switch.is_halted()

    def test_new_session_can_halt_again(self, switch: HaltSwitch, cfg: ArcSettings) -> None:
        switch.check_daily_loss(_acct("96000", "100000"), cfg, now=NOW)
        switch.resume(actor=OWNER, config=cfg, now=NOW)
        tomorrow = NOW + dt.timedelta(days=1)
        assert switch.check_daily_loss(_acct("96000", "100000"), cfg, now=tomorrow) is not None

    def test_manual_halt_does_not_suppress_daily_loss(
        self, switch: HaltSwitch, cfg: ArcSettings
    ) -> None:
        switch.halt(actor=OTHER, reason="manual", now=NOW)
        assert switch.check_daily_loss(_acct("96000", "100000"), cfg, now=NOW) is not None


# ---------------------------------------------------------------------------
# Slack surface
# ---------------------------------------------------------------------------


def _slack() -> tuple[ArcSlackClient, MagicMock]:
    fake = MagicMock()
    fake.chat_postMessage.return_value = {"ok": True, "ts": "1.2"}
    return ArcSlackClient(client=fake), fake


class TestSlackCommands:
    def test_non_command_ignored(self, switch: HaltSwitch, cfg: ArcSettings) -> None:
        assert handle_command("hello", user=OTHER, switch=switch, config=cfg, now=NOW) is None
        assert not switch.is_halted()

    def test_halt_from_anyone(self, switch: HaltSwitch, cfg: ArcSettings) -> None:
        reply = handle_command(
            "!halt  market is weird ", user=OTHER, switch=switch, config=cfg, now=NOW
        )
        assert reply is not None and "HALT" in reply and f"<@{OTHER}>" in reply
        (rec,) = switch.state().active
        assert rec.actor == OTHER and rec.reason == "market is weird"

    def test_halt_default_reason(self, switch: HaltSwitch, cfg: ArcSettings) -> None:
        handle_command("!HALT", user=OTHER, switch=switch, config=cfg, now=NOW)
        assert switch.state().active[0].reason == "manual !halt"

    def test_halt_without_user_still_halts(self, switch: HaltSwitch, cfg: ArcSettings) -> None:
        handle_command("!halt", user="", switch=switch, config=cfg, now=NOW)
        assert switch.state().active[0].actor == "unknown"

    def test_resume_non_owner_denied(self, switch: HaltSwitch, cfg: ArcSettings) -> None:
        handle_command("!halt", user=OTHER, switch=switch, config=cfg, now=NOW)
        reply = handle_command("!resume", user=OTHER, switch=switch, config=cfg, now=NOW)
        assert reply is not None and "not allowed" in reply
        assert switch.is_halted()

    def test_resume_without_user_denied(self, switch: HaltSwitch, cfg: ArcSettings) -> None:
        handle_command("!halt", user=OTHER, switch=switch, config=cfg, now=NOW)
        reply = handle_command("!resume", user="", switch=switch, config=cfg, now=NOW)
        assert reply is not None and "not allowed" in reply
        assert switch.is_halted()

    def test_resume_owner(self, switch: HaltSwitch, cfg: ArcSettings) -> None:
        handle_command("!halt", user=OTHER, switch=switch, config=cfg, now=NOW)
        reply = handle_command("!resume", user=OWNER, switch=switch, config=cfg, now=NOW)
        assert reply is not None and "RESUMED" in reply and "1 halt." in reply
        assert not switch.is_halted()

    def test_resume_owner_not_halted(self, switch: HaltSwitch, cfg: ArcSettings) -> None:
        reply = handle_command("!resume", user=OWNER, switch=switch, config=cfg, now=NOW)
        assert reply is not None and "was not halted" in reply


class TestAutoHaltPost:
    def test_posts_to_arc_investor(self, switch: HaltSwitch, cfg: ArcSettings) -> None:
        slack, fake = _slack()
        rec = auto_halt_on_daily_loss(
            switch, _acct("96000", "100000"), cfg, now=NOW, slack=slack, thread_ts="9.9"
        )
        assert rec is not None
        kw = fake.chat_postMessage.call_args.kwargs
        assert kw["channel"] == CHANNEL_ARC_INVESTOR and kw["thread_ts"] == "9.9"
        assert "daily loss" in kw["text"]

    def test_posts_root_without_thread(self, switch: HaltSwitch, cfg: ArcSettings) -> None:
        slack, fake = _slack()
        auto_halt_on_daily_loss(switch, _acct("96000", "100000"), cfg, now=NOW, slack=slack)
        kw = fake.chat_postMessage.call_args.kwargs
        assert kw["channel"] == CHANNEL_ARC_INVESTOR and "thread_ts" not in kw

    def test_no_post_without_breach(self, switch: HaltSwitch, cfg: ArcSettings) -> None:
        slack, fake = _slack()
        assert auto_halt_on_daily_loss(switch, _acct(), cfg, now=NOW, slack=slack) is None
        fake.chat_postMessage.assert_not_called()

    def test_no_repost_same_session(self, switch: HaltSwitch, cfg: ArcSettings) -> None:
        slack, fake = _slack()
        auto_halt_on_daily_loss(switch, _acct("96000", "100000"), cfg, now=NOW, slack=slack)
        auto_halt_on_daily_loss(switch, _acct("96000", "100000"), cfg, now=NOW, slack=slack)
        assert fake.chat_postMessage.call_count == 1

    def test_slack_failure_still_halts(self, switch: HaltSwitch, cfg: ArcSettings) -> None:
        slack, fake = _slack()
        fake.chat_postMessage.side_effect = RuntimeError("slack down")
        rec = auto_halt_on_daily_loss(switch, _acct("96000", "100000"), cfg, now=NOW, slack=slack)
        assert rec is not None and switch.is_halted()

    def test_without_slack_client(self, switch: HaltSwitch, cfg: ArcSettings) -> None:
        rec = auto_halt_on_daily_loss(switch, _acct("96000", "100000"), cfg, now=NOW, slack=None)
        assert rec is not None and switch.is_halted()


class TestHaltRepoRaw:
    def test_exists_for_session_and_clear_all(self, db_path: Path) -> None:
        repo = HaltRepo(_open(db_path))
        repo.halt(reason="r", actor="a", kind="daily_loss", session_date="2026-10-09")
        assert repo.exists_for_session(kind="daily_loss", session_date="2026-10-09")
        assert not repo.exists_for_session(kind="daily_loss", session_date="2026-10-10")
        assert repo.clear_all(actor=OWNER) == 1
        assert repo.clear_all(actor=OWNER) == 0
        assert repo.exists_for_session(kind="daily_loss", session_date="2026-10-09")

    def test_kind_check_constraint(self, db_path: Path) -> None:
        repo = HaltRepo(_open(db_path))
        with pytest.raises(sqlite3.IntegrityError):
            repo.halt(kind="bogus")


# ---------------------------------------------------------------------------
# E11.4 (D73): halt scope ('opens' = new opens stop, exits keep running)
# ---------------------------------------------------------------------------


class TestOpensOnlyScope:
    def test_existing_rows_default_to_scope_all(self, switch: HaltSwitch, db_path: Path) -> None:
        switch.halt(actor=OTHER, reason="old style", now=NOW)
        conn = _open(db_path)
        assert conn.execute("SELECT scope FROM halts").fetchone()[0] == "all"
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO halts (id, at, reason, actor, kind, scope) "
                "VALUES ('x', 'a', 'r', 'o', 'manual', 'closes')"
            )

    def test_opens_only_state_and_apply(self, switch: HaltSwitch) -> None:
        rec = switch.halt(actor="arc:expiry", reason="not flat", now=NOW, scope=H.HaltScope.OPENS)
        assert rec.scope is H.HaltScope.OPENS
        state = switch.state()
        assert not state.halted and state.opens_only and state.opens_blocked
        assert not switch.is_halted() and switch.opens_blocked()
        a = switch.apply(_acct())
        assert a.opens_halted and not a.halted
        # an already-flagged snapshot is returned as is
        assert switch.apply(a) is a
        # a full halt on top wins: everything stops
        switch.halt(actor=OTHER, reason="full", now=NOW)
        state = switch.state()
        assert state.halted and not state.opens_only
        b = switch.apply(_acct())
        assert b.halted and not b.opens_halted

    def test_no_halt_apply_is_identity(self, switch: HaltSwitch) -> None:
        a = _acct()
        assert switch.apply(a) is a and not switch.opens_blocked()

    def test_submit_guard_lets_closes_through_an_opens_only_halt(self, switch: HaltSwitch) -> None:
        switch.halt(actor="arc:expiry", reason="x", now=NOW, scope=H.HaltScope.OPENS)
        require_trading_allowed(switch, closing=True)
        with pytest.raises(TradingHaltedError):
            require_trading_allowed(switch)

    def test_gate_opens_only_halt_via_evaluate_with_halt(
        self, switch: HaltSwitch, cfg: ArcSettings
    ) -> None:
        from tests.test_gate import NOW as GNOW
        from tests.test_gate import _held_for_close, make_proposal, mkt

        switch.halt(actor="arc:expiry", reason="x", now=NOW, scope=H.HaltScope.OPENS)
        a = AccountSnapshot(equity=D("100000"), last_equity=D("100000"), as_of=GNOW)
        p = make_proposal()
        opened = evaluate_with_halt(switch, p, a, Portfolio(), cfg, market=mkt(), now=GNOW)
        assert RuleCode.HALTED.value in [v.split(":")[0] for v in opened.violations]
        closed = evaluate_with_halt(
            switch, p, a, _held_for_close(), cfg, market=mkt(), now=GNOW, closing=True
        )
        assert RuleCode.HALTED.value not in [v.split(":")[0] for v in closed.violations]

    def test_halt_opens_once_per_session_per_reason(
        self, switch: HaltSwitch, cfg: ArcSettings
    ) -> None:
        first = switch.halt_opens_once(actor="arc:expiry", reason="IWM not flat", now=NOW)
        assert first is not None and first.scope is H.HaltScope.OPENS
        assert switch.halt_opens_once(actor="arc:expiry", reason="IWM not flat", now=NOW) is None
        # the owner resumes: the next tick the same session does not re-raise it
        switch.resume(actor=OWNER, config=cfg, now=NOW + dt.timedelta(minutes=5))
        later = NOW + dt.timedelta(minutes=10)
        assert switch.halt_opens_once(actor="arc:expiry", reason="IWM not flat", now=later) is None
        assert switch.halt_opens_once(actor="arc:expiry", reason="other", now=later) is not None
        # a new session raises it again
        tomorrow = NOW + dt.timedelta(days=1)
        assert switch.halt_opens_once(actor="arc:expiry", reason="IWM not flat", now=tomorrow)

    def test_cli_halt_scope_opens_and_status(self, db_path: Path) -> None:
        env = {**os.environ, "ARC_DB_PATH": str(db_path), "ARC_OWNER_SLACK_USER_ID": OWNER}

        def arc(*args: str) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                [sys.executable, "-m", "arc.cli", *args], capture_output=True, text=True,
                env=env, cwd=REPO_ROOT, check=False,
            )  # fmt: skip

        out = arc("halt", "--actor", OWNER, "--reason", "expiry test", "--scope", "opens")
        assert out.returncode == 0 and "OPENS HALTED" in out.stdout, out.stderr
        status = arc("halt-status")
        assert status.returncode == 1
        assert "exits still run" in status.stdout and "scope=opens" in status.stdout
