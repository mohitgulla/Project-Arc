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
}


def reset_env(monkeypatch: pytest.MonkeyPatch, *names: str) -> None:
    """Undo a test's own env for *names*: back to the pre-D85 pin, or unset."""
    for name in names:
        if name in PRE_D85_ENV:
            monkeypatch.setenv(name, PRE_D85_ENV[name])
        else:
            monkeypatch.delenv(name, raising=False)
