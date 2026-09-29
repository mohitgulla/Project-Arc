"""The D32 daily options order budget (E6.5): pure core, count I/O, ladder guard, CLI."""

from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal as D
from typing import TYPE_CHECKING

import pytest
from hypothesis import given
from hypothesis import settings as h_settings
from hypothesis import strategies as st

from arc.broker.base import BrokerOrderRef
from arc.budget import (
    HARD_CEILING,
    OrderBudget,
    OrderBudgetConfig,
    RestrictiveConfig,
    Tier,
    budget_state,
    can_submit,
    count_orders,
    current_budget,
    effective_cooldown,
    effective_improvement_steps,
    restrictive_floors,
    tier_settings,
    worst_case_attempts,
)
from arc.budget.orders import REFUSED_DETAIL_PREFIX, tier_for
from arc.config import ArcSettings
from arc.context.ttl import to_db
from arc.execution.ladder import ExecStatus
from arc.models import OrderState
from arc.store.db import connect
from arc.store.migrate import migrate
from arc.store.repos import OrderRepo
from arc.utils.calendar import ET
from tests import test_execution_ladder as L

if TYPE_CHECKING:
    import sqlite3

NOW = dt.datetime(2026, 10, 9, 10, 0, tzinfo=ET)
DAY = NOW.date()
CFG = OrderBudgetConfig()  # 200 / 100 / 25


def settings(**kw: object) -> ArcSettings:
    return ArcSettings(_env_file=None, **kw)  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# Pure core
# ---------------------------------------------------------------------------


def test_defaults_match_d32() -> None:
    assert CFG.daily_max == HARD_CEILING == 200
    assert CFG.restrict_at == 100 and CFG.close_reserve == 25 and CFG.open_limit == 175
    r = CFG.restrictive
    assert (r.director_max_shortlist, r.max_new_opens_per_loop) == (1, 1)
    assert (r.min_net_ev_multiplier, r.min_pop_delta_pp) == (1.5, 5.0)
    assert (r.max_improvement_steps, r.dedupe_cooldown_multiplier) == (2, 2.0)
    assert OrderBudgetConfig.from_settings(settings()) == CFG


def test_hard_ceiling_is_code_level() -> None:
    with pytest.raises(ValueError, match="less than or equal to 200"):
        OrderBudgetConfig(daily_max=201)
    with pytest.raises(ValueError, match="less than or equal to 200"):
        settings(order_budget_daily_max=201)
    with pytest.raises(ValueError, match="must not exceed the open limit"):
        settings(order_budget_restrict_at=180)
    with pytest.raises(ValueError, match="must be below"):
        OrderBudgetConfig(daily_max=20, close_reserve=20)


@pytest.mark.parametrize(
    ("used", "tier", "opens", "total"),
    [
        (0, Tier.NORMAL, 175, 200),
        (99, Tier.NORMAL, 76, 101),
        (100, Tier.RESTRICTIVE, 75, 100),
        (174, Tier.RESTRICTIVE, 1, 26),
        (175, Tier.OPENS_EXHAUSTED, 0, 25),
        (199, Tier.OPENS_EXHAUSTED, 0, 1),
        (200, Tier.EXHAUSTED, 0, 0),
        (230, Tier.EXHAUSTED, 0, 0),  # dashboard orders pushed it past the cap
    ],
)
def test_tier_boundaries(used: int, tier: Tier, opens: int, total: int) -> None:
    b = budget_state(used, CFG, day=DAY)
    assert b.tier is tier and (b.remaining_opens, b.remaining_total) == (opens, total)
    assert b.summary() == f"orders today {used}/200 ({tier.value})"
    assert b.brief() == {"used": used, "limit": 200, "tier": tier.value}
    assert tier.opens_allowed is (used < 175) and tier.closes_allowed is (used < 200)
    assert tier.restricted is (used >= 100)


def test_budget_state_rejects_negative() -> None:
    with pytest.raises(ValueError, match=">= 0"):
        budget_state(-1, CFG, day=DAY)


