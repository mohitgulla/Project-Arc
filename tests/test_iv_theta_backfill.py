"""E7.13 (D84): ``theta_backfill`` IV source, configured precedence, the CHECK migration,
``arc iv backfill --source theta``, and the nightly ``history.topup`` job.

All offline: synthetic ThetaData parquet (``tests.backtest_data_fixtures``), a fake EOD
provider and fake daily bars. ``now`` is injected; nothing reads the wall clock.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from typing import TYPE_CHECKING, Any

import pandas as pd
import pytest

from arc.backtest.history_topup import (
    plan,
    refresh_closes,
    run_topup,
    seeded_names,
    servable_session,
)
from arc.backtest.option_stats import StatsRunner, daily_path, read_daily
from arc.backtest.theta_iv import METHOD_STATS, METHOD_TOPUP, seed, seed_ticker, stats_config
from arc.backtest.underlying import UnderlyingStore
from arc.config import ArcSettings
from arc.context import ContextStore
from arc.control.registry import NOT_EXPOSED, REGISTRY, TunableError, lookup
from arc.data.history.store import ParquetHistoryStore
from arc.features.vol import iv_rank
from arc.iv.store import (
    BACKFILL,
    FORWARD,
    OPTIONSTRATEGIST,
    SERIES_SOURCES,
    THETA,
    IvRow,
    IvStore,
    safe_store,
)
from arc.routines.config import HistoryTopupSettings, RoutinesConfig, load_routines
from arc.routines.handlers import (
    HISTORY_TOPUP_DOWN_DAY,
    JobContext,
    JobSkippedError,
    history_topup_source,
)
from arc.routines.runs import RoutineStateRepo
from arc.store.db import connect
from arc.store.migrate import MIGRATIONS_DIR, migrate
from arc.utils.calendar import ET, previous_session, sessions_between
from tests.backtest_data_fixtures import (
    sessions,
    spot_path,
    synth_rows,
    write_history,
    write_underlying,
    write_universe,
)

if TYPE_CHECKING:
    from pathlib import Path

NOW = dt.datetime(2026, 10, 9, 21, 0, tzinfo=ET)
START = dt.date(2025, 3, 3)
N = 40


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = connect(":memory:")
    migrate(c)
    return c


def _rows(store: IvStore, ticker: str, days: list[dt.date], source: Any, iv: float) -> None:
    store.upsert([IvRow(ticker, d, iv, "x", source) for d in days], now=NOW)


# ---------------------------------------------------------------------------
# migration 038: the source CHECK
# ---------------------------------------------------------------------------


def _store_before_038(path: Path) -> sqlite3.Connection:
    c = connect(path)
    c.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY)")
    for sql in sorted(MIGRATIONS_DIR.glob("*.sql")):
        v = int(sql.stem.split("_", 1)[0])
        if v >= 38:
            break
        c.executescript(sql.read_text())
        c.execute("INSERT INTO schema_version (version) VALUES (?)", (v,))
    c.commit()
    return c


def test_migration_038_keeps_rows_and_admits_theta_backfill(tmp_path: Path) -> None:
    c = _store_before_038(tmp_path / "arc.db")
    ins = (
        "INSERT INTO iv_daily (ticker, day, iv30, method, source, detail, created_at) "
        "VALUES (?, ?, ?, ?, ?, '{}', 'x')"
    )
    c.execute(ins, ("SPY", "2026-10-01", 0.18, "cm30", "alpaca_cm30"))
    c.execute(ins, ("SPY", "2026-09-30", 0.19, "bf", "alpaca_backfill"))
    c.commit()
    with pytest.raises(sqlite3.IntegrityError):
        c.execute(ins, ("SPY", "2026-09-29", 0.2, "t", "theta_backfill"))
    assert 38 in migrate(c)
    rows = c.execute("SELECT ticker, day, iv30, source FROM iv_daily ORDER BY day").fetchall()
    assert [tuple(r) for r in rows] == [
        ("SPY", "2026-09-30", 0.19, "alpaca_backfill"),
        ("SPY", "2026-10-01", 0.18, "alpaca_cm30"),
    ]
    c.execute(ins, ("SPY", "2026-09-29", 0.2, "t", "theta_backfill"))
    with pytest.raises(sqlite3.IntegrityError):
        c.execute(ins, ("SPY", "2026-09-28", 0.2, "t", "bogus"))
    with pytest.raises(sqlite3.IntegrityError):  # primary key kept
        c.execute(ins, ("SPY", "2026-09-29", 0.3, "t", "theta_backfill"))
    with pytest.raises(sqlite3.IntegrityError):  # iv30 > 0 kept
        c.execute(ins, ("QQQ", "2026-09-29", 0.0, "t", "theta_backfill"))
    idx = {r[1] for r in c.execute("PRAGMA index_list(iv_daily)")}
    assert "idx_iv_daily_source_day" in idx


# ---------------------------------------------------------------------------
# precedence (config, not code)
# ---------------------------------------------------------------------------


def test_default_precedence_is_cm30_then_theta_then_alpaca_backfill() -> None:
    assert ArcSettings().iv_series_sources == ["alpaca_cm30", "theta_backfill", "alpaca_backfill"]
    assert SERIES_SOURCES == (FORWARD, THETA, BACKFILL)


def test_same_day_precedence_and_merged_series(conn: sqlite3.Connection) -> None:
    store = IvStore(conn)
    d1, d2, d3, d4 = (dt.date(2026, 10, i) for i in (1, 2, 5, 6))
    _rows(store, "AAA", [d1, d2, d3], BACKFILL, 0.10)
    _rows(store, "AAA", [d2, d3, d4], THETA, 0.20)
    _rows(store, "AAA", [d3], FORWARD, 0.30)
    _rows(store, "AAA", [d1, d2, d3, d4], OPTIONSTRATEGIST, 0.99)
    assert store.series("AAA") == {d1: 0.10, d2: 0.20, d3: 0.30, d4: 0.20}
    flipped = IvStore(conn, series_sources=[FORWARD, BACKFILL, THETA])
    assert flipped.series("AAA") == {d1: 0.10, d2: 0.10, d3: 0.30, d4: 0.20}
    only = safe_store(conn, series_sources=["alpaca_backfill"])
    assert only is not None and only.series("AAA") == {d1: 0.1, d2: 0.1, d3: 0.1}


def test_iv_rank_uses_the_merged_series(conn: sqlite3.Connection) -> None:
    """A name with only 20 alpaca_backfill days gets a rank once theta_backfill fills 2025."""
    store = IvStore(conn)
    days = sessions_between(dt.date(2025, 10, 1), dt.date(2026, 10, 9))
    old, recent = days[:-20], days[-20:]
    _rows(store, "AAA", recent, BACKFILL, 0.30)
    with pytest.raises(Exception, match="IV rank needs"):
        iv_rank(pd.Series(store.series("AAA")), recent[-1], min_obs=120)
    store.upsert(
        [IvRow("AAA", d, 0.2 + 0.2 * (i % 2), METHOD_STATS, THETA) for i, d in enumerate(old)],
        now=NOW,
    )
    series = pd.Series(store.series("AAA"))
    assert len(series) == len(days)
    assert iv_rank(series, recent[-1], min_obs=120) == pytest.approx(0.5)  # 0.3 in [0.2, 0.4]


def test_iv_backfill_topup_treats_a_theta_seeded_name_as_not_short(
    conn: sqlite3.Connection,
) -> None:
    from arc.iv.topup import run_topup as iv_topup

    lookback = sessions_between(dt.date(2026, 6, 1), dt.date(2026, 10, 8))
    _rows(IvStore(conn), "AAA", lookback, THETA, 0.25)
    calls: list[int] = []

    def factory() -> Any:
        calls.append(1)
        raise AssertionError("no Alpaca client for a seeded name")

    kw: dict[str, Any] = dict(
        sessions=lookback, lookback=lookback, now=NOW, min_obs=20, max_tickers=5,
        max_runtime_s=60, r=0.04, dividend_yields={},
    )  # fmt: skip
    res = iv_topup(conn, factory, ["AAA"], **kw)
    assert res.picked == [] and calls == []
    only_alpaca = [FORWARD, BACKFILL]
    with pytest.raises(AssertionError, match="no Alpaca client"):
        iv_topup(conn, factory, ["AAA"], series_sources=only_alpaca, **kw)


def test_series_sources_validation() -> None:
    with pytest.raises(ValueError, match="twice"):
        ArcSettings(iv_series_sources=["alpaca_cm30", "alpaca_cm30"])
    with pytest.raises(ValueError):
        ArcSettings(iv_series_sources=["optionstrategist"])
    with pytest.raises(ValueError):
        ArcSettings(iv_series_sources=[])
    with pytest.raises(ValueError, match="not a series source"):
        IvStore(connect(":memory:"), series_sources=["optionstrategist"])  # type: ignore[list-item]


def test_knobs_are_classified() -> None:
    assert "iv_series_sources" in NOT_EXPOSED and "iv_series_sources" not in REGISTRY
    assert not any(k.startswith("history_topup") for k in REGISTRY)
    with pytest.raises(TunableError):
        lookup("history_topup.max_runtime_s")


# ---------------------------------------------------------------------------
# seeding from the cached Theta history
# ---------------------------------------------------------------------------


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    days = sessions(START, N)
    for t, seed_ in (("AAA", 3), ("BBB", 5)):
        spots = spot_path(N, seed=seed_)
        write_history(tmp_path, t, synth_rows(t, days, spots))
        write_underlying(tmp_path, t, days, spots)
    write_universe(tmp_path, ["AAA", "BBB", "CCC"], [(dt.date(2025, 1, 1), "AAA", True)])
    return tmp_path


def _run_stats(data_dir: Path, tickers: list[str], end: dt.date | None = None) -> None:
    """E7.16's writer over *tickers* (through *end*): ``option_stats/daily`` parquet."""
    from arc.backtest.universe import load_universe

    cfg, cost = stats_config()
    _, rows, _ = load_universe(data_dir)
    days = sessions(START, N)
    runner = StatsRunner(
        data_dir, cfg=cfg, cost=cost, rows=rows, start=days[0], end=end or days[-1]
    )
    for t in tickers:
        runner.run_ticker(t)


