"""E7.16 (D84): per-ticker daily option stats computed from the cached Theta history.

Every column on synthetic fixtures, the point-in-time ×3 test, equality with
``HistoryView``'s IV30 / IV rank (and the ``iv_source: option_stats`` hook), earnings
event stats, the quarterly liquidity table, the runner's completeness / idempotency /
checkpoints, ``--follow`` and the CLI.
"""

from __future__ import annotations

import datetime as dt
import json
import math
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd
import pytest

from arc.backtest.costs import load_cost_model
from arc.backtest.data import DataSettings, HistoryView
from arc.backtest.option_stats import (
    DAILY_COLUMNS,
    EVENT_COLUMNS,
    QUARTERLY_COLUMNS,
    StatsConfig,
    StatsRunner,
    daily_path,
    iv_settings_hash,
    liquidity_quarterly,
    load_manifest,
    read_daily,
    read_iv_points,
    skew_25d,
    stage2_near_atm_oi,
    stats_dir,
    ticker_stats,
)
from arc.backtest.universe import Stage2Config, load_universe
from arc.features.vol import rolling_historical_vol
from tests.backtest_data_fixtures import (
    R,
    sessions,
    spot_path,
    synth_rows,
    write_history,
    write_underlying,
    write_universe,
)

if TYPE_CHECKING:
    from pathlib import Path

COST = load_cost_model()
N = 160
START = dt.date(2025, 3, 3)
CFG = StatsConfig(data=DataSettings(), flat_rate=R, min_iv_obs=20)


def _term_iv(i: int, d: dt.date, e: dt.date) -> float:
    """Upward term structure: 22 vol at 0 DTE + 6 pts per 60 days, gently cycling."""
    return 0.22 + 0.06 * (e - d).days / 60 + 0.03 * math.sin(i / 9.0)


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    days = sessions(START, N)
    spots = spot_path(N)
    write_history(tmp_path, "AAA", synth_rows("AAA", days, spots, iv_fn=_term_iv, oi=400.0))
    write_underlying(tmp_path, "AAA", days, spots)
    write_universe(
        tmp_path,
        ["AAA", "BBB"],
        [(dt.date(2025, 1, 1), "AAA", True), (dt.date(2025, 4, 1), "AAA", True)],
    )
    return tmp_path


def _view(data_dir: Path, **kw: object) -> HistoryView:
    days = sessions(START, N)
    return HistoryView(
        data_dir,
        settings=DataSettings(**kw),  # type: ignore[arg-type]
        universe="c4",
        start=days[0],
        end=days[-1],
        cost=COST,
        flat_rate=R,
        min_iv_obs=20,
    )


def _stats(data_dir: Path, t: str = "AAA", **kw: object):  # noqa: ANN202
    return ticker_stats(data_dir, t, cfg=CFG, cost=COST, **kw)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# columns
# ---------------------------------------------------------------------------


def test_every_daily_column_present_and_typed(data_dir: Path) -> None:
    r = _stats(data_dir)
    df = r.daily
    assert tuple(df.columns) == DAILY_COLUMNS
    assert len(df) == N == r.sessions
    assert df["ticker"].eq("AAA").all()
    assert df["date"].tolist() == sessions(START, N)
    row = df.iloc[100]
    for c in DAILY_COLUMNS:
        if c in ("put_call_vol",):  # every synthetic contract trades 50
            continue
        assert pd.notna(row[c]), c
    assert df["d9_pass"].dtype == "boolean"


def test_iv30_iv60_term_slope_and_front(data_dir: Path) -> None:
    df = _stats(data_dir).daily
    r = df.iloc[100]
    # upward term structure -> iv60 > iv30 > front IV, term_slope = iv60 - iv30
    assert r["iv60"] > r["iv30"] > r["front_iv"]
    assert r["term_slope"] == pytest.approx(r["iv60"] - r["iv30"])
    assert r["front_over_iv30"] == pytest.approx(r["front_iv"] / r["iv30"])
    assert 7 <= r["front_dte"] <= 14
    # cm30 on the BSM fixture recovers ~ the generating vol at 30 DTE
    i = 100
    assert r["iv30"] == pytest.approx(0.22 + 0.06 * 30 / 60 + 0.03 * math.sin(i / 9.0), abs=0.01)


