"""E4.12 (D55): iv_daily store, forward iv.record, backfill, Option Strategist, validate."""

from __future__ import annotations

import datetime as dt
import json
import math
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from arc.data.base import HistoryBar, OptionContract, UnderlyingQuote
from arc.features.vol import atm_iv_from_chain, atm_term_points, constant_maturity_iv
from arc.iv import backfill as bf
from arc.iv import optionstrategist as osf
from arc.iv.record import CrossCheck, crosscheck_names, iv30_from_chain, parse_cboe_iv30, record_day
from arc.iv.store import (
    BACKFILL,
    FORWARD,
    OPTIONSTRATEGIST,
    IvRow,
    IvStore,
    import_csv_dir,
    safe_store,
)
from arc.iv.validate import format_validation, hv20, validate
from arc.pricing.bs import BSMInputs, OptionKind, price
from arc.store.db import connect
from arc.store.migrate import migrate
from arc.utils.calendar import ET

DAY = dt.date(2026, 10, 5)
NOW = dt.datetime(2026, 10, 5, 15, 50, tzinfo=ET)


@pytest.fixture
def conn() -> Any:
    c = connect(":memory:")
    migrate(c)
    return c


# ---------------------------------------------------------------------------
# store
# ---------------------------------------------------------------------------


def test_store_upsert_series_preference_and_skips(conn: Any) -> None:
    s = IvStore(conn)
    d1, d2, d3 = dt.date(2026, 9, 30), dt.date(2026, 10, 1), dt.date(2026, 10, 2)
    s.skip("spy", d2, BACKFILL, "no traded call", now=NOW)
    assert s.days("SPY", BACKFILL) == set()
    assert s.days("SPY", BACKFILL, include_skips=True) == {d2}
    s.upsert(
        [
            IvRow("spy", d1, 0.15, "bars_bs_cm30", BACKFILL),
            IvRow("SPY", d2, 0.16, "bars_bs_cm30", BACKFILL),  # clears the skip
            IvRow("SPY", d2, 0.14, "chain_cm30", FORWARD),  # forward wins for d2
            IvRow("SPY", d3, 0.30, "os_cur_iv", OPTIONSTRATEGIST, ext_percentile=0.18),
        ],
        now=NOW,
    )
    assert conn.execute("SELECT COUNT(*) FROM iv_skips").fetchone()[0] == 0
    assert s.series("SPY") == {d1: 0.15, d2: 0.14}  # OS never mixed in
    assert s.series("SPY", until=d1) == {d1: 0.15}
    # A re-run of the same day upserts (one row per key).
    s.upsert([IvRow("SPY", d2, 0.145, "chain_cm30", FORWARD, detail={"x": 1})], now=NOW)
    rows = s.rows("SPY", [FORWARD])
    assert [(r.day, r.iv30, r.detail) for r in rows] == [(d2, 0.145, {"x": 1})]
    ext = s.latest_external("SPY", until=d3)
    assert ext is not None and ext.ext_percentile == 0.18
    assert s.latest_external("SPY", until=d2) is None
    assert [r.ticker for r in s.rows_on(d3, OPTIONSTRATEGIST)] == ["SPY"]
    assert s.counts() == {BACKFILL: 2, FORWARD: 1, OPTIONSTRATEGIST: 1}
    assert s.rows("SPY", [BACKFILL], since=d2)[0].day == d2
    with pytest.raises(ValueError, match="positive"):
        s.upsert([IvRow("SPY", d1, 0.0, "x", FORWARD)], now=NOW)


def test_safe_store_without_table() -> None:
    import sqlite3

    assert safe_store(None) is None
    assert safe_store(sqlite3.connect(":memory:")) is None


def test_import_csv_dir(conn: Any, tmp_path: Any) -> None:
    assert import_csv_dir(conn, tmp_path / "missing", now=NOW) == {}
    (tmp_path / "SPY.csv").write_text("date,atm_iv\n2026-09-24,0.14\n2026-09-25,0.15\n")
    assert import_csv_dir(conn, tmp_path, now=NOW) == {"SPY": 2}
    assert import_csv_dir(conn, tmp_path, now=NOW) == {"SPY": 0}  # idempotent
    assert IvStore(conn).series("SPY") == {dt.date(2026, 9, 24): 0.14, dt.date(2026, 9, 25): 0.15}


# ---------------------------------------------------------------------------
# constant maturity (shared by forward + backfill)
# ---------------------------------------------------------------------------


