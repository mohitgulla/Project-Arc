"""E13.5 (D56): options_slow source — Cboe daily options statistics + CFE VX curve."""

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

from arc.context.kinds import KINDS, OptionsDailyPayload, VxCurvePayload, VxPoint
from arc.context.store import ContextStore
from arc.control.registry import NOT_EXPOSED_PATHS, REGISTRY
from arc.ingest import cboe_daily
from arc.ingest.cboe_daily import (
    NotPublishedError,
    curve_shape,
    fetch_daily_options,
    fetch_vx_settlements,
    parse_daily_options,
    parse_vx_settlements,
    session_date,
    vx_curve,
)
from arc.ingest.options_data import parse_put_call
from arc.routines import handlers
from arc.routines.config import CatchUp, RoutinesConfig, load_routines
from arc.routines.dispatcher import Dispatcher
from arc.routines.handlers import (
    BUILTIN_HANDLERS,
    JobContext,
    JobResult,
    JobSkippedError,
    options_daily_source,
    vix_futures_source,
)
from arc.routines.heartbeat import RecordingNotifier
from arc.store.db import connect
from arc.store.migrate import migrate
from arc.utils.calendar import ET, completed_session

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable, Iterator

FIX = Path(__file__).resolve().parents[1] / "arc" / "ingest" / "fixtures" / "cboe"
DAILY_JSON = FIX / "2026-10-05_daily_options.json"
VX_CSV = FIX / "vx_settlement_2026-10-05.csv"
DAY = dt.date(2026, 10, 5)
REPO = Path(__file__).resolve().parents[1]


def et(*a: int) -> dt.datetime:
    return dt.datetime(*a, tzinfo=ET)


def _daily() -> dict[str, Any]:
    return json.loads(DAILY_JSON.read_text())


def _http_error(status: int) -> requests.HTTPError:
    resp = requests.Response()
    resp.status_code = status
    return requests.HTTPError(f"{status}", response=resp)


def _get_returning(body: bytes) -> Callable[[str, str], bytes]:
    def get(url: str, ua: str) -> bytes:
        return body

    return get


def _get_raising(status: int) -> Callable[[str, str], bytes]:
    def get(url: str, ua: str) -> bytes:
        raise _http_error(status)

    return get


# ---------------------------------------------------------------------------
# Daily options statistics
# ---------------------------------------------------------------------------


class TestParseDailyOptions:
    def test_six_ratios_with_volumes(self) -> None:
        p = parse_daily_options(_daily(), DAY, fetched_at=et(2026, 10, 5, 18, 30))
        assert p is not None
        assert [r.segment for r in p.ratios] == ["total", "index", "etp", "equity", "vix", "spx"]
        by = {r.segment: r for r in p.ratios}
        assert by["total"].ratio == 0.83
        assert (by["total"].call_volume, by["total"].put_volume) == (7720926, 6429431)
        assert by["equity"].ratio == 0.59
        assert by["spx"].ratio == 1.03
        assert (by["spx"].call_volume, by["spx"].put_volume) == (2684268, 2753568)
        assert by["vix"].ratio == 0.31
        assert p.as_of == "2026-10-05"
        assert p.fetched_at == "2026-10-05T18:30:00-04:00"
        assert p.url.endswith("/2026-10-05_daily_options")
        assert p.source == "cboe"

    def test_open_interest_per_product(self) -> None:
        p = parse_daily_options(_daily(), DAY, fetched_at=et(2026, 10, 5, 18, 30))
        assert p is not None
        oi = {o.product: o for o in p.open_interest}
        assert list(oi) == ["all", "index", "etp", "equity", "vix", "spx"]
        assert oi["equity"].call_oi == 257907011
        assert oi["equity"].put_oi == 161442487
        assert oi["equity"].total_oi == 419349498
        assert oi["equity"].volume == 4104793
        assert oi["index"].total_oi == 42674879
        for o in p.open_interest:
            assert o.call_oi + o.put_oi == o.total_oi

    def test_no_total_ratio_is_none(self) -> None:
        data = _daily()
        data["ratios"] = [r for r in data["ratios"] if "TOTAL" not in r["name"].upper()]
        assert parse_daily_options(data, DAY, fetched_at=et(2026, 10, 5, 18, 30)) is None

    def test_bad_values_skipped(self) -> None:
        data = {
            "ratios": [
                {"name": "TOTAL PUT/CALL RATIO", "value": "0.9"},
                {"name": "EQUITY PUT/CALL RATIO", "value": "n/a"},
                {"name": "INDEX PUT/CALL RATIO", "value": "-1"},
                {"name": "UNKNOWN RATIO", "value": "1.0"},
                "junk",
            ],
            "EQUITY OPTIONS": [{"name": "OPEN INTEREST", "call": "x", "put": 1, "total": 2}],
        }
        p = parse_daily_options(data, DAY, fetched_at=et(2026, 10, 5, 18, 30))
        assert p is not None
        assert [r.segment for r in p.ratios] == ["total"]
        assert p.ratios[0].call_volume is None
        assert p.open_interest == []

    def test_put_call_wrapper_matches(self) -> None:
        legacy = parse_put_call(_daily(), DAY)
        assert legacy is not None
        assert (legacy.total, legacy.equity, legacy.index, legacy.spx, legacy.vix) == (
            0.83,
            0.59,
            0.91,
            1.03,
            0.31,
        )
        assert legacy.as_of == "2026-10-05"


