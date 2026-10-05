"""D50 (E8.8g): trade direction from leg sides and strikes, and its Tower wiring."""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import sys
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient

from arc.models import Leg, LegIntent, Stance, Structure, StructureKind
from arc.structures import legs_direction, structure_stance
from arc.tower.api import create_app
from arc.tower.data import connect_ro, direction_of
from arc.tower.data_overview import load_positions
from arc.tower.data_performance import structure_label
from arc.tower.data_trades import TradeFilters, load_trades
from arc.utils.calendar import ET

if TYPE_CHECKING:
    import sqlite3

REPO = Path(__file__).resolve().parent.parent
NOW = dt.datetime(2026, 9, 28, 15, 40, tzinfo=ET)
L, S = LegIntent.LONG, LegIntent.SHORT


def _leg(occ: str, side: LegIntent, ratio: int = 1) -> Leg:
    return Leg(occ_symbol=occ, side=side, ratio=ratio, premium=Decimal("1.00"))


C = lambda k: f"SPY261120C{k * 1000:08d}"  # noqa: E731 - table shorthand
P = lambda k: f"SPY261120P{k * 1000:08d}"  # noqa: E731

# ---------------------------------------------------------------------------
# the rule
# ---------------------------------------------------------------------------

CASES = [
    ("long call", [_leg(C(660), L)], Stance.BULLISH),
    ("long put", [_leg(P(660), L)], Stance.BEARISH),
    ("lone short call", [_leg(C(660), S)], Stance.NEUTRAL),
    ("call debit vertical (long low)", [_leg(C(660), L), _leg(C(670), S)], Stance.BULLISH),
    ("call credit vertical (long high)", [_leg(C(670), L), _leg(C(660), S)], Stance.BEARISH),
    ("put debit vertical (long high)", [_leg(P(930), L), _leg(P(855), S)], Stance.BEARISH),
    ("put credit vertical (long low)", [_leg(P(625), L), _leg(P(675), S)], Stance.BULLISH),
    ("legs in either order", [_leg(C(670), S), _leg(C(660), L)], Stance.BULLISH),
    (
        "iron condor",
        [_leg(P(600), L), _leg(P(610), S), _leg(C(700), S), _leg(C(710), L)],
        Stance.NEUTRAL,
    ),
    ("straddle", [_leg(C(660), L), _leg(P(660), L)], Stance.NEUTRAL),
    ("strangle", [_leg(C(680), L), _leg(P(640), L)], Stance.NEUTRAL),
    (
        "butterfly",
        [_leg(C(650), L), _leg(C(660), S, 2), _leg(C(670), L)],
        Stance.NEUTRAL,
    ),
    (
        "calendar",
        [_leg("SPY261120C00660000", S), _leg("SPY261218C00660000", L)],
        Stance.NEUTRAL,
    ),
    ("mixed call + put", [_leg(C(660), L), _leg(P(640), S)], Stance.NEUTRAL),
    ("same strike", [_leg(C(660), L), _leg(C(660), S)], Stance.NEUTRAL),
    ("ratio spread", [_leg(C(660), L), _leg(C(670), S, 2)], Stance.NEUTRAL),
    ("two longs", [_leg(C(660), L), _leg(C(670), L)], Stance.NEUTRAL),
    ("unparsable OCC", [_leg("NOT-AN-OCC", L)], Stance.NEUTRAL),
]


@pytest.mark.parametrize(("name", "legs", "want"), CASES, ids=[c[0] for c in CASES])
def test_legs_direction_table(name: str, legs: list[Leg], want: Stance) -> None:
    assert legs_direction(legs) is want, name


def test_no_legs_is_none() -> None:
    assert legs_direction([]) is None
    assert direction_of(None) is None and direction_of("[]") is None and direction_of([]) is None


def test_reversed_side_close_matches_its_open() -> None:
    """NFLX put debit vertical open (long 67.5 / short 62.5) and its close (sides reversed,
    a credit net): the close reads bearish once flipped, like its open, not bullish."""
    open_ = [_leg("NFLX261120P00067500", L), _leg("NFLX261120P00062500", S)]
    close = [_leg("NFLX261120P00067500", S), _leg("NFLX261120P00062500", L)]
    assert legs_direction(open_) is Stance.BEARISH
    assert legs_direction(close, closing=True) is Stance.BEARISH
    # read naively (no flip) the close leg set is a bullish credit put vertical
    assert legs_direction(close) is Stance.BULLISH


def test_closing_long_call_flips_back() -> None:
    assert legs_direction([_leg(C(660), S)], closing=True) is Stance.BULLISH


def _st(kind: StructureKind, legs: list[Leg], net: str) -> Structure:
    return Structure(
        kind=kind,
        legs=legs,
        net_debit_credit=Decimal(net),
        max_loss=Decimal("100"),
        dte=30,
    )


