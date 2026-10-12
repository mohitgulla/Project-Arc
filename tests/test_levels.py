"""E23.0 (D98): the pre-market ``levels`` job, the regime merge and the ``tech`` render."""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING, Any

import pytest

from arc.account_profiles import DteSleeve
from arc.config import ArcSettings
from arc.context import ContextStore
from arc.context.kinds import KINDS, RegimePayload, validate_payload
from arc.data.base import HistoryBar, OptionContract
from arc.features.levels import LevelsPayload
from arc.levels.refresh import (
    CHAIN_CALLS,
    level_tickers,
    profile_sessions,
    refresh_levels,
    swing_window,
)
from arc.personas.builders import (
    build_research_prompt,
    research_input_from_context,
    tech_segment,
)
from arc.routines.config import (
    OiWallsSettings,
    RoutinesConfig,
    VolumeProfileSettings,
    load_routines,
)
from arc.routines.handlers import BUILTIN_HANDLERS, JobContext, JobSkippedError, levels_source
from arc.store.db import connect
from arc.store.migrate import migrate
from arc.utils.calendar import ET, previous_session

if TYPE_CHECKING:
    import sqlite3

DAY = dt.date(2026, 10, 12)  # Monday
NOW = dt.datetime.combine(DAY, dt.time(7, 20), ET)
VP = VolumeProfileSettings(enabled=True)
OI = OiWallsSettings(enabled=True, min_oi=500)
SLEEVES = [
    DteSleeve(name="short", dte_min=10, dte_max=21),
    DteSleeve(name="swing", dte_min=30, dte_max=60),
]


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = connect(":memory:")
    migrate(c)
    return c


class _Market:
    """15-min RTH bars around 100 (heaviest at 100-101) plus pre/after-hours noise."""

    def __init__(self, fail: frozenset[str] = frozenset(), chain_fail: bool = False) -> None:
        self.bar_calls: list[tuple[str, dt.date, dt.date, str, str | None]] = []
        self.chain_calls: list[tuple[str, dt.date, dt.date]] = []
        self.fail = fail
        self.chain_fail = chain_fail

    def history_bars(self, symbol, start, end, timeframe="1Day", feed=None):  # noqa: ANN001, ANN201
        self.bar_calls.append((symbol, start, end, timeframe, feed))
        if symbol in self.fail:
            raise RuntimeError("bars down")
        out: list[HistoryBar] = []
        d = start
        while d <= end:
            if d.weekday() < 5:  # noqa: PLR2004
                for k, (h, m) in enumerate([(4, 0), (9, 30), (10, 0), (12, 0), (15, 45), (17, 0)]):
                    ts = dt.datetime.combine(d, dt.time(h, m), ET)
                    lo = 100.0 + (0.0 if k in (1, 2, 3) else 3.0)
                    vol = 10_000.0 if k in (1, 2, 3) else 1_000.0
                    if k in (0, 5):  # extended hours: a huge print far away
                        lo, vol = 150.0, 1e9
                    out.append(HistoryBar(timestamp=ts, open=lo, high=lo + 1, low=lo,
                                          close=lo + 0.5, volume=vol))  # fmt: skip
            d += dt.timedelta(days=1)
        return out

    def option_chain(self, underlying, exp_start, exp_end, **kw):  # noqa: ANN001, ANN003, ANN201
        self.chain_calls.append((underlying, exp_start, exp_end))
        if self.chain_fail:
            raise RuntimeError("chain down")
        exp = exp_start + dt.timedelta(days=5)

        def c(strike: float, kind: str, oi: int | None) -> OptionContract:
            return OptionContract(symbol=f"{underlying}{strike}{kind[0]}", underlying=underlying,
                                  expiration=exp, strike=strike, option_type=kind,
                                  open_interest=oi)  # fmt: skip

        return [c(110, "call", 4000), c(105, "call", 900), c(95, "put", 3000), c(90, "put", 100)]


