"""D64 (E14.7): discovery + trending 48 h two-run carry-over.

Covers the pure merge (both / today-only / prev-only, never past 2 runs, tier cap),
prev selection (48 h strict window, earlier ET day only, Fri -> Mon no prev), a failed
run keeping the previous list for 48 h, the off switch, exclusions on carried names,
pre-D64 payloads (score parsed from ``reason``), the per-kind TTL override, the
registry knobs, the journal row, both writers end to end and the Tower row fields.
"""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING, Any
from unittest import mock

import pytest
from hypothesis import given
from hypothesis import settings as hsettings
from hypothesis import strategies as st

from arc.config import DEFAULT_UNIVERSE, ArcSettings
from arc.context.store import ContextStore
from arc.control.registry import REGISTRY, lookup, read_raw, write_raw
from arc.journal.reasons import REASON_LABELS, ReasonCode
from arc.routines.config import DEFAULT_ROUTINES_PATH, RoutinesConfig, load_routines
from arc.routines.handlers import JobContext, universe_trending_source
from arc.store.db import connect
from arc.store.migrate import migrate
from arc.tower.data_universe import UniverseActiveRow, _active_row
from arc.universe.carryover import (
    apply_carryover,
    journal_carried,
    merge_members,
    own_score,
    parse_reason_score,
    previous_entry,
)
from arc.universe.guard import UniverseGuard
from arc.universe.master import SymbolInfo, SymbolMaster
from arc.universe.screen import LiquidityMetrics, ScreenResult
from arc.universe.tiers import (
    CarryoverSettings,
    Tier,
    TierMember,
    UniverseTierPayload,
    load_tier_inputs,
)
from arc.utils.calendar import ET

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path

# Thu 2026-10-08 05:50 ET (trending slot); the next day is Fri, then Mon 10-12.
THU = dt.datetime(2026, 10, 8, 5, 50, tzinfo=ET)
THU_DAY = THU.date()
FRI = THU + dt.timedelta(days=1)
MON = THU + dt.timedelta(days=4)
ON = CarryoverSettings()
OFF = CarryoverSettings(enabled=False)


def _settings(**kw: Any) -> ArcSettings:
    return ArcSettings(_env_file=None, env="paper", **kw)  # type: ignore[call-arg]


@pytest.fixture
def db(tmp_path: Path) -> sqlite3.Connection:
    conn = connect(tmp_path / "arc.db")
    migrate(conn)
    return conn


def _m(
    ticker: str,
    rank: int,
    score: float | None,
    *,
    day: dt.date = THU_DAY,
    tier: Tier = Tier.TRENDING,
    reason: str | None = None,
    d64: bool = True,
) -> TierMember:
    base = reason if reason is not None else f"reddit #{rank}"
    kw: dict[str, Any] = {}
    if d64:
        kw = {"score": score, "score_today": score, "runs": [day] if score is not None else []}
    return TierMember(
        ticker=ticker,
        tier=tier,
        rank=rank,
        source="reddit",
        reason=f"{base} · score {score:.2f}" if score is not None else base,
        as_of=day,
        **kw,
    )


def _today(*rows: tuple[str, float], day: dt.date) -> list[TierMember]:
    return [_m(t, i, s, day=day) for i, (t, s) in enumerate(rows, 1)]


def _write(
    conn: sqlite3.Connection,
    members: list[TierMember],
    at: dt.datetime,
    *,
    tier: Tier = Tier.TRENDING,
    ttl: str = "48h",
) -> str:
    e = ContextStore(conn).write(
        kind="universe_tier",
        subject=tier.value,
        payload=UniverseTierPayload(tier=tier, members=members, fetched_at=at, source="t"),
        produced_by="t",
        ttl=ttl,
        valid_from=at,
        now=at,
    )
    conn.commit()
    return e.id


# -- pure merge -------------------------------------------------------------------------


