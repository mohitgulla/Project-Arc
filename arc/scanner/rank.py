"""Pluggable, config-selected candidate rankers (PLAN D25; card E7.5).

A ranker orders a menu of structure candidates for one ticker on one day. Every
ranker is a pure function of :class:`RankInputs`: no I/O, no clock, no LLM. The
E7.5 ranking backtest runs each of them over the same menus, so the only thing
that differs between runs is which candidate gets picked.

Rankers (:class:`Ranker`)
-------------------------
``credit_width``   Incumbent (scanner ``rank_by=credit_width``). Credit structures
                   by credit ÷ widest wing, ``ev_proxy`` breaks ties. Debit
                   structures have no credit; they rank *after* every credit
                   structure by ``ev_ratio`` (same grouping as the scanner).
``debit_width``    The debit analogue of ``credit_width``: debit verticals by payoff
                   width ÷ debit, ``ev_proxy`` breaks ties. Single long options have
                   no width; they rank after every vertical by ``ev_ratio``.
``ev_proxy``       Static, hold-to-expiry flat-vol EV after entry costs, dollars.
``managed_net_ev`` E2.4 managed-exit Monte Carlo Net EV, dollars (all costs).
``rorc_day``       managed Net EV ÷ (max loss × expected days held).
``rorc_day_vrp``   ``rorc_day`` after a VRP gate: a candidate is dropped when
                   ``ATM IV − realised-vol forecast ≤ vrp_threshold`` (or unknown).

A candidate whose primary key is unknown (``None``) is dropped by that ranker,
except under the width rankers, whose fallback group keeps it.

Hard filters (:class:`RankFilters`) run before any ranker and are the same for all
of them: managed Net EV > floor, managed PoP ≥ floor. Liquidity is enforced where
the menu is built (scanner liquidity rules live, leg volume in the backtester).

Ties are broken by the candidate ``key`` (e.g. its OCC legs), so the order is total
and deterministic.
"""

from __future__ import annotations

import math
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

import yaml
from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

__all__ = [
    "DEFAULT_RANKING_PATH",
    "INCUMBENT",
    "RankFilters",
    "RankInputs",
    "Ranker",
    "RankingConfig",
    "applicable",
    "incumbent_for",
    "load_ranking_config",
    "passes_filters",
    "rank",
]

DEFAULT_RANKING_PATH = Path(__file__).resolve().parent.parent.parent / "config" / "ranking.yaml"
_FORBID = ConfigDict(extra="forbid", frozen=True)


class Ranker(StrEnum):
    CREDIT_WIDTH = "credit_width"
    DEBIT_WIDTH = "debit_width"
    EV_PROXY = "ev_proxy"
    MANAGED_NET_EV = "managed_net_ev"
    RORC_DAY = "rorc_day"
    RORC_DAY_VRP = "rorc_day_vrp"


# The incumbent a ranker is compared against, by whether the account can sell premium.
INCUMBENT = {True: Ranker.CREDIT_WIDTH, False: Ranker.DEBIT_WIDTH}


class RankInputs(BaseModel):
    """What a ranker may read about one candidate (all known at the decision time)."""

    model_config = _FORBID

    key: str = Field(..., description="Stable id for tie-breaks, e.g. the legs' OCC symbols")
    credit: bool = Field(..., description="Net credit at mid")
    vertical: bool = Field(..., description="Two-leg vertical (has a payoff width)")
    credit_width: float | None = Field(None, description="credit ÷ widest wing (credits)")
    debit_width: float | None = Field(None, description="payoff width ÷ debit (debit verticals)")
    ev_proxy: float = Field(..., description="Static flat-vol EV after entry costs, $ per unit")
    ev_ratio: float | None = Field(None, description="ev_proxy ÷ max loss")
    managed_net_ev: float | None = Field(None, description="E2.4 managed Net EV, $ per unit")
    managed_pop: float | None = Field(None, ge=0.0, le=1.0, description="Managed PoP after costs")
    rorc_day: float | None = Field(None, description="managed Net EV ÷ (max loss × days held)")
    vrp: float | None = Field(None, description="ATM IV − realised-vol forecast")


