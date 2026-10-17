"""E22.1 / D97: named entry-DTE sleeves on an account profile.

Model validation, sleeve lookup, the gate's ``dte_window`` + ``sleeve_max_positions``
rules, entry-DTE derivation and the single-window back-compat. Runs under
``make test-gate`` (100% branch coverage of ``arc.gate``).
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal as D
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from arc.account_profiles import (
    DEFAULT_SLEEVE,
    AccountProfile,
    DteSleeve,
    entry_dte_of,
    sleeve_for_dte,
    sleeve_of,
)
from arc.config import ArcSettings
from arc.gate import AccountSnapshot, MarketSnapshot, Portfolio, Quote, RuleCode, evaluate
from arc.gate import rules as R
from arc.gate.inputs import Position
from arc.models import Proposal, QuantMetrics, Sizing, Structure
from arc.structures import long_call
from arc.utils.calendar import ET

AS_OF = dt.date(2026, 10, 9)
NOW = dt.datetime(2026, 10, 9, 10, 0, tzinfo=ET)
D97_SLEEVES = {
    "short": {"dte_min": 10, "dte_max": 29, "max_positions": 5, "exit_policy": "short"},
    "swing": {"dte_min": 30, "dte_max": 60, "max_positions": 5},
}


def sleeved_file(tmp_path: Path, sleeves: dict[str, Any] | None = None) -> Path:
    """The shipped profiles file with cash_debit switched to sleeves (D97 by default)."""
    data = yaml.safe_load(Path("config/account_profiles.yaml").read_text())
    cd = data["profiles"]["cash_debit"]
    cd.pop("dte_min", None)
    cd.pop("dte_max", None)
    cd["sleeves"] = D97_SLEEVES if sleeves is None else sleeves
    f = tmp_path / "account_profiles.yaml"
    f.write_text(yaml.safe_dump(data))
    return f


def single_window_file(tmp_path: Path) -> Path:
    """The shipped profiles file with the cash profiles back on one 30-60 window.

    The pre-D97 shape (E22.4 switched the shipped cash profiles to sleeves): pins the
    single-window back-compat path, which margin and any sleeve-less profile still use.
    """
    data = yaml.safe_load(Path("config/account_profiles.yaml").read_text())
    for name in ("cash_debit", "cash_long_only"):
        prof = data["profiles"][name]
        prof.pop("sleeves", None)
        prof["dte_min"], prof["dte_max"] = 30, 60
    f = tmp_path / "account_profiles_single.yaml"
    f.write_text(yaml.safe_dump(data))
    return f


def cfg(profiles_file: Path | None = None, profile: str = "cash_debit") -> ArcSettings:
    kw: dict[str, Any] = {"account_profile": profile}
    if profiles_file is not None:
        kw["account_profiles_file"] = profiles_file
    return ArcSettings(_env_file=None, **kw)  # type: ignore[call-arg]


def call_at(dte: int, strike: int = 570) -> Structure:
    return long_call("SPY", AS_OF + dt.timedelta(days=dte), strike, "6.00", as_of=AS_OF)


def gate(s: Structure, config: ArcSettings, portfolio: Portfolio | None = None) -> Any:
    quotes = {
        leg.occ_symbol: Quote(
            bid=leg.premium - D("0.05"),  # type: ignore[operator]
            ask=leg.premium + D("0.05"),  # type: ignore[operator]
            as_of=NOW - dt.timedelta(seconds=5),
        )
        for leg in s.legs
    }
    p = Proposal(
        candidate_id="c1",
        structure=s,
        thesis="t",
        quant=QuantMetrics(pop=0.5, ev=D("1"), cost_bps=1.0),
        sizing=Sizing(contracts=1, notional=D("600"), pct_equity=0.006),
        expires_at=NOW + dt.timedelta(minutes=10),
    )
    account = AccountSnapshot(
        equity=D("100000"),
        last_equity=D("100000"),
        settled_cash=D("100000"),
        as_of=NOW - dt.timedelta(seconds=5),
    )
    return evaluate(
        p,
        account,
        portfolio or Portfolio(),
        config,
        market=MarketSnapshot(
            quotes=quotes, next_earnings={"SPY": None}, underlying_spot={"SPY": D("580")}
        ),
        now=NOW,
    )


def codes(decision: Any) -> set[str]:
    return {v.split(":", 1)[0] for v in decision.violations}


def held(*entry_dtes: int | None, root: str = "QQQ") -> Portfolio:
    return Portfolio(
        positions=[Position(underlying=root, max_loss=D("100"), entry_dte=e) for e in entry_dtes]
    )


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


def _profile(**kw: Any) -> AccountProfile:
    base: dict[str, Any] = {
        "name": "p",
        "allowed_kinds": ["long_call"],
        "require_net_debit": True,
        "buying_power": "cash_settled",
        "allow_short_legs": "none",
        "stance_strategies": {"bullish": ["long_call"]},
    }
    base.update(kw)
    return AccountProfile.model_validate(base)


class TestModel:
    def test_mapping_and_list_forms(self) -> None:
        a = _profile(sleeves=D97_SLEEVES)
        b = _profile(sleeves=[{"name": k, **v} for k, v in D97_SLEEVES.items()])
        assert a == b
        assert [s.name for s in a.entry_sleeves()] == ["short", "swing"]
        assert a.has_sleeves

    def test_sleeves_ordered_by_window(self) -> None:
        p = _profile(sleeves={"swing": D97_SLEEVES["swing"], "short": D97_SLEEVES["short"]})
        assert [s.name for s in p.entry_sleeves()] == ["short", "swing"]

    @pytest.mark.parametrize(
        ("sleeves", "match"),
        [
            ({"a": {"dte_min": 10, "dte_max": 30}, "b": {"dte_min": 30, "dte_max": 60}}, "overlap"),
            ({"a": {"dte_min": 10, "dte_max": 40}, "b": {"dte_min": 20, "dte_max": 30}}, "overlap"),
            ({}, "at least one"),
            ({"a": {"dte_min": 20, "dte_max": 10}}, "dte_max 10 < dte_min 20"),
            ({"A": {"dte_min": 10, "dte_max": 20}}, "pattern"),
            ({"a": {"dte_min": 10, "dte_max": 20, "max_positions": -1}}, "greater than or equal"),
            ({"a": {"dte_min": 10, "dte_max": 20, "bogus": 1}}, "Extra inputs"),
        ],
    )
    def test_invalid_sleeves(self, sleeves: Any, match: str) -> None:
        with pytest.raises(ValidationError, match=match):
            _profile(sleeves=sleeves)

    def test_duplicate_names(self) -> None:
        with pytest.raises(ValidationError, match="duplicate sleeve name"):
            _profile(
                sleeves=[
                    {"name": "a", "dte_min": 10, "dte_max": 20},
                    {"name": "a", "dte_min": 30, "dte_max": 40},
                ]
            )

    def test_sleeves_exclude_single_window(self) -> None:
        with pytest.raises(ValidationError, match="dte_min/dte_max must be unset"):
            _profile(dte_min=30, dte_max=60, sleeves=D97_SLEEVES)

    def test_single_window_is_implicit_default_sleeve(self) -> None:
        p = _profile(dte_min=30, dte_max=60)
        assert not p.has_sleeves
        assert p.entry_sleeves() == [DteSleeve(name=DEFAULT_SLEEVE, dte_min=30, dte_max=60)]
        assert p.sleeve_for_dte(45) is not None and p.sleeve_for_dte(61) is None

    def test_no_window_uses_fallback(self) -> None:
        p = _profile()
        assert p.entry_sleeves() == []
        assert p.entry_sleeves((0, 45)) == [DteSleeve(name="default", dte_min=1, dte_max=45)]
        assert p.entry_sleeves((0, 0)) == [DteSleeve(name="default", dte_min=1, dte_max=1)]

    @pytest.mark.parametrize(
        ("dte", "name"),
        [(9, None), (10, "short"), (29, "short"), (30, "swing"), (60, "swing"), (61, None)],
    )
    def test_sleeve_for_dte_boundaries(self, dte: int, name: str | None) -> None:
        p = _profile(sleeves=D97_SLEEVES)
        got = p.sleeve_for_dte(dte)
        assert (got.name if got else None) == name
        assert sleeve_for_dte(p.entry_sleeves(), None) is None

    def test_label(self) -> None:
        assert DteSleeve(name="short", dte_min=10, dte_max=29).label == "short[10-29]"


class TestEntryDte:
    def test_stored_dte_wins(self) -> None:
        st = call_at(20)
        assert st.dte == 20
        assert entry_dte_of(st, "2026-09-01T14:00:00+00:00") == 20

    def test_fallback_from_opened_at(self) -> None:
        st = call_at(20).model_copy(update={"dte": 0})
        assert entry_dte_of(st, None) == 0
        assert entry_dte_of(st, dt.date(2026, 10, 1)) == 28
        # 2026-10-02 01:00Z is 2026-10-01 21:00 ET
        assert entry_dte_of(st, "2026-10-02T01:00:00+00:00") == 28
        assert entry_dte_of(st, dt.datetime(2026, 10, 2, 1, 0)) == 28  # naive = UTC
        assert entry_dte_of(st, dt.datetime(2026, 10, 2, 1, 0, tzinfo=dt.UTC)) == 28

    def test_sleeve_of(self, tmp_path: Path) -> None:
        s = cfg(sleeved_file(tmp_path))
        assert sleeve_of(s.entry_sleeves, call_at(20)).name == "short"  # type: ignore[union-attr]
        assert sleeve_of(s.entry_sleeves, call_at(45)).name == "swing"  # type: ignore[union-attr]
        assert sleeve_of(s.entry_sleeves, call_at(5)) is None
        assert s.exit_block_of(call_at(20)) == "short"
        assert s.exit_block_of(call_at(45)) is None  # swing: kind policy
        assert s.exit_block_of(call_at(80)) is None  # outside every sleeve
        assert s.exit_block_for_dte(15) == "short" and s.exit_block_for_dte(None) is None


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


class TestSettings:
    def test_envelope_and_sleeves(self, tmp_path: Path) -> None:
        s = cfg(sleeved_file(tmp_path))
        assert s.entry_dte_window == (10, 60)
        assert [(x.name, x.dte_min, x.dte_max) for x in s.entry_sleeves] == [
            ("short", 10, 29),
            ("swing", 30, 60),
        ]

    def test_shipped_cash_debit_is_d97_sleeves(self) -> None:
        """E22.4 (D97): the shipped cash_debit profile carries the short + swing sleeves
        (D118: short 10-35 / swing 36-60, was 10-29 / 30-60)."""
        s = cfg()
        assert s.profile.has_sleeves
        assert s.entry_dte_window == (10, 60)
        assert [(x.name, x.dte_min, x.dte_max, x.max_positions) for x in s.entry_sleeves] == [
            ("short", 10, 35, 5),
            ("swing", 36, 60, 5),
        ]
        assert sum(x.max_positions or 0 for x in s.entry_sleeves) == s.max_open_positions
        assert s.exit_block_for_dte(20) == "short"
        assert s.exit_block_for_dte(35) == "short"  # D118: DTE 30-35 moved to short
        assert s.exit_block_for_dte(36) is None  # DTE 36 belongs to swing
        assert s.exit_block_for_dte(45) is None

    def test_single_window_cash_debit_is_one_default_sleeve(self, tmp_path: Path) -> None:
        s = cfg(single_window_file(tmp_path))
        assert not s.profile.has_sleeves
        assert s.entry_dte_window == (30, 60)
        assert [(x.name, x.dte_min, x.dte_max, x.max_positions) for x in s.entry_sleeves] == [
            ("default", 30, 60, None)
        ]
        assert s.exit_block_for_dte(45) is None

    def test_sleeveless_margin_uses_global_window(self) -> None:
        from tests.legacy_profiles import legacy_margin_settings

        s = legacy_margin_settings()  # D128: shipped margin has sleeves
        assert s.entry_dte_window == (s.dte_min, s.dte_max)
        assert s.entry_sleeves[0].name == "default"


# ---------------------------------------------------------------------------
# Gate
# ---------------------------------------------------------------------------


class TestGateDteWindow:
    @pytest.mark.parametrize(
        ("dte", "inside"), [(9, False), (10, True), (29, True), (30, True), (60, True), (61, False)]
    )
    def test_sleeved_window(self, tmp_path: Path, dte: int, inside: bool) -> None:
        d = gate(call_at(dte), cfg(sleeved_file(tmp_path)))
        assert (RuleCode.DTE_WINDOW not in codes(d)) is inside

    def test_gap_between_sleeves_is_outside(self, tmp_path: Path) -> None:
        f = sleeved_file(
            tmp_path,
            {"short": {"dte_min": 10, "dte_max": 20}, "swing": {"dte_min": 30, "dte_max": 60}},
        )
        d = gate(call_at(25), cfg(f))
        assert any("DTE 25 outside sleeves short[10-20], swing[30-60]" in v for v in d.violations)

    def test_message_names_sleeves(self, tmp_path: Path) -> None:
        d = gate(call_at(61), cfg(sleeved_file(tmp_path)))
        assert any("outside sleeves short[10-29], swing[30-60]" in v for v in d.violations)

    def test_zero_dte_never_allowed(self, tmp_path: Path) -> None:
        f = sleeved_file(tmp_path, {"a": {"dte_min": 1, "dte_max": 20}})
        v = R.check_dte_window(_derived(call_at(0)), cfg(f), NOW)
        assert v and "DTE 0 outside sleeves a[1-20]" in v[0].detail

    def test_single_window_message_unchanged(self, tmp_path: Path) -> None:
        d = gate(call_at(61), cfg(single_window_file(tmp_path)))
        assert any("DTE 61 outside [30, 60]" in v for v in d.violations)


def _derived(s: Structure) -> R.Derived:
    """The gate's own derivation of a proposal on *s*."""
    p = Proposal(
        candidate_id="c1",
        structure=s,
        thesis="t",
        quant=QuantMetrics(pop=0.5, ev=D("1"), cost_bps=1.0),
        sizing=Sizing(contracts=1, notional=D("600"), pct_equity=0.006),
        expires_at=NOW + dt.timedelta(minutes=10),
    )
    return R.derive(p)


