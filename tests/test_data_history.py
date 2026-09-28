"""Tests for arc.data.history (E7.1): contracts, providers, parquet store, coverage, CLI."""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
from hypothesis import given
from hypothesis import strategies as st

from arc.data.history import (
    EOD_COLUMNS,
    AlpacaHistoryProvider,
    HistoricalDataProvider,
    OptionEodRow,
    OptionRight,
    ParquetHistoryStore,
    ThetaDataEodProvider,
    ThetaTerminalError,
    download,
    format_coverage,
    missing_ranges,
    occ_symbol,
    parse_eod_csv,
)
from arc.data.history import cli as hcli
from arc.utils.calendar import sessions_between

ET = ZoneInfo("America/New_York")
UTC = dt.UTC


def _row(day: dt.date, strike: float = 500.0, **kw: Any) -> OptionEodRow:
    base: dict[str, Any] = {
        "provider": "fake",
        "underlying": "SPY",
        "date": day,
        "symbol": occ_symbol("SPY", dt.date(2024, 3, 1), OptionRight.CALL, strike),
        "expiration": dt.date(2024, 3, 1),
        "strike": strike,
        "right": OptionRight.CALL,
        "close": 1.25,
    }
    base.update(kw)
    return OptionEodRow(**base)


# ---------------------------------------------------------------------------
# base
# ---------------------------------------------------------------------------


