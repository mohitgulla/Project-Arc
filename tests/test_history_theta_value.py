"""E7.6 (D84): ThetaData Value-tier provider, concurrent resumable download, --plan."""

from __future__ import annotations

import datetime as dt
import json
import threading
import time
from typing import TYPE_CHECKING, Any

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from arc.data.history import cli as hcli
from arc.data.history.base import EOD_COLUMNS, OptionEodRow, OptionRight, occ_symbol
from arc.data.history.download import (
    RunLedger,
    download,
    format_plan,
    format_results,
    plan_download,
    span_runs,
)
from arc.data.history.store import ParquetHistoryStore
from arc.data.history.thetadata import (
    TIER_CONCURRENCY,
    ThetaDataEodProvider,
    ThetaTerminalError,
    ThetaTier,
    clamp_concurrency,
    join_open_interest,
    parse_eod_csv,
    parse_oi_csv,
    tier_earliest_date,
)
from arc.utils.calendar import ET, sessions_between

if TYPE_CHECKING:
    from pathlib import Path

    from arc.data.history.thetadata import RequestLog

TODAY = dt.date(2025, 11, 10)

EOD_HEADER = (
    "symbol,expiration,strike,right,created,last_trade,open,high,low,close,volume,count,"
    "bid_size,bid_exchange,bid,bid_condition,ask_size,ask_exchange,ask,ask_condition\n"
)
OI_HEADER = "symbol,expiration,strike,right,timestamp,open_interest\n"


def eod_line(day: dt.date, strike: float = 100.0, right: str = "CALL", sym: str = "SPY") -> str:
    return (
        f"{sym},2020-02-21,{strike:.3f},{right},{day.isoformat()}T17:15:00.000,"
        f"{day.isoformat()}T15:59:30.000,1,1.2,0.9,1.1,10,3,5,1,1.05,0,6,1,1.15,0\n"
    )


def oi_line(day: dt.date, oi: int, strike: float = 100.0, right: str = "CALL") -> str:
    return f"SPY,2020-02-21,{strike:.3f},{right},{day.isoformat()}T06:30:00.000,{oi}\n"


class Resp:
    def __init__(self, status: int, text: str = "") -> None:
        self.status_code = status
        self.text = text


class RoutedSession:
    """Fake Terminal: answers each request from the date window, per endpoint.

    ``script`` (optional) is a list of statuses/exceptions consumed before the
    normal answer, to inject 429s / timeouts.  Counts in-flight calls.
    """

    def __init__(
        self,
        *,
        script: list[int | Exception] | None = None,
        rows_per_day: int = 1,
        delay_s: float = 0.0,
    ) -> None:
        self.script = list(script or [])
        self.rows_per_day = rows_per_day
        self.delay_s = delay_s
        self.calls: list[dict[str, Any]] = []
        self.lock = threading.Lock()
        self.in_flight = 0
        self.max_in_flight = 0

    def get(self, url: str, params: dict[str, Any], timeout: float) -> Resp:
        with self.lock:
            self.calls.append({"url": url, **params})
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
            step = self.script.pop(0) if self.script else None
        try:
            if self.delay_s:
                time.sleep(self.delay_s)
            if isinstance(step, Exception):
                raise step
            if isinstance(step, int):
                return Resp(step, "err")
            lo = dt.datetime.strptime(params["start_date"], "%Y%m%d").date()
            hi = dt.datetime.strptime(params["end_date"], "%Y%m%d").date()
            days = sessions_between(lo, hi)
            sym = params["symbol"]
            if url.endswith("/open_interest"):
                body = "".join(
                    oi_line(d, 1000 + i, strike=100 + i)
                    for d in days
                    for i in range(self.rows_per_day)
                )
                return Resp(200, OI_HEADER + body) if body else Resp(472, "No data")
            body = "".join(
                eod_line(d, strike=100 + i, sym=sym) for d in days for i in range(self.rows_per_day)
            )
            return Resp(200, EOD_HEADER + body) if body else Resp(472, "No data")
        finally:
            with self.lock:
                self.in_flight -= 1


def provider(session: Any, **kw: Any) -> ThetaDataEodProvider:
    kw.setdefault("sleep", lambda _s: None)
    kw.setdefault("today", lambda: TODAY)
    return ThetaDataEodProvider("http://theta:1", session, **kw)


