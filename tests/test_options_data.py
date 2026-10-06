"""E4.5 / D30 options-data connectors: parsers on recorded payloads + handlers.

Fixtures in tests/fixtures/options_data are trimmed copies of the live payloads
fetched 2026-09-29 (Cboe history CSVs + daily market statistics, the Fed FOMC
calendar page for 2026 and 2024, the BLS release-schedule ICS for Sep-Dec 2026).
``bea_schedule.ics`` is a trimmed raw copy (CRLF, folded lines, ``\\,`` escapes) of the
BEA release-schedule ICS fetched 2026-10-04: 22 VEVENTs (8 GDP, 7 Personal Income and
Outlays, and 7 regional/territory/trade releases that must be dropped).
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

import pytest
import requests
from hypothesis import given
from hypothesis import strategies as st

from arc.context.kinds import MacroEvent
from arc.context.store import ContextStore
from arc.data.base import OptionContract
from arc.ingest.options_data import (
    BLS_UA,
    BROWSER_UA,
    CBOE_DAILY_URL,
    UoaThresholds,
    fetch_macro_calendar,
    fetch_put_call,
    fetch_vol_term,
    macro_calendar,
    next_ex_dividends,
    parse_bea_ics,
    parse_bls_ics,
    parse_cboe_history,
    parse_fomc_calendar,
    parse_put_call,
    prior_volumes,
    record_volume,
    scan_unusual,
    unusual_activity,
    vol_term_from_closes,
)
from arc.personas.builders import (
    build_research_prompt,
    build_risk_prompt,
    research_input_from_context,
    risk_input_from_context,
)
from arc.store.db import connect
from arc.store.migrate import migrate
from arc.utils.calendar import ET

FIX = Path(__file__).parent / "fixtures" / "options_data"
TODAY = dt.date(2026, 9, 29)
NOW = dt.datetime(2026, 9, 29, 9, 0, tzinfo=ET)


@pytest.fixture()
def conn():
    c = connect(":memory:")
    migrate(c)
    return c


def _fixture_get(url: str, ua: str) -> bytes:
    """Offline stand-in for http_get: serves the recorded payload for *url*."""
    name = url.rsplit("/", 1)[-1]
    if name.endswith("_daily_options"):
        name += ".json"
    name = {"online-calendar-subscription.ics": "bea_schedule.ics"}.get(name, name)
    path = FIX / name
    if not path.exists():
        raise requests.HTTPError(f"404 {url}")
    assert ua in (BROWSER_UA, BLS_UA)
    return path.read_bytes()


# ---------------------------------------------------------------------------
# VIX term structure
# ---------------------------------------------------------------------------


class TestVolTerm:
    def test_recorded_cboe_history(self) -> None:
        p = fetch_vol_term(get=_fixture_get)
        assert p is not None
        assert p.as_of == "2026-09-28"
        assert (p.vix9d, p.vix, p.vix3m, p.vvix) == (14.39, 16.07, 18.23, 91.02)
        assert p.structure == "contango" and p.ratio_3m_1m == pytest.approx(1.1344)

    def test_backwardation_and_flat(self) -> None:
        d = dt.date(2026, 3, 9)
        back = vol_term_from_closes({"VIX": {d: 30.0}, "VIX3M": {d: 26.0}})
        flat = vol_term_from_closes({"VIX": {d: 20.0}, "VIX3M": {d: 20.2}})
        assert back is not None and back.structure == "backwardation"
        assert flat is not None and flat.structure == "flat"

    def test_missing_vix_is_none_and_missing_others_is_partial(self) -> None:
        assert vol_term_from_closes({"VIX3M": {TODAY: 20.0}}) is None
        p = vol_term_from_closes({"VIX": {TODAY: 20.0}})
        assert p is not None and p.vix3m is None and p.structure == "flat"

    def test_one_index_down_does_not_fail_the_fetch(self) -> None:
        def get(url: str, ua: str) -> bytes:
            if "VVIX" in url:
                raise requests.ConnectionError("down")
            return _fixture_get(url, ua)

        p = fetch_vol_term(get=get)
        assert p is not None and p.vvix is None and p.vix == 16.07

    def test_parse_skips_bad_rows(self) -> None:
        text = "DATE,OPEN,HIGH,LOW,CLOSE\n09/25/2026,1,1,1,15.5\nbad,1,1,1,x\n09/26/2026,1,1,1,0\n"
        assert parse_cboe_history(text) == {dt.date(2026, 9, 25): 15.5}


# ---------------------------------------------------------------------------
# Put/call
# ---------------------------------------------------------------------------


class TestPutCall:
    def test_recorded_daily_statistics(self) -> None:
        p = parse_put_call(json.loads((FIX / "2026-09-28_daily_options.json").read_text()), TODAY)
        assert p is not None
        assert (p.total, p.equity, p.index, p.etp, p.vix) == (0.88, 0.58, 0.97, 1.15, 0.48)

    def test_walks_back_to_latest_published_session(self) -> None:
        """09-29 is not published yet at 09:00 ET: the fetch falls back to 09-28."""
        p = fetch_put_call(TODAY, get=_fixture_get)
        assert p is not None and p.as_of == "2026-09-28"
        assert CBOE_DAILY_URL.format(day="x").endswith("x_daily_options")

    def test_none_when_nothing_in_lookback(self) -> None:
        def get(url: str, ua: str) -> bytes:
            raise requests.HTTPError("404")

        assert fetch_put_call(TODAY, get=get) is None
        assert (
            parse_put_call({"ratios": [{"name": "INDEX PUT/CALL RATIO", "value": "1"}]}, TODAY)
            is None
        )


# ---------------------------------------------------------------------------
# Macro calendar
# ---------------------------------------------------------------------------


class TestMacroCalendar:
    def test_fomc_decision_days(self) -> None:
        events = parse_fomc_calendar((FIX / "fomccalendars.htm").read_text())
        days = [e.date for e in events]
        assert "2026-10-28" in days and "2026-12-09" in days
        assert "2024-05-01" in days  # "Apr/May 30-1" -> the decision is on May 1
        assert all(e.kind == "fomc" and e.time == "14:00" for e in events)
        sep = {e.date for e in events if "projections" in e.name}
        assert "2026-12-09" in sep  # "*" = Summary of Economic Projections meeting

    def test_notation_votes_are_skipped(self) -> None:
        html = (
            "<h4>2025 FOMC Meetings</h4>"
            '<div class="fomc-meeting__month x"><strong>August</strong></div>'
            '<div class="fomc-meeting__date x">22 (notation vote)</div>'
            '<div class="fomc-meeting__month x"><strong>September</strong></div>'
            '<div class="fomc-meeting__date x">16-17*</div>'
        )
        assert [e.date for e in parse_fomc_calendar(html)] == ["2025-09-17"]

    def test_bls_market_moving_releases_only(self) -> None:
        events = parse_bls_ics((FIX / "bls.ics").read_text())
        kinds = {e.kind for e in events}
        assert kinds == {"cpi", "ppi", "nfp", "jolts", "eci"}
        nfp = next(e for e in events if e.kind == "nfp" and e.date == "2026-10-02")
        assert nfp.time == "08:30"
        # regional/state series share prefixes with the national ones; they are dropped
        assert not any("State" in e.name or "Metropolitan" in e.name for e in events)

    def test_merged_window(self) -> None:
        payload, counts = fetch_macro_calendar(TODAY, 45, get=_fixture_get)
        assert counts["fomc"] > 0 and counts["bls"] > 0 and counts["bea"] > 0
        got = [(e.date, e.kind) for e in payload.events]
        assert got[:6] == [
            ("2026-09-29", "jolts"),
            ("2026-09-30", "gdp"),
            ("2026-09-30", "pce"),
            ("2026-10-02", "nfp"),
            ("2026-10-14", "cpi"),
            ("2026-10-15", "ppi"),
        ]
        assert ("2026-10-28", "fomc") in got
        assert ("2026-10-29", "gdp") in got and ("2026-10-29", "pce") in got
        assert all(TODAY.isoformat() <= d <= "2026-11-13" for d, _ in got)

    def test_bls_and_bea_get_the_contact_user_agent(self) -> None:
        """Live 2026-09-29: BLS 403s an agent without a contact email; BEA gets the same."""
        seen: dict[str, str] = {}

        def get(url: str, ua: str) -> bytes:
            seen[url.split("/")[2]] = ua
            return _fixture_get(url, ua if ua == BROWSER_UA else BLS_UA)

        fetch_macro_calendar(TODAY, 45, get=get, contact_ua="Owner arc@owner.example")
        assert seen["www.bls.gov"] == "Owner arc@owner.example"
        assert seen["www.bea.gov"] == "Owner arc@owner.example"
        assert "@" in BLS_UA

    def test_one_calendar_down_still_returns_the_other(self) -> None:
        def get(url: str, ua: str) -> bytes:
            if "federalreserve" in url:
                raise requests.ConnectionError("down")
            return _fixture_get(url, ua)

        payload, counts = fetch_macro_calendar(TODAY, 45, get=get)
        assert counts["fomc"] == 0 and payload.events

    @pytest.mark.parametrize(
        "failure",
        [requests.HTTPError("403 Forbidden"), requests.ConnectionError("down"), "garbage"],
    )
    def test_bea_failure_keeps_fomc_and_bls(self, failure: object) -> None:
        from structlog.testing import capture_logs

        status: dict[str, str] = {}

        def get(url: str, ua: str) -> bytes:
            if "bea.gov" in url:
                if isinstance(failure, Exception):
                    raise failure
                # parses, but the date is impossible: the parser must raise, not crash the run
                return b"BEGIN:VEVENT\r\nSUMMARY:GDP (Advance Estimate)\\, 3rd Quarter 2026\r\n" + (
                    b"DTSTART;VALUE=DATE-TIME:20261399T123000Z\r\nEND:VEVENT\r\n"
                )
            return _fixture_get(url, ua)

        with capture_logs() as logs:
            payload, counts = fetch_macro_calendar(TODAY, 45, get=get, status=status)
        assert counts["bea"] == 0 and counts["fomc"] > 0 and counts["bls"] > 0
        assert status["fomc"] == status["bls"] == "ok"
        assert status["bea"].startswith("failed:")
        assert {e.kind for e in payload.events} >= {"fomc", "cpi"}
        assert not any(e.source == "bea.gov" for e in payload.events)
        failed = [x for x in logs if x["event"] == "macro_calendar.source_failed"]
        assert [x["source"] for x in failed] == ["bea"]

    def test_dedupes_same_day_same_kind(self) -> None:
        events = parse_bls_ics((FIX / "bls.ics").read_text())
        p = macro_calendar(events + events, TODAY, 90)
        assert len({(e.date, e.kind) for e in p.events}) == len(p.events)

    def test_dedupe_key_includes_name(self) -> None:
        """Two distinct releases of one kind on one day both survive; exact repeats don't."""
        a = MacroEvent(date="2026-10-29", time="08:30", kind="gdp", name="GDP A", source="x")
        b = a.model_copy(update={"name": "GDP B"})
        p = macro_calendar([a, b, a], TODAY, 45)
        assert [e.name for e in p.events] == ["GDP A", "GDP B"]


