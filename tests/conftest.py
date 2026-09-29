"""Suite-wide fixtures."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import yaml

from arc.universe.config import DEFAULT_UNIVERSE_CONFIG

if TYPE_CHECKING:
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
