"""Beta vs a benchmark from daily closes (E3.6, D62). Pure, deterministic.

beta = cov(r_name, r_bench) / var(r_bench) over the last *window* aligned daily log
returns. Two series are aligned on the dates both have a positive close; a return is
taken between consecutive aligned dates. Fewer than *min_days* returns (or a flat
benchmark) gives ``beta = None``; callers then count the name as plain stock (1.0).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import datetime as dt
    from collections.abc import Mapping

BENCHMARK = "SPY"
WINDOW = 252
MIN_DAYS = 120
#: D62: a name never counts as less than plain stock.
BETA_FLOOR = 1.0


@dataclass(frozen=True)
class BetaResult:
    """Raw beta (``None`` = too few aligned days) and the evidence behind it."""

    beta: float | None
    n_days: int
    window: int
    as_of: dt.date | None


def beta_vs(
    closes: Mapping[dt.date, float],
    bench: Mapping[dt.date, float],
    window: int = WINDOW,
    min_days: int = MIN_DAYS,
) -> BetaResult:
    """Beta of *closes* against *bench* over the last *window* aligned log returns."""
    days = sorted(d for d, c in closes.items() if c > 0 and bench.get(d, 0) > 0)
    days = days[-(window + 1) :]
    r_x = [math.log(closes[b] / closes[a]) for a, b in zip(days, days[1:], strict=False)]
    r_m = [math.log(bench[b] / bench[a]) for a, b in zip(days, days[1:], strict=False)]
    n = len(r_m)
    as_of = days[-1] if days else None
    if n < max(min_days, 2):
        return BetaResult(None, n, window, as_of)
    mx, mm = sum(r_x) / n, sum(r_m) / n
    var = sum((m - mm) ** 2 for m in r_m)
    if var <= 0:
        return BetaResult(None, n, window, as_of)
    cov = sum((x - mx) * (m - mm) for x, m in zip(r_x, r_m, strict=True))
    return BetaResult(cov / var, n, window, as_of)


def floored_beta(beta: float | None) -> float:
    """D62: the beta the caps use, ``max(beta, 1.0)``; missing / non-finite -> 1.0."""
    if beta is None or not math.isfinite(beta):
        return BETA_FLOOR
    return max(beta, BETA_FLOOR)