class TestMerge:
    def test_both_today_only_prev_only(self) -> None:
        prev = _today(("AAA", 0.9), ("BBB", 0.5), day=THU.date())
        today = _today(("AAA", 0.4), ("CCC", 0.8), day=FRI.date())
        res = merge_members(today, prev, today_day=FRI.date(), prev_day=THU.date(), cfg=ON, size=25)
        by = {m.ticker: m for m in res.members}
        # both: 0.6 x 0.4 + 0.4 x 0.9 = 0.6; today only: 0.6 x 0.8 = 0.48; prev only 0.2
        assert by["AAA"].score == 0.6 and by["AAA"].score_prev == 0.9
        assert by["AAA"].runs == [THU.date(), FRI.date()]
        assert by["CCC"].score == 0.48 and by["CCC"].score_prev is None
        assert by["BBB"].score == 0.2 and by["BBB"].score_today is None
        assert [m.ticker for m in res.members] == ["AAA", "CCC", "BBB"]
        assert [m.rank for m in res.members] == [1, 2, 3]
        assert [m.ticker for m in res.carried] == ["BBB"]
        # reason keeps its details; the trailing score is now the combined score
        assert by["AAA"].reason == "reddit #1 · score 0.60"
        assert by["BBB"].reason == "reddit #2 · carried from 2026-10-08 · score 0.20"
        assert by["BBB"].as_of == FRI.date() and by["BBB"].runs == [THU.date()]

    def test_tie_break_today_rank_then_prev_rank_then_ticker(self) -> None:
        prev = _today(("ZZZ", 0.5), ("YYY", 0.5), day=THU.date())
        today = _today(("BBB", 0.5), ("AAA", 0.5), day=FRI.date())
        res = merge_members(today, prev, today_day=FRI.date(), prev_day=THU.date(), cfg=ON, size=25)
        # today-only 0.3 each (today rank order), prev-only 0.2 each (prev rank order)
        assert [m.ticker for m in res.members] == ["BBB", "AAA", "ZZZ", "YYY"]

    def test_never_chains_past_two_runs(self) -> None:
        d1, d2, d3 = THU.date(), FRI.date(), (FRI + dt.timedelta(days=1)).date()
        run1 = _today(("OLD", 0.9), day=d1)
        run2 = merge_members(
            _today(("NEW", 0.5), day=d2), run1, today_day=d2, prev_day=d1, cfg=ON, size=25
        ).members
        assert {m.ticker for m in run2} == {"OLD", "NEW"}
        old = next(m for m in run2 if m.ticker == "OLD")
        assert own_score(old) is None  # carried: no own score in run 2
        run3 = merge_members(
            _today(("X", 0.7), day=d3), run2, today_day=d3, prev_day=d2, cfg=ON, size=25
        ).members
        # OLD informed runs 1 and 2 only; run 3 uses run 2's own score for NEW (0.5)
        assert [m.ticker for m in run3] == ["X", "NEW"]
        assert next(m for m in run3 if m.ticker == "NEW").score == 0.2

    def test_cap_at_tier_size(self) -> None:
        prev = _today(*[(f"P{i:02d}", 0.9) for i in range(20)], day=THU.date())
        today = _today(*[(f"T{i:02d}", 0.9) for i in range(20)], day=FRI.date())
        res = merge_members(today, prev, today_day=FRI.date(), prev_day=THU.date(), cfg=ON, size=25)
        assert len(res.members) == 25
        assert [m.rank for m in res.members] == list(range(1, 26))
        assert all(m.ticker.startswith("T") for m in res.members[:20])
        assert len(res.cut) == 15

    def test_exclusions_reapplied_to_carried_names_only(self) -> None:
        prev = _today(("SPY", 0.9), ("AAA", 0.8), day=THU.date())
        today = _today(("SPY", 0.1), day=FRI.date())  # today's run decides about its own
        res = merge_members(
            today,
            prev,
            today_day=FRI.date(),
            prev_day=THU.date(),
            cfg=ON,
            size=25,
            excluded=lambda s: "market_reference" if s in {"SPY", "AAA"} else None,
        )
        assert [m.ticker for m in res.members] == ["SPY"]
        assert res.excluded == {"AAA": "market_reference"}

    def test_off_is_this_run_only_in_run_order(self) -> None:
        prev = _today(("AAA", 0.9), day=THU.date())
        today = _today(("BBB", 0.2), ("CCC", 0.9), day=FRI.date())
        res = merge_members(
            today, prev, today_day=FRI.date(), prev_day=THU.date(), cfg=OFF, size=25
        )
        assert [m.ticker for m in res.members] == ["BBB", "CCC"]
        assert [m.score for m in res.members] == [0.2, 0.9]
        assert all(m.score_prev is None for m in res.members) and not res.carried
        assert res.members[0].reason == today[0].reason

    def test_no_prev(self) -> None:
        today = _today(("BBB", 0.2), ("CCC", 0.9), day=FRI.date())
        res = merge_members(today, None, today_day=FRI.date(), prev_day=None, cfg=ON, size=25)
        assert [(m.ticker, m.score) for m in res.members] == [("CCC", 0.54), ("BBB", 0.12)]

    def test_pre_d64_payload_loads_and_scores_from_reason(self) -> None:
        legacy = UniverseTierPayload.model_validate(
            {
                "tier": "discovery",
                "members": [
                    {
                        "ticker": "TSM",
                        "tier": "discovery",
                        "rank": 1,
                        "source": "scout",
                        "reason": "bullish · youtube:arete · score 0.72",
                        "as_of": "2026-10-07",
                    }
                ],
                "fetched_at": "2026-10-07T06:00:00-04:00",
                "source": "scout",
            }
        )
        m = legacy.members[0]
        assert m.score is None and m.runs == [] and legacy.merged_from is None
        assert parse_reason_score(m.reason) == 0.72 == own_score(m)
        assert parse_reason_score("core list") is None
        res = merge_members(
            [], legacy.members, today_day=THU.date(), prev_day=(THU.date()), cfg=ON, size=25
        )
        assert res.members[0].score == 0.288 and res.members[0].score_prev == 0.72
        assert res.members[0].reason.startswith("bullish · youtube:arete · carried from")


