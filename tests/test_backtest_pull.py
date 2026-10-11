"""E7.9 (D84): pull progress, checkpoints.json, the refusal gate and the option-liquidity score."""

from __future__ import annotations

import argparse
import datetime as dt
import json
from typing import TYPE_CHECKING

import pandas as pd
import pytest

from arc.backtest import pull
from arc.backtest import pull_cli as pcli
from arc.backtest.pull import (
    CheckpointIncompleteError,
    CheckpointRecord,
    cut_line,
    format_status,
    measured_rate,
    near_atm_oi,
    optliq_scores,
    optliq_summary,
    pull_status,
    read_checkpoints,
    record_snapshot,
    require_checkpoint,
    verify_oi,
    would_move,
    write_checkpoint,
    write_optliq,
)
from arc.backtest.underlying import UnderlyingStore
from arc.backtest.universe import (
    Stage2Config,
    UniverseBuild,
    UniverseManifest,
    UniverseRow,
    write_build,
)
from arc.data.history.base import OptionEodRow, OptionRight
from arc.data.history.store import ParquetHistoryStore
from arc.utils.calendar import ET, sessions_between

if TYPE_CHECKING:
    from pathlib import Path

P = "thetadata"
SESS = sessions_between(dt.date(2020, 1, 2), dt.date(2020, 1, 30))  # 20 sessions
NOW = dt.datetime(2020, 2, 3, 9, 0, tzinfo=ET)
VERSION = "u4-test"


def rows4() -> list[UniverseRow]:
    cps = ["c1", "c2", "c4", "c4"]
    return [
        UniverseRow(
            position=i + 1,
            symbol=s,
            bucket=1,
            bucket_name="always",
            asset_type="stock",
            checkpoint=cps[i],
        )
        for i, s in enumerate(["AAA", "BBB", "CCC", "DDD"])
    ]


def opt(
    u: str, d: dt.date, strike: float, *, exp: dt.date, right: str = "call", **kw: float
) -> OptionEodRow:
    sym = f"{u}{exp:%y%m%d}{right[0].upper()}{int(strike * 1000):08d}"
    return OptionEodRow(
        provider=P,
        underlying=u,
        date=d,
        symbol=sym,
        expiration=exp,
        strike=strike,
        right=OptionRight(right),
        close=1.0,
        bid=0.9,
        ask=1.1,
        volume=kw.get("volume", 10.0),
        open_interest=kw.get("oi", 100.0),
    )


def fill(data_dir: Path, sym: str, days: list[dt.date], *, with_oi: bool = True) -> None:
    store = ParquetHistoryStore(data_dir)
    for d in days:
        rows = [opt(sym, d, 100.0, exp=d + dt.timedelta(days=20))]
        store.write_day(P, sym, d, rows, with_oi=with_oi)


# -- progress -----------------------------------------------------------------


def test_pull_status_rolls_up_nested_checkpoints(tmp_path: Path) -> None:
    fill(tmp_path, "AAA", SESS)
    fill(tmp_path, "BBB", SESS[:10])
    fill(tmp_path, "CCC", [dt.date(2019, 12, 31)])  # outside the window: not counted
    st = pull_status(tmp_path, rows4(), version=VERSION, provider=P, sessions=SESS)
    c1, c2, c4 = (st.checkpoint(c) for c in ("c1", "c2", "c4"))
    assert (c1.names, c1.sessions_done, c1.complete, c1.names_complete) == (1, 20, True, 1)
    assert (c2.names, c2.sessions_done, c2.complete) == (2, 30, False)
    assert (c4.sessions_total, c4.sessions_done, c4.remaining) == (80, 30, 50)
    assert st.current == "c2"
    assert c1.bytes > 0 and c4.bytes >= c2.bytes >= c1.bytes
    assert st.order == ["AAA", "BBB", "CCC", "DDD"]


def test_cut_line_walks_priority_order() -> None:
    st = pull.PullStatus(
        provider=P,
        version=VERSION,
        start=SESS[0],
        end=SESS[-1],
        sessions=20,
        checkpoints=[],
        done_by_name={"AAA": 20, "BBB": 10},
        order=["AAA", "BBB", "CCC", "DDD"],
    )
    assert cut_line(st, sessions_per_hour=10, hours_left=1) == 2  # BBB needs 10
    assert cut_line(st, sessions_per_hour=10, hours_left=2.9) == 2
    assert cut_line(st, sessions_per_hour=10, hours_left=3) == 3
    assert cut_line(st, sessions_per_hour=100, hours_left=1) == 4
    assert cut_line(st, sessions_per_hour=0, hours_left=99) == 1