def test_structure_stance_matches_the_old_net_sign_rule_on_well_formed_structures() -> None:
    """portfolio_context's previous rule read the net debit sign; on every structure the
    builders make (debit/credit agrees with strike geometry) the leg rule gives the same."""
    old = {
        (StructureKind.VERTICAL_DEBIT, "c"): Stance.BULLISH,
        (StructureKind.VERTICAL_CREDIT, "c"): Stance.BEARISH,
        (StructureKind.VERTICAL_DEBIT, "p"): Stance.BEARISH,
        (StructureKind.VERTICAL_CREDIT, "p"): Stance.BULLISH,
    }
    legs = {
        (StructureKind.VERTICAL_DEBIT, "c"): ([_leg(C(660), L), _leg(C(670), S)], "4"),
        (StructureKind.VERTICAL_CREDIT, "c"): ([_leg(C(670), L), _leg(C(660), S)], "-4"),
        (StructureKind.VERTICAL_DEBIT, "p"): ([_leg(P(670), L), _leg(P(660), S)], "4"),
        (StructureKind.VERTICAL_CREDIT, "p"): ([_leg(P(660), L), _leg(P(670), S)], "-4"),
    }
    for key, want in old.items():
        lg, net = legs[key]
        assert structure_stance(_st(key[0], lg, net)) is want
    assert structure_stance(_st(StructureKind.LONG_CALL, [_leg(C(660), L)], "5")) is Stance.BULLISH
    assert structure_stance(_st(StructureKind.LONG_PUT, [_leg(P(660), L)], "5")) is Stance.BEARISH
    ic = [_leg(P(600), L), _leg(P(610), S), _leg(C(700), S), _leg(C(710), L)]
    assert structure_stance(_st(StructureKind.IRON_CONDOR, ic, "-2")) is Stance.NEUTRAL


def test_portfolio_context_reuses_the_moved_rule() -> None:
    import arc.pipeline.portfolio_context as pc

    assert pc.structure_stance is structure_stance


def test_direction_of_malformed_leg_is_neutral() -> None:
    assert direction_of([{"occ_symbol": "SPY261120C00660000", "side": "sideways"}]) == "neutral"
    assert direction_of(json.dumps([{"occ_symbol": "SPY261120C00660000", "side": "long"}])) == (
        "bullish"
    )


@pytest.mark.parametrize(
    ("kind", "label"),
    [
        ("vertical_debit", "Debit Vertical"),
        ("vertical_credit", "Credit Vertical"),
        ("long_call", "Long Call"),
        ("long_put", "Long Put"),
        ("iron_condor", "Iron Condor"),
        ("covered_call", "Covered Call"),
        ("cash_secured_put", "Cash-Secured Put"),
        ("broken_wing_fly", "Broken Wing Fly"),
    ],
)
def test_performance_structure_labels_title_case(kind: str, label: str) -> None:
    assert structure_label(kind) == label


# ---------------------------------------------------------------------------
# Tower wiring on the fixture DB
# ---------------------------------------------------------------------------


def _load(name: str, path: Path):  # noqa: ANN202 - module loaded by path
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(name, mod)
    spec.loader.exec_module(mod)
    return mod


fixture = _load("tower_fixture_db", REPO / "scripts" / "tower_fixture_db.py")


@pytest.fixture(scope="module")
def fx_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return fixture.build(tmp_path_factory.mktemp("fx") / "arc.db", NOW)


@pytest.fixture
def conn(fx_db: Path):
    c = connect_ro(fx_db)
    yield c
    c.close()


def _trades(conn: sqlite3.Connection):  # noqa: ANN202
    return load_trades(conn, TradeFilters(), now=NOW, page=1, size=200).items


def test_every_trade_row_carries_a_direction(conn: sqlite3.Connection) -> None:
    rows = _trades(conn)
    assert rows
    for r in rows:
        assert r.direction in {"bullish", "bearish", "neutral"}, (r.ticker, r.kind)
    by = {(r.ticker, r.kind): r.direction for r in rows}
    assert by[("SPY", "open")] == "bullish"  # call debit vertical, long the lower strike
    assert by[("QQQ", "open")] == "bearish"  # put debit vertical, long the higher strike
    assert by[("NVDA", "open")] == "bullish"  # long call


def test_close_row_direction_equals_its_open(conn: sqlite3.Connection) -> None:
    rows = _trades(conn)
    opens = {r.structure_id: r for r in rows if r.kind == "open" and r.structure_id}
    closes = [r for r in rows if r.kind == "close"]
    assert closes
    paired = 0
    for c in closes:
        exited = c.closes_structure_id
        if exited is None:
            exited = conn.execute(
                "SELECT structure_id FROM executions WHERE proposal_hash = ?", (c.proposal_hash,)
            ).fetchone()
            exited = exited[0] if exited else None
        if exited in opens:
            assert c.direction == opens[exited].direction, c.ticker
            paired += 1
    assert paired == len(closes)


def test_every_position_row_carries_a_direction(conn: sqlite3.Connection) -> None:
    rows = load_positions(conn, now=NOW, status="all", stale_after=dt.timedelta(minutes=15)).items
    assert rows
    assert all(p.direction in {"bullish", "bearish", "neutral"} for p in rows)
    assert {p.ticker: p.direction for p in rows}["QQQ"] == "bearish"


def test_api_trades_and_positions_serve_direction(fx_db: Path) -> None:
    with TestClient(create_app(fx_db, clock=lambda: NOW)) as client:
        trades = client.get("/api/trades", params={"size": 200}).json()["items"]
        assert trades and all("direction" in t and t["direction"] for t in trades)
        pos = client.get("/api/positions", params={"status": "all"}).json()["items"]
        assert pos and all(p["direction"] for p in pos)
        ov = client.get("/api/overview").json()
        assert all(m["direction"] for m in ov["movers"])
