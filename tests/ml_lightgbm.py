"""Shared test helper: import lightgbm (``ml`` extra) or skip.

``pytest.importorskip("lightgbm")`` raises ``OSError`` (an error, not a skip) on macOS
without Homebrew's ``libomp``; :func:`arc.ml.m2.train.load_lightgbm` turns that into an
``ImportError``, so the test skips (run with ``DYLD_LIBRARY_PATH`` = the bundled
``libomp`` dir to exercise it, OPS.md E27.4).
"""

from __future__ import annotations

from typing import Any

import pytest


def require_lightgbm() -> Any:
    from arc.ml.m2.train import load_lightgbm

    try:
        return load_lightgbm()
    except ImportError as exc:  # pragma: no cover - env without the ml extra
        pytest.skip(f"lightgbm unavailable: {exc}", allow_module_level=True)