@settings(max_examples=60)
@given(
    d1=st.integers(7, 30),
    d2=st.integers(30, 75),
    v1=st.floats(0.05, 2.0),
    v2=st.floats(0.05, 2.0),
)
def test_constant_maturity_between_bracket(d1: int, d2: int, v1: float, v2: float) -> None:
    iv = constant_maturity_iv([(d1, v1), (d2, v2)])
    lo, hi = min(v1, v2), max(v1, v2)
    # Total variance is linear in T, so iv30 sits inside the two vols' range.
    assert lo - 1e-9 <= iv <= hi + 1e-9
    assert constant_maturity_iv([(d2, v2)]) == v2  # one side: flat


def test_constant_maturity_empty() -> None:
    with pytest.raises(ValueError, match="term point"):
        constant_maturity_iv([])


# ---------------------------------------------------------------------------
# forward iv.record
# ---------------------------------------------------------------------------


def _c(exp: dt.date, k: float, kind: str, iv: float | None) -> OptionContract:
    return OptionContract(
        symbol=f"T{exp:%y%m%d}{kind[0].upper()}{int(k * 1000):08d}",
        underlying="T",
        expiration=exp,
        strike=k,
        option_type=kind,
        bid=1.0,
        ask=1.1,
        implied_volatility=iv,
    )


class _Market:
    def __init__(self, bid: float, ask: float, close: float | None = None) -> None:
        self.bid, self.ask, self.close = bid, ask, close
        e1, e2 = DAY + dt.timedelta(days=24), DAY + dt.timedelta(days=38)
        self.chain = [
            _c(e, k, kind, iv)
            for e, base in ((e1, 0.40), (e2, 0.44))
            for k in (170.0, 340.0, 380.0, 420.0)
            for kind, iv in (("call", base + (0.30 if k < 300 else 0)), ("put", base))
        ] + [_c(e1, 380.0, "call", None)]

    def option_chain(self, underlying: str, start: dt.date, end: dt.date) -> list[OptionContract]:
        if underlying == "BAD":
            raise RuntimeError("chain down")
        return [c for c in self.chain if start <= c.expiration <= end]

    def underlying_quote(self, symbol: str) -> UnderlyingQuote:
        return UnderlyingQuote(
            symbol=symbol, bid=self.bid, ask=self.ask, mid=(self.bid + self.ask) / 2, timestamp=NOW
        )

    def history_bars(self, symbol, start, end, timeframe="1Day"):  # noqa: ANN001, ANN201
        if self.close is None:
            return []
        ts = dt.datetime.combine(DAY, dt.time(4), tzinfo=dt.UTC)
        c = self.close
        return [HistoryBar(timestamp=ts, open=c, high=c, low=c, close=c, volume=1.0)]


def test_iv30_from_chain_uses_fixed_spot() -> None:
    # TSLA-like: ask=0 -> raw mid 171 picks the deep-ITM 170 strike (vol 0.70+).
    m = _Market(342.0, 0.0, close=379.0)
    row = iv30_from_chain(m, "tsla", DAY, max_spread_pct=0.05)
    assert row.spot == 379.0 and row.spot_basis == "last_close"
    assert row.iv30 == pytest.approx(constant_maturity_iv([(24, 0.40), (38, 0.44)]))
    assert row.iv30 < 0.45
    assert row.ticker == "TSLA" and row.source == FORWARD and row.method == "chain_cm30"
    assert row.detail["bracket"][0]["dte"] == 24 and row.detail["bracket"][1]["dte"] == 38
    assert row.n_contracts == 16
    # The raw half-price mid would have given the bogus ~0.7 IV.
    bogus = atm_iv_from_chain(m.chain, 171.0, DAY)
    assert bogus is not None and bogus > 0.55  # call+put mean at the 170 strike
    assert atm_term_points(m.chain, 379.0, DAY)[0] == (24, pytest.approx(0.40))


def test_iv30_from_chain_errors() -> None:
    with pytest.raises(LookupError, match="no usable spot"):
        iv30_from_chain(_Market(0.0, 0.0), "X", DAY, max_spread_pct=0.05)
    m = _Market(379.0, 379.1)
    m.chain = [_c(DAY + dt.timedelta(days=24), 380.0, "call", None)]
    with pytest.raises(LookupError, match="no contract with an IV"):
        iv30_from_chain(m, "X", DAY, max_spread_pct=0.05)


def test_parse_cboe_iv30() -> None:
    assert parse_cboe_iv30(json.dumps({"data": {"iv30": 12.548}})) == pytest.approx(0.12548)
    assert parse_cboe_iv30(b'{"data": {"iv30": 0}}') is None
    assert parse_cboe_iv30(b'{"data": {}}') is None
    assert parse_cboe_iv30(b"not json") is None