# ---------------------------------------------------------------------------
# tiers
# ---------------------------------------------------------------------------


class TestTiers:
    def test_free_default_is_byte_identical(self) -> None:
        # Pre-E7.6 behaviour: today - 365 days (2025-11-10 -> 2024-11-10).
        p = provider(RoutedSession())
        assert p.tier is ThetaTier.FREE and p.with_oi is False
        assert p.earliest_date() == TODAY - dt.timedelta(days=365)
        assert provider(RoutedSession(), lookback_days=10_000).earliest_date() == dt.date(
            2023, 6, 1
        )  # free floor from the subscription docs

    @pytest.mark.parametrize(
        ("tier", "first"),
        [
            ("value", dt.date(2020, 1, 1)),
            ("standard", dt.date(2016, 1, 1)),
            ("pro", dt.date(2012, 6, 1)),
        ],
    )
    def test_paid_first_dates(self, tier: str, first: dt.date) -> None:
        p = provider(RoutedSession(), tier=tier)
        assert p.earliest_date() == first
        assert p.with_oi is True  # default on for tier >= value
        assert tier_earliest_date(ThetaTier(tier), TODAY) == first

    def test_concurrency_caps(self) -> None:
        assert TIER_CONCURRENCY == {
            ThetaTier.FREE: 1,
            ThetaTier.VALUE: 2,
            ThetaTier.STANDARD: 4,
            ThetaTier.PRO: 8,
        }
        assert clamp_concurrency(ThetaTier.VALUE, None) == 2
        assert clamp_concurrency(ThetaTier.VALUE, 5) == 2
        assert clamp_concurrency(ThetaTier.VALUE, 0) == 1
        assert clamp_concurrency(ThetaTier.FREE, None) == 1

    def test_oi_needs_value(self) -> None:
        with pytest.raises(ValueError, match="Value"):
            provider(RoutedSession(), tier="free", with_oi=True)
        p = provider(RoutedSession(), tier="free")
        with pytest.raises(ThetaTerminalError, match="Value"):
            p.fetch_open_interest("SPY", TODAY, TODAY, max_dte=60)


# ---------------------------------------------------------------------------
# parsing + OI join
# ---------------------------------------------------------------------------


class TestParseAndJoin:
    def test_eod_extra_fields(self) -> None:
        d = dt.date(2020, 1, 2)
        (r,) = parse_eod_csv(EOD_HEADER + eod_line(d), "SPY")
        assert r.last_trade == dt.datetime(2020, 1, 2, 15, 59, 30, tzinfo=ET)
        assert r.created == dt.datetime(2020, 1, 2, 17, 15, tzinfo=ET)
        assert r.open_interest is None

    def test_blank_last_trade(self) -> None:
        text = EOD_HEADER + eod_line(dt.date(2020, 1, 2)).replace("T15:59:30.000", "", 1)
        text = text.replace(",2020-01-02,1,", ",,1,")
        (r,) = parse_eod_csv(text, "SPY")
        assert r.last_trade is None

    def test_aware_timestamps_converted_to_et(self) -> None:
        r = OptionEodRow(
            provider="x",
            underlying="SPY",
            date=dt.date(2020, 1, 2),
            symbol="SPY200221C00100000",
            expiration=dt.date(2020, 2, 21),
            strike=100,
            right=OptionRight.CALL,
            created=dt.datetime(2020, 1, 2, 22, 15, tzinfo=dt.UTC),
        )
        assert r.created == dt.datetime(2020, 1, 2, 17, 15, tzinfo=ET)

    def test_oi_join(self) -> None:
        d1, d2 = dt.date(2020, 1, 2), dt.date(2020, 1, 3)
        rows = parse_eod_csv(
            EOD_HEADER + eod_line(d1) + eod_line(d1, 101, "PUT") + eod_line(d2), "SPY"
        )
        oi = parse_oi_csv(
            OI_HEADER
            + oi_line(d1, 1500)
            + oi_line(d2, 1600)
            + oi_line(d1, 7, strike=999)  # contract not in the EOD report
            + "SPY,2020-02-21,100.000,X,2020-01-02T06:30:00.000,5\n"  # bad right: skipped
            + "SPY,2020-02-21,100.000,CALL,2020-01-02T06:30:00.000,\n",  # blank OI: skipped
            "SPY",
        )
        sym = occ_symbol("SPY", dt.date(2020, 2, 21), OptionRight.CALL, 100)
        assert oi[(d1, sym)] == 1500 and len(oi) == 3
        joined = join_open_interest(rows, oi)
        got = {(r.date, r.symbol): r.open_interest for r in joined}
        assert got[(d1, sym)] == 1500
        assert got[(d2, sym)] == 1600
        assert got[(d1, occ_symbol("SPY", dt.date(2020, 2, 21), OptionRight.PUT, 101))] is None

    def test_fetch_with_oi_joins_and_calls_both_endpoints(self) -> None:
        sess = RoutedSession(rows_per_day=2)
        p = provider(sess, tier="value")
        rows = p.fetch_option_eod("SPY", dt.date(2020, 1, 2), dt.date(2020, 1, 3), max_dte=60)
        assert len(rows) == 4 and all(r.open_interest is not None for r in rows)
        assert [c["url"].rsplit("/", 1)[-1] for c in sess.calls] == ["eod", "open_interest"]
        assert sess.calls[1]["expiration"] == "*" and sess.calls[1]["max_dte"] == 60


