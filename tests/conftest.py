"""Suite-wide fixtures."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest import mock

import pytest
import yaml

from arc.universe.config import DEFAULT_UNIVERSE_CONFIG
from tests.pre_d85_env import PRE_D85_ENV

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture(autouse=True)
def _hermetic_symbol_master(tmp_path_factory: pytest.TempPathFactory, monkeypatch) -> Path:
    """E5.7: never read (or write) the real ``data/symbol_master.json`` from a test.

    Points ``ARC_UNIVERSE_CONFIG_FILE`` at a copy of ``config/universe.yaml`` whose
    symbol-master cache lives in an empty temp dir, so the open universe starts with
    no master (seed list only) unless a test provides one.
    """
    root = tmp_path_factory.mktemp("universe")
    data = yaml.safe_load(DEFAULT_UNIVERSE_CONFIG.read_text())
    data.setdefault("symbol_master", {})["cache"] = str(root / "symbol_master.json")
    cfg = root / "universe.yaml"
    cfg.write_text(yaml.safe_dump(data))
    monkeypatch.setenv("ARC_UNIVERSE_CONFIG_FILE", str(cfg))
    return cfg


# D85 (E20.1): the suite runs on the pre-D85 defaults; see tests/pre_d85_env.py.
# Session scope so module-scoped fixtures (the shared fixture-pipeline DBs) see it too;
# a test's own monkeypatch.setenv/delenv still restores to these values afterwards.
@pytest.fixture(autouse=True, scope="session")
def _pre_d85_defaults() -> Iterator[None]:
    """Pin the pre-D85 per-env switches, scorecard gate and per-underlying cap."""
    with pytest.MonkeyPatch.context() as mp:
        for var, value in PRE_D85_ENV.items():
            mp.setenv(var, value)
        yield


@pytest.fixture(autouse=True)
def _stop_leaked_patches() -> Iterator[None]:
    """Stop every ``mock.patch(...).start()`` when its test ends.

    Helpers such as ``tests.test_scout_persona._guard`` start a patch on
    ``arc.universe.guard.measure_liquidity`` and rely on the calling module's own
    ``stopall`` fixture. A module that borrows the helper without that fixture
    (``test_carryover_d64``) leaked the fake into later tests on the same xdist
    worker, so ``test_universe`` saw every name as liquid, depending on test order.
    """
    yield
    mock.patch.stopall()