def test_rv_and_vrp_use_the_live_hv(data_dir: Path) -> None:
    df = _stats(data_dir).daily.set_index("date")
    days = sessions(START, N)
    spots = pd.Series(spot_path(N), index=days)
    hv20 = rolling_historical_vol(spots, 20)
    hv60 = rolling_historical_vol(spots, 60)
    d = days[100]
    assert df.at[d, "rv20"] == pytest.approx(hv20[d])
    assert df.at[d, "rv60"] == pytest.approx(hv60[d])
    assert df.at[d, "vrp20"] == pytest.approx(df.at[d, "iv30"] - hv20[d])
    assert pd.isna(df.at[days[10], "rv20"])  # < 21 closes
    assert df.at[d, "spot"] == pytest.approx(spots[d])


def test_spread_quotes_oi_volume_and_shape(data_dir: Path) -> None:
    df = _stats(data_dir).daily
    r = df.iloc[50]
    # synthetic NBBO: half-spread = max(0.01, 2 % of the price) -> spread/mid <= ~4 %
    assert 0.0 < r["atm_spread_pct"] <= 0.045
    assert r["two_sided_pct"] == pytest.approx(1.0)
    assert r["quoted_rows"] == r["rows"]
    assert r["oi_total"] == pytest.approx(400.0 * r["rows"])
    assert r["opt_volume"] == pytest.approx(50.0 * r["rows"])
    assert r["expiries"] == 4 and 35 <= r["max_dte_seen"] <= 42
    assert 0.5 < r["put_call_oi"] < 2.0
    assert r["put_call_vol"] == pytest.approx(r["put_call_oi"])
    # near-ATM (Stage2Config: ATM ± 5 strikes, both rights, every expiry <= 60 DTE)
    # (the fixture drops far-OTM sub-penny rows, so not every side has 11 strikes;
    # the exact strike_range semantics are pinned in the stage2 test below)
    assert 4 * 11 * 400.0 <= r["oi_near_atm"] <= 4 * 2 * 11 * 400.0
    assert r["oi_near_atm"] < r["oi_total"] or r["rows"] <= 88


def test_d9_pass_is_the_live_screen(data_dir: Path) -> None:
    df = _stats(data_dir).daily.set_index("date")
    v = _view(data_dir)
    for d in (sessions(START, N)[5], sessions(START, N)[60]):
        e = v.eligibility("AAA", d)
        assert bool(df.at[d, "d9_pass"]) == e.screen_pass
        m = v.liquidity_metrics(v.load("AAA"), d)
        assert df.at[d, "d9_atm_oi"] == m.atm_open_interest
        expect = "passed" if e.screen_pass else "; ".join(e.screen_reasons)
        assert df.at[d, "d9_reasons"] == expect
    assert "ADV unknown" in df.iloc[3]["d9_reasons"]  # < 20 sessions of volume


def test_skew_25d_on_a_put_skewed_chain() -> None:
    from arc.backtest.marks import RateCurve, quoted_marks

    day = dt.date(2025, 6, 2)
    spot = 100.0

    def iv_fn(i: int, d: dt.date, e: dt.date) -> float:
        return 0.25

    rows = synth_rows("SKW", [day], [spot], iv_fn=iv_fn, strikes_each_side=14, step=2.0)[day]
    raw = pd.DataFrame([r.model_dump() for r in rows])
    flat, _ = quoted_marks(raw, spot, r=RateCurve(R).rate(day), max_spread_pct=0.5)
    sk, p25, c25 = skew_25d(flat)
    assert sk is not None and abs(sk) < 0.01  # flat vol -> ~0 skew
    assert p25 == pytest.approx(0.25, abs=0.01) and c25 == pytest.approx(0.25, abs=0.01)
    tilted = flat.copy()
    tilted.loc[tilted["right"] == "put", "iv"] += 0.05
    sk2, p2, c2 = skew_25d(tilted)
    assert sk2 == pytest.approx(0.05, abs=0.005)
    assert skew_25d(flat.iloc[0:0]) == (None, None, None)
    # one-sided deltas (no 25-delta bracket) -> None
    assert skew_25d(flat[flat["right"] == "call"]) == (None, None, None)