class TestGateSleevePositions:
    def test_cap_per_sleeve(self, tmp_path: Path) -> None:
        c = cfg(sleeved_file(tmp_path))
        full_short = held(15, 15, 20, 25, 29)
        d = gate(call_at(20), c, full_short)
        assert RuleCode.SLEEVE_POSITIONS in codes(d)
        assert any("6 positions in sleeve short[10-29] > max 5" in v for v in d.violations)
        # the swing sleeve is empty: a swing proposal passes this rule
        assert RuleCode.SLEEVE_POSITIONS not in codes(gate(call_at(45), c, full_short))

    def test_under_cap_passes(self, tmp_path: Path) -> None:
        c = cfg(sleeved_file(tmp_path))
        assert RuleCode.SLEEVE_POSITIONS not in codes(gate(call_at(20), c, held(15, 20, 25, 29)))

    def test_unknown_entry_dte_counts_everywhere(self, tmp_path: Path) -> None:
        c = cfg(sleeved_file(tmp_path))
        pf = held(None, None, None, 15, 20)
        assert RuleCode.SLEEVE_POSITIONS in codes(gate(call_at(20), c, pf))
        pf2 = held(None, None, None, None, None)
        assert RuleCode.SLEEVE_POSITIONS in codes(gate(call_at(45), c, pf2))

    def test_positions_outside_every_sleeve_count_nowhere(self, tmp_path: Path) -> None:
        c = cfg(sleeved_file(tmp_path))
        assert RuleCode.SLEEVE_POSITIONS not in codes(gate(call_at(20), c, held(5, 5, 5, 70, 70)))

    def test_zero_closes_the_sleeve(self, tmp_path: Path) -> None:
        """D97 rollback: ``max_positions: 0`` blocks every new open in that sleeve."""
        f = sleeved_file(
            tmp_path,
            {"short": {"dte_min": 10, "dte_max": 29, "max_positions": 0},
             "swing": {"dte_min": 30, "dte_max": 60, "max_positions": 5}},
        )  # fmt: skip
        d = gate(call_at(20), cfg(f))
        assert any("1 positions in sleeve short[10-29] > max 0" in v for v in d.violations)
        assert RuleCode.SLEEVE_POSITIONS not in codes(gate(call_at(45), cfg(f)))

    def test_uncapped_sleeve(self, tmp_path: Path) -> None:
        f = sleeved_file(
            tmp_path, {"a": {"dte_min": 10, "dte_max": 29}, "b": {"dte_min": 30, "dte_max": 60}}
        )
        v = R.check_sleeve_positions(
            held(20, 20, 20, 20, 20, 20), _derived(call_at(20)), cfg(f), NOW
        )
        assert v == []

    def test_proposal_outside_sleeves_passes_this_rule(self, tmp_path: Path) -> None:
        c = cfg(sleeved_file(tmp_path))
        assert R.check_sleeve_positions(held(20), _derived(call_at(70)), c, NOW) == []

    def test_single_window_profile_never_fires(self, tmp_path: Path) -> None:
        many = held(*([45] * 30))
        c = cfg(single_window_file(tmp_path))
        assert R.check_sleeve_positions(many, _derived(call_at(45)), c, NOW) == []

    def test_reason_code_label(self) -> None:
        from arc.journal.reasons import REASON_LABELS, ReasonCode

        assert ReasonCode.GATE_SLEEVE_POSITIONS.value == "gate:sleeve_max_positions"
        assert ReasonCode.GATE_SLEEVE_POSITIONS in REASON_LABELS
        assert RuleCode.SLEEVE_POSITIONS.value == "sleeve_max_positions"