def test_tickers_sessions_and_swing_window() -> None:
    assert level_tickers(["nvda", "SPY", "NVDA"]) == ["NVDA", "SPY", "QQQ", "IWM"]
    s = profile_sessions(DAY, 20)
    assert len(s) == 20 and s[-1] == previous_session(DAY) and DAY not in s
    assert s == sorted(s)
    assert swing_window(SLEEVES, DAY) == (DAY + dt.timedelta(30), DAY + dt.timedelta(60))
    other = [
        DteSleeve(name="a", dte_min=7, dte_max=14),
        DteSleeve(name="b", dte_min=20, dte_max=45),
    ]
    assert swing_window(other, DAY) == (DAY + dt.timedelta(20), DAY + dt.timedelta(45))
    assert swing_window([], DAY) is None


def test_refresh_profiles_walls_feed_and_budget() -> None:
    m = _Market()
    taken: list[int] = []
    res = refresh_levels(m, ["NVDA"], DAY, vp_cfg=VP, oi_cfg=OI, sleeves=SLEEVES, feed="sip",
                         take=lambda: taken.append(1))  # fmt: skip
    assert [p.ticker for p in res.payloads] == ["NVDA", "SPY", "QQQ", "IWM"]
    # one bars request (SIP, 15Min, the 20 sessions before DAY) + one chain per ticker
    sessions = profile_sessions(DAY, 20)
    assert {c[1:] for c in m.bar_calls} == {(sessions[0], sessions[-1], "15Min", "sip")}
    assert len(m.chain_calls) == 4
    assert {c[1:] for c in m.chain_calls} == {(DAY + dt.timedelta(30), DAY + dt.timedelta(60))}
    assert res.calls == len(taken) == 4 * (1 + CHAIN_CALLS)
    p = res.payloads[0]
    vp = p.volume_profile
    assert vp is not None and vp.feed == "sip" and vp.sessions == 20 and vp.bar == "15Min"
    assert vp.bars == 20 * 4  # extended-hours bars dropped
    assert 100.0 <= vp.poc <= 101.0 and vp.vah < 150  # the after-hours print is ignored
    assert vp.first_session == sessions[0] and vp.last_session == sessions[-1]
    w = p.oi_walls
    assert w is not None and w.spot == pytest.approx(103.5)  # last RTH close
    assert (w.call_wall, w.put_wall) == (110, 95)  # 105 call and 90 put below min_oi
    assert p.missing == []
    m2 = res.metrics()
    assert (m2["profiles"], m2["oi_walls"], m2["errors"]) == (4, 4, 0)
    assert res.summary().startswith("levels for 4 tickers (2026-10-12) · 4 volume profiles")


def test_refresh_parts_off_errors_and_runtime_cap() -> None:
    m = _Market(fail=frozenset({"BAD"}), chain_fail=True)
    res = refresh_levels(m, ["BAD", "NVDA"], DAY, vp_cfg=VP, oi_cfg=OI, sleeves=SLEEVES,
                         feed="sip")  # fmt: skip
    assert set(res.errors) == {"BAD"} and "bars down" in res.errors["BAD"]
    nv = res.payloads[0]
    assert nv.volume_profile is not None and nv.oi_walls is None
    assert nv.missing == ["oi_walls: chain down"]
    assert res.summary().endswith("· 1 errors")
    # OI walls off: no chain call
    m = _Market()
    res = refresh_levels(m, ["NVDA"], DAY, vp_cfg=VP, oi_cfg=OiWallsSettings(enabled=False),
                         sleeves=SLEEVES, feed="sip")  # fmt: skip
    assert m.chain_calls == [] and all(p.oi_walls is None for p in res.payloads)
    # profile off: bars still give the walls' spot, no profile stored
    res = refresh_levels(_Market(), ["NVDA"], DAY, vp_cfg=VolumeProfileSettings(enabled=False),
                         oi_cfg=OI, sleeves=SLEEVES, feed="sip")  # fmt: skip
    assert res.payloads[0].volume_profile is None and res.payloads[0].oi_walls is not None
    # no sleeve
    res = refresh_levels(_Market(), ["NVDA"], DAY, vp_cfg=VP, oi_cfg=OI, sleeves=[], feed=None)
    assert res.payloads[0].missing == ["oi_walls: no entry sleeve"]
    # runtime cap: the clock jumps past the cap after the first ticker
    ticks = iter([0.0, 0.0, 1000.0, 1000.0, 1000.0, 1000.0])
    res = refresh_levels(_Market(), ["NVDA"], DAY, vp_cfg=VP, oi_cfg=OI, sleeves=SLEEVES,
                         feed="sip", max_runtime_s=900, clock=lambda: next(ticks))  # fmt: skip
    assert [p.ticker for p in res.payloads] == ["NVDA"]
    assert res.not_started == ["SPY", "QQQ", "IWM"]
    assert "3 not started (runtime cap)" in res.summary()