# ---------------------------------------------------------------------------
# retry / backoff / adaptive chunk
# ---------------------------------------------------------------------------


class TestRetryAndChunk:
    def test_429_then_200(self) -> None:
        sleeps: list[float] = []
        reqs: list[RequestLog] = []
        sess = RoutedSession(script=[429, 503])
        p = provider(
            sess, tier="value", with_oi=False, sleep=sleeps.append, on_request=reqs.append,
            backoff_base_s=0.5,
        )  # fmt: skip
        rows = p.fetch_option_eod("SPY", dt.date(2020, 1, 2), dt.date(2020, 1, 2), max_dte=60)
        assert len(rows) == 1
        assert len(sess.calls) == 3
        assert sleeps == [0.5, 1.0]  # exponential backoff
        assert reqs[-1].status == 200 and reqs[-1].retries == 2

    def test_retries_exhausted(self) -> None:
        p = provider(RoutedSession(script=[429] * 3), tier="value", with_oi=False, max_retries=2)
        with pytest.raises(ThetaTerminalError, match="HTTP 429"):
            p.fetch_option_eod("SPY", dt.date(2020, 1, 2), dt.date(2020, 1, 2), max_dte=60)

    def test_connection_error_retried(self) -> None:
        sess = RoutedSession(script=[ConnectionError("reset")])
        p = provider(sess, tier="value", with_oi=False)
        assert p.fetch_option_eod("SPY", dt.date(2020, 1, 2), dt.date(2020, 1, 2), max_dte=60)

    def test_472_is_empty_not_error(self) -> None:
        p = provider(RoutedSession(script=[472]), tier="value", with_oi=False)
        assert p.fetch_option_eod("SPY", dt.date(2020, 1, 2), dt.date(2020, 1, 2), max_dte=60) == []

    def test_timeout_halves_chunk(self) -> None:
        reqs: list[RequestLog] = []
        sess = RoutedSession(script=[TimeoutError("read timed out")])
        p = provider(sess, tier="value", with_oi=False, chunk_days=8, on_request=reqs.append,
                     grow_below_rows_per_day=0)  # fmt: skip
        rows = p.fetch_option_eod("SPY", dt.date(2020, 1, 6), dt.date(2020, 1, 13), max_dte=60)
        assert len(rows) == 6  # 6 sessions, 1 row each
        assert reqs[0].error and reqs[0].chunk_days == 8
        assert [r.chunk_days for r in reqs[1:]] == [4, 4]
        assert p.chunk_for("SPY") == 4
        assert [(c["start_date"], c["end_date"]) for c in sess.calls[1:]] == [
            ("20200106", "20200109"),
            ("20200110", "20200113"),
        ]

    def test_timeout_at_one_day_backs_off_then_raises(self) -> None:
        sess = RoutedSession(script=[TimeoutError("t")] * 3)
        sleeps: list[float] = []
        p = provider(sess, tier="value", with_oi=False, chunk_days=1, max_retries=1,
                     sleep=sleeps.append)  # fmt: skip
        with pytest.raises(ThetaTerminalError, match="timeout"):
            p.fetch_option_eod("SPY", dt.date(2020, 1, 2), dt.date(2020, 1, 2), max_dte=60)
        assert len(sleeps) == 1

    def test_large_request_and_size_threshold_halve(self) -> None:
        reqs: list[RequestLog] = []
        sess = RoutedSession(script=[570], rows_per_day=3)
        p = provider(sess, tier="value", with_oi=False, chunk_days=4, max_response_bytes=500,
                     grow_below_rows_per_day=0, on_request=reqs.append)  # fmt: skip
        p.fetch_option_eod("SPY", dt.date(2020, 1, 6), dt.date(2020, 1, 17), max_dte=60)
        # 570 halves 4 -> 2; every 2-day body (~6 rows) exceeds 500 bytes -> 1.
        assert [r.chunk_days for r in reqs[:3]] == [4, 2, 1]
        assert reqs[0].status == 570
        assert p.chunk_for("SPY") == 1

    def test_large_request_at_one_day_raises(self) -> None:
        p = provider(RoutedSession(script=[570]), tier="value", with_oi=False, chunk_days=1)
        with pytest.raises(ThetaTerminalError, match="570"):
            p.fetch_option_eod("SPY", dt.date(2020, 1, 2), dt.date(2020, 1, 2), max_dte=60)

    def test_small_chain_grows_to_cap(self) -> None:
        reqs: list[RequestLog] = []
        p = provider(RoutedSession(), tier="value", with_oi=False, on_request=reqs.append)
        p.fetch_option_eod("SPY", dt.date(2020, 1, 1), dt.date(2020, 4, 30), max_dte=60)
        assert [r.chunk_days for r in reqs[:4]] == [7, 14, 28, 28]
        assert p.chunk_for("spy") == 28

    def test_min_interval_still_spaces_calls(self) -> None:
        sleeps: list[float] = []
        p = provider(RoutedSession(), tier="value", with_oi=False, chunk_days=1,
                     max_chunk_days=1, min_interval_s=30, sleep=sleeps.append)  # fmt: skip
        p.fetch_option_eod("SPY", dt.date(2020, 1, 2), dt.date(2020, 1, 3), max_dte=60)
        assert len(sleeps) == 1 and 0 < sleeps[0] <= 30


