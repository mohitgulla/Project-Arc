"""E15.12: the paired view attaches one experiment's arm stores, never every store ever
recorded (12 stores across XP-1..XP-3 > SQLite's ATTACH limit of 10 broke the nightly
``experiments.evaluate`` and ``arc experiment report``)."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sqlite3
from typing import TYPE_CHECKING

import pytest

from arc.experiments.arms import ArmIdentity, write_identity
from arc.experiments.cli import add_experiment_parser, run_experiment
from arc.experiments.paired import ArmAttachLimitError, attach_arms, paired_view
from arc.routines.runs import RoutineStateRepo
from arc.store.db import connect
from arc.store.migrate import migrate
from tests import experiment_fixtures as fx

if TYPE_CHECKING:
    from pathlib import Path

ARMS = ("treatment", "treatment_2", "treatment_3", "shadow_control")
NOW = dt.datetime(2026, 10, 27, 16, 40, tzinfo=dt.UTC)


def _arm_store(path: Path, control: Path, eid: str, arm: str) -> None:
    c = sqlite3.connect(str(path))
    migrate(c)
    write_identity(
        c,
        ArmIdentity(
            arm_id=f"{eid}:{arm}", experiment_id=eid, arm=arm, spec_arm=arm,
            keys_env="ALPACA_EXP", control_db=str(control), created_at=NOW,
        ),
    )  # fmt: skip
    c.close()


def _control_with_12_arm_stores(tmp_path: Path) -> tuple[Path, list[dt.date]]:
    """XP-3 running (A/B curves in the control store); XP-1..XP-3 x 4 arm stores recorded."""
    db = tmp_path / "control.db"
    c = connect(db)
    migrate(c)
    fx.start(c, fx.spec("XP-3"))
    days = fx.equity_curves(c, "XP-3", [100, -50, 30], [150, -20, 10])
    state = RoutineStateRepo(c)
    for eid in ("XP-1", "XP-2", "XP-3"):
        for arm in ARMS:
            path = tmp_path / f"exp-{eid}-{arm}.db"
            _arm_store(path, db, eid, arm)
            state.set(f"experiment_arm:{eid}:{arm}", str(path), now=NOW)
    c.close()
    return db, days


def _cli(*argv: str) -> argparse.Namespace:
    p = argparse.ArgumentParser()
    add_experiment_parser(p.add_subparsers(dest="command"))
    return p.parse_args(["experiment", *argv])


def _attached(view: sqlite3.Connection) -> list[str]:
    return sorted(
        str(view.execute(f"SELECT arm_id FROM {r[1]}.arm_identity").fetchone()[0])  # noqa: S608
        for r in view.execute("PRAGMA database_list")
        if r[1] not in ("main", "temp")
    )


def test_scoped_view_attaches_only_that_experiments_four_stores(tmp_path: Path) -> None:
    db, _ = _control_with_12_arm_stores(tmp_path)
    c = connect(db)
    try:
        with paired_view(c, experiment_id="XP-3") as view:
            assert _attached(view) == sorted(f"XP-3:{a}" for a in ARMS)
        # the unscoped call (every recorded store) is refused with the count, not SQLite's error
        with (
            pytest.raises(ArmAttachLimitError, match="12 arm stores for all recorded"),
            paired_view(c),
        ):
            pass  # pragma: no cover - never entered
    finally:
        c.close()


def test_report_and_evaluate_succeed_with_12_recorded_stores(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db, days = _control_with_12_arm_stores(tmp_path)
    now = (fx.eod(days[-1]) + dt.timedelta(minutes=15)).isoformat()
    assert run_experiment(_cli("report", "XP-3", "--db", str(db), "--now", now)) == 0
    assert "verdict CONTINUE" in capsys.readouterr().out
    assert run_experiment(_cli("covariate-check", "XP-3", "--db", str(db), "--now", now)) == 0
    capsys.readouterr()
    assert run_experiment(_cli("evaluate", "--db", str(db), "--now", now, "--json")) == 0
    out = capsys.readouterr().out
    stored = json.loads(out[out.index("[\n") :])  # structlog prints to stdout under pytest
    assert [r["experiment_id"] for r in stored] == ["XP-3"]
    assert stored[0]["sessions"] == 3


def test_attach_refuses_past_the_limit_before_attaching_anything(tmp_path: Path) -> None:
    ctl = tmp_path / "control.db"
    sqlite3.connect(str(ctl)).close()
    paths = []
    for i in range(11):
        p = tmp_path / f"arm{i}.db"
        _arm_store(p, ctl, "XP-9", f"t{i}")
        paths.append(p)
    c = sqlite3.connect(str(ctl))
    try:
        with pytest.raises(ArmAttachLimitError, match="11 arm stores for experiment XP-9"):
            attach_arms(c, paths, experiment_id="XP-9")
        assert [r[1] for r in c.execute("PRAGMA database_list")] == ["main"]
        assert len(attach_arms(c, paths[:10], experiment_id="XP-9")) == 10
    finally:
        c.close()


def test_explicit_paths_still_win_over_the_recorded_stores(tmp_path: Path) -> None:
    """repair-ledger passes the arm stores it repaired (e.g. a scratch copy)."""
    db, _ = _control_with_12_arm_stores(tmp_path)
    scratch = tmp_path / "scratch-XP-3-treatment.db"
    _arm_store(scratch, db, "XP-3", "treatment")
    c = connect(db)
    try:
        with paired_view(c, [scratch], experiment_id="XP-3") as view:
            assert _attached(view) == ["XP-3:treatment"]
            row = view.execute("PRAGMA database_list").fetchall()[-1]
            assert row[2] == str(scratch.resolve())
    finally:
        c.close()