def test_refresh_no_bars_records_missing() -> None:
    class _Empty(_Market):
        def history_bars(self, symbol, start, end, timeframe="1Day", feed=None):  # noqa: ANN001, ANN201
            return []

    res = refresh_levels(_Empty(), [], DAY, vp_cfg=VP, oi_cfg=OI, sleeves=SLEEVES, feed="sip")
    p = res.payloads[0]
    assert p.volume_profile is None and p.oi_walls is None
    assert p.missing == ["volume_profile: no regular-hours bars", "oi_walls: no reference close"]


# ---------------------------------------------------------------------------
# Handler + shipped job
# ---------------------------------------------------------------------------


def _ctx(conn: sqlite3.Connection, *, vp: str = "on", oi: str = "on") -> JobContext:
    routines = RoutinesConfig.model_validate(
        {
            "sources": {
                "levels": {"schedule": ["07:20"], "writes": ["levels"], "reference": True,
                           "tickers": ["NVDA"]},
            },
            "volume_profile": {"enabled": vp},
            "oi_walls": {"enabled": oi},
        }
    )  # fmt: skip
    kind, spec = routines.step("levels")
    return JobContext(
        job="levels", kind=kind, spec=spec, run_id="run-1", chain_run_id=None,
        scheduled_for=NOW, now=NOW, conn=conn, snapshot=ContextStore(conn).snapshot(NOW),
        routines=routines, settings_factory=ArcSettings,
    )  # fmt: skip


def test_levels_handler_writes_one_entry_per_ticker(conn: sqlite3.Connection) -> None:
    res = levels_source(_ctx(conn), market=_Market(), take=lambda: None)
    assert res.metrics["tickers"] == 4 and res.metrics["profiles"] == 4
    snap = ContextStore(conn).snapshot(NOW)
    e = snap.latest("levels", "NVDA")
    assert e is not None and e.payload["as_of"] == DAY.isoformat()
    assert LevelsPayload.model_validate(e.payload).volume_profile is not None
    assert {t for t in ("NVDA", "SPY", "QQQ", "IWM") if snap.latest("levels", t)} == {
        "NVDA", "SPY", "QQQ", "IWM"
    }  # fmt: skip


def test_levels_handler_both_off_skips_and_total_failure_raises(
    conn: sqlite3.Connection,
) -> None:
    m = _Market()
    with pytest.raises(JobSkippedError, match="off"):
        levels_source(_ctx(conn, vp="off", oi="off"), market=m, take=lambda: None)
    assert m.bar_calls == [] and m.chain_calls == []
    down = _Market(fail=frozenset({"NVDA", "SPY", "QQQ", "IWM"}))
    with pytest.raises(RuntimeError, match="no levels computed"):
        levels_source(_ctx(conn), market=down, take=lambda: None)