@hsettings(max_examples=60, deadline=None)
@given(
    t=st.lists(st.floats(0, 1, allow_nan=False), max_size=12),
    p=st.lists(st.floats(0, 1, allow_nan=False), max_size=12),
    w=st.floats(0.5, 1.0),
    size=st.integers(0, 25),
)
def test_merge_properties(t: list[float], p: list[float], w: float, size: int) -> None:
    today = [_m(f"S{i}", i + 1, s, day=FRI.date()) for i, s in enumerate(t)]
    prev = [_m(f"S{i * 2}", i + 1, s, day=THU.date()) for i, s in enumerate(p)]
    cfg = CarryoverSettings(w_today=w)
    res = merge_members(today, prev, today_day=FRI.date(), prev_day=THU.date(), cfg=cfg, size=size)
    assert len(res.members) <= size
    assert [m.rank for m in res.members] == list(range(1, len(res.members) + 1))
    scores = [m.score or 0 for m in res.members]
    assert scores == sorted(scores, reverse=True)
    for m in res.members:
        exp = round(w * (m.score_today or 0) + (1 - w) * (m.score_prev or 0), 4)
        assert m.score == pytest.approx(exp, abs=1e-9)
        assert 1 <= len(m.runs) <= 2
    assert len({m.ticker for m in res.members}) == len(res.members)


# -- prev selection + store behaviour -----------------------------------------------------