# ---------------------------------------------------------------------------
# store: schema evolution + OI completeness
# ---------------------------------------------------------------------------


def _old_alpaca_file(store: ParquetHistoryStore, day: dt.date) -> None:
    """Write a pre-E7.6 file (no last_trade/created/open_interest columns)."""
    from arc.data.history.store import SCHEMA

    old = pa.schema([f for f in SCHEMA if f.name not in ("last_trade", "created", "open_interest")])
    rec = {
        "provider": "alpaca", "underlying": "SPY", "date": day, "symbol": "SPY200221C00100000",
        "expiration": dt.date(2020, 2, 21), "strike": 100.0, "right": "call", "close": 1.0,
        "vwap": 1.0,
    }  # fmt: skip
    path = store.path_for("alpaca", "SPY", day)
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist([rec], schema=old), path)


class TestStoreSchema:
    def test_old_and_new_schema_read_into_one_frame(self, tmp_path: Path) -> None:
        store = ParquetHistoryStore(tmp_path)
        d = dt.date(2024, 2, 1)
        _old_alpaca_file(store, d)
        old = store.read("alpaca", "SPY")
        assert list(old.columns) == list(EOD_COLUMNS)
        assert old["open_interest"].isna().all() and old["last_trade"].isna().all()
        (new_row,) = parse_eod_csv(EOD_HEADER + eod_line(d), "SPY")
        new_row = new_row.model_copy(update={"open_interest": 42.0})
        store.write_day("thetadata", "SPY", d, [new_row], with_oi=True)
        new = store.read("thetadata", "SPY")
        both = pd.concat([old, new], ignore_index=True)
        assert list(both.columns) == list(EOD_COLUMNS) and len(both) == 2
        assert both["open_interest"].tolist()[1] == 42.0 and pd.isna(both["open_interest"][0])
        assert both["vwap"].notna().tolist() == [True, False]
        assert str(new["last_trade"].dt.tz) == "America/New_York"
        assert new["last_trade"][0].hour == 15
        # coverage still works on old files
        (cov,), _ = store.coverage("alpaca", ["SPY"], d, d)
        assert cov.ok == 1

    def test_oi_marker_and_fill(self, tmp_path: Path) -> None:
        store = ParquetHistoryStore(tmp_path)
        d = dt.date(2020, 1, 2)
        rows = parse_eod_csv(EOD_HEADER + eod_line(d) + eod_line(d, 101), "SPY")
        store.write_day("thetadata", "SPY", d, rows)
        assert not store.has_open_interest("thetadata", "SPY", d)
        assert store.oi_dates("thetadata", "SPY") == set()
        sym = rows[0].symbol
        assert store.add_open_interest("thetadata", "SPY", d, {(d, sym): 9.0}) == 1
        assert store.has_open_interest("thetadata", "SPY", d)
        df = store.read("thetadata", "SPY")
        assert df["open_interest"].tolist()[0] == 9.0 and pd.isna(df["open_interest"][1])
        assert not store.has_open_interest("thetadata", "SPY", dt.date(2020, 1, 3))
        assert store.providers() == ["thetadata"]
        assert ParquetHistoryStore(tmp_path / "none").providers() == []