def test_can_submit_validation() -> None:
    with pytest.raises(ValueError, match="open' or 'close"):
        can_submit(0, CFG, kind="hedge")
    with pytest.raises(ValueError, match=">= 1"):
        can_submit(0, CFG, kind="open", attempts=0)


@given(
    attempts=st.lists(
        st.tuples(st.sampled_from(["open", "close"]), st.integers(1, 7)), max_size=400
    ),
    restrict_at=st.integers(0, 150),
    close_reserve=st.integers(0, 50),
    daily_max=st.integers(1, 200),
)
@h_settings(max_examples=300)
def test_property_used_never_exceeds_caps(
    attempts: list[tuple[str, int]], restrict_at: int, close_reserve: int, daily_max: int
) -> None:
    """For any sequence of attempts admitted by ``can_submit``, ``used`` never exceeds
    ``daily_max`` and the open count never exceeds ``daily_max - close_reserve``."""
    if close_reserve >= daily_max:
        close_reserve = daily_max - 1
    restrict_at = min(restrict_at, daily_max - close_reserve)
    cfg = OrderBudgetConfig(
        daily_max=daily_max, restrict_at=restrict_at, close_reserve=close_reserve
    )
    used = opens = 0
    for kind, n in attempts:
        if can_submit(used, cfg, kind=kind, attempts=n):
            used += n
            if kind == "open":
                opens += n
        assert used <= cfg.daily_max
        assert opens <= cfg.open_limit
        b = budget_state(used, cfg, day=DAY)
        assert b.tier is tier_for(used, cfg)
        assert b.remaining_total == cfg.daily_max - used
    # once opens are exhausted a close may still go, until the cap
    if used == cfg.open_limit and cfg.close_reserve:
        assert can_submit(used, cfg, kind="close") and not can_submit(used, cfg, kind="open")


def test_tier_adjusted_steps_and_cooldown() -> None:
    s = settings()  # 3 steps
    assert effective_improvement_steps(s, Tier.NORMAL) == 3
    assert effective_improvement_steps(s, Tier.RESTRICTIVE) == 2
    assert (
        worst_case_attempts(s, Tier.NORMAL) == 4 and worst_case_attempts(s, Tier.RESTRICTIVE) == 3
    )
    assert tier_settings(s, Tier.NORMAL) is s
    assert tier_settings(s, Tier.RESTRICTIVE).execution_improvement_steps == 2
    # a lower base than the cap is left alone
    one = settings(execution_improvement_steps=1)
    assert tier_settings(one, Tier.RESTRICTIVE) is one
    base = dt.timedelta(minutes=30)
    assert effective_cooldown(base, Tier.NORMAL, CFG.restrictive) == base
    assert effective_cooldown(base, Tier.RESTRICTIVE, CFG.restrictive) == dt.timedelta(hours=1)


def test_restrictive_floors() -> None:
    r = RestrictiveConfig()
    # $4 width debit vertical: max loss 150, max gain 250 -> breakeven PoP 0.375 (+5pp = 0.425)
    f = restrictive_floors(
        r,
        base_net_ev_floor=0.0,
        base_pop_floor=0.0,
        round_trip_cost=6.0,
        max_loss=150,
        max_gain=250,
    )
    assert f.net_ev_floor == pytest.approx(9.0) and f.pop_floor == pytest.approx(0.425)
    assert f.passes(9.0, 0.43) == (True, "")
    ok, why = f.passes(8.0, 0.40)
    assert not ok and "net EV" in why and "PoP" in why
    assert f.passes(None, 0.5)[0] is False  # fails closed without a managed model
    # unbounded max gain (long option): only the base floor moves
    g = restrictive_floors(
        r,
        base_net_ev_floor=2.0,
        base_pop_floor=0.30,
        round_trip_cost=1.0,
        max_loss=300,
        max_gain=None,
    )
    assert g.net_ev_floor == pytest.approx(3.0) and g.pop_floor == pytest.approx(0.35)
    assert (
        restrictive_floors(
            r, base_net_ev_floor=0, base_pop_floor=0.99, round_trip_cost=0, max_loss=1, max_gain=1
        ).pop_floor
        == 1.0
    )


