"""E15.3 (D69): shared-account limits for experiment arms.

* the per-arm D32 budget slice and the account-level guard,
* the file-locked Alpaca rate bucket shared by every arm process (fairness across
  three simulated processes, submit priority, a 429 empties it),
* ``cross_arm_conflict``: the pre-check before the broker and the Alpaca 403
  ``40310000`` mapping, both journalled and counted in the arm report,
* ``arc budget status --arm``.
"""

from __future__ import annotations

import datetime as dt
import json
import multiprocessing
from decimal import Decimal as D
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
import requests
from alpaca.common.exceptions import APIError
from hypothesis import given
from hypothesis import strategies as st
from structlog.testing import capture_logs

from arc.broker.base import BrokerOrderRef, MlegLeg, MlegOrder
from arc.broker.http import TimeoutSession
from arc.broker.ratelimit import AccountRateBucket, bucket_path, request_kind
from arc.budget import OrderBudgetConfig, Tier, budget_state, count_orders
from arc.config import ArcSettings
from arc.control.effective import effective_settings, shared_arm_settings
from arc.execution.ladder import ExecStatus
from arc.execution.resolve import WASH_TRADE_CODE, is_wash_trade_rejection
from arc.execution.submission import RefusalCode, SubmitRefused, parse_order_id
from arc.experiments.arms import arm_stores
from arc.experiments.conflict import find_conflict, open_legs
from arc.experiments.multi import cross_arm_conflicts
from arc.journal.reasons import REASON_LABELS, ReasonCode
from arc.store.db import connect
from arc.store.migrate import migrate
from arc.utils.calendar import ET
from tests import test_execution_ladder as L
from tests import test_experiment_shared_attribution as A

if TYPE_CHECKING:
    import sqlite3

NOW = L.NOW
DAY = NOW.date()

#: the real 403 body from the 2026-10-09 stress test
WASH_BODY = {
    "code": 40310000,
    "message": "potential wash trade detected. use complex orders",
    "reject_reason": "opposite side market/stop order exists",
}


def settings(**kw: object) -> ArcSettings:
    return ArcSettings(_env_file=None, **kw)  # type: ignore[call-arg]


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = connect(":memory:")
    migrate(c)
    return c


# ---------------------------------------------------------------------------
# Budget split
# ---------------------------------------------------------------------------


def test_arm_slice_defaults_to_200_over_max_parallel_arms() -> None:
    s = OrderBudgetConfig().arm_slice(per_arm=0, max_parallel_arms=10)
    # 20 = 200 // 10; restrictive at floor(100 * 0.1); reserve ceil(25 * 0.1)
    assert (s.daily_max, s.restrict_at, s.close_reserve, s.open_limit) == (20, 10, 3, 17)
    assert s.account_max == 200
    s4 = OrderBudgetConfig().arm_slice(per_arm=0, max_parallel_arms=4)
    assert (s4.daily_max, s4.restrict_at, s4.close_reserve) == (50, 25, 7)
    s16 = OrderBudgetConfig().arm_slice(per_arm=0, max_parallel_arms=16)
    assert (s16.daily_max, s16.restrict_at, s16.close_reserve) == (12, 6, 2)


def test_arm_slice_per_arm_override_and_never_above_the_account() -> None:
    s = OrderBudgetConfig().arm_slice(per_arm=40, max_parallel_arms=10)
    assert (s.daily_max, s.restrict_at, s.close_reserve) == (40, 20, 5)
    cap = OrderBudgetConfig(daily_max=100, restrict_at=50, close_reserve=10)
    assert cap.arm_slice(per_arm=200, max_parallel_arms=2).daily_max == 100
    with pytest.raises(ValueError, match="needed"):
        cap.arm_slice(per_arm=-1, max_parallel_arms=2)
    with pytest.raises(ValueError, match="needed"):
        cap.arm_slice(per_arm=0, max_parallel_arms=0)
    with pytest.raises(ValueError, match="less than or equal to 200"):
        settings(order_budget_per_arm=201)