class TestFetchDailyOptions:
    now = et(2026, 10, 5, 18, 30)

    def test_ok(self) -> None:
        seen: list[str] = []

        def get(url: str, ua: str) -> bytes:
            seen.append(url)
            return DAILY_JSON.read_bytes()

        p = fetch_daily_options(DAY, get, now=self.now)
        assert seen == [
            "https://cdn.cboe.com/data/us/options/market_statistics/daily/2026-10-05_daily_options"
        ]
        assert isinstance(p, OptionsDailyPayload)

    @pytest.mark.parametrize("status", [403, 404])
    def test_missing_is_not_published(self, status: int) -> None:
        with pytest.raises(NotPublishedError, match="2026-10-05"):
            fetch_daily_options(DAY, _get_raising(status), now=self.now)

    def test_server_error_raises(self) -> None:
        with pytest.raises(requests.HTTPError):
            fetch_daily_options(DAY, _get_raising(500), now=self.now)

    def test_http_error_without_response_raises(self) -> None:
        def get(url: str, ua: str) -> bytes:
            raise requests.HTTPError("boom")

        with pytest.raises(requests.HTTPError):
            fetch_daily_options(DAY, get, now=self.now)

    def test_empty_is_not_published(self) -> None:
        with pytest.raises(NotPublishedError, match="empty"):
            fetch_daily_options(DAY, _get_returning(b"  "), now=self.now)

    def test_no_total_is_not_published(self) -> None:
        with pytest.raises(NotPublishedError, match="no total"):
            fetch_daily_options(DAY, _get_returning(b'{"ratios": []}'), now=self.now)

    def test_non_object_raises(self) -> None:
        with pytest.raises(ValueError, match="JSON object"):
            fetch_daily_options(DAY, _get_returning(b"[1, 2]"), now=self.now)


# ---------------------------------------------------------------------------
# VX settlement curve
# ---------------------------------------------------------------------------