def test_stage2_near_atm_oi_matches_strike_range_semantics() -> None:
    day = dt.date(2025, 6, 2)
    rows = []
    for exp_days in (10, 80):  # the 80-DTE expiry is past max_dte 60
        for k in range(80, 121, 2):
            for right in ("call", "put"):
                rows.append(
                    {
                        "expiration": day + dt.timedelta(days=exp_days),
                        "strike": float(k),
                        "right": right,
                        "open_interest": 10.0,
                    }
                )
    raw = pd.DataFrame(rows)
    cfg = Stage2Config(strike_range=2)
    # ATM 100 ± 2 strikes = 96..104 (5 strikes) x 2 rights x 10, one expiry only
    assert stage2_near_atm_oi(raw, 100.4, day=day, cfg=cfg) == pytest.approx(100.0)
    assert stage2_near_atm_oi(raw.iloc[0:0], 100.0, day=day, cfg=cfg) == 0.0
    assert stage2_near_atm_oi(raw, float("nan"), day=day, cfg=cfg) == 0.0


# ---------------------------------------------------------------------------
# point in time + equality with HistoryView
# ---------------------------------------------------------------------------


def _triple_after(data_dir: Path, ticker: str, cut: dt.date) -> None:
    from arc.backtest.entry_filter import OhlcStore
    from arc.backtest.underlying import UnderlyingStore
    from arc.data.history.store import ParquetHistoryStore

    st = ParquetHistoryStore(data_dir)
    for d in st.cached_dates("thetadata", ticker):
        if d <= cut:
            continue
        p = st.path_for("thetadata", ticker, d)
        df = pd.read_parquet(p)
        for c in ("close", "bid", "ask", "open_interest", "volume", "strike"):
            df[c] = df[c] * 3
        df.to_parquet(p, index=False)
    us = UnderlyingStore(data_dir)
    bars = us.read_bars(ticker)
    bars.loc[pd.Index(bars.index) > cut, ["close", "volume"]] *= 3
    us.write_bars(ticker, bars)
    o = OhlcStore(data_dir)
    ohlc = o.read(ticker)
    ohlc.loc[pd.Index(ohlc.index) > cut] *= 3
    o.write(ticker, ohlc)


def test_mutating_every_row_after_d_leaves_day_d_unchanged(data_dir: Path) -> None:
    days = sessions(START, N)
    cut = days[110]
    before = _stats(data_dir).daily
    _triple_after(data_dir, "AAA", cut)
    after = _stats(data_dir).daily
    b = before[before["date"] <= cut].reset_index(drop=True)
    a = after[after["date"] <= cut].reset_index(drop=True)
    pd.testing.assert_frame_equal(a, b)
    assert before.iloc[130]["spot"] != after.iloc[130]["spot"]  # later rows did change


def test_iv30_and_iv_rank_equal_history_view(data_dir: Path) -> None:
    df = _stats(data_dir).daily.set_index("date")
    v = _view(data_dir)
    h = v.load("AAA")
    n = 0
    for d in v.sessions("AAA"):
        p = h.iv[d]
        assert df.at[d, "iv30"] == pytest.approx(p.iv30)
        if p.iv_rank is None:
            assert pd.isna(df.at[d, "iv_rank_252"])
        else:
            n += 1
            assert df.at[d, "iv_rank_252"] == pytest.approx(p.iv_rank)
            assert df.at[d, "iv_pct_252"] == pytest.approx(p.iv_percentile)
        assert df.at[d, "iv_obs"] == p.n_obs
        assert df.at[d, "iv30_expiries"] == p.expiries
    assert n > 100


