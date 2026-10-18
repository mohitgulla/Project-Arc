"""E27.1a: the audited OHLC cache refresh (``arc backtest ohlc refresh``).

Replaces only the requested range, dry-run writes nothing, a second run is a no-op,
backups are refused when they exist, and ``load_ohlc`` without a refresh still never
refetches a covered range. The source is injected: no network.
"""

from __future__ import annotations

import argparse
import datetime as dt
import math
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd
import pytest

from arc.backtest.cli import add_backtest_parser
from arc.backtest.entry_filter import OHLC_COLUMNS, OhlcStore, load_ohlc
from arc.backtest.ohlc_refresh import BackupExistsError, refresh_ohlc, run_ohlc_cli
from arc.utils.calendar import ET, sessions_between

if TYPE_CHECKING:
    from pathlib import Path

NOW = dt.datetime(2026, 10, 17, 19, 0, 5, tzinfo=ET)
DAYS = sessions_between(dt.date(2026, 9, 1), dt.date(2026, 10, 16))


def _frame(days: list[dt.date], base: float = 100.0) -> pd.DataFrame:
    c = np.asarray([base + i for i in range(len(days))], dtype=float)
    return pd.DataFrame(
        {"open": c - 0.5, "high": c + 1, "low": c - 1, "close": c, "vwap": c - 0.2}, index=days
    )


class _Src:
    """Serves *df*; records each call."""

    def __init__(self, df: pd.DataFrame) -> None:
        self.df = df
        self.calls: list[tuple[str, dt.date, dt.date]] = []

    def daily_ohlc(self, symbol: str, start: dt.date, end: dt.date) -> pd.DataFrame:
        self.calls.append((symbol, start, end))
        idx = pd.Index(self.df.index)
        return self.df[(idx >= start) & (idx <= end)]


@pytest.fixture
def seeded(tmp_path: Path) -> tuple[OhlcStore, pd.DataFrame, pd.DataFrame]:
    store = OhlcStore(tmp_path)
    cached = _frame(DAYS)
    store.write("AMD", cached)
    revised = cached.copy()
    lo, hi = dt.date(2026, 10, 5), dt.date(2026, 10, 9)
    win = [d for d in DAYS if lo <= d <= hi]
    revised.loc[win, ["open", "high", "low", "close"]] *= 1.0029  # the SIP vs IEX gap size
    return store, cached, revised


def test_refresh_replaces_only_the_requested_range(
    seeded: tuple[OhlcStore, pd.DataFrame, pd.DataFrame],
) -> None:
    store, cached, revised = seeded
    lo, hi = dt.date(2026, 10, 5), dt.date(2026, 10, 9)
    res = refresh_ohlc(store, "amd", lo, hi, _Src(revised), now=NOW, backup_tag="e27.1a")
    assert res.symbol == "AMD" and res.written and res.changes == 5
    assert [r.kind for r in res.rows] == ["changed"] * 5
    first = res.rows[0]
    assert first.before is not None and first.after is not None
    assert first.after["close"] == pytest.approx(first.before["close"] * 1.0029)
    assert first.after["vwap"] == first.before["vwap"]  # vwap untouched by the revision
    after = store.read("AMD")
    inside = [d for d in after.index if lo <= d <= hi]
    outside = [d for d in after.index if not lo <= d <= hi]
    pd.testing.assert_frame_equal(after.loc[inside], revised.loc[inside], check_freq=False)
    pd.testing.assert_frame_equal(after.loc[outside], cached.loc[outside], check_freq=False)
    assert res.backup is not None
    assert res.backup.name == "AMD.parquet.bak-pre-e27.1a-20261017-190005"
    pd.testing.assert_frame_equal(
        OhlcStore(store.root.parent).read("AMD").loc[outside], cached.loc[outside]
    )
    bak = pd.read_parquet(res.backup)
    assert list(bak["close"]) == list(cached["close"])  # the backup is the pre-refresh file


def test_refresh_ignores_source_rows_outside_the_range(
    seeded: tuple[OhlcStore, pd.DataFrame, pd.DataFrame],
) -> None:
    store, cached, revised = seeded

    class _Wide(_Src):  # returns everything, ignoring the requested range
        def daily_ohlc(self, symbol: str, start: dt.date, end: dt.date) -> pd.DataFrame:
            return self.df * 2

    lo = hi = dt.date(2026, 10, 7)
    res = refresh_ohlc(store, "AMD", lo, hi, _Wide(revised), now=NOW)
    assert [r.date for r in res.rows] == [lo]
    after = store.read("AMD")
    others = [d for d in after.index if d != lo]
    pd.testing.assert_frame_equal(after.loc[others], cached.loc[others], check_freq=False)


def test_dry_run_writes_nothing(
    seeded: tuple[OhlcStore, pd.DataFrame, pd.DataFrame], tmp_path: Path
) -> None:
    store, _cached, revised = seeded
    p = store.path_for("AMD")
    before = p.read_bytes()
    res = refresh_ohlc(
        store,
        "AMD",
        dt.date(2026, 10, 5),
        dt.date(2026, 10, 9),
        _Src(revised),
        now=NOW,
        dry_run=True,
    )
    assert res.changes == 5 and res.dry_run and not res.written and res.backup is None
    assert p.read_bytes() == before
    assert sorted(x.name for x in p.parent.iterdir()) == ["AMD.parquet"]


