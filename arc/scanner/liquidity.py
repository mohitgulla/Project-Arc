"""E2.6 (D116): liquidity-aware strike and expiry choice inside the delta bands.

Pure helpers for :mod:`arc.scanner.scan`; nothing here touches the network.

- :func:`liquidity_scores` gives every contract of one expiry a score in ``[0, 1]``::

      score = (w_oi * norm(log1p(OI)) + w_vol * norm(log1p(volume))
               + w_spread * (1 - min(spread_pct / spread_max_pct, 1))) / (w_oi + w_vol + w_spread)

  ``norm`` is min-max over the contracts passed in (the expiry's contracts that passed
  the filters), so the score is relative to that chain. A chain where every contract
  has the same OI (or volume) scores that term 1. ``spread_pct`` = (ask - bid) / mid.
  With the default weights (0.35 / 0.35 / 0.30, summing to 1) the divisor is 1.
- :func:`strike_key` is the strike sort key inside an unchanged delta band:
  ``fit / scale - lambda * score`` (lower is better), where ``fit`` is the pre-E2.6
  distance (|Δ - target|, or the width fit) and ``scale`` the band half-width (or the
  target width). The band check stays in the caller: a contract outside the band is
  never a candidate, however liquid.
- :func:`expiry_volume_shares`: per expiry, the traded volume of the filtered
  contracts within :data:`NEAR_SPOT_PCT` (15%) of spot, as a share of the expiries in
  the same DTE sleeve. :func:`expiry_bonus` turns that into the scanner rank bonus
  ``mu * share`` (0 for an expiry alone in its sleeve). It only reorders; no expiry
  is ever dropped.

``scanner.liquidity_rank.enabled: off`` (``ScanParams.liquidity_rank = None``) leaves
every selection and every dump byte-identical to the pre-E2.6 scanner.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

if TYPE_CHECKING:
    import datetime as dt
    from collections.abc import Iterable, Mapping, Sequence

    from arc.data.base import OptionContract

__all__ = [
    "NEAR_SPOT_PCT",
    "LiquidityRank",
    "LiquidityWeights",
    "contract_spread_pct",
    "expiry_bonus",
    "expiry_volume_shares",
    "liquidity_rank_from",
    "liquidity_scores",
    "min_leg_score",
    "strike_key",
]

#: Strikes within this share of spot count toward an expiry's traded volume.
NEAR_SPOT_PCT = 0.15


class LiquidityWeights(BaseModel):
    """Weights of the three score terms (normalised by their sum)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    oi: float = Field(0.35, ge=0.0, le=1.0)
    volume: float = Field(0.35, ge=0.0, le=1.0)
    spread: float = Field(0.30, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _some_weight(self) -> LiquidityWeights:
        if self.oi + self.volume + self.spread <= 0:
            msg = "liquidity weights: at least one weight must be > 0"
            raise ValueError(msg)
        return self


class LiquidityRank(BaseModel):
    """The scanner's E2.6 knobs (``config/strategy.yaml`` ``scanner.liquidity_rank``)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    weights: LiquidityWeights = Field(default_factory=lambda: LiquidityWeights())
    lam: float = Field(0.5, ge=0.0, le=2.0, description="Weight of the score vs the delta fit")
    expiry_mu: float = Field(
        0.25, ge=0.0, le=1.0, description="Rank bonus per unit of expiry volume share"
    )


def liquidity_rank_from(cfg: Any) -> LiquidityRank | None:
    """The scanner knobs of a ``scanner.liquidity_rank`` settings block; ``None`` when off."""
    if cfg is None or not cfg.enabled:
        return None
    w = cfg.weights
    return LiquidityRank(
        weights=LiquidityWeights(oi=w.oi, volume=w.volume, spread=w.spread),
        lam=cfg.lam,
        expiry_mu=cfg.expiry_mu,
    )


def contract_spread_pct(c: OptionContract) -> float:
    """(ask - bid) / mid of a quoted contract (the filters guarantee bid > 0, ask >= bid)."""
    assert c.bid is not None and c.ask is not None
    return (c.ask - c.bid) / ((c.bid + c.ask) / 2)


def _minmax(values: Mapping[str, float]) -> dict[str, float]:
    if not values:
        return {}
    lo, hi = min(values.values()), max(values.values())
    if hi - lo <= 1e-12:
        return dict.fromkeys(values, 1.0)
    return {k: (v - lo) / (hi - lo) for k, v in values.items()}


def liquidity_scores(
    contracts: Iterable[OptionContract], *, spread_max_pct: float, weights: LiquidityWeights
) -> dict[str, float]:
    """Score in ``[0, 1]`` per contract symbol, normalised within *contracts*."""
    cs = list(contracts)
    oi = _minmax({c.symbol: math.log1p(max(c.open_interest or 0, 0)) for c in cs})
    vol = _minmax({c.symbol: math.log1p(max(c.volume or 0, 0)) for c in cs})
    total = weights.oi + weights.volume + weights.spread
    out: dict[str, float] = {}
    for c in cs:
        tight = 0.0
        if spread_max_pct > 0:
            tight = 1.0 - min(contract_spread_pct(c) / spread_max_pct, 1.0)
        raw = weights.oi * oi[c.symbol] + weights.volume * vol[c.symbol] + weights.spread * tight
        out[c.symbol] = min(max(raw / total, 0.0), 1.0)
    return out


def strike_key(fit: float, scale: float, score: float, lam: float) -> float:
    """``fit / scale - lam * score`` (lower is better), rounded against float noise."""
    norm_fit = fit / scale if scale > 0 else fit
    return round(norm_fit - lam * score, 9)


def min_leg_score(symbols: Sequence[str], scores: Mapping[str, float]) -> float:
    """A structure's score: its thinnest leg's (the thin leg decides)."""
    return min(scores[s] for s in symbols)


def expiry_volume_shares(
    contracts: Iterable[OptionContract],
    spot: float,
    sleeve_of: Mapping[dt.date, int],
    *,
    near_spot_pct: float = NEAR_SPOT_PCT,
) -> tuple[dict[dt.date, int], dict[dt.date, float]]:
    """``(near-spot volume, share within its sleeve)`` per expiry of *sleeve_of*.

    *contracts* are the filtered contracts; *sleeve_of* maps each expiry that produced
    candidates to its DTE sleeve. A sleeve with no near-spot volume at all shares
    equally.
    """
    vol: dict[dt.date, int] = dict.fromkeys(sleeve_of, 0)
    for c in contracts:
        if c.expiration in vol and abs(c.strike - spot) <= near_spot_pct * spot:
            vol[c.expiration] += max(c.volume or 0, 0)
    by_sleeve: dict[int, list[dt.date]] = {}
    for e, s in sleeve_of.items():
        by_sleeve.setdefault(s, []).append(e)
    share: dict[dt.date, float] = {}
    for exps in by_sleeve.values():
        tot = sum(vol[e] for e in exps)
        for e in exps:
            share[e] = vol[e] / tot if tot > 0 else 1.0 / len(exps)
    return vol, share


def expiry_bonus(
    share: Mapping[dt.date, float], sleeve_of: Mapping[dt.date, int], mu: float
) -> dict[dt.date, float]:
    """``mu * share`` per expiry; 0 for an expiry alone in its sleeve."""
    count: dict[int, int] = {}
    for s in sleeve_of.values():
        count[s] = count.get(s, 0) + 1
    return {e: (mu * share[e] if count[sleeve_of[e]] >= 2 else 0.0) for e in sleeve_of}
