"""Smoke tests for the arc package scaffold."""

from __future__ import annotations

import importlib
import pkgutil
import subprocess
import sys

import arc


def _arc_modules() -> list[str]:
    """Every module under ``arc/``, discovered rather than hand-listed."""
    return ["arc", *(m.name for m in pkgutil.walk_packages(arc.__path__, "arc."))]


def test_arc_package_imports() -> None:
    """Every arc module imports without error."""
    packages = _arc_modules()
    assert len(packages) == len(set(packages)), "duplicate module names"
    for pkg in packages:
        mod = importlib.import_module(pkg)
        assert mod is not None, f"Failed to import {pkg}"


def test_module_discovery_covers_core_packages() -> None:
    """Discovery is not silently empty: core domain packages are found."""
    found = set(_arc_modules())
    for pkg in ("arc.cli", "arc.gate", "arc.gate.rules", "arc.execution", "arc.broker.base"):
        assert pkg in found, f"{pkg} not discovered"


def test_cli_help() -> None:
    """arc --help exits 0 and lists commands."""
    result = subprocess.run(
        [sys.executable, "-m", "arc.cli", "--help"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    for cmd in (
        "scan",
        "chains",
        "propose",
        "gate",
        "approve",
        "execute",
        "reconcile",
        "report",
        "tower",
    ):
        assert cmd in result.stdout, f"{cmd} not in help output"


def test_cli_stub_command() -> None:
    """Stub commands print 'not yet implemented'."""
    from arc.cli import main

    assert main(["report"]) == 0