def test_seed_reuses_option_stats_then_computes_newer_sessions(
    conn: sqlite3.Connection, data_dir: Path
) -> None:
    days = sessions(START, N)
    _run_stats(data_dir, ["AAA"], end=days[29])  # option_stats covers the first 30 sessions
    stats = read_daily(data_dir, "AAA")
    assert stats["date"].max() == days[29]
    rep = seed(conn, data_dir, ["aaa", "AAA"], now=NOW)
    t = rep.tickers[0]
    assert len(rep.tickers) == 1 and t.error is None
    store = IvStore(conn)
    rows = {r.day: r for r in store.rows("AAA", [THETA])}
    have_stats = stats.dropna(subset=["iv30"])
    assert t.from_stats == len(have_stats) > 20
    assert t.computed == 10 and t.stored == len(rows)
    for r in have_stats.itertuples():  # the stored point is E7.16's number, unchanged
        assert rows[r.date].iv30 == pytest.approx(float(r.iv30))
        assert rows[r.date].method == METHOD_STATS
    assert {rows[d].method for d in days[30:]} == {METHOD_TOPUP}
    # the computed sessions match what E7.16 writes for them (same code path)
    _run_stats(data_dir, ["AAA"])
    full = read_daily(data_dir, "AAA").set_index("date")
    for d in days[30:]:
        assert rows[d].iv30 == pytest.approx(float(full.loc[d, "iv30"]), abs=1e-9)
    assert rows[days[35]].source == THETA and rows[days[35]].detail["provider"] == "thetadata"


