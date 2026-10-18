"""E7.19 (D125): PoP of a structure with no breakevens (one ``(0, inf)`` interval).

A debit spread priced above its width (a thin far-dated quote that the all-expiry menus
reach) has no breakeven. The PoP probe for that interval used to be ``hi / 2 = inf``:
``inf - inf`` across the legs gave NaN, a ``RuntimeWarning`` and a silent PoP of 0.
The probe is now the spot. (The builders reject a non-positive debit, so the
always-profitable no-breakeven case never reaches the PoP.)
"""

from __future__ import annotations

import datetime as dt
import math
import warnings
from decimal import Decimal
from typing import TYPE_CHECKING

import pytest

from arc.exits.model import analytic_pop
from arc.scanner.scan import _pop
from arc.structures import debit_vertical, interval_probe

if TYPE_CHECKING:
    from arc.models import Structure

EXP = dt.date(2026, 11, 20)
AS_OF = dt.date(2026, 10, 16)
SPOT, IV, R = 100.0, 0.30, 0.04


def _spread(net: float) -> Structure:
    """A 100/105 bull call with net debit *net* (width 5)."""
    return debit_vertical(
        "call",
        "AAA",
        EXP,
        long_strike=100,
        long_premium=net + 1.0,
        short_strike=105,
        short_premium=1.0,
        as_of=AS_OF,
    )


def test_probe_is_finite_for_the_whole_line() -> None:
    assert interval_probe(0.0, math.inf, SPOT) == SPOT
    assert interval_probe(0.0, math.inf, 0.0) == 1.0
    assert interval_probe(0.0, 104.0, SPOT) == 52.0
    assert interval_probe(104.0, math.inf, SPOT) == 208.0
    assert interval_probe(100.0, 110.0, SPOT) == 105.0


@pytest.mark.parametrize(("net", "want"), [(5.5, 0.0), (7.0, 0.0)])
def test_no_breakeven_debit_spread_pop_is_exact_without_warning(net: float, want: float) -> None:
    s = _spread(net)
    assert s.breakevens == []  # debit > width: never profitable at expiry
    with warnings.catch_warnings():
        warnings.simplefilter("error", RuntimeWarning)
        assert analytic_pop(s, spot=SPOT, iv=IV, r=R) == want
        assert _pop(s, SPOT, IV, 35 / 365, R) == want


def test_normal_spread_pop_unchanged() -> None:
    s = _spread(2.0)
    assert [float(b) for b in s.breakevens] == [102.0]
    p = analytic_pop(s, spot=SPOT, iv=IV, r=R)
    assert 0.0 < p < 1.0
    assert _pop(s, SPOT, IV, s.dte / 365, R) == pytest.approx(p, abs=1e-9)
    assert Decimal(str(p)) > 0
