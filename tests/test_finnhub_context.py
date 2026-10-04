"""E4.8 (D46): shared Finnhub client, cross-process budget, per-ticker context jobs."""

from __future__ import annotations

import datetime as dt
import json
import multiprocessing
import urllib.error
from email.message import Message
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlparse

import pytest
import yaml
from structlog.testing import capture_logs

from arc.config import ArcSettings
from arc.context import ContextStore
from arc.context.kinds import KINDS
from arc.ingest import finnhub as fh
from arc.ingest.finnhub import (
    DbRateLimiter,
    FinnhubClient,
    FinnhubError,
    FinnhubForbidden,
    FinnhubNoKey,
    FinnhubRateLimited,
    MemoryRateLimiter,
    redact,
)
from arc.ingest.finnhub_context import (
    FUNDAMENTAL_FIELDS,
    OPEN_MARKET_CODES,
    InsiderRules,
    parse_basic_financials,
    parse_earnings_surprises,
    parse_insider_transactions,
    parse_recommendation_trends,
    recent_reporters,
    ticker_scope,
)
from arc.routines.config import RoutinesConfig, load_routines
from arc.routines.handlers import (
    BUILTIN_HANDLERS,
    JobContext,
    JobSkippedError,
    finnhub_earnings_history_source,
    finnhub_fundamentals_source,
    finnhub_insider_source,
    finnhub_recs_source,
    resolve_handler,
)
from arc.routines.schedule import slots_between
from arc.store.db import connect
from arc.store.migrate import migrate
from arc.universe.master import SymbolInfo, SymbolMaster
from arc.utils.calendar import ET

if TYPE_CHECKING:
    import sqlite3

FIX = Path(__file__).parent / "fixtures" / "finnhub"
NOW = dt.datetime(2026, 10, 5, 6, 30, tzinfo=ET)  # Monday
TODAY = NOW.date()
KEY = "SENTINEL_KEY_e48_do_not_log"


def fixture(name: str) -> Any:
    return json.loads((FIX / name).read_text())


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = connect(":memory:")
    migrate(c)
    return c


def _http_error(code: int, headers: dict[str, str] | None = None) -> urllib.error.HTTPError:
    msg = Message()
    for k, v in (headers or {}).items():
        msg[k] = v
    return urllib.error.HTTPError(f"https://finnhub.io/api/v1/x?token={KEY}", code, "e", msg, None)


class FakeFinnhub:
    """``get_json`` double keyed on (path, symbol); records every call."""

    def __init__(self, responses: dict[tuple[str, str], Any] | None = None, default: Any = None):
        self.responses = responses or {}
        self.default = default
        self.calls: list[tuple[str, dict[str, list[str]]]] = []

    def __call__(self, url: str, _timeout: float) -> Any:
        u = urlparse(url)
        q = parse_qs(u.query)
        path = u.path.removeprefix("/api/v1")
        self.calls.append((path, q))
        resp = self.responses.get((path, q.get("symbol", [""])[0]), self.default)
        if isinstance(resp, BaseException):
            raise resp
        if callable(resp):
            return resp(path, q)
        return resp


def _client(fake: FakeFinnhub, **kw: Any) -> FinnhubClient:
    return FinnhubClient(KEY, get_json=fake, sleep=lambda _s: None, **kw)


# ---------------------------------------------------------------------------
# Parsers on recorded payloads (tests/fixtures/finnhub, token stripped)
# ---------------------------------------------------------------------------


