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
        "arc.llm_routing",
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
        "arc.data.recorded",
        "arc.data.history",
        "arc.data.history.base",
        "arc.data.history.store",
        "arc.data.history.alpaca",
        "arc.data.history.thetadata",
        "arc.data.history.download",
        "arc.data.history.cli",
        "arc.backtest.costs",
        "arc.backtest.chain",
        "arc.backtest.strategies",
        "arc.backtest.engine",
        "arc.backtest.metrics",
        "arc.backtest.regime",
        "arc.backtest.report",
        "arc.backtest.underlying",
        "arc.backtest.cli",
        "arc.pricing",
        "arc.structures",
        "arc.structures.occ",
        "arc.structures.analytics",
        "arc.structures.builders",
        "arc.scanner",
        "arc.scanner.filters",
        "arc.scanner.iv",
        "arc.scanner.scan",
        "arc.ingest",
        "arc.ingest.store",
        "arc.ingest.rss",
        "arc.ingest.edgar",
        "arc.ingest.earnings",
        "arc.ingest.youtube",
        "arc.ingest.caption_backoff",
        "arc.ingest.transcribe",
        "arc.ingest.llm",
        "arc.ingest.scout",
        "arc.ingest.channels",
        "arc.ingest.channels.base",
        "arc.ingest.channels.briefs",
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
        "arc.routines.monitor",
        "arc.routines.runs",
        "arc.routines.schedule",
        "arc.features",
        "arc.personas",
        "arc.gate",
        "arc.gate.inputs",
        "arc.gate.rules",
        "arc.gate.halt",
        "arc.approvals",
        "arc.journal",
        "arc.journal.reasons",
        "arc.journal.models",
        "arc.journal.store",
        "arc.journal.attribution",
        "arc.journal.report",
        "arc.journal.cli",
        "arc.approvals.card",
        "arc.approvals.trail",
        "arc.approvals.service",
        "arc.approvals.slack",
        "arc.approvals.cli",
        "arc.slack.blocks",
        "arc.slack.digests",
        "arc.execution",
        "arc.execution.guard",
        "arc.broker",
        "arc.broker.base",
        "arc.broker.alpaca_paper",
        "arc.reconcile",
        "arc.backtest",
        "arc.exits",
        "arc.exits.policy",
        "arc.exits.model",
        "arc.exits.position",
        "arc.exits.cli",
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
    for cmd in ("scan", "chains", "propose", "gate", "approve", "execute", "reconcile", "report"):
        assert cmd in result.stdout, f"{cmd} not in help output"


def test_cli_stub_command() -> None:
    """Stub commands print 'not yet implemented'."""
    from arc.cli import main

    assert main(["report"]) == 0
