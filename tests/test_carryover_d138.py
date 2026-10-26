"""D138 (E13.7b): the carry-over window counted in trading sessions.

``carryover.window_sessions: 1`` merges the previous trading session's run, so Monday
merges Friday, Tuesday merges Monday and the Monday after a Friday holiday merges
Thursday. A same-day re-run never merges today with today. ``window_sessions: null``
(or the registry's ``0``) is exactly the D64 48 clock-hour rule.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from typing import TYPE_CHECKING, Any

import pytest

from arc.config import ArcSettings
from arc.context.store import ContextStore
from arc.control.registry import REGISTRY, Direction, Risk, direction, lookup
from arc.store.db import connect
from arc.store.migrate import migrate
from arc.universe.carryover import apply_carryover, previous_entry
from arc.universe.config import universe_config
from arc.universe.tiers import (
    CarryoverKnobs,
    CarryoverSettings,
    Tier,
    TierMember,
    UniverseTierPayload,
)
from arc.utils.calendar import ET, is_session

if TYPE_CHECKING:
    from pathlib import Path


def _at(y: int, m: int, d: int, hh: int = 6, mm: int = 0) -> dt.datetime:
    return dt.datetime(y, m, d, hh, mm, tzinfo=ET)


# Week of 2026-10-19: Thu 22, Fri 23, Mon 26, Tue 27 (Scout slot 06:00 ET).
THU, FRI, MON, TUE = _at(2026, 10, 22), _at(2026, 10, 23), _at(2026, 10, 26), _at(2026, 10, 27)
# Good Friday 2026-04-03 (NYSE closed): Thu 04-02 -> Mon 04-06.
GF_THU, GF_MON = _at(2026, 4, 2), _at(2026, 4, 6)
# Labor Day Mon 2026-09-07: Fri 09-04 -> Tue 09-08.
LD_FRI, LD_TUE = _at(2026, 9, 4), _at(2026, 9, 8)

S1 = CarryoverSettings(window_sessions=1)
H48 = CarryoverSettings(window_sessions=None)


def _settings(**kw: Any) -> ArcSettings:
    return ArcSettings(_env_file=None, env="paper", **kw)  # type: ignore[call-arg]


@pytest.fixture
def db(tmp_path: Path) -> sqlite3.Connection:
    conn = connect(tmp_path / "arc.db")
    migrate(conn)
    return conn


def _write(
    conn: sqlite3.Connection, at: dt.datetime, ticker: str = "AAA", tier: Tier = Tier.DISCOVERY
) -> str:
    day = at.date()
    m = TierMember(
        ticker=ticker,
        tier=tier,
        rank=1,
        source="t",
        reason="x · score 0.80",
        as_of=day,
        score=0.8,
        score_today=0.8,
        runs=[day],
    )
    e = ContextStore(conn).write(
        kind="universe_tier",
        subject=tier.value,
        payload=UniverseTierPayload(tier=tier, members=[m], fetched_at=at, source="t"),
        produced_by="t",
        ttl="48h",
        valid_from=at,
        now=at,
    )
    conn.commit()
    return e.id


def _prev(conn: sqlite3.Connection, now: dt.datetime, cfg: CarryoverSettings, tier: Tier) -> Any:
    got = previous_entry(
        conn, tier, now=now, window_h=cfg.window_h, window_sessions=cfg.window_sessions
    )
    return None if got is None else got[0]


def test_calendar_fixtures_are_real_sessions() -> None:
    assert all(is_session(d.date()) for d in (THU, FRI, MON, TUE, GF_THU, GF_MON, LD_FRI, LD_TUE))
    assert not is_session(dt.date(2026, 4, 3))  # Good Friday
    assert not is_session(dt.date(2026, 9, 7))  # Labor Day


@pytest.mark.parametrize("tier", [Tier.DISCOVERY, Tier.TRENDING])
class TestSessionWindow:
    def test_monday_merges_friday(self, db: sqlite3.Connection, tier: Tier) -> None:
        fri = _write(db, FRI, tier=tier)
        assert _prev(db, MON, S1, tier) == fri
        assert _prev(db, MON, H48, tier) is None  # D64: 72 h > 48 h

    def test_tuesday_merges_monday(self, db: sqlite3.Connection, tier: Tier) -> None:
        _write(db, FRI, tier=tier)
        mon = _write(db, MON, tier=tier)
        assert _prev(db, TUE, S1, tier) == mon
        assert _prev(db, TUE, H48, tier) == mon

    def test_monday_after_friday_holiday_merges_thursday(
        self, db: sqlite3.Connection, tier: Tier
    ) -> None:
        thu = _write(db, GF_THU, tier=tier)
        assert _prev(db, GF_MON, S1, tier) == thu
        assert _prev(db, GF_MON, H48, tier) is None

    def test_tuesday_after_monday_holiday_merges_friday(
        self, db: sqlite3.Connection, tier: Tier
    ) -> None:
        fri = _write(db, LD_FRI, tier=tier)
        assert _prev(db, LD_TUE, S1, tier) == fri

    def test_older_than_one_session_is_out(self, db: sqlite3.Connection, tier: Tier) -> None:
        thu = _write(db, THU, tier=tier)  # Monday's previous session is Friday
        assert _prev(db, MON, S1, tier) is None
        assert _prev(db, MON, CarryoverSettings(window_sessions=2), tier) == thu

    def test_same_day_rerun_never_merges_today(self, db: sqlite3.Connection, tier: Tier) -> None:
        _write(db, MON, tier=tier)
        rerun = MON + dt.timedelta(hours=3)
        assert _prev(db, rerun, S1, tier) is None
        fri = _write(db, FRI, tier=tier)
        assert _prev(db, rerun, S1, tier) == fri

    def test_other_tier_ignored(self, db: sqlite3.Connection, tier: Tier) -> None:
        other = Tier.TRENDING if tier is Tier.DISCOVERY else Tier.DISCOVERY
        _write(db, FRI, tier=other)
        assert _prev(db, MON, S1, tier) is None


def test_apply_carryover_monday_merges_friday(db: sqlite3.Connection) -> None:
    fri = _write(db, FRI, ticker="OLD")
    today = TierMember(
        ticker="NEW",
        tier=Tier.DISCOVERY,
        rank=1,
        source="t",
        reason="y · score 0.50",
        as_of=MON.date(),
        score=0.5,
        score_today=0.5,
        runs=[MON.date()],
    )
    payload = UniverseTierPayload(tier=Tier.DISCOVERY, members=[today], fetched_at=MON, source="t")
    out, res = apply_carryover(db, payload, now=MON, cfg=CarryoverSettings(), size=25)
    assert out.merged_from == fri
    assert [m.ticker for m in res.carried] == ["OLD"]
    assert out.merge is not None
    assert out.merge.model_dump() == {"window_sessions": 1, "w_today": 0.6, "w_prev": 0.4}
    # round trip through the stored JSON
    again = UniverseTierPayload.model_validate_json(out.model_dump_json())
    assert again.merge == out.merge


class TestSettings:
    def test_default_is_one_session(self) -> None:
        assert CarryoverSettings().window_sessions == 1

    def test_zero_and_null_mean_off(self) -> None:
        assert CarryoverSettings(window_sessions=0).window_sessions is None
        assert CarryoverSettings(window_sessions=None).window_sessions is None
        assert CarryoverSettings(window_sessions=None).knobs() == CarryoverKnobs(
            window_h=48, w_today=0.6, w_prev=0.4
        )

    @pytest.mark.parametrize("bad", [-1, 4])
    def test_bounds(self, bad: int) -> None:
        with pytest.raises(ValueError):
            CarryoverSettings(window_sessions=bad)

    def test_old_d64_knobs_still_load(self) -> None:
        old = CarryoverKnobs.model_validate({"window_h": 48, "w_today": 0.6, "w_prev": 0.4})
        assert old.window_h == 48 and old.window_sessions is None


class TestRegistry:
    def test_tunable(self) -> None:
        t = REGISTRY["universe.carryover.window_sessions"]
        assert t.path == ("carryover", "window_sessions") and t.risk is Risk.UP
        assert (t.min, t.max, t.ref) == (0, 3, "D138")
        assert lookup("carryover.window_sessions") is t
        assert direction(t, 1, 2) is Direction.RISKIER
        assert direction(t, 1, 0) is Direction.SAFER

    def test_set_and_revert_reach_universe_config(self, tmp_path: Path) -> None:
        from arc.control.effective import effective_settings
        from arc.control.service import ControlService

        conn = connect(tmp_path / "arc.db")
        migrate(conn)
        conn.row_factory = sqlite3.Row
        owner = "U0OWNER"
        base = _settings(approver_slack_user_ids=[owner])
        now = _at(2026, 10, 26, 10)
        svc = ControlService(conn, base=base, now=lambda: now, is_halted=lambda: False)
        key = "universe.carryover.window_sessions"

        def eff() -> int | None:
            return universe_config(effective_settings(conn, base=base)).carryover.window_sessions

        assert eff() == 1
        r = svc.set(key, "0", actor=owner, source="slack")  # safer: applies at once
        assert r.outcome == "applied", r
        assert eff() is None
        r = svc.revert(key, actor=owner, source="slack")  # back to the file default 1
        if r.pending is not None:
            r = svc.confirm(r.pending.code, actor=owner, source="slack")
        assert r.outcome in ("applied", "reverted"), r
        assert eff() == 1
