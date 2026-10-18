"""Correlation clusters (E28.4, D131): the pure lookup the cluster cap and the sim share.

A :class:`CorrMatrix` is one day's pairwise 60-session return correlations (from the
``correlations`` table via :func:`arc.betas.corr_store.corr_matrix`, or built point in
time from cached closes by :func:`arc.backtest.correlations.pit_matrix`).
:func:`cluster_peers` answers "which open names share a cluster with *ticker*?".

No storage, no network: the gate inputs and the backtest hand it a matrix.
"""

from __future__ import annotations

import datetime as _dt  # noqa: TC003 - dataclass field type
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from arc.features.correlation import CLUSTER_THRESHOLD, WINDOW

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

__all__ = ["CorrMatrix", "cluster_peers", "pair_key"]


def pair_key(a: str, b: str) -> tuple[str, str]:
    """The stored ``(a, b)`` order of a pair: upper-cased, ``a < b``."""
    x, y = a.upper(), b.upper()
    return (x, y) if x < y else (y, x)


@dataclass(frozen=True)
class CorrMatrix:
    """One day's pairwise correlations, keyed by :func:`pair_key`.

    ``day`` is the row day the values were computed for (``None`` = no stored day);
    ``tickers`` every name the run covered (a covered pair with no value had too few
    returns).
    """

    day: _dt.date | None
    pairs: Mapping[tuple[str, str], float] = field(default_factory=dict)
    window: int = WINDOW
    tickers: frozenset[str] = frozenset()

    def get(self, a: str, b: str) -> float | None:
        """Correlation of *a* and *b* (1.0 for a name with itself; ``None`` = unknown)."""
        if a.upper() == b.upper():
            return 1.0
        return self.pairs.get(pair_key(a, b))

    def top(self, n: int = 10, among: Iterable[str] | None = None) -> list[tuple[str, str, float]]:
        """The *n* most correlated pairs (optionally only pairs inside *among*)."""
        keep = None if among is None else {t.upper() for t in among}
        rows = [
            (a, b, c)
            for (a, b), c in self.pairs.items()
            if keep is None or (a in keep and b in keep)
        ]
        return sorted(rows, key=lambda r: (-r[2], r[0], r[1]))[:n]


def cluster_peers(
    matrix: CorrMatrix,
    ticker: str,
    open_tickers: Iterable[str],
    threshold: float = CLUSTER_THRESHOLD,
    *,
    missing_is_peer: bool = False,
) -> list[str]:
    """Open names in *ticker*'s cluster: correlation strictly above *threshold*.

    *ticker* itself is never its own peer (a second structure on the same name is not
    a new cluster member). *missing_is_peer* decides an unknown pair: the cluster-cap
    rule (E28.5) counts it as correlated (fails closed); reports count it as not.
    Returned sorted and de-duplicated.
    """
    me = ticker.upper()
    out: set[str] = set()
    for t in open_tickers:
        other = t.upper()
        if other == me:
            continue
        c = matrix.get(me, other)
        if (c is None and missing_is_peer) or (c is not None and c > threshold):
            out.add(other)
    return sorted(out)
