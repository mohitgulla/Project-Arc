"""E7.7 (D84): the point-in-time backtest universe builder."""

from __future__ import annotations

import argparse
import datetime as dt
import sqlite3
from typing import TYPE_CHECKING

import pandas as pd
import pytest

from arc.backtest import universe_cli as ucli
from arc.backtest.ranking import load_ranking_file
from arc.backtest.underlying import UnderlyingStore
from arc.backtest.universe import (
    Bucket,
    Stage2Config,
    UniverseConfig,
    UniverseError,
    build_universe,
    checkpoint_symbols,
    classify_asset,
    ever_traded,
    format_build,
    load_bars,
    load_universe,
    open_ro,
    quarter_starts,
    run_stage2,
    stage1_quarter,
    stage2_score,
    write_build,
)
from arc.utils.calendar import ET, sessions_between

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

AS_OF = dt.date(2021, 3, 31)
BUILT = dt.datetime(2021, 4, 1, 8, 0, tzinfo=ET)
SESSIONS = sessions_between(dt.date(2019, 9, 1), AS_OF)
Q = quarter_starts(dt.date(2020, 1, 1), AS_OF)  # 6 quarters 2020-01 .. 2021-01


def small_cfg(**kw: object) -> UniverseConfig:
    base: dict[str, object] = {
        "target_size": 6,
        "checkpoints": [2, 4, 6],
        "dv_window": 20,
        "min_sessions": 10,
        "max_stale_sessions": 3,
        "stage1_keep": 50,
        "always": ["SPY"],
    }
    base.update(kw)
    return UniverseConfig.model_validate(base)


def bars_for(
    dv: float,
    *,
    price: float = 50.0,
    start: dt.date = SESSIONS[0],
    end: dt.date = AS_OF,
    dv_by_day: Mapping[dt.date, float] | None = None,
) -> pd.DataFrame:
    days = [d for d in SESSIONS if start <= d <= end]
    vol = [(dv_by_day or {}).get(d, dv) / price for d in days]
    return pd.DataFrame({"close": [price] * len(days), "volume": vol}, index=days, dtype=float)