def test_crosscheck() -> None:
    assert crosscheck_names(["NVDA", "SPY", "AAPL", "TSLA"], 2) == ["SPY", "QQQ", "NVDA", "AAPL"]
    cc = CrossCheck("SPY", 0.1315, 0.1255, 3.0)
    assert cc.diff_pts == 0.6 and not cc.breach
    assert CrossCheck("X", 0.20, 0.16, 3.0).breach
    assert CrossCheck("X", 0.20, None, 3.0).diff_pts is None


def test_record_day_writes_rows_and_flags_breach(conn: Any) -> None:
    m = _Market(379.0, 379.2)
    calls: list[str] = []

    def cboe(url: str) -> bytes:
        calls.append(url)
        if "QQQ" in url:
            raise RuntimeError("cboe down")
        iv = 20.0 if "NVDA" in url else 41.0
        return json.dumps({"data": {"iv30": iv}}).encode()

    res = record_day(
        conn, m, ["NVDA", "AAPL", "BAD", "nvda"], DAY, now=NOW, max_spread_pct=0.05,
        cboe_get=cboe, crosscheck_max_pts=3.0, crosscheck_max_names=1,
    )  # fmt: skip
    assert sorted(r.ticker for r in res.rows) == ["AAPL", "NVDA", "QQQ", "SPY"]
    assert set(res.errors) == {"BAD"}
    assert sorted(c.ticker for c in res.checks) == ["NVDA", "QQQ", "SPY"]  # AAPL not sampled
    assert [c.ticker for c in res.breaches] == ["NVDA"]
    m_ = res.metrics()
    assert m_["breaches"] == ["NVDA"] and m_["crosscheck"]["QQQ"]["cboe"] is None
    assert m_["recorded"] == 4 and m_["errors"] == 1
    nvda = IvStore(conn).rows("NVDA", [FORWARD])[0]
    assert nvda.detail["breach"] is True and nvda.detail["cboe_iv30"] == pytest.approx(0.2)
    assert any(u.endswith("/SPY.json") for u in calls)


def test_record_day_without_cboe(conn: Any) -> None:
    res = record_day(
        conn, _Market(379.0, 379.2), ["AAPL"], DAY, now=NOW, max_spread_pct=0.05,
        cboe_get=None, crosscheck_max_pts=3.0, crosscheck_max_names=5,
    )  # fmt: skip
    assert [r.ticker for r in res.rows] == ["AAPL"] and res.checks == []


# ---------------------------------------------------------------------------
# backfill
# ---------------------------------------------------------------------------


def _bs(s: float, k: float, dte: int, sigma: float, kind: str, r: float = 0.04) -> float:
    return price(BSMInputs(S=s, K=k, t=dte / 365.0, r=r, q=0.0, sigma=sigma, flag=OptionKind(kind)))


class _Hist:
    """Two expiries bracketing 30 DTE for each day; option closes priced at a known vol."""

    def __init__(self, sessions: list[dt.date], vol: float = 0.25) -> None:
        self.sessions = sessions
        self.vol = vol
        self.exps = sorted({d + dt.timedelta(days=n) for d in sessions for n in (21, 42)})
        self.untraded: set[tuple[str, dt.date]] = set()
        self.bar_calls = 0

    def underlying_closes(self, ticker, start, end):  # noqa: ANN001, ANN201
        return {d: 100.0 + i for i, d in enumerate(self.sessions) if start <= d <= end}

    def contracts(self, ticker, exp_start, exp_end, lo, hi):  # noqa: ANN001, ANN201
        out = []
        for e in self.exps:
            if exp_start <= e <= exp_end:
                for k in range(80, 131, 5):
                    if lo <= k <= hi:
                        for kind in ("c", "p"):
                            out.append(
                                bf.ListedContract(f"{ticker}{e:%y%m%d}{kind}{k}", e, float(k), kind)
                            )
        return out

    def daily_bars(self, symbols, start, end):  # noqa: ANN001, ANN201
        self.bar_calls += 1
        closes = self.underlying_closes("", start, end)
        out: dict[str, dict[dt.date, bf.DailyBar]] = {}
        for sym in symbols:
            exp = dt.datetime.strptime(sym[1:7], "%y%m%d").date()
            kind = sym[7]
            k = float(sym[8:])
            for d, s in closes.items():
                if (sym, d) in self.untraded or exp <= d:
                    continue
                p = _bs(s, k, (exp - d).days, self.vol, kind)
                out.setdefault(sym, {})[d] = bf.DailyBar(close=p, volume=10)
        return out


