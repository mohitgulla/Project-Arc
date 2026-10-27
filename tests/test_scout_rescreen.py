"""E13.7c (D138): ``scout.rescreen``, the RTH re-screen of the Scout's 06:00 screen drops.

Covers: only names dropped by the liquidity screen are re-screened (never the higher
tier / reference / floor / not-in-calls drops); the higher-tier and cap re-checks;
revived names join today's discovery tier through the Scout's writer (active list
re-resolved, the 06:00 entry's expiry); idempotent per ET day; a data error leaves the
tier unchanged; no ``scout_read`` today = skipped; the shipped job and its wiring.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import TYPE_CHECKING, Any
from unittest import mock

import pytest

from arc.config import DEFAULT_UNIVERSE, ArcSettings
from arc.context.kinds import ScoutReadPayload
from arc.context.store import ContextStore
from arc.experiments.runner import PERSONA_JOBS
from arc.journal.reasons import REASON_LABELS, ReasonCode
from arc.routines.config import DEFAULT_ROUTINES_PATH, RoutinesConfig, load_routines
from arc.routines.handlers import (
    BUILTIN_HANDLERS,
    JobContext,
    JobSkippedError,
    scout_persona,
    scout_rescreen,
)
from arc.universe.master import SymbolInfo, SymbolMaster
from arc.universe.tiers import Tier, UniverseTierPayload, tier_membership
from arc.utils.calendar import ET
from tests.test_scout_persona import (
    BRIEFS_OPTIONS,
    ILLIQUID,
    MOMENTUM,
    NOW,
    PASS,
    REPLY,
    FakeLLM,
    _call,
    _seed,
    _settings,
    _write_tier,
    db,  # noqa: F401 - fixture
)

if TYPE_CHECKING:
    import sqlite3

    from arc.universe.screen import LiquidityMetrics

RTH = NOW.replace(hour=10)  # 2026-10-06 10:00 ET (a Tuesday)
ERROR = PASS.model_copy(update={"price": None, "error": "APIError: 401 Authorization Required"})

_SCOUT_WRITES = ["scout_read", "candidate", "note", "universe_tier", "active_universe"]


@pytest.fixture(autouse=True)
def _stop_patches() -> Any:
    yield
    mock.patch.stopall()


def _routines(*, max_discovery: int = 25) -> RoutinesConfig:
    tier_policy = {"universe_tier": {"ttl": "48h", "supersede": "latest"}}
    return RoutinesConfig.model_validate(
        {
            "sources": {"youtube.briefs": {"schedule": ["02:00"], **BRIEFS_OPTIONS}},
            "personas": {
                "scout": {
                    "schedule": ["06:00"],
                    "persona": "scout",
                    "llm": True,
                    "writes": _SCOUT_WRITES,
                    "context_kinds": tier_policy,
                },
                "scout.rescreen": {
                    "schedule": ["10:00"],
                    "persona": "scout",
                    "llm": False,
                    "reads": ["scout_read", "universe_tier", "active_universe"],
                    "writes": ["universe_tier", "active_universe"],
                    "context_kinds": tier_policy,
                },
            },
            "context_ttl": {"scout_read": {"ttl": "24h", "supersede": "latest"}},
            "funnel": {"scout": {"max_discovery": max_discovery}},
        }
    )


def _ctx(
    conn: sqlite3.Connection,
    job: str,
    now: dt.datetime,
    routines: RoutinesConfig,
    settings: ArcSettings,
) -> JobContext:
    kind, spec = routines.step(job)
    return JobContext(
        job=job,
        kind=kind,
        spec=spec,
        run_id=f"run-{job}-{now:%H%M%S}",
        chain_run_id=None,
        scheduled_for=now,
        now=now,
        conn=conn,
        snapshot=ContextStore(conn).snapshot(now),
        routines=routines,
        settings_factory=lambda: settings,
    )


class Screens:
    """A universe guard whose market data comes from *metrics[ticker]* (PASS default);
    records every ticker it was asked to measure."""

    def __init__(self, metrics: dict[str, LiquidityMetrics] | None = None) -> None:
        self.metrics = metrics or {}
        self.asked: list[str] = []

    def guard(self, conn: sqlite3.Connection, settings: ArcSettings, now: dt.datetime) -> Any:
        from arc.universe.guard import UniverseGuard

        syms = {*DEFAULT_UNIVERSE, *MOMENTUM, "RKLB", "IONQ", "ASTS", "SOFI", "ZZLO", "QQLO"}
        g = UniverseGuard.from_settings(
            settings,
            now=now,
            conn=conn,
            master=SymbolMaster(
                fetched_at=now,
                symbols={
                    s: SymbolInfo(symbol=s, sources=["sec", "alpaca"], options=True, tradable=True)
                    for s in syms
                },
            ),
            market_factory=mock.MagicMock,
        )

        def fake(_market: Any, sym: str, **_: Any) -> LiquidityMetrics:
            self.asked.append(sym)
            return self.metrics.get(sym, PASS).model_copy(update={"ticker": sym})

        mock.patch("arc.universe.guard.measure_liquidity", side_effect=fake).start()
        return g


REPLY2 = {
    **REPLY,
    "ticker_calls": [*REPLY["ticker_calls"], _call("QQLO", 0.75)],
    "discovery": [*REPLY["discovery"], "QQLO"],
}


def _scout_0600(
    conn: sqlite3.Connection,
    routines: RoutinesConfig,
    settings: ArcSettings,
    bad: tuple[str, ...] = ("ZZLO", "QQLO"),
) -> ScoutReadPayload:
    _seed(conn)
    ctx = _ctx(conn, "scout", NOW, routines, settings)
    scr = Screens(dict.fromkeys(bad, ILLIQUID))
    scout_persona(ctx, llm=FakeLLM(REPLY2), guard=scr.guard(conn, settings, NOW))
    conn.commit()
    mock.patch.stopall()
    entry = ContextStore(conn).snapshot(NOW + dt.timedelta(minutes=1)).latest("scout_read")
    assert entry is not None
    return ScoutReadPayload.model_validate(entry.payload)


def _rescreen(
    conn: sqlite3.Connection,
    routines: RoutinesConfig,
    settings: ArcSettings,
    scr: Screens,
    now: dt.datetime = RTH,
) -> Any:
    ctx = _ctx(conn, "scout.rescreen", now, routines, settings)
    res = scout_rescreen(ctx, guard=scr.guard(conn, settings, now))
    conn.commit()
    mock.patch.stopall()
    return res


def _discovery(conn: sqlite3.Connection, at: dt.datetime) -> list[str]:
    entries = ContextStore(conn).query(as_of=at, kinds=["universe_tier"], subjects=["discovery"])
    payload = UniverseTierPayload.model_validate(entries[-1].payload)
    return [m.ticker for m in sorted(payload.members, key=lambda m: m.rank)]


def _tier_entries(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    return [
        (r[0], r[1])
        for r in conn.execute(
            "SELECT produced_by, status FROM context_entries "
            "WHERE kind = 'universe_tier' AND subject = 'discovery' ORDER BY rowid"
        )
    ]


def _rows(conn: sqlite3.Connection) -> list[tuple[str, str, str]]:
    return [
        (r[0], r[1], r[2])
        for r in conn.execute(
            "SELECT subject, reason_code, reason_text FROM decisions WHERE run_id LIKE "
            "'run-scout.rescreen-%' ORDER BY rowid"
        )
    ]


class TestRescreen:
    def test_revives_only_screen_drops(self, db: sqlite3.Connection) -> None:  # noqa: F811
        settings, routines = _settings(), _routines()
        read = _scout_0600(db, routines, settings)
        assert read.discovery == ["RKLB", "IONQ", "ASTS"]
        assert read.screened_out["ZZLO"].startswith("screened_out:")
        assert read.screened_out["QQLO"].startswith("screened_out:")
        scout_read_before = db.execute(
            "SELECT id, payload FROM context_entries WHERE kind = 'scout_read'"
        ).fetchall()

        scr = Screens({"QQLO": ILLIQUID})  # ZZLO passes in RTH, QQLO still fails
        res = _rescreen(db, routines, settings, scr)

        # only the two screen drops were measured: never LRCX (higher tier), SPY
        # (reference), SOFI (below floor), PLTR / OKLO (not in calls)
        assert scr.asked == ["ZZLO", "QQLO"]
        assert res.summary == "rescreen 2 dropped → +1 admitted (ZZLO) · discovery 4/25"
        assert res.metrics["admitted"] == ["ZZLO"] and res.metrics["failed"] == ["QQLO"]
        later = RTH + dt.timedelta(minutes=1)
        assert _discovery(db, later) == ["ZZLO", "RKLB", "IONQ", "ASTS"]  # score order
        tiers = tier_membership(db, settings, later)
        assert tiers["ZZLO"] is Tier.DISCOVERY and "QQLO" not in tiers
        active = ContextStore(db).query(as_of=later, kinds=["active_universe"])[-1]
        assert "ZZLO" in json.dumps(active.payload)
        assert active.produced_by == "scout.rescreen"
        assert _tier_entries(db) == [("scout", "superseded"), ("scout.rescreen", "active")]

        rows = _rows(db)
        assert ("ZZLO", "scout_discovery_rescreened") in {(s, c) for s, c, _ in rows}
        q = [t for s, c, t in rows if s == "QQLO"]
        assert len(q) == 1 and q[0].startswith("rescreen: screened_out: ") and "ADV" in q[0]
        # the 06:00 record is untouched
        assert (
            db.execute(
                "SELECT id, payload FROM context_entries WHERE kind = 'scout_read'"
            ).fetchall()
            == scout_read_before
        )

    def test_same_expiry_as_the_scout_entry(self, db: sqlite3.Connection) -> None:  # noqa: F811
        settings, routines = _settings(), _routines()
        _scout_0600(db, routines, settings)
        _rescreen(db, routines, settings, Screens())
        exp = [
            r[0]
            for r in db.execute(
                "SELECT expires_at FROM context_entries WHERE kind = 'universe_tier' "
                "AND subject = 'discovery' ORDER BY rowid"
            )
        ]
        assert len(exp) == 2 and exp[0] == exp[1]

    def test_idempotent(self, db: sqlite3.Connection) -> None:  # noqa: F811
        settings, routines = _settings(), _routines()
        _scout_0600(db, routines, settings)
        _rescreen(db, routines, settings, Screens({"QQLO": ILLIQUID}))
        rows, members = _rows(db), _discovery(db, RTH + dt.timedelta(minutes=1))
        n_entries = len(_tier_entries(db))

        again = RTH + dt.timedelta(minutes=20)  # a manual re-run
        res = _rescreen(db, routines, settings, Screens({"QQLO": ILLIQUID}), now=again)
        assert _discovery(db, again + dt.timedelta(minutes=1)) == members
        assert sorted(r[:2] for r in _rows(db)) == sorted(r[:2] for r in rows)
        assert res.summary == "rescreen 2 dropped → +0 admitted · discovery 4/25"
        assert len(_tier_entries(db)) == n_entries  # nothing new admitted = no write

    def test_respects_higher_tier(self, db: sqlite3.Connection) -> None:  # noqa: F811
        settings, routines = _settings(), _routines()
        _scout_0600(db, routines, settings)
        _write_tier(db, Tier.MOMENTUM, [*MOMENTUM, "ZZLO"])  # ZZLO joined momentum since
        db.commit()
        scr = Screens()
        res = _rescreen(db, routines, settings, scr)
        assert scr.asked == ["QQLO"]
        assert res.metrics["admitted"] == ["QQLO"]
        texts = {s: t for s, _, t in _rows(db)}
        assert texts["ZZLO"] == "rescreen: in_higher_tier: momentum"
        assert "ZZLO" not in _discovery(db, RTH + dt.timedelta(minutes=1))

    def test_respects_the_discovery_cap(self, db: sqlite3.Connection) -> None:  # noqa: F811
        settings, routines = _settings(), _routines(max_discovery=4)
        _scout_0600(db, routines, settings)  # 3 of 4 used at 06:00
        scr = Screens()
        res = _rescreen(db, routines, settings, scr)
        assert scr.asked == ["ZZLO"]  # QQLO hits the cap before any market data
        assert res.metrics["admitted"] == ["ZZLO"]
        assert res.summary.endswith("discovery 4/4")
        texts = {s: t for s, _, t in _rows(db)}
        assert texts["QQLO"] == "rescreen: over_max_discovery"

    def test_data_error_leaves_the_tier_unchanged(self, db: sqlite3.Connection) -> None:  # noqa: F811
        settings, routines = _settings(), _routines()
        _scout_0600(db, routines, settings)
        before = _tier_entries(db)
        res = _rescreen(db, routines, settings, Screens({"ZZLO": ERROR, "QQLO": ERROR}))
        assert res.metrics["admitted"] == [] and res.metrics["warning"] is True
        assert res.summary.endswith("· data error (QQLO, ZZLO)")
        assert _tier_entries(db) == before
        assert _discovery(db, RTH + dt.timedelta(minutes=1)) == ["RKLB", "IONQ", "ASTS"]
        assert all("401" in t for _, _, t in _rows(db))

    def test_no_scout_read_today_skips(self, db: sqlite3.Connection) -> None:  # noqa: F811
        settings, routines = _settings(), _routines()
        with pytest.raises(JobSkippedError, match="no scout_read today"):
            _rescreen(db, routines, settings, Screens())
        _scout_0600(db, routines, settings)
        tomorrow = RTH + dt.timedelta(days=1)  # Wednesday: Tuesday's read is not today's
        with pytest.raises(JobSkippedError, match="no scout_read today"):
            _rescreen(db, routines, settings, Screens(), now=tomorrow)

    def test_nothing_dropped_is_a_quiet_ok(self, db: sqlite3.Connection) -> None:  # noqa: F811
        settings, routines = _settings(), _routines()
        _scout_0600(db, routines, settings, bad=())
        before = _tier_entries(db)
        scr = Screens()
        res = _rescreen(db, routines, settings, scr)
        assert res.summary == "rescreen 0 dropped → +0 admitted · discovery 5/25"
        assert scr.asked == [] and _tier_entries(db) == before


class TestWiring:
    def test_shipped_job(self) -> None:
        c = load_routines(DEFAULT_ROUTINES_PATH)
        kind, spec = c.step("scout.rescreen")
        assert kind == "persona" and spec.enabled and not spec.llm
        assert [t.strftime("%H:%M") for t in spec.schedule] == ["10:00"]
        assert spec.ttl is not None and spec.ttl.duration <= dt.timedelta(hours=2)
        assert not spec.after_sources and spec.catch_up is None
        assert set(spec.writes or []) == {"universe_tier", "active_universe"}
        assert "scout_read" in (spec.reads or [])
        assert c.context_policy("universe_tier", "scout.rescreen") == c.context_policy(
            "universe_tier", "scout"
        )
        assert BUILTIN_HANDLERS["scout.rescreen"] == "arc.routines.handlers:scout_rescreen"

    def test_reason_label_and_arm_jobs(self) -> None:
        assert ReasonCode.SCOUT_DISCOVERY_RESCREENED.value == "scout_discovery_rescreened"
        assert REASON_LABELS[ReasonCode.SCOUT_DISCOVERY_RESCREENED]
        # an arm owning the Scout runs its re-screen too (never synced from control)
        assert PERSONA_JOBS["scout"] == ("scout", "scout.rescreen")

    def test_rth_time(self) -> None:
        assert RTH.tzinfo is ET and RTH.hour == 10
