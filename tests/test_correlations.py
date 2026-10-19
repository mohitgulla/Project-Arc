"""E28.4 (D131): 60-session return correlations — math, store, job, PIT sim, sync, CLI."""

from __future__ import annotations

import datetime as dt
import math
import sqlite3
from typing import TYPE_CHECKING, Any

import numpy as np
import pandas as pd
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from arc.backtest.correlations import pit_closes, pit_matrices, pit_matrix
from arc.betas.corr import CorrMatrix, cluster_peers, pair_key
from arc.betas.corr_cli import render_show
from arc.betas.corr_store import (
    MAX_AGE_SESSIONS,
    CorrRow,
    compute_rows,
    corr_matrix,
    latest_day,
    replace_day,
)
from arc.betas.refresh import refresh_betas
from arc.data.base import HistoryBar
from arc.experiments.reference import ARM_REFERENCE_TABLES, sync_reference_tables
from arc.features.correlation import (
    CLUSTER_THRESHOLD,
    MIN_OBS,
    WINDOW,
    pearson,
    return_corr,
)
from arc.store.db import connect
from arc.store.migrate import migrate
from arc.utils.calendar import ET, sessions_between

if TYPE_CHECKING:
    from pathlib import Path

NOW = dt.datetime(2026, 10, 8, 7, 10, tzinfo=ET)
DAY = NOW.date()


@pytest.fixture
def conn() -> Any:
    c = connect(":memory:")
    migrate(c)
    return c


def _days(n: int, end: dt.date = DAY - dt.timedelta(days=1)) -> list[dt.date]:
    return sessions_between(end - dt.timedelta(days=2 * n), end)[-n:]


def _series(days: list[dt.date], rets: list[float], start: float = 100.0) -> dict[dt.date, float]:
    out, px = {days[0]: start}, start
    for d, r in zip(days[1:], rets, strict=True):
        px *= math.exp(r)
        out[d] = px
    return out


def _rets(n: int, seed: int) -> list[float]:
    return list(np.random.default_rng(seed).normal(0, 0.015, n))


def _np_corr(a: dict[dt.date, float], b: dict[dt.date, float], window: int = WINDOW) -> float:
    days = sorted(set(a) & set(b))[-(window + 1) :]
    ra = np.diff(np.log([a[d] for d in days]))
    rb = np.diff(np.log([b[d] for d in days]))
    return float(np.corrcoef(ra, rb)[0, 1])


# ---------------------------------------------------------------------------
# math (pure)
# ---------------------------------------------------------------------------


def test_defaults_match_d131() -> None:
    assert (WINDOW, MIN_OBS, CLUSTER_THRESHOLD) == (60, 40, 0.7)


def test_corr_matches_numpy_and_uses_the_last_window() -> None:
    days = _days(200)
    base = _rets(199, 1)
    noise = _rets(199, 2)
    a = _series(days, base)
    # old history uncorrelated, the last 61 closes strongly correlated: only the window counts
    b = _series(
        days,
        noise[:138] + [0.9 * x + 0.3 * y for x, y in zip(base[138:], noise[138:], strict=True)],
    )
    res = return_corr(a, b)
    assert res.n_obs == 60 and res.window == 60 and res.as_of == days[-1]
    assert res.corr == pytest.approx(_np_corr(a, b), abs=1e-12)
    assert res.corr is not None and res.corr > 0.9


def test_perfect_and_inverse_correlation() -> None:
    days = _days(80)
    base = _rets(79, 3)
    a = _series(days, base)
    assert return_corr(a, _series(days, [2 * r for r in base])).corr == pytest.approx(1.0)
    assert return_corr(a, _series(days, [-r for r in base])).corr == pytest.approx(-1.0)
    assert return_corr(a, a).corr == 1.0  # clamped, never 1.0000000000000002