class TestBea:
    def _events(self) -> list[MacroEvent]:
        return parse_bea_ics((FIX / "bea_schedule.ics").read_text())

    def test_counts_and_kinds(self) -> None:
        events = self._events()
        # fixture: 22 VEVENTs = 8 GDP + 7 Personal Income and Outlays kept; 7 dropped
        # (State / Puerto Rico / County GDP, 2x Real PCE by State, International Trade)
        assert sum(e.kind == "gdp" for e in events) == 8
        assert sum(e.kind == "pce" for e in events) == 7
        assert len(events) == 15
        assert {e.kind for e in events} == {"gdp", "pce"}  # never "other"
        assert all(e.source == "bea.gov" for e in events)

    def test_ignored_releases_are_dropped(self) -> None:
        names = " ".join(e.name for e in self._events())
        for word in ("County", "State", "Puerto", "Trade", "Real Personal"):
            assert word not in names
        for day in ("2025-03-28", "2025-09-16", "2026-02-05", "2026-02-19", "2026-12-02"):
            assert not any(e.date == day for e in self._events())

    def test_names_and_next_release(self) -> None:
        by_day = {(e.date, e.kind): e for e in self._events()}
        gdp = by_day[("2026-10-29", "gdp")]
        pce = by_day[("2026-10-29", "pce")]
        assert gdp.name == "GDP Q3 2026 (advance)" and gdp.time == "08:30"
        assert pce.name == "PCE / Personal Income September 2026" and pce.time == "08:30"
        assert by_day[("2026-11-25", "gdp")].name == "GDP Q3 2026 (second)"
        assert by_day[("2026-12-23", "gdp")].name == "GDP Q3 2026 (third)"
        # pre-2026 long title, folded across two lines in the raw ICS
        assert by_day[("2025-01-30", "gdp")].name == "GDP Q4 2024 (advance)"
        assert by_day[("2025-03-27", "gdp")].name == "GDP Q4 2024 (third)"
        assert by_day[("2025-12-23", "gdp")].name == "GDP Q3 2025 (initial)"

    def test_utc_to_et_across_dst(self) -> None:
        by_day = {(e.date, e.kind): e for e in self._events()}
        assert by_day[("2026-10-29", "gdp")].time == "08:30"  # 12:30Z in EDT
        assert by_day[("2026-11-25", "gdp")].time == "08:30"  # 13:30Z in EST
        assert by_day[("2025-12-05", "pce")].time == "10:00"  # 15:00Z in EST

    def test_late_utc_rolls_back_to_the_et_date(self) -> None:
        ics = (
            "BEGIN:VEVENT\r\nSUMMARY:Personal Income and Outlays\\, May 2026\r\n"
            "DTSTART;VALUE=DATE-TIME:20260627T020000Z\r\nEND:VEVENT\r\n"
        )
        (e,) = parse_bea_ics(ics)
        assert (e.date, e.time) == ("2026-06-26", "22:00")

    def test_tzid_and_all_day_starts(self) -> None:
        ics = (
            "BEGIN:VEVENT\r\nSUMMARY:GDP (Advance Estimate)\\, 1st Quarter 2027\r\n"
            "DTSTART;TZID=America/New_York:20270429T083000\r\nEND:VEVENT\r\n"
            "BEGIN:VEVENT\r\nSUMMARY:Personal Income and Outlays\\, March 2027\r\n"
            "DTSTART;VALUE=DATE:20270430\r\nEND:VEVENT\r\n"
            "BEGIN:VEVENT\r\nSUMMARY:Personal Income and Outlays\\, April 2027\r\n"
            "DTSTART;TZID=Europe/London:20270528T083000\r\nEND:VEVENT\r\n"
            "BEGIN:VEVENT\r\nSUMMARY:GDP (Second Estimate)\r\nDTSTART:not-a-date\r\nEND:VEVENT\r\n"
        )
        got = [(e.date, e.time, e.kind) for e in parse_bea_ics(ics)]
        assert got == [("2027-04-29", "08:30", "gdp"), ("2027-04-30", None, "pce")]

    def test_quarterless_gdp_title_and_escapes(self) -> None:
        ics = (
            "BEGIN:VEVENT\r\nSUMMARY:GDP (Advance Estimate)\\; revised\\\\notes\r\n"
            "DTSTART:20270129T133000Z\r\nEND:VEVENT\r\n"
            "BEGIN:VEVENT\r\nSUMMARY:Gross Domestic Product\\, annual update\r\n"
            "DTSTART:20270130T133000Z\r\nEND:VEVENT\r\n"
        )
        assert [e.name for e in parse_bea_ics(ics)] == ["GDP (advance)", "GDP"]

    @given(
        st.datetimes(
            min_value=dt.datetime(2020, 1, 1),  # noqa: DTZ001 - naive UTC wall time for the ICS
            max_value=dt.datetime(2035, 12, 31),  # noqa: DTZ001
        )
    )
    def test_property_et_conversion(self, at: dt.datetime) -> None:
        ics = (
            "BEGIN:VEVENT\r\nSUMMARY:Personal Income and Outlays\\, X\r\n"
            f"DTSTART:{at:%Y%m%dT%H%M}00Z\r\nEND:VEVENT\r\n"
        )
        (e,) = parse_bea_ics(ics)
        want = at.replace(second=0, microsecond=0, tzinfo=dt.UTC).astimezone(ET)
        assert (e.date, e.time) == (want.date().isoformat(), want.strftime("%H:%M"))