@given(
    daily=st.integers(2, 200),
    reserve_frac=st.floats(0, 0.9),
    restrict_frac=st.floats(0, 1),
    per_arm=st.integers(0, 200),
    arms=st.integers(1, 16),
)
def test_arm_slice_is_always_a_valid_proportional_budget(
    daily: int, reserve_frac: float, restrict_frac: float, per_arm: int, arms: int
) -> None:
    reserve = min(daily - 1, int(daily * reserve_frac))
    restrict = int((daily - reserve) * restrict_frac)
    base = OrderBudgetConfig(daily_max=daily, restrict_at=restrict, close_reserve=reserve)
    s = base.arm_slice(per_arm=per_arm, max_parallel_arms=arms)
    assert 1 <= s.daily_max <= daily and s.account_max == daily
    assert s.close_reserve < s.daily_max and s.restrict_at <= s.open_limit
    # the tiers scale with the same fraction (reserve rounded toward closes)
    f = s.daily_max / daily
    assert s.restrict_at <= restrict * f + 1e-9
    assert s.close_reserve >= min(reserve * f, s.daily_max - 1) - 1e-9


def test_shared_arm_settings_cut_the_d32_fields() -> None:
    s = shared_arm_settings(settings())
    assert (s.order_budget_daily_max, s.order_budget_restrict_at) == (20, 10)
    assert (s.order_budget_close_reserve, s.order_budget_account_max) == (3, 200)
    s = shared_arm_settings(settings(order_budget_per_arm=60))
    assert (s.order_budget_daily_max, s.order_budget_close_reserve) == (60, 8)


def test_effective_settings_slice_only_a_shared_arm_store(tmp_path: Path) -> None:
    conn, stores = A._shared_setup(tmp_path)
    ctl = effective_settings(conn)
    assert (ctl.order_budget_daily_max, ctl.order_budget_account_max) == (200, None)
    arm = A._db(stores["t2"])
    s = effective_settings(arm)
    # config/experiments.yaml: max_parallel_arms 10 -> 20 / 10 / 3 + the 200 account guard
    assert (s.order_budget_daily_max, s.order_budget_restrict_at) == (20, 10)
    assert (s.order_budget_close_reserve, s.order_budget_account_max) == (3, 200)
    assert OrderBudgetConfig.from_settings(s).account_max == 200


# ---------------------------------------------------------------------------
# Arm count + account guard
# ---------------------------------------------------------------------------


def _ref(coid: str, k: int) -> BrokerOrderRef:
    return BrokerOrderRef(
        broker_order_id=f"o{k}",
        client_order_id=coid,
        mleg=True,
        submitted_at=NOW - dt.timedelta(minutes=k % 50),
    )


class _SplitBroker:
    """``option_orders_split`` as a shared-mode VirtualBroker answers it."""

    def __init__(self, mine: int, others: int) -> None:
        self.mine = [_ref(f"arc2.1.tok{k}.s0", k) for k in range(mine)]
        self.others = [_ref(f"arc2.2.tok{k}.s0", 1000 + k) for k in range(others)]

    def option_orders_split(self, since: dt.datetime) -> tuple[list[Any], list[Any]]:
        return self.mine, [*self.mine, *self.others]


def test_arm_counts_its_tagged_orders_and_the_account_guard_overrides() -> None:
    c = A._db()
    cfg = OrderBudgetConfig().arm_slice(per_arm=0, max_parallel_arms=10)
    n = count_orders(c, _SplitBroker(4, 50), DAY, account_max=cfg.account_max)
    assert (n.broker, n.account, n.used, n.account_blocked) == (4, 54, 4, False)
    assert budget_state(n.used, cfg, day=DAY, count=n).tier is Tier.NORMAL
    # the whole account reaches 200 while this arm used only 4: every tier reads exhausted
    n = count_orders(c, _SplitBroker(4, 196), DAY, account_max=cfg.account_max)
    assert n.account_blocked and n.used == 200
    b = budget_state(n.used, cfg, day=DAY, count=n)
    assert b.tier is Tier.EXHAUSTED and b.remaining_total == 0
    assert (b.account, b.account_max) == (200, 200)
    # control (no account_max) and a plain broker are unchanged
    n = count_orders(c, _SplitBroker(4, 196), DAY)
    assert not n.account_blocked and n.used == 4


