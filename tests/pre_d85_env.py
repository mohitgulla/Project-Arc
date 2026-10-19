"""The pre-D85 settings defaults the suite runs on (E20.1).

D85 flipped these shipped defaults: paper auto-approve and auto-exit on, the E7.5a
scorecard gate off, and the per-underlying cap 5% -> 10%. Most of the suite exercises
the manual approval path, the gate's default-on behaviour and sizing numbers worked out
at 5%, so ``tests/conftest.py`` pins the old values through the ``ARC_*`` env vars for
every test. Tests of the shipped defaults clear them (``monkeypatch.delenv``), see
``tests/test_config_defaults_d85.py``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pytest

PRE_D85_ENV: dict[str, str] = {
    "ARC_AUTO_APPROVE": "false",
    "ARC_AUTO_EXIT_DEFINED_RISK": "false",
    "ARC_AUTO_APPROVE_SCORECARD_GATE": "true",
    "ARC_MAX_ALLOC_PCT": "0.05",
    # D131 (E28.1): the pre-D131 credit wing (fixed $ wing, no credit/width floor), so the
    # fixture pipelines' canned Quant picks keep their menu keys. The shipped pct_spot
    # wing + 0.10 floor are tested in tests/test_e281_credit_wings.py.
    "ARC_SCANNER_WING_MODE": "dollars",
    "ARC_SCANNER_MIN_CREDIT_WIDTH": "0.0",
    # D131 (E28.2): the short-premium regime gate off, so the fixture pipelines' bull puts
    # and condors are not refused for the missing SPY regime row (fail closed). The
    # shipped default (on) is tested in tests/test_e282_regime_gate.py.
    "ARC_REGIME_GATE_ENABLED": "false",
    # D131 (E28.3): the portfolio open-risk cap at its 100% rollback, so the fixture
    # pipelines' prompts, sizing and Tower goldens stay byte-identical. The shipped 40%
    # is tested in tests/test_e283_open_risk_cap.py and tests/test_gate_open_risk.py.
    "ARC_MAX_OPEN_RISK_PCT": "1.0",
    # D131 (E28.5): the correlation cluster cap off, so the fixture books (no stored
    # correlations: every pair unknown, counted) keep opening and their prompts stay
    # byte-identical. The shipped default (on) is tested in tests/test_gate_cluster.py
    # and tests/test_e285_cluster_cap.py.
    "ARC_CLUSTER_CAP_ENABLED": "false",
}


def reset_env(monkeypatch: pytest.MonkeyPatch, *names: str) -> None:
    """Undo a test's own env for *names*: back to the pre-D85 pin, or unset."""
    for name in names:
        if name in PRE_D85_ENV:
            monkeypatch.setenv(name, PRE_D85_ENV[name])
        else:
            monkeypatch.delenv(name, raising=False)