def test_min_obs_rule() -> None:
    days = _days(80)
    a = _series(days, _rets(79, 4))
    b = _series(days, _rets(79, 5))
    # 41 aligned closes = 40 returns: a value; 40 closes = 39 returns: none
    short_ok = {d: c for d, c in b.items() if d >= days[-41]}
    short_no = {d: c for d, c in b.items() if d >= days[-40]}
    assert return_corr(a, short_ok).n_obs == 40 and return_corr(a, short_ok).corr is not None
    res = return_corr(a, short_no)
    assert res.n_obs == 39 and res.corr is None and res.as_of == days[-1]
    assert return_corr({}, b) == return_corr({}, b)  # empty: no as_of
    assert return_corr({}, b).as_of is None and return_corr({}, b).n_obs == 0


def test_alignment_skips_gaps_and_bad_closes() -> None:
    days = _days(100)
    a = _series(days, _rets(99, 6))
    b = _series(days, _rets(99, 7))
    gap = {d: c for d, c in b.items() if d != days[-5]}
    gap[days[-10]] = 0.0  # a bad close drops that date on both sides
    res = return_corr(a, gap)
    a_al = {d: c for d, c in a.items() if d not in (days[-5], days[-10])}
    assert res.corr == pytest.approx(_np_corr(a_al, {d: c for d, c in gap.items() if c > 0}))


def test_flat_series_has_no_value() -> None:
    days = _days(80)
    a = _series(days, _rets(79, 8))
    flat = dict.fromkeys(days, 50.0)
    assert return_corr(a, flat).corr is None
    assert pearson([1.0], [1.0]) is None and pearson([1.0, 2.0], [1.0]) is None


@settings(max_examples=60, deadline=None)
@given(
    st.lists(st.floats(-0.08, 0.08), min_size=45, max_size=70),
    st.lists(st.floats(-0.08, 0.08), min_size=45, max_size=70),
)
def test_property_symmetric_bounded_and_matches_numpy(ra: list[float], rb: list[float]) -> None:
    n = min(len(ra), len(rb))
    days = _days(n + 1)
    a, b = _series(days, ra[:n]), _series(days, rb[:n])
    ab, ba = return_corr(a, b), return_corr(b, a)
    assert ab == ba  # symmetric
    if ab.corr is None:
        return
    assert -1.0 <= ab.corr <= 1.0
    want = _np_corr(a, b)
    if math.isfinite(want):
        assert ab.corr == pytest.approx(want, abs=1e-9)


# ---------------------------------------------------------------------------
# cluster lookup (pure)
# ---------------------------------------------------------------------------


def _matrix() -> CorrMatrix:
    pairs = {
        pair_key("NVDA", "AMD"): 0.82,
        pair_key("NVDA", "MU"): 0.71,
        pair_key("NVDA", "KO"): 0.05,
        pair_key("NVDA", "AVGO"): 0.70,  # exactly the threshold: not a peer (strict >)
        pair_key("AMD", "MU"): 0.76,
    }
    names = frozenset({"NVDA", "AMD", "MU", "KO", "AVGO"})
    return CorrMatrix(day=DAY, pairs=pairs, tickers=names)


def test_pair_key_orders_and_upper_cases() -> None:
    assert pair_key("nvda", "AMD") == ("AMD", "NVDA") == pair_key("AMD", "nvda")


def test_matrix_get_is_symmetric_and_self_is_one() -> None:
    m = _matrix()
    assert m.get("nvda", "amd") == m.get("AMD", "NVDA") == 0.82
    assert m.get("NVDA", "nvda") == 1.0
    assert m.get("NVDA", "TSLA") is None


def test_cluster_peers() -> None:
    m = _matrix()
    open_ = ["amd", "MU", "KO", "AVGO", "NVDA", "TSLA", "AMD"]
    assert cluster_peers(m, "nvda", open_) == ["AMD", "MU"]  # self, dupes, = 0.70 excluded
    assert cluster_peers(m, "NVDA", open_, missing_is_peer=True) == ["AMD", "MU", "TSLA"]
    assert cluster_peers(m, "NVDA", open_, threshold=0.8) == ["AMD"]
    assert cluster_peers(m, "KO", open_) == []
    assert cluster_peers(CorrMatrix(day=None), "NVDA", ["AMD"]) == []
    assert cluster_peers(CorrMatrix(day=None), "NVDA", ["AMD"], missing_is_peer=True) == ["AMD"]