class TestPrevious:
    def test_within_window_on_an_earlier_day(self, db: sqlite3.Connection) -> None:
        eid = _write(db, _today(("AAA", 0.9), day=THU.date()), THU)
        got = previous_entry(db, Tier.TRENDING, now=FRI, window_h=48)
        assert got is not None and got[0] == eid
        assert previous_entry(db, Tier.DISCOVERY, now=FRI, window_h=48) is None

    def test_fri_to_mon_strict_48h_has_no_prev(self, db: sqlite3.Connection) -> None:
        _write(db, _today(("AAA", 0.9), day=FRI.date()), FRI)
        assert previous_entry(db, Tier.TRENDING, now=MON, window_h=48) is None
        assert previous_entry(db, Tier.TRENDING, now=MON, window_h=96) is not None
        payload = UniverseTierPayload(
            tier=Tier.TRENDING,
            members=_today(("BBB", 0.5), day=MON.date()),
            fetched_at=MON,
            source="t",
        )
        out, res = apply_carryover(db, payload, now=MON, cfg=ON, size=25)
        assert [m.ticker for m in out.members] == ["BBB"] and out.merged_from is None
        assert out.merge is not None and out.merge.w_prev == 0.4 and not res.carried

    def test_same_day_rerun_merges_the_earlier_day_not_today(self, db: sqlite3.Connection) -> None:
        thu_id = _write(db, _today(("OLD", 0.9), day=THU.date()), THU)
        cfg_payload = UniverseTierPayload(
            tier=Tier.TRENDING,
            members=_today(("NEW", 0.5), day=FRI.date()),
            fetched_at=FRI,
            source="t",
        )
        first, _ = apply_carryover(db, cfg_payload, now=FRI, cfg=ON, size=25)
        _write(db, first.members, FRI)  # supersedes Thursday's entry
        rerun_at = FRI + dt.timedelta(hours=2)
        again, res = apply_carryover(
            db,
            cfg_payload.model_copy(update={"fetched_at": rerun_at}),
            now=rerun_at,
            cfg=ON,
            size=25,
        )
        assert again.merged_from == thu_id  # Thursday (superseded), never Friday's own
        assert [(m.ticker, m.score) for m in again.members] == [("OLD", 0.36), ("NEW", 0.3)]
        assert [m.ticker for m in res.carried] == ["OLD"]

    def test_failed_run_keeps_prev_for_48h_then_expires(self, db: sqlite3.Connection) -> None:
        settings = _settings()
        _write(db, _today(("AAA", 0.9), day=THU.date()), THU)
        # Friday's run fails: nothing written. Thursday's list still serves.
        assert [m.ticker for m in load_tier_inputs(db, settings, FRI).trending] == ["AAA"]
        just_before = THU + dt.timedelta(hours=47, minutes=59)
        assert [m.ticker for m in load_tier_inputs(db, settings, just_before).trending] == ["AAA"]
        after = load_tier_inputs(db, settings, THU + dt.timedelta(hours=48, minutes=1))
        assert after.trending == [] and Tier.TRENDING in after.expired_tiers

    def test_off_writes_this_run_only(self, db: sqlite3.Connection) -> None:
        _write(db, _today(("AAA", 0.9), day=THU.date()), THU)
        payload = UniverseTierPayload(
            tier=Tier.TRENDING,
            members=_today(("BBB", 0.5), day=FRI.date()),
            fetched_at=FRI,
            source="t",
        )
        out, res = apply_carryover(db, payload, now=FRI, cfg=OFF, size=25)
        assert [m.ticker for m in out.members] == ["BBB"]
        assert out.merged_from is None and out.merge is None and not res.carried


# -- journal ------------------------------------------------------------------------------


def test_journal_carried_once_per_day(db: sqlite3.Connection) -> None:
    assert ReasonCode.UNIVERSE_CARRIED_OVER.value == "universe:carried_over"
    assert ReasonCode.UNIVERSE_CARRIED_OVER in REASON_LABELS
    res = merge_members(
        _today(("NEW", 0.5), day=FRI.date()),
        _today(("OLD", 0.9), day=THU.date()),
        today_day=FRI.date(),
        prev_day=THU.date(),
        cfg=ON,
        size=25,
    )
    assert journal_carried(db, res, tier=Tier.TRENDING, at=FRI) == 1
    assert journal_carried(db, res, tier=Tier.TRENDING, at=FRI + dt.timedelta(hours=1)) == 0
    assert (
        journal_carried(db, res.model_copy(update={"carried": []}), tier=Tier.TRENDING, at=FRI) == 0
    )
    row = db.execute(
        "SELECT subject, choice, stage, payload FROM decisions WHERE reason_code = ?",
        ("universe:carried_over",),
    ).fetchone()
    assert row["subject"] == "OLD" and row["choice"] == "selected" and row["stage"] == "candidate"
    import json

    assert json.loads(row["payload"]) == {
        "tier": "trending",
        "score": 0.36,
        "score_prev": 0.9,
        "from": "2026-10-08",
    }


# -- config + registry --------------------------------------------------------------------


