"""E18.1 (D78): exit policy v2 — the profit lock (trailing take profit) + debit TP 0.60.

Covers the lock truth table, the validators, rule order, the hypothesis property
(never fires below the arm / above the floor), the managed-exit Monte Carlo, peak
tracking over stored marks, the rollback golden (lock null + TP 1.00 reproduce
main's numbers exactly), the registry rollback keys, reason codes and the
mandatory-exit path end to end.
"""

from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as hst
from pydantic import ValidationError

from arc.backtest.costs import CostModel
from arc.context.store import ContextStore
from arc.control.registry import TunableError, format_value, lookup, parse_value
from arc.exits import (
    ExitModelConfig,
    ExitPolicy,
    ExitReason,
    OpenPosition,
    PositionMarks,
    ProfitLock,
    check_rules,
    evaluate_position,
    load_exit_config,
    lock_fires,
    model_exits,
    peak_pnl,
    resolve_rules,
)
from arc.journal.reasons import REASON_LABELS, ReasonCode
from arc.models import StructureKind
from arc.positions.evaluate import SignalKind
from arc.positions.exit_case import MANDATORY_KINDS
from arc.positions.marks import stored_mark_pnls, stored_peak_pnl
from arc.store.db import connect
from arc.store.migrate import migrate
from arc.structures import credit_vertical, long_call
from tests import exits_v1_golden as golden
from tests.golden_compare import assert_json_close
from tests.test_positions_steps import LONG_CALL, NOW, _env_with, _held, _open, _run

if TYPE_CHECKING:
    import sqlite3

AS_OF = golden.AS_OF
EXP = golden.EXP
LOCK = ProfitLock(arm_pct=0.50, floor_pct=0.20)
V2_DEBIT = golden.V1_DEBIT.model_copy(
    update={"take_profit_pct_of_debit": 0.60, "profit_lock": LOCK}
)


def _rules(policy: ExitPolicy = V2_DEBIT) -> Any:
    # $2.00 debit long call: basis 2.00, arm +1.00, floor +0.40, TP +1.20, stop -1.50
    return resolve_rules(long_call("TST", EXP, 100, 2.00, as_of=AS_OF), policy)


# -- policy model ---------------------------------------------------------------


def test_profit_lock_validators() -> None:
    with pytest.raises(ValidationError, match="floor_pct"):
        ProfitLock(arm_pct=0.2, floor_pct=0.2)
    with pytest.raises(ValidationError):
        ProfitLock(arm_pct=0.5, floor_pct=0.0)
    with pytest.raises(ValidationError):
        ProfitLock.model_validate({"arm_pct": 0.5, "floor_pct": 0.2, "extra": 1})
    with pytest.raises(ValidationError, match="arm_pct 0.6 must be < take_profit_pct_of_debit"):
        ExitPolicy(
            take_profit_pct_of_max_gain=None,
            take_profit_pct_of_debit=0.6,
            profit_lock=ProfitLock(arm_pct=0.6, floor_pct=0.2),
        )
    with pytest.raises(ValidationError, match="take_profit_pct_of_max_gain"):
        ExitPolicy(
            take_profit_pct_of_max_gain=0.5,
            take_profit_pct_of_debit=None,
            profit_lock=ProfitLock(arm_pct=0.5, floor_pct=0.25),
        )
    # no take profit at all: any lock is fine
    ExitPolicy(take_profit_pct_of_max_gain=None, take_profit_pct_of_debit=None, profit_lock=LOCK)


def test_shipped_yaml_is_d78() -> None:
    cfg = load_exit_config()
    for kind in (StructureKind.VERTICAL_DEBIT, StructureKind.LONG_CALL, StructureKind.LONG_PUT):
        p = cfg.policy_for(kind)
        assert p.take_profit_pct_of_debit == 0.60
        assert p.profit_lock == ProfitLock(arm_pct=0.5, floor_pct=0.2, eod_only=False)
    for kind in (StructureKind.VERTICAL_CREDIT, StructureKind.IRON_CONDOR, None):
        assert cfg.policy_for(kind).profit_lock is None
    assert "profit lock 50% -> 20%" in cfg.policy_for(StructureKind.LONG_CALL).summary()


# -- lock truth table -------------------------------------------------------------


