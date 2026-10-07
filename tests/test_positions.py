"""E6.4 position manager: sizing (D18 + S-7), capacity rejection, evaluator, swap scorer.

Pure-function tests; the routine chain (gate + approval + swaps) is in
tests/test_positions_steps.py.
"""

from __future__ import annotations

import datetime as dt
from collections import Counter
from decimal import Decimal as D

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from arc.exits import ExitConfig, ExitPolicy, TimeAdjustedTarget
from arc.exits.policy import PositionsConfig, StopBasis, StopRule
from arc.exits.position import OpenPosition, PositionMarks
from arc.gate.rules import CapacityRejection, RuleCode
from arc.models import StructureKind
from arc.positions.evaluate import PositionReview, SignalKind, review_position
from arc.positions.reallocate import CapacityCandidate, ReallocRules, score_swaps
from arc.sizing import size_contracts
from arc.structures import credit_vertical, debit_vertical, long_call

AS_OF = dt.date(2026, 9, 25)
EXP = dt.date(2026, 10, 30)  # 35 DTE
NEAR = dt.date(2026, 10, 3)  # 8 DTE


# ---------------------------------------------------------------------------
# Sizing: D18 + Sentinel S-7 (remaining per-underlying budget)
# ---------------------------------------------------------------------------


def test_size_contracts_d18_min_of_suggestion_and_cap() -> None:
    r = size_contracts(
        suggestion=3, max_loss_per_contract=D("400"), equity=D("100000"), cap_pct=0.05
    )
    assert (r.contracts, r.cap_contracts, r.code) == (3, 12, "ok")
    r = size_contracts(
        suggestion=50, max_loss_per_contract=D("400"), equity=D("100000"), cap_pct=0.05
    )
    assert (r.contracts, r.code) == (12, "capped")
    assert r.max_loss_total == D("4800") and r.pct_equity == pytest.approx(0.048)


def test_size_contracts_no_trade_when_one_contract_exceeds_cap() -> None:
    r = size_contracts(
        suggestion=2, max_loss_per_contract=D("6000"), equity=D("100000"), cap_pct=0.05
    )
    assert (r.contracts, r.code, r.trade) == (0, "cap_zero", False)


def test_size_contracts_uses_remaining_budget() -> None:
    """Sentinel S-7 repro: 100k equity, 334.45/contract, existing SPY max loss 1,000 -> 11."""
    r = size_contracts(
        suggestion=20,
        max_loss_per_contract=D("334.45"),
        equity=D("100000"),
        cap_pct=0.05,
        existing_max_loss=D("1000"),
    )
    assert r.contracts == 11
    assert r.existing_max_loss == D("1000")
    assert r.max_loss_total + D("1000") <= D("5000")
    # without the existing exposure the old sizing gave 14, which the gate rejected
    assert (
        size_contracts(
            suggestion=20, max_loss_per_contract=D("334.45"), equity=D("100000"), cap_pct=0.05
        ).contracts
        == 14
    )


def test_size_contracts_zero_when_budget_exhausted() -> None:
    r = size_contracts(
        suggestion=5,
        max_loss_per_contract=D("334.45"),
        equity=D("100000"),
        cap_pct=0.05,
        existing_max_loss=D("4800"),
    )
    assert (r.contracts, r.code) == (0, "budget_exhausted")
    assert "existing max loss 4800" in r.reason
    over = size_contracts(
        suggestion=5,
        max_loss_per_contract=D("100"),
        equity=D("100000"),
        cap_pct=0.05,
        existing_max_loss=D("9000"),  # already over the cap
    )
    assert (over.contracts, over.cap_contracts, over.code) == (0, 0, "budget_exhausted")


@settings(max_examples=300, deadline=None)
@given(
    equity=st.decimals(min_value=D("1000"), max_value=D("5000000"), places=2),
    cap_pct=st.decimals(min_value=D("0.005"), max_value=D("0.25"), places=3),
    per=st.decimals(min_value=D("1"), max_value=D("50000"), places=2),
    existing=st.decimals(min_value=D("0"), max_value=D("500000"), places=2),
    suggestion=st.integers(min_value=0, max_value=500),
)
def test_sizing_never_exceeds_remaining_budget(
    equity: D, cap_pct: D, per: D, existing: D, suggestion: int
) -> None:
    r = size_contracts(
        suggestion=suggestion,
        max_loss_per_contract=per,
        equity=equity,
        cap_pct=cap_pct,
        existing_max_loss=existing,
    )
    assert r.contracts * per + existing <= cap_pct * equity or r.contracts == 0
    assert r.contracts <= suggestion
    if r.contracts:
        # D18: the smaller of Risk and the cap, and at least one contract when one fits
        assert r.contracts == min(suggestion, r.cap_contracts)
    elif suggestion > 0 and cap_pct * equity - existing >= per:
        pytest.fail("one contract fits under the remaining budget but none was sized")