class TestParsers:
    def test_fixtures_carry_no_token(self) -> None:
        for f in FIX.glob("*.json"):
            assert "token" not in f.read_text().lower(), f

    def test_earnings_surprises(self) -> None:
        p = parse_earnings_surprises("AAPL", fixture("earnings_AAPL.json"), as_of="2026-10-05")
        assert [q.period for q in p.quarters] == [
            "2026-06-30",
            "2026-03-31",
            "2025-12-31",
            "2025-09-30",
        ]
        assert p.quarters[0].actual == 1.91 and p.quarters[0].estimate == 1.9271
        assert p.quarters[0].surprise_pct == pytest.approx(-0.8873)
        assert (p.beat_count, p.miss_count) == (3, 1)
        assert p.source == "finnhub"

    def test_earnings_surprises_edge_cases(self) -> None:
        rows = [
            {"period": f"20{20 + i // 4}-0{1 + 3 * (i % 4)}-28", "actual": 1, "estimate": 1}
            for i in range(10)
        ]
        rows += [
            {"period": "2030-03-31", "actual": None, "estimate": 2.0},  # not yet reported
            {"period": "bad", "actual": 1, "estimate": 0.5},
            "junk",
            {"period": "2030-06-30", "actual": 2.0, "estimate": 1.5, "surprise": None},
        ]
        p = parse_earnings_surprises("X", rows, as_of="2030-07-01")
        assert len(p.quarters) == 8  # newest 8
        assert p.quarters[0].period == "2030-06-30"
        assert p.quarters[0].surprise == pytest.approx(0.5)  # computed when missing
        assert p.quarters[1].actual is None
        assert (p.beat_count, p.miss_count) == (1, 0)  # ties count as neither
        assert parse_earnings_surprises("X", {"error": "x"}, as_of="2030-07-01").quarters == []

    def test_insider_transactions_open_market_only(self) -> None:
        data = fixture("insider_AAPL.json")
        codes = {r["transactionCode"] for r in data["data"]}
        assert codes - OPEN_MARKET_CODES  # the fixture holds grants / exercises too
        p = parse_insider_transactions(
            "AAPL", data, as_of=dt.date(2026, 10, 3), rules=InsiderRules()
        )
        assert p.buy_count == 0 and p.sell_count == 8
        assert p.net_shares == -13430
        assert p.net_value_usd == pytest.approx(-4342254.52)
        assert p.distinct_insiders_selling == 1 and p.distinct_insiders_buying == 0
        assert p.last_txn_date == "2026-09-29"
        assert p.cluster_buy is False
        jpm = parse_insider_transactions(
            "JPM", fixture("insider_JPM.json"), as_of=dt.date(2026, 10, 3), rules=InsiderRules()
        )
        assert jpm.sell_count == 2 and jpm.buy_count == 0  # A and G rows excluded

    def test_insider_cluster_window_and_exclusions(self) -> None:
        def row(name: str, day: str, code: str = "P", change: int = 100, **kw: Any) -> dict:
            return {
                "name": name,
                "transactionDate": day,
                "transactionCode": code,
                "change": change,
                "transactionPrice": kw.get("price", 10.0),
                "isDerivative": kw.get("deriv", False),
                "id": kw.get("id", f"{name}{day}"),
            }

        rows = [
            row("A", "2026-09-01"),
            row("B", "2026-09-15"),
            row("C", "2026-09-30"),  # A..C within 30 days -> cluster
            row("A", "2026-09-02"),  # same buyer twice: still 3 distinct
            row("D", "2026-09-20", code="M"),  # option exercise: excluded
            row("E", "2026-09-20", code="A"),  # grant: excluded
            row("F", "2026-09-20", deriv=True),  # derivative: excluded
            row("G", "2026-05-01"),  # outside the 90-day window
            row("H", "2026-09-21", code="S", change=-50, price=0),  # sale without a price
        ]
        rules = InsiderRules(window_days=90, cluster_buyers=3, cluster_days=30)
        p = parse_insider_transactions("X", {"data": rows}, as_of=dt.date(2026, 10, 1), rules=rules)
        assert (p.buy_count, p.sell_count) == (4, 1)
        assert p.distinct_insiders_buying == 3
        assert p.net_shares == 400 - 50
        assert p.net_value_usd == pytest.approx(4000.0)  # the unpriced sale adds no value
        assert p.cluster_buy is True
        narrow = InsiderRules(window_days=90, cluster_buyers=3, cluster_days=20)
        q = parse_insider_transactions(
            "X", {"data": rows}, as_of=dt.date(2026, 10, 1), rules=narrow
        )
        assert q.cluster_buy is False
        none = parse_insider_transactions(
            "X", {"data": []}, as_of=dt.date(2026, 10, 1), rules=rules
        )
        assert none.net_value_usd is None and none.last_txn_date is None
        dup = parse_insider_transactions(
            "X", {"data": [rows[0], dict(rows[0])]}, as_of=dt.date(2026, 10, 1), rules=rules
        )
        assert dup.buy_count == 1  # an exact repeated row counts once

    def test_recommendation_trends(self) -> None:
        p = parse_recommendation_trends(
            "AAPL", fixture("recommendation_AAPL.json"), as_of="2026-10-05"
        )
        assert p is not None
        assert p.period == "2026-09-01"
        assert (p.strong_buy, p.buy, p.hold, p.sell, p.strong_sell) == (12, 22, 15, 3, 1)
        assert p.prev_period is not None and p.prev_period.period == "2026-08-01"
        assert p.net_change == (12 + 22 - 3 - 1) - (13 + 24 - 3 - 0)
        assert parse_recommendation_trends("X", [], as_of="2026-10-05") is None
        one = parse_recommendation_trends(
            "X", [{"period": "2026-09-01", "buy": 1}], as_of="2026-10-05"
        )
        assert one is not None and one.prev_period is None and one.net_change is None

    def test_basic_financials_trimmed(self) -> None:
        p = parse_basic_financials("AAPL", fixture("metric_AAPL.json"), as_of="2026-10-05")
        assert p is not None
        assert p.beta == pytest.approx(1.106227)
        assert p.high_52w == 345.34 and p.high_52w_date == "2026-09-22"
        assert p.market_cap_musd == 4869932
        assert p.rel_sp500_13w == pytest.approx(4.7819)
        assert p.forward_pe == pytest.approx(35.33932)
        dumped = p.model_dump()
        # Only the trimmed D46 set (+ ticker/as_of/source): margins, ROE, debt are dropped.
        assert set(dumped) == {*FUNDAMENTAL_FIELDS.values(), "ticker", "as_of", "source"}
        assert "roeTTM" in fixture("metric_AAPL.json")["metric"]

    def test_basic_financials_missing_is_none_never_zero(self) -> None:
        spy = parse_basic_financials("SPY", fixture("metric_SPY.json"), as_of="2026-10-05")
        assert spy is not None
        assert spy.forward_pe is None and spy.market_cap_musd is None
        assert spy.eps_growth_ttm_yoy is None
        p = parse_basic_financials(
            "X", {"metric": {"beta": "n/a", "forwardPE": None, "52WeekHighDate": "?"}}, as_of="x"
        )
        assert p is not None and p.beta is None and p.forward_pe is None
        assert p.high_52w_date is None
        assert parse_basic_financials("X", {"metric": {}}, as_of="x") is None
        assert parse_basic_financials("X", [], as_of="x") is None