def test_an_unreadable_split_falls_back_to_the_local_count() -> None:
    class Down:
        def option_orders_split(self, since: dt.datetime) -> Any:
            raise requests.ConnectionError("down")

    n = count_orders(A._db(), Down(), DAY, account_max=200)
    assert (n.broker, n.account, n.account_blocked) == (None, None, False)


def test_virtual_broker_splits_the_account_orders_by_arm_code(tmp_path: Path) -> None:
    conn, stores = A._shared_setup(tmp_path)

    class Acct(A._Account):
        def option_orders_since(self, since: dt.datetime) -> list[BrokerOrderRef]:
            return [_ref(o["client_order_id"], k) for k, o in enumerate(A.LIVE["orders"])]

    acct = Acct(A.LIVE["fills"], A.LIVE["orders"], A.LIVE_POS, A.CASH_T0)
    t2 = A._vb(conn, stores["t2"], acct, "XP-30:t2")  # code 1
    mine, everyone = t2.option_orders_split(NOW)
    assert everyone is not None and len(everyone) == len(A.LIVE["orders"])
    assert mine and all(parse_order_id(o.client_order_id).arm_code == "1" for o in mine)
    assert len(mine) < len(everyone)


def test_ladder_stops_on_the_account_guard_with_its_own_reason(conn: sqlite3.Connection) -> None:
    class Guarded(L.ScriptedBroker, _SplitBroker):
        arm_code = "1"

        def __init__(self) -> None:
            L.ScriptedBroker.__init__(self, [["new", "filled"]], fills={0: (2, "-0.85")})
            _SplitBroker.__init__(self, 2, 198)

        def cross_arm_conflict(self, order: Any) -> None:
            return None

    b = Guarded()
    cfg = shared_arm_settings(L.cfg())
    out = L.run(conn, b, config=cfg)
    assert out.status is ExecStatus.CANCELLED and not b.orders
    assert out.detail == (
        "shared account order cap reached (200 of 200 option orders on the account today)"
    )
    assert ("no_trade", "order:budget_exhausted") in L.journal(conn)


def test_ladder_arm_slice_runs_out_before_the_account(conn: sqlite3.Connection) -> None:
    class Arm(L.ScriptedBroker, _SplitBroker):
        arm_code = "1"

        def __init__(self) -> None:
            L.ScriptedBroker.__init__(self, [["new", "filled"]], fills={0: (2, "-0.85")})
            _SplitBroker.__init__(self, 17, 40)

        def cross_arm_conflict(self, order: Any) -> None:
            return None

    out = L.run(conn, Arm(), config=shared_arm_settings(L.cfg()))
    assert out.detail == "order budget exhausted (17 of 17 open orders used today)"


# ---------------------------------------------------------------------------
# Rate bucket
# ---------------------------------------------------------------------------


