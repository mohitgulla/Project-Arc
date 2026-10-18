"""E27.1 train/serve parity: the live-store adapter and the history-store adapter, fed the
same market facts, give the same features (``compute_panel`` is shared; this proves the
adapters build identical inputs)."""

from __future__ import annotations

import datetime as dt
import json
import uuid
import zlib
from typing import TYPE_CHECKING, Any

import numpy as np
import pandas as pd
import pytest

from arc.iv.store import IvRow, IvStore
from arc.ml.features.adapters.history import history_inputs
from arc.ml.features.adapters.live import live_book, live_inputs, upcoming_earnings
from arc.ml.features.compute import (
    EARN_SRC_CALENDAR,
    EARN_SRC_PROJECTED,
    compute_panel,
    projected_reaction,
)
from arc.ml.features.parity import EXACT_TOL, compare_panels
from arc.ml.features.registry import panel_feature_names, registry
from arc.store.db import connect
from arc.store.migrate import migrate
from arc.utils.calendar import ET, sessions_between

if TYPE_CHECKING:
    from pathlib import Path

TICKERS = ("AAA", "BBB", "CCC")
ALL = (*TICKERS, "SPY")
END = dt.date(2026, 9, 30)
N = 330
NOW = dt.datetime(2026, 10, 1, 9, 0, tzinfo=ET)


@pytest.fixture(scope="module")
def facts() -> dict[str, Any]:
    rng = np.random.default_rng(11)
    sess = sessions_between(END - dt.timedelta(days=int(N * 1.6)), END)[-N:]
    px: dict[str, pd.DataFrame] = {}
    opt: dict[str, pd.DataFrame] = {}
    for k, t in enumerate(ALL):
        r = rng.normal(0.0003, 0.012 + 0.005 * k, N)
        c = np.round(50.0 * (1 + k) * np.exp(np.cumsum(r)), 4)
        o = np.round(c * np.exp(rng.normal(0, 0.004, N)), 4)
        h = np.round(np.maximum(o, c) * 1.006, 4)
        lo = np.round(np.minimum(o, c) * 0.994, 4)
        px[t] = pd.DataFrame({"open": o, "high": h, "low": lo, "close": c}, index=sess)
        iv = np.round(0.22 + 0.04 * k + 0.03 * np.sin(np.arange(N) / 15.0), 6)
        f = pd.DataFrame(index=sess)
        f["iv30"] = iv
        f.iloc[::29, 0] = np.nan  # days without an IV
        f["atm_spread_pct"] = np.nan
        f["opt_volume"] = np.nan
        f["put_call_vol"] = np.nan
        # the scanner snapshots only the last 25 sessions
        for d in sess[-25:]:
            call, put = float(rng.integers(1000, 9000)), float(rng.integers(1000, 9000))
            f.loc[d, ["atm_spread_pct", "opt_volume", "put_call_vol"]] = [
                round(float(rng.uniform(0.02, 0.09)), 4),
                call + put,
                put / call,
            ]
            f.loc[d, "_call"], f.loc[d, "_put"] = call, put
        opt[t] = f
    events = pd.DataFrame(
        {
            "ticker": ["AAA", "AAA", "BBB"],
            "event_date": [sess[150], sess[213], sess[285]],
            "realised_over_implied": [0.7, 1.2, 1.5],
        }
    )
    return {"sess": sess, "px": px, "opt": opt, "events": events}


def _history_dir(tmp: Path, facts: dict[str, Any]) -> Path:
    (tmp / "underlying_ohlc").mkdir(parents=True)
    (tmp / "option_stats" / "daily").mkdir(parents=True)
    for t, df in facts["px"].items():
        df.rename_axis("date").reset_index().to_parquet(tmp / "underlying_ohlc" / f"{t}.parquet")
    for t, df in facts["opt"].items():
        out = df.drop(columns=["_call", "_put"]).rename_axis("date").reset_index()
        out.to_parquet(tmp / "option_stats" / "daily" / f"underlying={t}.parquet")
    facts["events"].to_parquet(tmp / "option_stats" / "earnings_events.parquet")
    return tmp


def _iso_utc(d: dt.date, hh: int, mm: int = 0) -> str:
    return dt.datetime(d.year, d.month, d.day, hh, mm, tzinfo=ET).astimezone(dt.UTC).isoformat()