def test_history_view_hook_reads_daily_with_identical_numbers(data_dir: Path) -> None:
    rows = load_universe(data_dir)[1]
    days = sessions(START, N)
    runner = StatsRunner(data_dir, cfg=CFG, cost=COST, rows=rows, start=days[0], end=days[-1])
    assert runner.run_ticker("AAA") == "computed"
    computed = _view(data_dir).load("AAA").iv
    hooked = _view(data_dir, iv_source="option_stats").load("AAA").iv
    assert hooked == computed
    # other settings -> the hook declines and the view recomputes
    want = iv_settings_hash(DataSettings(), provider="thetadata", flat_rate=R, min_iv_obs=20)
    assert read_iv_points(data_dir, "AAA", expect_settings=want) is not None
    other = iv_settings_hash(
        DataSettings(max_quoted_spread_pct=0.2), provider="thetadata", flat_rate=R, min_iv_obs=20
    )
    assert read_iv_points(data_dir, "AAA", expect_settings=other) is None
    v = _view(data_dir, iv_source="option_stats", max_quoted_spread_pct=0.2)
    assert v.load("AAA").iv == _view(data_dir, max_quoted_spread_pct=0.2).load("AAA").iv
    assert read_iv_points(data_dir, "ZZZ") is None


# ---------------------------------------------------------------------------
# earnings events
# ---------------------------------------------------------------------------


def _earnings_dir(tmp_path: Path) -> tuple[Path, list[dt.date], int]:
    days = sessions(START, 60)
    spots = [100.0] * 59 + [100.0]
    ev_i = 30
    spots[ev_i] = 106.0  # a 6 % reaction move on the event session

    def iv_fn(i: int, d: dt.date, e: dt.date) -> float:
        front = (e - d).days <= 7 and e > days[ev_i] >= d
        return 0.45 if (front and i == ev_i - 1) else 0.25

    write_history(tmp_path, "EEE", synth_rows("EEE", days, spots, iv_fn=iv_fn))
    write_underlying(tmp_path, "EEE", days, spots)
    write_history(tmp_path, "SPY", synth_rows("SPY", days, [100.0] * 60))
    write_underlying(tmp_path, "SPY", days, [100.0] * 60)
    write_universe(
        tmp_path,
        ["SPY", "EEE"],
        [(dt.date(2025, 1, 1), "EEE", True), (dt.date(2025, 1, 1), "SPY", True)],
        asset_types={"SPY": "etf"},
    )
    return tmp_path, days, ev_i


def test_earnings_event_stats(tmp_path: Path) -> None:
    data_dir, days, ev_i = _earnings_dir(tmp_path)
    rows = load_universe(data_dir)[1]
    runner = StatsRunner(data_dir, cfg=CFG, cost=COST, rows=rows, start=days[0], end=days[-1])
    assert runner.pass_once() == {"SPY": "computed", "EEE": "computed"}
    ev = pd.read_parquet(stats_dir(data_dir) / "earnings_events.parquet")
    assert tuple(ev.columns) == EVENT_COLUMNS
    assert len(ev) == 1
    e = ev.iloc[0]
    assert e["ticker"] == "EEE" and pd.Timestamp(e["event_date"]).date() == days[ev_i]
    assert pd.Timestamp(e["prev_date"]).date() == days[ev_i - 1]
    assert e["basis"] == "spy_relative"
    assert e["pre_iv_front"] == pytest.approx(0.45, abs=0.02)
    assert e["iv_crush_pts"] == pytest.approx(e["pre_iv_front"] - e["post_iv_front"])
    assert e["iv_crush_pct"] == pytest.approx(1 - e["post_iv_front"] / e["pre_iv_front"])
    assert e["realised_move"] == pytest.approx(0.06)
    # front ATM straddle at 45 vol, <= 7 DTE: ~0.8 * 0.45 * sqrt(t) of spot
    assert 0.02 < e["implied_move"] < 0.1
    assert e["realised_over_implied"] == pytest.approx(e["realised_move"] / e["implied_move"])
    assert load_manifest(data_dir)["tickers"]["EEE"]["market_ref"] == "SPY"


