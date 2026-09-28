"""Strategy specs and delta-targeted leg selection for the Phase-1 whitelist (PLAN D4).

A :class:`StrategySpec` fully describes how one structure is opened on one
session: which expirations qualify (DTE window), the target |Δ| of the
*anchor* strike and the wing width. Anchor = the short strike for credit
spreads, debit spreads and iron condors (D4: "16–30Δ short strikes"), and the
bought strike for single long options.

Geometry
--------
============  =========================================================
bull_put      short put @ anchor, long put ≈ anchor − width (credit)
bear_call     short call @ anchor, long call ≈ anchor + width (credit)
bull_call     short call @ anchor, long call ≈ anchor − width (debit)
bear_put      short put @ anchor, long put ≈ anchor + width (debit)
iron_condor   bull_put + bear_call at the same |Δ| and width
long_call     long call @ anchor
long_put      long put @ anchor
============  =========================================================
"""

from __future__ import annotations

import datetime as dt  # noqa: TC003 — pydantic fields
from enum import StrEnum
from typing import TYPE_CHECKING

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, model_validator

if TYPE_CHECKING:
    import pandas as pd

__all__ = [
    "ExpiryMode",
    "LegPick",
    "StrategyKind",
    "StrategySpec",
    "pick_expirations",
    "select_legs",
]


class StrategyKind(StrEnum):
    LONG_CALL = "long_call"
    LONG_PUT = "long_put"
    BULL_CALL = "bull_call"
    BEAR_PUT = "bear_put"
    BULL_PUT = "bull_put"
    BEAR_CALL = "bear_call"
    IRON_CONDOR = "iron_condor"


CREDIT_KINDS = frozenset({StrategyKind.BULL_PUT, StrategyKind.BEAR_CALL, StrategyKind.IRON_CONDOR})
SINGLE_KINDS = frozenset({StrategyKind.LONG_CALL, StrategyKind.LONG_PUT})


class ExpiryMode(StrEnum):
    NEAREST = "nearest"  # one expiration: closest to the middle of the DTE window
    ALL = "all"  # every expiration in the window (baseline: maximise trade count)


class StrategySpec(BaseModel):
    """How to open one structure on one session."""

    model_config = ConfigDict(frozen=True)

    kind: StrategyKind
    dte_min: int = Field(30, ge=1)
    dte_max: int = Field(45, ge=1)
    delta: float = Field(0.20, gt=0.0, lt=1.0, description="Target |Δ| of the anchor strike")
    delta_tol: float = Field(0.05, ge=0.0, description="Max |selected |Δ| − target|")
    width_pct: float = Field(0.02, gt=0.0, description="Wing width as a fraction of spot")
    expiry_mode: ExpiryMode = ExpiryMode.NEAREST
    min_volume: float = Field(1.0, ge=0.0, description="Min session volume on every leg")

    @model_validator(mode="after")
    def _window(self) -> StrategySpec:
        if self.dte_min > self.dte_max:
            msg = "dte_min must be <= dte_max"
            raise ValueError(msg)
        return self

    @property
    def label(self) -> str:
        w = "" if self.kind in SINGLE_KINDS else f"_w{self.width_pct * 100:g}"
        return f"{self.kind}_d{round(self.delta * 100)}{w}_dte{self.dte_min}-{self.dte_max}"


class LegPick(BaseModel):
    """One selected contract and its side (+1 long / −1 short)."""

    model_config = ConfigDict(frozen=True)

    symbol: str
    right: str
    strike: float
    expiration: dt.date
    side: int
    mid: float
    spread: float
    delta: float
    iv: float