# ---------------------------------------------------------------------------
# Client + rate limiter
# ---------------------------------------------------------------------------


class TestClient:
    def test_no_key(self) -> None:
        with pytest.raises(FinnhubNoKey, match="no_api_key"):
            FinnhubClient("")

    def test_get_counts_endpoints(self) -> None:
        fake = FakeFinnhub(default={"ok": 1})
        c = _client(fake)
        assert c.get("/stock/earnings", {"symbol": "AAPL"}) == {"ok": 1}
        c.get("/stock/earnings", {"symbol": "MSFT"})
        c.get("/stock/recommendation", {"symbol": "MSFT"})
        assert c.calls == 3
        assert c.endpoints == {"/stock/earnings": 2, "/stock/recommendation": 1}
        assert KEY not in repr(c)

    def test_paid_endpoint_refused_without_a_call(self) -> None:
        fake = FakeFinnhub(default={})
        with pytest.raises(FinnhubForbidden):
            _client(fake).get("/stock/candle", {"symbol": "AAPL"})
        assert fake.calls == []

    def test_403_is_forbidden(self) -> None:
        fake = FakeFinnhub(default=_http_error(403))
        with pytest.raises(FinnhubForbidden, match="forbidden"):
            _client(fake).get("/stock/metric", {"symbol": "AAPL"})

    def test_429_retry_then_rate_limited(self) -> None:
        slept: list[float] = []
        fake = FakeFinnhub(default=_http_error(429, {"Retry-After": "3"}))
        c = FinnhubClient(KEY, get_json=fake, sleep=slept.append)
        with pytest.raises(FinnhubRateLimited, match="rate_limited"):
            c.get("/stock/earnings", {"symbol": "AAPL"})
        assert len(fake.calls) == 2 and 3.0 in slept
        state = {"n": 0}

        def once(_p: str, _q: dict) -> Any:
            state["n"] += 1
            if state["n"] == 1:
                raise _http_error(429, {"Retry-After": "bad"})
            return [1]

        slept.clear()
        c2 = FinnhubClient(KEY, get_json=FakeFinnhub(default=once), sleep=slept.append)
        assert c2.get("/stock/earnings", {"symbol": "AAPL"}) == [1]
        assert 60.0 in slept  # unparsable Retry-After -> the default

    def test_other_errors_are_finnhub_error_and_redacted(self) -> None:
        for exc in (_http_error(500), OSError(f"boom token={KEY}"), ValueError("bad json")):
            with capture_logs() as logs, pytest.raises(FinnhubError) as info:
                _client(FakeFinnhub(default=exc)).get("/stock/earnings", {"symbol": "AAPL"})
            assert KEY not in str(info.value)
            assert all(KEY not in json.dumps(e, default=str) for e in logs)

    def test_raw_errors_keep_type_but_strip_token(self) -> None:
        with pytest.raises(urllib.error.HTTPError) as info:
            _client(FakeFinnhub(default=_http_error(500)), raw_errors=True).get("/x", {})
        assert KEY not in str(info.value.url) and "token=***" in info.value.url

    def test_redact(self) -> None:
        assert redact(f"https://f/x?a=1&token={KEY}&b=2") == "https://f/x?a=1&token=***&b=2"
        assert redact(f"key is {KEY}", KEY) == "key is ***"

    def test_pacing_interval(self) -> None:
        clock = {"t": 0.0}
        slept: list[float] = []

        def sleep(s: float) -> None:
            slept.append(s)
            clock["t"] += s

        c = FinnhubClient(
            KEY,
            get_json=FakeFinnhub(default={}),
            sleep=sleep,
            clock=lambda: clock["t"],
            min_interval_s=2.0,
            limiter=MemoryRateLimiter(sleep=sleep, wall=lambda: clock["t"]),
        )
        for _ in range(3):
            c.get("/stock/earnings", {"symbol": "A"})
        assert slept == [2.0, 2.0]


