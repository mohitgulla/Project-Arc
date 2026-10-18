"""Return correlation between two daily close series (E28.4, D131). Pure, deterministic.

corr = Pearson correlation of the last *window* aligned daily log returns. Two series
are aligned on the dates both have a positive close; a return is taken between
consecutive aligned dates (so a halt day drops out of both sides). Fewer than
*min_obs* returns, or a flat series, gives ``corr = None`` (no ``correlations`` row).

D131: 60 sessions, at least 40 returns; two names with corr > 0.7 are one cluster.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import datetime as dt
    from collections.abc import Mapping

#: D131: 60-session return window, at least 40 aligned returns, corr > 0.7 = one cluster.
WINDOW = 60
MIN_OBS = 40
CLUSTER_THRESHOLD = 0.7


@dataclass(frozen=True)
class CorrResult:
    """Correlation (``None`` = too few returns or a flat series) and its evidence."""

    corr: float | None
    n_obs: int
    window: int
    as_of: dt.date | None


def log_returns(closes: Mapping[dt.date, float], days: list[dt.date]) -> list[float]:
    """Log returns of *closes* between consecutive *days* (all must have a close)."""
    return [math.log(closes[b] / closes[a]) for a, b in zip(days, days[1:], strict=False)]


def pearson(x: list[float], y: list[float]) -> float | None:
    """Pearson correlation of two equal-length samples; ``None`` when either is flat."""
    n = len(x)
    if n < 2 or len(y) != n:
        return None
    mx, my = sum(x) / n, sum(y) / n
    sxx = sum((a - mx) ** 2 for a in x)
    syy = sum((b - my) ** 2 for b in y)
    if sxx <= 0 or syy <= 0:
        return None
    sxy = sum((a - mx) * (b - my) for a, b in zip(x, y, strict=True))
    # clamp float noise (a perfectly collinear pair can land at 1.0000000000000002)
    return max(-1.0, min(1.0, sxy / math.sqrt(sxx * syy)))


def return_corr(
    a: Mapping[dt.date, float],
    b: Mapping[dt.date, float],
    window: int = WINDOW,
    min_obs: int = MIN_OBS,
) -> CorrResult:
    """Correlation of *a* and *b* over their last *window* aligned daily log returns."""
    days = sorted(d for d, c in a.items() if c > 0 and b.get(d, 0) > 0)
    days = days[-(window + 1) :]
    r_a, r_b = log_returns(a, days), log_returns(b, days)
    n = len(r_a)
    as_of = days[-1] if days else None
    if n < max(min_obs, 2):
        return CorrResult(None, n, window, as_of)
    return CorrResult(pearson(r_a, r_b), n, window, as_of)
