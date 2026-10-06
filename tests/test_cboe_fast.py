"""E13.6 (D56): options_fast source — Cboe delayed index vols, chain snapshot, symbol_data."""

from __future__ import annotations

import datetime as dt
import json
import textwrap
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from arc.context.categories import KIND_CATEGORY, SourceCategory
from arc.context.kinds import (
    KINDS,
    ChainSnapshotPayload,
    ExchangeVolumePayload,
    IndexVolsPayload,
)
from arc.context.store import ContextStore
from arc.control.registry import (
    NEVER_TUNABLE_PATHS,
    NOT_EXPOSED_PATHS,
    REGISTRY,
    TunableError,
    lookup,
)
from arc.ingest import cboe_fast as cf
from arc.routines.config import load_routines
from arc.routines.handlers import BUILTIN_HANDLERS, JobContext, options_fast_source
from arc.store.db import connect
from arc.store.migrate import migrate
from arc.utils.calendar import ET

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable, Iterator

REPO = Path(__file__).resolve().parents[1]
FIX = REPO / "arc" / "ingest" / "fixtures" / "cboe"
TODAY = dt.date(2026, 10, 6)
NOW = dt.datetime(2026, 10, 6, 10, 0, tzinfo=ET)
WINDOW = (30, 60)


def et(*a: int) -> dt.datetime:
    return dt.datetime(*a, tzinfo=ET)


def fixture_get(url: str) -> bytes:
    """Serve the 2026-10-06 live captures by URL (any chain URL -> the NVDA capture)."""
    if "/quotes/_" in url:
        return (FIX / url.rsplit("/", 1)[1]).read_bytes()
    if "/delayed_quotes/options/" in url:
        return (FIX / "NVDA.json").read_bytes()
    if "symbol_data" in url:
        return (FIX / "symbol_data_opt.csv").read_bytes()
    raise AssertionError(url)


def _quote(price: float, ts: str = "2026-10-06T10:39:16") -> bytes:
    return json.dumps({"data": {"current_price": price, "last_trade_time": ts}}).encode()


def _vols(**px: float) -> Callable[[str], bytes]:
    def get(url: str) -> bytes:
        sym = url.rsplit("_", 1)[1].removesuffix(".json")
        if sym not in px:
            raise ConnectionError(sym)
        return _quote(px[sym])

    return get


# ---------------------------------------------------------------------------
# kinds / categories / config
# ---------------------------------------------------------------------------


class TestWiring:
    def test_kinds_and_categories(self) -> None:
        for kind in ("index_vols", "chain_snapshot", "exchange_volume"):
            assert kind in KINDS
            assert KIND_CATEGORY[kind] is SourceCategory.OPTIONS_FAST
            assert (REPO / "schemas" / "context" / f"{kind}.v1.json").is_file()

    def test_routines_yaml(self) -> None:
        cfg = load_routines(REPO / "config" / "routines.yaml")
        spec = cfg.sources["options_fast"]
        assert spec.every == dt.timedelta(minutes=30)
        assert spec.lane.value == "background"
        assert cfg.source_category("options_fast") is SourceCategory.OPTIONS_FAST
        assert spec.options["max_tickers"] == 50
        assert spec.options["strikes"] == 3
        assert spec.options["symbol_data_markets"] == ["opt"]
        assert spec.options["tickers"] == "active_list"
        assert "options_fast" in BUILTIN_HANDLERS
        for kind in ("index_vols", "chain_snapshot", "exchange_volume"):
            assert cfg.context_ttl[kind].ttl.duration == dt.timedelta(hours=1)
        assert cfg.options_fast.max_csv_bytes == 20_000_000
        assert cfg.options_fast.vix_flags.vix_gt_25 == 25
        assert cfg.category_spec(SourceCategory.OPTIONS_FAST).max_age.duration == dt.timedelta(
            minutes=30
        )

    def test_registry_classification(self) -> None:
        t = REGISTRY["sources.options_fast.max_tickers"]
        assert (t.min, t.max) == (1, 60)
        for key in (
            "sources.options_fast.symbol_data_markets",
            "sources.options_fast.strikes",
            "options_fast.vix_flags.vix_gt_25",
            "options_fast.vix_flags.vix_gt_35",
        ):
            assert key in NOT_EXPOSED_PATHS
        assert "options_fast.max_csv_bytes" in NEVER_TUNABLE_PATHS
        with pytest.raises(TunableError, match="never tunable"):
            lookup("options_fast.max_csv_bytes")

    def test_max_tickers_read_and_write(self) -> None:
        from arc.control.registry import read_raw, write_raw

        raw = {"sources": {"options_fast": {"max_tickers": 50, "every": "30m"}}}
        t = REGISTRY["sources.options_fast.max_tickers"]
        assert read_raw(t, raw) == 50
        assert write_raw(t, 40, raw) == [(("sources", "options_fast", "max_tickers"), 40)]


