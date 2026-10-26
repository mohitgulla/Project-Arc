"""E3.9a (D137): a daily-loss halt expires at the next session open.

Part of ``make test-gate`` (100% branch coverage on ``arc.gate``). Every test injects
``now``; nothing reads the wall clock.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal as D
from typing import TYPE_CHECKING

import pytest

from arc.config import ArcSettings
from arc.gate import AccountSnapshot, HaltKind, HaltSwitch
from arc.gate import halt as H
from arc.store.db import connect
from arc.store.migrate import migrate
from arc.store.repos import HaltRepo
from arc.utils.calendar import ET

if TYPE_CHECKING:
    from pathlib import Path

OWNER = "U0C5KUMH28G"
FRI_1330 = dt.datetime(2026, 10, 23, 13, 30, tzinfo=ET)  # the observed halt
SAT_1000 = dt.datetime(2026, 10, 24, 10, 0, tzinfo=ET)
SUN_2300 = dt.datetime(2026, 10, 25, 23, 0, tzinfo=ET)
MON_0925 = dt.datetime(2026, 10, 26, 9, 25, tzinfo=ET)
MON_1500 = dt.datetime(2026, 10, 26, 15, 0, tzinfo=ET)


@pytest.fixture
def cfg() -> ArcSettings:
    return ArcSettings(_env_file=None, account_profile="margin", owner_slack_user_id=OWNER)  # type: ignore[call-arg]


@pytest.fixture
def repo(tmp_path: Path) -> HaltRepo:
    conn = connect(tmp_path / "arc.db")
    migrate(conn)
    return HaltRepo(conn)


def _acct(equity: str, last: str = "100000") -> AccountSnapshot:
    return AccountSnapshot(equity=D(equity), last_equity=D(last), as_of=FRI_1330)


def _daily_loss(repo: HaltRepo, cfg: ArcSettings, now: dt.datetime) -> H.HaltRecord:
    rec = HaltSwitch(repo).check_daily_loss(_acct("96000"), cfg, now=now, ticks=2)
    assert rec is not None and rec.kind is HaltKind.DAILY_LOSS
    return rec


def _row(repo: HaltRepo, halt_id: str) -> dict[str, object]:
    row = repo.conn.execute("SELECT * FROM halts WHERE id = ?", (halt_id,)).fetchone()
    return dict(row)


class TestRollover:
    def test_friday_halt_ignored_on_weekend_cleared_monday(
        self, repo: HaltRepo, cfg: ArcSettings
    ) -> None:
        rec = _daily_loss(repo, cfg, FRI_1330)
        assert HaltSwitch(repo, now=FRI_1330).opens_blocked()
        for weekend in (SAT_1000, SUN_2300):
            # still recorded (no clear on a non-session day) ...
            assert HaltSwitch(repo).expire_stale_session_halts(weekend) == []
            assert _row(repo, rec.id)["cleared_at"] is None
            # ... but the read side already treats it as expired
            assert not HaltSwitch(repo, now=weekend).opens_blocked()
        # the legacy switch (no clock) still counts the uncleared row
        assert HaltSwitch(repo).opens_blocked()
        (cleared,) = HaltSwitch(repo).expire_stale_session_halts(MON_0925)
        assert cleared.id == rec.id and cleared.cleared_by == H.SESSION_ROLLOVER_ACTOR
        assert cleared.cleared_at == MON_0925
        row = _row(repo, rec.id)
        assert row["cleared_by"] == "arc:session-rollover" and row["cleared_at"] is not None
        state = HaltSwitch(repo).state()
        assert not state.opens_blocked and state.active == []

    def test_holiday_monday_rolls_to_tuesday(self, repo: HaltRepo, cfg: ArcSettings) -> None:
        fri = dt.datetime(2026, 9, 4, 14, 0, tzinfo=ET)  # Labor Day is Mon 2026-09-07
        _daily_loss(repo, cfg, fri)
        labor_day = dt.datetime(2026, 9, 7, 9, 25, tzinfo=ET)
        assert HaltSwitch(repo).expire_stale_session_halts(labor_day) == []
        assert HaltSwitch(repo).opens_blocked()
        tue = dt.datetime(2026, 9, 8, 9, 25, tzinfo=ET)
        assert len(HaltSwitch(repo).expire_stale_session_halts(tue)) == 1
        assert not HaltSwitch(repo).opens_blocked()

    def test_same_session_halt_not_cleared(self, repo: HaltRepo, cfg: ArcSettings) -> None:
        _daily_loss(repo, cfg, MON_0925 + dt.timedelta(hours=2))
        assert HaltSwitch(repo).expire_stale_session_halts(MON_1500) == []
        assert HaltSwitch(repo, now=MON_1500).opens_blocked()

    def test_other_kinds_from_a_prior_session_not_cleared(self, repo: HaltRepo) -> None:
        switch = HaltSwitch(repo)
        switch.halt(actor=OWNER, reason="manual", now=FRI_1330)
        switch.halt(actor="arc:reconcile", reason="fill_unknown", now=FRI_1330)
        switch.halt(actor="arc:experiments", reason="book_invariant: x", now=FRI_1330)
        assert switch.halt_opens_once(actor="arc:expiry", reason="IWM not flat", now=FRI_1330)
        assert switch.expire_stale_session_halts(MON_0925) == []
        assert len(HaltSwitch(repo, now=MON_0925).state().active) == 4
        assert HaltSwitch(repo, now=MON_0925).is_halted()

    def test_mixed_stack_clears_only_daily_loss(self, repo: HaltRepo, cfg: ArcSettings) -> None:
        stale = _daily_loss(repo, cfg, FRI_1330)
        HaltSwitch(repo).halt(actor=OWNER, reason="hold", now=FRI_1330)
        (cleared,) = HaltSwitch(repo).expire_stale_session_halts(MON_0925)
        assert cleared.id == stale.id
        state = HaltSwitch(repo).state()
        assert state.halted and [h.kind for h in state.active] == [HaltKind.MANUAL]

    def test_clear_twice_is_noop(self, repo: HaltRepo, cfg: ArcSettings) -> None:
        rec = _daily_loss(repo, cfg, FRI_1330)
        assert len(HaltSwitch(repo).expire_stale_session_halts(MON_0925)) == 1
        first = _row(repo, rec.id)
        assert HaltSwitch(repo).expire_stale_session_halts(MON_1500) == []
        assert _row(repo, rec.id) == first

    def test_row_cleared_concurrently_is_not_reported(
        self, repo: HaltRepo, cfg: ArcSettings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # another process (an owner !resume) cleared the row between read and update
        _daily_loss(repo, cfg, FRI_1330)
        monkeypatch.setattr(repo, "resume", lambda *a, **k: False)
        assert HaltSwitch(repo).expire_stale_session_halts(MON_0925) == []

    def test_read_guard_before_clear(self, repo: HaltRepo, cfg: ArcSettings) -> None:
        _daily_loss(repo, cfg, FRI_1330)
        clocked = HaltSwitch(repo, now=lambda: MON_0925)
        assert not clocked.opens_blocked() and not clocked.is_halted()
        acct = clocked.apply(_acct("100000"))
        assert not acct.halted and not acct.opens_halted
        # fixed-time switch agrees
        assert HaltSwitch(repo, now=MON_0925).state().active == []

    def test_read_guard_still_fails_closed(self, repo: HaltRepo) -> None:
        repo.conn.close()
        state = HaltSwitch(repo, now=MON_0925).state()
        assert state.halted and state.error

    def test_new_breach_next_session_raises_new_halt(
        self, repo: HaltRepo, cfg: ArcSettings
    ) -> None:
        _daily_loss(repo, cfg, FRI_1330)
        HaltSwitch(repo).expire_stale_session_halts(MON_0925)
        again = _daily_loss(repo, cfg, MON_1500)
        assert again.session_date == MON_1500.date()
        assert HaltSwitch(repo, now=MON_1500).opens_blocked()

    def test_resumed_same_session_does_not_refire(self, repo: HaltRepo, cfg: ArcSettings) -> None:
        _daily_loss(repo, cfg, MON_0925)
        HaltSwitch(repo).resume(actor=OWNER, config=cfg, now=MON_0925)
        acct = _acct("96000")
        assert HaltSwitch(repo).check_daily_loss(acct, cfg, now=MON_1500, ticks=3) is None


class TestPureHelpers:
    def test_halt_row_expired(self) -> None:
        assert H.halt_row_expired("daily_loss", "2026-10-23", MON_0925)
        assert H.halt_row_expired("daily_loss", dt.date(2026, 10, 23), MON_0925)
        assert not H.halt_row_expired("daily_loss", "2026-10-26", MON_0925)
        assert not H.halt_row_expired("manual", "2026-10-23", MON_0925)
        assert not H.halt_row_expired("daily_loss", None, MON_0925)
        assert not H.halt_row_expired("daily_loss", "", MON_0925)

    def test_et_day_boundary(self) -> None:
        # 23:30 ET Friday is 03:30 UTC Saturday: still Friday's session
        late = dt.datetime(2026, 10, 24, 3, 30, tzinfo=dt.UTC)
        assert not H.halt_row_expired("daily_loss", "2026-10-23", late)

    def test_notice(self) -> None:
        rec = H.HaltRecord(
            id="x",
            kind=HaltKind.DAILY_LOSS,
            reason="r",
            actor="arc:daily-loss",
            at=FRI_1330,
            session_date=FRI_1330.date(),
        )
        assert H.session_rollover_notice(rec) == (
            "[Ops] daily-loss halt from Fri Oct 23 expired at session open; opens allowed"
        )
        no_session = rec.model_copy(update={"session_date": None})
        assert H.session_rollover_notice(no_session) == H.session_rollover_notice(rec)