def test_config_only_change_moves_the_tier() -> None:
    """``restrict_at: 50`` changes behaviour with no code change (card acceptance)."""
    s = settings(order_budget_restrict_at=50)
    cfg = OrderBudgetConfig.from_settings(s)
    assert budget_state(49, cfg, day=DAY).tier is Tier.NORMAL
    assert budget_state(50, cfg, day=DAY).tier is Tier.RESTRICTIVE
    assert budget_state(50, CFG, day=DAY).tier is Tier.NORMAL  # default config: still normal


# ---------------------------------------------------------------------------
# count_orders: local rows, broker cross-check, reservations
# ---------------------------------------------------------------------------


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = connect(":memory:")
    migrate(c)
    return c


def _seed_proposal(conn: sqlite3.Connection, phash: str) -> None:
    from arc.store.repos import CandidateRepo, ProposalRepo

    cid = CandidateRepo(conn).insert(
        ticker="SPY", stance="bullish", catalyst_type="t", confidence=0.7, id=f"c-{phash}"
    )
    ProposalRepo(conn).insert(
        candidate_id=cid,
        proposal_hash=phash,
        structure_json="{}",
        thesis="t",
        quant_json="{}",
        sizing_json="{}",
        expires_at=NOW.isoformat(),
        ticker="SPY",
    )


def _order(
    conn: sqlite3.Connection, phash: str, k: int, at: dt.datetime, refused: bool = False
) -> None:
    repo = OrderRepo(conn)
    oid = repo.create(
        proposal_hash=phash, client_order_id=f"{phash}.s{k}.{at.isoformat()}", created_at=to_db(at)
    )
    if refused:
        repo.transition(order_id=oid, to_state=OrderState.GATED, event_at=to_db(at))
        repo.transition(order_id=oid, to_state=OrderState.APPROVED, event_at=to_db(at))
        repo.transition(
            order_id=oid,
            to_state=OrderState.CANCELLED,
            detail=f"{REFUSED_DETAIL_PREFIX}: halted",
            event_at=to_db(at),
        )


class ListingBroker:
    def __init__(self, refs: list[BrokerOrderRef], fail: bool = False) -> None:
        self.refs, self.fail = refs, fail
        self.calls: list[dt.datetime] = []

    def option_orders_since(self, since: dt.datetime) -> list[BrokerOrderRef]:
        self.calls.append(since)
        if self.fail:
            msg = "rate limited"
            raise RuntimeError(msg)
        return self.refs


def _ref(i: int, at: dt.datetime | None = None) -> BrokerOrderRef:
    return BrokerOrderRef(broker_order_id=f"b{i}", mleg=True, submitted_at=at)


def test_count_local_rows_by_et_day(conn: sqlite3.Connection) -> None:
    _seed_proposal(conn, "p1")
    _order(conn, "p1", 0, NOW)
    _order(conn, "p1", 1, NOW + dt.timedelta(hours=5))
    _order(conn, "p1", 2, NOW - dt.timedelta(days=1))  # yesterday
    _order(
        conn, "p1", 3, dt.datetime(2026, 10, 9, 23, 30, tzinfo=ET)
    )  # 03:30Z next day, still ET 10-09
    _order(conn, "p1", 4, NOW, refused=True)  # never reached the broker
    c = count_orders(conn, None, DAY)
    assert (c.local, c.broker, c.reserved, c.used, c.mismatch) == (3, None, 0, 3, False)


def test_count_takes_max_of_local_and_broker(conn: sqlite3.Connection) -> None:
    """A dashboard-placed order counts: the broker lists 3 while the DB knows 2."""
    _seed_proposal(conn, "p1")
    _order(conn, "p1", 0, NOW)
    _order(conn, "p1", 1, NOW)
    b = ListingBroker([_ref(1, NOW), _ref(2, NOW), _ref(3, NOW + dt.timedelta(hours=1))])
    c = count_orders(conn, b, DAY)
    assert (c.local, c.broker, c.used, c.mismatch) == (2, 3, 3, True)
    assert b.calls == [dt.datetime.combine(DAY, dt.time(0), tzinfo=ET)]
    # orders the broker stamps outside the ET day are not today's
    b2 = ListingBroker([_ref(1, NOW - dt.timedelta(days=1)), _ref(2, None)])
    assert count_orders(conn, b2, DAY).broker == 1
    # a local count above the broker's wins (the broker list is a floor, not the truth)
    b3 = ListingBroker([_ref(1, NOW)])
    c3 = count_orders(conn, b3, DAY)
    assert (c3.local, c3.broker, c3.used) == (2, 1, 2)