def _live_store(facts: dict[str, Any]) -> Any:
    conn = connect(":memory:")
    migrate(conn)
    sess = facts["sess"]
    # the loop's daily bars, recorded in two overlapping tape payloads (newest wins)
    for t, df in facts["px"].items():
        bars = [
            {
                "timestamp": _iso_utc(d, 0),  # Alpaca daily bar = midnight ET
                "open": float(r.open), "high": float(r.high), "low": float(r.low),
                "close": float(r.close), "volume": 1e6,
            }
            for d, r in df.iterrows()
        ]  # fmt: skip
        for i, chunk in enumerate((bars[:200], bars[150:])):
            call = f'history_bars(["{t}", "{sess[0]}", "{sess[-1]}", "1Day"])'
            conn.execute(
                "INSERT INTO market_tape (chain_run_id, call, payload, at) VALUES (?, ?, ?, ?)",
                (
                    f"run{i}",
                    call,
                    zlib.compress(json.dumps(chunk).encode()),
                    _iso_utc(sess[-1], 17 + i),
                ),
            )
    store = IvStore(conn)
    rows = [
        IvRow(ticker=t, day=d, iv30=float(v), method="chain_cm30", source="alpaca_cm30")
        for t, df in facts["opt"].items()
        for d, v in df["iv30"].dropna().items()
    ]
    store.upsert(rows, now=NOW)
    for t, df in facts["opt"].items():
        for d, r in df.dropna(subset=["atm_spread_pct"]).iterrows():
            for hh, scale in ((11, 0.5), (15, 1.0)):  # earlier scan is superseded by the last
                payload = {
                    "atm_spread_pct": float(r.atm_spread_pct) if scale == 1.0 else 0.5,
                    "call_volume_td": r._call * scale,
                    "put_volume_td": r._put * scale,
                }
                conn.execute(
                    "INSERT INTO context_entries (id, kind, subject, payload, schema_version, "
                    "produced_by, created_at, valid_from) VALUES (?, 'chain_snapshot', ?, ?, 1, "
                    "'scanner', ?, ?)",
                    (uuid.uuid4().hex, t, json.dumps(payload), _iso_utc(d, hh), _iso_utc(d, hh)),
                )
    # Finnhub calendar: BBB's next report (amc on the projected reaction eve), known late
    cal = sessions_between(sess[0], END + dt.timedelta(days=200))
    nxt = projected_reaction(facts["events"]["event_date"].iloc[2], cal)
    assert nxt is not None and nxt > END
    report = cal[cal.index(nxt) - 1]
    for i, (text, ing) in enumerate(
        [
            (f"Earnings report for BBB on {report}. Reporting: amc EPS estimate: 1.0", sess[-10]),
            (f"Earnings report for ZZZ on {report}.", sess[-20]),
        ]
    ):
        conn.execute(
            "INSERT INTO raw_docs (id, source, url, published_at, text, content_hash, ingested_at) "
            "VALUES (?, 'earnings', ?, ?, ?, ?, ?)",
            (f"d{i}", f"u{i}", f"{report}T00:00:00+00:00", text, f"h{i}", _iso_utc(ing, 18)),
        )
    conn.commit()
    return conn


@pytest.fixture(scope="module")
def panels(facts: dict[str, Any], tmp_path_factory: Any) -> tuple[pd.DataFrame, pd.DataFrame, list]:
    hdir = _history_dir(tmp_path_factory.mktemp("hist"), facts)
    conn = _live_store(facts)
    days = facts["sess"][-30:]
    live = compute_panel(
        live_inputs(conn, ALL, days[0], days[-1], data_dir=hdir, etfs=frozenset({"SPY"})), days, ALL
    )
    hist = compute_panel(
        history_inputs(hdir, ALL, days[0], days[-1], members={}, etfs=frozenset({"SPY"})), days, ALL
    )
    return live, hist, days


def test_same_facts_same_features(panels: tuple[pd.DataFrame, pd.DataFrame, list]) -> None:
    live, hist, days = panels
    assert len(live) == len(hist) == 30 * len(ALL)
    reg = registry()
    feats = [f for f in panel_feature_names() if reg[f].available_live]
    a = live.set_index(["date", "ticker"])
    b = hist.set_index(["date", "ticker"])
    for f in feats:
        if f in ("earn_src", "sess_to_earn"):
            continue  # live knows BBB's Finnhub date; checked below
        x, y = a[f].astype(float), b[f].astype(float)
        assert (x.isna() == y.isna()).all(), f
        np.testing.assert_allclose(x[x.notna()], y[y.notna()], rtol=0, atol=EXACT_TOL, err_msg=f)
    # live features are populated (not trivially NaN == NaN)
    last = a.xs(days[-1], level="date")
    for f in (
        "ret_252_21",
        "iv_rank",
        "iv30_over_har",
        "atm_spread_pct",
        "opt_vol_ratio_20",
        "vix_proxy_pct_252",
    ):
        assert last[f].notna().any(), f
    rep = compare_panels(live, hist, feats)
    assert bool(rep["ok"].all())


def test_earn_src_same_semantics(panels: tuple[pd.DataFrame, pd.DataFrame, list]) -> None:
    live, hist, days = panels
    a = live.set_index(["date", "ticker"])
    b = hist.set_index(["date", "ticker"])
    bbb_l, bbb_h = a.xs("BBB", level="ticker"), b.xs("BBB", level="ticker")
    known = [d for d in days if d >= days[-10]]
    assert (bbb_h["earn_src"] == EARN_SRC_PROJECTED).all()
    assert (bbb_l.loc[known, "earn_src"] == EARN_SRC_CALENDAR).all()
    assert (bbb_l.drop(index=known)["earn_src"] == EARN_SRC_PROJECTED).all()
    # the Finnhub amc report the session before the projected reaction day counts to the same
    # reaction session as the history projection
    pd.testing.assert_series_equal(bbb_l["sess_to_earn"], bbb_h["sess_to_earn"])
    other = [t for t in ALL if t != "BBB"]
    for t in other:
        pd.testing.assert_series_equal(
            a.xs(t, level="ticker")["earn_src"], b.xs(t, level="ticker")["earn_src"]
        )


