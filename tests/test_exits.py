"""E2.4: exit policy, managed-exit Monte Carlo model, evaluate_position, wiring."""

from __future__ import annotations

import datetime as dt
import json
import time
from typing import TYPE_CHECKING

import numpy as np
import pytest
import yaml
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from arc.backtest.costs import CostModel
from arc.exits import (
    DEFAULT_EXITS_PATH,
    HOLD_TO_EXPIRY,
    ExitConfig,
    ExitModelConfig,
    ExitPolicy,
    ExitReason,
    ExitSummary,
    OpenPosition,
    PositionMarks,
    StopBasis,
    StopRule,
    TimeAdjustedTarget,
    analytic_pop,
    check_rules,
    evaluate_position,
    load_exit_config,
    model_exits,
    realized_vol_forecast,
    resolve_rules,
)
from arc.exits.model import iv_path, sim_legs, simulate
from arc.exits.policy import IvModel
from arc.models import StructureKind
from arc.structures import credit_vertical, debit_vertical, iron_condor, long_call

if TYPE_CHECKING:
    from pathlib import Path

    from arc.models import Structure

AS_OF = dt.date(2026, 9, 25)
EXP = dt.date(2026, 10, 28)  # 33 DTE
SPOT = 100.0
IV = 0.20
R = 0.04
NO_COST = CostModel(slippage_frac=0.0, commission_per_contract=0.0)
FAST = ExitModelConfig(n_paths=4000, seed=7)


def condor(credit_scale: float = 1.0) -> Structure:
    return iron_condor(
        "TST",
        EXP,
        long_put_strike=85,
        long_put_premium=0.30,
        short_put_strike=90,
        short_put_premium=round(0.80 * credit_scale, 4),
        short_call_strike=110,
        short_call_premium=round(0.75 * credit_scale, 4),
        long_call_strike=115,
        long_call_premium=0.25,
        as_of=AS_OF,
    )


def bull_put() -> Structure:
    return credit_vertical(
        "put",
        "TST",
        EXP,
        short_strike=95,
        short_premium=1.60,
        long_strike=90,
        long_premium=0.60,
        as_of=AS_OF,
    )


def bull_call() -> Structure:
    return debit_vertical(
        "call",
        "TST",
        EXP,
        long_strike=100,
        long_premium=2.60,
        short_strike=105,
        short_premium=0.90,
        as_of=AS_OF,
    )


def lcall() -> Structure:
    return long_call("TST", EXP, 100, 2.60, as_of=AS_OF)


CREDIT_POLICY = ExitPolicy(
    take_profit_pct_of_max_gain=0.5,
    take_profit_pct_of_debit=None,
    stop=StopRule(basis=StopBasis.CREDIT_MULTIPLE, value=2.0),
    close_at_dte=7,
)
DEBIT_POLICY = ExitPolicy(
    take_profit_pct_of_max_gain=None,
    take_profit_pct_of_debit=1.0,
    stop=StopRule(basis=StopBasis.PCT_DEBIT, value=0.5),
    close_at_dte=7,
)


# ---------------------------------------------------------------------------
# Policy config
# ---------------------------------------------------------------------------