def test_top_pairs() -> None:
    m = _matrix()
    assert m.top(2) == [("AMD", "NVDA", 0.82), ("AMD", "MU", 0.76)]
    assert m.top(5, among=["nvda", "ko", "mu"]) == [("MU", "NVDA", 0.71), ("KO", "NVDA", 0.05)]


# ---------------------------------------------------------------------------
# store
# ---------------------------------------------------------------------------


def _three() -> dict[str, dict[dt.date, float]]:
    days = _days(120)
    base = _rets(119, 10)
    return {
        "nvda": _series(days, base),
        "AMD": _series(
            days, [0.8 * x + 0.2 * y for x, y in zip(base, _rets(119, 11), strict=True)]
        ),
        "KO": _series(days, _rets(119, 12)),
        "THIN": _series(days[-30:], _rets(29, 13)),  # 29 returns: no pairs
    }


def test_compute_rows_pairs_a_lt_b_and_min_obs() -> None:
    rows = compute_rows(_three(), DAY)
    assert [(r.a, r.b) for r in rows] == [("AMD", "KO"), ("AMD", "NVDA"), ("KO", "NVDA")]
    assert all(r.a < r.b and r.n_obs == 60 and r.window == 60 and r.day == DAY for r in rows)
    amd_nvda = next(r for r in rows if (r.a, r.b) == ("AMD", "NVDA"))
    s = _three()
    assert amd_nvda.corr == pytest.approx(_np_corr(s["AMD"], s["nvda"]), abs=1e-6)


def test_replace_day_and_matrix(conn: sqlite3.Connection) -> None:
    rows = compute_rows(_three(), DAY)
    assert replace_day(conn, DAY, rows, now=NOW) == 3
    # a re-run replaces the day (a pair that disappeared does not linger)
    assert replace_day(conn, DAY, rows[:2], now=NOW) == 2
    assert conn.execute("SELECT count(*) FROM correlations").fetchone()[0] == 2
    # a reversed-order row is stored as a < b
    rev = CorrRow(DAY, "ZZ", "AA", 0.9, 50, 60, DAY - dt.timedelta(days=1))
    replace_day(conn, DAY, [*rows[:2], rev], now=NOW)
    m = corr_matrix(conn, DAY)
    assert m.day == DAY and m.get("zz", "aa") == 0.9 and ("AA", "ZZ") in m.pairs
    assert m.tickers == frozenset({"AMD", "KO", "NVDA", "AA", "ZZ"})


def test_matrix_freshness_and_exact_day(conn: sqlite3.Connection) -> None:
    rows = compute_rows(_three(), DAY)
    replace_day(conn, DAY, rows, now=NOW)
    later = sessions_between(DAY, DAY + dt.timedelta(days=20))
    fresh, stale = later[MAX_AGE_SESSIONS], later[MAX_AGE_SESSIONS + 1]
    assert corr_matrix(conn, fresh).day == DAY
    assert corr_matrix(conn, stale).day is None  # too old: every pair unknown
    assert corr_matrix(conn, stale, day=DAY).day == DAY  # an exact day ignores age
    assert corr_matrix(conn, DAY - dt.timedelta(days=1)).day is None  # never a future row
    assert corr_matrix(conn, DAY, day=DAY - dt.timedelta(days=1)).day is None  # no rows
    assert latest_day(conn, stale) == DAY
    assert corr_matrix(None, DAY).day is None and latest_day(None, DAY) is None


def test_old_store_without_the_table() -> None:
    bare = sqlite3.connect(":memory:")
    assert latest_day(bare, DAY) is None
    assert corr_matrix(bare, DAY).day is None
    assert corr_matrix(bare, DAY, day=DAY).day is None


# ---------------------------------------------------------------------------
# the betas job also writes correlations (one bars pull)
# ---------------------------------------------------------------------------