# ---------------------------------------------------------------------------
# Unusual options activity
# ---------------------------------------------------------------------------


def _c(
    symbol: str,
    kind: str,
    vol: int,
    oi: int | None,
    strike: float = 100.0,
    expiry: dt.date = dt.date(2026, 10, 16),
) -> OptionContract:
    return OptionContract(
        symbol=symbol,
        underlying="NVDA",
        expiration=expiry,
        strike=strike,
        option_type=kind,
        volume=vol,
        open_interest=oi,
    )


T = UoaThresholds(min_volume=500, vol_oi_ratio=2.0, volume_spike_ratio=2.0)


class TestUnusualOptions:
    def test_vol_oi_and_spike_flags(self) -> None:
        chain = [
            _c("C1", "call", 5000, 1000),  # vol/oi 5 -> hot
            _c("C2", "call", 800, 1000),  # 0.8 -> not hot
            _c("P1", "put", 400, 10),  # below min volume
            _c("P2", "put", 600, None),  # OI unknown -> can't judge, not listed
            _c("P3", "put", 3000, 1500),  # vol/oi 2.0 -> hot (boundary)
        ]
        p = unusual_activity("NVDA", chain, [1500] * 20, TODAY, T)
        assert (p.call_volume, p.put_volume, p.total_volume) == (5800, 4000, 9800)
        assert p.avg_volume == 1500.0 and p.volume_ratio == pytest.approx(9800 / 1500, rel=1e-3)
        assert p.flags == ["volume_spike", "vol_oi"]
        assert [u.symbol for u in p.contracts] == ["C1", "P3"]

    def test_live_noise_is_not_flagged(self) -> None:
        """Live 2026-09-29: 18/20 tickers flagged, driven by 0-2 DTE lines and OI of 1."""
        chain = [
            _c("ZERO", "call", 16689, 16, expiry=TODAY + dt.timedelta(days=1)),  # 0DTE churn
            _c("EMPTY", "put", 868, 1),  # 868x on a line with OI 1
        ]
        p = unusual_activity("SPY", chain, [], TODAY, T)
        assert p.flags == [] and p.contracts == []
        loose = UoaThresholds(min_dte=0, min_open_interest=1)
        assert [u.symbol for u in unusual_activity("SPY", chain, [], TODAY, loose).contracts] == [
            "ZERO",
            "EMPTY",
        ]

    def test_hot_lines_must_be_a_real_share_of_volume(self) -> None:
        """Live: SPY had 3k hot contracts out of 4M (0.08%); BAC 21k of 50k (42%)."""
        deep = [_c("HOT", "put", 3263, 133), _c("BULK", "call", 4_000_000, 10_000_000)]
        p = unusual_activity("SPY", deep, [], TODAY, T)
        assert p.flags == [] and [u.symbol for u in p.contracts] == ["HOT"]
        assert p.hot_volume_share == pytest.approx(3263 / 4_003_263, abs=1e-4)
        thin = [_c("HOT", "put", 21470, 4144), _c("BULK", "call", 29000, 90000)]
        assert unusual_activity("BAC", thin, [], TODAY, T).flags == ["vol_oi"]

    def test_spike_needs_history(self) -> None:
        p = unusual_activity("NVDA", [_c("C", "call", 10000, 100000)], [100, 100], TODAY, T)
        assert p.volume_ratio == 100.0 and "volume_spike" not in p.flags  # < 5 sessions

    @given(st.lists(st.tuples(st.integers(0, 5000), st.integers(0, 5000)), max_size=30))
    def test_totals_add_up(self, rows: list[tuple[int, int]]) -> None:
        chain = [_c(f"S{i}", "call" if i % 2 else "put", v, oi) for i, (v, oi) in enumerate(rows)]
        p = unusual_activity("X", chain, [], TODAY, T)
        assert p.call_volume + p.put_volume == p.total_volume == sum(v for v, _ in rows)
        assert len(p.contracts) <= T.max_contracts

    def test_scan_persists_volume_for_the_20_day_average(self, conn) -> None:
        for i in range(1, 21):
            record_volume(conn, "NVDA", TODAY - dt.timedelta(days=i), 700, 300, now="t")

        class _Market:
            def option_chain(self, underlying: str, a: dt.date, b: dt.date) -> list[OptionContract]:
                if underlying == "BAD":
                    raise RuntimeError("no chain")
                return [_c("C1", "call", 4000, 500), _c("P1", "put", 1000, 5000)]

        payloads, errors = scan_unusual(
            conn,
            _Market(),
            ["NVDA", "BAD"],
            TODAY,
            T,
            max_dte=45,
            now="t",  # type: ignore[arg-type]
        )
        assert errors.keys() == {"BAD"}
        [p] = payloads
        assert p.history_days == 20 and p.avg_volume == 1000.0 and p.volume_ratio == 5.0
        assert prior_volumes(conn, "NVDA", TODAY + dt.timedelta(days=1), 1) == [5000]
        # re-running the same day updates, never duplicates
        scan_unusual(conn, _Market(), ["NVDA"], TODAY, T, max_dte=45, now="t2")  # type: ignore[arg-type]
        n = conn.execute(
            "SELECT count(*) FROM options_volume_daily WHERE ticker='NVDA' AND day=?",
            (TODAY.isoformat(),),
        ).fetchone()[0]
        assert n == 1