class TestOccSymbol:
    def test_known(self) -> None:
        assert occ_symbol("spy", dt.date(2024, 3, 1), OptionRight.CALL, 500) == "SPY240301C00500000"
        assert (
            occ_symbol("XLE", dt.date(2024, 2, 16), OptionRight.PUT, 82.5) == "XLE240216P00082500"
        )

    @pytest.mark.parametrize("strike", [0, -1, 100_000])
    def test_out_of_range(self, strike: float) -> None:
        with pytest.raises(ValueError, match="OCC range"):
            occ_symbol("SPY", dt.date(2024, 3, 1), OptionRight.PUT, strike)

    @given(
        root=st.text(alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZ", min_size=1, max_size=6),
        exp=st.dates(min_value=dt.date(2000, 1, 1), max_value=dt.date(2099, 12, 31)),
        right=st.sampled_from(list(OptionRight)),
        milli=st.integers(min_value=1, max_value=99_999_999),
    )
    def test_roundtrip(self, root: str, exp: dt.date, right: OptionRight, milli: int) -> None:
        sym = occ_symbol(root, exp, right, milli / 1000)
        assert sym[:-15] == root
        yymmdd = sym[-15:-9]
        assert dt.date(2000 + int(yymmdd[:2]), int(yymmdd[2:4]), int(yymmdd[4:])) == exp
        assert sym[-9] == ("C" if right is OptionRight.CALL else "P")
        assert int(sym[-8:]) == milli


class TestOptionEodRow:
    def test_upper_and_frozen(self) -> None:
        r = _row(dt.date(2024, 2, 1), underlying="spy")
        assert r.underlying == "SPY"
        with pytest.raises(ValueError):
            r.close = 2.0  # type: ignore[misc]

    def test_negative_bid_rejected(self) -> None:
        with pytest.raises(ValueError):
            _row(dt.date(2024, 2, 1), bid=-0.01)

    def test_columns_match_model(self) -> None:
        assert EOD_COLUMNS[0] == "provider"
        assert "bid" in EOD_COLUMNS and "vwap" in EOD_COLUMNS


def test_sessions_between() -> None:
    days = sessions_between(dt.date(2024, 2, 1), dt.date(2024, 2, 9))
    assert days[0] == dt.date(2024, 2, 1) and days[-1] == dt.date(2024, 2, 9)
    assert len(days) == 7  # weekend excluded
    assert sessions_between(dt.date(2024, 2, 9), dt.date(2024, 2, 1)) == []
    # Presidents' Day 2024-02-19 is a holiday
    assert dt.date(2024, 2, 19) not in sessions_between(dt.date(2024, 2, 16), dt.date(2024, 2, 20))


# ---------------------------------------------------------------------------
# store + coverage
# ---------------------------------------------------------------------------


class TestStore:
    def test_write_read_roundtrip(self, tmp_path: Any) -> None:
        s = ParquetHistoryStore(tmp_path)
        d = dt.date(2024, 2, 1)
        rows = [_row(d, 500, bid=1.2, ask=1.3), _row(d, 505)]
        path = s.write_day("fake", "SPY", d, rows)
        assert path == tmp_path / "options_eod/provider=fake/underlying=SPY/2024-02-01.parquet"
        assert not path.with_suffix(".parquet.tmp").exists()
        df = s.read("fake", "SPY")
        assert list(df.columns) == list(EOD_COLUMNS)
        assert len(df) == 2
        assert set(df["right"]) == {"call"}
        assert df["bid"].isna().sum() == 1

    def test_empty_day_and_filters(self, tmp_path: Any) -> None:
        s = ParquetHistoryStore(tmp_path)
        s.write_day("fake", "SPY", dt.date(2024, 2, 2), [])
        s.write_day("fake", "SPY", dt.date(2024, 2, 5), [_row(dt.date(2024, 2, 5))])
        assert s.has("fake", "SPY", dt.date(2024, 2, 2))
        assert s.cached_dates("fake", "SPY") == {dt.date(2024, 2, 2), dt.date(2024, 2, 5)}
        assert len(s.read("fake", "SPY", start=dt.date(2024, 2, 3))) == 1
        assert len(s.read("fake", "SPY", end=dt.date(2024, 2, 2))) == 0
        assert list(s.read("fake", "QQQ").columns) == list(EOD_COLUMNS)
        assert s.cached_dates("fake", "QQQ") == set()

    def test_bad_filename_ignored(self, tmp_path: Any) -> None:
        s = ParquetHistoryStore(tmp_path)
        s.write_day("fake", "SPY", dt.date(2024, 2, 5), [])
        (s.path_for("fake", "SPY", dt.date(2024, 2, 5)).parent / "junk.parquet").write_bytes(b"")
        assert s.cached_dates("fake", "SPY") == {dt.date(2024, 2, 5)}

    @pytest.mark.parametrize(
        "kw",
        [{"date": dt.date(2024, 2, 2)}, {"provider": "other"}, {"underlying": "QQQ"}],
    )
    def test_write_day_rejects_foreign_rows(self, tmp_path: Any, kw: dict[str, Any]) -> None:
        s = ParquetHistoryStore(tmp_path)
        with pytest.raises(ValueError, match="does not belong"):
            s.write_day("fake", "SPY", dt.date(2024, 2, 1), [_row(dt.date(2024, 2, 1), **kw)])

    def test_write_range_groups_and_drops_stray(self, tmp_path: Any) -> None:
        s = ParquetHistoryStore(tmp_path)
        days = [dt.date(2024, 2, 1), dt.date(2024, 2, 2)]
        rows = [_row(days[0]), _row(days[0], 505), _row(dt.date(2024, 2, 5))]
        assert s.write_range("fake", "SPY", days, rows) == 2
        assert s.cached_dates("fake", "SPY") == set(days)
        assert len(s.read("fake", "SPY")) == 2

    def test_coverage(self, tmp_path: Any) -> None:
        s = ParquetHistoryStore(tmp_path)
        s.write_day("fake", "SPY", dt.date(2024, 2, 1), [_row(dt.date(2024, 2, 1), bid=1, ask=2)])
        s.write_day("fake", "SPY", dt.date(2024, 2, 2), [])
        summaries, detail = s.coverage(
            "fake", ["spy", "QQQ"], dt.date(2024, 2, 1), dt.date(2024, 2, 5)
        )
        spy, qqq = summaries
        assert (spy.sessions, spy.ok, spy.empty, spy.missing, spy.rows) == (3, 1, 1, 1, 1)
        assert spy.missing_dates == [dt.date(2024, 2, 5)]
        assert spy.pct_cached == pytest.approx(2 / 3)
        assert qqq.missing == 3 and qqq.pct_cached == 0
        assert len(detail) == 6
        ok = detail[(detail.underlying == "SPY") & (detail.status == "ok")].iloc[0]
        assert (ok.rows, ok.contracts, ok.with_close, ok.with_quote) == (1, 1, 1, 1)
        text = format_coverage(summaries)
        assert "SPY" in text and "66.7%" in text and "0.0%" in text

    def test_coverage_empty_window(self, tmp_path: Any) -> None:
        s = ParquetHistoryStore(tmp_path)
        (summary,), detail = s.coverage("fake", ["SPY"], dt.date(2024, 2, 3), dt.date(2024, 2, 4))
        assert summary.sessions == 0 and summary.pct_cached == 0.0
        assert detail.empty


# ---------------------------------------------------------------------------
# download orchestration
# ---------------------------------------------------------------------------


class FakeProvider:
    name = "fake"

    def __init__(self, fail_on: str | None = None) -> None:
        self.calls: list[tuple[str, dt.date, dt.date, int]] = []
        self.fail_on = fail_on

    def earliest_date(self) -> dt.date:
        return dt.date(2024, 2, 1)

    def fetch_option_eod(
        self, underlying: str, start: dt.date, end: dt.date, *, max_dte: int
    ) -> list[OptionEodRow]:
        self.calls.append((underlying, start, end, max_dte))
        if underlying == self.fail_on:
            raise RuntimeError("boom")
        return [
            _row(d, underlying=underlying, provider=self.name)
            for d in sessions_between(start, end)
            if d != dt.date(2024, 2, 2)
        ]


class TestDownload:
    def test_protocol(self) -> None:
        assert isinstance(FakeProvider(), HistoricalDataProvider)

    def test_missing_ranges(self) -> None:
        s = sessions_between(dt.date(2024, 2, 1), dt.date(2024, 2, 9))
        runs = missing_ranges(s, {dt.date(2024, 2, 6)}, 2)
        assert runs == [
            [dt.date(2024, 2, 1), dt.date(2024, 2, 2)],
            [dt.date(2024, 2, 5)],
            [dt.date(2024, 2, 7), dt.date(2024, 2, 8)],
            [dt.date(2024, 2, 9)],
        ]
        assert missing_ranges(s, set(s), 5) == []
        with pytest.raises(ValueError):
            missing_ranges(s, set(), 0)

    def test_incremental_and_clamped(self, tmp_path: Any) -> None:
        store = ParquetHistoryStore(tmp_path)
        p = FakeProvider()
        res = download(p, store, ["spy"], dt.date(2024, 1, 1), dt.date(2024, 2, 9), max_dte=30)
        assert res[0].underlying == "SPY"
        assert res[0].requested_sessions == 7 and res[0].fetched_sessions == 7
        assert res[0].rows == 6 and res[0].error is None
        assert p.calls[0][1] == dt.date(2024, 2, 1)  # clamped to earliest_date
        assert p.calls[0][3] == 30
        # Second run: nothing to fetch
        p.calls.clear()
        res = download(p, store, ["SPY"], dt.date(2024, 2, 1), dt.date(2024, 2, 9))
        assert p.calls == [] and res[0].skipped_cached == 7 and res[0].fetched_sessions == 0
        # Refresh re-fetches
        res = download(p, store, ["SPY"], dt.date(2024, 2, 1), dt.date(2024, 2, 9), refresh=True)
        assert res[0].fetched_sessions == 7 and res[0].skipped_cached == 0

    def test_error_isolated_per_ticker(self, tmp_path: Any) -> None:
        store = ParquetHistoryStore(tmp_path)
        res = download(
            FakeProvider(fail_on="QQQ"),
            store,
            ["QQQ", "SPY"],
            dt.date(2024, 2, 1),
            dt.date(2024, 2, 2),
        )
        assert res[0].error == "RuntimeError: boom" and res[0].fetched_sessions == 0
        assert res[1].error is None and res[1].fetched_sessions == 2
        assert store.cached_dates("fake", "QQQ") == set()


# ---------------------------------------------------------------------------
# Alpaca provider (fake clients)
# ---------------------------------------------------------------------------


def _contract(sym: str, exp: dt.date, strike: float, typ: str) -> SimpleNamespace:
    return SimpleNamespace(
        symbol=sym, expiration_date=exp, strike_price=strike, type=SimpleNamespace(value=typ)
    )


def _bar(day: dt.date, close: float = 2.0) -> SimpleNamespace:
    ts = dt.datetime.combine(day, dt.time(5, 0), tzinfo=UTC)  # Alpaca daily bars: 05:00Z
    return SimpleNamespace(
        timestamp=ts,
        open=1.0,
        high=2.5,
        low=0.9,
        close=close,
        volume=10.0,
        trade_count=3.0,
        vwap=1.7,
    )


class FakeContracts:
    def __init__(self) -> None:
        self.requests: list[Any] = []

    def get_option_contracts(self, request: Any) -> Any:
        self.requests.append(request)
        status = str(getattr(request.status, "value", request.status))
        if status == "active":
            return SimpleNamespace(option_contracts=None, next_page_token=None)
        if request.page_token is None:
            return SimpleNamespace(
                option_contracts=[
                    _contract("SPY240209C00500000", dt.date(2024, 2, 9), 500, "call"),
                    _contract("1SPY240209C00500000", dt.date(2024, 2, 9), 500, "call"),
                    _contract("SPY1240209C00500000", dt.date(2024, 2, 9), 500, "call"),
                ],
                next_page_token="p2",
            )
        return SimpleNamespace(
            option_contracts=[
                _contract("SPY240405P00480000", dt.date(2024, 4, 5), 480, "put"),
            ],
            next_page_token=None,
        )


class FakeBars:
    def __init__(self) -> None:
        self.requests: list[Any] = []

    def get_option_bars(self, request_params: Any) -> Any:
        self.requests.append(request_params)
        data: dict[str, list[SimpleNamespace]] = {}
        for sym in request_params.symbol_or_symbols:
            if sym.startswith("SPY240209C"):
                data[sym] = [_bar(dt.date(2024, 2, 1)), _bar(dt.date(2024, 2, 5))]
            else:  # 2024-04-05 put: DTE 64 on 02-01 (> max_dte), 59 on 02-06 (ok)
                data[sym] = [_bar(dt.date(2024, 2, 1)), _bar(dt.date(2024, 2, 6))]
        data["UNKNOWN"] = [_bar(dt.date(2024, 2, 1))]
        return SimpleNamespace(data=data)


class TestAlpacaProvider:
    def test_fetch(self) -> None:
        cc, bc = FakeContracts(), FakeBars()
        p = AlpacaHistoryProvider(cc, bc, batch_size=1)
        assert isinstance(p, HistoricalDataProvider)
        assert p.earliest_date() == dt.date(2024, 2, 1)
        rows = p.fetch_option_eod("spy", dt.date(2024, 1, 1), dt.date(2024, 2, 6), max_dte=60)
        got = sorted((r.symbol, r.date) for r in rows)
        assert got == [
            ("SPY240209C00500000", dt.date(2024, 2, 1)),
            ("SPY240209C00500000", dt.date(2024, 2, 5)),
            ("SPY240405P00480000", dt.date(2024, 2, 6)),
        ]
        put = next(r for r in rows if r.right is OptionRight.PUT)
        assert put.strike == 480 and put.provider == "alpaca" and put.underlying == "SPY"
        assert put.bid is None and put.vwap == 1.7
        # both statuses queried, pagination followed, exp window = start..end+max_dte
        assert len(cc.requests) == 3
        assert cc.requests[0].expiration_date_gte == dt.date(2024, 2, 1)
        assert cc.requests[0].expiration_date_lte == dt.date(2024, 4, 6)
        assert len(bc.requests) == 2  # batch_size=1 → one request per symbol
        # alpaca-py normalises to naive UTC: midnight ET == 05:00Z in winter
        assert bc.requests[0].start == dt.datetime(2024, 2, 1, 5, 0)

    def test_before_history_start(self) -> None:
        p = AlpacaHistoryProvider(FakeContracts(), FakeBars())
        assert (
            p.fetch_option_eod("SPY", dt.date(2023, 1, 1), dt.date(2024, 1, 31), max_dte=30) == []
        )

    def test_missing_keys(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ALPACA_API_KEY", raising=False)
        monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)
        with pytest.raises(RuntimeError, match="ALPACA_API_KEY"):
            AlpacaHistoryProvider()


# ---------------------------------------------------------------------------
# ThetaData provider (fake HTTP)
# ---------------------------------------------------------------------------

THETA_CSV = (
    "symbol,expiration,strike,right,created,last_trade,open,high,low,close,volume,count,"
    "bid_size,bid_exchange,bid,bid_condition,ask_size,ask_exchange,ask,ask_condition\n"
    "AAPL,2024-11-15,170.000,CALL,2024-11-04T17:15:00.000,2024-11-04T15:59:00.000,"
    "52.1,53.0,51.0,52.5,12,4,10,1,52.30,0,12,1,52.70,0\n"
    "AAPL,2024-11-15,170.000,PUT,2024-11-04T17:15:00.000,,,,,,0,0,5,1,0.01,0,7,1,0.03,0\n"
    "AAPL,20241115,170.000,X,2024-11-04T17:15:00.000,,,,,,0,0,5,1,0.01,0,7,1,0.03,0\n"
)


class FakeResp:
    def __init__(self, status: int, text: str) -> None:
        self.status_code = status
        self.text = text


class FakeSession:
    def __init__(self, responses: list[FakeResp | Exception]) -> None:
        self.responses = responses
        self.calls: list[dict[str, Any]] = []

    def get(self, url: str, params: dict[str, Any], timeout: float) -> FakeResp:
        self.calls.append({"url": url, **params})
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


class TestThetaData:
    def test_parse_csv(self) -> None:
        rows = parse_eod_csv(THETA_CSV, "aapl")
        assert len(rows) == 2  # bad right skipped
        call, put = rows
        assert call.symbol == "AAPL241115C00170000"
        assert call.date == dt.date(2024, 11, 4)
        assert (call.close, call.volume, call.trade_count) == (52.5, 12, 4)
        assert (call.bid, call.ask, call.bid_size, call.ask_size) == (52.30, 52.70, 10, 12)
        assert put.right is OptionRight.PUT and put.close is None and put.bid == 0.01

    def test_parse_compact_dates(self) -> None:
        text = "symbol,expiration,strike,right,created,close\nSPY,20241115,500,C,20241104,1.0\n"
        (r,) = parse_eod_csv(text, "SPY")
        assert r.expiration == dt.date(2024, 11, 15) and r.date == dt.date(2024, 11, 4)

    def _provider(self, session: FakeSession, **kw: Any) -> ThetaDataEodProvider:
        return ThetaDataEodProvider(
            "http://theta:1/", session, today=lambda: dt.date(2025, 11, 10), **kw
        )

    def test_fetch_chunks_and_clamps(self) -> None:
        sess = FakeSession([FakeResp(200, THETA_CSV), FakeResp(472, "No data"), FakeResp(200, "")])
        sleeps: list[float] = []
        p = self._provider(sess, chunk_days=3, min_interval_s=60, sleep=sleeps.append)
        assert isinstance(p, HistoricalDataProvider)
        assert p.earliest_date() == dt.date(2024, 11, 10)
        # start before free-tier window is clamped; AAPL row on 11-04 is thus outside → filtered
        rows = p.fetch_option_eod("AAPL", dt.date(2024, 11, 1), dt.date(2024, 11, 18), max_dte=45)
        assert rows == []
        assert [c["start_date"] for c in sess.calls] == ["20241110", "20241113", "20241116"]
        assert sess.calls[-1]["end_date"] == "20241118"
        assert sess.calls[0]["url"] == "http://theta:1/v3/option/history/eod"
        assert sess.calls[0]["expiration"] == "*" and sess.calls[0]["max_dte"] == 45
        assert len(sleeps) == 2 and all(0 < s <= 60 for s in sleeps)

    def test_fetch_returns_rows_in_window(self) -> None:
        p = self._provider(FakeSession([FakeResp(200, THETA_CSV)]), lookback_days=10_000)
        rows = p.fetch_option_eod("AAPL", dt.date(2024, 11, 4), dt.date(2024, 11, 4), max_dte=45)
        assert len(rows) == 2 and rows[0].provider == "thetadata"

    def test_http_error(self) -> None:
        p = self._provider(FakeSession([FakeResp(500, "oops")]), lookback_days=10_000)
        with pytest.raises(ThetaTerminalError, match="HTTP 500"):
            p.fetch_option_eod("AAPL", dt.date(2024, 11, 4), dt.date(2024, 11, 4), max_dte=45)

    def test_unreachable(self) -> None:
        p = self._provider(FakeSession([ConnectionError("refused")]), lookback_days=10_000)
        with pytest.raises(ThetaTerminalError, match="unreachable"):
            p.fetch_option_eod("AAPL", dt.date(2024, 11, 4), dt.date(2024, 11, 4), max_dte=45)

    def test_bad_chunk(self) -> None:
        with pytest.raises(ValueError):
            self._provider(FakeSession([]), chunk_days=0)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


class TestCli:
    def test_download_and_coverage(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from arc.cli import main

        fake = FakeProvider()
        fake.name = "alpaca"  # type: ignore[misc]
        monkeypatch.setattr(hcli, "_make_provider", lambda name, args: fake)
        argv = ["history", "download", "--provider", "alpaca", "--tickers", "spy",
                "--start", "2024-02-01", "--end", "2024-02-09",
                "--data-dir", str(tmp_path)]  # fmt: skip
        assert main(argv) == 0
        out = capsys.readouterr().out
        assert "SPY" in out and "100.0%" in out
        csv_path = tmp_path / "coverage" / "alpaca_by_date.csv"
        detail = pd.read_csv(csv_path)
        assert len(detail) == 7 and (detail.status == "empty").sum() == 1

        argv[1] = "coverage"
        assert main(argv) == 0

    def test_download_error_exit_code(self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        from arc.cli import main

        fake = FakeProvider(fail_on="SPY")
        monkeypatch.setattr(hcli, "_make_provider", lambda name, args: fake)
        argv = ["history", "download", "--provider", "thetadata", "--tickers", "SPY",
                "--start", "2024-02-01", "--end", "2024-02-02",
                "--data-dir", str(tmp_path)]  # fmt: skip
        assert main(argv) == 1

    def test_coverage_defaults_to_universe(
        self, tmp_path: Any, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from arc.cli import main

        assert (
            main(["history", "coverage", "--end", "2024-02-09", "--data-dir", str(tmp_path)]) == 0
        )
        out = capsys.readouterr().out
        assert "alpaca" in out and "thetadata" in out and "HD" in out

    def test_make_provider(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import argparse

        args = argparse.Namespace(theta_url="http://x:1", theta_interval=0.5)
        theta = hcli._make_provider("thetadata", args)
        assert isinstance(theta, ThetaDataEodProvider)
        monkeypatch.setenv("ALPACA_API_KEY", "k")
        monkeypatch.setenv("ALPACA_SECRET_KEY", "s")
        assert isinstance(hcli._make_provider("alpaca", args), AlpacaHistoryProvider)

    def test_load_env(self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        env = tmp_path / ".env"
        env.write_text("ALPACA_API_KEY=kk\nALPACA_SECRET_KEY=ss\nOTHER=x\n")
        monkeypatch.setattr(hcli, "_HERMES_ENV", env)
        monkeypatch.delenv("ALPACA_API_KEY", raising=False)
        monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)
        monkeypatch.delenv("OTHER", raising=False)
        hcli._load_alpaca_env()
        import os

        assert os.environ["ALPACA_API_KEY"] == "kk" and "OTHER" not in os.environ
        # no-op when already set / file missing
        monkeypatch.setattr(hcli, "_HERMES_ENV", tmp_path / "nope")
        hcli._load_alpaca_env()
        monkeypatch.delenv("ALPACA_API_KEY")
        hcli._load_alpaca_env()
        assert "ALPACA_API_KEY" not in os.environ
