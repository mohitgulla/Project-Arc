"""E6.6 / D34: per-env auto-approve switch, in-chain Execute, stale-band re-price."""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from decimal import Decimal as D
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest import mock

import pytest

from arc.approvals.auto import auto_status, notice_text, set_auto
from arc.approvals.service import AUTO_APPROVER, ApprovalService, LogCardPoster, approval_record
from arc.config import PER_ENV_SWITCHES, ArcEnv, ArcSettings
from arc.control.effective import apply_changes
from arc.control.registry import lookup
from arc.control.service import LOCAL_ACTOR, ControlService
from arc.control.store import ConfigChangeRepo
from arc.execution.ladder import ExecStatus
from arc.models import ApprovalDecision
from arc.pipeline.env import FIXTURE_NOW
from arc.pipeline.runner import fixture_run
from arc.routines.config import RoutinesConfig, load_routines
from arc.routines.dispatcher import Dispatcher
from arc.routines.handlers import JobContext, JobResult, JobSkippedError, RunEnv
from arc.routines.heartbeat import RecordingNotifier
from arc.routines.investor import (
    approval_events,
    chain_proposals,
    execute_step,
    investor_command,
)
from arc.routines.runs import RoutineEventRepo, RoutineRunRepo
from arc.store.db import connect
from arc.store.migrate import migrate
from arc.utils.calendar import ET
from tests import test_execution_ladder as L

if TYPE_CHECKING:
    from collections.abc import Sequence

OWNER = "U0OWNER001"
NOW = dt.datetime(2026, 10, 9, 10, 0, tzinfo=ET)
_FAKE_LIVE = Path(__file__)  # any existing file satisfies the live-env guard


def base(**kw: object) -> ArcSettings:
    return ArcSettings(_env_file=None, approver_slack_user_ids=[OWNER], **kw)  # type: ignore[call-arg]


def live_settings(**kw: object) -> ArcSettings:
    """A live ArcSettings without touching ~/.arc/live.env (the guard is patched)."""
    with mock.patch("arc.config._LIVE_ENV_PATH", _FAKE_LIVE):
        return ArcSettings(_env_file=None, env="live", **kw)  # type: ignore[call-arg]


def live_on(**kw: object) -> ArcSettings:
    """Live settings with auto_approve on the way the store turns it on (post-validation)."""
    return live_settings(**kw).model_copy(update={"auto_approve": True})


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = connect(":memory:")
    migrate(c)
    return c


@pytest.fixture(autouse=True)
def _no_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("ARC_AUTO_APPROVE", "ARC_AUTO_EXIT_DEFINED_RISK", "ARC_ENV"):
        monkeypatch.delenv(var, raising=False)


# ---------------------------------------------------------------------------
# Ladder: freshness re-price
# ---------------------------------------------------------------------------


def _fresh(conn: sqlite3.Connection, broker: Any, *, age_s: int, mid: D | None, **kw: Any) -> Any:
    clock = L.Clock()
    return (
        L.run(
            conn,
            broker,
            clock=clock,
            config=L.cfg(execution_max_quote_age_seconds=60),
            priced_at=L.NOW - dt.timedelta(seconds=age_s),
            fresh_mid=lambda: mid,
            **kw,
        ),
        clock,
    )