class TestPolicyConfig:
    def test_repo_config_loads_per_kind(self) -> None:
        cfg = load_exit_config()
        assert DEFAULT_EXITS_PATH.name == "exits.yaml"
        for kind in (StructureKind.VERTICAL_CREDIT, StructureKind.IRON_CONDOR):
            p = cfg.policy_for(kind)
            assert p.take_profit_pct_of_max_gain == 0.5
            # D23: relaxed stops (75% of max loss), end-of-day marks only
            assert p.stop == StopRule(basis=StopBasis.PCT_MAX_LOSS, value=0.75)
            assert p.stop_eod_only
            assert p.close_at_dte == 7
        for kind in (StructureKind.VERTICAL_DEBIT, StructureKind.LONG_CALL, StructureKind.LONG_PUT):
            p = cfg.policy_for(kind)
            assert p.take_profit_pct_of_debit == 1.0
            assert p.stop == StopRule(basis=StopBasis.PCT_DEBIT, value=0.75)
            assert p.stop_eod_only
        assert cfg.policy_for(StructureKind.OTHER) == cfg.default
        assert cfg.policy_for(None) == cfg.default
        assert cfg.model.n_paths == 20_000
        assert cfg.pipeline.rank_menu_by == "scanner"  # scanner rank_by unchanged by default
        assert cfg.model.path_vol == "realized_forecast"

    def test_threshold_change_needs_no_code_change(self, tmp_path: Path) -> None:
        data = yaml.safe_load(DEFAULT_EXITS_PATH.read_text())
        data["kinds"]["iron_condor"]["take_profit_pct_of_max_gain"] = 0.25
        data["kinds"]["iron_condor"]["close_at_dte"] = 21
        data["kinds"]["iron_condor"]["time_adjusted_targets"] = [
            {"dte_lte": 14, "take_profit_pct": 0.35}
        ]
        data["model"]["n_paths"] = 1234
        p = tmp_path / "exits.yaml"
        p.write_text(yaml.safe_dump(data))
        cfg = load_exit_config(p)
        pol = cfg.policy_for(StructureKind.IRON_CONDOR)
        assert pol.take_profit_pct_of_max_gain == 0.25
        assert pol.close_at_dte == 21
        assert pol.take_profit_pct(credit=True, dte=30) == 0.25
        assert pol.take_profit_pct(credit=True, dte=14) == 0.35
        assert cfg.model.n_paths == 1234
        # and the model picks it up
        r = model_exits(
            condor(), pol, spot=SPOT, iv=IV, r=R, cfg=cfg.model.model_copy(update={"n_paths": 500})
        )
        assert r.policy.take_profit_pct_of_max_gain == 0.25
        assert r.take_profit is not None
        assert r.take_profit.pnl == pytest.approx(0.25 * (0.80 + 0.75 - 0.30 - 0.25), abs=1e-4)

    def test_extra_keys_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ExitConfig.model_validate({"default": {"take_profit": 0.5}})
        with pytest.raises(ValidationError):
            ExitConfig.model_validate({"kinds": {"straddle": {}}})

    def test_stop_basis_must_match_direction(self) -> None:
        with pytest.raises(ValidationError, match="credit structures only"):
            ExitConfig.model_validate(
                {"kinds": {"long_call": {"stop": {"basis": "credit_multiple", "value": 2}}}}
            )
        with pytest.raises(ValidationError, match="debit structures only"):
            ExitConfig.model_validate(
                {"kinds": {"iron_condor": {"stop": {"basis": "pct_debit", "value": 0.5}}}}
            )

    def test_stop_fraction_range(self) -> None:
        with pytest.raises(ValidationError, match="fraction"):
            StopRule(basis=StopBasis.PCT_MAX_LOSS, value=1.5)
        assert StopRule(basis=StopBasis.CREDIT_MULTIPLE, value=3.0).value == 3.0
        with pytest.raises(ValidationError, match="> 1"):
            StopRule(basis=StopBasis.CREDIT_MULTIPLE, value=1.0)

    def test_time_adjusted_targets_sorted_and_unique(self) -> None:
        p = ExitPolicy(
            time_adjusted_targets=[
                TimeAdjustedTarget(dte_lte=21, take_profit_pct=0.4),
                TimeAdjustedTarget(dte_lte=10, take_profit_pct=0.25),
            ]
        )
        assert [t.dte_lte for t in p.time_adjusted_targets] == [10, 21]
        assert p.take_profit_pct(credit=True, dte=30) == 0.5
        assert p.take_profit_pct(credit=True, dte=21) == 0.4
        assert p.take_profit_pct(credit=True, dte=5) == 0.25  # tightest bucket wins
        assert p.take_profit_pct(credit=False, dte=30) == 1.0
        with pytest.raises(ValidationError, match="unique"):
            ExitPolicy(
                time_adjusted_targets=[
                    TimeAdjustedTarget(dte_lte=10, take_profit_pct=0.4),
                    TimeAdjustedTarget(dte_lte=10, take_profit_pct=0.3),
                ]
            )

    def test_iv_model_needs_params(self) -> None:
        with pytest.raises(ValidationError, match="long_run"):
            IvModel(kind="mean_reverting")

    def test_summary_mentions_every_rule(self) -> None:
        s = CREDIT_POLICY.model_copy(
            update={"time_adjusted_targets": [TimeAdjustedTarget(dte_lte=14, take_profit_pct=0.3)]}
        ).summary()
        assert "50% of max gain" in s
        assert "credit_multiple 2" in s
        assert "7 DTE" in s
        assert "≤14 DTE" in s
        assert "(end of day)" in s
        assert "no stop" in HOLD_TO_EXPIRY.summary()


# ---------------------------------------------------------------------------
# Rule resolution
# ---------------------------------------------------------------------------


