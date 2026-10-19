"""E7.19: the post-D128 attribution grid (scripts/sim_alignment_ablation.py)."""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import sim_alignment_ablation as ab  # noqa: E402


def test_margin_grid_is_full_factorial() -> None:
    cells = ab._cells("margin", "managed_net_ev", "credit_width")
    assert len(cells) == 16
    keys = {(c["ranker"], c["credit_only"], c["bp"], c["d131"]) for c in cells}
    assert len(keys) == 16
    names = {ab._name(f, c) for f in ("live", "none") for c in cells}
    assert len(names) == 32


def test_cash_grid_never_toggles_margin_only_knobs() -> None:
    cells = ab._cells("cash_debit", "managed_net_ev", "debit_width")
    assert len(cells) == 4
    assert all(c["credit_only"] is False and c["bp"] is False for c in cells)


def test_name_is_stable() -> None:
    c = {"ranker": "managed_net_ev", "credit_only": True, "bp": False, "d131": True}
    assert ab._name("live", c) == "f-live__managed_net_ev__credit__bp-off__d131-on"


def test_debit_kinds_are_the_debit_strategies() -> None:
    from arc.backtest.strategies import CREDIT_KINDS

    assert not set(ab.DEBIT_KINDS) & {str(getattr(k, "value", k)) for k in CREDIT_KINDS}


def test_eq_at_takes_last_value_on_or_before_day() -> None:
    idx = [dt.date(2021, 12, 30), dt.date(2021, 12, 31), dt.date(2022, 1, 3)]
    eq = pd.Series([100.4, 200.6, 300.0], index=idx)
    assert ab._eq_at(eq, dt.date(2021, 12, 31)) == 201.0
    assert ab._eq_at(eq, dt.date(2021, 1, 1)) is None