def test_shipped_levels_job_and_switches() -> None:
    r = load_routines()
    assert r.volume_profile == VolumeProfileSettings(enabled=True)
    assert r.oi_walls == OiWallsSettings(enabled=True, min_oi=500)
    assert BUILTIN_HANDLERS["levels"] == "arc.routines.handlers:levels_source"
    assert KINDS["levels"].model is LevelsPayload
    jobs = r.jobs()
    _, spec = jobs["levels"]
    slot = spec.schedule[0]
    assert spec.writes == ["levels"]
    # after every pre-market active-list writer incl. the Scout / Trending catch-up ...
    writers = {
        n: j
        for n, (_, j) in jobs.items()
        if "active_universe" in (j.writes or []) and j.schedule and min(j.schedule) < slot
    }
    assert {"symbols", "universe.trending", "scout"} <= set(writers)
    for name, j in writers.items():
        assert max(j.schedule) < slot, name
        if name in ("scout", "universe.trending"):  # same catch-up rule as betas (D90)
            assert j.ttl is not None and j.ttl.duration is not None
            assert (dt.datetime.combine(DAY, max(j.schedule)) + j.ttl.duration).time() <= slot
    # ... and its catch-up ends before the research loop merges it into regime
    research = jobs["research"][1]
    assert spec.ttl is not None and spec.ttl.duration is not None
    assert research.window is not None and "levels" in (research.reads or [])
    assert (dt.datetime.combine(DAY, slot) + spec.ttl.duration).time() <= research.window.start
    off = load_routines(overrides={("volume_profile", "enabled"): "off",
                                   ("oi_walls", "enabled"): "off"})  # fmt: skip
    assert not off.volume_profile.enabled and not off.oi_walls.enabled


def test_registry_keys() -> None:
    from arc.control.registry import Target, lookup

    for key in ("volume_profile.enabled", "volume_profile.sessions", "volume_profile.bar",
                "volume_profile.value_area", "volume_profile.bin_atr",
                "volume_profile.hvn_min_share", "oi_walls.enabled", "oi_walls.min_oi"):  # fmt: skip
        assert lookup(key).target is Target.STRATEGY
    assert lookup("oi_walls.enabled").choices == ("on", "off")


def test_routines_yaml_refuses_the_strategy_blocks(tmp_path: Any) -> None:
    from arc.routines.strategy import MOVED_BLOCKS

    assert {"volume_profile", "oi_walls"} <= set(MOVED_BLOCKS)


# ---------------------------------------------------------------------------
# Regime merge (the research step) + render
# ---------------------------------------------------------------------------


def _tech(close: float = 180.0) -> dict[str, Any]:
    return {"as_of": "2026-10-12", "bar_date": "2026-10-09", "close": close, "atr14": 4.0,
            "rsi14": 55.0}  # fmt: skip


def _regime(close: float = 180.0) -> dict[str, Any]:
    from tests.test_features_technicals import _payload

    p = _payload()
    p["as_of"] = DAY.isoformat()
    p["technicals"] = {**p["technicals"], "close": close, "atr14": 4.0}
    return p


def _levels(as_of: dt.date = DAY) -> LevelsPayload:
    from tests.test_oi_walls import _levels as lv

    return lv().model_copy(update={"as_of": as_of, "ticker": "NVDA"})


def _store(conn: sqlite3.Connection, kind: str, subject: str, payload: Any) -> None:
    ContextStore(conn).write(kind=kind, subject=subject, payload=payload, produced_by="test",
                             run_id="r", now=NOW)  # fmt: skip


def _research_ctx(conn: sqlite3.Connection, *, vp: str = "on", oi: str = "on") -> JobContext:
    routines = load_routines(
        overrides={("volume_profile", "enabled"): vp, ("oi_walls", "enabled"): oi}
    )
    kind, spec = routines.step("research")
    later = NOW.replace(hour=9, minute=45)
    return JobContext(
        job="research", kind=kind, spec=spec, run_id="run-r", chain_run_id=None,
        scheduled_for=later, now=later, conn=conn,
        snapshot=ContextStore(conn).snapshot(later), routines=routines,
        settings_factory=lambda: ArcSettings(regime_model="v1"),  # type: ignore[call-arg]
    )  # fmt: skip