def test_name_computed_before_spy_is_redone_once_spy_exists(tmp_path: Path) -> None:
    data_dir, days, _ = _earnings_dir(tmp_path)
    rows = load_universe(data_dir)[1]
    runner = StatsRunner(data_dir, cfg=CFG, cost=COST, rows=rows, start=days[0], end=days[-1])
    assert runner.run_ticker("EEE") == "computed"
    assert load_manifest(data_dir)["tickers"]["EEE"]["market_ref"] is None
    assert runner.run_ticker("SPY") == "computed"
    assert runner.run_ticker("EEE") == "computed"  # SPY term now on disk
    assert runner.run_ticker("EEE") == "skipped"
    ev = pd.read_parquet(stats_dir(data_dir) / "earnings_events.parquet")
    assert ev["ticker"].tolist() == ["EEE"]  # replaced, not duplicated


# ---------------------------------------------------------------------------
# quarterly liquidity
# ---------------------------------------------------------------------------


def _daily(
    t: str, days: list[dt.date], *, passes: list[bool], oi: float, vol: float
) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "ticker": t,
            "date": days,
            "d9_pass": passes,
            "oi_near_atm": [oi * (i + 1) for i in range(len(days))],
            "opt_volume": [vol] * len(days),
        }
    )


def test_liquidity_quarterly_score_pass_rate_and_persistence() -> None:
    cal = sessions(dt.date(2025, 1, 2), 190)
    q2, q3, q4 = dt.date(2025, 4, 1), dt.date(2025, 7, 1), dt.date(2025, 10, 1)
    passes = [d >= q3 or d < q2 for d in cal]  # liquid Q1, illiquid Q2, liquid Q3+
    df = liquidity_quarterly(
        {"AAA": _daily("AAA", cal, passes=passes, oi=1.0, vol=10.0)},
        cal,
        cfg=StatsConfig(stage2=Stage2Config(volume_days=20, oi_weight=1.0, volume_weight=2.0)),
        quarters={"AAA": [q2, q3, q4]},
    )
    assert tuple(df.columns) == QUARTERLY_COLUMNS
    r2, r3, r4 = (df[df["quarter"] == q].iloc[0] for q in (q2, q3, q4))
    before_q2 = [d for d in cal if d < q2]
    last = len(before_q2)  # oi on the last session strictly before Q (1-based index)
    assert r2["near_atm_oi"] == pytest.approx(float(last))
    assert r2["opt_volume_20d"] == pytest.approx(200.0) and r2["sessions_used"] == 20
    assert r2["score"] == pytest.approx(last + 2 * 200.0)
    assert r2["score_source"] == "option_stats"
    assert r2["pass_rate_d9"] == 0.0 and not r2["liquid"]
    assert r2["liquid_prev_q"] is None or pd.isna(r2["liquid_prev_q"])  # first listed quarter
    assert r3["pass_rate_d9"] == 1.0 and r3["liquid"] and r3["liquid_prev_q"] is False
    assert not r3["persistent"]
    assert r4["persistent"] and r4["liquid_prev_q"]
    assert r4["sessions_in_q"] == len([d for d in cal if d >= q4])


def test_liquidity_quarterly_reuses_e79_optliq_and_is_point_in_time() -> None:
    cal = sessions(dt.date(2025, 1, 2), 130)
    q2 = dt.date(2025, 4, 1)
    base = _daily("AAA", cal, passes=[True] * len(cal), oi=1.0, vol=10.0)
    a = liquidity_quarterly({"AAA": base}, cal, cfg=CFG, quarters={"AAA": [q2]})
    later = base.copy()
    later.loc[later["date"] >= q2, ["oi_near_atm", "opt_volume"]] *= 3
    b = liquidity_quarterly({"AAA": later}, cal, cfg=CFG, quarters={"AAA": [q2]})
    assert a["score"].tolist() == b["score"].tolist()  # only sessions < Q feed the score
    optliq = pd.DataFrame(
        [{"quarter": q2, "symbol": "AAA", "near_atm_oi": 5.0, "opt_volume_20d": 7.0,
          "score": 12.0, "sessions_used": 20}]
    )  # fmt: skip
    c = liquidity_quarterly({"AAA": base}, cal, cfg=CFG, quarters={"AAA": [q2]}, optliq=optliq)
    assert c.iloc[0]["score"] == 12.0 and c.iloc[0]["score_source"] == "e79_optliq"
    assert liquidity_quarterly({"X": base.iloc[0:0]}, cal, cfg=CFG).empty