# ---------------------------------------------------------------------------
# 1. index vols
# ---------------------------------------------------------------------------


class TestIndexVols:
    def test_parse_fixture(self) -> None:
        v = cf.fetch_index_vols(fixture_get, NOW)
        assert [q.symbol for q in v.quotes] == ["VIX1D", "VIX9D", "VIX", "VIX3M", "VVIX", "VXN"]
        assert v.value("VIX") == 15.4
        assert v.value("VXN") == 21.94
        assert v.ratio_9d_30d == pytest.approx(12.78 / 15.4, abs=1e-4)
        assert v.ratio_30d_3m == pytest.approx(15.4 / 17.88, abs=1e-4)
        assert v.flags == []
        assert v.quotes[2].as_of == "2026-10-06T10:39:16-04:00"

    @pytest.mark.parametrize(
        ("px", "flags"),
        [
            ({"VIX": 20, "VIX9D": 22, "VIX3M": 19}, ["9d_over_30d", "backwardation_30d_3m"]),
            ({"VIX": 26, "VIX9D": 20, "VIX3M": 27}, ["vix_gt_25"]),
            (
                {"VIX": 40, "VIX9D": 45, "VIX3M": 30},
                ["9d_over_30d", "backwardation_30d_3m", "vix_gt_25", "vix_gt_35"],
            ),
            ({"VIX": 20, "VIX9D": 20, "VIX3M": 20}, []),  # exactly 1.0 raises nothing
            ({"VIX": 25}, []),  # threshold is strict, missing ratios -> None
        ],
    )
    def test_flags(self, px: dict[str, float], flags: list[str]) -> None:
        errors: dict[str, str] = {}
        v = cf.fetch_index_vols(_vols(**px), NOW, errors=errors)
        assert v.flags == flags
        assert set(errors) == set(cf.INDEX_VOL_SYMBOLS) - set(px)

    def test_vix_missing_raises(self) -> None:
        with pytest.raises(cf.CboeQuoteError, match="VIX"):
            cf.fetch_index_vols(_vols(VIX9D=12), NOW)

    def test_bad_quote_bodies(self) -> None:
        assert cf.parse_cboe_quote(b"<html>") is None
        assert cf.parse_cboe_quote(b'{"data": {"current_price": 0}}') is None
        assert cf.parse_cboe_quote(b'{"data": {"current_price": true}}') is None
        assert cf.parse_cboe_quote(b'{"data": {"current_price": 15}}') == (15.0, None)

    def test_as_of_fallbacks(self) -> None:
        assert cf._as_of(None, NOW) == "2026-10-06T10:00:00-04:00"  # noqa: SLF001
        assert cf._as_of("garbage", NOW) == "2026-10-06T10:00:00-04:00"  # noqa: SLF001
        assert cf._as_of("2026-10-06T14:39:16+00:00", NOW) == "2026-10-06T10:39:16-04:00"  # noqa: SLF001

    def test_flips(self) -> None:
        calm = cf.fetch_index_vols(_vols(VIX=20, VIX9D=18, VIX3M=22), NOW)

        def at(ts: str, **px: float) -> IndexVolsPayload:
            def get(url: str) -> bytes:
                sym = url.rsplit("_", 1)[1].removesuffix(".json")
                return _quote(px[sym], ts)

            return cf.fetch_index_vols(get, NOW)

        stress = at("2026-10-06T11:00:00", VIX=26, VIX9D=28, VIX3M=27, VIX1D=1, VVIX=1, VXN=1)
        assert cf.detect_flips(None, stress) == []
        assert cf.detect_flips(calm, stress) == ["+9d_over_30d", "+vix_gt_25"]
        back = at("2026-10-06T11:30:00", VIX=20, VIX9D=18, VIX3M=22, VIX1D=1, VVIX=1, VXN=1)
        assert cf.detect_flips(stress, back) == ["-9d_over_30d", "-vix_gt_25"]
        assert cf.detect_flips(stress, stress) == []  # same quote time never flips
        line = cf.vix_line(stress, cf.detect_flips(calm, stress))
        assert "⚑ NEW VIX9D > VIX (front-end stress); NEW VIX > 25" in line
        assert "cleared VIX9D > VIX" in cf.vix_line(back, ["-9d_over_30d"])