class TestConfig:
    def test_shipped_yaml(self) -> None:
        c = load_routines(DEFAULT_ROUTINES_PATH)
        assert c.universe.carryover == CarryoverSettings(enabled=True, window_h=48, w_today=0.6)
        for job in ("scout", "universe.trending"):
            pol = c.context_policy("universe_tier", job)
            assert pol.ttl is not None and pol.ttl.duration == dt.timedelta(hours=48), job
        # the Scout's override is per kind: its other writes keep the shared defaults
        assert c.context_policy("scout_read", "scout").ttl == c.context_ttl["scout_read"].ttl
        assert c.context_policy("candidate", "scout").ttl == c.context_ttl["candidate"].ttl
        # momentum keeps its own 35d; the shared default stays 1 session
        assert c.context_policy("universe_tier", "universe.momentum").ttl.duration == (  # type: ignore[union-attr]
            dt.timedelta(days=35)
        )
        assert c.context_ttl["universe_tier"].ttl.sessions == 1  # type: ignore[union-attr]

    def test_context_kinds_must_be_written(self) -> None:
        with pytest.raises(ValueError, match="not in this job's writes"):
            RoutinesConfig.model_validate(
                {
                    "sources": {
                        "x": {
                            "schedule": ["05:00"],
                            "writes": ["note"],
                            "context_kinds": {"universe_tier": {"ttl": "48h"}},
                        }
                    }
                }
            )
        with pytest.raises(ValueError, match="unknown context kind"):
            RoutinesConfig.model_validate(
                {
                    "sources": {
                        "x": {
                            "schedule": ["05:00"],
                            "writes": ["note"],
                            "context_kinds": {"nope": {"ttl": "48h"}},
                        }
                    }
                }
            )

    def test_bounds(self) -> None:
        with pytest.raises(ValueError):
            CarryoverSettings(w_today=0.4)
        with pytest.raises(ValueError):
            CarryoverSettings(window_h=100)
        assert CarryoverSettings(w_today=0.75).w_prev == 0.25

    def test_registry(self) -> None:
        from arc.control.effective import raw_yaml
        from arc.control.registry import Target

        raw = raw_yaml(Target.ROUTINES)
        assert read_raw(REGISTRY["universe.carryover.enabled"], raw) is True
        assert read_raw(REGISTRY["universe.carryover.window_h"], raw) == 48
        assert read_raw(REGISTRY["universe.carryover.w_today"], raw) == 0.6
        assert lookup("carryover.w_today").key == "universe.carryover.w_today"
        t = REGISTRY["universe.carryover.window_h"]
        assert (t.min, t.max) == (24, 96)
        assert write_raw(REGISTRY["universe.carryover.enabled"], False, raw) == [
            (("universe", "carryover", "enabled"), False)
        ]

    def test_override_applies(self) -> None:
        c = load_routines(
            DEFAULT_ROUTINES_PATH, overrides={("universe", "carryover", "enabled"): False}
        )
        assert c.universe.carryover.enabled is False


# -- writers end to end -------------------------------------------------------------------


def _master(*syms: str, names: dict[str, str] | None = None) -> SymbolMaster:
    names = names or {}
    return SymbolMaster(
        fetched_at=THU,
        symbols={
            s: SymbolInfo(
                symbol=s,
                name=names.get(s, f"{s} Inc"),
                exchange="NASDAQ",
                options=True,
                tradable=True,
                sources=["sec", "alpaca"],
            )
            for s in syms
        },
    )


def _buzz(reddit: list[str], stocktwits: list[str], at: dt.datetime) -> Any:
    from arc.context.kinds import RetailBuzzInput, RetailBuzzPayload, RetailBuzzRow

    def inp(type_: str, syms: list[str]) -> RetailBuzzInput:
        return RetailBuzzInput(
            type=type_,  # type: ignore[arg-type]
            status="ok",
            fetched_at=at.isoformat(),
            urls=[f"https://{type_}/1"],
            rows=[
                RetailBuzzRow(
                    symbol=s,
                    position=i,
                    rank=i,
                    mentions=float(100 - i),
                    rank_24h_ago=i,
                    trending_score=float(100 - i),
                )
                for i, s in enumerate(syms, 1)
            ],
        )

    return RetailBuzzPayload(
        as_of=at.isoformat(),
        session=at.date().isoformat(),
        inputs={"reddit": inp("apewisdom", reddit), "stocktwits": inp("stocktwits", stocktwits)},
    )