class RankFilters(BaseModel):
    """Hard filters applied before every ranker (identical for all of them)."""

    model_config = _FORBID

    enabled: bool = True
    min_managed_net_ev: float = Field(0.0, description="Keep only managed Net EV > this ($)")
    min_managed_pop: float = Field(0.0, ge=0.0, le=1.0, description="Keep managed PoP ≥ this")


class RankingConfig(BaseModel):
    """Validated ``config/ranking.yaml``."""

    model_config = _FORBID

    rankers: list[Ranker] = Field(default_factory=lambda: list(Ranker))
    vrp_threshold: float = Field(0.0, description="rorc_day_vrp drops candidates with vrp ≤ this")
    filters: RankFilters = Field(default_factory=RankFilters)


def load_ranking_config(path: Path | str | None = None) -> RankingConfig:
    """Load and validate the ranking config (default: ``config/ranking.yaml``)."""
    p = Path(path) if path is not None else DEFAULT_RANKING_PATH
    data = yaml.safe_load(p.read_text()) or {}
    return RankingConfig.model_validate(data.get("ranking", data))


def applicable(ranker: Ranker, *, allows_credit: bool) -> bool:
    """``credit_width`` needs an account that can sell premium; ``debit_width`` one
    that cannot (it is the debit-only incumbent). The others apply everywhere."""
    if ranker is Ranker.CREDIT_WIDTH:
        return allows_credit
    if ranker is Ranker.DEBIT_WIDTH:
        return not allows_credit
    return True


def incumbent_for(*, allows_credit: bool) -> Ranker:
    return INCUMBENT[allows_credit]


def passes_filters(c: RankInputs, f: RankFilters) -> bool:
    """Hard filters; a candidate without a managed model fails them when enabled."""
    if not f.enabled:
        return True
    if c.managed_net_ev is None or c.managed_pop is None:
        return False
    return c.managed_net_ev > f.min_managed_net_ev and c.managed_pop >= f.min_managed_pop


def _num(x: float | None) -> float:
    """Descending sort component: larger first, unknown last."""
    return math.inf if x is None or not math.isfinite(x) else -x


_Key = tuple[int, float, float, str]


def _width_key(c: RankInputs, *, credit_side: bool) -> _Key | None:
    primary_ok = c.credit if credit_side else (not c.credit and c.vertical)
    width = c.credit_width if credit_side else c.debit_width
    if primary_ok and width is not None:
        return (0, _num(width), _num(c.ev_proxy), c.key)
    return (1, _num(c.ev_ratio), _num(c.ev_proxy), c.key)


def _field_key(value: float | None, tie: float | None, key: str) -> _Key | None:
    if value is None or not math.isfinite(value):
        return None
    return (0, -value, _num(tie), key)


def _key_fn(ranker: Ranker, vrp_threshold: float) -> Callable[[RankInputs], _Key | None]:
    if ranker is Ranker.CREDIT_WIDTH:
        return lambda c: _width_key(c, credit_side=True)
    if ranker is Ranker.DEBIT_WIDTH:
        return lambda c: _width_key(c, credit_side=False)
    if ranker is Ranker.EV_PROXY:
        return lambda c: _field_key(c.ev_proxy, c.ev_ratio, c.key)
    if ranker is Ranker.MANAGED_NET_EV:
        return lambda c: _field_key(c.managed_net_ev, c.rorc_day, c.key)
    if ranker is Ranker.RORC_DAY:
        return lambda c: _field_key(c.rorc_day, c.managed_net_ev, c.key)

    def vrp_gated(c: RankInputs) -> _Key | None:
        if c.vrp is None or c.vrp <= vrp_threshold:
            return None
        return _field_key(c.rorc_day, c.managed_net_ev, c.key)

    return vrp_gated


def rank(
    candidates: Sequence[RankInputs],
    ranker: Ranker,
    *,
    filters: RankFilters | None = None,
    vrp_threshold: float = 0.0,
) -> list[RankInputs]:
    """Candidates that pass *filters* and the ranker's own gate, best first."""
    f = filters or RankFilters(enabled=False)
    fn = _key_fn(ranker, vrp_threshold)
    keyed = [(k, c) for c in candidates if passes_filters(c, f) and (k := fn(c)) is not None]
    keyed.sort(key=lambda kc: kc[0])
    return [c for _, c in keyed]
