"""Tests for arc.config — ARC_ENV, limits, universe."""

from __future__ import annotations

import os
from pathlib import Path
from unittest import mock

import pytest
from pydantic import ValidationError

from arc.config import DEFAULT_UNIVERSE, ArcEnv, ArcSettings, StructureKind, get_settings

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


class TestDefaults:
    """All defaults match PLAN.md §5 / D9."""

    def test_default_env_is_paper(self) -> None:
        s = get_settings()
        assert s.env is ArcEnv.PAPER

    def test_default_limits(self) -> None:
        s = get_settings()
        assert s.max_alloc_pct == 0.05
        assert s.daily_loss_halt_pct == 0.03
        assert s.spread_max_pct == 0.10
        assert s.spread_max_abs == 0.10
        assert s.wash_sale_days == 30
        assert s.portfolio_delta_cap == 0.30
        assert s.portfolio_vega_cap_pct == 0.005
        assert s.dte_min == 30
        assert s.dte_max == 45
        assert s.earnings_blackout is True
        assert s.max_open_positions == 8
        assert s.approval_ttl_seconds == 1200
        assert s.auto_approve is False

    def test_default_structure_whitelist(self) -> None:
        s = get_settings()
        expected = {
            StructureKind.VERTICAL_DEBIT,
            StructureKind.VERTICAL_CREDIT,
            StructureKind.IRON_CONDOR,
            StructureKind.LONG_CALL,
            StructureKind.LONG_PUT,
        }
        assert set(s.structure_whitelist) == expected

    def test_default_universe_is_d56_core(self) -> None:
        s = get_settings()
        assert s.universe == DEFAULT_UNIVERSE
        assert len(s.universe) == 20
        # no ETFs in the core; SPY/QQQ/IWM are the market reference, not trade names
        assert not {"SPY", "QQQ", "IWM"} & set(s.universe)
        assert s.universe[-4:] == ["PLTR", "HOOD", "INTC", "NFLX"]


# ---------------------------------------------------------------------------
# Env overrides
# ---------------------------------------------------------------------------


class TestEnvOverrides:
    """Settings are overridable via ARC_-prefixed env vars."""

    def test_override_max_alloc(self) -> None:
        with mock.patch.dict(os.environ, {"ARC_MAX_ALLOC_PCT": "0.10"}):
            s = ArcSettings()
        assert s.max_alloc_pct == 0.10

    def test_override_universe_json(self) -> None:
        with mock.patch.dict(os.environ, {"ARC_UNIVERSE": '["AAPL", "MSFT", "GOOG"]'}):
            s = ArcSettings()
        assert s.universe == ["AAPL", "MSFT", "GOOG"]

    def test_override_dte(self) -> None:
        with mock.patch.dict(os.environ, {"ARC_DTE_MIN": "20", "ARC_DTE_MAX": "60"}):
            s = ArcSettings()
        assert s.dte_min == 20
        assert s.dte_max == 60

    def test_override_max_open_positions(self) -> None:
        with mock.patch.dict(os.environ, {"ARC_MAX_OPEN_POSITIONS": "12"}):
            s = ArcSettings()
        assert s.max_open_positions == 12


# ---------------------------------------------------------------------------
# Constructor overrides
# ---------------------------------------------------------------------------


class TestConstructorOverrides:
    """Settings accept constructor kwargs (top priority)."""

    def test_override_via_kwargs(self) -> None:
        s = get_settings(max_alloc_pct=0.08, dte_min=25, dte_max=50)
        assert s.max_alloc_pct == 0.08
        assert s.dte_min == 25
        assert s.dte_max == 50

    def test_universe_list_override(self) -> None:
        s = get_settings(universe=["AAPL", "MSFT"])
        assert s.universe == ["AAPL", "MSFT"]


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


class TestValidation:
    """Field constraints and model validators."""

    def test_dte_max_below_min_rejected(self) -> None:
        with pytest.raises(ValidationError, match="dte_max"):
            get_settings(dte_min=40, dte_max=20)

    def test_max_alloc_above_one_rejected(self) -> None:
        with pytest.raises(ValidationError):
            get_settings(max_alloc_pct=1.5)

    def test_max_alloc_zero_rejected(self) -> None:
        with pytest.raises(ValidationError):
            get_settings(max_alloc_pct=0.0)

    def test_max_open_positions_zero_rejected(self) -> None:
        with pytest.raises(ValidationError):
            get_settings(max_open_positions=0)


# ---------------------------------------------------------------------------
# ARC_ENV=live enforcement
# ---------------------------------------------------------------------------


class TestLiveEnvEnforcement:
    """ARC_ENV=live requires ~/.arc/live.env to exist."""

    def test_live_without_env_file_raises(self) -> None:
        """When live.env is absent, setting env=live must raise."""
        # Ensure the file doesn't exist (it shouldn't in Phase 1)
        live_path = Path.home() / ".arc" / "live.env"
        assert not live_path.is_file(), "~/.arc/live.env must NOT exist in Phase 1"
        with pytest.raises(ValidationError, match="live.env"):
            get_settings(env="live")

    def test_live_with_env_file_succeeds(self, tmp_path: Path) -> None:
        """When live.env exists, env=live is accepted."""
        fake_live_env = tmp_path / "live.env"
        fake_live_env.write_text("# placeholder")
        with mock.patch("arc.config._LIVE_ENV_PATH", fake_live_env):
            s = get_settings(env="live")
        assert s.env is ArcEnv.LIVE

    def test_paper_does_not_need_env_file(self) -> None:
        s = get_settings(env="paper")
        assert s.env is ArcEnv.PAPER


# ---------------------------------------------------------------------------
# Auto-approve gate (D10)
# ---------------------------------------------------------------------------


class TestAutoApprove:
    """auto_approve is paper-only."""

    def test_auto_approve_paper(self) -> None:
        s = get_settings(auto_approve=True)
        assert s.auto_approve is True
        assert s.env is ArcEnv.PAPER

    def test_auto_approve_forced_off_live(self, tmp_path: Path) -> None:
        fake_live_env = tmp_path / "live.env"
        fake_live_env.write_text("# placeholder")
        with mock.patch("arc.config._LIVE_ENV_PATH", fake_live_env):
            s = get_settings(env="live", auto_approve=True)
        assert s.auto_approve is False
