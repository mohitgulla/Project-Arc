"""E22.1 / D97: sleeve-aware exit policy, scanner window, entry-window text and the
registry/control keys for profile sleeves."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from arc.config import ArcSettings
from arc.control.registry import TunableError, lookup
from arc.control.service import ControlService
from arc.exits import ExitConfig, load_exit_config
from arc.models import StructureKind as SK
from arc.personas.entry_window import entry_terms
from arc.scanner.scan import ScanParams
from arc.store.db import connect
from arc.store.migrate import migrate
from arc.utils.calendar import ET
from tests.test_gate_sleeves import D97_SLEEVES, call_at, codes, gate, sleeved_file

OWNER = "U0OWNER001"
NOW = dt.datetime(2026, 10, 9, 10, 0, tzinfo=ET)


def _exits_with_sleeves(tmp_path: Path, sleeves: dict[str, Any]) -> Path:
    data = yaml.safe_load(Path("config/exits.yaml").read_text())
    data["sleeves"] = sleeves
    f = tmp_path / "exits.yaml"
    f.write_text(yaml.safe_dump(data))
    return f


# ---------------------------------------------------------------------------
# ExitConfig.policy_for(kind, sleeve)
# ---------------------------------------------------------------------------


class TestPolicyFor:
    def test_no_sleeve_block_is_identical(self) -> None:
        cfg = load_exit_config()
        assert cfg.sleeves == {}
        for kind in [*SK, None]:
            base = cfg.policy_for(kind)
            assert cfg.policy_for(kind, None) == base
            assert cfg.policy_for(kind, "short") == base  # unknown block: same fallback
        # golden: the shipped policies dump the same with and without a sleeve name
        dump = {k.value: cfg.policy_for(k).model_dump(mode="json") for k in SK}
        dump_s = {k.value: cfg.policy_for(k, "short").model_dump(mode="json") for k in SK}
        assert dump == dump_s

    def test_fallback_order(self, tmp_path: Path) -> None:
        f = _exits_with_sleeves(tmp_path, {"short": {"kinds": {"long_call": {"close_at_dte": 3}}}})
        cfg = load_exit_config(f)
        base = load_exit_config()
        assert cfg.policy_for(SK.LONG_CALL, "short").close_at_dte == 3  # sleeve + kind
        assert cfg.policy_for(SK.LONG_PUT, "short") == base.policy_for(SK.LONG_PUT)  # kind
        assert cfg.policy_for(SK.LONG_CALL, None) == base.policy_for(SK.LONG_CALL)
        assert cfg.policy_for(None, "short") == base.default  # default
        assert cfg.policy_for(SK.LONG_CALL, "swing") == base.policy_for(SK.LONG_CALL)

    def test_sleeve_stops_must_match_direction(self) -> None:
        bad = {"kinds": {"long_call": {"stop": {"basis": "credit_multiple", "value": 2.0}}}}
        with pytest.raises(ValidationError, match=r"sleeves\.short\.long_call: credit_multiple"):
            ExitConfig.model_validate({"sleeves": {"short": bad}})
        bad2 = {"kinds": {"vertical_credit": {"stop": {"basis": "pct_debit", "value": 0.5}}}}
        with pytest.raises(ValidationError, match="pct_debit stop applies to debit"):
            ExitConfig.model_validate({"sleeves": {"x": bad2}})

    def test_sleeve_block_rejects_unknown_keys(self) -> None:
        with pytest.raises(ValidationError, match="Extra inputs"):
            ExitConfig.model_validate({"sleeves": {"short": {"kinds": {}, "bogus": 1}}})


# ---------------------------------------------------------------------------
# Scanner window and entry-window text
# ---------------------------------------------------------------------------


def cfg(profiles_file: Path | None = None) -> ArcSettings:
    kw: dict[str, Any] = {"account_profile": "cash_debit"}
    if profiles_file is not None:
        kw["account_profiles_file"] = profiles_file
    return ArcSettings(_env_file=None, **kw)  # type: ignore[call-arg]


class TestScannerAndPrompt:
    def test_single_window_params_unchanged(self) -> None:
        p = ScanParams.from_settings(cfg())
        assert (p.dte_min, p.dte_max, p.dte_windows) == (30, 60, None)

    def test_sleeved_params(self, tmp_path: Path) -> None:
        p = ScanParams.from_settings(cfg(sleeved_file(tmp_path)))
        assert (p.dte_min, p.dte_max) == (10, 60)
        assert p.dte_windows == [(10, 29), (30, 60)]

    def test_single_window_prompt_text_unchanged(self) -> None:
        t = entry_terms(cfg())
        assert t.sleeves is None and t.window == "30-60 DTE"

    def test_sleeved_prompt_text(self, tmp_path: Path) -> None:
        t = entry_terms(cfg(sleeved_file(tmp_path)))
        assert t.window == "10-29 DTE (short) / 30-60 DTE (swing)"
        # E22.2 (D97): the Quant/Risk lines name each sleeve and the one-per-ticker rule
        q = t.quant_lines()
        assert "Entry windows: short 10-29 DTE, swing 30-60 DTE" in q
        assert "At most one structure per ticker across sleeves" in q
        assert "do not reject or skip a menu item on DTE alone" in q
        r = t.risk_line()
        assert "Entry windows: short 10-29 DTE, swing 30-60 DTE" in r
        assert "Do not decline or downsize a structure for its DTE alone" in r


# ---------------------------------------------------------------------------
# Registry / control
# ---------------------------------------------------------------------------


def test_sleeve_keys_in_registry() -> None:
    t = lookup("profiles.cash_debit.sleeves.short.max_positions")
    assert t.path == ("profiles", "cash_debit", "sleeves", "short", "max_positions")
    assert (t.min, t.max) == (0, 20)
    lo = lookup("profiles.cash_debit.sleeves.short.dte_min")
    assert (lo.min, lo.max, lo.hard_ceiling) == (7, 120, 7)
    hi = lookup("profiles.cash_debit.sleeves.swing.dte_max")
    assert hi.hard_ceiling is None
    with pytest.raises(TunableError):
        lookup("profiles.cash_debit.sleeves.short.exit_policy")


@pytest.fixture
def sleeved_svc(tmp_path: Path) -> ControlService:
    c = connect(":memory:")
    migrate(c)
    base = ArcSettings(  # type: ignore[call-arg]
        _env_file=None,
        approver_slack_user_ids=[OWNER],
        account_profiles_file=sleeved_file(tmp_path),
    )
    return ControlService(
        c, base=base, now=lambda: NOW, optionable=lambda s: True, is_halted=lambda: False
    )


def test_service_lists_sleeve_keys(sleeved_svc: ControlService) -> None:
    keys = {v.tunable.key for v in sleeved_svc.show()}
    for sleeve in D97_SLEEVES:
        for attr in ("dte_min", "dte_max", "max_positions"):
            assert f"profiles.cash_debit.sleeves.{sleeve}.{attr}" in keys
    assert "profiles.cash_debit.dte_min" not in keys
    assert sleeved_svc.view("profiles.cash_debit.sleeves.short.dte_max").value == 29


def test_single_window_key_refused_on_sleeved_profile(sleeved_svc: ControlService) -> None:
    r = sleeved_svc.set("profiles.cash_debit.dte_min", "20", actor=OWNER, source="slack")
    assert r.outcome == "refused"
    assert "has DTE sleeves" in (r.message or "")
    assert "profiles.cash_debit.sleeves.short.dte_min" in (r.message or "")


def test_sleeve_cap_set_reaches_gate(sleeved_svc: ControlService) -> None:
    from tests.test_gate_sleeves import held

    s = sleeved_svc.settings()
    pf = held(15, 20)
    assert "sleeve_max_positions" not in codes(gate(call_at(20), s, pf))
    r = sleeved_svc.set(
        "profiles.cash_debit.sleeves.short.max_positions", "2", actor=OWNER, source="slack"
    )
    assert r.outcome == "applied", r
    s2 = sleeved_svc.settings()
    assert s2.sleeve_for_dte(20).max_positions == 2  # type: ignore[union-attr]
    assert "sleeve_max_positions" in codes(gate(call_at(20), s2, pf))


def test_sleeve_window_set_validated(sleeved_svc: ControlService) -> None:
    r = sleeved_svc.set(
        "profiles.cash_debit.sleeves.short.dte_max", "35", actor=OWNER, source="slack"
    )
    assert r.outcome == "refused"  # overlaps swing[30-60]
    r = sleeved_svc.set(
        "profiles.cash_debit.sleeves.short.dte_max", "25", actor=OWNER, source="slack"
    )
    assert r.outcome == "applied"
    assert sleeved_svc.settings().entry_dte_window == (10, 60)
    assert "dte_window" in codes(gate(call_at(27), sleeved_svc.settings()))