class _Bars:
    def __init__(self, series: dict[str, dict[dt.date, float]]) -> None:
        self.series = series
        self.calls: list[str] = []

    def history_bars(self, symbol, start, end, timeframe="1Day", feed=None):  # noqa: ANN001, ANN201
        self.calls.append(symbol)
        if symbol == "BAD":
            raise RuntimeError("bars down")
        return [
            HistoryBar(
                timestamp=dt.datetime.combine(d, dt.time(4), tzinfo=dt.UTC),
                open=c,
                high=c,
                low=c,
                close=c,
                volume=1.0,
            )  # fmt: skip
            for d, c in sorted(self.series.get(symbol, {}).items())
            if start <= d <= end
        ]


def _market() -> _Bars:
    days = _days(300)
    spy = _rets(299, 20)
    mk = lambda k, seed: _series(  # noqa: E731
        days, [k * x + (1 - k) * y for x, y in zip(spy, _rets(299, seed), strict=True)]
    )
    return _Bars(
        {
            "SPY": _series(days, spy),
            "QQQ": mk(0.9, 21),
            "IWM": mk(0.7, 22),
            "NVDA": mk(0.6, 23),
            "AMD": mk(0.6, 24),
            "KO": mk(0.0, 25),
        }
    )


def test_refresh_writes_pairs_from_the_same_fetch(conn: sqlite3.Connection) -> None:
    m = _market()
    ticks = iter([1.0, 1.25])
    res = refresh_betas(
        conn, m, ["NVDA", "AMD", "KO", "BAD"], DAY, now=NOW, clock=lambda: next(ticks)
    )
    # one bars request per symbol (SPY once): no second pull for the correlations
    assert sorted(m.calls) == sorted(["SPY", "NVDA", "AMD", "KO", "BAD", "QQQ", "IWM"])
    names = ["AMD", "IWM", "KO", "NVDA", "QQQ", "SPY"]  # BAD failed: no pairs
    assert len(res.corr_rows) == len(names) * (len(names) - 1) // 2 == 15
    assert res.corr_seconds == pytest.approx(0.25)
    stored = conn.execute("SELECT count(*), min(n_obs), max(window) FROM correlations").fetchone()
    assert tuple(stored) == (15, 60, 60)
    mx = corr_matrix(conn, DAY)
    assert mx.get("SPY", "QQQ") == pytest.approx(
        _np_corr(m.series["SPY"], m.series["QQQ"]), abs=1e-6
    )
    met = res.metrics()
    assert met["correlations"] == 15 and met["corr_seconds"] == 0.25
    assert met["top_pairs"][0][:2] == ["QQQ", "SPY"]
    assert res.top_pairs(1) == [tuple(met["top_pairs"][0])]


def test_refresh_never_reads_a_bar_on_the_row_day(conn: sqlite3.Connection) -> None:
    m = _market()
    # a by-hand mid-session run: a (partial) bar dated the row day must not count
    for s in m.series.values():
        s[DAY] = next(reversed(s.values())) * 1.5
    m.history_bars = _ignore_end(m.history_bars)  # type: ignore[method-assign]
    res = refresh_betas(conn, m, ["NVDA"], DAY, now=NOW)
    assert all(r.as_of < DAY for r in res.corr_rows)


def _ignore_end(fn):  # noqa: ANN001, ANN202
    def wrapped(symbol, start, end, timeframe="1Day", feed=None):  # noqa: ANN001, ANN202, ARG001
        return fn(symbol, start, DAY, timeframe)

    return wrapped


# ---------------------------------------------------------------------------
# backtest: point in time
# ---------------------------------------------------------------------------


def _pd(series: dict[dt.date, float]) -> pd.Series:
    return pd.Series(list(series.values()), index=list(series.keys()), dtype=float)


def test_pit_matrix_equals_the_live_job_on_the_same_closes(conn: sqlite3.Connection) -> None:
    m = _market()
    refresh_betas(conn, m, ["NVDA", "AMD", "KO"], DAY, now=NOW)
    live = corr_matrix(conn, DAY)
    sim = pit_matrix({t: _pd(s) for t, s in m.series.items()}, DAY)
    assert sim.day == DAY and set(sim.pairs) == set(live.pairs)
    for k, v in live.pairs.items():
        assert sim.pairs[k] == pytest.approx(v, abs=1e-6)