def test_with_levels_merges_today_only_and_respects_switches(conn: sqlite3.Connection) -> None:
    from arc.pipeline.steps import _with_levels

    reg = _regime()
    _store(conn, "levels", "NVDA", _levels())
    merged = _with_levels(_research_ctx(conn), "NVDA", reg, DAY)
    assert merged is not None
    t = merged["technicals"]
    assert t["vp_poc"] == 182.4 and t["oi_call_wall"] == 190.0
    assert t["dist_poc_atr"] == pytest.approx(-0.6)
    assert isinstance(validate_payload("regime", merged), RegimePayload)
    # already merged -> nothing to rewrite
    assert _with_levels(_research_ctx(conn), "NVDA", merged, DAY) is None
    # both off: never merged (byte-identical)
    assert _with_levels(_research_ctx(conn, vp="off", oi="off"), "NVDA", reg, DAY) is None
    only_oi = _with_levels(_research_ctx(conn, vp="off"), "NVDA", reg, DAY)
    assert only_oi is not None and only_oi["technicals"]["vp_poc"] is None
    # yesterday's levels / no levels / no technicals
    assert _with_levels(_research_ctx(conn), "NVDA", reg, DAY + dt.timedelta(1)) is None
    assert _with_levels(_research_ctx(conn), "AAPL", reg, DAY) is None
    assert _with_levels(_research_ctx(conn), "NVDA", {**reg, "technicals": None}, DAY) is None


def test_regime_entries_merge_levels_into_todays_rows(conn: sqlite3.Connection) -> None:
    """A regime row written before the levels landed gets them merged in place."""
    from arc.pipeline.steps import _regime_entries

    _store(conn, "regime", "NVDA", RegimePayload.model_validate(_regime()))
    _store(conn, "levels", "NVDA", _levels())

    class _NoNet:
        def history_bars(self, *a: Any, **k: Any) -> Any:
            raise AssertionError("no network for today's rows")

    from arc.pipeline.env import PipelineEnv

    env = PipelineEnv(market=_NoNet(), account=lambda: None, positions=list, llms={})  # type: ignore[arg-type]
    ctx = _research_ctx(conn)
    written = _regime_entries(ctx, env, ["NVDA"])
    assert written == ["NVDA"]
    later = NOW.replace(hour=9, minute=46)
    row = ContextStore(conn).snapshot(later).latest("regime", "NVDA")
    assert row is not None and row.payload["technicals"]["vp_poc"] == 182.4


def test_tech_segment_appends_levels_and_is_unchanged_without_them() -> None:
    from tests.test_features_technicals import _payload

    base = _payload()["technicals"]
    plain = tech_segment(base)
    nulls = {k: None for k in ("vp_poc", "vp_vah", "vp_val", "oi_call_wall", "oi_put_wall")}
    assert tech_segment({**base, **nulls}) == plain  # off / pre-E23.0: byte-identical
    lv = {**base, "vp_poc": 182.4, "vp_vah": 186.1, "vp_val": 178.9, "vp_pos": 0.31,
          "dist_poc_atr": -0.6, "oi_call_wall": 190.0, "oi_put_wall": 175.0}  # fmt: skip
    assert tech_segment(lv) == (
        plain + " · vp POC 182.4 (-0.6 ATR) VA 178.9–186.1 pos 0.31 · OI walls C190 / P175"
    )
    assert tech_segment({**base, "oi_put_wall": 22.5}) == plain + " · OI walls P22.5"
    small = {**base, "vp_poc": 24.37, "vp_vah": 25.0, "vp_val": 23.9}
    assert tech_segment(small) == plain + " · vp POC 24.37 VA 23.90–25.00"


def test_research_prompt_legend_only_with_levels(conn: sqlite3.Connection) -> None:
    from tests.test_features_technicals import _payload

    p = _payload()
    _store(conn, "regime", "NVDA", RegimePayload.model_validate(p))
    snap = ContextStore(conn).snapshot(NOW)
    kw: dict[str, Any] = {"portfolio_summary": "Equity $25,000.", "scan_date": "2026-10-12",
                          "compact": True, "technicals": True}  # fmt: skip
    before = build_research_prompt(research_input_from_context(snap, **kw))
    off = build_research_prompt(research_input_from_context(snap, **kw, levels=False))
    on = build_research_prompt(research_input_from_context(snap, **kw, levels=True))
    assert off == before and "vp = 20-session volume profile" not in off
    assert "vp = 20-session volume profile" in on