def _run_trending(
    db: sqlite3.Connection,
    routines: RoutinesConfig,
    now: dt.datetime,
    reddit: list[str],
    stocktwits: list[str],
    master: SymbolMaster,
) -> Any:
    at = now - dt.timedelta(minutes=10)
    ContextStore(db).write(
        kind="retail_buzz",
        subject="all",
        payload=_buzz(reddit, stocktwits, at),
        produced_by="retail_buzz",
        ttl="24h",
        valid_from=at,
        now=at,
    )
    db.commit()
    kind, spec = routines.step("universe.trending")
    ctx = JobContext(
        job="universe.trending",
        kind=kind,
        spec=spec,
        run_id=f"r-{now:%m%d}",
        chain_run_id=None,
        scheduled_for=now,
        now=now,
        conn=db,
        snapshot=ContextStore(db).snapshot(now, kinds=[]),
        routines=routines,
        settings_factory=_settings,
    )

    def screen(sym: str, _p: Any = None) -> ScreenResult:
        metrics = LiquidityMetrics(ticker=sym, as_of=now.date())
        return ScreenResult(ticker=sym, passed=True, failures=[], metrics=metrics)

    with (
        mock.patch("arc.universe.load_symbol_master", return_value=master),
        mock.patch.object(UniverseGuard, "screen", side_effect=screen),
    ):
        res = universe_trending_source(ctx)
    db.commit()
    return res


def _stored(db: sqlite3.Connection, subject: str) -> UniverseTierPayload:
    row = db.execute(
        "SELECT payload FROM context_entries WHERE kind='universe_tier' AND subject=? "
        "ORDER BY valid_from DESC, rowid DESC LIMIT 1",
        (subject,),
    ).fetchone()
    return UniverseTierPayload.model_validate_json(row[0])


class TestTrendingJob:
    SYMS = ("TEM", "AAA", "BBB", "CCC", "SOXL", *DEFAULT_UNIVERSE)

    def test_two_runs_merge_and_journal(self, db: sqlite3.Connection) -> None:
        routines = load_routines(DEFAULT_ROUTINES_PATH)
        master = _master(*self.SYMS)
        _run_trending(db, routines, THU, ["TEM", "AAA"], ["TEM", "BBB"], master)
        thu = _stored(db, "trending")
        assert thu.merged_from is None and all(m.runs == [THU.date()] for m in thu.members)
        res = _run_trending(db, routines, FRI, ["TEM", "CCC"], ["TEM"], master)
        fri = _stored(db, "trending")
        by = {m.ticker: m for m in fri.members}
        assert fri.merged_from is not None
        assert fri.merge is not None and fri.merge.w_today == 0.6
        assert by["TEM"].score_today is not None and by["TEM"].score_prev is not None
        assert by["TEM"].runs == [THU.date(), FRI.date()]
        assert by["AAA"].score_today is None and "carried from 2026-10-08" in by["AAA"].reason
        assert set(res.metrics["carried"]) == {"AAA", "BBB"}
        assert res.metrics["journaled"]["universe:carried_over"] == 2
        assert "2 carried" in res.notice
        scores = [m.score for m in fri.members]
        assert scores == sorted(scores, reverse=True)  # type: ignore[type-var]
        # the 48 h policy: Friday's entry expires 48 h after it was written
        exp = db.execute(
            "SELECT expires_at FROM context_entries WHERE kind='universe_tier' "
            "AND subject='trending' AND status='active'"
        ).fetchone()[0]
        from arc.context.ttl import from_db

        assert from_db(exp) == FRI + dt.timedelta(hours=48)
        # carried names are admitted no new trending row (only universe:carried_over)
        assert not db.execute(
            "SELECT 1 FROM decisions WHERE subject='AAA' AND reason_code="
            "'universe:trending_admitted' AND at >= ?",
            ("2026-10-09",),
        ).fetchone()

    def test_carried_leveraged_name_dropped(self, db: sqlite3.Connection) -> None:
        routines = load_routines(DEFAULT_ROUTINES_PATH)
        # Thursday SOXL slipped in (old master had no fund name); today it reads leveraged
        _run_trending(db, routines, THU, ["SOXL", "AAA"], ["AAA"], _master(*self.SYMS))
        lev = _master(*self.SYMS, names={"SOXL": "Direxion Daily Semiconductor Bull 3X ETF"})
        res = _run_trending(db, routines, FRI, ["TEM"], ["TEM"], lev)
        assert res.metrics["carry_excluded"] == {"SOXL": "leveraged"}
        assert "SOXL" not in {m.ticker for m in _stored(db, "trending").members}

    def test_off_switch(self, db: sqlite3.Connection) -> None:
        routines = load_routines(
            DEFAULT_ROUTINES_PATH, overrides={("universe", "carryover", "enabled"): False}
        )
        master = _master(*self.SYMS)
        _run_trending(db, routines, THU, ["TEM", "AAA"], ["TEM"], master)
        _run_trending(db, routines, FRI, ["CCC"], ["CCC"], master)
        fri = _stored(db, "trending")
        assert [m.ticker for m in fri.members] == ["CCC"]
        assert fri.merge is None and fri.merged_from is None