class TestLadderFreshness:
    def test_fresh_proposal_walks_as_approved(self, conn: sqlite3.Connection) -> None:
        b = L.ScriptedBroker([["new", "filled"]], fills={0: (2, "-0.85")})
        out, _ = _fresh(conn, b, age_s=30, mid=D("-0.70"))  # mid ignored: not stale
        assert out.status is ExecStatus.FILLED
        assert b.orders[0].limit_price == D("-0.85")

    def test_stale_in_band_reanchors_first_attempt(self, conn: sqlite3.Connection) -> None:
        b = L.ScriptedBroker([["new", "filled"]], fills={0: (2, "-0.80")})
        out, _ = _fresh(conn, b, age_s=120, mid=D("-0.803"))
        assert out.status is ExecStatus.FILLED
        assert b.orders[0].limit_price == D("-0.80")
        assert ("selected", "order:step") in L.journal(conn)

    def test_stale_outside_band_sends_nothing(self, conn: sqlite3.Connection) -> None:
        b = L.ScriptedBroker([["new", "filled"]])
        out, _ = _fresh(conn, b, age_s=120, mid=D("-0.70"))
        assert out.status is ExecStatus.CANCELLED and b.orders == []
        assert "stale band" in out.detail and "not sent" in out.detail
        assert ("no_trade", "order:stale_band") in L.journal(conn)
        assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0

    def test_stale_no_mid_sends_nothing(self, conn: sqlite3.Connection) -> None:
        b = L.ScriptedBroker([["new", "filled"]])
        out, _ = _fresh(conn, b, age_s=120, mid=None)
        assert out.status is ExecStatus.CANCELLED and b.orders == []
        assert ("no_trade", "order:stale_band") in L.journal(conn)

    def test_quote_error_fails_closed(self, conn: sqlite3.Connection) -> None:
        def boom() -> D:
            raise RuntimeError("feed down")

        b = L.ScriptedBroker([["new", "filled"]])
        clock = L.Clock()
        out = L.run(
            conn,
            b,
            clock=clock,
            config=L.cfg(execution_max_quote_age_seconds=60),
            priced_at=L.NOW - dt.timedelta(seconds=120),
            fresh_mid=boom,
        )
        assert out.status is ExecStatus.CANCELLED and b.orders == []

    def test_no_priced_at_means_no_reprice(self, conn: sqlite3.Connection) -> None:
        b = L.ScriptedBroker([["new", "filled"]], fills={0: (2, "-0.85")})
        out = L.run(conn, b, fresh_mid=lambda: D("0.5"))
        assert out.status is ExecStatus.FILLED


# ---------------------------------------------------------------------------
# Config: per-env switches
# ---------------------------------------------------------------------------


