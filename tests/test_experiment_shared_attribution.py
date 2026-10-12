"""E15.2 (D69): shared-paper-account attribution per arm.

The arm tag in the client order id, code allocation at t0, FILL / FEE / option-event
attribution, the per-arm virtual ledger rows, and the book invariant that halts arms.

``tests/fixtures/experiments/shared_account_live.json`` is the production paper
account's 2026-10-12 FILL activities and filled orders (read-only probe), sanitized:
every Alpaca uuid replaced, account fields dropped, the client ids re-tagged to arm
codes 1 / 2. FEE / OPEXP / OPASN / OPTRD rows below follow Alpaca's documented
shapes (none had posted on our paper accounts yet).
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from decimal import Decimal as D
from pathlib import Path
from typing import Any

import pytest

import arc.experiments.attribution as attribution
from arc.broker.base import AccountInfo, BrokerPosition
from arc.config import ArcSettings
from arc.execution import attempt_order_id, submit
from arc.execution.submission import (
    ARM_CODES,
    MAX_CLIENT_ORDER_ID,
    broker_arm_code,
    build_order,
    parse_order_id,
)
from arc.experiments.arms import arm_stores, read_identity
from arc.experiments.attribution import (
    SMOKE,
    ArmWindow,
    BookViolation,
    CodeAllocationError,
    account_book,
    account_violations,
    account_windows,
    attribute,
    build_book,
    check_book,
    free_codes,
    halt_violations,
    held_codes,
    order_tags,
    parse_activity,
    split_cents,
)
from arc.experiments.config import ArmRunner, ExperimentDefaults, RunnerConfig
from arc.experiments.models import ExperimentSpec, ExperimentStatus, StopReason
from arc.experiments.overlay import fill_defaults
from arc.experiments.runner import ArmAccount, ArmStartError, arms_tick, start_arms
from arc.experiments.store import ExperimentStore
from arc.experiments.virtual import (
    SharedAccountError,
    VirtualBroker,
    arm_positions,
    replay,
    rows,
)
from arc.gate import HaltSwitch, issue_token, order_payload, proposal_hash
from arc.gate.band import PriceBand
from arc.gate.hook_policy import check_tool_call
from arc.gate.token import TokenError, verify_client_order_id
from arc.journal.reasons import ReasonCode
from arc.models import (
    ApprovalDecision,
    ApprovalRecord,
    GateDecision,
    Proposal,
    QuantMetrics,
    Sizing,
)
from arc.monitoring.alerts import RecordingOpsNotifier
from arc.pipeline.env import FIXTURE_NOW
from arc.reconcile.engine import MismatchKind
from arc.routines.handlers import JobResult
from arc.store.db import connect
from arc.store.migrate import migrate
from arc.store.repos import HaltRepo
from arc.structures import credit_vertical
from arc.utils.calendar import ET

FIX = Path(__file__).parent / "fixtures" / "experiments" / "shared_account_live.json"
LIVE = json.loads(FIX.read_text())
DAY = dt.date(2026, 10, 12)
T0 = dt.datetime(2026, 10, 12, 9, 0, tzinfo=ET)
CASH_T0 = D("25000")
OWNER = "local"
ACCT = "PA3EXP0001"


def _w(arm: str, code: str, **kw: Any) -> ArmWindow:
    return ArmWindow(arm_id=arm, code=code, t0=kw.pop("t0", T0), cash_t0=CASH_T0, **kw)


WINDOWS = [_w("XP-30:t1", "1"), _w("XP-30:t2", "2")]


def _acts(*extra: dict[str, Any]) -> list[Any]:
    out = [parse_activity(r) for r in [*LIVE["fills"], *extra]]
    assert all(a is not None for a in out)
    return out


def _pos(sym: str, qty: str, px: str = "1") -> BrokerPosition:
    q = D(qty)
    return BrokerPosition(
        symbol=sym, qty=abs(q), side="short" if q < 0 else "long", current_price=D(px)
    )


#: the live account's positions after those fills (2026-10-12)
LIVE_POS = [
    _pos("IREN261030C00036000", "5", "1.90"),
    _pos("IREN261030C00039000", "-5", "0.95"),
    _pos("NVDA261106C00230000", "1", "5.40"),
    _pos("HOOD261023P00111000", "2", "4.80"),
]


# --- the arm tag in the client order id --------------------------------------------------

SECRET = "s" * 32
NOW = dt.datetime(2026, 10, 9, 10, 0, tzinfo=ET)
EXP = dt.date(2026, 11, 20)
BAND = PriceBand(lo=D("-0.85"), hi=D("-0.76"), max_steps=3)


def _proposal() -> Proposal:
    return Proposal(
        candidate_id="c",
        structure=credit_vertical(
            "put",
            "SPY",
            EXP,
            short_strike=570,
            short_premium="2.10",
            long_strike=565,
            long_premium="1.25",
            as_of=dt.date(2026, 10, 9),
        ),  # fmt: skip
        thesis="t",
        quant=QuantMetrics(pop=0.7, ev=D("1"), cost_bps=0.0),
        sizing=Sizing(contracts=2, notional=D("830"), pct_equity=0.01),
        expires_at=NOW + dt.timedelta(minutes=10),
    )


def _gated(p: Proposal, band: PriceBand | None = BAND) -> GateDecision:
    d = GateDecision(proposal_hash=proposal_hash(p), passed=True)
    return issue_token(d, p, secret=SECRET.encode(), now=NOW, band=band)


class _TagBroker:
    def __init__(self, arm_code: str | None) -> None:
        self.arm_code = arm_code
        self.orders: list[Any] = []

    def submit_mleg(self, order: Any) -> str:
        self.orders.append(order)
        return f"brk-{len(self.orders)}"


def _halt() -> HaltSwitch:
    c = connect(":memory:")
    migrate(c)
    return HaltSwitch(HaltRepo(c))


def test_arm_code_round_trips_and_the_gate_still_verifies_it() -> None:
    p = _proposal()
    token = _gated(p).token
    assert token is not None
    for code in ("0", "Z", "z"):
        coid = attempt_order_id(token, 2, arm_code=code)
        assert coid.startswith(f"arc2.{code}.") and coid.endswith(".s2")
        assert len(coid) <= MAX_CLIENT_ORDER_ID
        tag = parse_order_id(coid)
        assert tag.arm_code == code and tag.gate_id == attempt_order_id(token, 2)
    # control's id carries no code; neither do arc1 / smoke / foreign ids
    assert parse_order_id(attempt_order_id(token, 0)).arm_code is None
    assert parse_order_id(_gated(p, None).token).arm_code is None
    for other in ("arc-0a1b2c", "test.abc", "", None, "arc2.", "arc2.ab.c"):
        assert parse_order_id(other).arm_code is None


def test_worst_case_tagged_id_fits_in_128() -> None:
    """Longest token (band at the $9,999.99 cap, both signs, 9 steps) at s9 + a code."""
    p = _proposal()
    wide = p.model_copy(update={"structure": credit_vertical(
        "put", "SPY", EXP, short_strike=570, short_premium="10000.00", long_strike=565,
        long_premium="0.01", as_of=dt.date(2026, 10, 9),
    )})  # fmt: skip
    lengths = []
    for lo, hi in (("-9999.99", "-9999.98"), ("-9999.99", "9999.99")):
        band = PriceBand(lo=D(lo), hi=D(hi), max_steps=9)
        token = _gated(wide, band).token
        assert token is not None
        coid = attempt_order_id(token, 9, arm_code="z")
        lengths.append(len(coid))
        assert parse_order_id(coid).arm_code == "z"
    assert max(lengths) == MAX_CLIENT_ORDER_ID  # exactly at the limit, never over


def test_bad_codes_and_arc1_tokens_are_refused() -> None:
    p = _proposal()
    arc2, arc1 = _gated(p).token, _gated(p, None).token
    assert arc2 and arc1
    for bad in ("", "00", "-", ".", "é"):
        with pytest.raises(TokenError, match="not base62"):
            attempt_order_id(arc2, 0, arm_code=bad)
    with pytest.raises(TokenError, match="arc2 token"):
        attempt_order_id(arc1, 0, arm_code="1")
    assert len(ARM_CODES) == 62 == len(set(ARM_CODES))


def test_submit_tags_orders_from_a_shared_arm_broker_only() -> None:
    p = _proposal()
    d = _gated(p)
    a = ApprovalRecord(
        proposal_hash=proposal_hash(p), slack_user="U1", slack_ts="1.1",
        decision=ApprovalDecision.APPROVED, at=NOW - dt.timedelta(minutes=1),
    )  # fmt: skip
    cfg = ArcSettings(_env_file=None, gate_secret=SECRET)  # type: ignore[call-arg]
    for code in ("7", None):
        broker = _TagBroker(code)
        submit(p, d, a, broker=broker, config=cfg, now=NOW, halt=_halt(),  # type: ignore[arg-type]
               step=1, limit_price=D("-0.80"))  # fmt: skip
        (order,) = broker.orders
        assert parse_order_id(order.client_order_id).arm_code == code
        assert (
            order.client_order_id
            == build_order(
                p, d.token or "", step=1, limit_price=D("-0.80"), arm_code=code
            ).client_order_id
        )
        # the gate verifies the bare id; the tagged one is not a gate id by itself
        bare = parse_order_id(order.client_order_id).gate_id
        payload = order_payload(p).model_copy(update={"limit_price": D("-0.80")})
        verify_client_order_id(bare, secret=SECRET.encode(), now=NOW, order=payload)
        if code is not None:
            with pytest.raises(TokenError):
                verify_client_order_id(
                    order.client_order_id, secret=SECRET.encode(), now=NOW, order=payload
                )
    assert broker_arm_code(object()) is None


# --- code allocation at t0 ---------------------------------------------------------------


def test_free_codes_in_order_and_fail_closed() -> None:
    assert free_codes([], 3) == ["0", "1", "2"]
    assert free_codes(["0", "2"], 2) == ["1", "3"]
    with pytest.raises(CodeAllocationError, match="no free arm code: 61 of 62"):
        free_codes(ARM_CODES[:61], 2)
    assert free_codes(ARM_CODES[:61], 1) == ["z"]


def _db(path: Path | str = ":memory:") -> sqlite3.Connection:
    c = sqlite3.connect(str(path))
    c.row_factory = sqlite3.Row
    migrate(c)
    return c


def _spec(eid: str, k: int) -> ExperimentSpec:
    data = {
        "spec_version": 2, "id": eid, "title": eid, "hypothesis": "noise", "area": "other",
        "kind": "aa", "proposed_by": "owner",
        "arms": {"treatments": {f"t{i}": {"overlay": {}} for i in range(1, k + 1)}},
    }  # fmt: skip
    return fill_defaults(ExperimentSpec.model_validate(data), ExperimentDefaults())


def _runner(mode: str = "shared") -> RunnerConfig:
    if mode == "shared":
        return RunnerConfig(
            account_mode="shared",
            max_parallel_arms=16,
            arms={
                "t": ArmRunner(
                    spec_arm="treatments", keys_env="ALPACA_EXP", db="exp-{experiment_id}-{arm}.db"
                )
            },  # fmt: skip
        )
    return RunnerConfig(
        arms={
            "treatment": ArmRunner(
                spec_arm="treatment", keys_env="ALPACA_EXP", db="exp-{experiment_id}.db"
            )
        }  # fmt: skip
    )


def _probe(keys_env: str) -> ArmAccount:
    return ArmAccount(account_number=ACCT, equity=D("250000"), positions=0, open_orders=0,
                      cash=CASH_T0)  # fmt: skip


def _start(conn: sqlite3.Connection, eid: str, k: int, tmp: Path, **kw: Any) -> Any:
    runner = kw.pop("runner", None) or _runner()
    store = ExperimentStore.for_runner(conn, runner)
    store.create(_spec(eid, k), actor=OWNER, owner_approval="P-1")
    store.register(eid, actor=OWNER, owner_approval="P-1")
    return start_arms(
        conn, eid, actor=OWNER, now=kw.pop("now", FIXTURE_NOW), t0_equity=D("10000"),
        runner=runner, arm_dir=tmp / "arms", control_sha="abcdef1", aa_override=True,
        probe=_probe,
    )  # fmt: skip


def _codes(conn: sqlite3.Connection, eid: str) -> dict[str, str | None]:
    out: dict[str, str | None] = {}
    for name, path in arm_stores(conn, eid).items():
        c = _db(path)
        ident = read_identity(c)
        c.close()
        assert ident is not None
        out[name] = ident.arm_code
    return out


def test_t0_allocates_distinct_codes_across_running_experiments(tmp_path: Path) -> None:
    conn = _db(tmp_path / "control.db")
    _start(conn, "XP-30", 3, tmp_path)
    _start(conn, "XP-31", 2, tmp_path)
    assert _codes(conn, "XP-30") == {"t1": "0", "t2": "1", "t3": "2"}
    assert _codes(conn, "XP-31") == {"t1": "3", "t2": "4"}
    c = _db(arm_stores(conn, "XP-30")["t1"])
    ident = read_identity(c)
    assert ident is not None and ident.account_cash_t0 == CASH_T0


def test_dedicated_arms_carry_no_code(tmp_path: Path) -> None:
    conn = _db(tmp_path / "control.db")
    store = ExperimentStore.for_runner(conn, _runner("dedicated"))
    sp = fill_defaults(
        ExperimentSpec.model_validate({
            "id": "XP-32", "title": "x", "hypothesis": "h", "area": "other", "kind": "aa",
            "proposed_by": "owner", "arms": {"treatment": {"overlay": {}}},
        }),
        ExperimentDefaults(),
    )  # fmt: skip
    store.create(sp, actor=OWNER, owner_approval="P-1")
    store.register("XP-32", actor=OWNER, owner_approval="P-1")
    start_arms(
        conn, "XP-32", actor=OWNER, now=FIXTURE_NOW, t0_equity=D("10000"),
        runner=_runner("dedicated"), arm_dir=tmp_path / "arms", control_sha="abcdef1",
        aa_override=True, probe=_probe, check_flat=lambda arm: None,
    )  # fmt: skip
    assert _codes(conn, "XP-32") == {"treatment": None}


def test_no_free_code_fails_t0_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(attribution, "ARM_CODES", "01")
    conn = _db(tmp_path / "control.db")
    with pytest.raises(ArmStartError, match="no free arm code"):
        _start(conn, "XP-30", 3, tmp_path)
    assert arm_stores(conn) == {}
    assert ExperimentStore(conn).require("XP-30").status is ExperimentStatus.REGISTERED


def test_a_stopped_arms_code_is_reused_only_once_it_is_flat(tmp_path: Path) -> None:
    conn = _db(tmp_path / "control.db")
    _start(conn, "XP-30", 2, tmp_path)
    ExperimentStore(conn).stop("XP-30", StopReason.OWNER, actor=OWNER)
    windows = account_windows(conn, account_sha256=None, keys_env="ALPACA_EXP")
    assert [w.running for w in windows] == [False, False]
    assert held_codes(windows) == set()  # flat: both codes free again
    # t1 still holds a contract in its ledger: its code stays taken
    t1 = connect(arm_stores(conn, "XP-30")["t1"])
    with t1:
        t1.execute(
            "INSERT INTO virtual_ledger (arm_id, kind, ref, amount, settles_on, at, detail) "
            "VALUES ('XP-30:t1', 'fill', 'x', '-100', NULL, '2026-09-25T19:30:00.000000Z', "
            '\'{"symbol": "SPY261120P00565000", "qty": "1"}\')'
        )
    t1.close()
    assert held_codes(account_windows(conn, account_sha256=None, keys_env="ALPACA_EXP")) == {"0"}


# --- attribution --------------------------------------------------------------------------


def test_live_fills_attribute_by_tag_with_mleg_legs_through_the_parent() -> None:
    tags = order_tags(LIVE["orders"])
    mleg = next(o for o in LIVE["orders"] if o["order_class"] == "mleg")
    assert all(tags[leg["id"]] == mleg["client_order_id"] for leg in mleg["legs"])
    attr = attribute(_acts(), tags, WINDOWS)
    assert attr.untagged == []
    # IREN call debit (5 x 1.95 bought, 5 x 0.99 sold) + HOOD put 2 x 5.00 -> t1
    assert attr.cash == {"XP-30:t1": D("-1480.00"), "XP-30:t2": D("-535.00")}
    assert sum(attr.cash.values()) == D("-2015.00")  # the live account's fill flows
    assert attr.arm_held("XP-30:t1") == {
        "IREN261030C00036000": D(5), "IREN261030C00039000": D(-5), "HOOD261023P00111000": D(2)
    }  # fmt: skip
    assert attr.arm_held("XP-30:t2") == {"NVDA261106C00230000": D(1)}
    refs = [r.ref for r in attr.rows["XP-30:t1"]]
    assert len(refs) == len(set(refs)) == 3 and all(r.startswith("act:") for r in refs)
    # re-reading the same page (duplicates) books nothing new
    again = attribute(_acts() + _acts(), tags, WINDOWS)
    assert again.rows == attr.rows


def test_fills_before_an_arms_t0_or_with_an_unknown_code_are_untagged() -> None:
    late = [_w("XP-30:t1", "1", t0=dt.datetime(2026, 10, 12, 12, 0, tzinfo=ET)),
            _w("XP-30:t2", "2")]  # fmt: skip
    attr = attribute(_acts(), order_tags(LIVE["orders"]), late)
    assert len(attr.untagged) == 3  # t1's three fills came before its t0
    assert attr.cash == {"XP-30:t2": D("-535.00")}


def test_a_reused_code_goes_to_the_later_arm() -> None:
    old = _w("XP-29:t1", "1", t0=T0 - dt.timedelta(days=5), running=False,
             stop=T0 - dt.timedelta(days=1))  # fmt: skip
    attr = attribute(_acts(), order_tags(LIVE["orders"]), [old, *WINDOWS])
    assert "XP-29:t1" not in attr.cash and attr.cash["XP-30:t1"] == D("-1480.00")


def _fee(amount: str, *, execution_id: str | None = None, **kw: Any) -> dict[str, Any]:
    row = {
        "id": kw.pop("id", "20261012000000000::fee-1"),
        "activity_type": "FEE",
        "date": kw.pop("date", "2026-10-12"),
        "net_amount": amount,
        "description": "ORF/OCC/TAF fees",
        "status": "executed",
    }
    if execution_id is not None:
        row["execution_id"] = execution_id
    return row | kw


def test_fees_split_pro_rata_by_contracts_filled_that_day_to_the_cent() -> None:
    attr = attribute(_acts(_fee("-0.33")), order_tags(LIVE["orders"]), WINDOWS)
    # 12 contracts on t1, 1 on t2: 30.46c / 2.54c -> largest remainder
    assert attr.fees == {"XP-30:t1": D("-0.30"), "XP-30:t2": D("-0.03")}
    fee_rows = [r for r in attr.rows["XP-30:t1"] if r.kind == "fee"]
    assert [(r.ref, r.detail["method"]) for r in fee_rows] == [("fee:20261012000000000::fee-1",
                                                                "pro_rata")]  # fmt: skip
    assert attr.unfeed_contracts == 0  # the day's fee posted


def test_a_fee_joined_to_its_fill_goes_to_that_arm() -> None:
    nvda = next(f for f in LIVE["fills"] if f["symbol"].startswith("NVDA"))
    for link in ({"execution_id": nvda["id"].rsplit("::", 1)[-1]},
                 {"details": {"parent_id": nvda["id"]}}):  # fmt: skip
        raw = _fee("-0.02", id="20261012000000000::fee-2") | link
        attr = attribute(_acts(raw), order_tags(LIVE["orders"]), WINDOWS)
        assert attr.fees == {"XP-30:t2": D("-0.02")}
        (row,) = [r for r in attr.rows["XP-30:t2"] if r.kind == "fee"]
        assert row.detail["method"] == "execution_id"


@pytest.mark.parametrize(
    ("total", "weights", "want"),
    [
        ("-0.33", {"a": D(12), "b": D(1)}, {"a": D("-0.30"), "b": D("-0.03")}),
        (
            "1.00",
            {"a": D(1), "b": D(1), "c": D(1)},
            {"a": D("0.34"), "b": D("0.33"), "c": D("0.33")},
        ),  # fmt: skip
        ("0", {"a": D(1)}, {"a": D("0.00")}),
    ],
)
def test_split_cents_sums_exactly(total: str, weights: dict[str, D], want: dict[str, D]) -> None:
    out = split_cents(D(total), weights)
    assert out == want and sum(out.values()) == D(total)


def _opexp(sym: str, qty: str) -> dict[str, Any]:
    return {"id": f"20261030000000000::exp-{sym}", "activity_type": "OPEXP",
            "date": "2026-10-30", "net_amount": "0", "description": "Option Expiry",
            "symbol": sym, "qty": qty, "status": "executed"}  # fmt: skip


def test_expiry_flattens_the_holding_arm_with_a_zero_cash_event_row() -> None:
    attr = attribute(_acts(_opexp("HOOD261023P00111000", "-2")), order_tags(LIVE["orders"]),
                     WINDOWS)  # fmt: skip
    assert "HOOD261023P00111000" not in attr.arm_held("XP-30:t1")
    (evt,) = [r for r in attr.rows["XP-30:t1"] if r.kind == "event"]
    assert evt.amount == 0 and evt.detail["qty"] == "-2" and evt.ref.startswith("evt:")
    assert attr.untagged == []


def test_expiry_of_a_contract_two_arms_hold_splits_by_their_qty() -> None:
    # t2 also buys 1 HOOD put through its own tagged order
    hood = next(o for o in LIVE["orders"] if o.get("symbol", "").startswith("HOOD"))
    nvda = next(o for o in LIVE["orders"] if o.get("symbol", "").startswith("NVDA"))
    extra = {"id": "20261012110000000::t2-hood", "activity_type": "FILL",
             "transaction_time": "2026-10-12T15:00:00Z", "type": "fill", "price": "4.90",
             "qty": "1", "side": "buy", "symbol": hood["symbol"], "order_id": nvda["id"],
             "leaves_qty": "0", "cum_qty": "1", "order_status": "filled"}  # fmt: skip
    attr = attribute(
        _acts(extra, _opexp(hood["symbol"], "-3")), order_tags(LIVE["orders"]), WINDOWS
    )
    assert hood["symbol"] not in attr.arm_held("XP-30:t1")
    assert hood["symbol"] not in attr.arm_held("XP-30:t2")


def test_assignment_and_its_share_leg_book_to_the_short_arm() -> None:
    sym = "IREN261030C00039000"  # t1 is short 5
    asn = {"id": "20261030000000000::asn-1", "activity_type": "OPASN", "date": "2026-10-30",
           "net_amount": "0", "description": "", "symbol": sym, "qty": "5",
           "status": "executed"}  # fmt: skip
    trd = {"id": "20261030000000000::trd-1", "activity_type": "OPTRD", "date": "2026-10-30",
           "net_amount": "19500", "description": "Option Trade", "symbol": "IREN",
           "qty": "-500", "price": "39", "status": "executed"}  # fmt: skip
    attr = attribute(_acts(asn, trd), order_tags(LIVE["orders"]), WINDOWS)
    held = attr.arm_held("XP-30:t1")
    assert sym not in held and held["IREN"] == D(-500)
    assert attr.cash["XP-30:t1"] == D("-1480.00") + D("19500")
    assert attr.untagged == []
    st = replay([_open("XP-30:t1"), *attr.rows["XP-30:t1"]], as_of=dt.date(2026, 10, 30))
    assert st.held["IREN"] == D(-500) and st.cash == D("10000") - D("1480") + D("19500")


def test_an_option_event_no_arm_holds_is_untagged() -> None:
    attr = attribute(_acts(_opexp("TSLA261030C00500000", "-1")), order_tags(LIVE["orders"]),
                     WINDOWS)  # fmt: skip
    assert any("no arm holds it" in u for u in attr.untagged)


# --- the book invariant -------------------------------------------------------------------


def _book(*extra: dict[str, Any], positions: list[BrokerPosition] | None = None,
          cash: D | None = None, orders: list[dict[str, Any]] | None = None) -> Any:  # fmt: skip
    return build_book(
        [*LIVE["fills"], *extra],
        LIVE["orders"] if orders is None else orders,
        WINDOWS,
        LIVE_POS if positions is None else positions,
        broker_cash=CASH_T0 - D("2015.00") - D("0.33") if cash is None else cash,
        since=T0,
    )


def test_live_day_balances_with_pending_fees_allowed() -> None:
    book = _book()
    assert account_violations(book) == []
    assert book.attribution.unfeed_contracts == 13
    totals = book.totals()
    assert totals["XP-30:t1"]["contracts"] == "12" and totals["XP-30:t2"]["contracts"] == "1"
    # the posted FEE closes the residual exactly
    assert account_violations(_book(_fee("-0.33"))) == []


def test_cash_off_by_more_than_pending_fees_is_an_account_violation() -> None:
    (v,) = account_violations(_book(cash=CASH_T0 - D("2015.00") - D("1.00")))
    assert (v.scope, v.kind) == ("account", "cash") and "residual -1.00" in v.detail
    (v,) = account_violations(_book(cash=CASH_T0 - D("2015.00") + D("0.05")))
    assert v.kind == "cash"


def test_a_position_only_one_arm_should_hold_is_an_arm_violation() -> None:
    pos = [p if not p.symbol.startswith("NVDA") else _pos(p.symbol, "2") for p in LIVE_POS]
    (v,) = account_violations(_book(positions=pos))
    assert (v.scope, v.arm_id, v.kind) == ("arm", "XP-30:t2", "qty")
    gone = [p for p in LIVE_POS if not p.symbol.startswith("HOOD")]
    (v,) = account_violations(_book(positions=gone))
    assert (v.scope, v.arm_id, v.subject) == ("arm", "XP-30:t1", "HOOD261023P00111000")


def test_a_broker_position_no_arm_holds_is_an_account_violation() -> None:
    (v,) = account_violations(_book(positions=[*LIVE_POS, _pos("SPY261120P00565000", "1")]))
    assert (v.scope, v.kind) == ("account", "qty") and "arms none" in v.detail


def test_an_untagged_order_or_fill_is_an_account_violation() -> None:
    orders = [dict(o) for o in LIVE["orders"]]
    orders[1]["client_order_id"] = parse_order_id(orders[1]["client_order_id"]).gate_id
    vs = account_violations(_book(orders=orders))
    kinds = sorted((v.scope, v.kind) for v in vs)
    assert ("account", "order") in kinds and ("account", "untagged") in kinds
    # a smoke order (`arc-<hex>`) belongs to no arm but still sums
    smoke = [dict(o) for o in LIVE["orders"]]
    smoke[1]["client_order_id"] = "arc-0a1b2c3d"
    book = _book(orders=smoke)
    assert SMOKE in book.attribution.held and account_violations(book) == []


def test_check_book_without_a_cash_baseline_checks_positions_only() -> None:
    attr = attribute(_acts(), order_tags(LIVE["orders"]), WINDOWS)
    assert check_book(attr, LIVE_POS, broker_cash=D(1), cash_baseline=None) == []


# --- halting ------------------------------------------------------------------------------


def test_halt_violations_halts_the_reached_arms_once_with_a_journal_row() -> None:
    arms = {"XP-30:t1": _db(), "XP-30:t2": _db()}
    ops = RecordingOpsNotifier()
    arm_v = BookViolation(scope="arm", arm_id="XP-30:t2", kind="qty", subject="NVDA", detail="d")
    at = dt.datetime(2026, 10, 12, 11, 0, tzinfo=ET)
    assert set(halt_violations([arm_v], arms, now=at, notifier=ops)) == {"XP-30:t2"}
    assert HaltRepo(arms["XP-30:t1"]).active() == []
    (h,) = HaltRepo(arms["XP-30:t2"]).active()
    assert h["reason"].startswith("book_invariant")
    (j,) = arms["XP-30:t2"].execute("SELECT reason_code, subject FROM decisions").fetchall()
    assert tuple(j) == (ReasonCode.RECONCILE_BOOK_INVARIANT.value, "NVDA")
    acct_v = BookViolation(scope="account", kind="cash", subject="cash", detail="c")
    assert set(halt_violations([acct_v], arms, now=at, notifier=ops)) == {"XP-30:t1"}
    assert halt_violations([acct_v, arm_v], arms, now=at, notifier=ops) == {}  # idempotent
    assert len(ops.posts) == 2 and all("book_invariant" in p for p in ops.posts)


# --- the arm's broker in shared mode, and the arms tick -----------------------------------


class _Account:
    """A fake shared paper account: read-only calls only."""

    def __init__(self, raw: list[dict[str, Any]], orders: list[dict[str, Any]],
                 positions: list[BrokerPosition], cash: D) -> None:  # fmt: skip
        self.raw, self.orders_, self.pos, self.cash = raw, orders, positions, cash
        self.calls: list[str] = []

    def raw_activities(self, since: dt.date) -> list[dict[str, Any]]:
        self.calls.append("raw_activities")
        return list(self.raw)

    def raw_orders(self, since: dt.datetime) -> list[dict[str, Any]]:
        self.calls.append("raw_orders")
        return list(self.orders_)

    def positions(self) -> list[BrokerPosition]:
        return list(self.pos)

    def account(self) -> AccountInfo:
        return AccountInfo(account_id=ACCT, equity=self.cash, cash=self.cash,
                           buying_power=self.cash, last_equity=self.cash)  # fmt: skip

    def fills(self, since: dt.datetime) -> list[Any]:
        self.calls.append("fills")  # reconcile's view; sync must not book from it
        return []


def _open(arm: str) -> Any:
    from arc.experiments.virtual import LedgerRow

    return LedgerRow(arm_id=arm, kind="open", ref="t0", amount=D("10000"), at=T0)


def _shared_setup(tmp_path: Path) -> tuple[sqlite3.Connection, dict[str, Path]]:
    """XP-30 with 3 arms: t1 code 0 (idle), t2 code 1, t3 code 2 (the fixture's tags)."""
    conn = _db(tmp_path / "control.db")
    _start(conn, "XP-30", 3, tmp_path, now=T0)
    return conn, arm_stores(conn, "XP-30")


def _vb(conn: sqlite3.Connection, path: Path, account: _Account, arm: str) -> VirtualBroker:
    c = _db(path)
    ident = read_identity(c)
    assert ident is not None
    ctl = Path(conn.execute("PRAGMA database_list").fetchone()[2])
    return VirtualBroker(
        account,  # type: ignore[arg-type]
        c, arm_id=arm, t0=T0, cash_settlement=False,
        now=lambda: dt.datetime(2026, 10, 12, 15, 0, tzinfo=ET),
        control=lambda: sqlite3.connect(f"{ctl.as_uri()}?mode=ro", uri=True),
        account_mode="shared", arm_code=ident.arm_code, keys_env=ident.keys_env,
        account_sha256=ident.account_sha256,
    )  # fmt: skip


def test_shared_arm_broker_books_its_own_rows_and_marks_its_own_positions(
    tmp_path: Path,
) -> None:
    conn, stores = _shared_setup(tmp_path)
    account = _Account([*LIVE["fills"], _fee("-0.33")], LIVE["orders"], LIVE_POS,
                       CASH_T0 - D("2015.33"))  # fmt: skip
    t2 = _vb(conn, stores["t2"], account, "XP-30:t2")
    assert t2.shared and t2.arm_code == "1"
    info = t2.account()
    assert "fills" not in account.calls  # booked from activities only
    state = replay(rows(t2.conn, "XP-30:t2"), as_of=DAY)
    assert state.cash == D("10000") - D("1480.00") - D("0.30")
    assert {p.symbol: (p.side, p.qty) for p in t2.positions()} == {
        "IREN261030C00036000": ("long", D(5)),
        "IREN261030C00039000": ("short", D(5)),
        "HOOD261023P00111000": ("long", D(2)),
    }
    # equity = own cash + own legs marked at the broker's prices, never t3's NVDA
    marks = 5 * D("1.90") * 100 - 5 * D("0.95") * 100 + 2 * D("4.80") * 100
    assert info.equity == state.cash + marks
    n = len(rows(t2.conn, "XP-30:t2"))
    t2.account()  # re-sync books nothing new
    assert len(rows(t2.conn, "XP-30:t2")) == n
    assert broker_arm_code(t2) == "1"
    t1 = _vb(conn, stores["t1"], account, "XP-30:t1")
    assert t1.positions() == [] and t1.account().equity == D("10000")
    assert [f.symbol for f in t1.fills(T0)] == []
    assert account.calls.count("raw_activities") >= 2


def test_arm_positions_marks_only_the_arms_qty() -> None:
    out = arm_positions({"NVDA261106C00230000": D(1), "IREN": D(-100)}, LIVE_POS)
    by = {p.symbol: p for p in out}
    assert by["NVDA261106C00230000"].market_value == D("540.00")
    assert by["IREN"].asset_class == "us_equity" and by["IREN"].side == "short"


def test_reconcile_maps_book_violations_and_account_book_reads_windows(tmp_path: Path) -> None:
    conn, stores = _shared_setup(tmp_path)
    good = _Account(LIVE["fills"], LIVE["orders"], LIVE_POS, CASH_T0 - D("2015.33"))
    book = account_book(good, conn, account_sha256=None, keys_env="ALPACA_EXP")
    assert [w.code for w in book.windows] == ["0", "1", "2"]
    assert account_violations(book) == []
    bad = _Account(LIVE["fills"], LIVE["orders"], LIVE_POS[:-1], CASH_T0 - D("2015.33"))
    t2 = _vb(conn, stores["t2"], bad, "XP-30:t2")
    at = dt.datetime(2026, 10, 12, 15, 0, tzinfo=ET)
    found = t2.book_check(now=at)
    assert [(v.scope, v.arm_id) for v in found] == [("arm", "XP-30:t2")]
    assert HaltRepo(t2.conn).active()[0]["reason"].startswith("book_invariant")
    assert MismatchKind.BOOK_INVARIANT.value == "book_invariant"


def _stub_handlers(ran: list[str]) -> dict[str, Any]:
    def handler(name: str) -> Any:
        def run(ctx: Any) -> JobResult:
            ran.append(f"{Path(ctx.run_env.db_path).name}:{name}")
            return JobResult(summary=f"{name} stub")

        return run

    return {j: handler(j) for j in ("monitor", "positions.evaluate", "broker.reconcile", "broker")}


def test_arms_tick_checks_each_shared_account_once_and_halts_on_violation(
    tmp_path: Path,
) -> None:
    conn, stores = _shared_setup(tmp_path)
    off = _Account(LIVE["fills"], LIVE["orders"], LIVE_POS, CASH_T0 - D("2100"))
    reads: list[str] = []

    def reader(keys_env: str) -> Any:
        reads.append(keys_env)
        return account_book(off, conn, account_sha256=None, keys_env=keys_env)

    ops = RecordingOpsNotifier()
    at = dt.datetime(2026, 10, 12, 15, 5, tzinfo=ET)
    report = arms_tick(conn, routines_path=None, now=at, lock_dir=None,
                       handlers=_stub_handlers([]), account_number=lambda k: ACCT,
                       notifier=ops, book_reader=reader)  # fmt: skip
    assert reads == ["ALPACA_EXP"]
    book = report["books"]["ALPACA_EXP"]
    assert [v["kind"] for v in book["violations"]] == ["cash"]
    assert book["halted"] == ["XP-30:t1", "XP-30:t2", "XP-30:t3"]  # account scope: all
    for path in stores.values():
        c = _db(path)
        assert HaltRepo(c).active()[0]["reason"].startswith("book_invariant")
        c.close()
    assert len(ops.posts) == 3


# --- shared-mode guards: an arm never acts on the whole account ---------------------------


@pytest.mark.parametrize(
    "tool",
    ["close_position", "close_all_positions", "cancel_all_orders", "cancel_orders",
     "exercise_options_position"],
)  # fmt: skip
def test_hook_refuses_account_wide_tools_for_any_session(tool: str) -> None:
    """A shared-mode arm runs agent sessions under the same hook: account-wide close /
    cancel-all tools never pass, whatever id they carry."""
    token = _gated(_proposal()).token
    assert token is not None
    for coid in (attempt_order_id(token, 0, arm_code="1"), attempt_order_id(token, 0)):
        v = check_tool_call(f"mcp__alpaca__{tool}", {"client_order_id": coid},
                            secret=SECRET.encode(), now=NOW)  # fmt: skip
        assert not v.allow, tool


def test_hook_refuses_an_mcp_order_carrying_an_arm_tag() -> None:
    """Arm orders go out only through ``submit()``; the full tagged id is not a gate id."""
    p = _proposal()
    token = _gated(p).token
    assert token is not None
    args = {"client_order_id": attempt_order_id(token, 0, arm_code="1")}
    v = check_tool_call("mcp__alpaca__place_option_order", args, secret=SECRET.encode(),
                        now=NOW)  # fmt: skip
    assert not v.allow and "order refused" in v.message


class _Orders(_Account):
    def __init__(self) -> None:
        super().__init__(LIVE["fills"], LIVE["orders"], LIVE_POS, CASH_T0 - D("2015.33"))
        self.cancelled: list[str] = []
        self.dne: list[str] = []

    def order_status(self, broker_order_id: str) -> Any:
        o = next(o for o in LIVE["orders"] if o["id"] == broker_order_id)
        return type("S", (), {"client_order_id": o["client_order_id"]})()

    def cancel(self, broker_order_id: str) -> None:
        self.cancelled.append(broker_order_id)

    def cancel_by_client_id(self, client_order_id: str) -> None:
        self.cancelled.append(client_order_id)

    def do_not_exercise(self, occ: str) -> None:
        self.dne.append(occ)

    def activities(self, since: dt.date, **kw: Any) -> list[Any]:
        return [type("A", (), {"id": r["id"]})() for r in self.raw if r["activity_type"] != "FILL"]


def test_a_shared_arm_cancels_only_its_own_orders(tmp_path: Path) -> None:
    conn, stores = _shared_setup(tmp_path)
    acct = _Orders()
    t2 = _vb(conn, stores["t2"], acct, "XP-30:t2")  # code 1
    mine = [o for o in LIVE["orders"] if parse_order_id(o["client_order_id"]).arm_code == "1"]
    theirs = [o for o in LIVE["orders"] if parse_order_id(o["client_order_id"]).arm_code == "2"]
    assert mine and theirs
    t2.cancel(mine[0]["id"])
    t2.cancel_by_client_id(mine[0]["client_order_id"])
    with pytest.raises(SharedAccountError, match="may not touch order"):
        t2.cancel(theirs[0]["id"])
    with pytest.raises(SharedAccountError):
        t2.cancel_by_client_id(theirs[0]["client_order_id"])
    with pytest.raises(SharedAccountError):
        t2.cancel_by_client_id("arc-0a1b2c3d")  # smoke / untagged: nobody's
    assert acct.cancelled == [mine[0]["id"], mine[0]["client_order_id"]]


def test_a_shared_arm_sets_dne_only_on_a_contract_it_alone_holds(tmp_path: Path) -> None:
    conn, stores = _shared_setup(tmp_path)
    acct = _Orders()
    t2 = _vb(conn, stores["t2"], acct, "XP-30:t2")
    t2.do_not_exercise("HOOD261023P00111000")
    with pytest.raises(SharedAccountError, match="not XP-30:t2 alone"):
        t2.do_not_exercise("NVDA261106C00230000")  # t3's contract
    assert acct.dne == ["HOOD261023P00111000"]


def test_a_shared_arm_sees_only_its_own_option_events(tmp_path: Path) -> None:
    conn, stores = _shared_setup(tmp_path)
    acct = _Orders()
    acct.raw = [*LIVE["fills"], _opexp("HOOD261023P00111000", "-2"),
                _opexp("NVDA261106C00230000", "-1")]  # fmt: skip
    t2 = _vb(conn, stores["t2"], acct, "XP-30:t2")
    t3 = _vb(conn, stores["t3"], acct, "XP-30:t3")
    assert [a.id for a in t2.activities(DAY)] == [acct.raw[-2]["id"]]
    assert [a.id for a in t3.activities(DAY)] == [acct.raw[-1]["id"]]
    assert _vb(conn, stores["t1"], acct, "XP-30:t1").activities(DAY) == []


def _split_partial(fill: dict[str, Any]) -> list[dict[str, Any]]:
    """Alpaca's shape for a partial: one ``partial_fill`` row per execution."""
    q = int(fill["qty"])
    first = fill | {"id": fill["id"] + "a", "qty": "1", "type": "partial_fill",
                    "cum_qty": "1", "leaves_qty": str(q - 1),
                    "order_status": "partially_filled"}  # fmt: skip
    rest = fill | {"id": fill["id"] + "b", "qty": str(q - 1), "cum_qty": str(q),
                   "leaves_qty": "0"}  # fmt: skip
    return [first, rest]


def test_overlapping_arms_with_a_partial_fill_balance_and_halt_on_drift(tmp_path: Path) -> None:
    hood = next(f for f in LIVE["fills"] if f["symbol"].startswith("HOOD"))
    nvda = next(f for f in LIVE["fills"] if f["symbol"].startswith("NVDA"))
    # t2 (code 2 in WINDOWS) also buys 3 of t1's HOOD put through its own order
    t2_hood = hood | {"id": "20261012120000000::t2-hood", "qty": "3", "price": "4.90",
                      "order_id": nvda["order_id"], "cum_qty": "3",
                      "transaction_time": "2026-10-12T16:00:00Z"}  # fmt: skip
    fills = [f for f in LIVE["fills"] if f is not hood] + _split_partial(hood) + [t2_hood]
    pos = [p if not p.symbol.startswith("HOOD") else _pos(p.symbol, "5", "4.80")
           for p in LIVE_POS]  # fmt: skip
    cash = CASH_T0 - D("2015.00") - D("1470.00")
    book = build_book(fills, LIVE["orders"], WINDOWS, pos, broker_cash=cash, since=T0)
    assert book.attribution.arm_held("XP-30:t1")["HOOD261023P00111000"] == D(2)
    assert book.attribution.arm_held("XP-30:t2")["HOOD261023P00111000"] == D(3)
    assert account_violations(book) == []
    # broker shows 6: the one-contract drift sits on a symbol two arms hold -> account
    drift = [p if not p.symbol.startswith("HOOD") else _pos(p.symbol, "6") for p in pos]
    vs = account_violations(build_book(fills, LIVE["orders"], WINDOWS, drift,
                                       broker_cash=cash, since=T0))  # fmt: skip
    assert [(v.scope, v.kind) for v in vs] == [("account", "qty")]


def _tick(conn: sqlite3.Connection, account: _Account) -> dict[str, Any]:
    def reader(keys_env: str) -> Any:
        return account_book(account, conn, account_sha256=None, keys_env=keys_env)

    at = dt.datetime(2026, 10, 12, 15, 5, tzinfo=ET)
    return arms_tick(conn, routines_path=None, now=at, lock_dir=None,
                     handlers=_stub_handlers([]), account_number=lambda k: ACCT,
                     notifier=RecordingOpsNotifier(), book_reader=reader)  # fmt: skip


def _halted(stores: dict[str, Path]) -> list[str]:
    out = []
    for name, path in sorted(stores.items()):
        c = _db(path)
        if HaltRepo(c).active():
            out.append(name)
        c.close()
    return out


def test_tick_an_untagged_leg_halts_every_arm(tmp_path: Path) -> None:
    conn, stores = _shared_setup(tmp_path)
    orders = [dict(o) for o in LIVE["orders"]]
    mleg = next(o for o in orders if o["order_class"] == "mleg")
    mleg["client_order_id"] = parse_order_id(mleg["client_order_id"]).gate_id  # no code
    cash = CASH_T0 - D("2015.33")
    report = _tick(conn, _Account(LIVE["fills"], orders, LIVE_POS, cash))
    kinds = {v["kind"] for v in report["books"]["ALPACA_EXP"]["violations"]}
    assert {"order", "untagged"} <= kinds
    assert _halted(stores) == ["t1", "t2", "t3"]


def test_tick_a_one_arm_drift_halts_only_that_arm(tmp_path: Path) -> None:
    conn, stores = _shared_setup(tmp_path)  # fixture code 1 -> t2, code 2 -> t3
    pos = [p if not p.symbol.startswith("NVDA") else _pos(p.symbol, "2") for p in LIVE_POS]
    report = _tick(conn, _Account(LIVE["fills"], LIVE["orders"], pos, CASH_T0 - D("2015.33")))
    assert report["books"]["ALPACA_EXP"]["halted"] == ["XP-30:t3"]
    assert _halted(stores) == ["t3"]


def test_tick_a_balanced_account_halts_nothing(tmp_path: Path) -> None:
    conn, stores = _shared_setup(tmp_path)
    report = _tick(conn, _Account(LIVE["fills"], LIVE["orders"], LIVE_POS,
                                  CASH_T0 - D("2015.33")))  # fmt: skip
    assert report["books"]["ALPACA_EXP"]["violations"] == []
    assert _halted(stores) == []