# ---------------------------------------------------------------------------
# Ex-dividend
# ---------------------------------------------------------------------------


class TestExDividend:
    def test_earliest_upcoming_within_horizon(self) -> None:
        actions: list[dict[str, Any]] = [
            {
                "symbol": "XOM",
                "ex_date": dt.date(2026, 11, 13),
                "rate": 1.03,
                "record_date": dt.date(2026, 11, 13),
                "payable_date": dt.date(2026, 12, 10),
            },
            {"symbol": "XOM", "ex_date": dt.date(2026, 10, 2), "rate": 1.0},
            {"symbol": "AAPL", "ex_date": "2026-08-11", "rate": 0.26},  # past
            {"symbol": "JPM", "ex_date": "2027-01-06", "rate": 1.4},  # beyond horizon
            {"symbol": "", "ex_date": "2026-10-01"},
        ]
        got = next_ex_dividends(actions, TODAY, 45)
        assert set(got) == {"XOM"}
        assert got["XOM"].ex_date == "2026-10-02" and got["XOM"].amount == 1.0


# ---------------------------------------------------------------------------
# Handlers + persona wiring
# ---------------------------------------------------------------------------


def _ctx(conn, job: str, writes: list[str], **opts: Any):
    from arc.config import ArcSettings
    from arc.routines.config import RoutinesConfig
    from arc.routines.handlers import JobContext

    routines = RoutinesConfig.model_validate(
        {
            "sources": {
                job: {"schedule": ["09:00"], "writes": writes, "category": "options_data", **opts}
            }
        }
    )
    kind, spec = routines.step(job)
    settings = ArcSettings(env="paper", universe=["NVDA", "XOM"])  # type: ignore[arg-type]
    return JobContext(
        job=job,
        kind=kind,
        spec=spec,
        run_id="run-1",
        chain_run_id=None,
        scheduled_for=NOW,
        now=NOW,
        conn=conn,
        snapshot=ContextStore(conn).snapshot(NOW, kinds=[]),
        routines=routines,
        settings_factory=lambda: settings,
    )