class TestRateLimiter:
    def test_memory_limiter_sliding_window(self) -> None:
        clock = {"t": 1000.0}

        def sleep(s: float) -> None:
            clock["t"] += s

        lim = MemoryRateLimiter(calls_per_minute=5, sleep=sleep, wall=lambda: clock["t"])
        stamps = []
        for _ in range(12):
            lim.acquire()
            stamps.append(clock["t"])
        _assert_window(stamps, 5)
        assert stamps[5] - stamps[0] >= 60.0

    def test_db_limiter_shared_across_connections(self, tmp_path: Path) -> None:
        """Two connections (as two processes would) share one budget."""
        db = tmp_path / "rl.db"
        c0 = connect(db)
        migrate(c0)
        clock = {"t": 5000.0}
        stamps: list[float] = []

        def sleep(s: float) -> None:
            clock["t"] += s

        def wall() -> float:
            return clock["t"]

        a = DbRateLimiter(connect(db), calls_per_minute=4, sleep=sleep, wall=wall)
        b = DbRateLimiter(connect(db), calls_per_minute=4, sleep=sleep, wall=wall)
        for i in range(14):
            (a if i % 2 else b).acquire()
            stamps.append(clock["t"])
            clock["t"] += 1.0
        _assert_window(stamps, 4)
        row = c0.execute("SELECT value FROM routine_state WHERE key = ?", (fh.RATE_STATE_KEY,))
        assert len(json.loads(row.fetchone()[0])) <= 4

    def test_db_limiter_corrupt_row_resets(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            "INSERT INTO routine_state (key, value, updated_at) VALUES (?, 'nope', 'x')",
            (fh.RATE_STATE_KEY,),
        )
        conn.commit()
        assert DbRateLimiter(conn, calls_per_minute=2).acquire() == 0.0

    def test_db_limiter_two_processes_never_exceed_budget(self, tmp_path: Path) -> None:
        db = tmp_path / "mp.db"
        c = connect(db)
        migrate(c)
        c.close()
        out = tmp_path / "stamps"
        out.mkdir()
        ctx = multiprocessing.get_context("spawn")
        procs = [
            ctx.Process(target=_worker, args=(str(db), str(out / f"{i}.json"))) for i in (0, 1)
        ]
        for p in procs:
            p.start()
        for p in procs:
            p.join(60)
            assert p.exitcode == 0
        stamps = sorted(t for f in out.glob("*.json") for t in json.loads(f.read_text()))
        assert len(stamps) == 16
        # 6 calls per 2-second window (scaled minute) across both processes
        _assert_window(stamps, 6, window=2.0)


def _worker(db: str, out: str) -> None:
    import time as _t

    # Record the stamp the limiter admitted (its last wall() read), not time.time()
    # after acquire() returns: a descheduled process would stamp late and make two
    # admissions look like they share a window (seen on CI).
    last = {"t": 0.0}

    def wall() -> float:
        last["t"] = _t.time()
        return last["t"]

    lim = DbRateLimiter(connect(db), calls_per_minute=6, window_s=2.0, wall=wall)
    got = []
    for _ in range(8):
        lim.acquire()
        got.append(last["t"])
    Path(out).write_text(json.dumps(got))


def _assert_window(stamps: list[float], limit: int, window: float = 60.0) -> None:
    s = sorted(stamps)
    for i, t in enumerate(s):
        inside = [x for x in s[i:] if x < t + window - 1e-6]
        assert len(inside) <= limit, (t, inside)


# ---------------------------------------------------------------------------
# Ticker scope
# ---------------------------------------------------------------------------


def _master(*syms: tuple[str, list[str]]) -> SymbolMaster:
    return SymbolMaster(
        fetched_at=NOW,
        symbols={s: SymbolInfo(symbol=s, sources=src) for s, src in syms},
    )