class TestVxCurve:
    def test_monthlies_then_weeklies(self) -> None:
        pts = parse_vx_settlements(VX_CSV.read_text())
        monthly = [p for p in pts if not p.weekly]
        weekly = [p for p in pts if p.weekly]
        assert [p.symbol for p in monthly][:3] == ["VX/V6", "VX/X6", "VX/Z6"]
        assert monthly[-1].symbol == "VX/M7"
        assert len(monthly) == 9
        assert {p.symbol for p in weekly} == {"VX40/V6", "VX41/V6", "VX43/V6", "VX44/X6", "VX45/X6"}
        # Monthlies first, each group sorted by expiry.
        assert pts == monthly + weekly
        assert [p.expiry for p in monthly] == sorted(p.expiry for p in monthly)
        assert [p.expiry for p in weekly] == sorted(p.expiry for p in weekly)
        # VXM (mini) and other CFE products are dropped.
        assert all(p.symbol.startswith("VX") and "/" in p.symbol for p in pts)

    def test_shape_from_fixture(self) -> None:
        pts = parse_vx_settlements(VX_CSV.read_text())
        c = vx_curve(pts, as_of=DAY, fetched_at=et(2026, 10, 5, 18, 30), url="u")
        assert (c.front, c.second, c.back) == (17.4555, 18.1393, 20.7995)
        assert c.slope_1_2_pct == pytest.approx(3.917)
        assert c.shape == "contango"
        assert c.source == "cboe_cfe"

    def test_bad_rows_skipped(self) -> None:
        text = textwrap.dedent(
            """\
            \ufeffProduct,Symbol,Expiration Date,Price
            VX,VX/V6,2026-10-21,17.5
            VX,VX/X6,bad-date,18.0
            VX,VX/Z6,2026-12-16,0
            VX,VXFOO,2026-12-16,18
            VXM,VXM/V6,2026-10-21,17.5
            VX,VX/F7,2027-01-20,19.0
            """
        )
        assert [p.symbol for p in parse_vx_settlements(text)] == ["VX/V6", "VX/F7"]

    def test_header_only_is_empty(self) -> None:
        assert parse_vx_settlements("Product,Symbol,Expiration Date,Price\r\n") == []

    @pytest.mark.parametrize(
        ("second", "band", "shape"),
        [
            (18.0, 0.5, "contango"),
            (16.0, 0.5, "backwardation"),
            (17.05, 0.5, "flat"),
            (16.95, 0.5, "flat"),
            (18.0, 10.0, "flat"),
        ],
    )
    def test_shape_rule(self, second: float, band: float, shape: str) -> None:
        pts = [
            VxPoint(symbol="VX/V6", expiry="2026-10-21", settle=17.0),
            VxPoint(symbol="VX/X6", expiry="2026-11-18", settle=second),
            VxPoint(symbol="VX40/V6", expiry="2026-10-07", settle=30.0, weekly=True),
        ]
        s = curve_shape(pts, flat_band=band)
        assert s.shape == shape
        assert (s.front, s.second, s.back) == (17.0, second, second)  # weekly ignored

    def test_needs_two_monthlies(self) -> None:
        pts = [
            VxPoint(symbol="VX/V6", expiry="2026-10-21", settle=17.0),
            VxPoint(symbol="VX40/V6", expiry="2026-10-07", settle=17.0, weekly=True),
        ]
        with pytest.raises(ValueError, match="two monthly"):
            curve_shape(pts)

    @given(
        front=st.floats(5, 90),
        second=st.floats(5, 90),
        band=st.floats(0, 10),
    )
    def test_shape_property(self, front: float, second: float, band: float) -> None:
        pts = [
            VxPoint(symbol="VX/V6", expiry="2026-10-21", settle=front),
            VxPoint(symbol="VX/X6", expiry="2026-11-18", settle=second),
        ]
        s = curve_shape(pts, flat_band=band)
        if s.slope_1_2_pct == 0 or abs(s.slope_1_2_pct) < band:
            assert s.shape == "flat"
        elif s.slope_1_2_pct > 0:
            assert s.shape == "contango"
            assert second > front
        else:
            assert s.shape == "backwardation"
            assert second < front

    def test_fetch_ok_and_unpublished(self) -> None:
        now = et(2026, 10, 5, 18, 30)
        seen: list[str] = []

        def get(url: str, ua: str) -> bytes:
            seen.append(url)
            return VX_CSV.read_bytes()

        c = fetch_vx_settlements(DAY, get, now=now)
        assert isinstance(c, VxCurvePayload)
        assert seen == [
            "https://www.cboe.com/us/futures/market_statistics/settlement/csv?dt=2026-10-05"
        ]
        header = b"Product,Symbol,Expiration Date,Price\r\n"
        with pytest.raises(NotPublishedError, match="no VX rows"):
            fetch_vx_settlements(DAY, _get_returning(header), now=now)
        with pytest.raises(NotPublishedError):
            fetch_vx_settlements(DAY, _get_raising(404), now=now)


