"""E12.2 / D51: momentum tier source (SPMO holdings), monthly cadence + catch-up."""

from __future__ import annotations

import datetime as dt
import json
import textwrap
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
import requests
import yaml
from hypothesis import given
from hypothesis import strategies as st

from arc.config import ArcSettings
from arc.context.store import ContextStore
from arc.ingest.options_data import http_get
from arc.monitoring import alerts, checks
from arc.routines.config import CatchUp, Days, JobSpec, RoutinesConfig, load_routines
from arc.routines.dispatcher import Dispatcher
from arc.routines.handlers import BUILTIN_HANDLERS, JobContext, JobResult
from arc.routines.heartbeat import RecordingNotifier
from arc.routines.schedule import catch_up_slots, day_matches, is_month_start, next_slot
from arc.store.db import connect
from arc.store.migrate import migrate
from arc.universe.config import MomentumConfig, load_universe_config
from arc.universe.master import SymbolInfo, SymbolMaster
from arc.universe.momentum import (
    HoldingRow,
    MomentumError,
    build_payload,
    fetch_momentum,
    is_stale,
    notice_line,
    parse_schwab,
    parse_stockanalysis,
    previous_members,
    select_members,
    tier_diff,
)
from arc.universe.tiers import Tier, UniverseTierPayload, build_active
from arc.utils.calendar import ET

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable

FIX = Path(__file__).parent / "fixtures" / "universe"
SA_HTML = (FIX / "stockanalysis_spmo_holdings.html").read_text()
SW_HTML = (FIX / "schwab_spmo_holdings.html").read_text()
NOW = dt.datetime(2026, 10, 5, 6, 0, tzinfo=ET)
CFG = MomentumConfig()
SA_URL = CFG.urls["stockanalysis"]
SW_URL = CFG.urls["schwab"]
# stockanalysis Oct 2 2026, in listed order (the card's "today's top 25").
SA_ORDER = [
    "MU",
    "AAPL",
    "AMD",
    "INTC",
    "GOOGL",
    "JNJ",
    "GOOG",
    "LRCX",
    "XOM",
    "AMAT",
    "SNDK",
    "CSCO",
    "MRK",
    "CAT",
    "STX",
    "PANW",
    "WDC",
    "UNH",
    "KO",
    "KLAC",
    "DELL",
    "MRVL",
    "MS",
    "LITE",
    "GS",
]
SHORT_PAGE = (
    "holdings:[" + ",".join(f'{{no:{i},n:"X",s:"$X{i}",as:"1%"}}' for i in range(19)) + "]"
).encode()
CORE_OVERLAP = {"MU", "AAPL", "AMD", "INTC", "GOOGL", "XOM", "UNH"}


def et(*a: int) -> dt.datetime:
    return dt.datetime(*a, tzinfo=ET)


def _settings(**kw: Any) -> ArcSettings:
    return ArcSettings(_env_file=None, env="paper", **kw)  # type: ignore[call-arg]


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    c = connect(tmp_path / "arc.db")
    migrate(c)
    return c


def _getter(pages: dict[str, bytes | Exception]) -> Callable[[str], bytes]:
    def get(url: str) -> bytes:
        page = pages[url]
        if isinstance(page, Exception):
            raise page
        return page

    return get


# -- parsers -------------------------------------------------------------------


class TestParsers:
    def test_stockanalysis_fixture(self) -> None:
        rows, as_of = parse_stockanalysis(SA_HTML)
        assert len(rows) == 25
        assert [r.symbol for r in rows] == SA_ORDER
        assert rows[0] == HoldingRow(1, "MU", "Micron Technology, Inc.", 9.48)
        assert as_of == dt.date(2026, 10, 2)

    def test_schwab_fixture(self) -> None:
        rows, as_of = parse_schwab(SW_HTML)
        assert len(rows) == 20  # server-rendered first page only
        assert rows[0].symbol == "MU" and rows[0].weight == pytest.approx(9.71)
        assert [r.rank for r in rows] == list(range(1, 21))
        assert as_of == dt.date(2026, 10, 1)

    def test_garbage_pages(self) -> None:
        assert parse_stockanalysis("<html>no data</html>") == ([], None)
        assert parse_schwab("<html>no data</html>") == ([], None)
        bad_date = 'holdings:[{no:1,n:"X",s:"$X",as:"1%"}],lastUpdated:"Foo 99, 2026"'
        assert parse_stockanalysis(bad_date)[1] is None
        assert parse_schwab("gHoldingsAsOfDate = '13/45/2026'")[1] is None


