"""E11.4 (D73): the expiry guard (pure rules), DNE guards, opens-only halts.

Expiry Fri 2026-11-20 unless stated. Every test injects ``now``.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal as D
from typing import TYPE_CHECKING, Any

import pytest
from hypothesis import given
from hypothesis import strategies as hs

from arc.config import ArcEnv, ArcSettings
from arc.execution.instructions import do_not_exercise
from arc.exits.expiry import (
    BrokerActivityView,
    ExpiryGuard,
    classify_expiry,
    closing_window,
    dne_candidates,
    expiry_cutoff,
    leg_shares,
    may_attempt,
    not_flat,
)
from arc.journal.reasons import ReasonCode
from arc.models import LegIntent
from arc.pricing.bs import OptionKind
from arc.store.db import connect
from arc.store.migrate import migrate
from arc.structures import credit_vertical, debit_vertical, long_call, parse_occ
from arc.utils.calendar import ET

if TYPE_CHECKING:
    import sqlite3

    from arc.models import Structure

EXP = dt.date(2026, 11, 20)  # Friday
G = ExpiryGuard()


def at(d: dt.date, h: int, m: int = 0) -> dt.datetime:
    return dt.datetime(d.year, d.month, d.day, h, m, tzinfo=ET)


def settings(**kw: object) -> ArcSettings:
    base: dict[str, object] = {"_env_file": None, "gate_secret": "g" * 40, "env": "paper"}
    base.update(kw)
    return ArcSettings(**base)  # type: ignore[arg-type]


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = connect(":memory:")
    migrate(c)
    return c


def call_debit(exp: dt.date = EXP) -> Structure:
    return debit_vertical(
        "call", "IWM", exp, long_strike=100, long_premium="3.00",
        short_strike=105, short_premium="1.00", as_of=dt.date(2026, 10, 1),
    )  # fmt: skip


def put_debit(exp: dt.date = EXP) -> Structure:
    """Long 270 put / short 268 put (a bear put debit)."""
    return debit_vertical(
        "put", "IWM", exp, long_strike=270, long_premium="3.00",
        short_strike=268, short_premium="2.00", as_of=dt.date(2026, 10, 1),
    )  # fmt: skip


def occ(st: Structure, side: LegIntent) -> str:
    leg = next(leg for leg in st.legs if leg.side == side)
    return parse_occ(leg.occ_symbol).format()


# ---------------------------------------------------------------------------
# window / cutoff
# ---------------------------------------------------------------------------


def test_window_flat_by_dte_default_1() -> None:
    tue, wed, thu, fri = (dt.date(2026, 11, d) for d in (17, 18, 19, 20))
    assert not closing_window(EXP, tue, G).in_window
    assert closing_window(EXP, tue, G).attempts_allowed == 1
    w = closing_window(EXP, wed, G)
    assert w.in_window and w.dte == 2 and w.attempts_allowed == 4
    w = closing_window(EXP, thu, G)
    assert w.in_window and w.flat_by == thu and not w.expiry_day
    w = closing_window(EXP, fri, G)
    assert w.expiry_day and w.cutoff == at(fri, 15, 15)
    assert expiry_cutoff(fri, G) == at(fri, 15, 15)
    # an early-close expiry (Fri 2026-11-27, 13:00 close): cutoff 12:15
    assert expiry_cutoff(dt.date(2026, 11, 27), G) == at(dt.date(2026, 11, 27), 12, 15)
    # after expiry nothing is proposed: the reconcile settles it
    assert closing_window(EXP, dt.date(2026, 11, 23), G).attempts_allowed == 0


def test_window_for_monday_expiry_keeps_a_retry_session_before_flat_by() -> None:
    mon = dt.date(2026, 11, 23)
    w = closing_window(mon, dt.date(2026, 11, 20), G)  # Fri: flat-by (Sun -> Fri)
    assert w.flat_by == dt.date(2026, 11, 20) and w.in_window
    assert closing_window(mon, dt.date(2026, 11, 19), G).in_window  # Thu: the retry day
    assert not closing_window(mon, dt.date(2026, 11, 18), G).in_window


def test_attempts_capped_per_day_and_outside_window_unchanged() -> None:
    far = closing_window(EXP, dt.date(2026, 11, 10), G)  # DTE 10
    now = at(dt.date(2026, 11, 10), 11)
    assert may_attempt(0, far, now) and not may_attempt(1, far, now)
    near = closing_window(EXP, dt.date(2026, 11, 19), G)  # DTE 1
    now = at(dt.date(2026, 11, 19), 11)
    assert [may_attempt(k, near, now) for k in range(6)] == [True] * 4 + [False] * 2


def test_expiry_day_cutoff_1515_et_no_proposals_after() -> None:
    w = closing_window(EXP, EXP, G)
    assert may_attempt(0, w, at(EXP, 15, 14))
    assert not may_attempt(0, w, at(EXP, 15, 16))


def test_not_flat_from_15_minutes_before_the_flat_by_close() -> None:
    thu = dt.date(2026, 11, 19)
    w = closing_window(EXP, thu, G)
    assert not not_flat(w, at(thu, 15, 44))
    assert not_flat(w, at(thu, 15, 45))
    assert not not_flat(closing_window(EXP, dt.date(2026, 11, 18), G), at(thu, 9, 30))
    assert not_flat(closing_window(EXP, EXP, G), at(EXP, 9, 31))  # expiry day: still not flat


@given(hs.integers(min_value=0, max_value=5), hs.integers(min_value=0, max_value=40))
def test_window_property_in_window_means_more_attempts(flat_by: int, days: int) -> None:
    g = ExpiryGuard(flat_by_dte=flat_by)
    w = closing_window(EXP, EXP - dt.timedelta(days=days), g)
    assert w.dte == days
    assert w.flat_by <= EXP
    if days <= flat_by + 1:
        assert w.in_window and w.attempts_allowed == g.attempts_per_day
    if not w.in_window:
        assert w.attempts_allowed == 1


# ---------------------------------------------------------------------------
# DNE
# ---------------------------------------------------------------------------


def test_dne_near_money_only() -> None:
    st = long_call("IWM", EXP, strike=100, premium="2.00", as_of=dt.date(2026, 10, 1))
    o = parse_occ(st.legs[0].occ_symbol).format()
    assert dne_candidates(st, D("100.30"), G) == [o]
    assert dne_candidates(st, D("99.70"), G) == [o]
    assert dne_candidates(st, D("103"), G) == []
    assert dne_candidates(st, None, G) == []  # no spot: fail safe (Alpaca decides)
    assert dne_candidates(st, D("100.30"), ExpiryGuard(dne="never")) == []
    assert dne_candidates(st, D("150"), ExpiryGuard(dne="all_longs")) == [o]
    # a short leg never gets a DNE
    cs = credit_vertical(
        "put", "IWM", EXP, short_strike=100, short_premium="2", long_strike=99,
        long_premium="1.5", as_of=dt.date(2026, 10, 1),
    )  # fmt: skip
    assert dne_candidates(cs, D("100.10"), G) == []
    assert dne_candidates(cs, D("99.10"), G) == [occ(cs, LegIntent.LONG)]


class DneBroker:
    def __init__(self, fail: bool = False) -> None:
        self.sent: list[str] = []
        self.fail = fail

    def do_not_exercise(self, occ: str) -> None:
        if self.fail:
            msg = "dne requests are not accepted after 15:30"
            raise RuntimeError(msg)
        self.sent.append(occ)


def _dne(conn: sqlite3.Connection, broker: Any, **kw: Any) -> Any:
    args: dict[str, Any] = {
        "occ": "IWM261120C00100000", "ticker": "IWM", "structure_id": "os-1",
        "settings": settings(), "now": at(EXP, 15, 16), "expiry_day": EXP,
        "cutoff": at(EXP, 15, 15), "held_long": True, "dry_run": False,
    }  # fmt: skip
    args.update(kw)
    return do_not_exercise(conn, broker, **args)


def test_dne_sent_only_when_every_guard_holds(conn: sqlite3.Connection) -> None:
    b = DneBroker()
    assert _dne(conn, b).sent and b.sent == ["IWM261120C00100000"]
    codes = [r[0] for r in conn.execute("SELECT reason_code FROM decisions")]
    assert codes == [ReasonCode.EXIT_DNE.value]


@pytest.mark.parametrize(
    ("kw", "why"),
    [
        ({"settings": settings().model_copy(update={"env": ArcEnv.LIVE})}, "paper only"),
        ({"dry_run": True}, "dry run"),
        ({"now": at(dt.date(2026, 11, 19), 15, 16)}, "not the expiration day"),
        ({"now": at(EXP, 15, 14)}, "outside"),
        ({"now": at(EXP, 15, 26)}, "outside"),
        ({"held_long": False}, "not a long leg"),
    ],
)
def test_dne_refused_outside_paper_or_dry_run(
    conn: sqlite3.Connection, kw: dict[str, Any], why: str
) -> None:
    b = DneBroker()
    out = _dne(conn, b, **kw)
    assert not out.sent and why in out.reason and b.sent == []
    assert conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 1  # journaled


def test_dne_broker_rejection_is_journaled_not_raised(conn: sqlite3.Connection) -> None:
    out = _dne(conn, DneBroker(fail=True))
    assert not out.sent and "broker rejected" in out.reason
    assert _dne(conn, None).reason.startswith("refused: the broker adapter")


# ---------------------------------------------------------------------------
# classification (pure)
# ---------------------------------------------------------------------------


def test_leg_shares_signs() -> None:
    assert leg_shares(LegIntent.LONG, OptionKind.CALL, 1, 2) == 200
    assert leg_shares(LegIntent.SHORT, OptionKind.PUT, 1, 2) == 200
    assert leg_shares(LegIntent.LONG, OptionKind.PUT, 1, 1) == -100
    assert leg_shares(LegIntent.SHORT, OptionKind.CALL, 1, 1) == -100


def test_classify_from_activities() -> None:
    st = call_debit()
    lo, sh = occ(st, LegIntent.LONG), occ(st, LegIntent.SHORT)
    acts = [
        BrokerActivityView(id="a1", activity_type="OPEXC", symbol=lo, qty=D(1)),
        BrokerActivityView(id="a2", activity_type="OPEXP", symbol=sh, qty=D(1)),
    ]
    c = classify_expiry(structure_id="s", structure=st, contracts=1, held=set(), shares=100,
                        activities=acts, settle=D("103"))  # fmt: skip
    assert c.confident and [e.kind for e in c.legs] == ["exercised", "expired"]
    assert c.close_net == D("-3") and c.shares_net == 100 and c.basis == D("103")


def test_classify_inferred_and_ambiguous() -> None:
    st = put_debit()
    # settle 267: both ITM -> long put -100, short put +100: net 0 shares
    c = classify_expiry(structure_id="s", structure=st, contracts=1, held=set(), shares=0,
                        activities=None, settle=D("267"))  # fmt: skip
    assert c.confident and c.shares_net == 0 and c.close_net == D("-2")
    # settle 269: only the long 270 put ITM -> -100 expected; +100 seen -> not confident
    c = classify_expiry(structure_id="s", structure=st, contracts=1, held=set(), shares=100,
                        activities=None, settle=D("269"))  # fmt: skip
    assert not c.confident and c.close_net is None
    # a leg still held is never classified
    c = classify_expiry(structure_id="s", structure=st, contracts=1,
                        held={occ(st, LegIntent.LONG)}, shares=0, activities=None,
                        settle=D("280"))  # fmt: skip
    assert not c.confident
    # no settle: never confident
    c = classify_expiry(structure_id="s", structure=st, contracts=1, held=set(), shares=0,
                        activities=None, settle=None)  # fmt: skip
    assert not c.confident


@given(
    hs.sampled_from(["call", "put"]),
    hs.integers(min_value=1, max_value=5),
    hs.decimals(min_value=D("80"), max_value=D("120"), places=2),
)
def test_classify_property_footprint_round_trips(kind: str, n: int, settle: D) -> None:
    """Whatever the settle, the share footprint of the ITM legs classifies confidently."""
    st = debit_vertical(
        kind, "IWM", EXP, long_strike=100, long_premium="3", short_strike=105 if kind == "call"
        else 95, short_premium="1", as_of=dt.date(2026, 10, 1),
    )  # fmt: skip
    shares = 0
    for leg in st.legs:
        o = parse_occ(leg.occ_symbol)
        itm = (settle > o.strike) if o.kind == OptionKind.CALL else (settle < o.strike)
        if itm:
            shares += leg_shares(leg.side, o.kind, leg.ratio, n)
    c = classify_expiry(structure_id="s", structure=st, contracts=n, held=set(), shares=shares,
                        activities=None, settle=settle)  # fmt: skip
    assert c.confident and c.shares_net == shares
    assert c.close_net is not None and c.close_net <= 0  # a debit vertical never costs to close