def test_format_status_eta_and_behind_warning(tmp_path: Path) -> None:
    fill(tmp_path, "AAA", SESS)
    st = pull_status(tmp_path, rows4(), version=VERSION, provider=P, sessions=SESS)
    txt = format_status(st, now=NOW, sessions_per_hour=10.0)
    assert "names complete 1/4" in txt and "current checkpoint c2" in txt
    assert "c1: complete" in txt and "c4: 25.0%" in txt and "ETA 6 h" in txt
    assert "behind" not in txt
    late = format_status(
        st, now=NOW, sessions_per_hour=1.0, deadline=dt.date(2020, 2, 5), buffer_days=1
    )
    assert "⚠️ behind" in late and "Projected cut line: position 1 (AAA)" in late
    unknown = format_status(st, now=NOW, sessions_per_hour=None)
    assert "ETA ?" in unknown


def test_measured_rate_from_snapshots(tmp_path: Path) -> None:
    fill(tmp_path, "AAA", SESS[:5])
    st = pull_status(tmp_path, rows4(), version=VERSION, provider=P, sessions=SESS)
    assert measured_rate(tmp_path, VERSION, NOW, 5) is None  # no snapshots yet
    record_snapshot(tmp_path, st, NOW - dt.timedelta(hours=30))  # outside the 24 h window
    record_snapshot(tmp_path, st, NOW - dt.timedelta(hours=2))
    assert measured_rate(tmp_path, VERSION, NOW, 25) == pytest.approx(10.0)
    assert measured_rate(tmp_path, VERSION, NOW - dt.timedelta(hours=1, minutes=50), 25) is None
    pull.progress_path(tmp_path, VERSION).write_text("garbage\n{}\n")
    assert measured_rate(tmp_path, VERSION, NOW, 25) is None


# -- checkpoints.json -----------------------------------------------------------


def _rec(**kw: object) -> CheckpointRecord:
    base: dict[str, object] = {
        "complete_at": NOW,
        "sessions": 20,
        "gb": 0.1,
        "rows": 20,
        "names": 1,
        "version": VERSION,
        "start": SESS[0],
        "end": SESS[-1],
    }
    base.update(kw)
    return CheckpointRecord.model_validate(base)


def test_checkpoints_json_roundtrip_keeps_first_complete_at(tmp_path: Path) -> None:
    assert read_checkpoints(tmp_path) == {}
    write_checkpoint(tmp_path, "c125", _rec())
    write_checkpoint(tmp_path, "c125", _rec(complete_at=NOW + dt.timedelta(days=1), gb=0.2))
    got = read_checkpoints(tmp_path)["c125"]
    assert got.complete_at == NOW and got.gb == 0.2
    raw = json.loads(pull.checkpoints_path(tmp_path).read_text())
    assert set(raw["c125"]) >= {"complete_at", "sessions", "gb"}  # the E9 Analyst's contract


def test_require_checkpoint(tmp_path: Path) -> None:
    require_checkpoint(tmp_path, "c250", provider=P, version=VERSION)  # no file: not enforced
    write_checkpoint(tmp_path, "c125", _rec())
    require_checkpoint(tmp_path, "c125", provider=P, version=VERSION)
    with pytest.raises(CheckpointIncompleteError, match=r"c250 is not complete.*complete: c125"):
        require_checkpoint(tmp_path, "c250", provider=P, version=VERSION)
    with pytest.raises(CheckpointIncompleteError, match="not other"):
        require_checkpoint(tmp_path, "c125", provider=P, version="other")
    require_checkpoint(tmp_path, "c125", provider="alpaca", version="other")  # not theta's
    write_checkpoint(tmp_path, "c250", _rec(complete_at=None))
    with pytest.raises(CheckpointIncompleteError):
        require_checkpoint(tmp_path, "C250", provider=P, version=VERSION)


def test_verify_oi_flags_missing_and_unstamped(tmp_path: Path) -> None:
    fill(tmp_path, "AAA", SESS[:3])
    fill(tmp_path, "AAA", SESS[3:4], with_oi=False)
    bad, n = verify_oi(tmp_path, ["AAA"], provider=P, sessions=SESS[:5])
    assert bad == [("AAA", SESS[3]), ("AAA", SESS[4])]
    assert n == 4


# -- option-liquidity score ---------------------------------------------------------