# ---------------------------------------------------------------------------
# runner: completeness, idempotency, checkpoints, follow
# ---------------------------------------------------------------------------


def _runner(data_dir: Path, n: int = N, **kw: object) -> StatsRunner:
    _, rows, membership = load_universe(data_dir)
    days = sessions(START, n)
    return StatsRunner(
        data_dir,
        cfg=CFG,
        cost=COST,
        rows=rows,
        start=days[0],
        end=days[-1],
        version="u-test",
        membership=membership,
        **kw,  # type: ignore[arg-type]
    )


def test_runner_waits_for_complete_tickers_and_is_idempotent(data_dir: Path) -> None:
    from arc.data.history.store import ParquetHistoryStore

    r = _runner(data_dir)
    assert r.run_ticker("BBB") == "incomplete"  # nothing cached
    assert r.run_ticker("AAA") == "computed"
    assert daily_path(data_dir, "AAA").is_file()
    assert r.run_ticker("AAA") == "skipped"
    assert _runner(data_dir).run_ticker("AAA") == "skipped"  # manifest survives a restart
    e = load_manifest(data_dir)["tickers"]["AAA"]
    assert e["sessions"] == N and e["complete_in_pull"] and e["last_session"]
    # a session file rewritten (e.g. an OI fill) -> recomputed
    st = ParquetHistoryStore(data_dir)
    d = sessions(START, N)[5]
    rows = synth_rows("AAA", [d], [spot_path(N)[5]], iv_fn=_term_iv)[d]
    st.write_day("thetadata", "AAA", d, rows, with_oi=True)
    assert r.run_ticker("AAA") == "computed"


def test_runner_requires_oi_on_every_session(data_dir: Path) -> None:
    from arc.data.history.store import ParquetHistoryStore

    st = ParquetHistoryStore(data_dir)
    d = sessions(START, N)[7]
    rows = synth_rows("AAA", [d], [spot_path(N)[7]], iv_fn=_term_iv)[d]
    st.write_day("thetadata", "AAA", d, rows, with_oi=False)  # quotes only
    r = _runner(data_dir)
    assert r.run_ticker("AAA") == "incomplete"
    assert r.run_ticker("AAA", require_complete=False) == "computed"
    assert load_manifest(data_dir)["tickers"]["AAA"]["complete_in_pull"] is False
    assert r.checkpoint_status()["c2"]["done"] == 0  # partial never counts


def test_code_change_recomputes(data_dir: Path) -> None:
    r = _runner(data_dir)
    assert r.run_ticker("AAA") == "computed"
    r2 = _runner(data_dir)
    r2.code = "different"
    assert r2.run_ticker("AAA") == "computed"


def test_follow_processes_tickers_as_the_pull_completes_them(data_dir: Path) -> None:
    days = sessions(START, N)
    spots = spot_path(N)
    events: list[str] = []
    polls: list[int] = []
    clock = [dt.datetime(2026, 10, 11, 12, 0, tzinfo=dt.UTC)]

    def sleep(sec: float) -> None:
        assert sec == 600.0
        polls.append(1)
        clock[0] += dt.timedelta(minutes=10)
        if len(polls) == 2:  # the pull finishes BBB between polls
            write_history(data_dir, "BBB", synth_rows("BBB", days, spots))
            write_underlying(data_dir, "BBB", days, spots)

    r = _runner(data_dir)
    n = r.follow(now=lambda: clock[0], sleep=sleep, on_event=events.append)
    assert n == 3 and len(polls) == 2
    # the fixture universe has 2 names, so its only checkpoint label is c2
    assert [e.split(":")[0] for e in events] == ["C2 stats complete"]
    assert "2 tickers" in events[0]
    cps = json.loads((stats_dir(data_dir) / "checkpoints.json").read_text())
    assert cps["c2"]["complete_at"] == "2026-10-11T12:20:00+00:00"
    assert cps["c2"]["rows"] == 2 * N and cps["version"] == "u-test"
    q = pd.read_parquet(stats_dir(data_dir) / "liquidity_quarterly.parquet")
    assert set(q["ticker"]) == {"AAA"}  # BBB has no membership rows
    assert r.record_checkpoints(clock[0]) == []  # complete_at is written once


