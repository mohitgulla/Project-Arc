"""E10.2: the account profile's day-trade rule (gate) and its counters (market)."""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from decimal import Decimal as D

import pytest

from arc.account_profiles import DayTradeRule, DayTrades
from arc.config import ArcSettings
from arc.gate import rules as R
from arc.gate.inputs import Portfolio
from arc.journal.reasons import ReasonCode
from arc.pipeline.market import day_trades_used, opened_today_symbols
from arc.store.migrate import migrate
from arc.utils.calendar import ET
from tests.test_gate import LP, SP, acct, make_proposal


def _cfg(rule: DayTradeRule = DayTradeRule.PATTERN_DAY_TRADER, **kw: object) -> ArcSettings:
    s = ArcSettings(_env_file=None, account_profile="margin")  # type: ignore[call-arg]
    assert s.account_profile_spec is not None
    s.account_profile_spec = s.account_profile_spec.model_copy(
        update={"day_trades": DayTrades(rule=rule, **kw)}  # type: ignore[arg-type]
    )
    return s


OPENED = Portfolio(legs={SP: -2, LP: 2}, opened_today=frozenset({SP}))


def test_default_profile_has_no_day_trade_rule() -> None:
    s = ArcSettings(_env_file=None, account_profile="margin")  # type: ignore[call-arg]
    assert s.profile.day_trades.rule is DayTradeRule.NONE
    assert R.check_day_trades(make_proposal(), acct(day_trades_used=9), OPENED, s) == []


@pytest.mark.parametrize(
    ("used", "equity", "portfolio", "fires"),
    [
        (2, "10000", OPENED, False),  # 3rd day trade fits max 3
        (3, "10000", OPENED, True),  # 4th exceeds
        (3, "30000", OPENED, False),  # above min_equity: no limit
        (3, "10000", Portfolio(legs={SP: -2, LP: 2}), False),  # not opened today
        (None, "10000", OPENED, False),  # count unknown: skipped
    ],
)
def test_day_trade_rule(used: int | None, equity: str, portfolio: Portfolio, fires: bool) -> None:
    a = acct(day_trades_used=used, equity=D(equity), last_equity=D(equity))
    out = R.check_day_trades(make_proposal(), a, portfolio, _cfg())
    assert bool(out) is fires
    if fires:
        assert out[0].code is R.RuleCode.DAY_TRADES


def test_reason_code_matches_rule_code() -> None:
    assert ReasonCode.GATE_DAY_TRADES.value == f"gate:{R.RuleCode.DAY_TRADES.value}"


def _db() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    migrate(c)
    c.execute("PRAGMA foreign_keys = OFF")  # bare open_structures rows, no proposals
    return c


def _structure(conn: sqlite3.Connection, sid: str, opened: dt.datetime, closed: dt.datetime | None):
    from arc.context.ttl import to_db

    cols = {r[1]: r for r in conn.execute("pragma table_info(open_structures)")}
    row: dict[str, object] = {}
    for name, info in cols.items():
        if info[3] and info[4] is None and not info[5]:  # NOT NULL, no default, not pk
            row[name] = f"{sid}-{name}"
    row.update(
        id=sid,
        structure_json=json.dumps({"legs": [{"occ_symbol": f"{sid}-L"}]}),
        opened_at=to_db(opened),
        closed_at=to_db(closed) if closed else None,
    )
    for k in ("contracts", "qty"):
        if k in cols and info_int(cols[k]):
            row[k] = 1
    keys = ", ".join(row)
    conn.execute(
        f"INSERT INTO open_structures ({keys}) VALUES ({', '.join('?' * len(row))})",  # noqa: S608
        tuple(row.values()),
    )


def info_int(info: tuple) -> bool:
    return "INT" in str(info[2]).upper() or "REAL" in str(info[2]).upper()


def test_day_trade_counters_from_open_structures() -> None:
    c = _db()
    fri = dt.datetime(2026, 10, 9, 10, 0, tzinfo=ET)
    _structure(c, "a", fri, fri + dt.timedelta(hours=2))  # day trade today
    _structure(c, "b", fri - dt.timedelta(days=3), fri - dt.timedelta(days=3, hours=-1))  # Tue
    _structure(c, "c", fri - dt.timedelta(days=10), fri - dt.timedelta(days=10, hours=-1))
    _structure(c, "d", fri - dt.timedelta(days=1), fri)  # overnight: not a day trade
    _structure(c, "e", fri, None)  # opened today, still open
    day = fri.date()
    assert day_trades_used(c, day, 5) == 2  # a + b; c is outside 5 sessions
    assert day_trades_used(c, day, 1) == 1
    assert opened_today_symbols(c, day) == {"a-L", "e-L"}
