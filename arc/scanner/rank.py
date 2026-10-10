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
``managed_net_ev_tilted`` / ``rorc_day_tilted``
                   E7.5b (D79): the same keys from the managed model run under a
                   stance-signed drift (:func:`tilted_drift`) instead of ``r``. A
                   ranking key only: the card, the D41 Net EV floor and the gate keep
                   the untilted numbers. Equal to the untilted key when the tilt is 0,
                   the stance is neutral, the kind is not directional or no tilted
                   model was run (the ``*_tilted`` input is ``None``).

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
    from collections.abc import Callable, Mapping, Sequence

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
    "passes_cost_filter",
    "live_net_ev_check",
    "passes_filters",
    "rank",
    "stance_sign",
    "tilted_drift",
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
    MANAGED_NET_EV_TILTED = "managed_net_ev_tilted"
    RORC_DAY_TILTED = "rorc_day_tilted"


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
    managed_net_ev_tilted: float | None = Field(
        None, description="E7.5b: managed Net EV under the stance-tilted drift (ranking only)"
    )
    rorc_day_tilted: float | None = Field(
        None, description="E7.5b: rorc_day under the stance-tilted drift (ranking only)"
    )
    est_cost: float | None = Field(
        None,
        ge=0.0,
        description="E2.4 expected round-trip cost under the managed exit policy, $ per unit "
        "(entry + exit spread/slippage, commissions, regulatory fees)",
    )


class RankFilters(BaseModel):
    """Hard filters applied before every ranker (identical for all of them)."""

    model_config = _FORBID

    enabled: bool = True
    min_managed_net_ev: float = Field(0.0, description="Keep only managed Net EV > this ($)")
    min_managed_pop: float = Field(0.0, ge=0.0, le=1.0, description="Keep managed PoP ≥ this")
    min_net_ev_to_cost: float | None = Field(
        None,
        ge=0.0,
        description="E7.5a: keep managed Net EV ÷ estimated cost ≥ this (None = off). A "
        "candidate without a cost estimate fails it when set.",
    )
    live: bool = Field(
        True,
        description="E6.4a: the live propose step rejects a structure whose managed Net EV "
        "is <= min_managed_net_ev (only that floor; the PoP / cost filters stay backtest-only)",
    )


class RankingConfig(BaseModel):
    """Validated ``config/ranking.yaml``."""

    model_config = _FORBID

    rankers: list[Ranker] = Field(default_factory=lambda: list(Ranker))
    vrp_threshold: float = Field(0.0, description="rorc_day_vrp drops candidates with vrp ≤ this")
    filters: RankFilters = Field(default_factory=RankFilters)


def load_ranking_config(
    path: Path | str | None = None, *, overrides: Mapping[tuple[str, ...], object] | None = None
) -> RankingConfig:
    """Load and validate the ranking config (default: ``config/ranking.yaml``).

    *overrides* (D26 control panel, ``path -> value`` from the file root, e.g.
    ``("ranking", "filters", "min_managed_net_ev")``) patch the YAML first;
    :func:`arc.control.effective.ranking_config` returns the effective config.
    """
    from arc.utils.yamlpatch import apply_overrides

    p = Path(path) if path is not None else DEFAULT_RANKING_PATH
    data = apply_overrides(yaml.safe_load(p.read_text()) or {}, overrides)
    return RankingConfig.model_validate(data.get("ranking", data))


def live_net_ev_check(managed_net_ev: float | None, f: RankFilters) -> tuple[bool, str]:
    """E6.4a live Net EV floor: ``(ok, why)`` for one re-priced proposal structure.

    Applies ``min_managed_net_ev`` (strict ``>``, $ per unit, E2.4 managed exits
    after all costs) when ``filters.enabled`` and ``filters.live``. A structure
    without a managed model fails closed: the floor cannot be shown to hold.
    """
    if not (f.enabled and f.live):
        return True, "live Net EV floor off"
    if managed_net_ev is None:
        return False, "no managed Net EV (exit model unavailable); floor fails closed"
    if managed_net_ev > f.min_managed_net_ev:
        return True, f"managed Net EV ${managed_net_ev:+,.2f} > floor ${f.min_managed_net_ev:+,.2f}"
    return False, (
        f"managed Net EV ${managed_net_ev:+,.2f}/unit after costs <= floor "
        f"${f.min_managed_net_ev:+,.2f}"
    )


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
    if not (c.managed_net_ev > f.min_managed_net_ev and c.managed_pop >= f.min_managed_pop):
        return False
    return passes_cost_filter(c, f.min_net_ev_to_cost)


def passes_cost_filter(c: RankInputs, min_ratio: float | None) -> bool:
    """E7.5a: managed Net EV ÷ estimated cost ≥ *min_ratio* (``None`` = no filter).

    A candidate with no cost estimate or no managed EV fails when the filter is set
    (fail closed); a zero cost passes whenever Net EV is positive.
    """
    if min_ratio is None:
        return True
    if c.managed_net_ev is None or c.est_cost is None:
        return False
    if c.est_cost <= 0.0:
        return c.managed_net_ev > 0.0
    return c.managed_net_ev / c.est_cost >= min_ratio


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


def _tilted_ev(c: RankInputs) -> float | None:
    """Tilted managed Net EV; the untilted value when no tilted model was run."""
    return c.managed_net_ev if c.managed_net_ev_tilted is None else c.managed_net_ev_tilted


def _tilted_rorc(c: RankInputs) -> float | None:
    """Tilted rorc_day; the untilted value when no tilted model was run."""
    return c.rorc_day if c.rorc_day_tilted is None else c.rorc_day_tilted


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
    if ranker is Ranker.MANAGED_NET_EV_TILTED:
        return lambda c: _field_key(_tilted_ev(c), _tilted_rorc(c), c.key)
    if ranker is Ranker.RORC_DAY_TILTED:
        return lambda c: _field_key(_tilted_rorc(c), _tilted_ev(c), c.key)

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


# ---------------------------------------------------------------------------
# E7.5b (D79): stance-tilted drift for the *_tilted rankers
# ---------------------------------------------------------------------------

# Structure kinds whose value depends on the direction of the underlying (verticals,
# single long options). An iron condor (or anything else) is always ranked at ``r``.
DIRECTIONAL_KINDS = frozenset({"vertical_debit", "vertical_credit", "long_call", "long_put"})


def stance_sign(stance: str | None, kind: str | None) -> int:
    """+1 bullish, -1 bearish, 0 neutral / unknown stance or a non-directional *kind*."""
    if kind is None or str(kind) not in DIRECTIONAL_KINDS:
        return 0
    return {"bullish": 1, "bearish": -1}.get(str(stance or "").lower(), 0)


def tilted_drift(*, r: float, sign: int, tilt: float, sigma: float, hold_years: float) -> float:
    """``mu = r + sign × tilt × sigma / sqrt(T_hold)`` (annual), the drift under which
    the expected log move over the hold is ``tilt`` of a 1σ hold move.

    *tilt* is config (``exits.pipeline.direction_tilt``), never an LLM number.
    ``sign == 0``, ``tilt == 0`` or a degenerate hold/vol returns *r* unchanged.
    """
    if sign == 0 or tilt == 0.0 or sigma <= 0.0 or hold_years <= 0.0:
        return r
    return r + math.copysign(1.0, sign) * tilt * sigma / math.sqrt(hold_years)