def test_seed_is_append_only_and_idempotent(conn: sqlite3.Connection, data_dir: Path) -> None:
    _run_stats(data_dir, ["AAA", "BBB"])
    first = seed(conn, data_dir, ["AAA", "BBB"], now=NOW)
    assert first.stored > 0 and not first.errors
    store = IvStore(conn)
    day = store.rows("AAA", [THETA])[5].day
    conn.execute(
        "UPDATE iv_daily SET iv30 = 9.9 WHERE ticker='AAA' AND day=? AND source=?",
        (day.isoformat(), THETA),
    )
    again = seed(conn, data_dir, ["AAA", "BBB"], now=NOW + dt.timedelta(days=1))
    assert again.stored == 0
    assert store.series("AAA")[day] == 9.9  # never rewritten
    before = store.counts()
    _rows(store, "AAA", [day], FORWARD, 0.5)
    assert store.counts()[THETA] == before[THETA]


def test_seed_window_missing_name_and_progress(conn: sqlite3.Connection, data_dir: Path) -> None:
    days = sessions(START, N)
    _run_stats(data_dir, ["AAA"])
    msgs: list[str] = []
    rep = seed(
        conn,
        data_dir,
        ["AAA", "ZZZ"],
        now=NOW,
        since=days[10],
        until=days[14],
        progress=msgs.append,
    )
    got = sorted(r.day for r in IvStore(conn).rows("AAA", [THETA]))
    assert got == [d for d in days[10:15] if d in got] and len(got) >= 4
    assert rep.tickers[1].stored == 0 and rep.tickers[1].error is None  # nothing cached
    assert len(msgs) == 2 and msgs[0].startswith("1/2 AAA: +")