def test_pit_has_no_look_ahead() -> None:
    m = _market()
    closes = {t: _pd(s) for t, s in m.series.items()}
    day = _days(300)[-30]
    before = pit_matrix(closes, day)
    # rewrite every close on or after *day* (a crash in NVDA/KO only from day on)
    shocked = {}
    for t, s in closes.items():
        s2 = s.copy()
        after = [d for d in s2.index if d >= day]
        s2.loc[after] = s2.loc[after] * (0.3 if t in ("NVDA", "KO") else 1.0)
        shocked[t] = s2
    assert pit_matrix(shocked, day).pairs == before.pairs
    assert all(max(v) < day for v in pit_closes(shocked, day).values())
    # the next sessions do see it, and pit_matrices covers each session
    sessions = [d for d in _days(300) if d >= day][:3]
    got = pit_matrices(shocked, sessions)
    assert list(got) == sessions and got[day].pairs == before.pairs
    assert got[sessions[2]].pairs != before.pairs


def test_pit_keeps_a_bounded_tail() -> None:
    days = _days(300)
    s = _pd(_series(days, _rets(299, 30)))
    kept = pit_closes({"x": s}, DAY)["X"]
    assert len(kept) == 3 * (WINDOW + 1) and max(kept) == days[-1]


# ---------------------------------------------------------------------------
# arms sync it (D106)
# ---------------------------------------------------------------------------


def test_correlations_is_a_reference_table_and_syncs_as_of_the_chain(
    conn: sqlite3.Connection,
) -> None:
    assert "correlations" in {t.name for t in ARM_REFERENCE_TABLES}
    rows = compute_rows(_three(), DAY)
    replace_day(conn, DAY, rows, now=NOW)
    nxt = DAY + dt.timedelta(days=1)
    replace_day(conn, nxt, [CorrRow(nxt, "AMD", "NVDA", 0.5, 60, 60, DAY)], now=NOW)
    arm = connect(":memory:")
    migrate(arm)
    chain = dt.datetime.combine(DAY, dt.time(12, 30), tzinfo=ET)
    assert sync_reference_tables(conn, arm, as_of=chain)["correlations"] == 3
    assert corr_matrix(arm, DAY).pairs == corr_matrix(conn, DAY).pairs
    assert (
        arm.execute(
            "SELECT count(*) FROM correlations WHERE day > ?", (DAY.isoformat(),)
        ).fetchone()[0]
        == 0
    )
    assert sync_reference_tables(conn, arm, as_of=chain)["correlations"] == 0  # idempotent


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_render_show() -> None:
    out = render_show(
        _matrix(), "nvda", open_tickers=["AMD", "KO", "TSLA", "NVDA"], top=3, threshold=0.7
    )
    assert out.splitlines() == [
        f"NVDA · correlations for {DAY} · 60-session log returns",
        "  AMD      0.820  cluster",
        "  MU       0.710  cluster",
        "  AVGO     0.700",
        "  … 1 more",
        "2 names above 0.7",
        "open-book peers: AMD · unknown pairs: TSLA",
    ]
    assert render_show(CorrMatrix(day=None), "X", open_tickers=[], top=5, threshold=0.7).startswith(
        "no fresh correlations"
    )
    lone = render_show(_matrix(), "TSLA", open_tickers=[], top=5, threshold=0.7)
    assert f"TSLA has no stored pairs on {DAY}" in lone and "open-book peers: none" in lone


def test_cli_show_end_to_end(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from arc.cli import main

    db = tmp_path / "arc.db"
    c = connect(str(db))
    migrate(c)
    replace_day(c, DAY, compute_rows(_three(), DAY), now=NOW)
    c.close()
    rc = main(["correlations", "show", "nvda", "--day", DAY.isoformat(), "--db", str(db)])
    out = capsys.readouterr().out
    assert rc == 0 and f"NVDA · correlations for {DAY}" in out  # stdout may carry log lines
    assert "AMD" in out and "KO" in out
    rc = main(["correlations", "show", "NVDA", "--day", "2020-01-02", "--db", str(db)])
    assert rc == 1 and "no fresh correlations" in capsys.readouterr().out
    with pytest.raises(FileNotFoundError):
        main(["correlations", "show", "NVDA", "--db", str(tmp_path / "missing.db")])
