"""E18.3 (D78): exit-policy report script, the keep/rollback rule and the grid."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

REPO = Path(__file__).resolve().parent.parent


def _load(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(name, mod)
    spec.loader.exec_module(mod)
    return mod


epr = _load("exit_policy_report", REPO / "scripts" / "exit_policy_report.py")


def _m(name: str, *, bear: float, bull: float, dd: float, side: float = 0.0) -> Any:
    return epr.RunMetrics(
        name=name,
        trades=10,
        net_pnl=bear + bull + side,
        max_dd=dd,
        avg_hold_days=5.0,
        subperiods={"bear": bear, "sideways": side, "bull": bull},
    )


def test_keep_rule_keeps_when_two_of_three_and_dd_within_10pct() -> None:
    v1 = _m("v1", bear=-100, bull=500, dd=1000)
    v2 = _m("v2", bear=-50, bull=400, dd=1100)  # bear better, sideways 0 = 0, dd +10 %
    v = epr.keep_rule(v1, v2)
    assert v.keep
    assert v.subperiods_not_lower == ["bear", "sideways"]
    assert v.dd_ratio == pytest.approx(1.1)
    assert v.text.startswith("KEEP v2")


def test_keep_rule_rolls_back_on_drawdown() -> None:
    v1 = _m("v1", bear=-100, bull=500, dd=1000)
    v2 = _m("v2", bear=0, bull=600, dd=1101)  # every sub-period better, dd +10.1 %
    v = epr.keep_rule(v1, v2)
    assert not v.keep and "breached" in v.text


def test_keep_rule_rolls_back_on_subperiods() -> None:
    v1 = _m("v1", bear=-100, bull=500, dd=1000, side=10)
    v2 = _m("v2", bear=-200, bull=400, dd=500, side=10)  # only sideways not lower
    v = epr.keep_rule(v1, v2)
    assert not v.keep and v.subperiods_not_lower == ["sideways"]
    assert v.text.startswith("ROLL BACK")


def test_keep_rule_zero_drawdown_baseline() -> None:
    v1 = _m("v1", bear=0, bull=0, dd=0)
    assert epr.keep_rule(v1, _m("v2", bear=0, bull=0, dd=0)).keep
    assert not epr.keep_rule(v1, _m("v2", bear=0, bull=0, dd=1)).keep


def test_variants_v1_rollback_v2_shipped_and_hold() -> None:
    from arc.exits.policy import load_exit_config
    from arc.models import StructureKind

    cfgs = {k: load_exit_config(overrides=v) for k, v in epr.variant_overrides().items()}
    for kind in (StructureKind.VERTICAL_DEBIT, StructureKind.LONG_CALL, StructureKind.LONG_PUT):
        v1, v2, hold = (cfgs[k].policy_for(kind) for k in ("v1", "v2", "hold"))
        assert v1.take_profit_pct_of_debit == 1.0 and v1.profit_lock is None
        assert v1.stop == v2.stop and v1.close_at_dte == v2.close_at_dte  # only TP + lock move
        assert v2.take_profit_pct_of_debit == 0.6
        assert v2.profit_lock is not None
        assert (v2.profit_lock.arm_pct, v2.profit_lock.floor_pct) == (0.5, 0.2)
        assert hold.take_profit_pct_of_debit is None and hold.stop is None
        assert hold.profit_lock is None and hold.close_at_dte is None
    # credit kinds are untouched by every variant
    for k in cfgs:
        assert cfgs[k].policy_for(StructureKind.VERTICAL_CREDIT) == cfgs["v2"].policy_for(
            StructureKind.VERTICAL_CREDIT
        )


def test_grid_cells_cover_the_card_grid_and_flag_impossible_locks() -> None:
    cells = epr.grid_cells()
    locks = [c for c in cells if c.name.startswith("lock ")]
    assert len(locks) == 3 * 4
    assert len([c for c in cells if c.name.startswith("tp ")]) == 2 * 4
    by = {c.name: c for c in cells}
    assert by["lock 0.3->0.3 tp 0.60"].invalid  # floor == arm
    assert by["lock 0.5->0.0 tp 0.60"].invalid is None  # breakeven floor is valid
    assert by["tp 0.50 lock 0.5->0.2"].invalid  # arm == take profit: the lock never acts
    assert by["tp 0.60 lock 0.5->0.2"].invalid is None
    for c in cells:
        if c.invalid is None:
            epr.cell_config(c)  # loads


def test_metrics_reason_shares_subperiods_and_lock_shadow() -> None:
    t = pd.DataFrame(
        {
            "exit_reason": ["profit_lock", "take_profit", "stop", "expiry"],
            "pnl": [20.0, 60.0, -75.0, 5.0],
            "hold_to_expiry_pnl": [-40.0, 100.0, -100.0, 5.0],
            "trend": ["bull", "bull", "bear", "bear"],
            "days_held": [3, 5, 10, 30],
        }
    )
    eq = pd.Series([100.0, 120.0, 90.0, 110.0])
    m = epr.metrics("x", t, eq)
    assert m.trades == 4 and m.net_pnl == pytest.approx(10.0)
    assert m.max_dd == pytest.approx(30.0)
    assert m.by_reason["profit_lock"] == pytest.approx(0.25)
    assert m.subperiods == {"bear": -70.0, "sideways": 0.0, "bull": 80.0}
    assert (m.lock_pnl, m.lock_shadow) == (20.0, -40.0)
    assert m.avg_hold_days == pytest.approx(12.0)
    empty = epr.metrics("e", t.iloc[0:0], pd.Series(dtype=float))
    assert empty.trades == 0 and empty.subperiods["bull"] == 0.0


def _res() -> dict[str, Any]:
    from dataclasses import asdict

    runs = {
        "v1": _m("v1", bear=-100, bull=500, dd=1000),
        "v2": _m("v2", bear=-50, bull=450, dd=1050),
        "hold": _m("hold", bear=-300, bull=900, dd=3000),
        "v1 exits on v2 picks": _m("v1 exits on v2 picks", bear=-90, bull=480, dd=1000),
    }
    cells = epr.grid_cells()
    for c in cells:
        if c.invalid is None:
            runs[f"grid: {c.name}"] = _m(c.name, bear=-10, bull=100, dd=500)
    return {
        "runs": {k: asdict(v) for k, v in runs.items()},
        "cells": [asdict(c) for c in cells],
        "setup": {
            "profile": "cash_debit",
            "ranker": "debit_width",
            "tickers": list(epr.TICKERS),
            "start": "2024-03-01",
            "end": "2026-07-31",
            "window": [30, 60],
            "slippage_x": 0.25,
            "n_paths": 5000,
            "marks": "smile",
        },
    }


def test_render_states_the_verdict_and_the_fill_day_caveat() -> None:
    live = {
        "closed": [
            {
                "ticker": "VST",
                "kind": "vertical_debit",
                "opened_at": "2026-10-06",
                "closed_at": "2026-10-07T17:05:01Z",
                "exit_reason": "research_review",
                "realised": 160.0,
                "mae": -103.0,
                "mfe": 212.0,
                "peak_pct_of_debit": 0.135,
                "shadow": None,
                "under_v2": False,
            }
        ],
        "under_v2": 0,
    }
    md = epr.render(_res(), live)
    assert "**KEEP v2" in md
    assert "fill-day guard (E18.2) is not modelled" in md
    assert "n/a:" in md  # invalid grid cells listed
    assert "closed under v2" in md and "| VST |" in md
    assert "Highest peak: 14% of the debit" in md
    assert "_Not run" in epr.render(_res(), None)


def test_live_tally_reads_the_store_read_only(tmp_path: Path) -> None:
    import datetime as dt
    from decimal import Decimal

    from arc.journal.models import OutcomeRecord, OutcomeStatus
    from arc.journal.store import JournalStore
    from arc.store.db import connect
    from arc.store.migrate import migrate

    db = tmp_path / "s.db"
    conn = connect(db)
    migrate(conn)
    conn.execute("PRAGMA foreign_keys = OFF")  # a bare open_structures row is enough here
    conn.execute(
        """INSERT INTO open_structures (id, ticker, open_proposal_hash, candidate_id,
               structure_json, contracts, entry_net, status, opened_at, closed_at, close_net,
               exit_reason)
           VALUES ('os-1', 'VST', 'h1', 'c1', '{"kind": "vertical_debit"}', 2, '7.85',
                   'closed', '2026-10-06T18:14:15Z', '2026-10-07T17:05:01Z', '-8.65',
                   'research_review')"""
    )
    JournalStore(conn).record_outcome(
        OutcomeRecord(
            proposal_hash="h1",
            status=OutcomeStatus.CLOSED,
            contracts=2,
            realised_pnl=Decimal(160),
            max_adverse_excursion=Decimal(-103),
            max_favourable_excursion=Decimal(212),
            at=dt.datetime(2026, 10, 7, 17, 5, tzinfo=dt.UTC),
        )
    )
    conn.commit()
    conn.close()
    before = db.read_bytes()
    t = epr.live_tally(db)
    assert db.read_bytes() == before
    assert t["under_v2"] == 0
    (r,) = t["closed"]
    assert r["mfe"] == 212.0 and r["peak_pct_of_debit"] == pytest.approx(212 / 1570)