@pytest.mark.parametrize(
    ("pnl", "peak", "expected"),
    [
        (0.30, None, None),  # missing marks: never armed (no made-up peak)
        (0.30, 0.90, None),  # never armed (peak below +1.00)
        (0.40, 1.10, ExitReason.PROFIT_LOCK),  # armed, then the floor hit
        (0.10, 1.10, ExitReason.PROFIT_LOCK),  # armed, fell well through
        (0.41, 1.10, None),  # armed, but recovered above the floor
        (0.40, 1.00, ExitReason.PROFIT_LOCK),  # peak exactly at the arm
        (1.20, 1.10, ExitReason.TAKE_PROFIT),  # armed and still running: TP takes it
        (-1.50, 1.10, ExitReason.STOP),  # stop first, even when armed
    ],
)
def test_lock_truth_table(pnl: float, peak: float | None, expected: ExitReason | None) -> None:
    assert check_rules(_rules(), pnl=pnl, dte=30, peak_pnl=peak) is expected


def test_rule_order_stop_lock_tp_dte() -> None:
    r = _rules()
    assert check_rules(r, pnl=0.2, dte=5, peak_pnl=1.1) is ExitReason.PROFIT_LOCK  # lock > DTE
    assert check_rules(r, pnl=0.2, dte=5, peak_pnl=0.5) is ExitReason.DTE_EXIT


def test_credit_kind_with_null_lock_never_locks() -> None:
    st = credit_vertical("put", "TST", EXP, short_strike=95, short_premium=1.6, long_strike=90,
                         long_premium=0.6, as_of=AS_OF)  # fmt: skip
    r = resolve_rules(st, golden.V1_CREDIT)
    assert r.lock_arm_pnl is None
    assert check_rules(r, pnl=0.01, dte=30, peak_pnl=0.49) is None


def test_eod_only_lock_waits_for_end_of_day() -> None:
    eod = V2_DEBIT.model_copy(update={"profit_lock": LOCK.model_copy(update={"eod_only": True})})
    r = _rules(eod)
    assert not lock_fires(r, pnl=0.3, peak=1.1, eod=False)
    assert lock_fires(r, pnl=0.3, peak=1.1, eod=True)


@settings(max_examples=300, deadline=None)
@given(
    pnl=hst.floats(-1.4, 3.0, allow_nan=False),
    peak=hst.floats(-1.4, 3.0, allow_nan=False),
    arm=hst.floats(0.05, 0.55, allow_nan=False),
    frac=hst.floats(0.05, 0.95, allow_nan=False),
)
def test_property_lock_never_fires_below_arm_or_above_floor(
    pnl: float, peak: float, arm: float, frac: float
) -> None:
    lock = ProfitLock(arm_pct=arm, floor_pct=arm * frac)
    r = _rules(V2_DEBIT.model_copy(update={"profit_lock": lock}))
    fired = lock_fires(r, pnl=pnl, peak=peak)
    if peak < r.lock_arm_pnl or pnl > r.lock_floor_pnl:
        assert not fired
    else:
        assert fired


# -- peak over stored marks --------------------------------------------------------


def test_peak_pnl_pure() -> None:
    assert peak_pnl([]) is None
    assert peak_pnl([None, float("nan")]) is None
    assert peak_pnl([0.1, None, 0.7, Decimal("0.3"), float("inf")]) == Decimal("0.7")


