"""E16.4 (D76): the daily market-health read (VIX/VVIX vs history, put/call, breadth).

Covers the pure math (percentile, rolling mean, breadth), the label truth table, the
walk-forward property (a value never depends on data after ``as_of``), staleness (a
stale input is ``None`` + named in ``missing``, never carried forward), the rendered
Research line, the flag-off byte identity of the Research prompt, the job end to end
on a scratch store, the resumable put/call backfill and the config / registry wiring.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
import yaml
from hypothesis import given, settings
from hypothesis import strategies as st

from arc.config import ArcSettings
from arc.context import ContextStore
from arc.context.categories import REFERENCE_KINDS, kind_category
from arc.context.kinds import KINDS, RegimePayload
from arc.control.registry import REGISTRY, lookup
from arc.features import market_health as mh
from arc.features.market_health import (
    HealthThresholds,
    IndexClose,
    IndexHistoryPayload,
    MarketHealthPayload,
    MarketTerm,
    PcHistoryPayload,
    PcPoint,
    breadth_from_technicals,
    compute_market_health,
    health_labels,
    market_health_line,
    merge_pc_points,
    percentile_prior,
    rolling_mean,
)
from arc.ingest import market_health as mhi
from arc.ingest.cboe_daily import NotPublishedError
from arc.personas.builders import build_research_prompt, research_input_from_context
from arc.pipeline.steps import RESEARCH_READS
from arc.routines.config import DEFAULT_ROUTINES_PATH, RoutinesConfig, load_routines
from arc.routines.handlers import JobContext, JobSkippedError, market_health_source
from arc.store.db import connect
from arc.store.migrate import migrate
from arc.utils.calendar import ET, sessions_between

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterator

REPO = Path(__file__).resolve().parents[1]
DAY = dt.date(2026, 10, 9)  # a Friday session
EVENING = dt.datetime(2026, 10, 9, 23, 15, tzinfo=ET)
MORNING = dt.datetime(2026, 10, 12, 8, 20, tzinfo=ET)  # Monday catch-up -> reads Oct 9
SESSIONS = sessions_between(dt.date(2025, 6, 1), DAY)


def _no_lag(day: dt.date, as_of: dt.date) -> int:
    return 0


def _series(values: list[float], end: dt.date = DAY) -> list[tuple[dt.date, float]]:
    days = [d for d in SESSIONS if d <= end][-len(values) :]
    assert len(days) == len(values)
    return list(zip(days, values, strict=True))


# ---------------------------------------------------------------------------
# Pure math
# ---------------------------------------------------------------------------


class TestPercentile:
    def test_share_of_prior_strictly_below(self) -> None:
        prior = [float(i) for i in range(200)]
        assert percentile_prior([*prior, 50.0]) == 50 / 200
        assert percentile_prior([*prior, 50.5]) == 51 / 200
        assert percentile_prior([*prior, -1.0]) == 0.0
        assert percentile_prior([*prior, 1e9]) == 1.0

    def test_ties_count_as_not_below(self) -> None:
        assert percentile_prior([5.0] * 200) == 0.0

    def test_lookback_and_min_obs(self) -> None:
        old = [100.0] * 300  # outside the 252 lookback: ignored
        recent = [1.0] * 252
        assert percentile_prior([*old, *recent, 50.0]) == 1.0
        assert percentile_prior([1.0] * 119 + [2.0]) is None  # 119 prior < 120
        assert percentile_prior([1.0] * 120 + [2.0]) == 1.0
        assert percentile_prior([]) is None

    @given(st.lists(st.floats(0.01, 1e4), min_size=1, max_size=400))
    def test_bounds(self, xs: list[float]) -> None:
        p = percentile_prior(xs)
        assert p is None or 0.0 <= p <= 1.0
        assert (p is None) == (len(xs) - 1 < mh.MIN_PCT_OBS)


def test_rolling_mean() -> None:
    assert rolling_mean([1.0, 2.0, 3.0, 4.0, 5.0, 6.0], 5) == [3.0, 4.0]
    assert rolling_mean([1.0, 2.0], 5) == []
    with pytest.raises(ValueError, match="window"):
        rolling_mean([1.0], 0)


def test_merge_pc_points_later_wins_and_trims() -> None:
    a = [PcPoint(day=dt.date(2026, 1, d), equity=0.5) for d in (2, 5, 6)]
    b = [PcPoint(day=dt.date(2026, 1, 5), equity=0.9)]
    out = merge_pc_points(a, b)
    assert [(p.day.day, p.equity) for p in out] == [(2, 0.5), (5, 0.9), (6, 0.5)]
    assert [p.day.day for p in merge_pc_points(a, keep=2)] == [5, 6]


def test_breadth_from_technicals() -> None:
    techs: list[dict[str, Any]] = [
        {"close": 110, "sma50": 100, "sma200": 120, "squeeze_on": True},
        {"close": 90, "sma50": 100, "sma200": 80, "squeeze_on": False},
        {"close": 105, "sma50": 100, "sma200": None},  # no SMA200: left out of that share
        {"close": None, "sma50": 1},  # no close: not counted at all
    ]
    b = breadth_from_technicals(techs)
    assert (b.n, b.above_50d, b.above_200d, b.squeeze_count) == (3, 2 / 3, 1 / 2, 1)
    assert breadth_from_technicals([]).n == 0
    assert breadth_from_technicals([{"close": 1.0}]).above_50d is None


# ---------------------------------------------------------------------------
# Labels truth table
# ---------------------------------------------------------------------------


def _p(**kw: Any) -> MarketHealthPayload:
    return MarketHealthPayload(as_of=DAY, **kw)


@pytest.mark.parametrize(
    ("fields", "labels"),
    [
        ({}, []),
        ({"vix": 24.0, "vix_sma50": 20.0}, ["vix_stretched_high"]),  # exactly 1.2x
        ({"vix": 23.9, "vix_sma50": 20.0}, []),
        ({"vix_pct_1y": 0.8}, ["vix_stretched_high"]),
        ({"vix_pct_1y": 0.79}, []),
        ({"vix_pct_1y": 0.2}, ["vix_compressed"]),
        ({"vix_pct_1y": 0.21}, []),
        ({"pc_equity_5d_pct_1y": 0.9}, ["pc_extreme_fear"]),
        ({"pc_equity_5d_pct_1y": 0.89}, []),
        ({"pc_equity_5d_pct_1y": 0.1}, ["pc_extreme_greed"]),
        ({"pc_equity_5d_pct_1y": 0.11}, []),
        ({"pc_total_5d_pct_1y": 0.99}, []),  # labels read the equity series only
        ({"breadth_above_50d": 0.39}, ["breadth_weak"]),
        ({"breadth_above_50d": 0.4}, []),
        ({"breadth_above_50d": 0.7}, []),
        ({"breadth_above_50d": 0.71}, ["breadth_strong"]),
        (
            {"vix": 30.0, "vix_sma50": 20.0, "pc_equity_5d_pct_1y": 0.95, "breadth_above_50d": 0.2},
            ["vix_stretched_high", "pc_extreme_fear", "breadth_weak"],
        ),
    ],
)
def test_labels_truth_table(fields: dict[str, Any], labels: list[str]) -> None:
    assert health_labels(_p(**fields), HealthThresholds()) == labels


def test_labels_follow_config_thresholds() -> None:
    th = HealthThresholds(breadth_weak=0.5, vix_compressed_pct=0.3)
    assert health_labels(_p(breadth_above_50d=0.45, vix_pct_1y=0.25), th) == [
        "vix_compressed",
        "breadth_weak",
    ]


# ---------------------------------------------------------------------------
# compute_market_health
# ---------------------------------------------------------------------------


def _pc(n: int, end: dt.date = DAY, equity: float = 0.6) -> list[PcPoint]:
    days = [d for d in SESSIONS if d <= end][-n:]
    return [
        PcPoint(day=d, equity=equity + 0.001 * i, total=0.9 + 0.001 * i) for i, d in enumerate(days)
    ]


def test_full_read() -> None:
    vix = [20.0] * 299 + [15.0]
    p = compute_market_health(
        as_of=DAY,
        vix=_series(vix),
        vvix=_series([90.0 + i * 0.1 for i in range(300)]),
        pc=_pc(300),
        term=(DAY, MarketTerm(structure="contango", ratio_9d_1m=0.9, ratio_3m_1m=1.1)),
        technicals=[{"close": 2, "sma50": 1, "sma200": 1}, {"close": 1, "sma50": 2, "sma200": 0.5}],
        technicals_as_of=DAY,
        lag=_no_lag,
    )
    assert p.vix == 15.0
    assert p.vix_sma50 == pytest.approx((49 * 20 + 15) / 50, abs=1e-4)
    assert p.vix_vs_sma50_pct == pytest.approx(15 / ((49 * 20 + 15) / 50) - 1, abs=1e-4)
    assert p.vix_pct_1y == 0.0
    assert p.vvix_pct_1y == 1.0  # rising series: today above every prior close
    assert p.pc_equity_5d_pct_1y == 1.0 and p.pc_total_5d_pct_1y == 1.0
    assert p.pc_equity == pytest.approx(0.6 + 0.299)
    assert p.term is not None and p.term.structure == "contango"
    assert (p.breadth_above_50d, p.breadth_above_200d, p.breadth_n) == (0.5, 1.0, 2)
    assert p.labels == ["vix_compressed", "pc_extreme_fear"]
    assert p.missing == []
    assert p.sources_as_of == {
        "vix": "2026-10-09",
        "vvix": "2026-10-09",
        "vol_term": "2026-10-09",
        "options_daily": "2026-10-09",
        "technicals": "2026-10-09",
    }


def test_short_history_gives_none_and_missing() -> None:
    p = compute_market_health(as_of=DAY, vix=_series([20.0] * 30), pc=_pc(3), lag=_no_lag)
    assert p.vix == 20.0 and p.vix_sma50 is None and p.vix_pct_1y is None
    assert p.pc_equity_5d is None and p.pc_equity == pytest.approx(0.602)
    assert "vix_sma50: 30 closes < 50" in p.missing
    assert "vix_pct_1y: 29 prior obs < 120" in p.missing
    assert "pc_equity_5d: 3 sessions < 5" in p.missing
    assert "vvix: no data" in p.missing and "vol_term: no data" in p.missing
    assert "breadth: no fresh technicals on the active list" in p.missing


def test_stale_inputs_are_none_never_carried_forward() -> None:
    old = SESSIONS[-4]  # three sessions before DAY
    p = compute_market_health(
        as_of=DAY,
        vix=_series([20.0] * 300, end=old),
        vvix=_series([90.0] * 300),
        pc=_pc(300, end=old),
        term=(old, MarketTerm(structure="flat")),
        lag=mhi.session_lag,
    )
    assert p.vix is None and p.vix_sma50 is None and p.vix_pct_1y is None
    assert p.pc_equity is None and p.pc_equity_5d is None and p.term is None
    assert p.vvix == 90.0  # fresh input still used
    assert f"vix: stale ({old.isoformat()})" in p.missing
    assert f"options_daily: stale ({old.isoformat()})" in p.missing
    assert f"vol_term: stale ({old.isoformat()})" in p.missing
    # two sessions old is still fresh at the default max_lag_sessions = 2
    p2 = compute_market_health(
        as_of=DAY, vix=_series([20.0] * 300, end=SESSIONS[-3]), lag=mhi.session_lag
    )
    assert p2.vix == 20.0
    p3 = compute_market_health(
        as_of=DAY,
        vix=_series([20.0] * 300, end=SESSIONS[-3]),
        lag=mhi.session_lag,
        thresholds=HealthThresholds(max_lag_sessions=1),
    )
    assert p3.vix is None


def test_term_after_as_of_is_ignored() -> None:
    p = compute_market_health(
        as_of=SESSIONS[-2], term=(DAY, MarketTerm(structure="flat")), lag=_no_lag
    )
    assert p.term is None and "vol_term: no data" in p.missing


@settings(max_examples=40, deadline=None)
@given(
    st.lists(st.floats(9.0, 80.0), min_size=150, max_size=300),
    st.lists(st.floats(0.3, 1.5), min_size=150, max_size=300),
    st.integers(1, 60),
    st.floats(9.0, 80.0),
)
def test_walk_forward_safe(vix: list[float], pc: list[float], cut: int, future: float) -> None:
    """Data after as_of never changes the read: appending (or rewriting) later sessions
    leaves every field for as_of identical."""
    days = SESSIONS[-len(vix) :]
    as_of = days[-cut] if cut < len(days) else days[0]
    v = list(zip(days, vix, strict=True))
    pdays = SESSIONS[-len(pc) :]
    pts = [PcPoint(day=d, equity=x, total=x) for d, x in zip(pdays, pc, strict=True)]
    base = compute_market_health(as_of=as_of, vix=v, pc=pts, lag=_no_lag)
    trunc = compute_market_health(
        as_of=as_of,
        vix=[(d, x) for d, x in v if d <= as_of],
        pc=[p for p in pts if p.day <= as_of],
        lag=_no_lag,
    )
    poisoned = compute_market_health(
        as_of=as_of,
        vix=[(d, future if d > as_of else x) for d, x in v],
        pc=[p if p.day <= as_of else PcPoint(day=p.day, equity=future) for p in pts],
        lag=_no_lag,
    )
    assert base == trunc == poisoned


# ---------------------------------------------------------------------------
# Rendered line
# ---------------------------------------------------------------------------


def test_line_example_format() -> None:
    p = _p(
        vix=15.4,
        vix_sma50=16.74,
        vix_vs_sma50_pct=-0.08,
        vix_pct_1y=0.22,
        vvix=88.2,
        vvix_pct_1y=0.40,
        pc_equity_5d=0.61,
        pc_equity_5d_pct_1y=0.08,
        breadth_above_50d=0.62,
        breadth_above_200d=0.71,
        breadth_n=40,
        squeeze_count=4,
        labels=["pc_extreme_greed"],
    )
    assert market_health_line(p.model_dump(mode="json")) == (
        "Market health (10-09): VIX 15.4 (-8% vs 50d, 1y p22) · VVIX 88 (p40) · "
        "P/C eq 5d 0.61 (p8, greed) · breadth 62% >50d / 71% >200d (n 40) · 4 in squeeze"
    )


def test_line_drops_missing_parts() -> None:
    assert market_health_line(_p().model_dump(mode="json")) == (
        "Market health (10-09): no fresh inputs"
    )
    line = market_health_line(
        _p(
            vix=31.0,
            breadth_above_50d=0.3,
            breadth_n=5,
            labels=["vix_stretched_high", "breadth_weak"],
        ).model_dump(mode="json")
    )
    assert line == (
        "Market health (10-09): VIX 31.0 (stretched high) · breadth 30% >50d (n 5, weak)"
    )


# ---------------------------------------------------------------------------
# Research prompt: flag off byte-identical, on adds one line
# ---------------------------------------------------------------------------


def _snap_with(conn: sqlite3.Connection, payload: MarketHealthPayload | None) -> Any:
    now = dt.datetime(2026, 10, 12, 10, 0, tzinfo=ET)
    store = ContextStore(conn)
    store.write(
        kind="vol_term",
        subject="market",
        payload={
            "as_of": "2026-10-09", "vix9d": 14.0, "vix": 15.4, "vix3m": 17.0, "vvix": 88.0,
            "ratio_9d_1m": 0.91, "ratio_3m_1m": 1.1, "structure": "contango",
        },
        produced_by="vol_term",
        ttl="1 session",
        now=now - dt.timedelta(hours=1),
    )  # fmt: skip
    if payload is not None:
        store.write(
            kind="market_health",
            subject="market",
            payload=payload,
            produced_by="market_health",
            ttl="1 session",
            now=now - dt.timedelta(hours=1),
        )
    return store.snapshot(now, kinds=RESEARCH_READS)


def test_research_prompt_flag_off_identical_on_adds_line() -> None:
    kw: dict[str, Any] = {"portfolio_summary": "Equity $25,000.", "scan_date": "2026-10-12"}
    payload = _p(vix=15.4, vix_pct_1y=0.22, breadth_above_50d=0.62, breadth_n=40)
    c1, c2 = connect(":memory:"), connect(":memory:")
    migrate(c1)
    migrate(c2)
    with_entry, without = _snap_with(c1, payload), _snap_with(c2, None)
    for compact in (True, False):
        off = build_research_prompt(research_input_from_context(with_entry, **kw, compact=compact))
        before = build_research_prompt(research_input_from_context(without, **kw, compact=compact))
        assert off == before  # flag off: a stored market_health entry changes nothing
        assert "Market health" not in off
    on = build_research_prompt(
        research_input_from_context(with_entry, **kw, compact=True, market_health=True)
    )
    line = market_health_line(payload.model_dump(mode="json"))
    assert line in on
    assert on.replace(line + "\n", "", 1) == build_research_prompt(
        research_input_from_context(without, **kw, compact=True)
    )
    # flag on with nothing stored: no line, prompt unchanged
    assert build_research_prompt(
        research_input_from_context(without, **kw, compact=True, market_health=True)
    ) == build_research_prompt(research_input_from_context(without, **kw, compact=True))


# ---------------------------------------------------------------------------
# The job on a scratch store
# ---------------------------------------------------------------------------


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    c = connect(tmp_path / "arc.db")
    migrate(c)
    yield c
    c.close()


def _ctx(conn: sqlite3.Connection, slot: dt.datetime, **personas: str) -> JobContext:
    overrides = {("personas", k): v for k, v in personas.items()}
    routines = load_routines(DEFAULT_ROUTINES_PATH, overrides=overrides or None)
    kind, spec = routines.step("market_health")
    return JobContext(
        job="market_health",
        kind=kind,
        spec=spec,
        run_id="r1",
        chain_run_id=None,
        scheduled_for=slot,
        now=slot,
        conn=conn,
        snapshot=ContextStore(conn).snapshot(slot, kinds=[]),
        routines=routines,
        settings_factory=lambda: ArcSettings(_env_file=None, env="paper"),  # type: ignore[call-arg]
    )


def _hist(index: str, values: list[float], end: dt.date = DAY) -> IndexHistoryPayload:
    return IndexHistoryPayload(
        index=index,
        as_of=end,
        closes=[IndexClose(day=d, close=v) for d, v in _series(values, end)],
        url=f"https://cdn.cboe.com/{index}",
    )


class FakeCboe:
    def __init__(self, end: dt.date = DAY, fail: frozenset[str] = frozenset()) -> None:
        self.end, self.fail = end, fail
        self.calls: list[str] = []

    def __call__(self, index: str) -> IndexHistoryPayload:
        self.calls.append(index)
        if index in self.fail:
            msg = "boom"
            raise OSError(msg)
        base = 18.0 if index == "VIX" else 95.0
        return _hist(index, [base + (i % 7) for i in range(299)] + [base - 3], self.end)


def _seed(conn: sqlite3.Connection, tickers: dict[str, tuple[float, float, float]]) -> None:
    """options_daily for the last 8 sessions, a vol_term, and regime technicals."""
    from tests.test_features_technicals import _payload

    store = ContextStore(conn)
    written = dt.datetime(2026, 10, 9, 23, 0, tzinfo=ET)
    for i, d in enumerate(SESSIONS[-8:]):
        store.write(
            kind="options_daily",
            subject="market",
            payload={
                "as_of": d.isoformat(),
                "fetched_at": written.isoformat(),
                "url": "https://cdn.cboe.com/x",
                "ratios": [
                    {"segment": "total", "ratio": 0.9 + i / 100},
                    {"segment": "equity", "ratio": 0.5 + i / 100},
                ],
                "open_interest": [],
            },
            produced_by="options_daily",
            ttl="1 session",
            now=written - dt.timedelta(days=8 - i),
        )
    store.write(
        kind="vol_term",
        subject="market",
        payload={
            "as_of": DAY.isoformat(), "vix9d": 14.0, "vix": 15.0, "vix3m": 17.0, "vvix": 90.0,
            "ratio_9d_1m": 0.93, "ratio_3m_1m": 1.13, "structure": "contango",
        },
        produced_by="vol_term",
        ttl="1 session",
        now=written,
    )  # fmt: skip
    base = _payload()
    for t, (close, sma50, sma200) in tickers.items():
        p = json.loads(json.dumps(base))
        p["ticker"] = t
        p["technicals"].update(close=close, sma50=sma50, sma200=sma200, squeeze_on=t == "AAA")
        store.write(
            kind="regime",
            subject=t,
            payload=RegimePayload.model_validate(p),
            produced_by="research",
            ttl="1 session",
            now=dt.datetime(2026, 10, 9, 15, 50, tzinfo=ET),
        )


def test_job_writes_read_histories_and_summary(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    import arc.universe.tiers as tiers

    _seed(conn, {"AAA": (110, 100, 90), "BBB": (95, 100, 90), "CCC": (120, 100, 130)})
    monkeypatch.setattr(tiers, "active_tickers", lambda *_a, **_k: ["AAA", "BBB", "CCC", "ZZZ"])
    fake = FakeCboe()
    res = market_health_source(_ctx(conn, EVENING), fetch_index=fake)
    assert fake.calls == ["VIX", "VVIX"]
    store = ContextStore(conn)
    later = EVENING + dt.timedelta(minutes=1)
    entries = {(e.kind, e.subject): e for e in store.query(as_of=later)}
    assert {k for k in entries if k[0] in {"index_history", "pc_history", "market_health"}} == {
        ("index_history", "VIX"),
        ("index_history", "VVIX"),
        ("pc_history", "market"),
        ("market_health", "market"),
    }
    p = MarketHealthPayload.model_validate(entries[("market_health", "market")].payload)
    assert p.as_of == DAY and p.vix == 15.0 and p.vix_pct_1y == 0.0
    assert p.breadth_n == 3 and p.breadth_above_50d == pytest.approx(2 / 3, abs=1e-4)
    assert p.breadth_above_200d == pytest.approx(2 / 3, abs=1e-4) and p.squeeze_count == 1
    assert p.term is not None and p.term.ratio_3m_1m == 1.13
    assert p.pc_equity == pytest.approx(0.57) and p.pc_equity_5d == pytest.approx(0.55, abs=1e-4)
    assert "pc_equity_5d_pct_1y: 3 prior obs < 120" in p.missing  # 8 sessions stored
    assert "vix_compressed" in p.labels
    pch = PcHistoryPayload.model_validate(entries[("pc_history", "market")].payload)
    assert len(pch.points) == 8 and pch.as_of == DAY
    assert res.summary.startswith("Market health (10-09): VIX 15.0")
    assert res.metrics["breadth_n"] == 3
    # 1 session ttl: the read serves Monday's loop, expires at Monday's close
    exp = entries[("market_health", "market")].expires_at
    assert exp is not None and exp.astimezone(ET).date() == dt.date(2026, 10, 12)


def test_job_evening_skips_until_cboe_publishes(conn: sqlite3.Connection) -> None:
    with pytest.raises(JobSkippedError, match="not published yet"):
        market_health_source(_ctx(conn, EVENING), fetch_index=FakeCboe(end=SESSIONS[-2]))
    assert ContextStore(conn).query(as_of=EVENING, kinds=["market_health"]) == []


def test_job_catch_up_writes_with_missing(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    import arc.universe.tiers as tiers

    monkeypatch.setattr(tiers, "active_tickers", lambda *_a, **_k: [])
    # a failing VVIX fetch falls back to the stored history (none yet) -> missing
    market_health_source(_ctx(conn, MORNING), fetch_index=FakeCboe(fail=frozenset({"VVIX"})))
    (e,) = ContextStore(conn).query(as_of=MORNING, kinds=["market_health"])
    p = MarketHealthPayload.model_validate(e.payload)
    assert p.as_of == DAY and p.vix is not None and p.vvix is None
    assert "vvix: no data" in p.missing and "fetch VVIX: OSError" in p.missing
    assert "options_daily: no data" in p.missing
    # the next run reuses the stored VVIX history when Cboe fails again
    market_health_source(_ctx(conn, MORNING + dt.timedelta(minutes=5)), fetch_index=FakeCboe())
    market_health_source(
        _ctx(conn, MORNING + dt.timedelta(minutes=10)),
        fetch_index=FakeCboe(fail=frozenset({"VVIX"})),
    )
    newest = mhi.latest_payload(conn, "market_health", "market")
    assert newest is not None and newest["vvix"] is not None


def test_catch_up_slot_dropped_once_written(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """until_written market_health:{day}: the 08:20 slot is skipped once 23:15 wrote."""
    import arc.universe.tiers as tiers
    from arc.routines.dispatcher import Dispatcher

    monkeypatch.setattr(tiers, "active_tickers", lambda *_a, **_k: [])
    market_health_source(_ctx(conn, EVENING), fetch_index=FakeCboe())
    routines = load_routines(DEFAULT_ROUTINES_PATH)
    d = Dispatcher(conn, routines)
    spec = routines.step("market_health")[1]
    assert d._session_written(spec, MORNING) == "already written: market_health for 2026-10-09"
    assert d._session_written(spec, MORNING + dt.timedelta(days=1)) is None


# ---------------------------------------------------------------------------
# Put/call backfill (resumable)
# ---------------------------------------------------------------------------


def test_pc_points_from_options_daily() -> None:
    pts = mhi.pc_points_from_options_daily(
        [
            {"as_of": "2026-10-08", "ratios": [{"segment": "equity", "ratio": 0.5}]},
            {"as_of": "2026-10-07", "ratios": [{"segment": "total", "ratio": 0.9}]},
            {"as_of": "2026-10-06", "ratios": [{"segment": "spx", "ratio": 1.2}]},  # neither
        ]
    )
    assert [(p.day.day, p.equity, p.total) for p in pts] == [(7, None, 0.9), (8, 0.5, None)]


def test_backfill_resumable_and_skips(conn: sqlite3.Connection) -> None:
    calls: list[dt.date] = []
    holes = {dt.date(2026, 9, 15)}

    def fetch(d: dt.date) -> Any:
        calls.append(d)
        if d in holes:
            raise NotPublishedError(d.isoformat())
        if d == dt.date(2026, 9, 16):
            msg = "bad json"
            raise ValueError(msg)
        return {"as_of": d.isoformat(), "ratios": [{"segment": "equity", "ratio": 0.6}]}

    now = dt.datetime(2026, 10, 10, 9, 0, tzinfo=ET)
    sleeps: list[float] = []
    start, end = dt.date(2026, 9, 1), DAY
    res = mhi.backfill_pc_history(
        conn, start=start, end=end, now=now, fetch=fetch, pace_s=0.25, save_every=5,
        sleep=sleeps.append,
    )  # fmt: skip
    n = len(sessions_between(start, end))
    assert len(res.fetched) == n - 2 and set(res.skipped) == {"2026-09-15", "2026-09-16"}
    assert "not published" in res.skipped["2026-09-15"]
    assert res.writes == -(-(n - 2) // 5) and sleeps == [0.25] * (n - 1)
    stored = PcHistoryPayload.model_validate(mhi.latest_payload(conn, "pc_history", "market"))
    assert len(stored.points) == n - 2 and stored.as_of == DAY
    # second run: only the two holes are retried
    calls.clear()
    holes.clear()
    res2 = mhi.backfill_pc_history(
        conn, start=start, end=end, now=now, fetch=fetch, sleep=sleeps.append
    )
    assert calls == [dt.date(2026, 9, 15), dt.date(2026, 9, 16)]
    assert res2.already == n - 2 and len(res2.fetched) == 1


def test_session_lag() -> None:
    assert mhi.session_lag(DAY, DAY) == 0
    assert mhi.session_lag(dt.date(2026, 10, 8), DAY) == 1
    assert mhi.session_lag(dt.date(2026, 10, 9), dt.date(2026, 10, 12)) == 1  # over a weekend
    assert mhi.session_lag(dt.date(2026, 10, 10), dt.date(2026, 10, 12)) == 1  # from a Saturday


def test_index_history_from_csv_keeps_tail() -> None:
    rows = "\n".join(
        f"{d:%m/%d/%Y},{v},{v},{v},{v}" for d, v in _series([10.0 + i for i in range(305)])
    )
    p = mhi.index_history_from_csv("VIX", "DATE,OPEN,HIGH,LOW,CLOSE\n" + rows)
    assert p is not None and len(p.closes) == mh.HISTORY_KEEP and p.as_of == DAY
    assert p.closes[-1].close == 314.0
    assert mhi.index_history_from_csv("VIX", "DATE,OPEN,HIGH,LOW,CLOSE\n") is None


# ---------------------------------------------------------------------------
# Config, kinds, registry, experiment draft
# ---------------------------------------------------------------------------


def test_kinds_are_reference_with_ttls() -> None:
    for kind in ("index_history", "pc_history", "market_health"):
        assert kind in KINDS and kind in REFERENCE_KINDS and kind_category(kind) is not None
    r = load_routines()
    assert "market_health" in RESEARCH_READS
    assert r.step("research")[1].reads == RESEARCH_READS


def test_flag_defaults_off_and_parses() -> None:
    r = load_routines()
    assert r.market_health_context.enabled is False
    assert r.market_health == HealthThresholds()
    on = load_routines(overrides={("personas", "market_health_context"): "on"})
    assert on.market_health_context.enabled is True
    with pytest.raises(ValueError, match="breadth_weak"):
        RoutinesConfig.model_validate({"market_health": {"breadth_weak": 2}})


def test_job_slot_after_options_daily() -> None:
    r = load_routines()
    _, spec = r.step("market_health")
    _, od = r.step("options_daily")
    assert spec.schedule == [dt.time(23, 15), dt.time(8, 20)]
    assert od.schedule == [dt.time(23, 0), dt.time(8, 15)]
    assert all(a > b for a, b in zip(spec.schedule, od.schedule, strict=True))
    assert spec.catch_up is not None and spec.catch_up.until_written == "market_health:{day}"
    assert set(spec.writes) == {"index_history", "pc_history", "market_health"}


def test_registry_entries() -> None:
    t = lookup("personas.market_health_context")
    assert lookup("market_health_context") is t and t.choices == ("off", "on")
    raw = yaml.safe_load(DEFAULT_ROUTINES_PATH.read_text())
    for name in HealthThresholds.model_fields:
        key = f"market_health.{name}"
        assert key in REGISTRY, key
        assert REGISTRY[key].path == ("market_health", name)
        assert name in raw["market_health"]


def test_xp13_draft_arm_turns_only_the_flag_on() -> None:
    spec = yaml.safe_load((REPO / "config/experiments/live/xp13_technicals.yaml").read_text())
    t5 = spec["arms"]["treatments"]["t5"]["overlay"]
    assert t5 == {"routines": {"personas": {"market_health_context": "on"}}}


def test_never_a_gate_input() -> None:
    gate = (REPO / "arc" / "gate").rglob("*.py")
    assert not any("market_health" in p.read_text() for p in gate)