# ---------------------------------------------------------------------------
# Evaluator: profit target per structure type, time-adjusted target, EV floor
# ---------------------------------------------------------------------------


def _put_spread(exp: dt.date = EXP):
    return credit_vertical(
        "put", "SPY", exp, short_strike=711, short_premium="5.60",
        long_strike=710, long_premium="4.70", as_of=AS_OF,
    )  # fmt: skip


def _call_debit(exp: dt.date = EXP):
    return debit_vertical(
        "call", "SPY", exp, long_strike=775, long_premium="8.00",
        short_strike=785, short_premium="3.00", as_of=AS_OF,
    )  # fmt: skip


def _long_call(exp: dt.date = EXP):
    return long_call("SPY", exp, strike=775, premium="10.00", as_of=AS_OF)


def _marks(st, values: dict[str, float], **kw) -> PositionMarks:  # type: ignore[no-untyped-def]
    mids = {leg.occ_symbol: values[leg.occ_symbol[9:10] + leg.occ_symbol[-8:-3]] for leg in st.legs}
    return PositionMarks(as_of=AS_OF, leg_mids=mids, **kw)


def _cfg(**policy: object) -> ExitConfig:
    return ExitConfig(default=ExitPolicy(**policy), positions=PositionsConfig())  # type: ignore[arg-type]


def _review(st, entry: float, vals: dict[str, float], cfg: ExitConfig, **kw) -> PositionReview:  # type: ignore[no-untyped-def]
    return review_position(
        structure_id="os-1",
        ticker="SPY",
        position=OpenPosition(structure=st, entry_net=entry, contracts=1),
        marks=_marks(st, vals, **kw),
        exits=cfg,
    )


@pytest.mark.parametrize(
    ("close_debit", "fires"),
    [(0.46, False), (0.45, True), (0.10, True)],  # credit 0.90: 50% of max gain = 0.45 debit
)
def test_profit_target_credit_structure(close_debit: float, fires: bool) -> None:
    st = _put_spread()
    rv = _review(st, -0.90, {"P00711": close_debit + 0.30, "P00710": 0.30}, _cfg())
    assert rv.credit and rv.pct_of_max_gain == pytest.approx((0.90 - close_debit) / 0.90, abs=1e-4)
    assert (rv.signal is not None and rv.signal.kind is SignalKind.PROFIT_TARGET) is fires


@pytest.mark.parametrize(("value", "fires"), [(3.99, False), (4.00, True)])  # debit 2.00, +100%
def test_profit_target_debit_vertical(value: float, fires: bool) -> None:
    st = _call_debit()
    rv = _review(st, 2.00, {"C00775": value + 1.0, "C00785": 1.0}, _cfg())
    assert not rv.credit and rv.pct_of_debit == pytest.approx((value - 2.0) / 2.0, abs=1e-4)
    assert (rv.signal is not None and rv.signal.kind is SignalKind.PROFIT_TARGET) is fires


@pytest.mark.parametrize(("value", "fires"), [(19.0, False), (20.0, True)])
def test_profit_target_long_option(value: float, fires: bool) -> None:
    st = _long_call()
    rv = _review(st, 10.0, {"C00775": value}, _cfg())
    assert rv.pct_of_debit == pytest.approx((value - 10.0) / 10.0)
    assert (rv.signal is not None and rv.signal.kind is SignalKind.PROFIT_TARGET) is fires


def test_time_adjusted_target_fires_only_inside_its_dte_bucket() -> None:
    cfg = _cfg(time_adjusted_targets=[TimeAdjustedTarget(dte_lte=10, take_profit_pct=0.25)])
    vals = {"P00711": 0.95, "P00710": 0.30}  # close debit 0.65: 28% of max gain
    far = _review(_put_spread(EXP), -0.90, vals, cfg)
    assert far.signal is None and far.take_profit_pct == 0.50
    near = _review(_put_spread(NEAR), -0.90, vals, cfg)
    assert near.dte == 8 and near.take_profit_pct == 0.25
    assert near.signal is not None and near.signal.kind is SignalKind.TIME_ADJUSTED_TARGET
    assert "time-adjusted target 25%" in near.signal.detail