class TestPerEnvConfig:
    def test_switches_listed(self) -> None:
        assert {"auto_approve", "auto_exit_defined_risk"} == PER_ENV_SWITCHES

    def test_env_var_is_paper_only(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ARC_AUTO_APPROVE", "true")
        monkeypatch.setenv("ARC_AUTO_EXIT_DEFINED_RISK", "true")
        p = ArcSettings(_env_file=None)  # type: ignore[call-arg]
        assert p.auto_approve is True and p.auto_exit_defined_risk is True
        s = live_settings()
        assert s.env is ArcEnv.LIVE
        assert s.auto_approve is False and s.auto_exit_defined_risk is False

    def test_live_kwarg_forced_off_too(self) -> None:
        assert live_settings(auto_approve=True).auto_approve is False

    def test_default_off_both_envs(self) -> None:
        assert ArcSettings(_env_file=None).auto_approve is False  # type: ignore[call-arg]
        assert live_settings().auto_approve is False

    def test_max_quote_age_default_and_bounds(self) -> None:
        assert ArcSettings(_env_file=None).execution_max_quote_age_seconds == 60  # type: ignore[call-arg]
        with pytest.raises(ValueError, match="less than or equal to 600"):
            ArcSettings(_env_file=None, execution_max_quote_age_seconds=601)  # type: ignore[call-arg]


class TestRegistry:
    @pytest.mark.parametrize(
        ("key", "env"),
        [
            ("auto_approve.paper", "paper"),
            ("auto_approve.live", "live"),
            ("auto_exit_defined_risk.paper", "paper"),
            ("auto_exit_defined_risk.live", "live"),
        ],
    )
    def test_per_env_keys(self, key: str, env: str) -> None:
        t = lookup(key)
        assert t.env == env and t.field in PER_ENV_SWITCHES

    def test_max_quote_age_key(self) -> None:
        t = lookup("max_quote_age")
        assert t.field == "execution_max_quote_age_seconds" and t.hard_ceiling == 600
        assert lookup("execute.max_quote_age") is t

    def _change(self, conn: sqlite3.Connection, key: str, new: object) -> Any:
        repo = ConfigChangeRepo(conn)
        repo.append(
            key=key,
            old=False,
            new=new,
            is_default=False,
            actor="t",
            reason=None,
            at=NOW,
            source="cli",
            status="applied",
            direction="riskier",
        )
        return repo.active()

    def test_paper_key_does_not_leak_into_live(self, conn: sqlite3.Connection) -> None:
        changes = self._change(conn, "auto_approve.paper", True)
        assert apply_changes(base(), changes, version=1).auto_approve is True
        with mock.patch("arc.config._LIVE_ENV_PATH", _FAKE_LIVE):
            assert apply_changes(live_settings(), changes, version=1).auto_approve is False

    def test_live_key_applies_only_in_live(self, conn: sqlite3.Connection) -> None:
        changes = self._change(conn, "auto_approve.live", True)
        with mock.patch("arc.config._LIVE_ENV_PATH", _FAKE_LIVE):
            assert apply_changes(live_settings(), changes, version=1).auto_approve is True
        assert apply_changes(base(), changes, version=1).auto_approve is False

    def test_env_var_plus_live_false_stays_off(
        self, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ARC_AUTO_APPROVE", "true")
        changes = self._change(conn, "auto_approve.paper", True)
        with mock.patch("arc.config._LIVE_ENV_PATH", _FAKE_LIVE):
            assert apply_changes(live_settings(), changes, version=1).auto_approve is False


# ---------------------------------------------------------------------------
# `arc approve auto` (control service path + confirm code)
# ---------------------------------------------------------------------------


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> dt.datetime:
        return self.now


def _svc(
    conn: sqlite3.Connection, clock: Clock, settings: ArcSettings | None = None
) -> ControlService:
    return ControlService(conn, base=settings or base(), now=clock, is_halted=lambda: False)


class TestAutoCli:
    def test_paper_on_off_applied_and_logged(self, conn: sqlite3.Connection) -> None:
        svc = _svc(conn, Clock())
        res = set_auto(svc, env="paper", on=True, reason="paper loop")
        assert res.outcome == "applied" and res.key == "auto_approve.paper"
        assert svc.settings().auto_approve is True
        st_ = auto_status(svc, base())
        assert st_ == {
            "env": "paper",
            "paper": True,
            "paper_overridden": True,
            "live": False,
            "live_overridden": False,
            "effective": True,
            "config_version": st_["config_version"],
        }
        hist = svc.history("auto_approve.paper")
        assert hist and hist[0].reason == "paper loop" and hist[0].actor.startswith(LOCAL_ACTOR)
        assert set_auto(svc, env="paper", on=False, reason=None).outcome == "applied"
        assert svc.settings().auto_approve is False

    @pytest.fixture(autouse=True)
    def _live_guard(self) -> Any:
        with mock.patch("arc.config._LIVE_ENV_PATH", _FAKE_LIVE):
            yield

    def test_live_on_needs_code(self, conn: sqlite3.Connection) -> None:
        clock = Clock()
        svc = _svc(conn, clock, live_settings(approver_slack_user_ids=[OWNER]))
        res = set_auto(svc, env="live", on=True, reason="go")
        assert res.outcome == "pending" and res.pending is not None
        assert svc.settings().auto_approve is False  # staged, not on
        code = res.pending.code
        # wrong code refused, real code applies, reuse refused
        assert (
            set_auto(svc, env="live", on=True, reason=None, confirm_code="nope").outcome
            == "refused"
        )
        ok = set_auto(svc, env="live", on=True, reason=None, confirm_code=code)
        assert ok.outcome == "applied" and svc.settings().auto_approve is True
        assert (
            set_auto(svc, env="live", on=True, reason=None, confirm_code=code).outcome == "refused"
        )
        # off is immediate
        assert set_auto(svc, env="live", on=False, reason=None).outcome == "applied"
        assert svc.settings().auto_approve is False

    def test_live_code_expires(self, conn: sqlite3.Connection) -> None:
        clock = Clock()
        svc = _svc(conn, clock, live_settings(approver_slack_user_ids=[OWNER]))
        res = set_auto(svc, env="live", on=True, reason=None)
        assert res.pending is not None
        clock.now = NOW + dt.timedelta(minutes=11)
        out = set_auto(svc, env="live", on=True, reason=None, confirm_code=res.pending.code)
        assert out.outcome == "refused" and svc.settings().auto_approve is False

    def test_live_switch_from_paper_process_stays_off_here(self, conn: sqlite3.Connection) -> None:
        """Setting auto_approve.live from a paper shell records it; paper stays unaffected."""
        svc = _svc(conn, Clock())
        res = set_auto(svc, env="live", on=True, reason=None)
        assert res.pending is not None
        assert (
            set_auto(svc, env="live", on=True, reason=None, confirm_code=res.pending.code).outcome
            == "applied"
        )
        assert svc.settings().auto_approve is False  # this process is paper
        assert auto_status(svc, base())["live"] is True

    def test_notice_text(self) -> None:
        assert notice_text("paper", True) == "Auto-approve: ON (paper)"
        assert notice_text("live", True) == "*Auto-approve: ON (live)*"
        assert notice_text("live", False) == "*Auto-approve: OFF (live)*"

    def test_cli_status_and_flip(self, tmp_path: Any, capsys: pytest.CaptureFixture[str]) -> None:
        from arc.cli import main

        db = str(tmp_path / "a.db")
        assert main(["approve", "auto", "status", "--db", db]) == 0
        assert "auto_approve: off (paper)" in capsys.readouterr().out
        assert main(["approve", "auto", "on", "--db", db, "--no-slack", "--reason", "t"]) == 0
        out = json.loads(capsys.readouterr().out.split("\n}")[0] + "\n}")
        assert out["outcome"] == "applied" and out["env"] == "paper"
        assert main(["approve", "auto", "status", "--db", db]) == 0
        assert "auto_approve: on (paper)" in capsys.readouterr().out
        assert main(["approve", "auto", "on", "--env", "live", "--db", db, "--no-slack"]) == 0
        out = json.loads(capsys.readouterr().out.split("\n}")[0] + "\n}")
        assert out["outcome"] == "pending" and "--confirm-live" in out["message"]
        assert main(["approve", "auto", "off", "--db", db, "--no-slack"]) == 0


class TestDayBanner:
    def test_banner_reads_effective_config(self, conn: sqlite3.Connection) -> None:
        from arc.routines.heartbeat import day_banner

        assert day_banner(conn) == "Auto-approve: OFF (paper)"
        set_auto(_svc(conn, Clock()), env="paper", on=True, reason=None)
        assert day_banner(conn) == "Auto-approve: ON (paper)"

    def test_first_use_posts_banner_under_root(self, conn: sqlite3.Connection) -> None:
        from arc.routines.heartbeat import day_thread_ts
        from arc.slack.client import ArcSlackClient

        client = mock.create_autospec(ArcSlackClient, instance=True)
        client.post_daily_session.return_value = {"ts": "1.0"}
        assert day_thread_ts(conn, client, NOW.date()) == "1.0"
        assert day_thread_ts(conn, client, NOW.date()) == "1.0"  # cached: no second post
        client.post_daily_session.assert_called_once()
        client.reply.assert_called_once()
        assert client.reply.call_args.kwargs["text"] == "Auto-approve: OFF (paper)"
        assert client.reply.call_args.kwargs["thread_ts"] == "1.0"


# ---------------------------------------------------------------------------
# Approval service: per-env auto-approve
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def _pipeline_db() -> bytes:
    conn, report = fixture_run(
        ArcSettings(_env_file=None, account_profile="margin"),
        load_routines(),  # type: ignore[call-arg]
    )
    assert len(report.proposals) == 1
    return conn.serialize()


def _db(raw: bytes) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.deserialize(raw)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("UPDATE gate_decisions SET token = ?", ("tok-fixture",))
    conn.commit()
    return conn


@pytest.fixture
def pconn(_pipeline_db: bytes) -> sqlite3.Connection:
    return _db(_pipeline_db)


def _phash(conn: sqlite3.Connection) -> str:
    return str(conn.execute("SELECT proposal_hash FROM proposals").fetchone()[0])


class TestServicePerEnv:
    def test_live_env_var_never_auto_approves(
        self, pconn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ARC_AUTO_APPROVE", "true")
        s = live_settings(account_profile="margin")
        poster = LogCardPoster()
        rep = ApprovalService(pconn, s, poster).publish_pending(FIXTURE_NOW)
        assert rep.auto_approved == [] and rep.published == [_phash(pconn)]
        assert poster.posted and "Auto-approved" not in json.dumps(poster.posted[0][1].blocks)

    def test_live_store_switch_auto_approves_marked_live(self, pconn: sqlite3.Connection) -> None:
        s = live_on(account_profile="margin")
        poster = LogCardPoster()
        rep = ApprovalService(pconn, s, poster).publish_pending(FIXTURE_NOW)
        ph = _phash(pconn)
        assert rep.auto_approved == [ph]
        rec = approval_record(pconn, ph)
        assert rec is not None and rec.slack_user == AUTO_APPROVER
        assert rec.decision is ApprovalDecision.APPROVED
        text = json.dumps(poster.posted[0][1].blocks)
        assert "Auto-approved (LIVE)" in text and "Approve" not in text.replace("Auto-approved", "")
        row = pconn.execute(
            "SELECT payload FROM decisions WHERE stage='approval' AND proposal_hash=?", (ph,)
        ).fetchone()
        assert json.loads(row[0])["env"] == "live"

    def test_paper_marked_paper(
        self, pconn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ARC_AUTO_APPROVE", "true")
        s = ArcSettings(_env_file=None, account_profile="margin")  # type: ignore[call-arg]
        poster = LogCardPoster()
        rep = ApprovalService(pconn, s, poster).publish_pending(FIXTURE_NOW)
        assert rep.auto_approved == [_phash(pconn)]
        assert "Auto-approved (paper)" in json.dumps(poster.posted[0][1].blocks)

    def test_publish_only_restricts_and_is_idempotent(self, pconn: sqlite3.Connection) -> None:
        s = ArcSettings(_env_file=None, account_profile="margin")  # type: ignore[call-arg]
        svc = ApprovalService(pconn, s, LogCardPoster())
        assert svc.publish_pending(FIXTURE_NOW, only=[]).published == []
        assert svc.publish_pending(FIXTURE_NOW, only=["nope"]).published == []
        ph = _phash(pconn)
        assert svc.publish_pending(FIXTURE_NOW, only=[ph]).published == [ph]
        # the later tick sweep finds nothing left for it
        assert svc.publish_pending(FIXTURE_NOW).published == []


# ---------------------------------------------------------------------------
# execute_step
# ---------------------------------------------------------------------------


EXEC_YAML = """
personas:
  director: {schedule: ["09:00"], days: trading, chain: [propose, execute], ttl: 2h}
  investor: {trigger: approval, llm: false}
steps:
  propose: {writes: [proposal], llm: false}
  execute: {reads: [proposal], writes: [], llm: false}
"""


def _ctx(
    conn: sqlite3.Connection,
    *,
    chain: str | None = "chain-abc",
    run_env: RunEnv | None = None,
    settings: ArcSettings | None = None,
) -> JobContext:
    from arc.context.store import ContextStore

    routines = RoutinesConfig.model_validate(
        {
            "personas": {
                "director": {"schedule": ["09:00"], "days": "trading", "chain": ["execute"]}
            },
            "steps": {"execute": {"reads": ["proposal"], "writes": [], "llm": False}},
        }
    )
    kind, step = routines.step("execute")
    return JobContext(
        job="execute",
        kind=kind,
        spec=step,
        run_id="run-exec",
        chain_run_id=chain,
        scheduled_for=FIXTURE_NOW,
        now=FIXTURE_NOW,
        conn=conn,
        snapshot=ContextStore(conn).snapshot(FIXTURE_NOW),
        routines=routines,
        settings_factory=lambda: settings or ArcSettings(_env_file=None, account_profile="margin"),  # type: ignore[call-arg]
        run_env=run_env or RunEnv(db_path="/tmp/x.db", lock_dir="/tmp/locks", slack=True),
    )


def _join_chain(conn: sqlite3.Connection, chain: str = "chain-abc") -> None:
    """Re-label the fixture run's chain as *chain* (propose already belongs to it).

    The fixture pipeline ran director -> ... -> propose -> execute as one chain; its
    execute step saw no Slack and published nothing, so the proposal is still fresh.
    """
    conn.execute(
        "UPDATE routine_runs SET chain_run_id = ? WHERE chain_run_id IS NOT NULL", (chain,)
    )
    conn.commit()
    assert chain_proposals(conn, chain) == [_phash(conn)]


class Spawner:
    def __init__(self) -> None:
        self.argv: list[list[str]] = []

    def __call__(self, argv: Sequence[str]) -> int:
        self.argv.append(list(argv))
        return 4242


class TestExecuteStep:
    def test_not_in_chain_skips(self, pconn: sqlite3.Connection) -> None:
        with pytest.raises(JobSkippedError):
            execute_step(_ctx(pconn, chain=None), spawn=Spawner())

    def test_off_is_noop_cards_actionable(self, pconn: sqlite3.Connection) -> None:
        _join_chain(pconn)
        poster = LogCardPoster()
        s = ArcSettings(_env_file=None, account_profile="margin")  # type: ignore[call-arg]
        svc = ApprovalService(pconn, s, poster)
        sp = Spawner()
        res = execute_step(_ctx(pconn, settings=s), spawn=sp, service=svc)
        assert "awaiting approval (1 card(s); auto-approve off" in res.summary
        assert res.metrics["dispatched"] == 0 and res.metrics["published"] == 1
        assert sp.argv == []
        assert pconn.execute("SELECT status FROM approval_requests").fetchone()[0] == "pending"
        assert '"action_id"' in json.dumps(poster.posted[0][1].blocks)  # buttons present

    def test_no_proposals(self, pconn: sqlite3.Connection) -> None:
        s = ArcSettings(_env_file=None, account_profile="margin")  # type: ignore[call-arg]
        res = execute_step(
            _ctx(pconn, chain="chain-other"),
            spawn=Spawner(),
            service=ApprovalService(pconn, s, LogCardPoster()),
        )
        assert res.summary == "no proposals to execute" and res.metrics["proposals"] == 0

    def test_no_slack_publishes_nothing(self, pconn: sqlite3.Connection) -> None:
        _join_chain(pconn)
        res = execute_step(_ctx(pconn, run_env=RunEnv(slack=False)), spawn=Spawner())
        assert "not published (no Slack)" in res.summary
        assert pconn.execute("SELECT COUNT(*) FROM approval_requests").fetchone()[0] == 0

    def test_on_auto_approves_and_dispatches(
        self, pconn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ARC_AUTO_APPROVE", "true")
        _join_chain(pconn)
        s = ArcSettings(_env_file=None, account_profile="margin")  # type: ignore[call-arg]
        svc = ApprovalService(pconn, s, LogCardPoster())
        sp = Spawner()
        res = execute_step(_ctx(pconn, settings=s), spawn=sp, service=svc)
        ph = _phash(pconn)
        assert res.metrics == {
            "proposals": 1,
            "published": 1,
            "auto_approved": 1,
            "dispatched": 1,
            "auto_approve": 1,
        }
        assert "1 ladder(s) dispatched" in res.summary and "started (paper)" in res.notice
        (argv,) = sp.argv
        evt = pconn.execute("SELECT id FROM routine_events WHERE name='approval'").fetchone()[0]
        # E6.2d: claimed by this execute run before the spawn; consumed later by the
        # Investor run, and never pending for the tick's drain in between.
        assert approval_events(pconn, [ph]) == {}
        ev = RoutineEventRepo(pconn).get(evt)
        assert ev is not None and ev.dispatched_by == "run-exec" and ev.consumed_at is None
        assert RoutineEventRepo(pconn).pending(until=FIXTURE_NOW) == []
        assert argv[3:] == [
            "routines",
            "run",
            "investor",
            "--event",
            evt,
            "--chain-run-id",
            "chain-abc",
            "--parent-run-id",
            "run-exec",
            "--db",
            "/tmp/x.db",
            "--lock-dir",
            "/tmp/locks",
        ]
        assert "--no-slack" not in argv
        assert chain_proposals(pconn, "chain-abc") == [ph]
        # the approval record is the D34 one
        rec = approval_record(pconn, ph)
        assert rec is not None and rec.slack_user == AUTO_APPROVER

    def test_halted_does_not_dispatch(
        self, pconn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from arc.gate.halt import HaltSwitch
        from arc.store.repos import HaltRepo

        monkeypatch.setenv("ARC_AUTO_APPROVE", "true")
        _join_chain(pconn)
        s = ArcSettings(_env_file=None, account_profile="margin")  # type: ignore[call-arg]
        HaltSwitch(HaltRepo(pconn)).halt(reason="test", actor="t", now=FIXTURE_NOW)
        sp = Spawner()
        res = execute_step(
            _ctx(pconn, settings=s), spawn=sp, service=ApprovalService(pconn, s, LogCardPoster())
        )
        assert res.summary.startswith("halted:") and sp.argv == []

    def test_investor_command_shape(self) -> None:
        argv = investor_command(
            RunEnv(db_path="d.db", config_path="r.yaml", lock_dir="lk", slack=False),
            "evt-1",
            chain_run_id="c",
            parent_run_id="p",
        )
        assert argv[3:] == [
            "routines",
            "run",
            "investor",
            "--event",
            "evt-1",
            "--chain-run-id",
            "c",
            "--parent-run-id",
            "p",
            "--db",
            "d.db",
            "--config",
            "r.yaml",
            "--lock-dir",
            "lk",
            "--no-slack",
        ]


# ---------------------------------------------------------------------------
# Dispatcher.run_event: joins the chain, consumes the event, no double run
# ---------------------------------------------------------------------------


class TestRunEvent:
    def _disp(self, conn: sqlite3.Connection, calls: list[str]) -> Dispatcher:
        def investor(ctx: JobContext) -> JobResult:
            calls.append(ctx.event.payload["proposal_hash"] if ctx.event else "?")
            return JobResult(summary="worked")

        return Dispatcher(
            conn,
            RoutinesConfig.model_validate(
                {"personas": {"investor": {"trigger": "approval", "llm": False}}}
            ),
            handlers={"investor": investor},
            notifier=RecordingNotifier(),
            is_halted=lambda: False,
        )

    def test_joins_chain_and_consumes(self, conn: sqlite3.Connection) -> None:
        calls: list[str] = []
        d = self._disp(conn, calls)
        RoutineRunRepo(conn).claim(
            job="director",
            scheduled_for=NOW,
            reason="t",
            chain_run_id="chain-1",
            step_index=0,
            now=NOW,
        )
        ev = RoutineEventRepo(conn).emit("approval", {"proposal_hash": "abc"}, now=NOW)
        out = d.run_event("investor", ev, now=NOW + dt.timedelta(seconds=5), chain_run_id="chain-1")
        assert [o.status for o in out] == ["ok"] and calls == ["abc"]
        run = RoutineRunRepo(conn).chain("chain-1")[-1]
        assert run.job == "investor" and run.step_index == 1 and run.reason == "event:approval"
        row = conn.execute("SELECT consumed_at, consumed_by FROM routine_events").fetchone()
        assert row["consumed_at"] and json.loads(row["consumed_by"]) == [run.run_id]
        # a later tick has nothing left to drain: the Investor never runs twice
        d.tick(NOW + dt.timedelta(minutes=5), since=NOW)
        assert calls == ["abc"]

    def test_unknown_job(self, conn: sqlite3.Connection) -> None:
        d = self._disp(conn, [])
        ev = RoutineEventRepo(conn).emit("approval", {"proposal_hash": "abc"}, now=NOW)
        with pytest.raises(KeyError):
            d.run_event("nope", ev, now=NOW)

    def test_cli_event_path(self, tmp_path: Any, capsys: pytest.CaptureFixture[str]) -> None:
        from arc.cli import main

        db = str(tmp_path / "e.db")
        c = connect(db)
        migrate(c)
        ev = RoutineEventRepo(c).emit("approval", {"proposal_hash": "zzz"}, now=NOW)
        c.close()
        assert (
            main(
                [
                    "routines",
                    "run",
                    "investor",
                    "--event",
                    "nope",
                    "--db",
                    db,
                    "--no-slack",
                    "--lock-dir",
                    str(tmp_path),
                ]
            )
            == 2
        )
        assert "unknown event" in capsys.readouterr().out
        # a real event: the investor refuses (no approval request) -> failed run, exit 1
        rc = main(
            [
                "routines",
                "run",
                "investor",
                "--event",
                ev.id,
                "--db",
                db,
                "--no-slack",
                "--lock-dir",
                str(tmp_path),
                "--now",
                NOW.isoformat(),
            ]
        )
        out = capsys.readouterr().out
        assert rc == 1 and '"job": "investor"' in out and '"status": "failed"' in out