# -- selection -----------------------------------------------------------------


def _row(rank: int, sym: str, w: float, name: str = "Co Inc") -> HoldingRow:
    return HoldingRow(rank, sym, name, w)


class TestSelect:
    def test_fixture_collapses_goog(self) -> None:
        rows, _ = parse_stockanalysis(SA_HTML)
        picks, dropped = select_members(rows, size=25, aliases={"GOOG": "GOOGL"})
        syms = [p.symbol for p in picks]
        assert len(picks) == 24 and "GOOG" not in syms and dropped == []
        googl = next(p for p in picks if p.symbol == "GOOGL")
        assert googl.merged == ("GOOG",) and googl.source_ranks == (5, 7)
        assert googl.weight == pytest.approx(4.82 + 3.75, abs=0.01)
        assert syms.index("GOOGL") == 2  # collapsed weight re-ranks it above AMD
        assert set(syms) >= CORE_OVERLAP

    def test_etf_fund_cash_and_non_optionable_dropped(self) -> None:
        master = SymbolMaster(
            fetched_at=NOW,
            symbols={
                "AAA": SymbolInfo(symbol="AAA", sources=["sec", "alpaca"], options=True),
                "NOOP": SymbolInfo(symbol="NOOP", sources=["sec", "alpaca"], options=False),
                "XLK": SymbolInfo(symbol="XLK", sources=["alpaca"], options=True),
            },
        )
        rows = [
            _row(1, "AAA", 5.0),
            _row(2, "SPY", 4.0),  # known ETF
            _row(3, "XLK", 3.5),  # Alpaca-only master row = a fund
            _row(4, "AGPXX", 3.0, "Invesco Government & Agency Portfolio"),  # money market
            _row(5, "", 2.0, "Cash"),  # not a ticker
            _row(6, "NOOP", 1.5),
            _row(7, "BBB", 1.0, "Some Sector ETF"),  # fund by name
            _row(8, "$brk-b", 0.5, "Berkshire Hathaway"),
        ]
        picks, dropped = select_members(
            rows, size=25, aliases={}, master=master, etfs={"SPY", "QQQ"}
        )
        assert [p.symbol for p in picks] == ["AAA", "BRK.B"]
        reasons = dict(dropped)
        assert reasons["SPY"] == reasons["XLK"] == reasons["AGPXX"] == reasons["BBB"] == "fund"
        assert reasons["Cash"] == "not_a_ticker"
        assert reasons["NOOP"].startswith("not_optionable")

    def test_size_cut_and_ties(self) -> None:
        rows = [_row(1, "BBB", 2.0), _row(2, "AAA", 2.0), _row(3, "CCC", 1.0)]
        picks, dropped = select_members(rows, size=2, aliases={})
        assert [p.symbol for p in picks] == ["BBB", "AAA"]  # tie -> best source rank
        assert dropped == [("CCC", "over_size")]

    @given(
        st.lists(
            st.tuples(
                st.sampled_from(["A", "B", "C", "D", "E", "GOOG", "GOOGL"]), st.floats(0, 10)
            ),
            max_size=30,
        ),
        st.integers(1, 10),
    )
    def test_properties(self, raw: list[tuple[str, float]], size: int) -> None:
        rows = [_row(i, s, w) for i, (s, w) in enumerate(raw, 1)]
        picks, dropped = select_members(rows, size=size, aliases={"GOOG": "GOOGL"})
        syms = [p.symbol for p in picks]
        assert len(syms) == len(set(syms)) <= size and "GOOG" not in syms
        weights = [p.weight for p in picks]
        assert weights == sorted(weights, reverse=True)
        assert picks == select_members(rows, size=size, aliases={"GOOG": "GOOGL"})[0]
        assert all(reason == "over_size" for _, reason in dropped)

    def test_tier_diff(self) -> None:
        assert tier_diff(["A", "B", "NEM"], ["A", "B", "LITE", "GS"]) == (["LITE", "GS"], ["NEM"])