def _candidate(conn: sqlite3.Connection, ticker: str) -> None:
    ContextStore(conn).write(
        kind="candidate",
        subject=ticker,
        payload={
            "ticker": ticker,
            "stance": "bullish",
            "catalyst_type": "news",
            "catalyst_date": None,
            "confidence": 0.6,
            "sources": ["https://x"],
            "created_at": NOW.isoformat(),
        },
        produced_by="scout",
        ttl=None,
        now=NOW - dt.timedelta(hours=1),
    )


def _open_structure(conn: sqlite3.Connection, ticker: str, status: str = "open") -> None:
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute(
        "INSERT INTO open_structures (id, ticker, open_proposal_hash, candidate_id,"
        " structure_json, contracts, entry_net, opened_at, status)"
        " VALUES (?, ?, ?, 'c', '{}', 1, '1.0', '2026-10-01', ?)",
        (f"os-{ticker}-{status}", ticker, f"h-{ticker}-{status}", status),
    )
    conn.commit()


class TestScope:
    def test_union_order_etf_skip_and_cap(self, conn: sqlite3.Connection) -> None:
        _candidate(conn, "ORCL")
        _candidate(conn, "AAPL")  # already seed
        _open_structure(conn, "NFLX")
        _open_structure(conn, "COST", status="closed")  # closed: out
        master = _master(("ARKK", ["alpaca"]), ("ORCL", ["sec", "alpaca"]))
        _candidate(conn, "ARKK")  # alpaca-only listing -> fund
        scope = ticker_scope(
            conn,
            seed=["SPY", "AAPL", "MSFT", "brk-b"],
            now=NOW,
            max_tickers=40,
            master=master,
            etfs=frozenset({"SPY"}),
        )
        assert scope.tickers == ["AAPL", "MSFT", "BRK.B", "ORCL", "NFLX"]
        assert set(scope.etfs_skipped) == {"SPY", "ARKK"}
        assert scope.sources == {"seed": 3, "candidates": 1, "open": 1}
        with capture_logs() as logs:
            capped = ticker_scope(conn, seed=["AAPL", "MSFT", "BRK.B"], now=NOW, max_tickers=2)
        assert capped.tickers == ["AAPL", "MSFT"]  # seed first
        assert capped.dropped == ["BRK.B", "ORCL", "ARKK", "NFLX"]
        assert any(e["event"] == "finnhub.scope_capped" and e["dropped"] == 4 for e in logs)

    def test_recent_reporters(self, conn: sqlite3.Connection) -> None:
        for sym, day in (("AAPL", "2026-10-03"), ("MSFT", "2026-09-30"), ("NVDA", "2026-10-05")):
            conn.execute(
                "INSERT INTO raw_docs (id, source, url, published_at, text, tickers_hint,"
                " content_hash, ingested_at) VALUES (?, 'earnings', ?, ?, 't', ?, ?, 'x')",
                (sym, f"u/{sym}", f"{day}T00:00:00+00:00", json.dumps([sym]), sym),
            )
        conn.execute(
            "INSERT INTO raw_docs (id, source, url, published_at, text, tickers_hint,"
            " content_hash, ingested_at) VALUES ('bad', 'earnings', 'u', '2026-10-03', 't',"
            " 'not json', 'bad', 'x')"
        )
        got = recent_reporters(conn, ["AAPL", "MSFT", "NVDA", "TSLA"], TODAY, 1, 3)
        assert got == ["AAPL"]  # MSFT 5 days ago, NVDA today


# ---------------------------------------------------------------------------
# Handlers: outcomes, writes, manifests, redaction
# ---------------------------------------------------------------------------

JOBS = {
    "finnhub.insider": ("insider_activity", finnhub_insider_source),
    "finnhub.recs": ("analyst_recs", finnhub_recs_source),
    "finnhub.fundamentals": ("fundamentals", finnhub_fundamentals_source),
    "finnhub.earnings_history": ("earnings_history", finnhub_earnings_history_source),
}


def _routines(job: str, **opts: Any) -> RoutinesConfig:
    kind = JOBS[job][0]
    return RoutinesConfig.model_validate(
        {
            "context_ttl": {kind: {"ttl": "8d", "supersede": "latest"}},
            "sources": {
                job: {"schedule": ["06:30"], "writes": [kind], "category": "company_data", **opts}
            },
        }
    )


def _settings(key: str = KEY, **kw: Any) -> ArcSettings:
    return ArcSettings(
        env="paper",
        finnhub_api_key=key,
        universe=kw.pop("universe", ["SPY", "AAPL", "MSFT", "JPM"]),
        universe_mode="strict",
        **kw,
    )


