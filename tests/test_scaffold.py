"""Smoke tests for the arc package scaffold."""

from __future__ import annotations

import importlib
import subprocess
import sys


def test_arc_package_imports() -> None:
    """All subpackages import without error."""
    packages = [
        "arc",
        "arc.config",
        "arc.utils",
        "arc.utils.calendar",
        "arc.cli",
        "arc.models",
        "arc.store",
        "arc.data",
        "arc.data.history",
        "arc.data.history.base",
        "arc.data.history.store",
        "arc.data.history.alpaca",
        "arc.data.history.thetadata",
        "arc.data.history.download",
        "arc.data.history.cli",
        "arc.data.base",
        "arc.data.alpaca",
        "arc.pricing",
        "arc.structures",
        "arc.structures.occ",
        "arc.structures.analytics",
        "arc.structures.builders",
        "arc.scanner",
        "arc.ingest",
        "arc.ingest.store",
        "arc.ingest.rss",
        "arc.ingest.edgar",
        "arc.ingest.earnings",
        "arc.ingest.youtube",
        "arc.ingest.llm",
        "arc.ingest.scout",
        "arc.context",
        "arc.context.kinds",
        "arc.context.store",
        "arc.context.ttl",
        "arc.routines",
        "arc.routines.cli",
        "arc.routines.conditions",
        "arc.routines.config",
        "arc.routines.dispatcher",
        "arc.routines.handlers",
        "arc.routines.heartbeat",
        "arc.routines.locks",
        "arc.routines.runs",
        "arc.routines.schedule",
        "arc.features",
        "arc.personas",
        "arc.gate",
        "arc.gate.inputs",
        "arc.gate.rules",
        "arc.approvals",
        "arc.execution",
        "arc.broker",
        "arc.broker.base",
        "arc.broker.alpaca_paper",
        "arc.reconcile",
        "arc.backtest",
    ]
    for pkg in packages:
        mod = importlib.import_module(pkg)
        assert mod is not None, f"Failed to import {pkg}"


def test_cli_help() -> None:
    """arc --help exits 0 and lists commands."""
    result = subprocess.run(
        [sys.executable, "-m", "arc.cli", "--help"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    for cmd in ("scan", "propose", "gate", "approve", "execute", "reconcile", "report"):
        assert cmd in result.stdout, f"{cmd} not in help output"


def test_cli_stub_command() -> None:
    """Stub commands print 'not yet implemented'."""
    from arc.cli import main

    assert main(["propose"]) == 0