def test_count_survives_broker_failure(conn: sqlite3.Connection) -> None:
    _seed_proposal(conn, "p1")
    _order(conn, "p1", 0, NOW)
    c = count_orders(conn, ListingBroker([], fail=True), DAY)  # type: ignore[arg-type]
    assert (c.local, c.broker, c.used) == (1, None, 1)

    class Plain:  # no option_orders_since at all
        pass

    assert count_orders(conn, Plain(), DAY).broker is None  # type: ignore[arg-type]


def test_count_reserves_working_ladders(conn: sqlite3.Connection) -> None:
    from arc.store.execution import ExecutionRepo

    for ph in ("w1", "w2", "done"):
        _seed_proposal(conn, ph)
    ex = ExecutionRepo(conn)
    for ph, attempts in (("w1", 1), ("w2", 0), ("done", 2)):
        ex.start(
            proposal_hash=ph, kind="open", token_version="arc2", band_lo=D(1), band_hi=D(2),
            max_steps=3, contracts=1, now=NOW,
        )  # fmt: skip
        for _ in range(attempts):
            ex.attempt(ph)
    ex.finish("done", status="cancelled", now=NOW)
    # w1: 4 attempts - 1 sent = 3 left; w2: 4 left; done: finished, not reserved
    c = count_orders(conn, None, DAY)
    assert (c.local, c.reserved, c.used) == (0, 7, 7)
    # the caller's own ladder is excluded from the reservation
    assert count_orders(conn, None, DAY, exclude_proposal_hash="w1").reserved == 4
    b = current_budget(conn, None, settings(), now=NOW)
    assert isinstance(b, OrderBudget)
    assert (b.used, b.local, b.broker, b.reserved, b.day) == (7, 0, None, 7, DAY)


# ---------------------------------------------------------------------------
# Ladder guard: the budget runs out mid-ladder
# ---------------------------------------------------------------------------


def _fill_budget(conn: sqlite3.Connection, n: int, at: dt.datetime) -> None:
    _seed_proposal(conn, "filler")
    for k in range(n):
        _order(conn, "filler", k, at)


def _open_proposal(conn: sqlite3.Connection):
    p = L.S.proposal(expires_at=L.NOW + dt.timedelta(minutes=20))
    L.seed(conn, p)
    return p


def test_ladder_stops_when_budget_runs_out_mid_ladder(conn: sqlite3.Connection) -> None:
    """Opens cap = 175. With 173 used, attempts s0 and s1 go out; s2 would be the 176th."""
    _fill_budget(conn, 173, L.NOW)
    b = L.ScriptedBroker(
        [["new"], ["new"], ["new", "filled"]],
        cancel_scripts={0: [("canceled", 0)], 1: [("canceled", 0)]},
        fills={2: (2, "-0.79")},
    )
    out = L.run(conn, b, p=_open_proposal(conn))
    assert out.status is ExecStatus.CANCELLED
    assert out.detail == "order budget exhausted (175 of 175 open orders used today)"
    assert len(out.attempts) == 2 and len(b.orders) == 2
    ex = L.ExecutionRepo(conn).get(out.proposal_hash)
    assert ex is not None and ex["status"] == "cancelled" and ex["attempts"] == 2
    assert ex["detail"] == out.detail
    assert ("no_trade", "order:budget_exhausted") in L.journal(conn)
    assert count_orders(conn, None, L.NOW.date()).used == 175  # never exceeded


def test_ladder_refuses_first_attempt_when_exhausted(conn: sqlite3.Connection) -> None:
    _fill_budget(conn, 175, L.NOW)
    b = L.ScriptedBroker([["new", "filled"]], fills={0: (2, "-0.85")})
    out = L.run(conn, b, p=_open_proposal(conn))
    assert out.status is ExecStatus.CANCELLED and not b.orders and not out.attempts
    assert "order budget exhausted" in out.detail