def universe(**over: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """Ten names, A0 biggest; *over* replaces a name's bars."""
    out = {f"A{i}": bars_for(1e9 / (i + 1)) for i in range(10)}
    out["SPY"] = bars_for(1e3)  # tiny: only the always bucket keeps it
    out.update(over)
    return out


def build(bars: Mapping[str, pd.DataFrame], cfg: UniverseConfig | None = None, **kw: object):
    cfg = cfg or small_cfg()
    names: dict[str, str | None] = {s: f"{s} Inc" for s in bars}
    names.update(kw.pop("names", {}))  # type: ignore[arg-type]
    return build_universe(
        candidates=names,
        bars=bars,
        cfg=cfg,
        as_of=AS_OF,
        built_at=BUILT,
        sessions=SESSIONS,
        **kw,  # type: ignore[arg-type]
    )


def member(b, q: dt.date) -> set[str]:  # noqa: ANN001
    return {m.symbol for m in b.membership if m.quarter == q and m.in_top}


# -- quarters / stage 1 ------------------------------------------------------


def test_quarter_starts() -> None:
    assert quarter_starts(dt.date(2020, 1, 1), dt.date(2020, 12, 31)) == [
        dt.date(2020, 1, 1),
        dt.date(2020, 4, 1),
        dt.date(2020, 7, 1),
        dt.date(2020, 10, 1),
    ]
    assert quarter_starts(dt.date(2020, 2, 15), dt.date(2020, 7, 1))[0] == dt.date(2020, 4, 1)
    assert quarter_starts(dt.date(2021, 1, 1), dt.date(2020, 1, 1)) == []


def test_no_look_ahead_bars_after_q_do_not_change_q() -> None:
    q = dt.date(2020, 7, 1)
    base = build(universe())
    # every bar on/after Q changed: A9 explodes, A0 dies, prices jump
    after = {d for d in SESSIONS if d >= q}
    mutated = {
        s: df.assign(
            volume=[
                v * (1e6 if s == "A9" else 1e-9) if d in after else v
                for d, v in df["volume"].items()
            ]
        )
        for s, df in universe().items()
    }
    changed = build(mutated)
    for qq in [x for x in Q if x <= q]:
        a = [(m.symbol, m.rank, m.stage1_dv) for m in base.membership if m.quarter == qq]
        b = [(m.symbol, m.rank, m.stage1_dv) for m in changed.membership if m.quarter == qq]
        assert a == b, qq
    # ...and later quarters do see it
    assert member(changed, dt.date(2020, 10, 1)) != member(base, dt.date(2020, 10, 1))


def test_window_uses_only_sessions_strictly_before_q() -> None:
    q = dt.date(2020, 4, 1)
    cfg = small_cfg()
    window = [d for d in SESSIONS if d < q][-cfg.dv_window :]
    df = bars_for(1e6)
    df.loc[q, ["close", "volume"]] = [50.0, 1e12]  # a huge bar ON Q
    (rec,) = stage1_quarter({"X": df}, window, cfg)
    assert rec.dv == pytest.approx(1e6)


def test_delisted_name_only_in_its_own_quarters() -> None:
    # DEAD is the biggest name until it stops trading in May 2020
    dead = bars_for(5e9, end=dt.date(2020, 5, 15))
    b = build(universe(DEAD=dead), small_cfg(target_size=12, checkpoints=[2, 12]))
    assert "DEAD" in member(b, dt.date(2020, 1, 1))
    assert "DEAD" in member(b, dt.date(2020, 4, 1))
    for q in Q[2:]:
        assert "DEAD" not in member(b, q), q
    row = next(r for r in b.rows if r.symbol == "DEAD")
    assert (row.first_q, row.last_q, row.quarters_in) == (
        dt.date(2020, 1, 1),
        dt.date(2020, 4, 1),
        2,
    )


def test_ipo_needs_min_sessions_before_entering() -> None:
    ipo = bars_for(9e9, start=dt.date(2020, 6, 22))  # 7 sessions before 2020-07-01
    b = build(universe(IPO=ipo))
    assert "IPO" not in member(b, dt.date(2020, 7, 1))
    assert "IPO" in member(b, dt.date(2020, 10, 1))


def test_min_price_screens_and_is_a_knob() -> None:
    cheap = bars_for(9e9, price=5.0)
    assert "CHEAP" not in {r.symbol for r in build(universe(CHEAP=cheap)).rows}
    b = build(universe(CHEAP=cheap), small_cfg(min_price=1.0))
    assert b.rows[1].symbol == "CHEAP"  # biggest $-volume, right after SPY


def test_missing_underlying_recorded_in_manifest() -> None:
    b = build(universe(), missing_underlying=["GONE", "ALSOGONE"])
    assert b.manifest.missing_underlying == ["ALSOGONE", "GONE"]


# -- buckets / truncation / checkpoints ----------------------------------------


def test_always_and_ever_bypass_the_screens() -> None:
    tiny = bars_for(1.0, price=2.0)  # fails the price screen and is last on $-volume
    cfg = small_cfg(always=["SPY", "TINY"], checkpoints=[3, 6])
    b = build(universe(TINY=tiny), cfg, ever=["NOBARS"])
    syms = [r.symbol for r in b.rows]
    assert syms[:3] == ["SPY", "TINY", "NOBARS"] or set(syms[:3]) == {"SPY", "TINY", "NOBARS"}
    assert [r.bucket for r in b.rows[:3]] == [1, 1, 2]
    nb = next(r for r in b.rows if r.symbol == "NOBARS")
    assert nb.median_rank is None and nb.quarters_in == 0
    assert all(m.bypass for m in b.membership if m.symbol == "NOBARS")


def test_priority_order_buckets_then_median_rank() -> None:
    # PART is the biggest name but only lists for 2 of 6 quarters -> bucket 4 (rest)
    bars = {s: df for s, df in universe().items() if s in {"SPY", "A0", "A1", "A2", "A3", "A4"}}
    bars["PART"] = bars_for(9e10, end=dt.date(2020, 5, 15))
    b = build(bars, small_cfg(target_size=8, checkpoints=[2, 8]), ever=["A4"])
    assert [(r.symbol, r.bucket) for r in b.rows] == [
        ("SPY", 1),
        ("A4", 2),
        ("A0", 3),
        ("A1", 3),
        ("A2", 3),
        ("A3", 3),
        ("PART", 4),
    ]
    b3 = [r.median_rank for r in b.rows if r.bucket == Bucket.PERSISTENT]
    assert b3 == sorted(b3)
    assert next(r for r in b.rows if r.symbol == "PART").median_rank == 1.0


def test_exactly_target_size_and_nested_checkpoints() -> None:
    b = build(universe())  # 11 candidates, target 6
    assert len(b.rows) == 6
    assert [r.position for r in b.rows] == [1, 2, 3, 4, 5, 6]
    assert [r.checkpoint for r in b.rows] == ["c2", "c2", "c4", "c4", "c6", "c6"]
    c2 = checkpoint_symbols(b.rows, "c2")
    c4 = checkpoint_symbols(b.rows, "c4")
    c6 = checkpoint_symbols(b.rows, "c6")
    assert c2 == c4[:2] and c4 == c6[:4] and len(c6) == 6
    assert [c.names for c in b.manifest.checkpoints] == [2, 4, 6]
    with pytest.raises(ValueError, match="c125"):
        checkpoint_symbols(b.rows, "top")


def test_short_list_is_noted_not_padded() -> None:
    b = build({"SPY": bars_for(1e6), "A0": bars_for(1e9)})
    assert len(b.rows) == 2
    assert any("short of target_size" in n for n in b.manifest.notes)


def test_buckets_1_2_overflowing_first_checkpoint_fails() -> None:
    with pytest.raises(UniverseError, match=r"= 3\) exceed the first checkpoint c2"):
        build(universe(), ever=["A1", "A2"])


def test_coverage_gap_counts_top_names_outside_the_list() -> None:
    # every quarter ranks 10 names but the list keeps 6 (SPY + 5): the rest are the gap
    b = build(universe())
    for q in b.manifest.quarters:
        assert q.top == 6
        assert q.top_outside_list == len(member(b, q.quarter) - {r.symbol for r in b.rows})
    assert sum(q.top_outside_list for q in b.manifest.quarters) > 0


def test_etf_stock_split_and_classifier() -> None:
    assert classify_asset("SPDR S&P 500 ETF TRUST") == "etf"
    assert classify_asset("iShares Russell 2000 ETF") == "etf"
    assert classify_asset("Apple Inc.") == "stock"
    assert classify_asset(None) == "unknown"
    b = build(universe(), names={"SPY": "SPDR S&P 500 ETF TRUST", "A0": "Invesco QQQ Trust"})
    c6 = b.manifest.checkpoints[-1]
    assert (c6.etf, c6.stock, c6.unknown) == (2, 4, 0)


def test_config_knobs_change_the_output() -> None:
    base = build(universe())
    assert [r.symbol for r in build(universe(), small_cfg(always=["A9"])).rows][0] == "A9"
    fewer = build(universe(), small_cfg(stage1_keep=3))
    assert {r.symbol for r in fewer.rows} == {"SPY", "A0", "A1", "A2"}
    bigger = build(universe(), small_cfg(target_size=8, checkpoints=[2, 8]))
    assert len(bigger.rows) == 8 and bigger.rows[-1].checkpoint == "c8"
    assert base.manifest.config_hash != fewer.manifest.config_hash
    assert base.manifest.version != fewer.manifest.version


def test_stage2_scores_rerank_and_drop_zero() -> None:
    q = dt.date(2021, 1, 1)
    scores = {(q, s): float(i) for i, s in enumerate(["A0", "A1", "A2", "A3", "A4", "A5"])}
    scores[(q, "A9")] = 1e9  # small underlying, huge options market
    b = build(universe(), stage2_scores=scores)
    m = {x.symbol: x for x in b.membership if x.quarter == q}
    assert m["A9"].rank == 1 and m["A9"].stage2_score == 1e9
    assert "A0" not in m  # score 0 = no options then: dropped from the quarter
    assert next(s for s in b.manifest.quarters if s.quarter == q).rank_basis == "stage2_score"
    assert b.manifest.quarters[0].rank_basis == "stage1_dv"
    s1 = build(universe(), stage2_scores=scores, stage1_only=True)
    assert all(s.rank_basis == "stage1_dv" for s in s1.manifest.quarters)


def test_config_validation() -> None:
    with pytest.raises(ValueError, match="must equal target_size"):
        UniverseConfig(target_size=500, checkpoints=[125, 250])
    with pytest.raises(ValueError, match="strictly increasing"):
        UniverseConfig(target_size=500, checkpoints=[250, 125, 500])
    with pytest.raises(ValueError, match="min_sessions"):
        UniverseConfig(dv_window=10, min_sessions=20)
    with pytest.raises(ValueError, match="max_stale_sessions"):
        UniverseConfig(dv_window=10, min_sessions=5, max_stale_sessions=20)
    with pytest.raises(ValueError, match="extra"):
        UniverseConfig.model_validate({"bogus": 1})
    c = UniverseConfig()
    assert c.checkpoint_label(1) == "c125" and c.checkpoint_label(126) == "c250"
    assert c.checkpoint_label(500) == "c500"
    with pytest.raises(ValueError, match="past target_size"):
        c.checkpoint_label(501)


def test_ranking_yaml_universe_defaults() -> None:
    u = load_ranking_file().backtest.universe
    assert u.target_size == 500 and u.checkpoints == [125, 250, 500]
    assert u.start == dt.date(2020, 1, 1) and u.min_price == 10.0 and u.stage1_keep == 1500
    assert u.dv_window == 60 and u.persistent_frac == 0.75
    assert u.stage2.max_dte == 60 and u.stage2.strike_range == 5
    assert u.always[:3] == ["SPY", "QQQ", "IWM"]
    assert {"DIA", "XLF", "XLE", "XLK", "BAC", "HD"} <= set(u.always) and len(u.always) == 20
    assert not u.include_index_roots


# -- read-only audit DB -----------------------------------------------------------


def _audit_db(path: Path) -> None:
    c = sqlite3.connect(path)
    c.execute("create table proposals (ticker text)")
    c.execute("create table open_structures (ticker text)")
    c.executemany("insert into proposals values (?)", [("NFLX",), ("nflx",), ("XOM",)])
    c.executemany("insert into open_structures values (?)", [("VST",), ("XOM",)])
    c.commit()
    c.close()


def test_ever_traded_distinct_and_read_only(tmp_path: Path) -> None:
    db = tmp_path / "arc.db"
    _audit_db(db)
    assert ever_traded(db) == ["NFLX", "VST", "XOM"]
    conn = open_ro(db)
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        conn.execute("insert into proposals values ('BAD')")
    conn.close()
    with pytest.raises(sqlite3.OperationalError):
        open_ro(tmp_path / "missing.db").execute("select 1")  # never creates a store
    assert not (tmp_path / "missing.db").exists()


def test_ever_traded_tolerates_missing_tables(tmp_path: Path) -> None:
    db = tmp_path / "empty.db"
    sqlite3.connect(db).close()
    assert ever_traded(db) == []


# -- bars cache ---------------------------------------------------------------------


class FakeBatch:
    def __init__(self, data: Mapping[str, pd.DataFrame]) -> None:
        self.data = data
        self.calls: list[list[str]] = []

    def daily_bars(
        self, symbols: Sequence[str], start: dt.date, end: dt.date
    ) -> dict[str, pd.DataFrame]:
        self.calls.append(list(symbols))
        return {
            s: df[(pd.Index(df.index) >= start) & (pd.Index(df.index) <= end)]
            for s, df in self.data.items()
            if s in symbols
        }


def test_load_bars_caches_and_records_missing(tmp_path: Path) -> None:
    store = UnderlyingStore(tmp_path)
    lo, hi = SESSIONS[0], AS_OF
    src = FakeBatch({"AAA": bars_for(1e6), "DEAD": bars_for(1e6, end=dt.date(2020, 3, 2))})
    ledger = tmp_path / "fetched.json"
    bars, missing = load_bars(
        store, ["AAA", "DEAD", "NONE"], lo, hi, src, batch=2, fetched_path=ledger
    )
    assert set(bars) == {"AAA", "DEAD"} and missing == ["NONE"]
    assert src.calls == [["AAA", "DEAD"], ["NONE"]]
    # second build: nothing re-fetched (DEAD ends early, NONE has none; the ledger knows)
    src2 = FakeBatch({})
    bars2, missing2 = load_bars(
        store, ["AAA", "DEAD", "NONE"], lo, hi, src2, batch=2, fetched_path=ledger
    )
    assert src2.calls == [] and missing2 == ["NONE"]
    pd.testing.assert_frame_equal(bars2["AAA"], bars["AAA"], check_freq=False)
    # offline: cache only
    bars3, _ = load_bars(store, ["AAA", "ZZZ"], lo, hi, None)
    assert set(bars3) == {"AAA"}


def test_underlying_store_keeps_volume_through_close_writes(tmp_path: Path) -> None:
    store = UnderlyingStore(tmp_path)
    df = bars_for(1e6).iloc[:5]
    store.write_bars("X", df)
    store.write("X", store.read("X"))  # the backtester's closes-only path
    got = store.read_bars("X")
    assert got["volume"].tolist() == pytest.approx(df["volume"].tolist())
    # a legacy close-only file reads with NaN volume
    legacy = UnderlyingStore(tmp_path / "legacy")
    legacy.path_for("Y").parent.mkdir(parents=True)
    pd.DataFrame({"date": list(df.index), "close": df["close"].tolist()}).to_parquet(
        legacy.path_for("Y"), index=False
    )
    assert legacy.read_bars("Y")["volume"].isna().all()
    assert len(legacy.read("Y")) == 5


# -- stage 2 ------------------------------------------------------------------------


class Row:
    def __init__(self, volume: float) -> None:
        self.volume = volume


class FakeProbe:
    def __init__(self, earliest: dt.date) -> None:
        self._earliest = earliest
        self.calls: list[tuple[str, str, dt.date, dt.date, int | None]] = []

    def earliest_date(self) -> dt.date:
        return self._earliest

    def fetch_option_eod(self, underlying, start, end, *, max_dte, strike_range=None):  # noqa: ANN001, ANN201
        self.calls.append(("eod", underlying, start, end, strike_range))
        if underlying == "BOOM":
            raise RuntimeError("no such root")
        return [Row(10.0), Row(5.0)]

    def fetch_open_interest(self, underlying, start, end, *, max_dte, strike_range=None):  # noqa: ANN001, ANN201
        self.calls.append(("oi", underlying, start, end, strike_range))
        return {"a": 100.0, "b": 50.0}


def test_stage2_score_window_and_weights() -> None:
    window = [d for d in SESSIONS if d < dt.date(2021, 1, 1)][-20:]
    probe = FakeProbe(dt.date(2020, 1, 1))
    cfg = Stage2Config(oi_weight=2.0, volume_weight=1.0)
    assert stage2_score(probe, "X", window, cfg, with_oi=True) == 2 * 150 + 15
    assert probe.calls[0] == ("eod", "X", window[0], window[-1], 5)
    assert probe.calls[1] == ("oi", "X", window[-1], window[-1], 5)  # OI: the last session only
    assert stage2_score(probe, "X", window, cfg, with_oi=False) == 15


def test_run_stage2_skips_old_quarters_and_resumes(tmp_path: Path) -> None:
    cfg = small_cfg()
    survivors = {dt.date(2020, 7, 1): ["X"], dt.date(2021, 1, 1): ["X", "BOOM"]}
    cache = tmp_path / "s2.parquet"
    probe = FakeProbe(dt.date(2020, 9, 1))
    scores, skipped = run_stage2(probe, survivors, SESSIONS, cfg, with_oi=False, cache_path=cache)
    assert skipped == [dt.date(2020, 7, 1)]
    assert scores == {(dt.date(2021, 1, 1), "X"): 15.0, (dt.date(2021, 1, 1), "BOOM"): 0.0}
    probe2 = FakeProbe(dt.date(2020, 9, 1))
    again, _ = run_stage2(probe2, survivors, SESSIONS, cfg, with_oi=False, cache_path=cache)
    assert again == scores and probe2.calls == []


# -- files + CLI -----------------------------------------------------------------------


def test_write_and_load_round_trip(tmp_path: Path) -> None:
    b = build(universe(DEAD=bars_for(5e9, end=dt.date(2020, 5, 15))), ever=["A7"])
    paths = write_build(b, tmp_path)
    assert {p.name for p in paths.values()} == {
        f"{b.manifest.version}.parquet",
        f"{b.manifest.version}.membership.parquet",
        f"{b.manifest.version}.json",
    }
    manifest, rows, membership = load_universe(tmp_path)
    assert manifest == b.manifest
    assert rows == b.rows
    assert len(membership) == len(b.membership)
    # the pull list feeds `arc history download --tickers-file` in priority order
    from arc.data.history.cli import read_tickers_file

    assert read_tickers_file(paths["pull_list"]) == [r.symbol for r in b.rows]
    text = format_build(b, top=3)
    assert "c2" in text and "top_outside_list" in text and "SPY" in text
    with pytest.raises(FileNotFoundError):
        load_universe(tmp_path / "nothing")


def test_gather_candidates_sources_and_index_roots() -> None:
    cfg = UniverseConfig()
    names, src, by, excl = ucli.gather_candidates(
        cfg,
        theta_symbols=None,
        master_rows={"AAPL": "Apple Inc.", "SPX": "S&P index", "VIX": "vix"},
        inactive={"TWTR": "Twitter Inc", "AAPL": "dup"},
        ever=["NFLX"],
    )
    assert src == "symbol_master" and excl == ["SPX", "VIX"]
    assert by == {"symbol_master": 3, "alpaca_inactive_listed": 1}
    assert names["AAPL"] == "Apple Inc." and "TWTR" in names and "NFLX" in names
    assert "SPY" in names and "SPX" not in names
    names2, src2, _, excl2 = ucli.gather_candidates(
        cfg.model_copy(update={"include_index_roots": True}),
        theta_symbols=["SPX", "AAPL"],
        master_rows={"AAPL": "Apple Inc."},
        inactive={},
        ever=[],
    )
    assert src2 == "thetadata" and excl2 == [] and "SPX" in names2


def _cli_args(tmp_path: Path, **kw: object) -> argparse.Namespace:
    base: dict[str, object] = {
        "universe_command": "build",
        "start": dt.date(2020, 1, 1),
        "as_of": AS_OF,
        "stage1_only": True,
        "data_dir": tmp_path / "data",
        "config": None,
        "db": None,
        "no_db": False,
        "offline": False,
        "theta_url": None,
        "theta_tier": "free",
        "top": 5,
        "symbol_master": None,
    }
    base.update(kw)
    return argparse.Namespace(**base)


def test_cli_build_and_show(tmp_path: Path, monkeypatch, capsys) -> None:  # noqa: ANN001
    db = tmp_path / "arc.db"
    _audit_db(db)
    src = FakeBatch({s: bars_for(1e9 / (i + 1)) for i, s in enumerate(["AAPL", "NFLX", "XOM"])})
    monkeypatch.setattr(ucli, "_theta", lambda args: None)
    monkeypatch.setattr(ucli, "_inactive_listed", dict)
    monkeypatch.setattr(ucli, "_git_sha", lambda: "abc123")
    (tmp_path / "data").mkdir()
    master = {
        "fetched_at": "2021-03-31T08:00:00-04:00",
        "symbols": {
            "AAPL": {"symbol": "AAPL", "name": "Apple Inc.", "options": True},
            "SPX": {"symbol": "SPX", "name": "S&P 500 index", "options": True},
            "OTC1": {"symbol": "OTC1", "name": "No options", "options": False},
        },
    }
    import json

    (tmp_path / "data" / "symbol_master.json").write_text(json.dumps(master))
    rc = ucli._build(_cli_args(tmp_path, db=db), bars_source_factory=lambda: src)
    assert rc == 0
    out = capsys.readouterr().out
    assert "pull_list:" in out and "first 5 rows" in out
    manifest, rows, _ = load_universe(tmp_path / "data")
    assert manifest.stage1_only and manifest.git_sha == "abc123"
    assert manifest.candidate_source == "symbol_master"
    assert manifest.candidates_by_source == {"symbol_master": 2}
    assert manifest.excluded_index_roots == ["SPX"]
    assert "AAPL" not in manifest.missing_underlying and "QQQ" in manifest.missing_underlying
    assert manifest.ever_traded == ["NFLX", "VST"]  # XOM is in the D9 always list
    assert [r.bucket for r in rows][:20] == [1] * 20
    # show + checkpoint
    rc = ucli.run_universe(
        argparse.Namespace(
            universe_command="show", version=None, data_dir=tmp_path / "data",
            checkpoint="c125", top=3,
        )
    )  # fmt: skip
    assert rc == 0
    assert capsys.readouterr().out.splitlines()[:3] == [r.symbol for r in rows[:3]]
    rc = ucli.run_universe(
        argparse.Namespace(
            universe_command="show", version=None, data_dir=tmp_path / "data",
            checkpoint=None, top=3,
        )
    )  # fmt: skip
    assert rc == 0 and manifest.version in capsys.readouterr().out


def test_cli_overflow_exits_2(tmp_path: Path, monkeypatch, capsys) -> None:  # noqa: ANN001
    cfg = tmp_path / "ranking.yaml"
    raw = load_ranking_file().model_dump(mode="json")
    raw["backtest"]["universe"].update({"target_size": 10, "checkpoints": [5, 10]})
    import yaml

    cfg.write_text(yaml.safe_dump(raw))
    monkeypatch.setattr(ucli, "_theta", lambda args: None)
    monkeypatch.setattr(ucli, "_inactive_listed", dict)
    monkeypatch.setattr(ucli, "_load_master", lambda args, now: None)
    rc = ucli._build(
        _cli_args(tmp_path, config=cfg, no_db=True), bars_source_factory=lambda: FakeBatch({})
    )
    assert rc == 2
    assert "exceed the first checkpoint c5" in capsys.readouterr().err


def test_cli_parser_wired() -> None:
    from arc.cli import _make_parser

    ns = _make_parser().parse_args(
        ["history", "universe", "build", "--stage1-only", "--from", "2020-01-01"]
    )
    assert ns.history_command == "universe" and ns.universe_command == "build"
    assert ns.stage1_only and ns.start == dt.date(2020, 1, 1)


# -- ThetaData additions (symbol list, strike_range) ------------------------------------


class _Resp:
    def __init__(self, status: int, text: str) -> None:
        self.status_code = status
        self.text = text


class _ThetaSession:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def get(self, url: str, params: dict[str, object], timeout: float) -> _Resp:
        self.calls.append((url, dict(params)))
        if url.endswith("/list/symbols"):
            return _Resp(200, "symbol\nspy\nAAPL\n\nAAPL\n")
        return _Resp(472, "")


def test_theta_list_symbols_and_strike_range() -> None:
    from arc.data.history.thetadata import ThetaDataEodProvider

    sess = _ThetaSession()
    p = ThetaDataEodProvider(session=sess, tier="value", today=lambda: dt.date(2021, 3, 31))
    assert p.list_option_symbols() == ["AAPL", "SPY"]
    assert (
        p.fetch_option_eod(
            "X", dt.date(2021, 3, 1), dt.date(2021, 3, 2), max_dte=60, strike_range=5
        )
        == []
    )
    sent = [c[1] for c in sess.calls[1:]]
    assert sent and all(c["strike_range"] == 5 for c in sent)
    assert {u.rsplit("/", 1)[-1] for u, _ in sess.calls[1:]} == {"eod", "open_interest"}
    p.fetch_option_eod("X", dt.date(2021, 3, 1), dt.date(2021, 3, 2), max_dte=60)
    assert "strike_range" not in sess.calls[-1][1]  # unchanged default request shape


def test_theta_list_symbols_no_data() -> None:
    from arc.data.history.thetadata import ThetaDataEodProvider

    class NoData(_ThetaSession):
        def get(self, url: str, params: dict[str, object], timeout: float) -> _Resp:
            return _Resp(472, "")

    assert ThetaDataEodProvider(session=NoData()).list_option_symbols() == []