def _structures(store: ContextStore) -> None:
    store.write(
        kind="structures",
        subject="session",
        payload={"structures": [], "analysis_notes": "none"},
        produced_by="quant",
        now=NOW,
    )


class TestHandlers:
    def test_data_jobs_write_typed_kinds(self, conn, monkeypatch: pytest.MonkeyPatch) -> None:
        import arc.ingest.options_data as od
        from arc.routines.handlers import (
            macro_calendar_source,
            put_call_source,
            vol_term_source,
        )

        monkeypatch.setattr(od, "http_get", _fixture_get)  # every fetcher resolves it per call

        r1 = vol_term_source(_ctx(conn, "vol_term", ["vol_term"]))
        r2 = put_call_source(_ctx(conn, "put_call", ["put_call"]))
        r3 = macro_calendar_source(
            _ctx(conn, "macro_calendar", ["macro_calendar"], horizon_days=45)
        )
        assert "contango" in r1.summary and "0.88" in r2.summary and "next JOLTS" in r3.summary
        kinds = {e.kind for e in ContextStore(conn).query(as_of=NOW)}
        assert kinds == {"vol_term", "put_call", "macro_calendar"}

    def test_unusual_options_handler(self, conn) -> None:
        from arc.routines.handlers import unusual_options_source

        class _Market:
            def option_chain(self, u: str, a: dt.date, b: dt.date) -> list[OptionContract]:
                return [_c(f"{u}C", "call", 3000, 100)]

        out = unusual_options_source(_ctx(conn, "unusual_options", ["unusual_options"]), _Market())  # type: ignore[arg-type]
        assert out.metrics == {"tickers": 2, "unusual": 2, "errors": 0}
        subjects = {
            e.subject for e in ContextStore(conn).query(as_of=NOW, kinds=["unusual_options"])
        }
        assert subjects == {"NVDA", "XOM"}

    def test_research_and_risk_prompts_carry_the_data(self, conn, monkeypatch) -> None:
        payload, _ = fetch_macro_calendar(TODAY, 45, get=_fixture_get)
        vt = fetch_vol_term(get=_fixture_get)
        assert vt is not None
        store = ContextStore(conn)
        store.write(kind="vol_term", subject="market", payload=vt, produced_by="vol_term", now=NOW)
        store.write(
            kind="macro_calendar", subject="market", payload=payload, produced_by="m", now=NOW
        )
        ex = next_ex_dividends([{"symbol": "XOM", "ex_date": "2026-10-02", "rate": 1.0}], TODAY, 45)
        store.write(kind="ex_dividend", subject="XOM", payload=ex["XOM"], produced_by="x", now=NOW)
        _structures(store)
        snap = store.snapshot(NOW)
        d = build_research_prompt(
            research_input_from_context(snap, portfolio_summary="flat", scan_date="2026-09-29")
        )
        assert "Options market data" in d and '"structure": "contango"' in d and "jolts" in d
        r = build_risk_prompt(
            risk_input_from_context(
                snap, portfolio_json="{}", calendar_json="{}", account_equity=1e5, scan_date="x"
            )
        )
        assert "Event risk" in r and '"ex_date": "2026-10-02"' in r

    def test_prompts_unchanged_without_data(self, conn) -> None:
        """No D30 data -> Research/Risk prompts are byte-identical to pre-E4.5 (replay)."""
        _structures(ContextStore(conn))
        snap = ContextStore(conn).snapshot(NOW)
        d = build_research_prompt(
            research_input_from_context(snap, portfolio_summary="flat", scan_date="2026-09-29")
        )
        assert "{inp.regime_features_json}" not in d
        assert "{}\n\n### Current portfolio" in d and "Options market data" not in d
        r = build_risk_prompt(
            risk_input_from_context(
                snap, portfolio_json="{}", calendar_json="{}", account_equity=1.0, scan_date="x"
            )
        )
        assert "{}\n\n### Account equity" in r and "Event risk" not in r