def test_upcoming_known_at_and_reaction_day(facts: dict[str, Any]) -> None:
    conn = _live_store(facts)
    sess = facts["sess"]
    assert upcoming_earnings(conn, ["BBB"], sess[-11]).empty  # not known yet
    up = upcoming_earnings(conn, ["BBB", "ZZZ"], sess[-1])
    assert up["ticker"].tolist() == ["BBB", "ZZZ"]
    bbb, zzz = up.iloc[0], up.iloc[1]
    assert bbb["known_at"] == sess[-10]
    # amc and unknown hour both react the next session
    assert bbb["reaction_date"] == zzz["reaction_date"]


def test_live_book_reads_open_structures() -> None:
    conn = connect(":memory:")
    migrate(conn)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(open_structures)")]
    assert {"ticker", "structure_json", "contracts", "status"} <= set(cols)
    assert live_book(conn).empty


def test_store_build_resumable(facts: dict[str, Any], tmp_path: Path) -> None:
    from arc.ml.features.store import build_store, date_path, version_dir

    hdir = _history_dir(tmp_path / "hist", facts)
    out = tmp_path / "ml"
    days = facts["sess"]
    first = build_store(hdir, out, list(TICKERS), days[-5], days[-1], members={})
    assert first == {"dates_skipped": 0, "dates_written": 5, "rows": 5 * len(TICKERS)}
    vdir = version_dir(out)
    p = date_path(vdir, days[-1])
    df = pd.read_parquet(p)
    assert list(df.columns[4:]) == panel_feature_names()
    assert (vdir / "manifest.json").is_file()
    again = build_store(hdir, out, list(TICKERS), days[-7], days[-1], members={})
    assert again == {"dates_skipped": 5, "dates_written": 2, "rows": 2 * len(TICKERS)}
    pd.testing.assert_frame_equal(pd.read_parquet(p), df)


def test_input_diffs_names_the_revised_bar(facts: dict[str, Any]) -> None:
    from arc.ml.features.parity import input_diffs

    px = facts["px"]
    rev = {t: df.copy() for t, df in px.items()}
    d = facts["sess"][-2]
    rev["BBB"].loc[d, "close"] *= 1.0004  # a vendor revision after the history pull
    out = input_diffs(rev, px, facts["sess"][-1])
    assert out["ticker"].tolist() == ["BBB"]
    assert out.iloc[0]["rows"] == 1 and out.iloc[0]["first"] == d
    assert out.iloc[0]["max_rel_diff"] == pytest.approx(0.0004, rel=1e-6)
    assert input_diffs(px, px, facts["sess"][-1]).empty


def test_tape_bars_prefers_the_history_feed_per_date() -> None:
    """E27.1a: the loop records a SIP 1Day read and, just after it, a feed-less 10-day
    spot read on the quote feed (IEX). Per date the SIP bar wins regardless of order;
    a date only the other payload has keeps it; within a tier the newest wins."""
    from arc.ml.features.adapters.live import tape_bars

    conn = connect(":memory:")
    migrate(conn)
    d = sessions_between(dt.date(2026, 10, 1), dt.date(2026, 10, 9))

    def bar(day: dt.date, close: float) -> dict[str, Any]:
        o = close - 1
        return {"timestamp": _iso_utc(day, 0), "open": o, "high": close + 1, "low": o - 1,
                "close": close, "volume": 1.0}  # fmt: skip

    def put(call: str, rows: list[dict[str, Any]], hh: int, mm: int) -> None:
        conn.execute(
            "INSERT INTO market_tape (chain_run_id, call, payload, at) VALUES (?, ?, ?, ?)",
            (f"r{hh}{mm}", call, zlib.compress(json.dumps(rows).encode()), _iso_utc(d[-1], hh, mm)),
        )

    sip = 'history_bars(["AMD", "2025-07-02", "2026-10-09", "1Day", "sip"])'
    iex = 'history_bars(["AMD", "2026-09-29", "2026-10-09", "1Day"])'
    put(sip, [bar(x, 600.0) for x in d[:-1]], 9, 40)  # SIP lacks the last day
    put(iex, [bar(x, 599.0) for x in d], 9, 41)  # newer, one-exchange
    put(sip, [bar(d[0], 601.0)], 9, 30)  # an older SIP recording: loses to the 9:40 one
    put('history_bars(["AMD", "2026-10-01", "2026-10-09", "5Min", "sip"])', [], 9, 50)
    out = tape_bars(conn, ["AMD", "MU"])
    assert list(out) == ["AMD"]
    amd = out["AMD"]
    assert list(amd.index) == d
    assert (amd.loc[d[:-1], "close"] == 600.0).all()
    assert amd.at[d[-1], "close"] == 599.0
    assert (tape_bars(conn, ["AMD"], feed="iex")["AMD"]["close"] == 599.0).all()