def test_second_run_is_a_no_op(seeded: tuple[OhlcStore, pd.DataFrame, pd.DataFrame]) -> None:
    store, _cached, revised = seeded
    lo, hi = dt.date(2026, 10, 5), dt.date(2026, 10, 9)
    refresh_ohlc(store, "AMD", lo, hi, _Src(revised), now=NOW)
    p = store.path_for("AMD")
    mtime, n_files = p.stat().st_mtime_ns, len(list(p.parent.iterdir()))
    again = refresh_ohlc(store, "AMD", lo, hi, _Src(revised), now=NOW + dt.timedelta(seconds=1))
    assert again.changes == 0 and again.rows == [] and not again.written and again.backup is None
    assert p.stat().st_mtime_ns == mtime and len(list(p.parent.iterdir())) == n_files


def test_existing_backup_is_never_overwritten(
    seeded: tuple[OhlcStore, pd.DataFrame, pd.DataFrame],
) -> None:
    store, _cached, revised = seeded
    p = store.path_for("AMD")
    bak = p.with_name("AMD.parquet.bak-pre-e27.1a-20261017-190005")
    bak.write_bytes(b"older backup")
    before = p.read_bytes()
    with pytest.raises(BackupExistsError):
        refresh_ohlc(
            store,
            "AMD",
            dt.date(2026, 10, 5),
            dt.date(2026, 10, 9),
            _Src(revised),
            now=NOW,
            backup_tag="e27.1a",
        )
    assert bak.read_bytes() == b"older backup" and p.read_bytes() == before


def test_added_and_kept_rows_and_nan_equality(tmp_path: Path) -> None:
    store = OhlcStore(tmp_path)
    cached = _frame(DAYS[:10])
    cached.loc[DAYS[3], "vwap"] = math.nan
    store.write("X", cached)
    src = _frame(DAYS[:12])
    src.loc[DAYS[3], "vwap"] = math.nan  # NaN == NaN: not a change
    src = src.drop(index=[DAYS[5]])  # the source no longer has this day: kept, not deleted
    res = refresh_ohlc(store, "X", DAYS[0], DAYS[11], _Src(src), now=NOW)
    kinds = {r.date: r.kind for r in res.rows}
    assert kinds == {DAYS[5]: "kept", DAYS[10]: "added", DAYS[11]: "added"}
    assert res.changes == 2
    after = store.read("X")
    assert list(after.index) == DAYS[:12]
    assert after.at[DAYS[5], "close"] == cached.at[DAYS[5], "close"]


def test_new_symbol_without_a_cache_file_needs_no_backup(tmp_path: Path) -> None:
    store = OhlcStore(tmp_path)
    res = refresh_ohlc(store, "NEW", DAYS[0], DAYS[4], _Src(_frame(DAYS)), now=NOW)
    assert res.written and res.backup is None and res.changes == 5
    assert list(store.read("NEW").index) == DAYS[:5]


def test_empty_range_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="empty"):
        refresh_ohlc(OhlcStore(tmp_path), "X", DAYS[3], DAYS[1], _Src(_frame(DAYS)), now=NOW)


def test_load_ohlc_without_refresh_never_refetches_a_covered_range(
    seeded: tuple[OhlcStore, pd.DataFrame, pd.DataFrame],
) -> None:
    store, cached, revised = seeded
    src = _Src(revised)
    out = load_ohlc(store, "AMD", DAYS[0], DAYS[-1], source=src)
    assert src.calls == []  # covered: the revised source is never asked
    pd.testing.assert_frame_equal(out, cached, check_freq=False)


def _args(argv: list[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser()
    add_backtest_parser(ap.add_subparsers(dest="command"))
    return ap.parse_args(argv)


def test_cli_dry_run_then_apply_then_noop(
    seeded: tuple[OhlcStore, pd.DataFrame, pd.DataFrame], capsys: pytest.CaptureFixture[str]
) -> None:
    store, _cached, revised = seeded
    data = str(store.root.parent)
    base = ["backtest", "ohlc", "refresh", "--tickers", "amd", "--from", "2026-10-05"]
    base += ["--to", "2026-10-09", "--data-dir", data, "--backup-tag", "e27.1a"]
    clock = iter([NOW, NOW + dt.timedelta(seconds=1), NOW + dt.timedelta(seconds=2)])

    def run(extra: list[str]) -> str:
        assert run_ohlc_cli(_args(base + extra), source=_Src(revised), now=lambda: next(clock)) == 0
        return capsys.readouterr().out

    out = run(["--dry-run"])
    assert "AMD 2026-10-05..2026-10-09: fetched 5, 5 changes (dry-run)" in out
    assert "2026-10-09 changed" in out and "->" in out
    assert "total changes: 5 (dry-run, nothing written)" in out
    out = run([])
    assert "5 changes (written), backup " in out and "bak-pre-e27.1a-20261017-190006" in out
    out = run(["--dry-run"])
    assert "0 changes (dry-run)" in out and "total changes: 0" in out


def test_cli_refuses_an_existing_backup(
    seeded: tuple[OhlcStore, pd.DataFrame, pd.DataFrame], capsys: pytest.CaptureFixture[str]
) -> None:
    store, _cached, revised = seeded
    store.path_for("AMD").with_name("AMD.parquet.bak-pre-refresh-20261017-190005").write_bytes(b"x")
    args = _args(
        ["backtest", "ohlc", "refresh", "--tickers", "AMD", "--from", "2026-10-05"]
        + ["--to", "2026-10-09", "--data-dir", str(store.root.parent)]
    )
    assert run_ohlc_cli(args, source=_Src(revised), now=lambda: NOW) == 3
    assert "refusing to overwrite" in capsys.readouterr().err


def test_columns_contract() -> None:
    assert OHLC_COLUMNS == ("open", "high", "low", "close", "vwap")