# ---------------------------------------------------------------------------
# download: resume, concurrency, ledger
# ---------------------------------------------------------------------------


class TestDownloadValue:
    START, END = dt.date(2020, 1, 2), dt.date(2020, 1, 31)

    def test_resume_skips_cached_sessions(self, tmp_path: Path) -> None:
        store = ParquetHistoryStore(tmp_path)
        sess = RoutedSession()
        p = provider(sess, tier="value")
        (r,) = download(p, store, ["spy"], self.START, self.END, need_oi=True)
        n = len(sessions_between(self.START, self.END))
        assert r.fetched_sessions == n and r.error is None
        assert store.oi_dates("thetadata", "SPY") == set(sessions_between(self.START, self.END))
        sess.calls.clear()
        (r,) = download(p, store, ["SPY"], self.START, self.END, need_oi=True)
        assert sess.calls == [] and r.skipped_cached == n and r.fetched_sessions == 0

    def test_resume_after_failure_fetches_only_gap(self, tmp_path: Path) -> None:
        store = ParquetHistoryStore(tmp_path)
        bad = RoutedSession()
        p = provider(bad, tier="value", with_oi=False, max_retries=0, chunk_days=7,
                     max_chunk_days=7)  # fmt: skip
        # Fail the 3rd request (third week).
        orig_get = bad.get
        count = {"n": 0}

        def flaky(url: str, params: dict[str, Any], timeout: float) -> Resp:
            count["n"] += 1
            if count["n"] == 3:
                return Resp(500, "boom")
            return orig_get(url, params, timeout)

        bad.get = flaky  # type: ignore[method-assign]
        (r,) = download(p, store, ["SPY"], self.START, self.END)
        assert r.error and "HTTP 500" in r.error
        assert len(r.failed_ranges) == 1 and r.failed_ranges[0][1] == self.END
        cached = store.cached_dates("thetadata", "SPY")
        assert cached and max(cached) < r.failed_ranges[0][0]
        out = format_results([r])
        assert "FAILED" in out and "re-run the same command" in out
        # Re-run: only the gap is requested.
        bad.get = orig_get  # type: ignore[method-assign]
        bad.calls.clear()
        (r2,) = download(p, store, ["SPY"], self.START, self.END)
        assert r2.error is None and r2.skipped_cached == len(cached)
        first = dt.datetime.strptime(bad.calls[0]["start_date"], "%Y%m%d").date()
        assert first == r.failed_ranges[0][0]
        assert store.cached_dates("thetadata", "SPY") == set(sessions_between(self.START, self.END))

    def test_resume_fills_missing_oi_only(self, tmp_path: Path) -> None:
        store = ParquetHistoryStore(tmp_path)
        sess = RoutedSession()
        download(provider(sess, tier="value", with_oi=False), store, ["SPY"], self.START,
                 self.END)  # fmt: skip
        assert store.oi_dates("thetadata", "SPY") == set()
        sess.calls.clear()
        p = provider(sess, tier="value")
        (r,) = download(p, store, ["SPY"], self.START, self.END, need_oi=True)
        n = len(sessions_between(self.START, self.END))
        assert r.fetched_sessions == 0 and r.oi_filled_sessions == n
        assert {c["url"].rsplit("/", 1)[-1] for c in sess.calls} == {"open_interest"}
        assert store.oi_dates("thetadata", "SPY") == set(sessions_between(self.START, self.END))
        assert store.read("thetadata", "SPY")["open_interest"].notna().all()

    def test_oi_fill_failure_recorded(self, tmp_path: Path) -> None:
        store = ParquetHistoryStore(tmp_path)
        download(provider(RoutedSession(), tier="value", with_oi=False), store, ["SPY"],
                 self.START, self.START)  # fmt: skip
        p = provider(RoutedSession(script=[500]), tier="value", max_retries=0)
        (r,) = download(p, store, ["SPY"], self.START, self.START, need_oi=True)
        assert r.error and r.oi_filled_sessions == 0 and r.failed_ranges

    def test_concurrency_never_exceeds_cap(self, tmp_path: Path) -> None:
        store = ParquetHistoryStore(tmp_path)
        sess = RoutedSession(delay_s=0.01)
        p = provider(sess, tier="value")
        tickers = ["SPY", "QQQ", "IWM", "AAPL", "MSFT", "NVDA"]
        res = download(p, store, tickers, self.START, self.END, need_oi=True,
                       concurrency=clamp_concurrency(ThetaTier.VALUE, 8))  # fmt: skip
        assert [r.underlying for r in res] == tickers  # input (priority) order kept
        assert all(r.error is None for r in res)
        assert sess.max_in_flight == 2
        for t in tickers:
            assert len(store.cached_dates("thetadata", t)) == len(
                sessions_between(self.START, self.END)
            )

    def test_concurrency_validation_and_empty_window(self, tmp_path: Path) -> None:
        store = ParquetHistoryStore(tmp_path)
        p = provider(RoutedSession(), tier="value")
        with pytest.raises(ValueError, match="concurrency"):
            download(p, store, ["SPY"], self.START, self.END, concurrency=0)
        (r,) = download(p, store, ["SPY"], dt.date(2020, 1, 4), dt.date(2020, 1, 5))
        assert r.requested_sessions == 0 and r.fetched_sessions == 0

    def test_ledger_and_progress(self, tmp_path: Path) -> None:
        store = ParquetHistoryStore(tmp_path)
        lines: list[str] = []
        clock = iter(float(i) for i in range(1000))
        ledger = RunLedger(tmp_path / "history_runs" / "r1.jsonl", out=lines.append,
                           progress_every=2, total_requests=10,
                           clock=lambda: next(clock))  # fmt: skip
        p = provider(RoutedSession(), tier="value", on_request=ledger.request, chunk_days=7,
                     max_chunk_days=7)  # fmt: skip
        download(p, store, ["SPY"], self.START, self.END, need_oi=True, ledger=ledger)
        recs = [json.loads(x) for x in ledger.path.read_text().splitlines()]  # type: ignore[union-attr]
        reqs = [r for r in recs if r["event"] == "request"]
        assert len(reqs) == ledger.requests == 10  # 5 weeks x (eod + oi)
        assert {"ticker", "start", "end", "rows", "bytes", "seconds", "status", "retries",
                "chunk_days"} <= reqs[0].keys()  # fmt: skip
        assert {r["kind"] for r in reqs} == {"eod", "oi"}
        assert any(r["event"] == "ticker_done" for r in recs)
        assert len(lines) == 5
        assert lines[-1].startswith("[history] requests 10/10")
        assert "GB" in lines[-1] and "req/s" in lines[-1] and "ETA 0m00s" in lines[-1]

    def test_span_runs_rereads_span(self) -> None:
        s = sessions_between(dt.date(2020, 1, 2), dt.date(2020, 1, 17))
        spans = iter([7, 1, 28])
        runs = list(span_runs(s, {dt.date(2020, 1, 8)}, lambda: next(spans)))
        assert runs[0] == [dt.date(2020, 1, 2), dt.date(2020, 1, 3), dt.date(2020, 1, 6),
                           dt.date(2020, 1, 7)]  # fmt: skip
        assert runs[1] == [dt.date(2020, 1, 9)]
        assert runs[2][0] == dt.date(2020, 1, 10) and runs[2][-1] == dt.date(2020, 1, 17)