def test_stop_precedes_profit_and_is_eod_only() -> None:
    cfg = _cfg(stop=StopRule(basis=StopBasis.PCT_DEBIT, value=0.5))
    vals = {"C00775": 1.4, "C00785": 0.5}  # value 0.90 on a 2.00 debit: -55%
    intraday = _review(_call_debit(), 2.0, vals, cfg, end_of_day=False)
    assert intraday.signal is None  # D23: stops on end-of-day marks only
    eod = _review(_call_debit(), 2.0, vals, cfg, end_of_day=True)
    assert eod.signal is not None and eod.signal.kind is SignalKind.STOP
    assert eod.pct_of_max_loss == pytest.approx(0.55, abs=1e-3)


def test_dte_exit_signal() -> None:
    cfg = _cfg(close_at_dte=10)
    rv = _review(_put_spread(NEAR), -0.90, {"P00711": 1.5, "P00710": 0.30}, cfg)
    assert rv.signal is not None and rv.signal.kind is SignalKind.DTE_EXIT


def _model_marks(**kw: float) -> dict[str, float]:
    return {"spot": 771.3, "iv": 0.16, "r": 0.04, **kw}


def _floor(v: float | None) -> ExitConfig:
    return ExitConfig(positions=PositionsConfig(remaining_ev_floor_per_bp=v))


def test_remaining_ev_floor_suggests_close_when_capital_is_idle() -> None:
    st = _long_call()
    vals = {"C00775": 12.0}
    off = _review(st, 12.0, vals, _floor(None), **_model_marks())
    assert off.remaining_ev is not None and off.remaining_ev_per_bp is not None
    assert off.remaining_pop is not None and 0.0 <= off.remaining_pop <= 1.0
    assert off.signal is None
    above = off.remaining_ev_per_bp + 0.001
    below = off.remaining_ev_per_bp - 0.001
    hit = _review(st, 12.0, vals, _floor(above), **_model_marks())
    assert hit.signal is not None and hit.signal.kind is SignalKind.REMAINING_EV_FLOOR
    assert "remaining EV per $ BP" in hit.signal.detail
    miss = _review(st, 12.0, vals, _floor(below), **_model_marks())
    assert miss.signal is None
    # per-kind override
    kinds = PositionsConfig(remaining_ev_floor_per_bp=None, kinds={StructureKind.LONG_CALL: above})
    assert _review(st, 12.0, vals, ExitConfig(positions=kinds), **_model_marks()).signal is not None


def test_review_without_model_inputs_has_no_ev_and_no_floor_signal() -> None:
    rv = _review(_long_call(), 10.0, {"C00775": 11.0}, _cfg())
    assert rv.remaining_ev is None and rv.remaining_ev_per_bp is None and rv.signal is None


# ---------------------------------------------------------------------------
# Reallocation scorer
# ---------------------------------------------------------------------------


def _rv(
    sid: str = "os-1",
    ticker: str = "SPY",
    *,
    rem_bp: float | None = 0.01,
    pop: float | None = 0.5,
    bp: float = 500.0,
    close_now: float = -5.0,
    value: float = 3.0,
) -> PositionReview:
    st = _call_debit()
    return PositionReview(
        structure_id=sid, ticker=ticker, kind="vertical_debit", credit=False, contracts=1,
        as_of=AS_OF, dte=35, entry_net=2.0, current_value=value, pnl=100.0, pnl_total=100.0,
        buying_power=bp, close_now_net=close_now,
        remaining_ev=None if rem_bp is None else rem_bp * bp, remaining_ev_per_bp=rem_bp,
        remaining_pop=pop, structure=st,
    )  # fmt: skip


def _cand(
    ref: str = "p-1",
    ticker: str = "SPY",
    *,
    ev: float = 50.0,
    bp: float = 200.0,
    pop: float = 0.6,
    codes: tuple[str, ...] = (RuleCode.PER_UNDERLYING.value,),
    why: CapacityRejection = CapacityRejection.BUYING_POWER,
) -> CapacityCandidate:
    return CapacityCandidate(
        source_ref=ref, ticker=ticker, rejected_for=why, violation_codes=list(codes),
        net_ev=ev, pop=pop, buying_power=bp,
    )  # fmt: skip