def test_seed_skips_a_session_without_iv30_and_waits_for_a_missing_close(
    conn: sqlite3.Connection, data_dir: Path
) -> None:
    days = sessions(START, N)
    # BBB: no close for the last 3 sessions yet; day 20 has no option rows at all; day 21
    # has rows but none inside the 7-75 DTE IV30 window
    bars = UnderlyingStore(data_dir).read_bars("BBB")
    UnderlyingStore(data_dir).write_bars("BBB", bars.loc[bars.index < days[-3]])
    hist = ParquetHistoryStore(data_dir)
    hist.write_day("thetadata", "BBB", days[20], [], with_oi=True)
    rows21 = synth_rows("BBB", [days[21]], [100.0])[days[21]]
    short = [r for r in rows21 if (r.expiration - days[21]).days < 7][:4]
    far = [r.model_copy(update={"expiration": days[21] + dt.timedelta(days=200)}) for r in rows21]
    hist.write_day("thetadata", "BBB", days[21], short or far, with_oi=True)
    out = seed_ticker(conn, data_dir, "BBB", now=NOW)
    assert out.skipped["no_close"] == 3 and out.skipped["no_options"] == 1
    assert out.skipped["no_iv30"] == 1
    stored = {r.day for r in IvStore(conn).rows("BBB", [THETA])}
    assert days[-1] not in stored
    skips = IvStore(conn).days("BBB", THETA, include_skips=True) - stored
    assert skips == {days[21]}  # close-less and option-less days are retried, not skipped
    write_underlying(data_dir, "BBB", days, spot_path(N, seed=5))
    write_history(data_dir, "BBB", {days[20]: synth_rows("BBB", [days[20]], [100.0])[days[20]]})
    again = seed_ticker(conn, data_dir, "BBB", now=NOW)
    assert again.computed == 4 and not again.skipped