def test_implied_vol_roundtrip_and_failures() -> None:
    p = _bs(100.0, 100.0, 30, 0.3, "c")
    assert bf.implied_vol(p, 100.0, 100.0, 30, 0.04, 0.0, "c") == pytest.approx(0.3, abs=1e-6)
    assert bf.implied_vol(0.0, 100.0, 100.0, 30, 0.04, 0.0, "c") is None
    assert bf.implied_vol(1.0, 100.0, 100.0, 0, 0.04, 0.0, "c") is None
    assert bf.implied_vol(0.5, 100.0, 80.0, 30, 0.04, 0.0, "c") is None  # below intrinsic


def test_backfill_recovers_known_vol_and_is_resumable(conn: Any) -> None:
    sessions = [dt.date(2024, 3, 4) + dt.timedelta(days=i) for i in range(5)]
    h = _Hist(sessions, vol=0.25)
    h.untraded.add(("X" + f"{sessions[2] + dt.timedelta(days=21):%y%m%d}" + "c100", sessions[2]))
    ticks = iter(range(0, 1000, 7))
    rep = bf.backfill(
        conn, h, ["x"], sessions, now=NOW, r=0.04, dividend_yields={},
        progress=lambda _m: None, clock=lambda: float(next(ticks)),
    )  # fmt: skip
    (t,) = rep.tickers
    assert t.stored == 5 and not t.skipped  # day 3 walks outward to the next expiry
    s = IvStore(conn).series("X")
    assert all(v == pytest.approx(0.25, abs=1e-4) for v in s.values())
    row = IvStore(conn).rows("X", [BACKFILL])[0]
    assert row.detail["lo"]["c"]["iv"] == pytest.approx(0.25, abs=1e-4)
    assert row.spot_basis == "last_close" and row.n_contracts == 4
    # Re-run: nothing to do, no data requests.
    calls = h.bar_calls
    rep2 = bf.backfill(conn, h, ["X"], sessions, now=NOW, r=0.04, dividend_yields={})
    assert rep2.tickers[0].already == 5 and h.bar_calls == calls
    out = bf.format_report(rep)
    assert "X" in out and "+/-2 vol pts" in out and "wall 7 s" in out


def test_backfill_skips_with_reasons(conn: Any) -> None:
    sessions = [dt.date(2024, 3, 4), dt.date(2024, 3, 5)]
    h = _Hist(sessions)
    h.exps = [sessions[0] + dt.timedelta(days=21)]  # no expiry above 30 DTE
    rep = bf.backfill(conn, h, ["X"], sessions, now=NOW, r=0.04, dividend_yields={"X": 0.01})
    t = rep.tickers[0]
    assert t.stored == 0 and sum(t.skipped.values()) == 2
    assert "incomplete 30-DTE bracket" in next(iter(t.skipped))
    reasons = [r[0] for r in conn.execute("SELECT reason FROM iv_skips").fetchall()]
    assert len(reasons) == 2


def test_backfill_no_close_and_errors(conn: Any) -> None:
    class Bad(_Hist):
        def contracts(self, *a: Any) -> list[bf.ListedContract]:
            raise RuntimeError("api 500")

    sessions = [dt.date(2024, 3, 4)]
    h = _Hist(sessions)
    rep = bf.backfill(conn, h, ["X"], [*sessions, dt.date(2024, 3, 6)], now=NOW, r=0.04,
                      dividend_yields={})  # fmt: skip
    assert rep.tickers[0].skipped["no underlying close"] == 1
    rep = bf.backfill(conn, Bad(sessions), ["Y"], sessions, now=NOW, r=0.04, dividend_yields={})
    assert next(iter(rep.tickers[0].skipped)).startswith("error: api 500")
    with pytest.raises(ValueError, match="no sessions"):
        bf.backfill(conn, h, ["X"], [], now=NOW, r=0.04, dividend_yields={})