def test_follow_stops_after_max_polls(data_dir: Path) -> None:
    r = _runner(data_dir)
    assert r.follow(now=lambda: dt.datetime(2026, 1, 1, tzinfo=dt.UTC), sleep=lambda s: None,
                    max_polls=2) == 2  # fmt: skip


def test_a_failing_ticker_does_not_stop_the_pass(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import arc.backtest.option_stats as m

    real = m.ticker_stats

    def boom(data_dir: Path, ticker: str, **kw: object):  # noqa: ANN202
        if ticker == "AAA":
            raise RuntimeError("bad file")
        return real(data_dir, ticker, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(m, "ticker_stats", boom)
    r = _runner(data_dir)
    assert r.pass_once(require_complete=False) == {"BBB": "computed"}


def test_listing_gap_sessions_are_skipped(tmp_path: Path) -> None:
    """Sessions with an empty file and no close (pre-IPO) produce no rows."""
    from arc.data.history.store import ParquetHistoryStore

    days = sessions(START, 40)
    spots = spot_path(40)
    write_history(tmp_path, "NEW", synth_rows("NEW", days[10:], spots[10:]))
    write_underlying(tmp_path, "NEW", days[10:], spots[10:])
    st = ParquetHistoryStore(tmp_path)
    for d in days[:10]:
        st.write_day("thetadata", "NEW", d, [], with_oi=True)
    df = ticker_stats(tmp_path, "NEW", cfg=CFG, cost=COST).daily
    assert df["date"].tolist() == days[10:]
    assert np.isfinite(df["iv30"]).all()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_build_and_show(data_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from arc.cli import main

    days = sessions(START, N)
    common = ["--data-dir", str(data_dir), "--start", days[0].isoformat(),
              "--end", days[-1].isoformat()]  # fmt: skip
    assert main(["history", "stats", "build", "--universe", "c4", *common]) == 0
    out = capsys.readouterr().out
    assert "2 names" in out and "AAA" in out and "'computed': 1" in out
    assert "'incomplete': 1" in out
    assert main(["history", "stats", "build", "--tickers", "AAA", *common]) == 0
    assert "'skipped': 1" in capsys.readouterr().out
    assert main(["history", "stats", "show", "AAA", "--data-dir", str(data_dir)]) == 0
    text = capsys.readouterr().out
    assert f"AAA: {N} sessions" in text and "IV30" in text and "2025" in text
    assert "earnings: no detected events" in text
    assert main(["history", "stats", "show", "ZZZ", "--data-dir", str(data_dir)]) == 1
    assert read_daily(data_dir, "AAA")["date"].iloc[0] == days[0]
    assert (stats_dir(data_dir) / "liquidity_quarterly.parquet").is_file()
    assert (stats_dir(data_dir) / "checkpoints.json").is_file()


def test_cli_follow_exits_when_the_cache_goes_idle(
    data_dir: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from arc.cli import main

    monkeypatch.setattr("arc.backtest.option_stats_cli.time.sleep", lambda s: None)
    days = sessions(START, N)
    rc = main(["history", "stats", "build", "--follow", "--idle-polls", "2", "--poll-seconds",
               "1", "--data-dir", str(data_dir), "--start", days[0].isoformat(), "--end",
               days[-1].isoformat()])  # fmt: skip
    assert rc == 1
    assert "1 names not computed: BBB" in capsys.readouterr().out