class _Clock:
    def __init__(self, t: float = 1_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += s


def test_bucket_path_uses_the_key_last4_only(tmp_path: Path) -> None:
    assert bucket_path("PKABCDEF1234", tmp_path) == tmp_path / "alpaca-1234.bucket"
    assert bucket_path("", tmp_path).name == "alpaca-none.bucket"
    assert request_kind("POST", "https://paper-api.alpaca.markets/v2/orders") == "submit"
    assert request_kind("post", "https://x/v2/orders/") == "submit"
    assert request_kind("GET", "https://x/v2/orders?status=all") == "read"
    assert request_kind("DELETE", "https://x/v2/orders/abc") == "read"
    assert request_kind("POST", "https://x/v2/positions/X/do-not-exercise") == "read"


def test_bucket_defaults_180_per_min_with_a_small_burst(tmp_path: Path) -> None:
    assert settings().alpaca_rate_per_min == 180
    b = AccountRateBucket(tmp_path / "b", 180, clock=_Clock())
    assert (b.capacity, b.read_reserve) == (18, 4)
    with pytest.raises(ValueError, match=">= 1"):
        AccountRateBucket(tmp_path / "b", 0)


def test_three_processes_share_one_bucket_fairly(tmp_path: Path) -> None:
    """Three bucket objects (three arm processes) on one file, a shared clock, 60 s of
    round-robin demand: the account stays under rate + burst and each gets ~1/3."""
    clock = _Clock()
    path = tmp_path / "alpaca-EXP1.bucket"
    procs = [AccountRateBucket(path, 180, clock=clock) for _ in range(3)]
    got = [0, 0, 0]
    start = clock()
    step = 0
    while clock() - start < 60.0:
        for i in range(3):
            j = (i + step) % 3  # rotate who asks first
            if procs[j].try_take("read") == 0.0:
                got[j] += 1
        clock.sleep(0.05)
        step += 1
    total = sum(got)
    assert total <= 180 + procs[0].capacity
    assert total >= 180 - procs[0].capacity - procs[0].read_reserve
    assert max(got) - min(got) <= 2, got


def test_submit_has_priority_over_reads(tmp_path: Path) -> None:
    clock = _Clock()
    b = AccountRateBucket(tmp_path / "b", 180, clock=clock, sleep=clock.sleep)
    reads = 0
    while b.try_take("read") == 0.0:
        reads += 1
    assert reads == b.capacity - b.read_reserve  # reads stop at the reserve
    wait = b.try_take("read")
    assert wait > 0
    for _ in range(b.read_reserve):
        assert b.try_take("submit") == 0.0  # submits still go
    assert b.try_take("submit") > 0
    waited = b.acquire("submit")  # blocks on the injected sleep until one is due
    assert 0 < waited <= 1 / b.per_second + 1e-9


def _drain(path: str, out: Any) -> None:  # runs in a child process
    b = AccountRateBucket(path, 180, clock=lambda: 1_000.0)  # frozen: no refill
    n = 0
    while b.try_take("submit") == 0.0:
        n += 1
    out.put(n)


def test_bucket_never_double_spends_across_real_processes(tmp_path: Path) -> None:
    path = tmp_path / "alpaca-proc.bucket"
    AccountRateBucket(path, 180, clock=lambda: 1_000.0).state()  # create, full
    ctx = multiprocessing.get_context("spawn")
    q = ctx.Queue()
    ps = [ctx.Process(target=_drain, args=(str(path), q)) for _ in range(3)]
    for p in ps:
        p.start()
    for p in ps:
        p.join(60)
        assert p.exitcode == 0
    taken = [q.get(timeout=5) for _ in ps]
    assert sum(taken) == 18  # exactly the burst: no token taken twice, none lost


def test_a_429_empties_the_bucket_and_logs_its_state(tmp_path: Path) -> None:
    clock = _Clock()
    bucket = AccountRateBucket(tmp_path / "b", 180, clock=clock, sleep=clock.sleep)

    class Adapter(requests.adapters.BaseAdapter):
        def __init__(self, code: int) -> None:
            super().__init__()
            self.code = code

        def send(self, request: Any, **kw: Any) -> requests.Response:
            r = requests.Response()
            r.status_code = self.code
            r.request = request
            r._content = b"{}"  # noqa: SLF001
            return r

        def close(self) -> None:
            pass

    s = TimeoutSession(5.0, 15.0, bucket)
    s.mount("https://", Adapter(200))
    s.request("GET", "https://paper-api.alpaca.markets/v2/account")
    assert bucket.state().tokens == pytest.approx(bucket.capacity - 1)
    s.mount("https://", Adapter(429))
    with capture_logs() as logs:
        s.request("POST", "https://paper-api.alpaca.markets/v2/orders")
    (ev,) = [e for e in logs if e["event"] == "alpaca.rate_limited"]
    assert ev["kind"] == "submit" and ev["path"] == "/orders"
    assert ev["bucket"]["capacity"] == 18 and ev["bucket"]["rate_per_min"] == 180
    assert bucket.state().tokens == 0.0  # every process backs off


def test_alpaca_broker_takes_the_bucket_on_its_session(tmp_path: Path) -> None:
    from unittest.mock import patch

    from arc.broker.alpaca_paper import AlpacaPaperBroker

    with patch.dict("os.environ", {"ALPACA_API_KEY": "PKXX9876", "ALPACA_SECRET_KEY": "s"}):
        b = AlpacaPaperBroker(api_key="PKXX9876", secret_key="s")
    assert b.key_id == "PKXX9876" and b.rate_bucket is None
    bucket = AccountRateBucket(bucket_path(b.key_id, tmp_path), 180)
    b.use_rate_bucket(bucket)
    assert b._client._session.bucket is bucket  # noqa: SLF001
    assert bucket.path.name == "alpaca-9876.bucket"


# ---------------------------------------------------------------------------
# cross_arm_conflict
# ---------------------------------------------------------------------------

LONG, SHORT = "SPY261120P00565000", "SPY261120P00570000"


def _order(*legs: tuple[str, str], qty: int = 1) -> MlegOrder:
    return MlegOrder(
        legs=[MlegLeg(symbol=s, side=side) for s, side in legs],
        qty=qty,
        limit_price=D("-0.80"),
        client_order_id="arc2.1.x.s0",
    )


def _raw(coid: str, status: str, *legs: tuple[str, str]) -> dict[str, Any]:
    return {
        "id": coid,
        "client_order_id": coid,
        "status": status,
        "legs": [{"symbol": s, "side": side} for s, side in legs],
    }


def test_open_legs_skips_own_and_terminal_orders() -> None:
    orders = [
        _raw("arc2.2.a.s0", "new", (SHORT, "buy"), (LONG, "sell")),
        _raw("arc2.1.b.s0", "new", (SHORT, "sell")),  # own
        _raw("arc2.3.c.s0", "filled", (SHORT, "buy")),  # terminal
        {"client_order_id": "x", "status": "accepted", "symbol": LONG, "side": "OrderSide.BUY"},
    ]
    assert open_legs(orders, exclude_code="1") == [
        (SHORT, "buy", "arc2.2.a.s0"),
        (LONG, "sell", "arc2.2.a.s0"),
        (LONG, "buy", "x"),
    ]


def test_find_conflict_against_another_arms_open_order() -> None:
    other = [(SHORT, "buy", "arc2.2.abcdef.s0")]
    why = find_conflict(_order((SHORT, "sell")), other_open=other, other_held={}, own_held={})
    assert why is not None and "working buy order" in why
    # same side is not a wash trade
    assert (
        find_conflict(_order((SHORT, "buy")), other_open=other, other_held={}, own_held={}) is None
    )


def test_find_conflict_against_another_arms_position_but_not_own_close() -> None:
    held = {"XP-30:t3": {LONG: D(2)}}
    why = find_conflict(_order((LONG, "sell")), other_open=[], other_held=held, own_held={})
    assert why is not None and "XP-30:t3's long 2" in why
    # closing this arm's own long of 2 never conflicts, even though t3 holds it too
    own = {LONG: D(2)}
    assert (
        find_conflict(_order((LONG, "sell"), qty=2), other_open=[], other_held=held, own_held=own)
        is None
    )
    # selling more than its own long would cross t3's long
    assert find_conflict(_order((LONG, "sell"), qty=3), other_open=[], other_held=held,
                         own_held=own) is not None  # fmt: skip
    # buying against another arm's short
    short = {"XP-30:t3": {SHORT: D(-1)}}
    assert find_conflict(_order((SHORT, "buy")), other_open=[], other_held=short, own_held={})
    assert find_conflict(_order((SHORT, "sell")), other_open=[], other_held=short,
                         own_held={}) is None  # fmt: skip


def test_virtual_broker_pre_check_reads_the_shared_account(tmp_path: Path) -> None:
    conn, stores = A._shared_setup(tmp_path)
    acct = A._Account(A.LIVE["fills"], A.LIVE["orders"], A.LIVE_POS, A.CASH_T0)
    t2 = A._vb(conn, stores["t2"], acct, "XP-30:t2")  # holds IREN/HOOD; t3 holds NVDA
    nvda = "NVDA261106C00230000"
    why = t2.cross_arm_conflict(_order((nvda, "sell")))
    assert why is not None and "XP-30:t3" in why
    assert t2.cross_arm_conflict(_order(("HOOD261023P00111000", "sell"), qty=2)) is None
    acct.orders_ = [*A.LIVE["orders"], _raw("arc2.2.w.s0", "new", ("AAPL261120C00250000", "buy"))]
    assert t2.cross_arm_conflict(_order(("AAPL261120C00250000", "sell"))) is not None
    assert t2.cross_arm_conflict(_order(("AAPL261120C00250000", "buy"))) is None


class _ArmBroker(L.ScriptedBroker):
    arm_code = "1"

    def __init__(self, scripts: list[list[str]], conflict: str | Exception | None, **kw: Any):
        super().__init__(scripts, **kw)
        self.conflict = conflict
        self.checked: list[MlegOrder] = []

    def cross_arm_conflict(self, order: MlegOrder) -> str | None:
        self.checked.append(order)
        if isinstance(self.conflict, Exception):
            raise self.conflict
        return self.conflict


def _decisions(conn: sqlite3.Connection, code: str) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT payload FROM decisions WHERE reason_code = ?", (code,)).fetchall()
    return [json.loads(r[0]) for r in rows]


def test_pre_check_refuses_without_a_broker_call_and_is_counted(
    conn: sqlite3.Connection,
) -> None:
    b = _ArmBroker([["filled"]], "sell SPY… is opposite to another arm's working buy order")
    out = L.run(conn, b)
    assert out.status is ExecStatus.REJECTED and not b.orders and len(b.checked) == 1
    assert parse_order_id(b.checked[0].client_order_id).arm_code == "1"
    (row,) = _decisions(conn, ReasonCode.CROSS_ARM_CONFLICT.value)
    assert row["source"] == "pre_check" and row["refusal"] == "cross_arm_conflict"
    assert L.order_states(conn, ".s0")[-1] == "cancelled"
    # refused before the broker: never counts against the order budget
    assert count_orders(conn, None, DAY).local == 0
    assert cross_arm_conflicts(conn, None, NOW - dt.timedelta(hours=1)) == 1
    assert REASON_LABELS[ReasonCode.CROSS_ARM_CONFLICT].startswith("Order refused")


def test_pre_check_fails_closed_when_the_account_cannot_be_read(
    conn: sqlite3.Connection,
) -> None:
    b = _ArmBroker([["filled"]], requests.ConnectionError("down"))
    out = L.run(conn, b)
    assert out.status is ExecStatus.REJECTED and not b.orders
    (row,) = _decisions(conn, ReasonCode.ORDER_REFUSED.value)
    assert row["refusal"] == RefusalCode.CROSS_ARM_UNCHECKED.value


def test_control_and_dedicated_brokers_skip_the_pre_check(conn: sqlite3.Connection) -> None:
    b = _ArmBroker([["filled"]], "would conflict", fills={0: (2, "-0.85")})
    b.arm_code = None  # type: ignore[assignment]
    out = L.run(conn, b)
    assert out.status is ExecStatus.FILLED and not b.checked


def _wash_403() -> APIError:
    resp = requests.Response()
    resp.status_code = 403
    resp._content = json.dumps(WASH_BODY).encode()  # noqa: SLF001
    return APIError(json.dumps(WASH_BODY), requests.HTTPError(response=resp))


def test_wash_trade_403_is_recognised_from_the_real_body() -> None:
    exc = _wash_403()
    assert exc.status_code == 403 and exc.code == WASH_TRADE_CODE
    assert is_wash_trade_rejection(exc)
    assert not is_wash_trade_rejection(RuntimeError("boom"))
    other = dict(WASH_BODY, code=40310001)
    assert not is_wash_trade_rejection(APIError(json.dumps(other)))
    resp = requests.Response()
    resp.status_code = 422
    assert not is_wash_trade_rejection(
        APIError(json.dumps(WASH_BODY), requests.HTTPError(response=resp))
    )


def test_broker_403_wash_trade_maps_to_cross_arm_conflict(conn: sqlite3.Connection) -> None:
    b = L.FaultBroker([["rejected"], ["filled"]], {0: (_wash_403(), False)})
    out = L.run(conn, b)
    assert out.status is ExecStatus.REJECTED and len(b.sent_coids) == 1
    assert len(b.lookups) == 1  # a 4xx is an answer
    (row,) = _decisions(conn, ReasonCode.CROSS_ARM_CONFLICT.value)
    assert row["source"] == "broker_403" and row["status_code"] == 403
    assert "potential wash trade detected" in row["submit_error"]
    assert ("rejected", "order:broker_rejected") not in L.journal(conn)
    assert cross_arm_conflicts(conn, None, NOW - dt.timedelta(hours=1)) == 1


def test_other_4xx_stays_broker_rejected(conn: sqlite3.Connection) -> None:
    b = L.FaultBroker([["rejected"]], {0: (L.Api422("unprocessable"), False)})
    L.run(conn, b)
    assert ("rejected", "order:broker_rejected") in L.journal(conn)
    assert not _decisions(conn, ReasonCode.CROSS_ARM_CONFLICT.value)


def test_submit_refused_carries_the_cross_arm_code() -> None:
    exc = SubmitRefused(RefusalCode.CROSS_ARM_CONFLICT, "x")
    assert str(exc) == "cross_arm_conflict: x"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_budget_status_arm_prints_arm_and_account_lines(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from arc.cli import main

    conn, stores = A._shared_setup(tmp_path)
    db = Path(conn.execute("PRAGMA database_list").fetchone()[2])
    conn.close()
    day = DAY.isoformat()
    rc = main(["budget", "status", "--db", str(db), "--day", day, "--no-broker", "--arm", "t2"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "orders today 0/20 (normal)" in out
    assert "arm t2: used 0 of 20 (slice; restrictive at 10, opens stop at 17)" in out
    assert "account: n/a (broker not read) of 200 option orders today" in out
    rc = main(["budget", "status", "--db", str(db), "--day", day, "--no-broker",
               "--arm", "XP-30:t2", "--json"])  # fmt: skip
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert (data["limit"], data["arm"], data["close_reserve"]) == (20, "XP-30:t2", 3)
    assert main(["budget", "status", "--db", str(db), "--no-broker", "--arm", "nope"]) == 2
    assert "is not an arm of this store" in capsys.readouterr().err


def test_cli_resolves_arm_keys(tmp_path: Path) -> None:
    from arc.budget.cli import resolve_arm_store

    conn, stores = A._shared_setup(tmp_path)
    assert set(arm_stores(conn)) == {"XP-30:t1", "XP-30:t2", "XP-30:t3"}
    assert resolve_arm_store(conn, "t3") == ("XP-30:t3", stores["t3"])
    assert resolve_arm_store(conn, "XP-30:t1")[0] == "XP-30:t1"
    A._start(conn, "XP-31", 1, tmp_path / "b", now=A.T0)
    with pytest.raises(LookupError, match="ambiguous"):
        resolve_arm_store(conn, "t1")


def test_now_is_et() -> None:
    assert NOW.tzinfo is ET