class TestScoutJob:
    def test_discovery_merges_previous_run(self, db: sqlite3.Connection) -> None:
        import tests.test_scout_persona as sp

        routines = load_routines(DEFAULT_ROUTINES_PATH)
        scout = routines.personas["scout"]
        cfg = sp._routines()
        cfg = cfg.model_copy(
            update={
                "personas": {
                    "scout": cfg.personas["scout"].model_copy(
                        update={"context_kinds": scout.context_kinds}
                    )
                },
                "universe": routines.universe,
            }
        )
        # yesterday's discovery entry (Scout NOW - 1 day): OKLO carried, RKLB in both
        prev_at = sp.NOW - dt.timedelta(days=1)
        prev = [
            TierMember(
                ticker=t,
                tier=Tier.DISCOVERY,
                rank=i,
                source="scout",
                reason=f"bullish · youtube:arete · score {s:.2f}",
                as_of=prev_at.date(),
            )
            for i, (t, s) in enumerate([("RKLB", 0.9), ("OKLO", 0.7), ("SPY", 0.8)], 1)
        ]
        _write(db, prev, prev_at, tier=Tier.DISCOVERY, ttl="48h")
        sp._seed(db)
        settings = sp._settings()
        ctx = sp._ctx(db, cfg, settings)
        sp.scout_persona(ctx, llm=sp.FakeLLM(), guard=sp._guard(db, settings, {"ZZLO"}))
        db.commit()
        pay = _stored(db, "discovery")
        by = {m.ticker: m for m in pay.members}
        assert set(by) == {"RKLB", "IONQ", "ASTS", "OKLO"}  # SPY excluded today
        assert by["RKLB"].score == round(0.6 * 0.8 + 0.4 * 0.9, 4)
        assert by["RKLB"].stance == "bullish" and by["RKLB"].origins
        assert by["OKLO"].score_today is None and by["OKLO"].score == 0.28
        assert pay.members[0].ticker == "RKLB"
        # carried names get no candidate row
        assert not db.execute("SELECT 1 FROM candidates WHERE ticker='OKLO'").fetchone()
        codes = {r[0] for r in db.execute("SELECT reason_code FROM decisions WHERE subject='OKLO'")}
        assert codes == {"universe:carried_over"}
        exp = db.execute(
            "SELECT expires_at FROM context_entries WHERE kind='universe_tier' "
            "AND subject='discovery' AND status='active'"
        ).fetchone()[0]
        from arc.context.ttl import from_db

        assert from_db(exp) == sp.NOW + dt.timedelta(hours=48)
        # the Scout's other kinds keep their own TTL (scout_read 24h)
        sr = db.execute(
            "SELECT expires_at FROM context_entries WHERE kind='scout_read'"
        ).fetchone()[0]
        assert from_db(sr) == sp.NOW + dt.timedelta(hours=24)


# -- Tower ---------------------------------------------------------------------------------


def test_tower_row_copies_member_fields() -> None:
    m = _m("AAA", 1, 0.5, day=FRI.date()).model_copy(
        update={
            "score_prev": 0.4,
            "runs": [THU.date(), FRI.date()],
            "stance": "bullish",
            "origins": ["youtube:arete"],
        }
    )
    row = _active_row(m)
    assert isinstance(row, UniverseActiveRow)
    assert (row.score, row.score_today, row.score_prev) == (0.5, 0.5, 0.4)
    assert row.runs == ["2026-10-08", "2026-10-09"]
    assert row.stance == "bullish" and row.origins == ["youtube:arete"]
    core = _active_row(
        TierMember(ticker="SPY", tier=Tier.CORE, rank=1, source="s", as_of=THU.date())
    )
    assert core.score is None and core.runs is None and core.origins is None