class TestRules:
    def test_credit_thresholds_hand_values(self) -> None:
        # $1.66 credit vertical: TP 50% → $0.83 debit to close; "2x credit" stop → buy back
        # at a $3.32 debit (a $1.66 loss)
        s = credit_vertical(
            "put",
            "TST",
            EXP,
            short_strike=95,
            short_premium=2.16,
            long_strike=90,
            long_premium=0.50,
            as_of=AS_OF,
        )
        rules = resolve_rules(s, CREDIT_POLICY)
        assert rules.credit
        assert rules.entry_net == pytest.approx(-1.66)
        assert rules.tp_pnl(30) == pytest.approx(0.83)
        assert abs(rules.value_at_pnl(rules.tp_pnl(30) or 0)) == pytest.approx(0.83)
        assert rules.stop_pnl == pytest.approx(-1.66)
        assert abs(rules.value_at_pnl(rules.stop_pnl or 0)) == pytest.approx(3.32)
        # D23 default: 75% of max loss (5 − 1.66 = 3.34) → a $2.505 loss, $4.165 debit
        d23 = CREDIT_POLICY.model_copy(
            update={"stop": StopRule(basis=StopBasis.PCT_MAX_LOSS, value=0.75)}
        )
        r23 = resolve_rules(s, d23)
        assert r23.stop_pnl == pytest.approx(-2.505)
        assert abs(r23.value_at_pnl(r23.stop_pnl or 0)) == pytest.approx(4.165)

    def test_debit_thresholds(self) -> None:
        rules = resolve_rules(lcall(), DEBIT_POLICY)
        assert not rules.credit
        assert rules.tp_pnl(30) == pytest.approx(2.60)  # 100% of debit
        assert rules.stop_pnl == pytest.approx(-1.30)  # half the debit

    def test_pct_max_loss_stop(self) -> None:
        pol = CREDIT_POLICY.model_copy(
            update={"stop": StopRule(basis=StopBasis.PCT_MAX_LOSS, value=0.5)}
        )
        rules = resolve_rules(bull_put(), pol)
        assert rules.stop_pnl == pytest.approx(-0.5 * 4.0)  # width 5 − credit 1 = 4

    def test_stop_basis_mismatch_raises(self) -> None:
        with pytest.raises(ValueError, match="debit structure"):
            _ = resolve_rules(lcall(), CREDIT_POLICY).stop_pnl
        with pytest.raises(ValueError, match="credit structure"):
            _ = resolve_rules(bull_put(), DEBIT_POLICY).stop_pnl

    def test_entry_override_shifts_max_gain_loss(self) -> None:
        mid = resolve_rules(bull_put(), CREDIT_POLICY)
        better = resolve_rules(bull_put(), CREDIT_POLICY, entry_net=-1.10)
        assert better.max_gain == pytest.approx((mid.max_gain or 0) + 0.10)
        assert better.max_loss == pytest.approx((mid.max_loss or 0) - 0.10)

    def test_check_rules_order(self) -> None:
        rules = resolve_rules(bull_put(), CREDIT_POLICY)
        # stop wins over everything
        assert check_rules(rules, pnl=-2.5, dte=3) is ExitReason.STOP
        # take profit wins over DTE exit
        assert check_rules(rules, pnl=0.6, dte=3) is ExitReason.TAKE_PROFIT
        assert check_rules(rules, pnl=0.1, dte=7) is ExitReason.DTE_EXIT
        assert check_rules(rules, pnl=0.1, dte=8) is None
        assert check_rules(resolve_rules(bull_put(), HOLD_TO_EXPIRY), pnl=-4, dte=1) is None

    def test_stop_eod_only(self) -> None:
        rules = resolve_rules(bull_put(), CREDIT_POLICY)
        assert CREDIT_POLICY.stop_eod_only
        # intraday marks never trigger an EOD-only stop; TP / DTE exit still fire
        assert check_rules(rules, pnl=-2.5, dte=20, eod=False) is None
        assert check_rules(rules, pnl=-2.5, dte=3, eod=False) is ExitReason.DTE_EXIT
        assert check_rules(rules, pnl=0.6, dte=20, eod=False) is ExitReason.TAKE_PROFIT
        assert check_rules(rules, pnl=-2.5, dte=20, eod=True) is ExitReason.STOP
        anytime = resolve_rules(
            bull_put(), CREDIT_POLICY.model_copy(update={"stop_eod_only": False})
        )
        assert check_rules(anytime, pnl=-2.5, dte=20, eod=False) is ExitReason.STOP


# ---------------------------------------------------------------------------
# Monte Carlo model
# ---------------------------------------------------------------------------