# ---------------------------------------------------------------------------
# --plan
# ---------------------------------------------------------------------------


class TestPlan:
    def test_plan_numbers_on_fixture(self, tmp_path: Path) -> None:
        store = ParquetHistoryStore(tmp_path)
        # Fixture: QQQ has 2 cached sessions of 4 and 6 rows (no OI) -> 5 rows/day.
        for d, n in ((dt.date(2020, 1, 2), 4), (dt.date(2020, 1, 3), 6)):
            rows = parse_eod_csv(
                EOD_HEADER + "".join(eod_line(d, 100 + i, sym="QQQ") for i in range(n)), "QQQ"
            )
            store.write_day("thetadata", "QQQ", d, rows)
        # IWM only has an Alpaca cache: 3 rows on one day.
        _old_alpaca_file(store, dt.date(2024, 2, 1))
        for p in (tmp_path / "options_eod/provider=alpaca/underlying=SPY").glob("*"):
            dest = tmp_path / "options_eod/provider=alpaca/underlying=IWM" / p.name
            dest.parent.mkdir(parents=True, exist_ok=True)
            p.rename(dest)
        plan = plan_download(
            store, "thetadata", ["spy", "QQQ", "IWM"], dt.date(2020, 1, 2), dt.date(2020, 1, 10),
            tier="value", with_oi=True, concurrency=2, default_rows_per_day=3000,
            seconds_per_request=4.0,
        )  # fmt: skip
        spy, qqq, iwm = plan.tickers
        assert plan.sessions == 7
        # SPY: no cache -> default 3000/day, chunk 7: runs {1/2..1/8} + {1/9,1/10} = 2 eod + 2 oi
        assert (spy.to_fetch, spy.rows_per_day_source, spy.chunk_days) == (7, "default", 7)
        assert spy.requests == 4 and spy.est_rows == 21_000
        assert spy.est_disk_bytes == 21_000 * 45
        assert spy.est_wire_bytes == 21_000 * (200 + 70)
        # QQQ: 5 rows/day cached -> small chain -> 28-day chunk; 5 sessions to fetch
        # (1/6..1/10) = 1 eod + 1 oi, plus 2 cached sessions without OI = 1 OI-only request.
        assert (qqq.to_fetch, qqq.oi_only, qqq.rows_per_day) == (5, 2, 5.0)
        assert (qqq.rows_per_day_source, qqq.chunk_days, qqq.requests) == ("cached", 28, 3)
        assert qqq.est_rows == 25
        # IWM: rows/day from the alpaca cache (1 row/day)
        assert (iwm.rows_per_day_source, iwm.rows_per_day, iwm.requests) == ("alpaca", 1.0, 2)
        assert plan.requests == 9
        assert plan.eta_seconds == pytest.approx(9 * 4.0 / 2)
        text = format_plan(plan)
        assert "total: 3 tickers · 9 requests" in text and "ETA 0m18s" in text

    def test_plan_without_oi_and_refresh(self, tmp_path: Path) -> None:
        store = ParquetHistoryStore(tmp_path)
        store.write_day("thetadata", "SPY", dt.date(2020, 1, 2), [])
        plan = plan_download(
            store, "thetadata", ["SPY"], dt.date(2020, 1, 2), dt.date(2020, 1, 3),
            tier="free", with_oi=False, concurrency=1, refresh=True,
        )  # fmt: skip
        (t,) = plan.tickers
        assert t.to_fetch == 2 and t.oi_only == 0 and t.requests == 1
        assert t.rows_per_day_source == "default"  # zero-row files don't count


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _argv(tmp_path: Path, *extra: str) -> list[str]:
    return ["history", "download", "--provider", "thetadata", "--data-dir", str(tmp_path),
            *extra]  # fmt: skip


