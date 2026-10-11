"""E20.1 (D85): the shipped defaults equal the live runtime config, with an empty store.

Every other test runs on the pre-D85 env pin (``tests/pre_d85_env.py``); this file
clears it so it reads the real shipped defaults.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from pathlib import Path
from unittest import mock

import pytest

from arc.config import ArcEnv, ArcSettings, get_settings
from arc.control.effective import apply_changes, effective_routines, effective_settings
from arc.control.registry import lookup
from arc.control.service import ControlService
from arc.store.db import connect
from arc.store.migrate import migrate
from arc.utils.calendar import ET
from tests.pre_d85_env import PRE_D85_ENV

REPO = Path(__file__).resolve().parent.parent
NOW = dt.datetime(2026, 10, 12, 9, 0, tzinfo=ET)
_FAKE_LIVE = Path(__file__)  # any existing file satisfies the live-env guard


@pytest.fixture(autouse=True)
def _shipped_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in PRE_D85_ENV:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv("ARC_ENV", raising=False)


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = connect(":memory:")
    migrate(c)
    c.row_factory = sqlite3.Row
    return c


def _paper() -> ArcSettings:
    return ArcSettings(_env_file=None)  # type: ignore[call-arg]


def _live() -> ArcSettings:
    with mock.patch("arc.config._LIVE_ENV_PATH", _FAKE_LIVE):
        return ArcSettings(_env_file=None, env="live")  # type: ignore[call-arg]


def test_max_alloc_pct_default_is_ten_percent_at_the_registry_ceiling() -> None:
    assert get_settings(_env_file=None).max_alloc_pct == 0.10
    t = lookup("max_alloc_pct")
    assert t.max == 0.10 and t.hard_ceiling == 0.10  # ceiling unchanged (owner decision)


def test_empty_store_paper_effective_switches(conn: sqlite3.Connection) -> None:
    assert conn.execute("SELECT COUNT(*) FROM config_changes").fetchone()[0] == 0
    s = effective_settings(conn, base=_paper())
    assert s.env is ArcEnv.PAPER
    assert s.auto_approve is True
    assert s.auto_exit_defined_risk is True
    assert s.auto_approve_scorecard_gate is False
    assert s.max_alloc_pct == 0.10


def test_empty_store_live_stays_off_and_gated(conn: sqlite3.Connection) -> None:
    with mock.patch("arc.config._LIVE_ENV_PATH", _FAKE_LIVE):
        s = effective_settings(conn, base=_live())
    assert s.env is ArcEnv.LIVE
    assert s.auto_approve is False
    assert s.auto_exit_defined_risk is False
    # D70: the paper opt-out never reaches live; the live gate stays required
    assert s.auto_approve_scorecard_gate is True
    assert s.live_auto_approve_requires_gate is True
    assert s.live_gate_met is False


def test_live_store_off_override_of_scorecard_gate_is_still_forced_on() -> None:
    # A stored paper-key `off` can never switch the gate off in a live process.
    from arc.control.store import ConfigChange

    ch = ConfigChange(
        id=1,
        key="auto_approve.scorecard_gate",
        new=False,
        actor="t",
        at=NOW,
        source="cli",
        status="applied",
        direction="riskier",
    )
    with mock.patch("arc.config._LIVE_ENV_PATH", _FAKE_LIVE):
        s = apply_changes(_live(), {ch.key: ch}, version=1)
    assert s.auto_approve_scorecard_gate is True


def test_arc_auto_approve_env_var_stays_paper_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARC_AUTO_APPROVE", "true")
    monkeypatch.setenv("ARC_AUTO_EXIT_DEFINED_RISK", "true")
    s = _live()
    assert s.auto_approve is False and s.auto_exit_defined_risk is False
    monkeypatch.setenv("ARC_AUTO_APPROVE", "false")
    assert _paper().auto_approve is False  # the shortcut still turns paper off


def test_control_views_show_per_env_defaults(conn: sqlite3.Connection) -> None:
    paper = ControlService(conn, base=_paper(), now=lambda: NOW, is_halted=lambda: False)
    assert paper.view("auto_approve.paper").value is True
    assert paper.view("auto_approve.live").value is False
    assert paper.view("auto_exit_defined_risk.paper").value is True
    assert paper.view("auto_exit_defined_risk.live").value is False
    assert paper.view("auto_approve.scorecard_gate").value is False
    assert not any(paper.view(k).overridden for k in ("auto_approve.paper", "max_alloc_pct"))
    with mock.patch("arc.config._LIVE_ENV_PATH", _FAKE_LIVE):
        live = ControlService(conn, base=_live(), now=lambda: NOW, is_halted=lambda: False)
        # the paper key read from a live process shows the shipped paper default
        assert live.view("auto_approve.paper").value is True
        assert live.view("auto_approve.live").value is False
        assert live.view("auto_exit_defined_risk.live").value is False


def test_empty_store_effective_routines(conn: sqlite3.Connection) -> None:
    r = effective_routines(conn)
    assert r.finnhub_context.enabled is True
    assert r.director_diversification.mode == "relaxed"
    assert r.loop.max_idle == dt.timedelta(minutes=15)


def test_rollback_values_still_parse_and_apply(conn: sqlite3.Connection) -> None:
    svc = ControlService(conn, base=_paper(), now=lambda: NOW, is_halted=lambda: False)
    for key, value in (
        ("personas.finnhub_context", "off"),
        ("personas.director_diversification", "strict"),
        ("auto_approve.paper", "off"),
        ("auto_exit_defined_risk.paper", "off"),
    ):
        r = svc.set(key, value, actor="local", source="cli")
        assert r.outcome == "applied", (key, r.message)  # safer direction: no confirm
    r = effective_routines(conn)
    assert r.finnhub_context.enabled is False
    assert r.director_diversification.mode == "strict"
    s = svc.settings()
    assert s.auto_approve is False and s.auto_exit_defined_risk is False


def test_retired_drafts_are_gone() -> None:
    live = REPO / "config" / "experiments" / "live"
    assert not (live / "xp2_finnhub_context.yaml").exists()
    assert not (live / "xp3_relaxed_diversification.yaml").exists()
    for key in ("personas.finnhub_context", "personas.director_diversification"):
        assert "XP-" not in lookup(key).description