def _close_proposal(conn: sqlite3.Connection, minutes: int):
    from arc.gate import proposal_hash
    from arc.store.repos import ProposalRepo

    p = L.S.proposal(
        thesis="exit", limit_price=D("0.40"), expires_at=L.NOW + dt.timedelta(minutes=minutes)
    )
    ProposalRepo(conn).insert(
        candidate_id=p.candidate_id, proposal_hash=proposal_hash(p),
        structure_json=p.structure.model_dump_json(), thesis=p.thesis,
        quant_json=p.quant.model_dump_json(), sizing_json=p.sizing.model_dump_json(),
        expires_at=p.expires_at.isoformat(), ticker="SPY", kind="close",
    )  # fmt: skip
    return p


def test_ladder_close_may_use_the_reserve(conn: sqlite3.Connection) -> None:
    """A close at 175 used still goes (cap 200); at 200 it does not."""
    # open first (1 order), then fill the budget up to the open limit
    b = L.ScriptedBroker([["filled"]], fills={0: (2, "-0.85")})
    opened = L.run(conn, b)
    assert opened.structure_id is not None
    _fill_budget(conn, 174, L.NOW)
    assert count_orders(conn, None, L.NOW.date()).used == 175
    band = L.PriceBand(lo=D("0.40"), hi=D("0.40"), max_steps=0)
    cp = _close_proposal(conn, 20)
    b2 = L.ScriptedBroker([["filled"]], fills={0: (2, "0.40")})
    out = L.run(
        conn, b2, p=cp, decision=L.gated(cp, band=band), kind="close",
        structure_id=opened.structure_id,
    )  # fmt: skip
    assert out.status is ExecStatus.FILLED and len(b2.orders) == 1
    # now 176 used; push to 200 and a second close is refused before the broker
    _seed_proposal(conn, "filler2")
    for k in range(24):
        _order(conn, "filler2", k, L.NOW)
    assert count_orders(conn, None, L.NOW.date()).used == 200
    cp2 = _close_proposal(conn, 21)
    b3 = L.ScriptedBroker([["filled"]], fills={0: (2, "0.40")})
    out2 = L.run(
        conn, b3, p=cp2, decision=L.gated(cp2, band=band), kind="close",
        structure_id=opened.structure_id,
    )  # fmt: skip
    assert out2.status is ExecStatus.CANCELLED and not b3.orders
    assert out2.detail == "order budget exhausted (200 of 200 close orders used today)"


def test_ladder_budget_is_config_driven(conn: sqlite3.Connection) -> None:
    _fill_budget(conn, 3, L.NOW)
    b = L.ScriptedBroker([["new", "filled"]], fills={0: (2, "-0.85")})
    cfg = L.cfg(order_budget_daily_max=5, order_budget_close_reserve=2, order_budget_restrict_at=1)
    out = L.run(conn, b, p=_open_proposal(conn), config=cfg)
    assert out.status is ExecStatus.CANCELLED and not b.orders
    assert out.detail == "order budget exhausted (3 of 3 open orders used today)"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_status(tmp_path, capsys: pytest.CaptureFixture[str]) -> None:
    from arc.cli import main

    db = tmp_path / "arc.db"
    c = connect(str(db))
    migrate(c)
    _seed_proposal(c, "p1")
    _order(c, "p1", 0, NOW)
    _order(c, "p1", 1, NOW)
    c.close()
    rc = main(["budget", "status", "--db", str(db), "--day", DAY.isoformat(), "--no-broker"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "orders today 2/200 (normal)" in out
    assert "used 2 = max(local 2, broker n/a) + reserved 0" in out
    assert "opens stop at 175 (close reserve 25)" in out
    rc = main(
        ["budget", "status", "--db", str(db), "--day", DAY.isoformat(), "--no-broker", "--json"]
    )
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert (
        data["used"] == 2
        and data["tier"] == "normal"
        and data["broker_source"].startswith("skipped")
    )