RULES = ReallocRules(min_edge=0.20, pop_tolerance=0.05, max_per_day=2, max_per_ticker_per_day=1)


def test_swap_suggested_when_edge_clears_costs() -> None:
    sugg, pairs = score_swaps([_rv()], [_cand()], RULES)
    assert len(sugg) == 1 and pairs[0].outcome == "suggested"
    s = sugg[0]
    assert s.new_ev_per_bp == pytest.approx(0.25)
    assert s.switching_cost_per_bp == pytest.approx(0.01)  # 5 / 500
    assert s.edge == pytest.approx(0.25 - 0.01 - 0.01)
    assert s.close_structure_id == "os-1" and s.source_ref == "p-1"


def test_swap_edge_must_beat_switching_costs() -> None:
    # new 0.02/$BP vs open 0.01/$BP: a raw edge, but the close cost (0.01/$BP) eats it
    sugg, pairs = score_swaps([_rv()], [_cand(ev=4.0)], RULES)
    assert not sugg and pairs[0].outcome == "edge_below_min"
    # same numbers, free to close -> edge 0.01 vs required 20% x 0.02 = 0.004
    sugg, _ = score_swaps([_rv(close_now=0.0)], [_cand(ev=4.0)], RULES)
    assert len(sugg) == 1


def test_swap_needs_relative_min_edge() -> None:
    # new 0.105 vs open 0.10: edge 0.005 - 0 < 20% x 0.105
    sugg, pairs = score_swaps([_rv(rem_bp=0.10, close_now=0.0)], [_cand(ev=21.0)], RULES)
    assert not sugg and pairs[0].outcome == "edge_below_min"
    loose = RULES.model_copy(update={"min_edge": 0.0})
    assert score_swaps([_rv(rem_bp=0.10, close_now=0.0)], [_cand(ev=21.0)], loose)[0]


def test_swap_pop_tolerance() -> None:
    sugg, pairs = score_swaps([_rv(pop=0.70)], [_cand(pop=0.64)], RULES)
    assert not sugg and pairs[0].outcome == "pop_below_open"
    assert score_swaps([_rv(pop=0.70)], [_cand(pop=0.65)], RULES)[0]


def test_swap_must_free_what_the_new_trade_lacks() -> None:
    # per-underlying limit on QQQ: closing SPY frees nothing
    sugg, pairs = score_swaps([_rv(ticker="SPY")], [_cand(ticker="QQQ")], RULES)
    assert not sugg and pairs[0].outcome == "frees_nothing"
    # max positions: any close frees a slot
    mp = _cand(
        ticker="QQQ", codes=(RuleCode.MAX_POSITIONS.value,), why=CapacityRejection.PORTFOLIO_CAP
    )
    assert score_swaps([_rv(ticker="SPY")], [mp], RULES)[0]
    # settled cash: closing a short-value (credit) position returns no cash
    cash = _cand(ticker="QQQ", codes=(RuleCode.ACCOUNT_CASH.value,))
    assert not score_swaps([_rv(value=-1.0)], [cash], RULES)[0]
    assert score_swaps([_rv(value=3.0)], [cash], RULES)[0]


def test_swap_skips_positions_without_numbers_or_already_exiting() -> None:
    sugg, pairs = score_swaps([_rv(rem_bp=None)], [_cand()], RULES)
    assert not sugg and pairs[0].outcome == "no_open_numbers"
    exiting = _rv().model_copy(update={"exit_pending": True})
    assert score_swaps([exiting], [_cand()], RULES) == ([], [])


def test_churn_limits_per_ticker_and_per_day() -> None:
    opens = [_rv("os-1", "SPY"), _rv("os-2", "QQQ"), _rv("os-3", "IWM")]
    mp = (RuleCode.MAX_POSITIONS.value,)
    cands = [
        _cand("p-1", "SPY", ev=60.0, codes=mp, why=CapacityRejection.PORTFOLIO_CAP),
        _cand("p-2", "SPY", ev=55.0, codes=mp, why=CapacityRejection.PORTFOLIO_CAP),
        _cand("p-3", "TLT", ev=50.0, codes=mp, why=CapacityRejection.PORTFOLIO_CAP),
        _cand("p-4", "GLD", ev=45.0, codes=mp, why=CapacityRejection.PORTFOLIO_CAP),
    ]
    sugg, pairs = score_swaps(opens, cands, RULES)
    assert len(sugg) == 2  # at most 2 per day
    # at most 1 swap per ticker per day (a same-ticker swap counts once)
    per_ticker = Counter(t for s in sugg for t in {s.close_ticker, s.open_ticker})
    assert max(per_ticker.values()) == 1
    outcomes = {p.outcome for p in pairs}
    assert {"churn_ticker", "churn_day"} <= outcomes | {"already_paired"}
    assert "churn_day" in outcomes
    # counts carried over from earlier swaps today
    assert score_swaps(opens, cands, RULES, swaps_today=2)[0] == []
    one_left, _ = score_swaps(opens, cands, RULES, swaps_today=1, ticker_swaps_today={"SPY": 1})
    assert len(one_left) == 1 and "SPY" not in (one_left[0].open_ticker, one_left[0].close_ticker)