def _ctx(
    conn: sqlite3.Connection, job: str, settings: ArcSettings, now: dt.datetime = NOW, **opts: Any
) -> JobContext:
    routines = _routines(job, **opts)
    kind, spec = routines.step(job)
    return JobContext(
        job=job,
        kind=kind,
        spec=spec,
        run_id=f"run-{job}",
        chain_run_id=None,
        scheduled_for=now,
        now=now,
        conn=conn,
        snapshot=ContextStore(conn).snapshot(now),
        routines=routines,
        settings_factory=lambda: settings,
    )


def _recorded() -> dict[tuple[str, str], Any]:
    return {
        ("/stock/insider-transactions", "AAPL"): fixture("insider_AAPL.json"),
        ("/stock/insider-transactions", "JPM"): fixture("insider_JPM.json"),
        ("/stock/insider-transactions", "MSFT"): {"data": [], "symbol": "MSFT"},
        ("/stock/recommendation", "AAPL"): fixture("recommendation_AAPL.json"),
        ("/stock/recommendation", "MSFT"): fixture("recommendation_AAPL.json"),
        ("/stock/recommendation", "JPM"): [],
        ("/stock/metric", "AAPL"): fixture("metric_AAPL.json"),
        ("/stock/metric", "MSFT"): fixture("metric_AAPL.json"),
        ("/stock/metric", "JPM"): fixture("metric_AAPL.json"),
        ("/stock/earnings", "AAPL"): fixture("earnings_AAPL.json"),
        ("/stock/earnings", "MSFT"): fixture("earnings_AAPL.json"),
        ("/stock/earnings", "JPM"): fixture("earnings_AAPL.json"),
    }


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeFinnhub:
    f = FakeFinnhub(_recorded(), default={})
    monkeypatch.setattr(fh, "_get_json", f)
    return f