# ---------------------------------------------------------------------------
# Session date
# ---------------------------------------------------------------------------


class TestSessionDate:
    @pytest.mark.parametrize(
        ("now", "expected"),
        [
            (et(2026, 10, 6, 17, 0), dt.date(2026, 10, 6)),  # after 16:30 -> today
            (et(2026, 10, 6, 16, 30), dt.date(2026, 10, 6)),  # boundary inclusive
            (et(2026, 10, 6, 16, 29), dt.date(2026, 10, 5)),
            (et(2026, 10, 6, 9, 0), dt.date(2026, 10, 5)),  # morning -> previous session
            (et(2026, 10, 5, 8, 15), dt.date(2026, 10, 2)),  # Monday -> Friday
            (et(2026, 10, 4, 18, 30), dt.date(2026, 10, 2)),  # Sunday evening -> Friday
            (et(2026, 11, 27, 8, 15), dt.date(2026, 11, 25)),  # day after Thanksgiving
        ],
    )
    def test_rule(self, now: dt.datetime, expected: dt.date) -> None:
        assert session_date(now) == expected
        assert completed_session(now) == expected

    def test_utc_input(self) -> None:
        # 2026-10-06 22:30 UTC == 18:30 ET
        assert session_date(dt.datetime(2026, 10, 6, 22, 30, tzinfo=dt.UTC)) == dt.date(2026, 10, 6)


# ---------------------------------------------------------------------------
# Handlers: skip on the evening slot, fail on the catch-up
# ---------------------------------------------------------------------------


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    c = connect(tmp_path / "arc.db")
    migrate(c)
    yield c
    c.close()


def _routines(**options_slow: Any) -> RoutinesConfig:
    cfg = load_routines(REPO / "config" / "routines.yaml")
    if options_slow:
        cfg = cfg.model_copy(
            update={"options_slow": cfg.options_slow.model_copy(update=options_slow)}
        )
    return cfg


def _ctx(conn: sqlite3.Connection, job: str, slot: dt.datetime, **kw: Any) -> JobContext:
    routines = kw.pop("routines", None) or _routines()
    kind, spec = routines.step(job)
    return JobContext(
        job=job,
        kind=kind,
        spec=spec,
        run_id="run-1",
        chain_run_id=None,
        scheduled_for=slot,
        now=slot,
        conn=conn,
        snapshot=ContextStore(conn).snapshot(slot, kinds=[]),
        routines=routines,
    )


def _payloads(conn: sqlite3.Connection, kind: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT payload FROM context_entries WHERE kind = ? ORDER BY rowid", (kind,)
    ).fetchall()
    return [json.loads(r[0]) for r in rows]