# ---------------------------------------------------------------------------
# 2. chain snapshot
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def nvda() -> cf.DelayedChain:
    return cf.parse_delayed_chain((FIX / "NVDA.json").read_bytes())


class TestChainSnapshot:
    def test_parse(self, nvda: cf.DelayedChain) -> None:
        assert nvda.ticker == "NVDA"
        assert nvda.spot == 242.225
        assert len(nvda.rows) == 414 and nvda.unparsed == 0
        r = next(r for r in nvda.rows if r.occ_symbol == "NVDA261106C00240000")
        assert (r.option_type, r.strike, r.expiry) == ("call", 240.0, dt.date(2026, 11, 6))

    def test_snapshot_picks_expiry_and_strikes(self, nvda: cf.DelayedChain) -> None:
        s = cf.snapshot_ticker(
            nvda, ticker="NVDA", today=TODAY, dte_window=WINDOW, fetched_at=NOW.isoformat()
        )
        # 2026-11-06 is 31 DTE (first expiry in 30-60); 10-30 is 24 DTE, out of window.
        assert s.expiry == "2026-11-06"
        assert [(b.strike, b.option_type) for b in s.book] == [
            (235.0, "call"),
            (235.0, "put"),
            (240.0, "call"),
            (240.0, "put"),
            (245.0, "call"),
            (245.0, "put"),
        ]
        atm = [b for b in s.book if b.strike == 240.0]
        assert atm[0].spread_pct == pytest.approx((9.9 - 9.8) / 9.85, abs=1e-4)
        assert s.atm_spread_pct == pytest.approx(
            ((9.9 - 9.8) / 9.85 + (6.8 - 6.65) / 6.725) / 2, abs=1e-4
        )
        assert s.atm_oi == 3679 + 556
        assert (s.call_volume_td, s.put_volume_td) == (517_223, 251_868)
        assert s.put_call_volume == pytest.approx(251_868 / 517_223, abs=1e-3)
        ChainSnapshotPayload.model_validate(s.model_dump())

    def test_strikes_option_and_window(self, nvda: cf.DelayedChain) -> None:
        s = cf.snapshot_ticker(
            nvda, ticker="NVDA", today=TODAY, dte_window=(7, 14), fetched_at="x", strikes=1
        )
        assert s.expiry == "2026-10-14" and {b.strike for b in s.book} == {242.5}  # spot 242.225
        with pytest.raises(ValueError, match="strikes"):
            cf.snapshot_ticker(
                nvda, ticker="NVDA", today=TODAY, dte_window=WINDOW, fetched_at="x", strikes=4
            )

    def test_skips(self, nvda: cf.DelayedChain) -> None:
        with pytest.raises(cf.SnapshotSkipError) as e:
            cf.snapshot_ticker(
                nvda, ticker="NVDA", today=TODAY, dte_window=(90, 120), fetched_at="x"
            )
        assert e.value.reason == "no_expiry_in_window"
        no_spot = cf.DelayedChain(ticker="X", spot=None, timestamp=None, rows=nvda.rows)
        with pytest.raises(cf.SnapshotSkipError) as e:
            cf.snapshot_ticker(no_spot, ticker="X", today=TODAY, dte_window=WINDOW, fetched_at="x")
        assert e.value.reason == "spot_missing"

    def test_parse_edge_rows(self) -> None:
        body = json.dumps(
            {
                "data": {
                    "symbol": "ABC",
                    "current_price": 10,
                    "options": [
                        {
                            "option": "ABC261120C00010000",
                            "bid": 0,
                            "ask": 0.5,
                            "volume": None,
                            "iv": 0,
                        },
                        {"option": "nonsense"},
                        {"option": "ABC261340C00010000"},  # month 13
                        "not a dict",
                    ],
                }
            }
        )
        ch = cf.parse_delayed_chain(body)
        assert ch.unparsed == 3 and len(ch.rows) == 1
        r = ch.rows[0]
        assert (r.volume, r.iv, cf.spread_pct(r.bid, r.ask)) == (0, None, None)
        with pytest.raises(ValueError, match="no data.options"):
            cf.parse_delayed_chain(b'{"data": {}}')

    @given(
        bid=st.floats(min_value=0.01, max_value=500, allow_nan=False),
        width=st.floats(min_value=0, max_value=50, allow_nan=False),
    )
    def test_spread_pct_bounds(self, bid: float, width: float) -> None:
        s = cf.spread_pct(bid, bid + width)
        assert s is not None and 0 <= s < 2  # (a-b)/mid is always < 2
        assert cf.spread_pct(bid, bid - 0.01) is None