@settings(max_examples=200, deadline=None)
@given(
    new_ev=st.floats(min_value=-200, max_value=500),
    new_bp=st.floats(min_value=10, max_value=5000),
    new_pop=st.floats(min_value=0, max_value=1),
    rem_bp=st.floats(min_value=-0.5, max_value=0.5),
    open_bp=st.floats(min_value=10, max_value=5000),
    close_cost=st.floats(min_value=0, max_value=50),
    open_pop=st.floats(min_value=0, max_value=1),
)
def test_suggested_swaps_always_clear_costs_edge_and_pop(
    new_ev: float,
    new_bp: float,
    new_pop: float,
    rem_bp: float,
    open_bp: float,
    close_cost: float,
    open_pop: float,
) -> None:
    rv = _rv(rem_bp=rem_bp, pop=open_pop, bp=open_bp, close_now=-close_cost)
    c = _cand(ev=new_ev, bp=new_bp, pop=new_pop)
    sugg, pairs = score_swaps([rv], [c], RULES)
    assert len(pairs) == 1
    if sugg:
        s = sugg[0]
        new = new_ev / new_bp
        assert new - rem_bp - close_cost / open_bp > 0
        assert s.edge >= RULES.min_edge * max(abs(new), abs(rem_bp)) - 1e-6  # edge is rounded
        assert new_pop >= open_pop - RULES.pop_tolerance
        assert new > rem_bp  # never swaps into a worse EV per $ of buying power


def test_positions_cli_fixture_dry_run(capsys: pytest.CaptureFixture[str]) -> None:
    from arc.cli import main

    assert main(["positions", "review", "--fixtures"]) == 0
    out = capsys.readouterr().out
    assert "Position reviews (3)" in out
    assert "close SPY fx-bull-put: profit_target" in out
    assert "close SPY fx-call-debit: profit_target" in out  # debit structures too (D25)
    assert "Suggested swaps (1;" in out and "swap SPY -> SPY" in out
    assert main(["positions", "review", "--fixtures", "--json"]) == 0
    import json as _json

    data = _json.loads(capsys.readouterr().out)
    assert {r["structure_id"] for r in data["reviews"]} == {
        "fx-bull-put", "fx-call-debit", "fx-long-call"
    }  # fmt: skip
    assert data["suggestions"][0]["close_structure_id"] == "fx-long-call"


def test_realloc_settings_feed_the_scorer(monkeypatch: pytest.MonkeyPatch) -> None:
    from arc.config import ArcSettings
    from arc.positions.reallocate import ReallocRules

    def _rules(s: ArcSettings) -> ReallocRules:  # as quant.propose builds them (E13.15)
        return ReallocRules(
            min_edge=s.realloc_min_edge,
            pop_tolerance=s.realloc_pop_tolerance,
            max_per_day=s.realloc_max_swaps_per_day,
            max_per_ticker_per_day=s.realloc_max_swaps_per_ticker_per_day,
        )

    monkeypatch.setenv("ARC_REALLOC_MIN_EDGE", "0.5")
    monkeypatch.setenv("ARC_REALLOC_MAX_SWAPS_PER_DAY", "1")
    r = _rules(ArcSettings(_env_file=None))  # type: ignore[call-arg]
    assert (r.min_edge, r.pop_tolerance, r.max_per_day, r.max_per_ticker_per_day) == (
        0.5, 0.05, 1, 1
    )  # fmt: skip


def test_d19_rules_backtest_alias() -> None:
    from arc.backtest.engine import _POLICY_MODES

    assert "d19_rules" in _POLICY_MODES and "policy" in _POLICY_MODES
