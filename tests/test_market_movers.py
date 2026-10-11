"""E14.3 (D60): Alpaca movers + most-actives as Scalp context, behind a default-off flag.

Pins: the screener parse, the write-time exclusions (price < $3, warrants / units /
rights, leveraged funds), the handler outcomes (ok / no key / failed fetch writes
nothing), the "Tape movers" block (active-list and story names only, <= 10 lines,
"no fresh info" when nothing fresh is stored), the prompt with the flag off
(byte-identical golden) and on (one added section), the flag in ``REGISTRY``, an
arm overlay that turns it on, and that the payload never reaches candidate / universe_tier.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
import yaml
from hypothesis import given
from hypothesis import strategies as st

from arc.config import ArcSettings
from arc.context import ContextStore
from arc.context.categories import KIND_CATEGORY, SourceCategory
from arc.context.kinds import MarketMoversPayload, MoverRow
from arc.control.effective import effective_routines
from arc.control.registry import REGISTRY, lookup, read_raw, write_raw
from arc.control.service import ControlService
from arc.experiments.overlay import arm_config_data
from arc.ingest import market_movers as mm
from arc.ingest.scalp import build_stage2_prompt
from arc.personas.builders import TAPE_MOVERS_NOTE
from arc.routines.config import (
    DEFAULT_ROUTINES_PATH,
    PERSONA_FLAGS,
    RoutinesConfig,
    load_routines,
)
from arc.routines.handlers import JobContext, JobSkippedError, market_movers_source
from arc.store.db import connect
from arc.store.migrate import migrate
from arc.utils.calendar import ET
from tests import experiment_fixtures as fx
from tests import scalp_prompt_golden as golden

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterator, Mapping

REPO = Path(__file__).resolve().parents[1]
GOLDEN = REPO / "tests" / "fixtures" / "scalp" / "stage2_prompt_flag_off.txt"
NOW = dt.datetime(2026, 10, 8, 12, 50, tzinfo=ET)
M30 = dt.timedelta(minutes=30)

# Shapes taken from the live screener (2026-10-08 12:48 ET probe), trimmed.
MOVERS = {
    "gainers": [
        {"change": 0.1555, "percent_change": 194.37, "price": 0.2355, "symbol": "DAAQW"},
        {"change": 0.925, "percent_change": 70.08, "price": 2.245, "symbol": "AIXI"},
        {"change": 11.11, "percent_change": 44.07, "price": 36.305, "symbol": "PCRX"},
        {"change": 0.0704, "percent_change": 26.11, "price": 4.34, "symbol": "PEW.WS"},
        {"change": 3.75, "percent_change": 22.52, "price": 20.4, "symbol": "ALISU"},
        {"change": 7.88, "percent_change": 21.23, "price": 45.0001, "symbol": "BUUU"},
        {"change": 5.0, "percent_change": 9.1, "price": 60.0, "symbol": "SOXL"},
        {"change": 5.0, "percent_change": 4.2, "price": 125.0, "symbol": "NVDA"},
    ],
    "losers": [
        {"change": -2.49, "percent_change": -87.98, "price": 0.3401, "symbol": "SXTC"},
        {"change": -9.0, "percent_change": -12.5, "price": 63.0, "symbol": "CMG"},
    ],
    "last_updated": "2026-10-08T16:48:00.308388003Z",
}
ACTIVES = {
    "last_updated": "2026-10-08T16:48:00.308388003Z",
    "most_actives": [
        {"symbol": "OLB", "trade_count": 408626, "volume": 340421624},
        {"symbol": "SOXS", "trade_count": 281735, "volume": 63589214},
        {"symbol": "INTC", "trade_count": 469730, "volume": 50569876},
        {"symbol": "NVDA", "trade_count": 1178897, "volume": 43451131},
        {"symbol": "MDT", "trade_count": 171339, "volume": 41576164},
    ],
}
SNAPSHOTS = {
    "OLB": {"latestTrade": {"p": 0.5601}, "prevDailyBar": {"c": 0.3794}},
    "SOXS": {"latestTrade": {"p": 8.1}, "prevDailyBar": {"c": 8.4}},
    "INTC": {"latestTrade": {"p": 36.0}, "prevDailyBar": {"c": 34.0}},
    "NVDA": {"latestTrade": {"p": 125.0}, "prevDailyBar": {"c": 120.0}},
    # MDT: no snapshot -> unpriced -> dropped (no_price)
}
NAMES = {
    "SOXL": "Direxion Daily Semiconductor Bull 3X ETF",
    "SOXS": "Direxion Daily Semiconductor Bear 3X ETF",
    "NVDA": "NVIDIA CORP",
    "INTC": "INTEL CORP",
    "BITO": "ProShares Bitcoin ETF",
}


def names(sym: str) -> str | None:
    return NAMES.get(sym)


class FakeAlpaca:
    """``get(url, params, timeout)`` over canned bodies; records every call."""

    def __init__(
        self,
        bodies: Mapping[str, Any] | None = None,
        status: Mapping[str, int] | None = None,
        raises: Mapping[str, Exception] | None = None,
    ) -> None:
        self.bodies = dict(
            bodies
            or {mm.MOVERS_URL: MOVERS, mm.MOST_ACTIVES_URL: ACTIVES, mm.SNAPSHOTS_URL: SNAPSHOTS}
        )
        self.status = dict(status or {})
        self.raises = dict(raises or {})
        self.calls: list[tuple[str, dict[str, str]]] = []

    def __call__(
        self, url: str, params: Mapping[str, str], timeout: float
    ) -> tuple[int, Mapping[str, str], Any]:
        self.calls.append((url, dict(params)))
        if url in self.raises:
            raise self.raises[url]
        code = self.status.get(url, 200)
        return code, {}, self.bodies.get(url) if code < 400 else None  # noqa: PLR2004


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    c = connect(tmp_path / "arc.db")
    migrate(c)
    yield c
    c.close()


def _payload(**kw: Any) -> MarketMoversPayload:
    return mm.build_payload(
        MOVERS,
        ACTIVES,
        mm.price_snapshots(SNAPSHOTS),
        active=kw.pop("active", ["NVDA", "CMG", "AAPL"]),
        names=names,
        now=NOW,
        **kw,
    )


# ---------------------------------------------------------------------------
# Parse + exclusions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sym", "want"),
    [
        ("DAAQW", True),
        ("ALISU", True),
        ("RFAIR", True),
        ("PEW.WS", True),
        ("SKYH.WS", True),
        ("ABC.U", True),
        ("ABC.RT", True),
        ("PEW-WS", True),  # normalised to PEW.WS
        ("SNOW", False),  # 4 letters: never a 5th-letter code
        ("NVDA", False),
        ("BRK.B", False),
        ("GOOGL", False),  # 5th letter L is a class, not W/U/R
        ("SPY", False),
    ],
)
def test_warrant_unit_right(sym: str, want: bool) -> None:
    assert mm.is_warrant_unit_right(sym) is want


def test_parse_and_exclusions() -> None:
    p = _payload()
    assert p.as_of == "2026-10-08T12:48:00-04:00"  # last_updated, ns trimmed, in ET
    assert p.fetched_at == NOW.isoformat()
    assert [r.symbol for r in p.gainers] == ["PCRX", "BUUU", "NVDA"]
    assert [r.symbol for r in p.losers] == ["CMG"]
    assert [r.symbol for r in p.most_actives] == ["INTC", "NVDA"]
    assert p.excluded == {
        "leveraged": 2,  # SOXL (gainers), SOXS (most-actives)
        "no_price": 1,  # MDT (no snapshot)
        "price_below_min": 3,  # AIXI 2.25, SXTC 0.34, OLB 0.56
        "warrant_unit_right": 3,  # DAAQW, PEW.WS, ALISU
    }
    nvda = p.most_actives[1]
    assert (nvda.price, nvda.pct, nvda.volume, nvda.trade_count) == (
        125.0,
        4.17,
        43451131,
        1178897,
    )
    assert nvda.in_active and not p.gainers[0].in_active
    pcrx = p.gainers[0]
    assert (pcrx.price, pcrx.pct, pcrx.volume) == (36.305, 44.07, None)
    # every kept row is >= $3, not a warrant/unit/right, not a leveraged fund
    for r in [*p.gainers, *p.losers, *p.most_actives]:
        assert r.price is not None and r.price >= 3
        assert not mm.is_warrant_unit_right(r.symbol)


def test_min_price_knob_and_bad_rows() -> None:
    movers = {
        "gainers": [
            {"symbol": "PCRX", "price": 36.3, "percent_change": 44},
            {"symbol": "PCRX", "price": 36.3, "percent_change": 44},  # duplicate
            {"symbol": "", "price": 5},  # no symbol
            "junk",
            {"symbol": "XYZ", "price": "n/a", "percent_change": None},  # unparseable
        ],
        "losers": "not a list",
        "last_updated": "garbage",
    }
    p = mm.build_payload(movers, {}, {}, active=[], names=names, now=NOW, min_price=40)
    assert p.as_of == NOW.isoformat()  # unreadable last_updated -> the fetch time
    assert p.gainers == [] and p.losers == [] and p.most_actives == []
    assert p.excluded == {"no_price": 1, "price_below_min": 1}


def test_price_snapshots_fallbacks() -> None:
    got = mm.price_snapshots(
        {
            "A": {"dailyBar": {"c": 10.0}, "prevDailyBar": {"c": 8.0}},  # no trade
            "B": {"latestTrade": {"p": 5.0}},  # no previous close
            "C": {"latestTrade": {"p": 0}},  # zero price
            "D": "junk",
            "brk-b": {"latestTrade": {"p": "NaN"}, "dailyBar": {"c": 400.0}},
        }
    )
    assert got == {
        "A": (10.0, 25.0),
        "B": (5.0, None),
        "C": (None, None),
        "BRK.B": (400.0, None),
    }


@given(
    price=st.floats(min_value=0.0001, max_value=10_000, allow_nan=False),
    floor=st.floats(min_value=0.5, max_value=50, allow_nan=False),
)
def test_price_floor_partition(price: float, floor: float) -> None:
    movers = {"gainers": [{"symbol": "PCRX", "price": price, "percent_change": 1.0}]}
    p = mm.build_payload(movers, {}, {}, active=[], names=names, now=NOW, min_price=floor)
    assert bool(p.gainers) == (price >= floor)


def test_payload_schema_is_strict() -> None:
    with pytest.raises(ValueError, match="Extra inputs"):
        MoverRow.model_validate({"symbol": "A", "in_active": True, "x": 1})
    with pytest.raises(ValueError, match="greater than 0"):
        MoverRow(symbol="A", price=0, in_active=False)
    assert KIND_CATEGORY["market_movers"] is SourceCategory.OPTIONS_FAST


# ---------------------------------------------------------------------------
# Fetch + handler
# ---------------------------------------------------------------------------


def test_fetch_calls_and_metrics() -> None:
    get = FakeAlpaca()
    p, metrics = mm.fetch_market_movers(get, top=20, active=["NVDA"], names=names, now=NOW)
    assert [c[0] for c in get.calls] == [mm.MOVERS_URL, mm.MOST_ACTIVES_URL, mm.SNAPSHOTS_URL]
    assert get.calls[0][1] == {"top": "20"} and get.calls[1][1] == {"top": "20"}
    assert get.calls[2][1] == {"symbols": "OLB,SOXS,INTC,NVDA,MDT"}
    assert metrics == {"raw_gainers": 8, "raw_losers": 2, "raw_most_actives": 5, "priced": 4}
    assert [r.symbol for r in p.most_actives] == ["INTC", "NVDA"]


def test_fetch_snapshot_failure_drops_unpriced_actives_only() -> None:
    get = FakeAlpaca(status={mm.SNAPSHOTS_URL: 500})
    p, metrics = mm.fetch_market_movers(get, top=20, active=[], names=names, now=NOW)
    assert metrics["snapshot_error"].startswith("error: HTTP 500")
    assert p.most_actives == [] and p.excluded["no_price"] == 4  # SOXS is leveraged first
    assert [r.symbol for r in p.gainers] == ["PCRX", "BUUU", "NVDA"]


def test_fetch_without_actives_skips_snapshot() -> None:
    get = FakeAlpaca(bodies={mm.MOVERS_URL: MOVERS, mm.MOST_ACTIVES_URL: {"most_actives": []}})
    _, metrics = mm.fetch_market_movers(get, top=5, active=[], names=names, now=NOW)
    assert len(get.calls) == 2 and metrics["raw_most_actives"] == 0


@pytest.mark.parametrize(
    ("status", "raises", "reason"),
    [
        ({mm.MOVERS_URL: 403}, {}, "forbidden"),
        ({mm.MOST_ACTIVES_URL: 401}, {}, "forbidden"),
        ({mm.MOVERS_URL: 429}, {}, "rate_limited"),
        ({mm.MOVERS_URL: 500}, {}, "error"),
        ({}, {mm.MOST_ACTIVES_URL: ConnectionError("down")}, "error"),
    ],
)
def test_fetch_errors(status: dict[str, int], raises: dict[str, Exception], reason: str) -> None:
    with pytest.raises(mm.MoversFetchError) as exc:
        mm.fetch_market_movers(
            FakeAlpaca(status=status, raises=raises), top=20, active=[], names=names, now=NOW
        )
    assert exc.value.reason == reason


def test_non_mapping_body_is_an_error() -> None:
    get = FakeAlpaca(bodies={mm.MOVERS_URL: ["x"], mm.MOST_ACTIVES_URL: ACTIVES})
    with pytest.raises(mm.MoversFetchError, match="HTTP 200"):
        mm.fetch_market_movers(get, top=20, active=[], names=names, now=NOW)


def _ctx(conn: sqlite3.Connection, **opts: Any) -> JobContext:
    routines = load_routines(DEFAULT_ROUTINES_PATH)
    kind, spec = routines.step("market_movers")
    if opts:
        spec = type(spec).model_validate({**spec.model_dump(exclude_unset=True), **opts})
    return JobContext(
        job="market_movers",
        kind=kind,
        spec=spec,
        run_id="r1",
        chain_run_id=None,
        scheduled_for=NOW,
        now=NOW,
        conn=conn,
        snapshot=ContextStore(conn).snapshot(NOW, kinds=[]),
        routines=routines,
        settings_factory=lambda: ArcSettings(_env_file=None, env="paper"),  # type: ignore[call-arg]
    )


def _stored(conn: sqlite3.Connection, kind: str) -> list[Any]:
    return ContextStore(conn).query(as_of=NOW, kinds=[kind])


def test_handler_writes_market_movers_only(conn: sqlite3.Connection) -> None:
    res = market_movers_source(_ctx(conn), alpaca_get=FakeAlpaca(), names=names)
    entries = _stored(conn, "market_movers")
    assert [(e.subject, e.produced_by) for e in entries] == [("market", "market_movers")]
    p = MarketMoversPayload.model_validate(entries[0].payload)
    assert [r.symbol for r in p.gainers] == ["PCRX", "BUUU", "NVDA"]
    assert res.summary.startswith("3 gainers, 1 losers, 2 most-actives kept · 9 excluded")
    assert res.metrics["excluded"]["warrant_unit_right"] == 3
    assert res.metrics["as_of"] == "2026-10-08T12:48:00-04:00"
    # D56: context only, never a candidate / universe_tier / discovery input
    for kind in ("candidate", "universe_tier", "active_universe"):
        assert _stored(conn, kind) == []
    assert conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0] == 0


def test_handler_marks_active_list_rows(conn: sqlite3.Connection) -> None:
    res = market_movers_source(_ctx(conn), alpaca_get=FakeAlpaca(), names=names)
    # no stored active list -> the core list; NVDA is core
    assert "NVDA" in res.metrics["in_active"]
    assert "on the active list (" in res.summary


def test_handler_top_and_min_price_options(conn: sqlite3.Connection) -> None:
    get = FakeAlpaca()
    market_movers_source(_ctx(conn, top=5, min_price=50), alpaca_get=get, names=names)
    assert get.calls[0][1] == {"top": "5"}
    p = MarketMoversPayload.model_validate(_stored(conn, "market_movers")[0].payload)
    assert [r.symbol for r in p.gainers] == ["NVDA"]


def test_handler_no_key_is_skipped(conn: sqlite3.Connection) -> None:
    with pytest.raises(JobSkippedError, match="no_api_key"):
        market_movers_source(_ctx(conn), alpaca_get=None, names=names)


def test_handler_failed_fetch_writes_nothing(conn: sqlite3.Connection) -> None:
    with pytest.raises(RuntimeError, match="market_movers forbidden"):
        market_movers_source(
            _ctx(conn), alpaca_get=FakeAlpaca(status={mm.MOVERS_URL: 403}), names=names
        )
    assert _stored(conn, "market_movers") == []


def test_handler_default_names_reads_the_cached_master(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    import arc.universe as universe

    def boom(*a: Any, **k: Any) -> Any:
        raise OSError("no master")

    monkeypatch.setattr(universe, "load_symbol_master", boom)
    res = market_movers_source(_ctx(conn), alpaca_get=FakeAlpaca())
    # without names the leveraged check cannot match by name; the other filters hold
    assert res.metrics["excluded"].get("leveraged") is None
    assert res.metrics["excluded"]["warrant_unit_right"] == 3


def test_shipped_source_config() -> None:
    routines = load_routines(DEFAULT_ROUTINES_PATH)
    raw = yaml.safe_load(DEFAULT_ROUTINES_PATH.read_text())["sources"]["market_movers"]
    assert {k: raw[k] for k in ("every", "window", "days", "ttl", "category", "feed")} == {
        "every": "30m",
        "window": "09:30-16:00",
        "days": "trading",
        "ttl": "20m",
        "category": "options_fast",
        "feed": "scalp",
    }
    assert raw["writes"] == ["market_movers"] and raw["top"] == 20
    assert "candidate" not in raw["writes"] and "universe_tier" not in raw["writes"]
    assert routines.context_policy("market_movers", "market_movers").ttl.duration == (
        dt.timedelta(hours=1)
    )
    # the Scalp jobs may read it; no Scout / Research / trending job does (D56)
    readers = {n for n, (_, s) in routines.jobs().items() if "market_movers" in (s.reads or [])}
    assert readers == {"scalp", "scalp.overnight"}


# ---------------------------------------------------------------------------
# The Tape movers block
# ---------------------------------------------------------------------------


def test_block_lists_only_active_and_story_names() -> None:
    p = _payload(active=["NVDA", "CMG"])
    text = mm.movers_block(p, now=NOW, max_age=M30, active=["NVDA", "CMG"], story_tickers=["PCRX"])
    lines = text.splitlines()
    assert lines[0] == "As of 12:48 ET: 3 gainers, 1 losers, 2 most-actives after the filters."
    assert lines[1:] == [
        "PCRX +44.1% $36.30 (named in a story) · gainer #1",
        "NVDA +4.2% $125.00 (active list) · gainer #3 · most active #2 (43.5M sh, 1.2M trades)",
        "CMG -12.5% $63.00 (active list) · loser #1",
    ]
    assert "BUUU" not in text and "INTC" not in text  # neither active nor in a story


def test_block_caps_lines_and_counts() -> None:
    rows = [
        MoverRow(symbol=f"T{i:02d}", price=10.0, pct=float(i), volume=999, in_active=True)
        for i in range(15)
    ]
    p = MarketMoversPayload(
        as_of=NOW.isoformat(), fetched_at=NOW.isoformat(), gainers=rows, most_actives=rows[:1]
    )
    active = [r.symbol for r in rows]
    text = mm.movers_block(p, now=NOW, max_age=M30, active=active, story_tickers=[])
    assert len(text.splitlines()) == 1 + 10
    assert text.splitlines()[1] == (
        "T00 +0.0% $10.00 (active list) · gainer #1 · most active #1 (999 sh, n/a trades)"
    )
    three = mm.movers_block(p, now=NOW, max_age=M30, active=active, story_tickers=[], max_lines=3)
    assert len(three.splitlines()) == 4


def test_block_none_matching() -> None:
    text = mm.movers_block(_payload(), now=NOW, max_age=M30, active=["ZZZ"], story_tickers=[])
    assert text.endswith("\nNo active-list or story names among them.")


def test_block_unpriced_row_and_counts() -> None:
    p = MarketMoversPayload(
        as_of=NOW.isoformat(),
        fetched_at=NOW.isoformat(),
        most_actives=[MoverRow(symbol="AAPL", volume=1_500, trade_count=12, in_active=True)],
    )
    text = mm.movers_block(p, now=NOW, max_age=M30, active=["AAPL"], story_tickers=[])
    assert text.splitlines()[1] == "AAPL n/a $n/a (active list) · most active #1 (2k sh, 12 trades)"


@pytest.mark.parametrize(
    ("payload", "now", "want"),
    [
        (None, NOW, "Tape movers: no fresh info (age none stored)"),
        ("fresh", NOW + dt.timedelta(minutes=31), "Tape movers: no fresh info (age 33m)"),
        ("fresh", NOW + dt.timedelta(hours=9), "Tape movers: no fresh info (age 9h02m)"),
        ("fresh", NOW - dt.timedelta(minutes=10), "Tape movers: no fresh info (age in the future)"),
        ("bad", NOW, "Tape movers: no fresh info (age none stored)"),
    ],
)
def test_block_no_fresh_info(payload: str | None, now: dt.datetime, want: str) -> None:
    p = None
    if payload == "fresh":
        p = _payload()  # as_of 12:48
    elif payload == "bad":
        p = MarketMoversPayload(as_of="not a time", fetched_at=NOW.isoformat())
    assert mm.movers_block(p, now=now, max_age=M30, active=["NVDA"], story_tickers=[]) == want


# ---------------------------------------------------------------------------
# Flag: config, registry, experiment, prompt off/on
# ---------------------------------------------------------------------------


def test_flag_defaults_off_in_the_shipped_config() -> None:
    raw = yaml.safe_load(DEFAULT_ROUTINES_PATH.read_text())
    assert raw["personas"]["scalp_movers_context"] == "off"
    cfg = load_routines(DEFAULT_ROUTINES_PATH)
    assert cfg.scalp_movers_context.enabled is False
    assert cfg.scalp_movers_context.max_lines == 10
    assert "scalp_movers_context" not in cfg.personas  # a switch, not a job
    assert "scalp_movers_context" in PERSONA_FLAGS
    assert RoutinesConfig().scalp_movers_context.enabled is False


@pytest.mark.parametrize(("value", "enabled"), [("on", True), ("off", False), (True, True)])
def test_flag_parses(value: object, enabled: bool) -> None:
    cfg = RoutinesConfig.model_validate({"personas": {"scalp_movers_context": value}})
    assert cfg.scalp_movers_context.enabled is enabled


@pytest.mark.parametrize(
    ("data", "match"),
    [
        ({"personas": {"scalp_movers_context": "maybe"}}, "on | off"),
        ({"scalp_movers_context": {"max_lines": 11}}, "less than or equal"),
        (
            {
                "personas": {"scalp_movers_context": "on"},
                "scalp_movers_context": {"enabled": True},
            },
            "set the switch as personas",
        ),
    ],
)
def test_flag_validated(data: dict[str, Any], match: str) -> None:
    with pytest.raises(ValueError, match=match):
        RoutinesConfig.model_validate(data)


def test_flag_registered_in_registry() -> None:
    t = lookup("personas.scalp_movers_context")
    assert t is REGISTRY["personas.scalp_movers_context"]
    assert lookup("routines.personas.scalp_movers_context") is t
    assert t.choices == ("off", "on")
    assert read_raw(t, {"personas": {"scalp_movers_context": "off"}}) == "off"
    assert read_raw(t, {"personas": {}}) == "off"
    assert write_raw(t, "on", {}) == [(("personas", "scalp_movers_context"), "on")]


def test_slack_override_reaches_the_effective_routines() -> None:
    c = connect(":memory:")
    migrate(c)
    base = ArcSettings(_env_file=None, approver_slack_user_ids=["U0OWNER"])  # type: ignore[call-arg]
    svc = ControlService(c, base=base, now=lambda: NOW)
    r = svc.set("personas.scalp_movers_context", "on", actor="U0OWNER", source="slack")
    assert r.pending is not None  # off -> on needs a confirm
    svc.confirm(r.pending.code, actor="U0OWNER", source="slack")
    assert effective_routines(c).scalp_movers_context.enabled is True


def test_flag_overlay_turns_only_the_flag_on() -> None:
    # D86: the retired tape-movers draft is idea-seed lead S-2; the overlay shape it
    # used still has to turn on exactly this flag in an arm's routines config.
    overlay = {"routines": {"personas": {"scalp_movers_context": "on"}}}
    spec = fx.spec("XP-20", arms={"treatment": {"overlay": overlay}})
    treat = RoutinesConfig.model_validate(arm_config_data(spec, "treatment", "routines"))
    base = load_routines(DEFAULT_ROUTINES_PATH)
    assert treat.scalp_movers_context.enabled is True
    assert treat.model_copy(update={"scalp_movers_context": base.scalp_movers_context}) == base


def test_flag_off_prompt_is_byte_identical_to_main() -> None:
    assert golden.prompt() == GOLDEN.read_text()
    off = build_stage2_prompt(
        golden.digests(),
        golden.settings(),
        golden.DAY,
        open_universe=True,
        ticker_facts="",
        universe=["NVDA", "AAPL", "SPY"],
        movers="",
    )
    assert off == GOLDEN.read_text()


def test_flag_on_prompt_only_adds_the_movers_block() -> None:
    block = mm.movers_block(
        _payload(), now=NOW, max_age=M30, active=["NVDA", "AAPL", "SPY"], story_tickers=["NVDA"]
    )
    on = build_stage2_prompt(
        golden.digests(),
        golden.settings(),
        golden.DAY,
        open_universe=True,
        ticker_facts="",
        universe=["NVDA", "AAPL", "SPY"],
        movers=block,
    )
    head, _, rest = on.partition("\n## Tape movers (Alpaca screener, code-built)\n")
    assert rest, "movers block missing"
    section, _, tail = rest.partition("\n\n## Output format")
    assert head + "\n## Output format" + tail == GOLDEN.read_text()
    assert section.startswith(TAPE_MOVERS_NOTE + "\n" + block)
    assert "never cite it in `sources`" in TAPE_MOVERS_NOTE


# ---------------------------------------------------------------------------
# Scalp run end to end (flag on / off)
# ---------------------------------------------------------------------------


def _seed_docs(conn: sqlite3.Connection) -> None:
    from arc.ingest.store import RawDocRepo

    RawDocRepo(conn).insert(
        source="rss",
        url="https://example.com/pcrx/0",
        published_at=(NOW - dt.timedelta(hours=1)).isoformat(),
        text="PCRX jumps after FDA approval of its pain drug",
        tickers_hint=["PCRX"],
        id="doc-pcrx",
    )


def _write_movers(conn: sqlite3.Connection, at: dt.datetime) -> None:
    ContextStore(conn).write(
        kind="market_movers",
        subject="market",
        payload=_payload(),
        produced_by="market_movers",
        ttl="1h",
        valid_from=at,
        now=at,
    )


def _run(conn: sqlite3.Connection, *, on: bool) -> tuple[Any, list[str]]:
    from arc.ingest.llm import FixtureScalpLLM
    from arc.ingest.scalp import run_scalp

    llm = FixtureScalpLLM([json.dumps({"candidates": [], "scan_summary": "s"})])
    routines = load_routines(
        overrides={("personas", "scalp_movers_context"): "on" if on else "off"}
    )
    settings = ArcSettings(_env_file=None, env="paper")  # type: ignore[call-arg]
    res = run_scalp(conn, settings, llm=llm, now=NOW, run_id="r-mv", routines=routines)
    return res, llm.prompts


def test_scalp_run_flag_on_shows_story_names(conn: sqlite3.Connection) -> None:
    _seed_docs(conn)
    _write_movers(conn, NOW - dt.timedelta(minutes=2))
    res, prompts = _run(conn, on=True)
    assert "## Tape movers (Alpaca screener, code-built)" in prompts[0]
    assert "PCRX +44.1% $36.30 (named in a story) · gainer #1" in prompts[0]
    assert res.movers_blocks and res.movers_blocks[0] in prompts[0]
    kinds = conn.execute("SELECT kinds FROM context_snapshots WHERE run_id = 'r-mv'").fetchall()
    assert any("market_movers" in r[0] for r in kinds)  # the read is audited


def test_scalp_run_flag_on_nothing_stored(conn: sqlite3.Connection) -> None:
    _seed_docs(conn)
    res, prompts = _run(conn, on=True)
    assert "\nTape movers: no fresh info (age none stored)\n" in prompts[0]
    assert res.movers_blocks == ["Tape movers: no fresh info (age none stored)"]


def test_scalp_run_flag_off_has_no_block(conn: sqlite3.Connection) -> None:
    _seed_docs(conn)
    _write_movers(conn, NOW - dt.timedelta(minutes=2))
    res, prompts = _run(conn, on=False)
    assert "Tape movers" not in prompts[0] and res.movers_blocks == []
    kinds = conn.execute("SELECT kinds FROM context_snapshots WHERE run_id = 'r-mv'").fetchall()
    assert not any("market_movers" in r[0] for r in kinds)