# ---------------------------------------------------------------------------
# 3. symbol_data
# ---------------------------------------------------------------------------


class TestSymbolData:
    def test_aggregate_excerpt(self) -> None:
        rows = cf.parse_symbol_data_csv((FIX / "symbol_data_opt.csv").read_text())
        assert len(rows) == 200
        agg = cf.aggregate_by_underlying(rows, "opt")
        top = [(r.underlying, r.volume, r.matched, r.routed, r.contracts) for r in agg[:4]]
        assert top == [
            ("SPY", 32_834, 31_844, 990, 32),
            ("QQQ", 19_322, 13_592, 5_730, 15),
            ("NVDA", 9_259, 9_195, 64, 18),
            ("IWM", 7_352, 7_349, 3, 9),
        ]
        assert sum(r.contracts for r in agg) == 200
        assert {"ETHA1"} <= {r.underlying for r in agg}  # adjusted roots stay distinct

    def test_select_active_plus_top_others(self) -> None:
        agg = cf.aggregate_by_underlying(
            cf.parse_symbol_data_csv((FIX / "symbol_data_opt.csv").read_text()), "opt"
        )
        sel = cf.select_exchange_rows(agg, ["nvda", "AAPL", "ZZZZ"])
        names = [r.underlying for r in sel]
        assert names[0] == "NVDA" and "ZZZZ" not in names
        assert len([n for n in names if n not in {"NVDA", "AAPL"}]) == 10
        assert names[-10:][:3] == ["SPY", "QQQ", "IWM"]
        assert len(cf.select_exchange_rows(agg, [], top_others=500, cap=60)) == 60

    def test_partial_columns_and_bad_rows(self) -> None:
        text = textwrap.dedent(
            """\
            Symbol,Volume,Matched
            AAA,10,5
            AAA,7,
            ,4,4
            BBB,x,1
            """
        )
        agg = cf.aggregate_by_underlying(cf.parse_symbol_data_csv(text), "cone")
        assert [(r.underlying, r.volume, r.matched, r.routed) for r in agg] == [
            ("AAA", 17, None, None)
        ]
        with pytest.raises(ValueError, match="symbol_data"):
            cf.parse_symbol_data_csv("a,b\n1,2\n")


# ---------------------------------------------------------------------------
# tape
# ---------------------------------------------------------------------------

EXPECTED_TAPE = (
    "VIX complex (Cboe ~15-min delayed, 10:39 ET): VIX1D 7.21 · VIX9D 12.78 · VIX 15.40"
    " · VIX3M 17.88 · VVIX 83.87 · VXN 21.94 · 9D/30D 0.83 · 30D/3M 0.86\n"
    "NVDA 242.22 · vol 769k P/C 0.49 · ATM 11-06 spread 1.6% · OI 4,235 · Cboe book 9,259"
)


def _parts() -> tuple[IndexVolsPayload, ChainSnapshotPayload, ExchangeVolumePayload]:
    v = cf.fetch_index_vols(fixture_get, NOW)
    s = cf.snapshot_ticker(
        cf.parse_delayed_chain((FIX / "NVDA.json").read_bytes()),
        ticker="NVDA",
        today=TODAY,
        dte_window=WINDOW,
        fetched_at=NOW.isoformat(),
    )
    agg = cf.aggregate_by_underlying(
        cf.parse_symbol_data_csv((FIX / "symbol_data_opt.csv").read_text()), "opt"
    )
    ev = ExchangeVolumePayload(
        fetched_at=NOW.isoformat(),
        rows=cf.select_exchange_rows(agg, ["NVDA"]),
        total_rows_parsed=200,
    )
    return v, s, ev