class TestHandlers:
    def test_registered(self) -> None:
        assert BUILTIN_HANDLERS["options_daily"].endswith(":options_daily_source")
        assert BUILTIN_HANDLERS["vix_futures"].endswith(":vix_futures_source")
        assert {"options_daily", "vx_curve"} <= set(KINDS)

    def test_evening_writes_both(
        self, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[str] = []

        def get(url: str, ua: str) -> bytes:
            seen.append(url)
            return DAILY_JSON.read_bytes() if "daily_options" in url else VX_CSV.read_bytes()

        monkeypatch.setattr(cboe_daily, "http_get", get)
        slot = et(2026, 10, 5, 18, 30)
        r1 = options_daily_source(_ctx(conn, "options_daily", slot))
        r2 = vix_futures_source(_ctx(conn, "vix_futures", slot))
        assert "2026-10-05" in seen[0]
        assert "dt=2026-10-05" in seen[1]
        assert "P/C total 0.83 · equity 0.59 · spx 1.03" in r1.summary
        assert r1.metrics["pc_spx"] == 1.03
        assert r1.metrics["probe_wait_s"] == 0
        assert "contango" in r2.summary
        assert r2.metrics["front"] == 17.4555
        (od,) = _payloads(conn, "options_daily")
        (vx,) = _payloads(conn, "vx_curve")
        assert od["as_of"] == "2026-10-05"
        assert vx["shape"] == "contango"
        rows = ContextStore(conn).query(
            as_of=slot + dt.timedelta(minutes=1), kinds=["options_daily", "vx_curve"]
        )
        assert {(e.kind, e.subject) for e in rows} == {
            ("options_daily", "market"),
            ("vx_curve", "market"),
        }

    def test_morning_reads_previous_session(
        self, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[str] = []

        def get(url: str, ua: str) -> bytes:
            seen.append(url)
            return DAILY_JSON.read_bytes()

        monkeypatch.setattr(cboe_daily, "http_get", get)
        options_daily_source(_ctx(conn, "options_daily", et(2026, 10, 6, 8, 15)))
        assert seen[0].endswith("/2026-10-05_daily_options")

    @pytest.mark.parametrize("job", ["options_daily", "vix_futures"])
    def test_unpublished_skips_in_the_evening(
        self, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, job: str
    ) -> None:
        monkeypatch.setattr(cboe_daily, "http_get", _get_raising(404))
        handler = options_daily_source if job == "options_daily" else vix_futures_source
        with pytest.raises(JobSkippedError, match="not published yet"):
            handler(_ctx(conn, job, et(2026, 10, 6, 18, 30)))

    @pytest.mark.parametrize("job", ["options_daily", "vix_futures"])
    def test_unpublished_fails_on_catch_up(
        self, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, job: str
    ) -> None:
        monkeypatch.setattr(cboe_daily, "http_get", _get_raising(404))
        handler = options_daily_source if job == "options_daily" else vix_futures_source
        with pytest.raises(RuntimeError, match="catch-up: Cboe still has not published 2026-10-06"):
            handler(_ctx(conn, job, et(2026, 10, 7, 8, 15)))

    def test_probe_waits_then_writes(
        self, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = {"n": 0}
        sleeps: list[float] = []

        def get(url: str, ua: str) -> bytes:
            calls["n"] += 1
            if calls["n"] < 3:  # noqa: PLR2004
                raise _http_error(403)
            return DAILY_JSON.read_bytes()

        monkeypatch.setattr(cboe_daily, "http_get", get)
        monkeypatch.setattr(handlers, "_probe_sleep", sleeps.append)
        ctx = _ctx(
            conn,
            "options_daily",
            et(2026, 10, 5, 18, 30),
            routines=_routines(publish_probe_minutes=5),
        )
        r = options_daily_source(ctx)
        assert sleeps == [60, 60]
        assert r.metrics["probe_wait_s"] == 120

    def test_probe_gives_up_then_skips(
        self, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sleeps: list[float] = []
        monkeypatch.setattr(cboe_daily, "http_get", _get_raising(403))
        monkeypatch.setattr(handlers, "_probe_sleep", sleeps.append)
        ctx = _ctx(
            conn,
            "vix_futures",
            et(2026, 10, 5, 18, 30),
            routines=_routines(publish_probe_minutes=2),
        )
        with pytest.raises(JobSkippedError):
            vix_futures_source(ctx)
        assert sleeps == [60, 60]

    def test_flat_band_from_config(
        self, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(cboe_daily, "http_get", _get_returning(VX_CSV.read_bytes()))
        ctx = _ctx(
            conn, "vix_futures", et(2026, 10, 5, 18, 30), routines=_routines(vx_flat_band=5.0)
        )
        assert "(flat)" in vix_futures_source(ctx).summary


# ---------------------------------------------------------------------------
# Config + dispatcher: `<kind>:{day}` catch-up
# ---------------------------------------------------------------------------


class TestCatchUpTemplate:
    def test_per_session_template(self) -> None:
        cu = CatchUp(until_written="options_daily:{day}")
        assert cu.per_session
        assert cu.target == ("options_daily", "{day}")
        assert not CatchUp(until_written="universe_tier:momentum").per_session

    @pytest.mark.parametrize("bad", ["options_daily:{date}", "options_daily:x{day}"])
    def test_other_templates_rejected(self, bad: str) -> None:
        with pytest.raises(ValueError, match="only template"):
            CatchUp(until_written=bad)

    def test_repo_config(self) -> None:
        cfg = _routines()
        for job, kind in (("options_daily", "options_daily"), ("vix_futures", "vx_curve")):
            spec = cfg.sources[job]
            assert [t.strftime("%H:%M") for t in spec.schedule] == ["18:30", "08:15"]
            assert spec.catch_up is not None
            assert spec.catch_up.until_written == f"{kind}:{{day}}"
            assert spec.options.get("category") == "options_slow"
            assert spec.options.get("feed") == "scout"
            assert spec.writes == [kind]
        assert cfg.options_slow.vx_flat_band == 0.5
        assert cfg.options_slow.publish_probe_minutes == 0

    def test_registry(self) -> None:
        assert REGISTRY["options_slow.vx_flat_band"].max == 10.0
        assert "options_slow.publish_probe_minutes" in NOT_EXPOSED_PATHS
        assert "options_slow.publish_probe_minutes" not in REGISTRY


YAML = """
    sources:
      options_daily:
        schedule: ["18:30", "08:15"]
        days: trading
        ttl: 2h
        catch_up: {days: trading, until_written: "options_daily:{day}"}
        category: options_slow
        writes: [options_daily]
    personas: {}
"""


class Writer:
    def __init__(self) -> None:
        self.calls: list[dt.datetime] = []
        self.publish = True

    def __call__(self, ctx: JobContext) -> JobResult:
        self.calls.append(ctx.scheduled_for)
        day = completed_session(ctx.scheduled_for)
        if not self.publish:
            raise JobSkippedError("not published yet")
        payload = parse_daily_options(_daily(), day, fetched_at=ctx.now)
        assert payload is not None
        ctx.write("options_daily", "market", payload)
        return JobResult(summary="ok")


def _disp(conn: sqlite3.Connection, w: Writer) -> Dispatcher:
    cfg = RoutinesConfig.model_validate(yaml.safe_load(textwrap.dedent(YAML)))
    return Dispatcher(
        conn,
        cfg,
        handlers={"options_daily": w},
        notifier=RecordingNotifier(),
        is_halted=lambda: False,
    )


def _tick(d: Dispatcher, at: dt.datetime) -> list[str]:
    rep = d.tick(at, since=at - dt.timedelta(minutes=5))
    return [o.status for o in rep.outcomes if o.job == "options_daily"]


class TestDispatcherCatchUp:
    def test_morning_skipped_after_evening_wrote(self, conn: sqlite3.Connection) -> None:
        w = Writer()
        d = _disp(conn, w)
        assert _tick(d, et(2026, 10, 6, 18, 30)) == ["ok"]
        assert _tick(d, et(2026, 10, 7, 8, 15)) == ["skipped"]
        assert w.calls == [et(2026, 10, 6, 18, 30)]
        (summary,) = conn.execute(
            "SELECT summary FROM routine_runs WHERE job='options_daily' "
            "AND scheduled_for LIKE '2026-10-07%'"
        ).fetchone()
        assert "already written: options_daily for 2026-10-06" in summary

    def test_morning_runs_when_evening_did_not_write(self, conn: sqlite3.Connection) -> None:
        w = Writer()
        w.publish = False
        d = _disp(conn, w)
        assert _tick(d, et(2026, 10, 6, 18, 30)) == ["skipped"]
        w.publish = True
        assert _tick(d, et(2026, 10, 7, 8, 15)) == ["ok"]
        assert w.calls == [et(2026, 10, 6, 18, 30), et(2026, 10, 7, 8, 15)]
        assert [r["as_of"] for r in _payloads(conn, "options_daily")] == ["2026-10-06"]

    def test_dry_run_plan(self, conn: sqlite3.Connection) -> None:
        w = Writer()
        d = _disp(conn, w)
        plan = d.plan(et(2026, 10, 7, 8, 15), since=et(2026, 10, 7, 8, 10))
        assert [(p.job, p.action) for p in plan] == [("options_daily", "run")]
        _tick(d, et(2026, 10, 6, 18, 30))
        plan = d.plan(et(2026, 10, 7, 8, 15), since=et(2026, 10, 7, 8, 10))
        assert [(p.job, p.action) for p in plan] == [("options_daily", "skip-written")]
        assert w.calls == [et(2026, 10, 6, 18, 30)]