# -- fetch + fallback -----------------------------------------------------------


class TestFetch:
    def test_primary(self) -> None:
        f = fetch_momentum(
            CFG,
            source_order=["stockanalysis", "schwab"],
            size=25,
            user_agent="t",
            get=_getter({SA_URL: SA_HTML.encode()}),
        )
        assert f.source == "stockanalysis" and f.url == SA_URL and len(f.rows) == 25
        assert len(f.tickers) == 24 and f.partial  # 25 rows, GOOG collapsed -> 24 names
        assert f.as_of == dt.date(2026, 10, 2) and len(f.digest) == 64 and f.errors == {}

    @pytest.mark.parametrize(
        "primary",
        [
            requests.ConnectionError("down"),
            b"<html>changed layout</html>",
            # 19 rows < min_rows 20
            (
                "holdings:["
                + ",".join(f'{{no:{i},n:"X",s:"$X{i}",as:"1%"}}' for i in range(19))
                + "]"
            ).encode(),
        ],
    )
    def test_fallback_to_schwab(self, primary: bytes | Exception) -> None:
        f = fetch_momentum(
            CFG,
            source_order=["stockanalysis", "schwab"],
            size=25,
            user_agent="t",
            get=_getter({SA_URL: primary, SW_URL: SW_HTML.encode()}),
        )
        assert f.source == "schwab" and f.partial and len(f.rows) == 20
        assert f.as_of == dt.date(2026, 10, 1) and "stockanalysis" in f.errors

    def test_all_fail(self) -> None:
        with pytest.raises(MomentumError, match="stockanalysis: .*schwab: .*bogus: unknown"):
            fetch_momentum(
                CFG,
                source_order=["stockanalysis", "schwab", "bogus"],
                size=25,
                user_agent="t",
                get=_getter({SA_URL: requests.Timeout("t/o"), SW_URL: b"<html></html>"}),
            )