class TestTape:
    def test_snapshot(self) -> None:
        v, s, ev = _parts()
        tape = cf.build_tape(v, [s], ev, None, now=NOW, max_age=dt.timedelta(minutes=30))
        assert tape == EXPECTED_TAPE
        assert tape == cf.build_tape(v, [s], ev, None, now=NOW, max_age=dt.timedelta(minutes=30))

    def test_stale(self) -> None:
        v, s, ev = _parts()
        late = NOW + dt.timedelta(minutes=31)
        assert cf.build_tape(v, [s], ev, None, now=late, max_age=dt.timedelta(minutes=30)) == (
            "Options tape: no fresh options tape (Cboe options_fast older than 30m)."
        )
        assert cf.build_tape(None, [], None, None, now=NOW, max_age=dt.timedelta(minutes=30))
        fresh_snap = s.model_copy(update={"fetched_at": late.isoformat()})
        tape = cf.build_tape(
            v, [fresh_snap], None, None, now=late, max_age=dt.timedelta(minutes=30)
        )
        assert tape.startswith("VIX complex: no fresh quote (older than 30m)")
        assert "Cboe book" not in tape  # no exchange volume -> no book column
        # a future-dated or unparseable fetched_at is never fresh
        assert not cf._fresh("nope", NOW, dt.timedelta(minutes=30))  # noqa: SLF001

    def test_char_cap(self) -> None:
        v, s, _ = _parts()
        snaps = [s.model_copy(update={"ticker": f"T{i:02d}"}) for i in range(60)]
        tape = cf.build_tape(v, snaps, None, None, now=NOW, max_age=dt.timedelta(minutes=30))
        assert len(tape) <= cf.MAX_TAPE_CHARS
        assert tape.splitlines()[-1].startswith("… +")
        assert tape.splitlines()[1].startswith("T00 ")  # volume tie -> by name

    def test_small_numbers(self) -> None:
        assert (cf._k(None), cf._k(950), cf._k(12_345), cf._k(2_500_000)) == (  # noqa: SLF001
            "n/a",
            "950",
            "12k",
            "2.5M",
        )


# ---------------------------------------------------------------------------
# handler
# ---------------------------------------------------------------------------


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    c = connect(tmp_path / "arc.db")
    migrate(c)
    yield c
    c.close()


def _ctx(conn: sqlite3.Connection, **options: Any) -> JobContext:
    from arc.config import ArcSettings

    routines = load_routines(REPO / "config" / "routines.yaml")
    kind, spec = routines.step("options_fast")
    if options:
        spec = type(spec).model_validate({**spec.model_dump(), **options})
    return JobContext(
        job="options_fast",
        kind=kind,
        spec=spec,
        run_id="run-1",
        chain_run_id=None,
        scheduled_for=NOW,
        now=NOW,
        conn=conn,
        snapshot=ContextStore(conn).snapshot(NOW, kinds=[]),
        routines=routines,
        settings_factory=lambda: ArcSettings(_env_file=None),  # type: ignore[call-arg]
    )


def _latest(conn: sqlite3.Connection, kind: str) -> list[Any]:
    return ContextStore(conn).query(as_of=NOW, kinds=[kind])