def test_iv30_for_day_edge_cases() -> None:
    day = dt.date(2024, 3, 4)
    assert bf.iv30_for_day({}, {}, day, 100.0, r=0.04, q=0.0)[1].startswith("no listed expiry")
    e = day + dt.timedelta(days=21)
    only_call = {e: {(100.0, "c"): bf.ListedContract("A", e, 100.0, "c")}}
    iv, why, _ = bf.iv30_for_day(only_call, {}, day, 100.0, r=0.04, q=0.0)
    assert iv is None and "both a call and a put" in why
    e30 = day + dt.timedelta(days=30)
    book = {
        (100.0, "c"): bf.ListedContract("C", e30, 100.0, "c"),
        (100.0, "p"): bf.ListedContract("P", e30, 100.0, "p"),
    }
    bars = {
        "C": {day: bf.DailyBar(_bs(100, 100, 30, 0.2, "c"), 3)},
        "P": {day: bf.DailyBar(0.0001, 3)},  # can't be inverted
    }
    iv, why, _ = bf.iv30_for_day({e30: book}, bars, day, 100.0, r=0.04, q=0.0)
    assert iv is None and "inversion failed" in why
    bars["P"] = {day: bf.DailyBar(_bs(100, 100, 30, 0.2, "p"), 3)}
    iv, _, _ = bf.iv30_for_day({e30: book}, bars, day, 100.0, r=0.04, q=0.0)
    assert iv == pytest.approx(0.2, abs=1e-4)  # exactly 30 DTE: both sides of the bracket
    bars["P"] = {day: bf.DailyBar(1.0, 0)}  # zero volume = untraded
    iv, why, _ = bf.iv30_for_day({e30: book}, bars, day, 100.0, r=0.04, q=0.0)
    assert iv is None and "no traded put" in why


# ---------------------------------------------------------------------------
# Option Strategist + validate
# ---------------------------------------------------------------------------

# Synthetic lines in the page's fixed-width layout (numbers made up, not OS data).
OS_HTML = (Path(__file__).parent / "fixtures" / "optionstrategist_sample.html").read_text()


def test_parse_optionstrategist() -> None:
    rows = {r.ticker: r for r in osf.parse(OS_HTML)}
    assert set(rows) == {"AAA", "BBB", "BRK.B"}
    a = rows["AAA"]
    assert (a.day, a.cur_iv, a.days, a.percentile, a.hv20, a.close) == (
        dt.date(2026, 10, 2), 0.225, 600, 0.15, 0.23, 333.79,
    )  # fmt: skip
    assert rows["BBB"].day == dt.date(2026, 10, 2) and rows["BBB"].hv20 == 0.415  # newest kept
    assert rows["BRK.B"].percentile == 1.0
    iv_row = a.to_iv_row()
    assert iv_row.source == OPTIONSTRATEGIST and iv_row.ext_percentile == 0.15
    # Plain text (a saved <pre>) parses too.
    plain = (
        "AAA                                 23    28    29  261002   22.50   600/ 15%ile  333.79\n"
    )
    assert [r.ticker for r in osf.parse(plain)] == ["AAA"]


def test_validate_and_report(conn: Any) -> None:
    s = IvStore(conn)
    day = dt.date(2026, 10, 2)
    assert validate(conn).day is None
    assert "run `arc iv import-optionstrategist`" in format_validation(validate(conn))
    # AAA: 200 days of history rising to today's 0.24 (OS 0.225, 15th pct).
    days = [day - dt.timedelta(days=i) for i in range(200)][::-1]
    s.upsert(
        [IvRow("AAA", d, 0.20 + 0.0002 * i, "bars_bs_cm30", BACKFILL) for i, d in enumerate(days)],
        now=NOW,
    )
    s.upsert([r.to_iv_row() for r in osf.parse(OS_HTML)], now=NOW)
    v = validate(conn, closes=lambda t, d: [100.0 * (1.01 if i % 2 else 0.99) for i in range(30)])
    assert [r.ticker for r in v.rows] == ["AAA"]
    assert sorted(v.missing) == ["BBB", "BRK.B"]
    r = v.rows[0]
    assert r.iv_diff == pytest.approx(abs(0.20 + 0.0002 * 199 - 0.225) * 100)
    assert r.pct_diff == pytest.approx(85.0)  # ours 100th pct vs OS 15th
    assert r.observations == 200 and r.hv_diff is not None
    assert v.verdict() == {"median_iv": True, "median_pct": False, "share_within": False}
    assert not v.passed
    text = format_validation(v)
    assert "overall: FAIL" in text and "worst percentile: AAA 85.0" in text
    assert validate(conn, tickers=["ZZZ"]).rows == []


def test_hv20() -> None:
    assert hv20([100.0] * 10) is None
    closes = [100.0 * math.exp(0.01 * (i % 2)) for i in range(25)]
    assert hv20(closes) == pytest.approx(0.0051 * math.sqrt(252) * 1.96, rel=0.2)