def test_near_atm_oi_strike_window_and_dte() -> None:
    d = dt.date(2020, 3, 31)
    near, far = d + dt.timedelta(days=10), d + dt.timedelta(days=90)
    rows = [opt("X", d, k, exp=near, oi=1.0) for k in range(90, 111)]  # 21 strikes
    rows += [opt("X", d, 100.0, exp=far, oi=1000.0)]  # > max_dte: excluded
    rows += [opt("X", d, 100.0, exp=near, right="put", oi=7.0)]
    df = pd.DataFrame([r.model_dump() for r in rows])
    cfg = Stage2Config(strike_range=2, max_dte=60)
    assert near_atm_oi(df, 100.4, d=d, cfg=cfg) == 5.0 + 7.0  # 98..102 calls + the ATM put
    assert near_atm_oi(df, float("nan"), d=d, cfg=cfg) == 0.0
    assert near_atm_oi(df.iloc[0:0], 100.0, d=d, cfg=cfg) == 0.0


def _optliq_fixture(tmp_path: Path) -> tuple[pd.DataFrame, list[dt.date], Stage2Config]:
    sess = sessions_between(dt.date(2020, 2, 3), dt.date(2020, 4, 30))
    store = ParquetHistoryStore(tmp_path)
    for d in sess:
        exp = d + dt.timedelta(days=30)
        rows = [opt("AAA", d, k, exp=exp, volume=2.0, oi=10.0) for k in (95.0, 100.0, 105.0)]
        store.write_day(P, "AAA", d, rows, with_oi=True)
    mem = pd.DataFrame({"quarter": [dt.date(2020, 4, 1)], "symbol": ["AAA"]})
    return mem, sess, Stage2Config(strike_range=1, volume_days=20, oi_weight=2.0, volume_weight=1.0)


def test_optliq_scores_formula(tmp_path: Path) -> None:
    mem, sess, cfg = _optliq_fixture(tmp_path)
    got = optliq_scores(tmp_path, mem, provider=P, cfg=cfg, sessions=sess, spot=lambda s, d: 100.0)
    (r,) = got
    assert r.quarter == dt.date(2020, 4, 1) and r.sessions_used == 20
    assert r.opt_volume_20d == 20 * 3 * 2.0
    assert r.near_atm_oi == 30.0  # 95/100/105 all within ±1 strike
    assert r.score == 2.0 * 30.0 + 120.0
    none = optliq_scores(tmp_path, mem, provider=P, cfg=cfg, sessions=sess, spot=lambda s, d: None)
    assert none[0].near_atm_oi == 0.0 and none[0].score == 120.0
    other = optliq_scores(
        tmp_path, mem, provider=P, cfg=cfg, sessions=sess, spot=lambda s, d: 1.0, symbols=["ZZZ"]
    )
    assert other == []


def test_optliq_is_point_in_time(tmp_path: Path) -> None:
    """Mutating every row on/after the quarter start leaves the score unchanged."""
    mem, sess, cfg = _optliq_fixture(tmp_path)
    before = optliq_scores(
        tmp_path, mem, provider=P, cfg=cfg, sessions=sess, spot=lambda s, d: 100.0
    )
    store = ParquetHistoryStore(tmp_path)
    for d in sess:
        if d >= dt.date(2020, 4, 1):
            exp = d + dt.timedelta(days=30)
            rows = [opt("AAA", d, 100.0, exp=exp, volume=9e9, oi=9e9)]
            store.write_day(P, "AAA", d, rows, with_oi=True)
    spot_later = {d: (100.0 if d < dt.date(2020, 4, 1) else 1e6) for d in sess}
    after = optliq_scores(
        tmp_path, mem, provider=P, cfg=cfg, sessions=sess, spot=lambda s, d: spot_later[d]
    )
    assert after == before


def test_write_optliq_merges_and_summary(tmp_path: Path) -> None:
    q1, q2 = dt.date(2020, 4, 1), dt.date(2020, 7, 1)
    r = pull.OptLiqRow
    write_optliq(
        tmp_path,
        VERSION,
        [r(quarter=q1, symbol="A", near_atm_oi=1, opt_volume_20d=1, score=2, sessions_used=20)],
    )
    p = write_optliq(
        tmp_path,
        VERSION,
        [
            r(quarter=q1, symbol="A", near_atm_oi=0, opt_volume_20d=0, score=0, sessions_used=0),
            r(quarter=q1, symbol="B", near_atm_oi=5, opt_volume_20d=5, score=10, sessions_used=20),
            r(quarter=q2, symbol="A", near_atm_oi=1, opt_volume_20d=1, score=2, sessions_used=20),
        ],
    )
    df = pd.read_parquet(p)
    assert list(df.columns) == [
        "quarter",
        "symbol",
        "near_atm_oi",
        "opt_volume_20d",
        "score",
        "sessions_used",
    ]
    assert len(df) == 3 and float(df.loc[df["symbol"].eq("A")].iloc[0]["score"]) == 0.0
    s = optliq_summary(df)
    assert list(s["rows"]) == [2, 1] and list(s["pct_scored"]) == [50.0, 100.0]
    assert optliq_summary(df.iloc[0:0]).empty