class TestHandler:
    def test_all_parts(self, conn: sqlite3.Connection) -> None:
        sleeps: list[float] = []
        res = options_fast_source(
            _ctx(conn, tickers=["NVDA", "AMD"]), get=fixture_get, sleep=sleeps.append
        )
        m = res.metrics
        assert m["failed_parts"] == [] and m["parts_ok"] == 3
        assert (m["tickers_requested"], m["tickers_fetched"]) == (2, 2)
        assert sleeps == [0.2]  # serial pacing between tickers, none before the first
        assert res.summary.startswith("VIX 15.40 · 2/2 chains · 200 exchange rows")
        assert [e.subject for e in _latest(conn, "chain_snapshot")] == ["NVDA", "AMD"]
        ev = _latest(conn, "exchange_volume")[0].payload
        assert ev["rows"][0]["underlying"] == "NVDA" and ev["total_rows_parsed"] == 200
        assert _latest(conn, "index_vols")[0].subject == "market"
        assert cf.tape_from_store(conn, NOW, dt.timedelta(minutes=30)).startswith(
            "VIX complex (Cboe"
        )

    def test_part_isolation(self, conn: sqlite3.Connection) -> None:
        def get(url: str) -> bytes:
            if "symbol_data" in url or "_VIX.json" in url:
                raise ConnectionError("down")
            return fixture_get(url)

        res = options_fast_source(_ctx(conn, tickers=["NVDA"]), get=get, sleep=lambda _: None)
        assert res.metrics["failed_parts"] == ["exchange_volume", "index_vols"]
        assert res.metrics["parts_ok"] == 1
        assert "failed: exchange_volume, index_vols" in res.summary
        assert _latest(conn, "index_vols") == [] and len(_latest(conn, "chain_snapshot")) == 1

    def test_all_parts_fail(self, conn: sqlite3.Connection) -> None:
        def down(url: str) -> bytes:
            raise ConnectionError(url)

        with pytest.raises(RuntimeError, match="all parts failed"):
            options_fast_source(_ctx(conn, tickers=["NVDA"]), get=down, sleep=lambda _: None)

    def test_ticker_skips_and_cap(self, conn: sqlite3.Connection) -> None:
        names = [f"T{i:02d}" for i in range(70)]
        seen: list[str] = []

        def get(url: str) -> bytes:
            if "/delayed_quotes/options/" in url:
                seen.append(url.rsplit("/", 1)[1])
                if url.endswith("T01.json"):
                    raise ConnectionError("x")
            return fixture_get(url)

        res = options_fast_source(_ctx(conn, tickers=names), get=get, sleep=lambda _: None)
        assert len(seen) == 50  # max_tickers
        assert res.metrics["tickers_skipped"] == {"T01": "fetch_error:ConnectionError"}
        assert res.metrics["tickers_fetched"] == 49

    def test_no_expiry_skip_and_csv_guard(
        self, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from arc.config import ArcSettings

        monkeypatch.setattr(ArcSettings, "entry_dte_window", property(lambda _: (200, 300)))
        ctx = _ctx(conn, tickers=["NVDA"])

        def big(url: str) -> bytes:
            return b"x" * 20_000_001 if "symbol_data" in url else fixture_get(url)

        res = options_fast_source(ctx, get=big, sleep=lambda _: None)
        assert res.metrics["tickers_skipped"] == {"NVDA": "no_expiry_in_window"}
        assert res.metrics["failed_parts"] == ["chain_snapshot", "exchange_volume"]
        assert "max_csv_bytes" in res.metrics["exchange_market_errors"]["opt"]

    def test_active_list_scope(self, conn: sqlite3.Connection) -> None:
        from arc.routines import handlers

        ctx = _ctx(conn)
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("arc.universe.tiers.active_tickers", lambda *_: ["NVDA", "AMD"])
            mp.setattr("arc.universe.tiers.open_underlyings", lambda _: ["AMD", "zzop"])
            assert handlers._options_fast_tickers(ctx, 50) == ["NVDA", "AMD", "ZZOP"]  # noqa: SLF001
            assert handlers._options_fast_tickers(ctx, 1) == ["NVDA"]  # noqa: SLF001


def test_http_get_max_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    from arc.ingest import options_data

    class Resp:
        content = b"x" * 11

        def raise_for_status(self) -> None:
            return None

    monkeypatch.setattr(options_data.requests, "get", lambda *a, **k: Resp())
    assert options_data.http_get("u", "ua", max_bytes=11) == b"x" * 11
    with pytest.raises(ValueError, match="max_bytes 10"):
        options_data.http_get("u", "ua", max_bytes=10)


def test_cli_tape(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = tmp_path / "arc.db"
    c = connect(db)
    migrate(c)
    options_fast_source(_ctx(c, tickers=["NVDA"]), get=fixture_get, sleep=lambda _: None)
    c.close()
    assert cf.main(["--tape", "--db", str(db), "--now", NOW.isoformat()]) == 0
    out = capsys.readouterr().out
    assert "VIX complex (Cboe ~15-min delayed, 10:39 ET)" in out
    assert cf.main(["--tape", "--db", str(db)]) == 0  # --now defaults to the newest entry
    assert "NVDA 242.22" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        cf.main(["--db", str(db)])  # nothing to do