class TestHttpGet:
    class _Resp:
        def __init__(self, status: int) -> None:
            self.status_code = status
            self.content = b"ok"

        def raise_for_status(self) -> None:
            if self.status_code >= 400:
                raise requests.HTTPError(response=self)  # type: ignore[arg-type]

    def test_retries_5xx_and_connection_then_ok(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seq: list[Any] = [requests.ConnectionError("x"), self._Resp(503), self._Resp(200)]

        def fake(*_a: Any, **_k: Any) -> Any:
            r = seq.pop(0)
            if isinstance(r, Exception):
                raise r
            return r

        monkeypatch.setattr(requests, "get", fake)
        sleeps: list[float] = []
        assert http_get("u", "ua", retries=2, sleep=sleeps.append) == b"ok"
        assert sleeps == [2.0, 4.0]

    def test_no_retry_on_4xx_or_when_exhausted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(requests, "get", lambda *a, **k: self._Resp(404))
        with pytest.raises(requests.HTTPError):
            http_get("u", "ua", retries=3, sleep=lambda _s: None)
        monkeypatch.setattr(requests, "get", lambda *a, **k: self._Resp(500))
        with pytest.raises(requests.HTTPError):
            http_get("u", "ua", retries=1, sleep=lambda _s: None)

        def boom(*_a: Any, **_k: Any) -> Any:
            raise requests.Timeout("t")

        monkeypatch.setattr(requests, "get", boom)
        with pytest.raises(requests.Timeout):
            http_get("u", "ua")  # default: no retries


# -- payload / notice / stale ---------------------------------------------------


def _fetch(pages: dict[str, bytes | Exception] | None = None) -> Any:
    return fetch_momentum(
        CFG,
        source_order=["stockanalysis", "schwab"],
        size=25,
        user_agent="t",
        get=_getter(pages or {SA_URL: SA_HTML.encode()}),
    )


def test_payload_and_notice() -> None:
    f = _fetch()
    p = build_payload(f, now=NOW)
    assert p.tier is Tier.MOMENTUM and p.source == "stockanalysis" and p.url == SA_URL
    assert p.partial and p.source_as_of == dt.date(2026, 10, 2) and p.digest == f.digest
    assert [m.rank for m in p.members] == list(range(1, 25))
    assert "incl. GOOG" in p.members[2].reason
    assert notice_line(f, None, stale=False, primary="stockanalysis") == (
        "Momentum tier: first list · 24 names · as of Oct 2 · partial (25 rows listed)"
    )
    prev = [t for t in f.tickers if t not in ("LITE", "GS")] + ["NEM"]
    line = notice_line(f, prev, stale=True, primary="stockanalysis")
    assert line.startswith("Momentum tier: +LITE +GS \u2212NEM · 24 names · as of Oct 2")
    assert line.endswith("STALE source")
    assert "no change" in notice_line(f, f.tickers, stale=False, primary="stockanalysis")
    sw = _fetch({SA_URL: requests.ConnectionError("x"), SW_URL: SW_HTML.encode()})
    assert "fallback schwab" in notice_line(sw, None, stale=False, primary="stockanalysis")
    sw.as_of = None
    assert "as of unknown" in notice_line(sw, None, stale=False, primary="stockanalysis")


def test_is_stale() -> None:
    assert not is_stale(dt.date(2026, 9, 1), dt.date(2026, 10, 11), 40)
    assert is_stale(dt.date(2026, 9, 1), dt.date(2026, 10, 12), 40)
    assert is_stale(None, dt.date(2026, 10, 12), 40)


def test_v1_payload_still_parses() -> None:
    """Rows written before E12.2 (schema v1, no url/partial) read with defaults."""
    v1 = {"tier": "momentum", "members": [], "fetched_at": NOW.isoformat(), "source": "x"}
    p = UniverseTierPayload.model_validate_json(json.dumps(v1))
    assert p.url == "" and p.partial is False


# -- schedule: month_start + catch-up -------------------------------------------


class TestMonthStart:
    @pytest.mark.parametrize(
        ("day", "expected"),
        [
            (dt.date(2026, 10, 1), True),  # Thu
            (dt.date(2026, 10, 2), False),
            (dt.date(2026, 1, 2), True),  # Jan 1 holiday
            (dt.date(2026, 1, 1), False),
            (dt.date(2026, 11, 2), True),  # Nov 1 2026 is a Sunday
            (dt.date(2026, 11, 1), False),
            (dt.date(2026, 8, 3), True),  # Aug 1 2026 is a Saturday
        ],
    )
    def test_first_session(self, day: dt.date, expected: bool) -> None:
        assert is_month_start(day) is expected
        assert day_matches(Days.MONTH_START, day) is expected

    @given(st.dates(dt.date(2025, 1, 1), dt.date(2027, 6, 30)))  # inside the XNYS calendar span
    def test_exactly_one_per_month(self, d: dt.date) -> None:
        first = d.replace(day=1)
        days = [first + dt.timedelta(days=i) for i in range(10)]
        assert sum(1 for x in days if x.month == first.month and is_month_start(x)) == 1

    def test_next_slot_spans_a_month(self) -> None:
        spec = JobSpec.model_validate({"schedule": ["06:00"], "days": "month_start"})
        assert next_slot(spec, et(2026, 10, 1, 7, 0)) == et(2026, 11, 2, 6, 0)


MOMENTUM_YAML = """
    sources:
      universe.momentum:
        schedule: ["06:00"]
        days: {days}
        ttl: 3h
        catch_up: {{days: trading, until_written: "universe_tier:momentum"}}
        context: {{ttl: {ttl}, supersede: latest}}
        writes: [universe_tier]
    personas: {{}}
"""


def _cfg(days: str = "month_start", ttl: str = "35d") -> RoutinesConfig:
    return RoutinesConfig.model_validate(
        yaml.safe_load(textwrap.dedent(MOMENTUM_YAML.format(days=days, ttl=ttl)))
    )


class Writer:
    def __init__(self) -> None:
        self.calls: list[dt.datetime] = []
        self.fail = False

    def __call__(self, ctx: JobContext) -> JobResult:
        self.calls.append(ctx.now)
        if self.fail:
            raise RuntimeError("source down")
        ctx.write("universe_tier", "momentum", build_payload(_fetch(), now=ctx.now))
        return JobResult(summary="ok")


def _disp(conn: sqlite3.Connection, w: Writer, cfg: RoutinesConfig | None = None) -> Dispatcher:
    return Dispatcher(
        conn,
        cfg or _cfg(),
        handlers={"universe.momentum": w},
        notifier=RecordingNotifier(),
        is_halted=lambda: False,
    )


def _tick(d: Dispatcher, at: dt.datetime) -> list[str]:
    rep = d.tick(at, since=at - dt.timedelta(minutes=5))
    return [o.status for o in rep.outcomes if o.job == "universe.momentum"]


class TestCatchUp:
    def test_first_deploy_runs_next_session_then_stops(self, conn: sqlite3.Connection) -> None:
        w = Writer()
        d = _disp(conn, w)
        assert _tick(d, et(2026, 10, 3, 6, 0)) == []  # Saturday: not a trading day
        assert _tick(d, et(2026, 10, 5, 6, 0)) == ["ok"]  # Mon: no entry since Oct 1 -> run
        assert _tick(d, et(2026, 10, 6, 6, 0)) == []  # written since the month-start slot
        assert _tick(d, et(2026, 11, 2, 6, 0)) == ["ok"]  # next month start (regular)
        assert len(w.calls) == 2
        row = conn.execute(
            "SELECT summary FROM routine_runs WHERE job='universe.momentum' ORDER BY rowid"
        ).fetchall()
        assert "catch-up: no universe_tier:momentum" in row[0][0]
        assert row[1][0] == "ok"

    def test_failed_month_start_retries_until_success(self, conn: sqlite3.Connection) -> None:
        w = Writer()
        d = _disp(conn, w)
        assert _tick(d, et(2026, 10, 1, 6, 0)) == ["ok"]
        w.fail = True
        assert _tick(d, et(2026, 11, 2, 6, 0)) == ["failed"]
        assert _tick(d, et(2026, 11, 3, 6, 0)) == ["failed"]  # Oct entry predates Nov 2
        # the October entry stays valid through the failures (35d TTL)
        snap = ContextStore(conn).query(as_of=et(2026, 11, 3, 7, 0), kinds=["universe_tier"])
        assert len(snap) == 1 and snap[0].created_at.date() == dt.date(2026, 10, 1)
        w.fail = False
        assert _tick(d, et(2026, 11, 4, 6, 0)) == ["ok"]
        assert _tick(d, et(2026, 11, 5, 6, 0)) == []

    def test_catch_up_window_and_outside_slots(self, conn: sqlite3.Connection) -> None:
        d = _disp(conn, Writer())
        assert d.plan(et(2026, 10, 5, 12, 0), since=et(2026, 10, 5, 11, 55)) == []  # past 3h
        assert [x.job for x in d.plan(et(2026, 10, 5, 8, 0), since=et(2026, 10, 5, 5, 0))] == [
            "universe.momentum"
        ]

    def test_weekly_is_a_yaml_edit(self, conn: sqlite3.Connection) -> None:
        cfg = _cfg(days="[mon]", ttl="8d")
        _kind, spec = cfg.jobs()["universe.momentum"]
        assert (
            spec.days_label == "mon"
            and str(cfg.context_policy("universe_tier", "universe.momentum").ttl) == "8d"
        )
        w = Writer()
        d = _disp(conn, w, cfg)
        assert _tick(d, et(2026, 10, 5, 6, 0)) == ["ok"]  # Monday: regular slot
        assert _tick(d, et(2026, 10, 6, 6, 0)) == []
        assert _tick(d, et(2026, 10, 12, 6, 0)) == ["ok"]

    def test_catch_up_slots_unit(self) -> None:
        spec = _cfg().jobs()["universe.momentum"][1]
        slots = catch_up_slots(spec, et(2026, 9, 30, 0, 0), et(2026, 10, 6, 7, 0))
        assert slots == [
            et(2026, 9, 30, 6, 0),
            et(2026, 10, 2, 6, 0),
            et(2026, 10, 5, 6, 0),
            et(2026, 10, 6, 6, 0),
        ]
        plain = JobSpec.model_validate({"schedule": ["06:00"]})
        assert catch_up_slots(plain, et(2026, 9, 30), et(2026, 10, 6)) == []

    def test_validation(self) -> None:
        with pytest.raises(ValueError, match="<kind>:<subject>"):
            CatchUp(until_written="universe_tier")
        with pytest.raises(ValueError, match="unknown context kind"):
            CatchUp(until_written="nope:x")
        with pytest.raises(ValueError, match="only valid with schedule"):
            JobSpec.model_validate({"every": "5m", "catch_up": {"until_written": "note:x"}})
        assert CatchUp(
            until_written="universe_tier:momentum", lookback="10d"
        ).lookback == dt.timedelta(days=10)
        weekly = {"until_written": "universe_tier:momentum", "days": ["mon"]}
        assert CatchUp.model_validate(weekly).target == (
            "universe_tier",
            "momentum",
        )

    def test_halted_persona_catch_up_is_skipped(self, conn: sqlite3.Connection) -> None:
        cfg = RoutinesConfig.model_validate(
            yaml.safe_load(
                textwrap.dedent(
                    """
                    sources: {}
                    personas:
                      p: {schedule: ["06:00"], days: month_start, ttl: 3h,
                          catch_up: {until_written: "universe_tier:momentum"}}
                    """
                )
            )
        )
        d = Dispatcher(conn, cfg, handlers={}, notifier=RecordingNotifier(), is_halted=lambda: True)
        plan = d.plan(et(2026, 10, 5, 6, 0), since=et(2026, 10, 5, 5, 55))
        assert [x.action for x in plan] == ["skip-halted"]


# -- shipped config + handler -----------------------------------------------------


def test_shipped_config() -> None:
    r = load_routines()
    kind, spec = r.jobs()["universe.momentum"]
    assert kind.value == "source" and spec.days is Days.MONTH_START
    assert [t.strftime("%H:%M") for t in spec.schedule] == ["06:00"]
    assert spec.catch_up is not None and spec.catch_up.target == ("universe_tier", "momentum")
    assert str(r.context_policy("universe_tier", "universe.momentum").ttl) == "35d"
    assert spec.options["source_order"] == ["stockanalysis", "schwab"]
    assert spec.options["size"] == 25
    assert "universe.momentum" in BUILTIN_HANDLERS
    u = load_universe_config()
    assert u.momentum.share_class_aliases == {"GOOG": "GOOGL"} and u.momentum.min_rows == 20


def _handler_disp(conn: sqlite3.Connection, notifier: RecordingNotifier) -> Dispatcher:
    from arc.routines.handlers import universe_momentum_source

    return Dispatcher(
        conn,
        load_routines(),
        handlers={"universe.momentum": universe_momentum_source},
        notifier=notifier,
        is_halted=lambda: False,
    )


@pytest.fixture
def pages(monkeypatch: pytest.MonkeyPatch) -> dict[str, bytes | Exception]:
    store: dict[str, bytes | Exception] = {SA_URL: SA_HTML.encode(), SW_URL: SW_HTML.encode()}
    import arc.universe.momentum as mom

    monkeypatch.setattr(mom, "_default_get", lambda cfg, ua: _getter(store))
    monkeypatch.setattr("arc.universe.load_symbol_master", lambda *a, **k: None)
    return store


def test_handler_end_to_end(conn: sqlite3.Connection, pages: dict[str, bytes | Exception]) -> None:
    notifier = RecordingNotifier()
    d = _handler_disp(conn, notifier)
    [out] = d.run_job("universe.momentum", NOW, reason="manual", now=NOW)
    assert out.status == "ok", out.summary
    assert out.metrics["names"] == 24 and out.metrics["source"] == "stockanalysis"
    assert out.metrics["stale"] is False and out.metrics["partial"] is True
    assert any("Momentum tier: first list · 24 names · as of Oct 2" in t for _, t in notifier.posts)
    entry = ContextStore(conn).query(as_of=NOW, kinds=["universe_tier"])[0]
    assert entry.subject == "momentum" and entry.schema_version == 2
    assert entry.expires_at is not None and (entry.expires_at - NOW).days == 35
    active = ContextStore(conn).query(as_of=NOW, kinds=["active_universe"])[0]
    assert active.expires_at is not None and (active.expires_at - NOW) < dt.timedelta(days=1)
    # run manifest (D27): the page's URL, digest and as-of
    manifest = json.loads(
        conn.execute(
            "SELECT payload FROM run_manifests WHERE run_id = ?", (out.run_id,)
        ).fetchone()[0]
    )
    spmo = next(i for i in manifest["external_inputs"] if i["name"] == "spmo_holdings")
    assert spmo["source"] == SA_URL and spmo["count"] == 25 and len(spmo["digest"]) == 64
    assert spmo["as_of"].startswith("2026-10-02")
    # merged + deduped against core: the 7 core names keep tier core
    active_list, _ = build_active(conn, _settings(), NOW)
    tiers = {m.ticker: m.tier for m in active_list.members}
    assert all(tiers[t] is Tier.CORE for t in CORE_OVERLAP)
    # D56: the tier is the feed's top 20 (universe_momentum_size_d56), cut before dedupe;
    # all 7 core overlaps are in that top 20
    assert sum(1 for t in tiers.values() if t is Tier.MOMENTUM) == 20 - len(CORE_OVERLAP)
    assert previous_members(conn) == [
        m.ticker
        for m in UniverseTierPayload.model_validate_json(
            conn.execute(
                "SELECT payload FROM context_entries WHERE kind='universe_tier'"
            ).fetchone()[0]
        ).members
    ]


def test_handler_all_sources_fail_keeps_previous(
    conn: sqlite3.Connection, pages: dict[str, bytes | Exception]
) -> None:
    notifier = RecordingNotifier()
    d = _handler_disp(conn, notifier)
    assert d.run_job("universe.momentum", NOW, reason="manual", now=NOW)[0].status == "ok"
    pages[SA_URL] = requests.ConnectionError("down")
    pages[SW_URL] = requests.ConnectionError("down")
    later = NOW + dt.timedelta(days=30)
    [out] = d.run_job("universe.momentum", later, reason="manual", now=later)
    assert out.status == "failed" and "every momentum source failed" in out.summary
    assert any("FAILED" in t for _, t in notifier.posts)
    snap = ContextStore(conn).query(as_of=later, kinds=["universe_tier"])
    assert len(snap) == 1 and snap[0].created_at == NOW  # previous entry still valid
    assert (
        ContextStore(conn).query(as_of=NOW + dt.timedelta(days=36), kinds=["universe_tier"]) == []
    )


def test_handler_diff_on_second_run(
    conn: sqlite3.Connection, pages: dict[str, bytes | Exception]
) -> None:
    d = _handler_disp(conn, RecordingNotifier())
    d.run_job("universe.momentum", NOW, reason="manual", now=NOW)
    pages[SA_URL] = requests.ConnectionError("down")  # fallback list: 19 names
    later = NOW + dt.timedelta(days=28)
    [out] = d.run_job("universe.momentum", later, reason="manual", now=later)
    assert out.status == "ok" and out.metrics["source"] == "schwab"
    assert out.metrics["added"] == [] and "LITE" in out.metrics["removed"]
    assert "fallback schwab" in out.summary and "\u2212LITE" in out.summary


# -- monitoring: coverage:universe.momentum ---------------------------------------


def _write_momentum(conn: sqlite3.Connection, as_of: dt.date | None, at: dt.datetime) -> None:
    f = _fetch()
    f.as_of = as_of
    ContextStore(conn).write(
        kind="universe_tier",
        subject="momentum",
        payload=build_payload(f, now=at),
        produced_by="test",
        ttl="35d",
        valid_from=at,
        now=at,
    )
    conn.commit()


STALE = 40


def _mc(conn: sqlite3.Connection, r: RoutinesConfig, at: dt.datetime) -> checks.CheckResult:
    return checks.momentum_coverage(conn, r, at, stale_after_days=STALE)


class _Ops:
    def __init__(self) -> None:
        self.posts: list[str] = []

    def post(self, text: str, thread_ts: str | None = None) -> str | None:
        self.posts.append(text)
        return "ts"


class TestMomentumCoverage:
    def test_not_judged(self, conn: sqlite3.Connection) -> None:
        r = load_routines()
        assert _mc(conn, r, NOW).severity == "ok"
        off = _cfg().model_copy(update={"sources": {}})
        assert "disabled" in _mc(conn, off, NOW).summary

    def test_fresh_stale_and_resolve(self, conn: sqlite3.Connection) -> None:
        r = load_routines()
        _write_momentum(conn, dt.date(2026, 9, 1), NOW)
        assert _mc(conn, r, et(2026, 10, 11)).severity == "ok"
        stale_at = et(2026, 10, 12, 9, 0)
        res = _mc(conn, r, stale_at)
        assert res.severity == "failed"
        [f] = res.findings
        assert f.key == "coverage:universe.momentum" and "41 d old" in f.message
        assert not alerts._slot_coverage(f.key)  # its own cause: never folded into an incident
        n = _Ops()
        out = alerts.apply(conn, [res], now=stale_at, correlation={}, notifier=n)
        assert [a.key for a in out.opened] == ["coverage:universe.momentum"]
        assert "momentum tier stale" in n.posts[0]
        _write_momentum(conn, dt.date(2026, 10, 2), stale_at + dt.timedelta(hours=1))
        ok = checks.momentum_coverage(
            conn, r, stale_at + dt.timedelta(hours=2), stale_after_days=40
        )
        assert ok.severity == "ok"
        out = alerts.apply(
            conn, [ok], now=stale_at + dt.timedelta(hours=2), correlation={}, notifier=n
        )
        assert [a.key for a in out.resolved] == ["coverage:universe.momentum"]
        assert "momentum tier source fresh again" in n.posts[-1]
        _write_momentum(conn, None, stale_at + dt.timedelta(hours=3))
        none = checks.momentum_coverage(
            conn, r, stale_at + dt.timedelta(hours=4), stale_after_days=40
        )
        assert none.severity == "failed" and "no as-of" in none.findings[0].message


# -- CLI -------------------------------------------------------------------------


def test_cli_dry_run_and_job(
    tmp_path: Path,
    pages: dict[str, bytes | Exception],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from arc.cli import main

    monkeypatch.chdir(tmp_path)  # lock dir under data/ stays in tmp
    assert main(["universe", "momentum", "--dry-run", "--json"]) == 0
    dry = json.loads(capsys.readouterr().out)
    assert dry["written"] is False and dry["names"] == 24 and dry["as_of"] == "2026-10-02"
    assert main(["universe", "momentum", "--dry-run", "--source", "schwab"]) == 0
    assert "source: schwab" in capsys.readouterr().out
    assert main(["universe", "momentum", "--source", "schwab"]) == 2
    capsys.readouterr()
    db = str(tmp_path / "arc.db")
    assert main(["universe", "momentum", "--json", "--db", db, "--no-slack"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["written"] is True and out["run"]["status"] == "ok" and out["names"] == 24
    assert out["members"][2]["ticker"] == "GOOGL"
    pages[SA_URL] = pages[SW_URL] = requests.ConnectionError("down")
    assert main(["universe", "momentum", "--dry-run"]) == 1
    assert "every momentum source failed" in capsys.readouterr().out
    assert main(["universe", "momentum", "--db", db, "--no-slack"]) == 1
