"""E28.3 / D131: portfolio open-risk cap wiring outside the pure gate.

Sizing's headroom fit (``arc.sizing.size_contracts``), the registry entry and reason
codes, the prompt lines (free slots, cap phrase, Risk rules), the Tower field and the
backtest walk. The gate rule itself is tested in ``tests/test_gate_open_risk.py``.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal as D

import pytest

from arc.config import ArcSettings
from arc.control.registry import REGISTRY, lookup
from arc.gate import Portfolio, Position, RuleCode
from arc.gate.caps import alloc_tier
from arc.journal.reasons import REASON_LABELS, ReasonCode, gate_reason
from arc.pipeline.free_slots import free_slots
from arc.pipeline.portfolio_context import _alloc_thresholds, cap_phrase
from arc.pipeline.steps import _risk_cap, _risk_rules, portfolio_open_risk
from arc.sizing import size_contracts
from arc.tower.data_overview import _greeks_section

EQ = D("100000")


@pytest.fixture(autouse=True)
def _shipped_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """The suite pins the 100% rollback (tests/pre_d85_env.py); test the shipped 40%."""
    monkeypatch.delenv("ARC_MAX_OPEN_RISK_PCT", raising=False)


def cfg(**kw: object) -> ArcSettings:
    return ArcSettings(_env_file=None, **kw)  # type: ignore[call-arg]


def size(headroom: D | None, *, suggestion: int = 10, per: str = "500") -> object:
    return size_contracts(
        suggestion=suggestion,
        max_loss_per_contract=D(per),
        equity=EQ,
        cap_pct=0.15,  # $15,000 budget → 30 contracts
        open_risk_headroom=headroom,
    )


class TestSizing:
    def test_no_headroom_argument_is_unchanged(self) -> None:
        r = size(None)
        assert r.contracts == 10 and r.code == "ok" and r.open_risk_contracts is None

    def test_headroom_wider_than_the_order_changes_nothing(self) -> None:
        r = size(D("10000"))
        assert r.contracts == 10 and r.code == "ok" and r.open_risk_contracts == 20

    def test_headroom_cuts_the_size(self) -> None:
        r = size(D("1999"))  # floor(1999 / 500) = 3
        assert r.contracts == 3 and r.code == "open_risk_capped"
        assert r.open_risk_contracts == 3 and r.max_loss_total == D("1500")

    def test_no_room_for_one_contract(self) -> None:
        r = size(D("499.99"))
        assert r.contracts == 0 and r.code == "open_risk_cap"
        assert "open-risk cap leaves $499.99" in r.reason

    def test_negative_headroom_is_zero(self) -> None:
        assert size(D("-5")).code == "open_risk_cap"

    def test_cash_fit_still_applies_after_the_open_risk_fit(self) -> None:
        r = size_contracts(
            suggestion=10,
            max_loss_per_contract=D("500"),
            equity=EQ,
            cap_pct=0.15,
            cash_available=D("1000"),
            cash_per_contract=D("500"),
            open_risk_headroom=D("1500"),
        )
        assert r.contracts == 2 and r.code == "cash_capped" and r.open_risk_contracts == 3

    def test_sizing_codes_have_reasons(self) -> None:
        from arc.pipeline.steps import _SIZING_REASONS

        assert _SIZING_REASONS["open_risk_capped"] is ReasonCode.SIZING_OPEN_RISK_CAPPED
        assert _SIZING_REASONS["open_risk_cap"] is ReasonCode.SIZING_OPEN_RISK_CAP


class TestSharedNumber:
    def test_sizing_headroom_is_the_gate_number(self) -> None:
        pf = Portfolio(
            positions=[Position(underlying="QQQ", max_loss=D("30000"))],
            reserved_max_loss=D("2000"),
        )
        r = portfolio_open_risk(pf, EQ, cfg())
        assert r.deployed == D("32000") and r.headroom == D("8000.0")
        tier = alloc_tier(D("32000"), EQ, cfg())
        assert tier.deployed == r.deployed  # one deployed sum for tier and cap

    def test_risk_cap_takes_the_tighter_budget(self) -> None:
        assert _risk_cap(D("15000"), None, D("500")) == 30
        assert _risk_cap(D("15000"), D("1200"), D("500")) == 2
        assert _risk_cap(D("1000"), D("9000"), D("500")) == 2


class TestRegistryAndReasons:
    def test_registered_riskier_up_to_the_ceiling(self) -> None:
        assert "max_open_risk_pct" in REGISTRY
        s = lookup("max_open_risk_pct")
        assert s.ref == "D131" and s.hard_ceiling == 1.0

    def test_reason_labels(self) -> None:
        for code in (
            ReasonCode.GATE_OPEN_RISK_CAP,
            ReasonCode.SIZING_OPEN_RISK_CAP,
            ReasonCode.SIZING_OPEN_RISK_CAPPED,
        ):
            assert code in REASON_LABELS
        v = f"{RuleCode.OPEN_RISK_CAP}: open risk 40,001 > 40,000"
        assert gate_reason(v) is ReasonCode.GATE_OPEN_RISK_CAP


class TestPrompts:
    def test_free_slots_line(self) -> None:
        fs = free_slots(
            (),
            [],
            max_open_positions=8,
            single=True,
            per_name_cap=D("10000"),
            open_risk_pct=D("0.12"),
            max_open_risk_pct=D("0.4"),
        )
        assert fs.line() == "Free slots: 0/0 \u00b7 per-name cap $10,000 \u00b7 open risk 12% / 40%"
        n_a = free_slots(
            (),
            [],
            max_open_positions=8,
            single=True,
            per_name_cap=D("10000"),
            max_open_risk_pct=D("0.4"),
        )
        assert n_a.line().endswith("\u00b7 open risk n/a / 40%")

    def test_cap_phrase_and_thresholds(self) -> None:
        tier = alloc_tier(D("7000"), EQ, cfg())
        th = _alloc_thresholds(tier, cfg())
        assert th["max_open_risk_pct"] == pytest.approx(0.40)
        assert th["open_risk_pct"] == pytest.approx(0.07)
        assert (
            _alloc_thresholds(tier, cfg(max_open_risk_pct=1.0))
            .keys()
            .isdisjoint({"max_open_risk_pct", "open_risk_pct"})
        )
        risk = portfolio_open_risk(
            Portfolio(positions=[Position(underlying="QQQ", max_loss=D("7000"))]), EQ, cfg()
        )
        assert cap_phrase(tier, risk).endswith(", open risk 7% / 40%")
        assert ", open risk" not in cap_phrase(tier)

    def test_risk_rules_name_the_headroom(self) -> None:
        settings = cfg()
        tier = alloc_tier(D("30000"), EQ, settings)
        risk = portfolio_open_risk(
            Portfolio(positions=[Position(underlying="QQQ", max_loss=D("30000"))]), EQ, settings
        )
        line = _risk_rules(settings, {}, tier, risk)[-1]
        assert "floor(open-risk headroom $10,000 / max_loss) (open risk 30% / 40%)" in line
        off = cfg(max_open_risk_pct=1.0)
        off_risk = portfolio_open_risk(
            Portfolio(positions=[Position(underlying="QQQ", max_loss=D("30000"))]), EQ, off
        )
        assert "open-risk" not in _risk_rules(off, {}, tier, off_risk)[-1]
        assert _risk_rules(off, {}, tier, off_risk) == _risk_rules(off, {}, tier)


class TestTower:
    def _section(self, cap: float | None) -> object:
        return _greeks_section(
            None,
            [],
            dollar_delta_cap_pct=0.5,
            vega_cap_pct=0.02,
            max_alloc_pct=0.15,
            stale_after=dt.timedelta(minutes=10),
            max_open_risk_pct=cap,
        )

    def test_cap_shown_below_100_percent(self) -> None:
        assert self._section(0.40).max_open_risk_pct == pytest.approx(0.40)  # type: ignore[attr-defined]

    @pytest.mark.parametrize("cap", [None, 1.0])
    def test_cap_hidden_when_off(self, cap: float | None) -> None:
        assert self._section(cap).max_open_risk_pct is None  # type: ignore[attr-defined]


def test_backtest_walk_names_the_rule() -> None:
    from arc.backtest import sim

    assert "gate_open_risk_cap" in sim.SKIP_REASONS
    assert any(r.startswith("check_open_risk") for r in sim.GATE_RULES_APPLIED)