class TestModel:
    def test_deterministic_same_seed(self) -> None:
        a = model_exits(condor(), CREDIT_POLICY, spot=SPOT, iv=IV, r=R, cfg=FAST)
        b = model_exits(condor(), CREDIT_POLICY, spot=SPOT, iv=IV, r=R, cfg=FAST)
        assert a == b
        c = model_exits(
            condor(), CREDIT_POLICY, spot=SPOT, iv=IV, r=R, cfg=FAST.model_copy(update={"seed": 8})
        )
        assert c.managed != a.managed

    def test_result_contract(self) -> None:
        r = model_exits(condor(), CREDIT_POLICY, spot=SPOT, iv=IV, r=R, cfg=FAST)
        assert r.model == "gbm_flat_iv"
        assert r.n_paths == 4000
        assert r.seed == 7
        assert r.iv_used == IV
        m = r.managed
        assert m.p_take_profit + m.p_stop + m.p_dte_exit + m.p_expiry == pytest.approx(1.0)
        assert m.p_expiry == 0.0  # close_at_dte=7 closes everything first
        assert 0 < m.expected_days_held <= r.dte
        assert r.dte_exit_day == r.dte - 7
        # the TP trigger: 50% of the credit, as a debit to close
        credit = -r.entry_net
        assert r.take_profit is not None
        assert r.take_profit.close_side == "debit"
        assert r.take_profit.close_price == pytest.approx(credit / 2, abs=1e-4)
        assert r.stop is not None
        assert r.stop.close_price == pytest.approx(2 * credit, abs=1e-4)
        # the 2x-credit stop on this $1.00 credit condor ($4 max loss) is reachable
        assert r.stop.reachable
        assert r.static.ev_per_bp_day is not None
        # JSON round trip
        assert type(r).model_validate_json(r.model_dump_json()) == r

    def test_unreachable_stop_flagged(self) -> None:
        # $1.67 credit, $5 wide: max loss $3.33 < a 3x-credit stop's 2 x credit loss
        s = iron_condor(
            "TST",
            EXP,
            long_put_strike=85,
            long_put_premium=0.30,
            short_put_strike=90,
            short_put_premium=1.20,
            short_call_strike=110,
            short_call_premium=1.07,
            long_call_strike=115,
            long_call_premium=0.30,
            as_of=AS_OF,
        )
        three_x = CREDIT_POLICY.model_copy(
            update={"stop": StopRule(basis=StopBasis.CREDIT_MULTIPLE, value=3.0)}
        )
        r = model_exits(s, three_x, spot=SPOT, iv=IV, r=R, cfg=FAST)
        assert r.stop is not None
        assert not r.stop.reachable
        assert r.managed.p_stop == 0.0

    def test_hold_to_expiry_policy_matches_static(self) -> None:
        r = model_exits(condor(), HOLD_TO_EXPIRY, spot=SPOT, iv=IV, r=R, cfg=FAST)
        assert r.managed.p_expiry == 1.0
        assert r.managed.gross_ev == r.static.gross_ev
        assert r.managed.net_ev == r.static.net_ev
        assert r.managed.pop == r.static.pop
        assert r.take_profit is None and r.stop is None and r.dte_exit_day is None

    @settings(max_examples=12, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(
        iv=st.floats(0.10, 0.60),
        spot=st.floats(88.0, 112.0),
        seed=st.integers(0, 10_000),
    )
    def test_static_pop_matches_analytic(self, iv: float, spot: float, seed: int) -> None:
        cfg = ExitModelConfig(n_paths=6000, seed=seed)
        s = condor()
        r = model_exits(s, HOLD_TO_EXPIRY, spot=spot, iv=iv, r=R, cost=NO_COST, cfg=cfg)
        p = analytic_pop(s, spot=spot, iv=iv, r=R)
        se = max(np.sqrt(p * (1 - p) / cfg.n_paths), 1e-3)
        # Daily GBM steps give the exact terminal lognormal, so this is pure MC error.
        assert abs(r.static.pop_gross - p) < 4.5 * se
        assert abs(r.managed.pop_gross - p) < 4.5 * se  # policy "none" = static

    @settings(max_examples=8, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(tp_lo=st.floats(0.15, 0.45), gap=st.floats(0.15, 0.5), seed=st.integers(0, 1000))
    def test_tighter_tp_hits_more_and_sooner(self, tp_lo: float, gap: float, seed: int) -> None:
        tp_hi = min(tp_lo + gap, 0.95)
        cfg = ExitModelConfig(n_paths=3000, seed=seed)
        base = CREDIT_POLICY.model_copy(update={"stop": None, "close_at_dte": None})
        lo = model_exits(
            condor(), base.model_copy(update={"take_profit_pct_of_max_gain": tp_lo}),
            spot=SPOT, iv=IV, r=R, cfg=cfg,
        )  # fmt: skip
        hi = model_exits(
            condor(), base.model_copy(update={"take_profit_pct_of_max_gain": tp_hi}),
            spot=SPOT, iv=IV, r=R, cfg=cfg,
        )  # fmt: skip
        # same paths: a lower target is reached no later on every path
        assert lo.managed.p_take_profit >= hi.managed.p_take_profit
        assert lo.managed.expected_days_held <= hi.managed.expected_days_held

    @settings(max_examples=10, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(
        which=st.sampled_from(["condor", "bull_put", "bull_call", "long_call"]),
        tp=st.floats(0.2, 1.0),
        cad=st.one_of(st.none(), st.integers(0, 20)),
        seed=st.integers(0, 1000),
    )
    def test_no_stop_loss_bounded_by_max_loss(
        self, which: str, tp: float, cad: int | None, seed: int
    ) -> None:
        s = {"condor": condor, "bull_put": bull_put, "bull_call": bull_call, "long_call": lcall}[
            which
        ]()
        pol = ExitPolicy(
            take_profit_pct_of_max_gain=tp,
            take_profit_pct_of_debit=tp,
            stop=None,
            close_at_dte=cad,
        )
        cfg = ExitModelConfig(n_paths=1500, seed=seed)
        rules = resolve_rules(s, pol)
        legs = sim_legs(s, NO_COST)
        out = simulate(legs, rules, spot=SPOT, iv=0.35, r=R, dte=s.dte, cost=NO_COST, cfg=cfg)
        pnl = (out.exit_value_mid - rules.entry_net) * 100
        assert s.max_loss is not None
        assert pnl.min() >= -float(s.max_loss) - 1e-6
        if s.max_gain is not None:
            assert pnl.max() <= float(s.max_gain) + 1e-6

    @settings(max_examples=10, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(
        x1=st.floats(0.0, 0.5),
        dx=st.floats(0.0, 0.5),
        f1=st.floats(0.0, 2.0),
        df=st.floats(0.0, 2.0),
        seed=st.integers(0, 1000),
    )
    def test_costs_lower_net_ev_monotonically(
        self, x1: float, dx: float, f1: float, df: float, seed: int
    ) -> None:
        cfg = ExitModelConfig(n_paths=1500, seed=seed)
        c1 = CostModel(slippage_frac=x1, commission_per_contract=f1)
        c2 = CostModel(slippage_frac=min(x1 + dx, 1.0), commission_per_contract=f1 + df)
        a = model_exits(condor(), CREDIT_POLICY, spot=SPOT, iv=IV, r=R, cost=c1, cfg=cfg)
        b = model_exits(condor(), CREDIT_POLICY, spot=SPOT, iv=IV, r=R, cost=c2, cfg=cfg)
        assert b.managed.net_ev <= a.managed.net_ev + 0.01
        assert b.static.net_ev <= a.static.net_ev + 0.01
        assert a.managed.net_ev <= a.managed.gross_ev + 0.01
        # costs never change which rule fires (rules are on the mid)
        assert a.managed.p_take_profit == b.managed.p_take_profit

    def test_debit_structure_model(self) -> None:
        r = model_exits(lcall(), DEBIT_POLICY, spot=SPOT, iv=IV, r=R, cfg=FAST)
        assert r.entry_net == pytest.approx(2.60)
        assert r.take_profit is not None and r.take_profit.close_side == "credit"
        assert r.take_profit.close_price == pytest.approx(5.20)
        assert r.stop is not None and r.stop.close_price == pytest.approx(1.30)
        assert r.managed.p_stop > 0

    def test_iv_mean_reversion_hook(self) -> None:
        m = IvModel(kind="mean_reverting", long_run=0.15, half_life_days=10)
        path = iv_path(0.30, 20, m)
        assert path[0] == pytest.approx(0.30)
        assert path[10] == pytest.approx(0.225)
        assert (np.diff(path) < 0).all()
        assert (iv_path(0.3, 5, IvModel()) == 0.3).all()
        cfg = FAST.model_copy(update={"iv_model": m})
        r = model_exits(condor(), CREDIT_POLICY, spot=SPOT, iv=0.30, r=R, cfg=cfg)
        assert r.iv_model.kind == "mean_reverting"

    def test_quoted_spreads_used(self) -> None:
        wide = {leg.occ_symbol: 0.50 for leg in condor().legs}
        a = model_exits(condor(), CREDIT_POLICY, spot=SPOT, iv=IV, r=R, cfg=FAST)
        b = model_exits(condor(), CREDIT_POLICY, spot=SPOT, iv=IV, r=R, cfg=FAST, spreads=wide)
        assert b.entry_costs > a.entry_costs
        assert b.managed.net_ev < a.managed.net_ev

    def test_rejects_bad_inputs(self) -> None:
        s = condor().model_copy(update={"dte": 0})
        with pytest.raises(ValueError, match="dte"):
            model_exits(s, CREDIT_POLICY, spot=SPOT, iv=IV, r=R, cfg=FAST)
        with pytest.raises(ValueError, match="> 0"):
            model_exits(condor(), CREDIT_POLICY, spot=SPOT, iv=0.0, r=R, cfg=FAST)

    def test_exit_summary(self) -> None:
        r = model_exits(condor(), CREDIT_POLICY, spot=SPOT, iv=IV, r=R, cfg=FAST)
        s = ExitSummary.from_result(r)
        assert s.managed_net_ev == r.managed.net_ev
        assert s.static_pop == pytest.approx(r.static.pop, abs=1e-4)
        assert s.take_profit_close == r.take_profit.close_price  # type: ignore[union-attr]
        assert "50% of max gain" in s.policy

    def test_realized_vol_paths(self) -> None:
        cond = condor()
        at_iv = model_exits(cond, CREDIT_POLICY, spot=SPOT, iv=IV, r=R, cfg=FAST)
        assert at_iv.path_vol == IV and at_iv.path_vol_source == "iv" and at_iv.vrp is None
        rich = model_exits(cond, CREDIT_POLICY, spot=SPOT, iv=IV, r=R, cfg=FAST, realized_vol=0.12)
        assert rich.path_vol == 0.12 and rich.path_vol_source == "realized_forecast"
        assert rich.iv_used == IV
        assert rich.vrp == pytest.approx(0.08)
        # IV above realised vol: selling premium earns the VRP
        assert rich.managed.net_ev > at_iv.managed.net_ev
        assert rich.static.net_ev > at_iv.static.net_ev
        assert rich.static.pop_analytic > at_iv.static.pop_analytic
        # config "iv" ignores the forecast for the paths but still reports the VRP
        iv_cfg = FAST.model_copy(update={"path_vol": "iv"})
        forced = model_exits(
            cond, CREDIT_POLICY, spot=SPOT, iv=IV, r=R, cfg=iv_cfg, realized_vol=0.12
        )
        assert forced.path_vol == IV and forced.vrp == pytest.approx(0.08)
        assert forced.managed == at_iv.managed
        with pytest.raises(ValueError, match="path_vol"):
            simulate(
                sim_legs(cond, NO_COST), resolve_rules(cond, CREDIT_POLICY), spot=SPOT, iv=IV,
                r=R, dte=5, cost=NO_COST, cfg=FAST, path_vol=0.0,
            )  # fmt: skip

    def test_ranking_inputs(self) -> None:
        r = model_exits(condor(), CREDIT_POLICY, spot=SPOT, iv=IV, r=R, cfg=FAST, realized_vol=0.15)
        assert r.rorc_day == pytest.approx(
            r.managed.net_ev / (400.0 * r.managed.expected_days_held), abs=1e-6
        )
        s = ExitSummary.from_result(r)
        assert s.rorc_day == r.rorc_day and s.vrp == r.vrp and s.path_vol == 0.15
        # unbounded max loss → no rorc
        from arc.exits.model import _rorc_day

        assert _rorc_day(5.0, lcall().model_copy(update={"max_loss": None}), 10) is None
        assert _rorc_day(5.0, lcall(), 0) is None

    def test_realized_vol_forecast(self) -> None:
        assert realized_vol_forecast(0.10, 0.20) == pytest.approx(0.15)
        assert realized_vol_forecast(None, 0.20) == 0.20
        assert realized_vol_forecast(0.10, None) == 0.10
        assert realized_vol_forecast(None, None) is None
        assert realized_vol_forecast(float("nan"), 0.0) is None

    @pytest.mark.serial  # wall-clock budget: runs alone, after the parallel pass (Makefile)
    def test_performance_20k_paths(self) -> None:
        cfg = load_exit_config().model
        assert cfg.n_paths == 20_000
        s = condor()
        model_exits(s, CREDIT_POLICY, spot=SPOT, iv=IV, r=R, cfg=cfg)  # warm up
        t0 = time.perf_counter()
        model_exits(s, CREDIT_POLICY, spot=SPOT, iv=IV, r=R, cfg=cfg)
        # Card: < 1 s on the dev Mac (≈0.06 s measured); generous bound for CI runners.
        assert time.perf_counter() - t0 < 3.0


# ---------------------------------------------------------------------------
# evaluate_position
# ---------------------------------------------------------------------------


def _marks(s: Structure, values: dict[str, float], **kw: object) -> PositionMarks:
    return PositionMarks(as_of=kw.pop("as_of", AS_OF), leg_mids=values, **kw)  # type: ignore[arg-type]


def _vertical_marks(short: float, long: float) -> dict[str, float]:
    s = bull_put()
    return {
        next(leg.occ_symbol for leg in s.legs if leg.side.value == "short"): short,
        next(leg.occ_symbol for leg in s.legs if leg.side.value == "long"): long,
    }


class TestEvaluatePosition:
    def test_hold(self) -> None:
        # credit 1.00; now 0.80 to close → +0.20 (20%) with 33 DTE: keep holding
        st_ = evaluate_position(
            OpenPosition(structure=bull_put()), _marks(bull_put(), _vertical_marks(1.3, 0.5)),
            CREDIT_POLICY,
        )  # fmt: skip
        assert st_.fired is None
        assert st_.dte == 33
        assert st_.pnl_per_share == pytest.approx(0.20)
        assert st_.pnl == pytest.approx(20.0)
        assert st_.pct_of_max_gain == pytest.approx(0.20)
        assert st_.close_side == "debit"
        assert st_.close_price == pytest.approx(0.80)
        assert st_.take_profit_pnl == pytest.approx(0.50)
        assert st_.stop_pnl == pytest.approx(-1.0)  # 2x credit: buy back at $2.00
        assert st_.close_now_net < 0  # closing costs slippage + commissions
        assert st_.remaining_net_ev is None  # no spot / IV given

    def test_take_profit_fires(self) -> None:
        st_ = evaluate_position(
            OpenPosition(structure=bull_put(), contracts=3),
            _marks(bull_put(), _vertical_marks(0.6, 0.1)),
            CREDIT_POLICY,
        )
        assert st_.fired is ExitReason.TAKE_PROFIT
        assert st_.pnl_total == pytest.approx(3 * 50.0)

    def test_stop_fires(self) -> None:
        st_ = evaluate_position(
            OpenPosition(structure=bull_put()),
            _marks(bull_put(), _vertical_marks(4.2, 1.1)),  # 3.10 to close: −2.10
            CREDIT_POLICY,
        )
        assert st_.fired is ExitReason.STOP
        intraday = evaluate_position(
            OpenPosition(structure=bull_put()),
            _marks(bull_put(), _vertical_marks(4.2, 1.1), end_of_day=False),
            CREDIT_POLICY,
        )
        assert intraday.fired is None  # EOD-only stop (D23)

    def test_dte_exit_fires(self) -> None:
        st_ = evaluate_position(
            OpenPosition(structure=bull_put()),
            _marks(bull_put(), _vertical_marks(1.1, 0.2), as_of=EXP - dt.timedelta(days=7)),
            CREDIT_POLICY,
        )
        assert st_.dte == 7
        assert st_.fired is ExitReason.DTE_EXIT

    def test_time_adjusted_target_fires(self) -> None:
        pol = CREDIT_POLICY.model_copy(
            update={
                "close_at_dte": None,
                "time_adjusted_targets": [TimeAdjustedTarget(dte_lte=14, take_profit_pct=0.3)],
            }
        )
        marks = _vertical_marks(1.0, 0.35)  # 0.65 to close: +0.35 = 35% of max gain
        early = evaluate_position(
            OpenPosition(structure=bull_put()), _marks(bull_put(), marks), pol
        )
        assert early.fired is None and early.take_profit_pct == 0.5
        late = evaluate_position(
            OpenPosition(structure=bull_put()),
            _marks(bull_put(), marks, as_of=EXP - dt.timedelta(days=14)),
            pol,
        )
        assert late.take_profit_pct == 0.3
        assert late.fired is ExitReason.TAKE_PROFIT

    def test_expired(self) -> None:
        st_ = evaluate_position(
            OpenPosition(structure=bull_put()),
            _marks(bull_put(), _vertical_marks(0.0, 0.0), as_of=EXP),
            CREDIT_POLICY,
        )
        assert st_.fired is ExitReason.EXPIRY

    def test_actual_fill_used(self) -> None:
        # filled at 1.10 credit instead of the 1.00 mid: same marks, +0.10 more P&L
        mid = evaluate_position(
            OpenPosition(structure=bull_put()), _marks(bull_put(), _vertical_marks(1.3, 0.5)),
            CREDIT_POLICY,
        )  # fmt: skip
        fill = evaluate_position(
            OpenPosition(structure=bull_put(), entry_net=-1.10),
            _marks(bull_put(), _vertical_marks(1.3, 0.5)),
            CREDIT_POLICY,
        )
        assert fill.pnl_per_share == pytest.approx(mid.pnl_per_share + 0.10)

    def test_debit_position(self) -> None:
        s = lcall()
        sym = s.legs[0].occ_symbol
        assert evaluate_position(
            OpenPosition(structure=s), _marks(s, {sym: 5.4}), DEBIT_POLICY
        ).fired is ExitReason.TAKE_PROFIT  # fmt: skip
        assert evaluate_position(
            OpenPosition(structure=s), _marks(s, {sym: 1.2}), DEBIT_POLICY
        ).fired is ExitReason.STOP  # fmt: skip

    def test_remaining_ev(self) -> None:
        marks = _marks(bull_put(), _vertical_marks(1.3, 0.5), spot=SPOT, iv=IV, r=R)
        a = evaluate_position(OpenPosition(structure=bull_put()), marks, CREDIT_POLICY, cfg=FAST)
        b = evaluate_position(OpenPosition(structure=bull_put()), marks, CREDIT_POLICY, cfg=FAST)
        assert a == b
        assert a.remaining_net_ev is not None and a.remaining_gross_ev is not None
        assert a.remaining_days_held is not None and 0 < a.remaining_days_held <= 33
        assert a.n_paths == FAST.n_paths
        # holding avoids today's exit costs but pays them later (or at expiry): the
        # net-vs-gross gap is bounded by one round of exit costs
        assert abs(a.remaining_net_ev - a.remaining_gross_ev) < 10.0

    def test_remaining_ev_realized_vol(self) -> None:
        base = _marks(bull_put(), _vertical_marks(1.3, 0.5), spot=SPOT, iv=IV, r=R)
        calm = _marks(
            bull_put(), _vertical_marks(1.3, 0.5), spot=SPOT, iv=IV, r=R, realized_vol=0.10
        )
        a = evaluate_position(OpenPosition(structure=bull_put()), base, CREDIT_POLICY, cfg=FAST)
        b = evaluate_position(OpenPosition(structure=bull_put()), calm, CREDIT_POLICY, cfg=FAST)
        assert b.remaining_net_ev is not None and a.remaining_net_ev is not None
        assert b.remaining_net_ev > a.remaining_net_ev  # short premium, IV > realised

    def test_missing_mark(self) -> None:
        with pytest.raises(LookupError, match="no mark"):
            evaluate_position(
                OpenPosition(structure=bull_put()),
                _marks(bull_put(), {bull_put().legs[0].occ_symbol: 1.0}),
                CREDIT_POLICY,
            )

    def test_marks_validation(self) -> None:
        with pytest.raises(ValidationError):
            PositionMarks(as_of=AS_OF, leg_mids={"X": -1.0})


# ---------------------------------------------------------------------------
# CLI and pipeline wiring
# ---------------------------------------------------------------------------


def test_cli_exits_model_fixture(capsys: pytest.CaptureFixture[str]) -> None:
    from arc.cli import main

    assert main(["exits", "model", "--fixture", "spy", "--paths", "2000"]) == 0
    out = capsys.readouterr().out
    assert "SPY iron_condor" in out
    assert "static (hold to exp.)" in out
    assert "managed (policy)" in out
    assert "take profit" in out and "debit" in out
    # D23: the relaxed default next to a 2x-credit stop and no stop
    assert "stop pct_max_loss 0.75 (end of day)" in out
    assert "managed (2x credit stop)" in out
    assert "managed (no stop)" in out
    assert "(realized_forecast)" in out and "VRP" in out and "rorc/day" in out

    assert main(["exits", "model", "--fixture", "spy", "--paths", "500", "--path-vol", "iv",
                 "--no-compare"]) == 0  # fmt: skip
    out = capsys.readouterr().out
    assert "(iv)" in out and "2x credit stop" not in out

    assert main(["exits", "model", "--fixture", "spy", "--paths", "500", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["model"] == "gbm_flat_iv"
    assert data["n_paths"] == 500
    assert set(data["managed"]) >= {"pop", "net_ev", "p_take_profit", "expected_days_held"}


def test_cli_exits_bad_config(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from arc.cli import main

    bad = tmp_path / "x.yaml"
    bad.write_text("default: {bogus: 1}\n")
    assert main(["exits", "model", "--fixture", "spy", "--config", str(bad)]) == 2
    assert "arc exits" in capsys.readouterr().err


def test_cli_exits_debit_structure_and_errors(capsys: pytest.CaptureFixture[str]) -> None:
    from arc.cli import main

    assert main(["exits", "model", "--fixture", "spy", "--rank", "99", "--paths", "500"]) == 1
    assert "no iron_condor candidate #99" in capsys.readouterr().err
    assert main(["exits", "model", "QQQ", "--fixture", "spy"]) == 2
    assert main(["exits", "model", "--fixture", "spy", "--strategy", "bull_put",
                 "--paths", "500"]) == 0  # fmt: skip
    assert "managed (no stop)" in capsys.readouterr().out