class TestHandlers:
    def test_registered(self) -> None:
        cfg = load_routines()
        for job, (kind, fn) in JOBS.items():
            assert job in BUILTIN_HANDLERS
            spec = cfg.sources[job]
            assert resolve_handler(job, spec) is fn
            assert spec.writes == [kind]
            assert spec.lane.value == "background"
            assert kind in KINDS and kind in cfg.context_ttl
        ttl = {k: (str(cfg.context_ttl[k].ttl), cfg.context_ttl[k].supersede.value) for k in (
            "earnings_history", "insider_activity", "analyst_recs", "fundamentals")}  # fmt: skip
        assert {k: v[1] for k, v in ttl.items()} == dict.fromkeys(ttl, "latest")

    @pytest.mark.parametrize("job", list(JOBS))
    def test_each_job_writes_its_kind(
        self, conn: sqlite3.Connection, fake: FakeFinnhub, job: str
    ) -> None:
        kind, fn = JOBS[job]
        ctx = _ctx(conn, job, _settings())
        with capture_logs() as logs:
            result = fn(ctx)
        entries = ContextStore(conn).query(as_of=NOW, kinds=[kind])
        assert {e.subject for e in entries} <= {"AAPL", "MSFT", "JPM"}
        assert "SPY" not in {e.subject for e in entries}  # ETF skipped
        assert all(e.produced_by == job for e in entries)
        m = result.metrics
        assert m["calls"] == 3 and m["tickers"] == 3
        assert m["written"] == len(entries) >= 2
        assert m["failed_tickers"] == []
        assert set(m) >= {"calls", "tickers", "written", "failed_tickers", "duration_s"}
        assert all(p == fake.calls[0][0] for p, _ in fake.calls)
        done = next(e for e in logs if e["event"] == "finnhub.done")
        assert done["calls"] == 3 and done["written"] == m["written"]
        (inp,) = ctx.external_inputs
        assert inp.source == "finnhub" and inp.count == 3 and inp.as_of == NOW
        assert inp.name == f"finnhub:{fake.calls[0][0]}"

    def test_insider_request_window(self, conn: sqlite3.Connection, fake: FakeFinnhub) -> None:
        finnhub_insider_source(_ctx(conn, "finnhub.insider", _settings()))
        _, q = fake.calls[0]
        assert q["from"] == [(TODAY - dt.timedelta(days=90)).isoformat()]
        assert q["to"] == [TODAY.isoformat()]

    def test_no_key_skipped_with_one_notice_per_day(self, conn: sqlite3.Connection) -> None:
        s = _settings(key="")
        with pytest.raises(JobSkippedError, match="no_api_key") as first:
            finnhub_insider_source(_ctx(conn, "finnhub.insider", s))
        assert "no_api_key" in first.value.notice
        with pytest.raises(JobSkippedError) as again:
            finnhub_recs_source(_ctx(conn, "finnhub.recs", s))
        assert again.value.notice == ""

    def test_403_fails_forbidden(self, conn: sqlite3.Connection, fake: FakeFinnhub) -> None:
        fake.responses[("/stock/metric", "MSFT")] = _http_error(403)
        with pytest.raises(FinnhubForbidden, match="forbidden"):
            finnhub_fundamentals_source(_ctx(conn, "finnhub.fundamentals", _settings()))

    def test_429_fails_rate_limited(
        self, conn: sqlite3.Connection, fake: FakeFinnhub, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(fh.time, "sleep", lambda _s: None)
        fake.responses[("/stock/recommendation", "AAPL")] = _http_error(429)
        ctx = _ctx(conn, "finnhub.recs", _settings())
        with pytest.raises(FinnhubRateLimited, match="rate_limited"):
            finnhub_recs_source(ctx)
        assert ctx.external_inputs and ctx.external_inputs[0].count == 2  # retried once

    def test_partial_failure_ok_until_half(
        self, conn: sqlite3.Connection, fake: FakeFinnhub
    ) -> None:
        s = _settings(universe=["AAPL", "MSFT", "JPM"])
        fake.responses[("/stock/metric", "JPM")] = _http_error(500)
        r = finnhub_fundamentals_source(_ctx(conn, "finnhub.fundamentals", s))
        assert r.metrics["failed_tickers"] == ["JPM"] and r.metrics["written"] == 2
        assert "1 failed (JPM)" in r.summary
        fake.responses[("/stock/metric", "MSFT")] = OSError("down")
        with pytest.raises(FinnhubError, match="2/3 tickers failed"):
            finnhub_fundamentals_source(_ctx(conn, "finnhub.fundamentals", s))
        fake.responses[("/stock/metric", "AAPL")] = OSError("down")
        with pytest.raises(FinnhubError, match="3/3 tickers failed"):
            finnhub_fundamentals_source(_ctx(conn, "finnhub.fundamentals", s))

    def test_exactly_half_failed_is_ok(self, conn: sqlite3.Connection, fake: FakeFinnhub) -> None:
        s = _settings(universe=["AAPL", "MSFT"])
        fake.responses[("/stock/metric", "MSFT")] = _http_error(502)
        r = finnhub_fundamentals_source(_ctx(conn, "finnhub.fundamentals", s))
        assert r.metrics["failed_tickers"] == ["MSFT"]

    def test_options_override_scope(self, conn: sqlite3.Connection, fake: FakeFinnhub) -> None:
        ctx = _ctx(
            conn, "finnhub.recs", _settings(), tickers=["AAPL", "MSFT", "JPM"], max_tickers=1
        )
        r = finnhub_recs_source(ctx)
        assert r.metrics["tickers"] == 1 and r.metrics["scope"]["dropped"] == 2
        assert "2 over cap" in r.summary

    def test_earnings_history_full_monday_then_recent_only(
        self, conn: sqlite3.Connection, fake: FakeFinnhub
    ) -> None:
        job = "finnhub.earnings_history"
        r = finnhub_earnings_history_source(_ctx(conn, job, _settings()))
        assert r.metrics["mode"] == "full" and r.metrics["tickers"] == 3
        conn.execute(
            "INSERT INTO raw_docs (id, source, url, published_at, text, tickers_hint,"
            " content_hash, ingested_at) VALUES ('d', 'earnings', 'u',"
            " '2026-10-05T00:00:00+00:00', 't', '[\"JPM\"]', 'h', 'x')"
        )
        tue = NOW + dt.timedelta(days=1)
        fake.calls.clear()
        r2 = finnhub_earnings_history_source(_ctx(conn, job, _settings(), now=tue))
        assert r2.metrics["mode"] == "recent_reporters"
        assert [q["symbol"] for _, q in fake.calls] == [["JPM"]]
        # Monday was a holiday: the first run 7+ days after the last full run is full.
        later = NOW + dt.timedelta(days=8)
        r3 = finnhub_earnings_history_source(_ctx(conn, job, _settings(), now=later))
        assert r3.metrics["mode"] == "full"

    def test_key_never_logged_or_stored(
        self, conn: sqlite3.Connection, fake: FakeFinnhub, tmp_path: Path
    ) -> None:
        """Sentinel key: grep logs, exceptions, context payloads and manifests."""
        from arc.routines.dispatcher import Dispatcher
        from arc.routines.heartbeat import RecordingNotifier
        from arc.routines.locks import LockManager

        fake.responses[("/stock/metric", "JPM")] = OSError(f"conn reset token={KEY}")
        fake.responses[("/stock/metric", "MSFT")] = _http_error(503)
        routines = _routines("finnhub.fundamentals")
        notifier = RecordingNotifier()
        d = Dispatcher(
            conn,
            routines,
            notifier=notifier,
            settings_factory=lambda: _settings(),
            locks=LockManager(tmp_path),
            is_halted=lambda: False,
        )
        with capture_logs() as logs:
            d.run_job("finnhub.fundamentals", NOW, reason="schedule", now=NOW)
        dumped = [json.dumps(e, default=str) for e in logs] + [p for _, p in notifier.posts]
        for table in ("context_entries", "run_manifests", "routine_runs", "routine_state"):
            dumped += [
                json.dumps(dict(r), default=str) for r in conn.execute(f"SELECT * FROM {table}")
            ]  # noqa: S608
        blob = "\n".join(dumped)
        assert KEY not in blob
        assert "token=***" in blob  # the redacted error text did get logged
        row = conn.execute(
            "SELECT status, error FROM routine_runs WHERE job = 'finnhub.fundamentals'"
        ).fetchone()
        assert row["status"] == "failed" and "2/3 tickers failed" in row["error"]
        manifest = json.loads(conn.execute("SELECT payload FROM run_manifests").fetchone()[0])
        (inp,) = manifest["external_inputs"]
        assert inp["name"] == "finnhub:/stock/metric" and inp["count"] == 3


class TestConfigOnlyCadence:
    def test_yaml_changes_schedule_without_code(self, tmp_path: Path) -> None:
        raw = yaml.safe_load(Path("config/routines.yaml").read_text())
        cfg = load_routines()
        week = (
            dt.datetime(2026, 10, 5, 0, 0, tzinfo=ET),
            dt.datetime(2026, 10, 11, 23, 0, tzinfo=ET),
        )
        ins = slots_between(cfg.sources["finnhub.insider"], *week)
        assert [s.strftime("%a %H:%M") for s in ins][:2] == ["Mon 06:30", "Tue 06:30"]
        assert len(ins) == 5
        recs = slots_between(cfg.sources["finnhub.recs"], *week)
        assert [s.strftime("%a %H:%M") for s in recs] == ["Mon 06:40"]
        fund = slots_between(cfg.sources["finnhub.fundamentals"], *week)
        assert [s.strftime("%a %H:%M") for s in fund] == ["Mon 06:50"]
        eh = slots_between(cfg.sources["finnhub.earnings_history"], *week)
        assert len(eh) == 5 and eh[0].strftime("%H:%M") == "07:00"

        raw["sources"]["finnhub.recs"]["schedule"] = ["08:15"]
        raw["sources"]["finnhub.recs"]["days"] = ["wed", "fri"]
        raw["sources"]["finnhub.insider"]["max_tickers"] = 7
        p = tmp_path / "routines.yaml"
        p.write_text(yaml.safe_dump(raw))
        cfg2 = load_routines(p)
        recs2 = slots_between(cfg2.sources["finnhub.recs"], *week)
        assert [s.strftime("%a %H:%M") for s in recs2] == ["Wed 08:15", "Fri 08:15"]
        assert cfg2.sources["finnhub.insider"].options["max_tickers"] == 7


def test_registry_exposes_finnhub_budget() -> None:
    from arc.control.registry import REGISTRY

    assert REGISTRY["finnhub_calls_per_minute"].hard_ceiling == 60
    assert ArcSettings().finnhub_calls_per_minute == 55
    assert ArcSettings().finnhub_max_tickers == 40


class TestEarningsCalendarOnSharedClient:
    """E4.1d's calendar now goes through the shared client and the shared budget."""

    def test_calendar_uses_shared_db_budget(self, conn: sqlite3.Connection) -> None:
        from arc.ingest.earnings import EarningsFetchConfig, fetch_earnings

        fake = FakeFinnhub(default={"earningsCalendar": []})
        cfg = EarningsFetchConfig(lookback_days=0, horizon_days=13)
        fetch_earnings(
            conn, _settings(), cfg=cfg, today=TODAY, get_json=fake, sleep=lambda _s: None
        )
        assert [p for p, _ in fake.calls] == ["/calendar/earnings"] * 2
        stamps = conn.execute(
            "SELECT value FROM routine_state WHERE key = ?", (fh.RATE_STATE_KEY,)
        ).fetchone()
        assert len(json.loads(stamps[0])) == 2  # counted against the key-wide budget

    def test_calendar_403_is_forbidden(self, conn: sqlite3.Connection) -> None:
        from arc.ingest.earnings import EarningsFetchConfig, EarningsFetchError, fetch_earnings

        cfg = EarningsFetchConfig(lookback_days=0, horizon_days=3)
        with pytest.raises(EarningsFetchError, match="forbidden") as info:
            fetch_earnings(
                conn, _settings(), cfg=cfg, today=TODAY,
                get_json=FakeFinnhub(default=_http_error(403)), sleep=lambda _s: None,
            )  # fmt: skip
        assert info.value.reason == "forbidden"
        assert KEY not in str(info.value)