class TestCliValue:
    def test_plan_cli_no_network(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from arc.cli import main

        def boom(*_a: Any, **_k: Any) -> Any:
            raise AssertionError("--plan must not build a provider")

        monkeypatch.setattr(hcli, "_make_provider", boom)
        argv = _argv(tmp_path, "--theta-tier", "value", "--tickers", "SPY",
                     "--start", "2020-01-02", "--end", "2020-01-10", "--plan")  # fmt: skip
        assert main(argv) == 0
        out = capsys.readouterr().out
        assert "tier=value" in out and "oi=on" in out and "concurrency=2" in out
        assert "total: 1 tickers · 4 requests · 14,000 rows" in out
        assert not (tmp_path / "coverage").exists()

    def test_plan_clamps_to_tier_start(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from arc.cli import main

        argv = _argv(tmp_path, "--theta-tier", "value", "--tickers", "SPY",
                     "--start", "2019-06-01", "--end", "2020-01-03", "--plan",
                     "--concurrency", "9")  # fmt: skip
        assert main(argv) == 0
        out = capsys.readouterr().out
        assert "2020-01-01 → 2020-01-03 (2 sessions)" in out
        assert "concurrency 9 capped to 2" in out

    def test_tickers_file_order_and_max(self, tmp_path: Path) -> None:
        import argparse

        txt = tmp_path / "u.txt"
        txt.write_text("# priority list\nqqq\nSPY\n\nqqq\nIWM  # dup and comment\nAAPL\n")
        args = argparse.Namespace(tickers="TSLA,SPY", tickers_file=txt, max_tickers=4)
        assert hcli.resolve_tickers(args) == ["TSLA", "SPY", "QQQ", "IWM"]
        args.max_tickers = None
        args.tickers = None
        assert hcli.resolve_tickers(args) == ["QQQ", "SPY", "IWM", "AAPL"]
        csv = tmp_path / "u.csv"
        pd.DataFrame({"rank": [1, 2, 3], "Ticker": ["msft", "nvda", None]}).to_csv(csv)
        assert hcli.read_tickers_file(csv) == ["MSFT", "NVDA"]
        pq_path = tmp_path / "u.parquet"
        pd.DataFrame({"symbol": ["XLE", "XLF"]}).to_parquet(pq_path)
        assert hcli.read_tickers_file(pq_path) == ["XLE", "XLF"]
        bad = tmp_path / "bad.csv"
        pd.DataFrame({"name": ["x"]}).to_csv(bad)
        with pytest.raises(ValueError, match="no ticker column"):
            hcli.read_tickers_file(bad)

    def test_download_cli_writes_ledger_and_table(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from arc.cli import main

        sess = RoutedSession()
        real = hcli._make_provider

        def make(name: str, args: Any, on_request: Any = None) -> Any:
            p = real(name, args, on_request=on_request)
            p._session = sess  # type: ignore[attr-defined]
            p._sleep = lambda _s: None  # type: ignore[attr-defined]
            return p

        monkeypatch.setattr(hcli, "_make_provider", make)
        tf = tmp_path / "list.txt"
        tf.write_text("SPY\nQQQ\nIWM\n")
        argv = _argv(tmp_path, "--theta-tier", "value", "--tickers-file", str(tf),
                     "--max-tickers", "2", "--start", "2020-01-02", "--end", "2020-01-10",
                     "--progress-every", "1")  # fmt: skip
        assert main(argv) == 0
        out = capsys.readouterr().out
        assert "[history] requests 1/8" in out
        assert "SPY" in out and "QQQ" in out and "IWM" not in out.split("ledger:")[0]
        (ledger,) = (tmp_path / "history_runs").glob("*.jsonl")
        recs = [json.loads(x) for x in ledger.read_text().splitlines()]
        assert recs[0]["event"] == "run_start" and recs[0]["concurrency"] == 2
        assert recs[0]["with_oi"] is True and recs[0]["planned_requests"] == 8
        assert sum(r["event"] == "request" for r in recs) == 8
        assert store_complete(tmp_path, "SPY") and store_complete(tmp_path, "QQQ")

    def test_make_provider_tier_flags(self) -> None:
        import argparse

        args = argparse.Namespace(theta_url="http://x:1", theta_interval=0.0,
                                  theta_tier="value", with_oi=None,
                                  theta_chunk_days=5)  # fmt: skip
        p = hcli._make_provider("thetadata", args)
        assert isinstance(p, ThetaDataEodProvider)
        assert p.tier is ThetaTier.VALUE and p.with_oi and p.chunk_days == 5
        args.with_oi = False
        assert hcli._make_provider("thetadata", args).with_oi is False  # type: ignore[attr-defined]


def store_complete(root: Path, ticker: str) -> bool:
    store = ParquetHistoryStore(root)
    want = set(sessions_between(dt.date(2020, 1, 2), dt.date(2020, 1, 10)))
    return store.oi_dates("thetadata", ticker) == want