def pick_expirations(chain: pd.DataFrame, spec: StrategySpec) -> list[dt.date]:
    """Expirations inside [dte_min, dte_max] per *spec.expiry_mode* (ascending)."""
    if chain.empty:
        return []
    by_exp = chain.groupby("expiration")["dte"].first()
    ok = by_exp[(by_exp >= spec.dte_min) & (by_exp <= spec.dte_max)]
    if ok.empty:
        return []
    if spec.expiry_mode is ExpiryMode.ALL:
        return sorted(ok.index)
    mid = (spec.dte_min + spec.dte_max) / 2.0
    dist = (ok - mid).abs()
    best = dist.min()
    return [min(d for d, v in dist.items() if v == best)]


def _pick(row: pd.Series, side: int) -> LegPick:
    return LegPick(
        symbol=str(row["symbol"]),
        right=str(row["right"]),
        strike=float(row["strike"]),
        expiration=row["expiration"],
        side=side,
        mid=float(row["mid"]),
        spread=float(row["spread"]),
        delta=float(row["delta"]),
        iv=float(row["iv"]),
    )


def _anchor(side_chain: pd.DataFrame, spec: StrategySpec) -> pd.Series | None:
    valid = side_chain[np.isfinite(side_chain["delta"])]
    if valid.empty:
        return None
    err = (valid["delta"].abs() - spec.delta).abs()
    i = err.idxmin()
    if err[i] > spec.delta_tol + 1e-12:
        return None
    return valid.loc[i]


def _wing(
    side_chain: pd.DataFrame, anchor_k: float, target_k: float, above: bool, width: float
) -> pd.Series | None:
    """Nearest strike to *target_k* strictly beyond the anchor, width within [0.5, 2]×."""
    beyond = side_chain["strike"] > anchor_k if above else side_chain["strike"] < anchor_k
    cand = side_chain[beyond]
    if cand.empty:
        return None
    i = (cand["strike"] - target_k).abs().idxmin()
    got = abs(float(cand.loc[i, "strike"]) - anchor_k)
    if not (0.5 * width <= got <= 2.0 * width):
        return None
    return cand.loc[i]


def _vertical(
    side_chain: pd.DataFrame, spec: StrategySpec, spot: float, *, wing_above: bool
) -> list[LegPick] | None:
    """Short the anchor, buy the wing. Wing direction alone fixes debit vs credit."""
    a = _anchor(side_chain, spec)
    if a is None:
        return None
    width = spec.width_pct * spot
    k = float(a["strike"])
    w = _wing(side_chain, k, k + width if wing_above else k - width, wing_above, width)
    if w is None:
        return None
    return [_pick(w, +1), _pick(a, -1)]


def select_legs(
    chain: pd.DataFrame, spec: StrategySpec, spot: float, expiration: dt.date
) -> list[LegPick] | None:
    """Legs for *spec* on one expiration of a prepared chain, or ``None`` if not tradable."""
    c = chain[(chain["expiration"] == expiration) & (chain["volume"].fillna(0) >= spec.min_volume)]
    calls = c[c["right"].astype(str) == "call"]
    puts = c[c["right"].astype(str) == "put"]
    k = spec.kind
    if k is StrategyKind.LONG_CALL:
        a = _anchor(calls, spec)
        return None if a is None else [_pick(a, +1)]
    if k is StrategyKind.LONG_PUT:
        a = _anchor(puts, spec)
        return None if a is None else [_pick(a, +1)]
    if k is StrategyKind.BULL_PUT:
        return _vertical(puts, spec, spot, wing_above=False)
    if k is StrategyKind.BEAR_CALL:
        return _vertical(calls, spec, spot, wing_above=True)
    if k is StrategyKind.BULL_CALL:
        return _vertical(calls, spec, spot, wing_above=False)
    if k is StrategyKind.BEAR_PUT:
        return _vertical(puts, spec, spot, wing_above=True)
    # iron condor
    p = _vertical(puts, spec, spot, wing_above=False)
    cl = _vertical(calls, spec, spot, wing_above=True)
    if p is None or cl is None:
        return None
    if p[1].strike >= cl[1].strike:  # short put must sit below short call
        return None
    return [*p, *cl]