def test_seed_runtime_cap_defers_the_rest(conn: sqlite3.Connection, data_dir: Path) -> None:
    ticks = iter([0.0, 0.0, 10.0, 10.0, 10.0])
    rep = seed(conn, data_dir, ["AAA", "BBB"], now=NOW, max_runtime_s=5, clock=lambda: next(ticks))
    assert [t.ticker for t in rep.tickers] == ["AAA"] and rep.deferred == ["BBB"]
    assert "1 deferred" in rep.summary()


def test_seed_one_bad_name_does_not_stop_the_rest(
    conn: sqlite3.Connection, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import arc.backtest.theta_iv as mod

    real = mod.seed_ticker

    def flaky(conn: Any, d: Any, t: str, **kw: Any) -> Any:
        if t == "AAA":
            raise OSError("disk")
        return real(conn, d, t, **kw)

    monkeypatch.setattr(mod, "seed_ticker", flaky)
    rep = seed(conn, data_dir, ["AAA", "BBB"], now=NOW)
    assert rep.tickers[0].error == "OSError: disk" and rep.tickers[1].stored > 0
    assert rep.metrics()["errors"] == {"AAA": "OSError: disk"}


# ---------------------------------------------------------------------------
# CLI: arc iv backfill --source theta
# ---------------------------------------------------------------------------


def test_cli_backfill_theta(
    data_dir: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from arc.cli import main

    monkeypatch.setenv("ARC_ENV", "paper")
    monkeypatch.setattr("arc.utils.calendar.now_et", lambda: NOW)
    db = str(tmp_path / "scratch.db")
    base = ["iv", "backfill", "--source", "theta", "--data-dir", str(data_dir), "--db", db]
    assert main([*base, "--universe", "c4", "--tickers", "AAA"]) == 2
    assert "exactly one" in capsys.readouterr().err
    assert main([*base, "--universe", "c4"]) == 0
    out = capsys.readouterr().out
    assert "theta_backfill:" in out and "for 3 tickers" in out
    assert "theta_backfill" in out.split("rows per source")[1]
    tf = tmp_path / "names.txt"
    tf.write_text("# C125\naaa\nBBB  # etf\n\n")
    assert main([*base, "--tickers-file", str(tf)]) == 0
    assert "theta_backfill: 0 rows" in capsys.readouterr().out  # idempotent
    c = connect(db)
    n = c.execute("SELECT COUNT(*) FROM iv_daily WHERE source='theta_backfill'").fetchone()[0]
    assert n == 2 * N
    # the Alpaca path still needs --tickers and --since
    assert main(["iv", "backfill", "--db", db]) == 2


# ---------------------------------------------------------------------------
# history.topup: plan, resume, closes, IV append, terminal_down
# ---------------------------------------------------------------------------


class FakeTheta:
    name = "thetadata"

    def __init__(self, source: dict[str, dict[dt.date, list[Any]]]) -> None:
        self.source = source
        self.calls: list[tuple[str, dt.date, dt.date]] = []

    def earliest_date(self) -> dt.date:
        return START

    def fetch_option_eod(
        self, underlying: str, start: dt.date, end: dt.date, *, max_dte: int
    ) -> list[Any]:
        self.calls.append((underlying, start, end))
        rows = self.source.get(underlying, {})
        return [r for d, rs in rows.items() if start <= d <= end for r in rs]


class FakeBars:
    def __init__(self, closes: dict[str, pd.DataFrame]) -> None:
        self.closes = closes
        self.calls: list[list[str]] = []

    def daily_bars(self, symbols: Any, start: dt.date, end: dt.date) -> dict[str, pd.DataFrame]:
        self.calls.append(list(symbols))
        return {
            s: df.loc[(df.index >= start) & (df.index <= end)]
            for s, df in self.closes.items()
            if s in symbols
        }


def _cut(data_dir: Path, ticker: str, keep: int) -> None:
    """Drop *ticker*'s cached sessions after the first *keep* (option rows + closes)."""
    days = sessions(START, N)
    store = ParquetHistoryStore(data_dir)
    for d in days[keep:]:
        store.path_for("thetadata", ticker, d).unlink(missing_ok=True)
    bars = UnderlyingStore(data_dir).read_bars(ticker)
    UnderlyingStore(data_dir).write_bars(ticker, bars.loc[bars.index < days[keep]])


def _topup_fixture(data_dir: Path) -> tuple[FakeTheta, FakeBars]:
    days = sessions(START, N)
    full = {t: synth_rows(t, days, spot_path(N, seed=s)) for t, s in (("AAA", 3), ("BBB", 5))}
    closes = {
        t: pd.DataFrame({"close": spot_path(N, seed=s), "volume": [1e6] * N}, index=list(days))
        for t, s in (("AAA", 3), ("BBB", 5))
    }
    _cut(data_dir, "AAA", 35)
    _cut(data_dir, "BBB", 25)
    return FakeTheta(full), FakeBars(closes)


def test_plan_is_behind_first_and_skips_current_names(data_dir: Path) -> None:
    days = sessions(START, N)
    _cut(data_dir, "AAA", 35)
    _cut(data_dir, "BBB", 25)
    store = ParquetHistoryStore(data_dir)
    got = plan(store, ["AAA", "BBB", "CCC"], end=days[-1], earliest=days[0])
    assert got == [("CCC", days[0]), ("BBB", days[25]), ("AAA", days[35])]
    assert plan(store, ["AAA"], end=days[30], earliest=days[0]) == []
    # free tier: never asks before its earliest servable day
    assert plan(store, ["CCC"], end=days[-1], earliest=days[30]) == [("CCC", days[30])]


def test_topup_resumes_refreshes_closes_and_appends_iv(
    conn: sqlite3.Connection, data_dir: Path
) -> None:
    days = sessions(START, N)
    theta, bars = _topup_fixture(data_dir)
    _run_stats(data_dir, ["AAA"])
    seed(conn, data_dir, ["AAA"], now=NOW)  # AAA seeded through day 34; BBB never seeded
    assert max(r.day for r in IvStore(conn).rows("AAA", [THETA])) == days[34]
    assert seeded_names(conn, ["AAA", "BBB"]) == {"AAA": days[34]}
    res = run_topup(
        data_dir=data_dir,
        names=["AAA", "BBB"],
        end=days[-1],
        earliest=days[0],
        provider_factory=lambda: theta,
        bars=bars,
        max_runtime_s=600,
        need_oi=False,
        conn=conn,
        now=NOW,
    )
    assert [c[0] for c in theta.calls][0] == "BBB"  # behind-first
    assert {c[1] for c in theta.calls if c[0] == "AAA"} == {days[35]}
    assert res.sessions_added == 15 + 5 and not res.errors
    assert res.closes_written == 2 and bars.calls == [["BBB", "AAA"]]
    assert UnderlyingStore(data_dir).read("BBB").index.max() == days[-1]
    got = {r.day: r for r in IvStore(conn).rows("AAA", [THETA])}
    assert {d for d in days[35:]} <= set(got) and got[days[-1]].method == METHOD_TOPUP
    assert IvStore(conn).rows("BBB", [THETA]) == []  # not seeded: waits for the backfill
    assert res.iv["rows"] == 5
    assert "20 sessions added" in res.summary() and "theta_backfill +5" in res.summary()
    # a second run is a no-op: everything is current
    theta.calls.clear()
    again = run_topup(
        data_dir=data_dir,
        names=["AAA", "BBB"],
        end=days[-1],
        earliest=days[0],
        provider_factory=lambda: theta,
        bars=bars,
        max_runtime_s=600,
        need_oi=False,
        conn=conn,
        now=NOW,
    )
    assert theta.calls == [] and again.behind == 0 and again.iv["rows"] == 0
    assert again.summary().startswith("2 names current through")


def test_topup_runtime_cap_defers_and_next_run_resumes(data_dir: Path) -> None:
    days = sessions(START, N)
    theta, bars = _topup_fixture(data_dir)
    ticks = iter([0.0, 0.0, 99.0, 99.0])
    res = run_topup(
        data_dir=data_dir,
        names=["AAA", "BBB"],
        end=days[-1],
        earliest=days[0],
        provider_factory=lambda: theta,
        bars=bars,
        max_runtime_s=60,
        need_oi=False,
        clock=lambda: next(ticks),
    )
    assert [r.underlying for r in res.results] == ["BBB"] and res.deferred == ["AAA"]
    assert "1 deferred" in res.summary()
    nxt = plan(ParquetHistoryStore(data_dir), ["AAA", "BBB"], end=days[-1], earliest=days[0])
    assert nxt == [("AAA", days[35])]


def test_topup_closes_failure_is_reported_not_raised(data_dir: Path) -> None:
    days = sessions(START, N)
    theta, _ = _topup_fixture(data_dir)

    class Down:
        def daily_bars(self, *a: Any) -> Any:
            raise ConnectionError("alpaca 503")

    res = run_topup(
        data_dir=data_dir,
        names=["AAA"],
        end=days[-1],
        earliest=days[0],
        provider_factory=lambda: theta,
        bars=Down(),
        max_runtime_s=600,
        need_oi=False,
    )
    assert res.sessions_added == 5 and res.closes_error == "ConnectionError: alpaca 503"
    assert "closes failed" in res.summary()


def test_refresh_closes_asks_only_for_names_missing_a_session(data_dir: Path) -> None:
    days = sessions(START, N)
    _, bars = _topup_fixture(data_dir)
    write_underlying(data_dir, "AAA", days, spot_path(N, seed=3))
    n = refresh_closes(data_dir, ["AAA", "BBB"], days[20], days[-1], bars, batch=1)
    assert n == 1 and bars.calls == [["BBB"]]
    full = UnderlyingStore(data_dir).read_bars("BBB")
    assert full.index.min() == days[0] and full.index.max() == days[-1]


def _ctx(conn: sqlite3.Connection, data_dir: Path, **block: Any) -> JobContext:
    routines = RoutinesConfig.model_validate(
        {
            "sources": {"history.topup": {"schedule": ["20:30"], "writes": []}},
            "history_topup": {"universe": "c4", "data_dir": str(data_dir), **block},
        }
    )
    kind, spec = routines.step("history.topup")
    return JobContext(
        job="history.topup", kind=kind, spec=spec, run_id="r1", chain_run_id=None,
        scheduled_for=NOW, now=NOW, conn=conn, snapshot=ContextStore(conn).snapshot(NOW),
        routines=routines, settings_factory=lambda: ArcSettings(),
    )  # fmt: skip


def test_handler_terminal_down_skips_with_one_notice_a_day(
    conn: sqlite3.Connection, data_dir: Path
) -> None:
    ctx = _ctx(conn, data_dir)
    with pytest.raises(JobSkippedError, match="terminal_down") as e1:
        history_topup_source(ctx, terminal_check=lambda url: False)
    assert "terminal_down" in e1.value.notice and "127.0.0.1:25503" in e1.value.notice
    assert RoutineStateRepo(conn).get(HISTORY_TOPUP_DOWN_DAY) == "2026-10-09"
    with pytest.raises(JobSkippedError) as e2:
        history_topup_source(ctx, terminal_check=lambda url: False)
    assert e2.value.notice == ""  # deduped for the day


def test_handler_runs_the_topup_on_the_checkpoint(conn: sqlite3.Connection, data_dir: Path) -> None:
    theta, bars = _topup_fixture(data_dir)
    _run_stats(data_dir, ["AAA"])
    seed(conn, data_dir, ["AAA"], now=NOW)
    end = sessions(START, N)[-1]
    ctx = _ctx(conn, data_dir)
    # the run the evening after *end*: *end*'s own 20:30 run can't ask for *end* yet
    later = dt.datetime.combine(end + dt.timedelta(days=1), dt.time(20, 30), tzinfo=ET)
    ctx = JobContext(**{**ctx.__dict__, "now": later, "scheduled_for": later})
    res = history_topup_source(
        ctx, provider_factory=lambda: theta, bars=bars, terminal_check=lambda url: True
    )
    # c4 = AAA, BBB, CCC: CCC (nothing cached, no rows at the vendor) is fetched from the
    # tier's first day and cached as empty sessions, so it isn't asked again
    assert res.metrics["names"] == 3 and res.metrics["behind"] == 3
    assert res.metrics["sessions_added"] == 5 + 15 + N
    assert [c[0] for c in theta.calls][:1] == ["CCC"]
    assert res.metrics["iv"]["rows"] == 5
    assert f"behind names topped up through {end}" in res.summary


def test_servable_session_never_asks_for_today() -> None:
    # the Terminal 400s on current-day `expiration=*` ("Cannot fetch current-day data
    # without specifying an expiration"), so the 20:30 ET run ends on the prior session
    fri = dt.date(2026, 10, 16)
    at = lambda d, h, m: dt.datetime.combine(d, dt.time(h, m), tzinfo=ET)  # noqa: E731
    assert servable_session(at(fri, 20, 30)) == dt.date(2026, 10, 15)
    assert servable_session(at(fri, 9, 0)) == dt.date(2026, 10, 15)
    # after midnight ET Friday is servable; over the weekend too
    assert servable_session(at(dt.date(2026, 10, 17), 0, 54)) == fri
    assert servable_session(at(dt.date(2026, 10, 19), 20, 30)) == fri
    # a UTC timestamp past midnight UTC is still Friday evening in ET
    utc = dt.datetime(2026, 10, 17, 0, 30, tzinfo=dt.UTC)
    assert servable_session(utc) == dt.date(2026, 10, 15)


def test_handler_never_requests_the_current_session(
    conn: sqlite3.Connection, data_dir: Path
) -> None:
    theta, bars = _topup_fixture(data_dir)
    end = sessions(START, N)[-1]
    ctx = _ctx(conn, data_dir, iv_append=False)
    same_day = dt.datetime.combine(end, dt.time(20, 30), tzinfo=ET)
    ctx = JobContext(**{**ctx.__dict__, "now": same_day, "scheduled_for": same_day})
    res = history_topup_source(
        ctx, provider_factory=lambda: theta, bars=bars, terminal_check=lambda url: True
    )
    assert theta.calls and all(e < end for _, _, e in theta.calls)
    assert f"through {previous_session(end)}" in res.summary


def test_handler_every_name_failing_is_a_failure(conn: sqlite3.Connection, data_dir: Path) -> None:
    class Boom(FakeTheta):
        def fetch_option_eod(self, *a: Any, **k: Any) -> list[Any]:
            raise RuntimeError("terminal 500")

    ctx = _ctx(conn, data_dir, iv_append=False)
    with pytest.raises(RuntimeError, match="failed for every name"):
        history_topup_source(
            ctx,
            provider_factory=lambda: Boom({}),
            bars=FakeBars({}),  # never a live Alpaca client: CI has no keys
            terminal_check=lambda u: True,
        )


def test_shipped_job_and_block() -> None:
    r = load_routines()
    _, spec = r.step("history.topup")
    assert spec.schedule == [dt.time(20, 30)]  # type: ignore[union-attr]
    assert spec.lane.value == "background"  # type: ignore[union-attr]
    assert r.history_topup == HistoryTopupSettings()
    assert r.history_topup.universe == "c500" and r.history_topup.theta_tier == "free"
    # the free tier's 20 req/min, and the run ends before the 3h catch-up window closes
    assert r.history_topup.request_interval_s >= 3.0
    assert r.history_topup.max_runtime_s < 3 * 3600


def test_daily_path_is_what_seed_reads(data_dir: Path) -> None:
    _run_stats(data_dir, ["AAA"])
    assert daily_path(data_dir, "AAA").is_file()