def test_would_move_report() -> None:
    q = dt.date(2020, 4, 1)
    mem = pd.DataFrame(
        {"quarter": [q] * 3, "symbol": ["A", "B", "C"], "in_top": [True, True, False]}
    )
    df = pd.DataFrame({"quarter": [q] * 3, "symbol": ["A", "B", "C"], "score": [5.0, 1.0, 9.0]})
    moved_in, moved_out = would_move(df, mem, top=2)
    assert moved_in == [("C", 1)] and moved_out == [("B", 1)]


# -- CLI --------------------------------------------------------------------------


def _write_universe(tmp_path: Path) -> None:
    rows = rows4()
    manifest = UniverseManifest.model_validate(
        {
            "version": VERSION,
            "built_at": NOW,
            "config_hash": "x",
            "config": {},
            "as_of": SESS[-1],
            "stage1_only": True,
            "candidate_source": "test",
            "candidate_count": 4,
        }
    )
    build = UniverseBuild(rows=rows, membership=[], manifest=manifest)
    write_build(build, tmp_path)


def _args(tmp_path: Path, **kw: object) -> argparse.Namespace:
    base: dict[str, object] = {
        "version": VERSION,
        "data_dir": tmp_path,
        "provider": P,
        "start": SESS[0],
        "end": SESS[-1],
        "deadline": None,
        "buffer_days": 7,
        "record": True,
        "mark_complete": True,
        "only_events": False,
    }
    base.update(kw)
    return argparse.Namespace(**base)


def test_pull_status_cli_marks_complete_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(pcli, "now_et", lambda: NOW)
    _write_universe(tmp_path)
    fill(tmp_path, "AAA", SESS)
    fill(tmp_path, "BBB", SESS[:-1])
    fill(tmp_path, "BBB", SESS[-1:], with_oi=False)  # all files, one without OI
    assert pcli.run_pull_status(_args(tmp_path)) == 0
    out = capsys.readouterr().out
    assert "✅ C1 complete · 1 names × 20 sessions" in out
    assert "C2 files all present but 1 session(s) lack OI" in out
    recs = read_checkpoints(tmp_path)
    assert set(recs) == {"c1"} and recs["c1"].rows == 20 and recs["c1"].version == VERSION
    assert pull.progress_path(tmp_path, VERSION).is_file()
    # cron mode: no new event -> silent (C2 still lacks OI, C1 already recorded)
    assert pcli.run_pull_status(_args(tmp_path, only_events=True)) == 0
    assert "complete" not in capsys.readouterr().out.replace("lack OI", "")


def test_optliq_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(pcli, "now_et", lambda: dt.datetime(2020, 5, 1, 9, tzinfo=ET))
    mem, sess, _ = _optliq_fixture(tmp_path)
    rows = rows4()
    from arc.backtest.universe import MembershipRow

    membership = [
        MembershipRow(quarter=dt.date(2020, 4, 1), symbol="AAA", in_top=True, in_pull_list=True)
    ]
    manifest = UniverseManifest.model_validate(
        {
            "version": VERSION,
            "built_at": NOW,
            "config_hash": "x",
            "config": {},
            "as_of": sess[-1],
            "stage1_only": True,
            "candidate_source": "test",
            "candidate_count": 4,
        }
    )
    write_build(UniverseBuild(rows=rows, membership=membership, manifest=manifest), tmp_path)
    UnderlyingStore(tmp_path).write("AAA", pd.Series([100.0] * len(sess), index=sess))
    args = argparse.Namespace(
        version=VERSION, data_dir=tmp_path, provider=P, checkpoint="c1", config=None
    )
    assert pcli.run_optliq(args) == 0
    out = capsys.readouterr().out
    assert "1 rows for c1" in out and "weights oi 1.0 volume 1.0" in out
    df = pd.read_parquet(pull.optliq_path(tmp_path, VERSION))
    assert len(df) == 1 and df.iloc[0]["score"] > 0