def _review(
    conn: sqlite3.Connection, sid: str, value: float, entry: float, at: dt.datetime
) -> None:
    conn.execute(
        "INSERT INTO context_entries (id, kind, subject, payload, schema_version, produced_by, "
        "created_at, valid_from, status) "
        "VALUES (?, 'position_review', ?, ?, 3, 'positions.evaluate', ?, ?, 'superseded')",
        (f"ctx-{sid}-{at.timestamp()}", sid,
         json.dumps({"current_value": value, "entry_net": entry}),
         at.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
         at.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")),
    )  # fmt: skip


def test_stored_peak_reads_marks_since_open_only() -> None:
    conn = connect(":memory:")
    migrate(conn)
    t0 = NOW - dt.timedelta(days=2)
    _review(conn, "os-1", 9.0, 2.0, t0 - dt.timedelta(days=1))  # before the open: ignored
    _review(conn, "os-1", 2.9, 2.0, t0)
    _review(conn, "os-1", 3.3, 2.0, t0 + dt.timedelta(hours=1))
    _review(conn, "os-1", 2.1, 2.0, t0 + dt.timedelta(hours=2))
    _review(conn, "os-2", 7.0, 2.0, t0)  # another structure
    conn.execute(
        "INSERT INTO context_entries (id, kind, subject, payload, schema_version, produced_by, "
        "created_at, valid_from, status) "
        "VALUES ('bad', 'position_review', 'os-1', '{}', 3, 'x', ?, ?, 'superseded')",
        ("2026-09-24T00:00:00.000000Z", "2026-09-24T00:00:00.000000Z"),
    )
    opened = t0.isoformat()
    assert [p for _, p in stored_mark_pnls(conn, "os-1", opened_at=opened)] == [0.9, 1.3, 0.1]
    assert stored_peak_pnl(conn, "os-1", opened_at=opened) == Decimal("1.3")
    before = t0 + dt.timedelta(minutes=30)
    assert stored_peak_pnl(conn, "os-1", opened_at=opened, before=before) == Decimal("0.9")
    assert stored_peak_pnl(conn, "os-3") is None


# -- evaluate_position + Monte Carlo ------------------------------------------------

CFG = ExitModelConfig(n_paths=3000, seed=11)


def _eval(scale: float, peak: float | None, policy: ExitPolicy = V2_DEBIT) -> Any:
    st = long_call("TST", EXP, 100, 2.00, as_of=AS_OF)
    return evaluate_position(
        OpenPosition(structure=st),
        PositionMarks(as_of=AS_OF, leg_mids={st.legs[0].occ_symbol: 2.0 * scale}, spot=100.0,
                      iv=0.20, realized_vol=0.18),
        policy, cost=CostModel(), cfg=CFG, peak_pnl=peak,
    )  # fmt: skip


def test_evaluate_position_reports_lock_state() -> None:
    s = _eval(1.15, None)
    assert s.fired is None and s.lock_armed is False and s.peak_pnl_per_share is None
    assert s.lock_arm_pnl == 1.0 and s.lock_floor_pnl == 0.4
    s = _eval(1.15, 1.05)
    assert s.fired is ExitReason.PROFIT_LOCK and s.lock_armed
    assert s.peak_pnl_per_share == 1.05
    # the current mark counts toward the peak (but a peak needs a stored mark)
    assert _eval(1.55, 0.2).lock_armed
    assert _eval(1.15, None, golden.V1_DEBIT).lock_armed is None


def test_monte_carlo_models_the_lock() -> None:
    st = long_call("TST", EXP, 100, 2.60, as_of=AS_OF)
    kw: dict[str, Any] = {"spot": 100.0, "iv": 0.20, "r": 0.04, "cost": CostModel(), "cfg": CFG}
    v1 = model_exits(st, golden.V1_DEBIT, **kw).managed
    v2 = model_exits(st, V2_DEBIT, **kw).managed
    assert v1.p_profit_lock == 0.0
    assert v2.p_profit_lock > 0.0
    total = v2.p_take_profit + v2.p_stop + v2.p_dte_exit + v2.p_expiry + v2.p_profit_lock
    assert total == pytest.approx(1.0, abs=1e-9)
    assert v2.expected_days_held < v1.expected_days_held  # v2 takes profit earlier


def test_open_position_seeded_peak_changes_remaining_ev() -> None:
    """An already-armed position (stored peak) locks on more paths than a fresh one."""
    fresh = _eval(1.3, None)  # +30 % of debit, no stored mark: lock not armed
    armed = _eval(1.3, 1.1)  # +30 %, stored peak +55 %: armed, floor at +20 %
    assert fresh.fired is None and armed.fired is None
    assert fresh.remaining_days_held is not None and armed.remaining_days_held is not None
    assert armed.remaining_days_held < fresh.remaining_days_held
    assert armed.remaining_net_ev != fresh.remaining_net_ev


# -- rollback golden ------------------------------------------------------------------


def test_rollback_values_reproduce_main_exactly() -> None:
    """``profit_lock: null`` + debit TP 1.00 → main's (pre-E18.1) numbers.

    The golden was generated on main before E18.1. New fields this card adds to the
    models (``p_profit_lock`` = 0, lock state = None) are dropped before comparing.
    """
    want = json.loads(golden.GOLDEN.read_text())
    got = json.loads(json.dumps(golden.compute(), sort_keys=True))
    new = {"p_profit_lock", "peak_pnl_per_share", "lock_arm_pnl", "lock_floor_pnl", "lock_armed",
           "profit_lock"}  # fmt: skip

    def strip(x: Any) -> Any:
        if isinstance(x, dict):
            assert all(x[k] in (None, 0.0) for k in new & set(x) if k != "profit_lock"), x
            return {k: strip(v) for k, v in x.items() if k not in new}
        if isinstance(x, list):
            return [strip(v) for v in x]
        return x

    # Floats to 1e-12 relative (tests/golden_compare): numpy's summation order differs
    # by platform (macOS vs the Linux CI runner) in the last ulp of a few derived ratios.
    # Rounding to N digits instead still flakes when a value sits on a rounding boundary.
    assert set(got) == set(want)
    for key in want:
        assert_json_close(strip(got[key]), strip(want[key]), key)


# -- registry rollback keys -------------------------------------------------------------


def test_registry_rollback_keys() -> None:
    t = lookup("exits.kinds.long_call.profit_lock")
    assert t.key == "exits.long_call.profit_lock"
    assert parse_value(t, "none") is None
    assert parse_value(t, "50%:20%") == {"arm_pct": 0.5, "floor_pct": 0.2, "eod_only": False}
    assert parse_value(t, "0.6:0.3:eod") == {"arm_pct": 0.6, "floor_pct": 0.3, "eod_only": True}
    assert format_value(t, {"arm_pct": 0.5, "floor_pct": 0.2, "eod_only": False}) == "50% -> 20%"
    for bad in ("0.2:0.5", "0.5", "a:b", "0.5:0.2:x", "0.5:0"):
        with pytest.raises(TunableError):
            parse_value(t, bad)
    tp = lookup("exits.kinds.vertical_debit.take_profit_pct_of_debit")
    assert tp.key == "exits.vertical_debit.take_profit"
    assert (tp.min, tp.max) == (0.20, 2.0)
    assert lookup("exits.kinds.iron_condor.profit_lock").path[-1] == "profit_lock"


def test_slack_rollback_reaches_the_effective_policy() -> None:
    from arc.config import ArcSettings
    from arc.control.effective import exit_config
    from arc.control.service import ControlService

    conn = connect(":memory:")
    migrate(conn)
    owner = "U0C5KUMH28G"
    base = ArcSettings(_env_file=None, gate_secret="g" * 40, env="paper",  # type: ignore[call-arg]
                       approver_slack_user_ids=[owner])  # fmt: skip
    svc = ControlService(conn, base=base, now=lambda: NOW, is_halted=lambda: False)
    for key, value in (("exits.kinds.long_call.profit_lock", "none"),
                       ("exits.kinds.long_call.take_profit_pct_of_debit", "1.0")):  # fmt: skip
        r = svc.set(key, value, actor=owner, source="slack")
        if r.pending is not None:
            svc.confirm(r.pending.code, actor=owner, source="slack")
    pol = exit_config(svc.settings()).policy_for(StructureKind.LONG_CALL)
    assert pol.profit_lock is None and pol.take_profit_pct_of_debit == 1.0
    want = golden.V1_DEBIT
    assert pol.model_dump(exclude={"time_adjusted_targets"}) == want.model_dump(
        exclude={"time_adjusted_targets"}
    )


# -- reason codes + mandatory path ------------------------------------------------------


def test_reason_code_and_mandatory() -> None:
    assert ReasonCode.EXIT_PROFIT_LOCK.value == "exit:profit_lock"
    assert REASON_LABELS[ReasonCode.EXIT_PROFIT_LOCK] == "Closed: profit lock"
    assert SignalKind.PROFIT_LOCK in MANDATORY_KINDS
    from arc.execution.exits import EXIT_CODES

    assert EXIT_CODES[ExitReason.PROFIT_LOCK] is ReasonCode.EXIT_PROFIT_LOCK


def test_live_path_profit_lock_is_closed_by_exits_mandatory() -> None:
    """Stored 30-min marks arm the lock; ``exits.mandatory`` proposes the close via the gate."""
    conn = connect(":memory:")
    migrate(conn)
    env = _env_with(_held(LONG_CALL))
    value = 11.415  # the fixture chain's long-call mid
    entry = round(value / 1.10, 4)  # current P&L +10 % of the debit (below the 20 % floor)
    sid = _open(conn, env, LONG_CALL, str(entry))
    ev = _run(conn, env, "positions.evaluate")
    assert ev.metrics.get("signal_profit_lock", 0) == 0  # no stored peak yet: not armed
    # a stored mark an hour earlier at +55 % of the debit arms the lock
    _review(conn, sid, entry * 1.55, entry, NOW - dt.timedelta(hours=1))
    ev = _run(conn, env, "positions.evaluate")
    assert ev.metrics["signal_profit_lock"] == 1
    latest = ContextStore(conn).snapshot(NOW).of_kind("position_review")[0].payload
    assert latest["lock_armed"] is True and latest["pct_peak"] == pytest.approx(0.55, abs=1e-3)
    assert [s["kind"] for s in latest["signals"]] == ["profit_lock"]
    out = _run(conn, env, "exits.mandatory")
    assert out.metrics["proposed"] == 1 and out.metrics["gate_passed"] == 1
    assert [c[:2] for c in out.metrics["closes"]] == [["SPY", "profit_lock"]]
