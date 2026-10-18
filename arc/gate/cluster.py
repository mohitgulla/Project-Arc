"""D131 (E28.5): correlation cluster cap — pure, no storage, no network.

A new open on ticker *T* is the ``max_names + 1``-th name of its cluster when at least
``cluster_cap_max_names`` (2) *other* open names *U* (open positions plus reserved
in-flight opens) have ``corr(T, U) > cluster_cap_corr_threshold`` (0.7). The owner rule
is at most 2 open names per cluster, so that open fails the gate's ``cluster_cap`` rule.

* A pair with no stored correlation counts as **correlated** (fail closed, like a
  missing β = 1.0) and is named in the violation.
* Same-ticker positions are not peers (other rules govern a second structure on *T*).
* Direction-agnostic in v1: a bull put and a bear call on two correlated names count
  alike (E7.18 measured same-cluster names regardless of direction).

The correlations come in on :attr:`arc.gate.inputs.MarketSnapshot.correlations`, keyed
by :func:`corr_key`, built outside the gate from the ``correlations`` table (E28.4).
The pipeline's pre-gate (Research / Quant lines, Quant menu drops) and the backtest
call :func:`cluster_check` too, so they agree with the rule by construction.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

__all__ = ["ClusterCheck", "cluster_check", "corr_key"]


def corr_key(a: str, b: str) -> str:
    """``"AAA|BBB"``: the two tickers upper-cased and sorted (the snapshot's dict key)."""
    x, y = a.upper(), b.upper()
    return f"{x}|{y}" if x < y else f"{y}|{x}"


class ClusterCheck(NamedTuple):
    """*ticker*'s open cluster peers: ``peers`` (corr above the threshold or unknown,
    sorted) and ``missing`` (the subset with no stored correlation)."""

    ticker: str
    peers: tuple[str, ...]
    missing: tuple[str, ...]
    max_names: int
    threshold: float

    @property
    def blocked(self) -> bool:
        """A new open would make *ticker* the (max_names + 1)-th name of the cluster."""
        return len(self.peers) >= self.max_names

    def describe(self) -> str:
        """``AMD, AVGO (no correlation stored for AVGO: counted)`` for prompts and violations."""
        text = ", ".join(self.peers) or "none"
        if self.missing:
            text += f" (no correlation stored for {', '.join(self.missing)}: counted)"
        return text


def cluster_check(
    ticker: str,
    open_names: Iterable[str],
    correlations: Mapping[str, float],
    *,
    threshold: float,
    max_names: int,
) -> ClusterCheck:
    """*ticker*'s cluster peers among *open_names* (see the module doc)."""
    me = ticker.upper()
    peers: set[str] = set()
    missing: set[str] = set()
    for name in open_names:
        other = name.upper()
        if other == me:
            continue
        c = correlations.get(corr_key(me, other))
        if c is None:
            missing.add(other)
            peers.add(other)
        elif c > threshold:
            peers.add(other)
    return ClusterCheck(me, tuple(sorted(peers)), tuple(sorted(missing)), max_names, threshold)
