"""Backtest-only ML arm options (card E27.5, PLAN D126): ``config/ranking.yaml`` →
``backtest``.

* ``ml_ranker``: ``incumbent`` (default) or ``ml_er``: sort the cross-name pool by the
  M2 expected return ``e_r`` (higher first), then the incumbent's tie-breaks.
* ``ml_filter``: drop a candidate whose ``e_r`` is below ``min_e_r`` or whose
  ``p_stop`` is above ``max_p_stop`` (``null`` = that leg off).
* ``ml_size``: ``shrink`` scales the D18 contracts by
  ``clip(e_r / median_positive_e_r, floor, cap)`` (never above the D18 size).
* ``ml_versions``: pin a model version per family (``m1`` / ``credit`` / ``debit``);
  unset = the family's newest registered version (never the champion pointer).

Every option defaults off and is left out of the config dump at its default, so the
config hashes of existing studies, menus and experiments are unchanged. The M1 Monte
Carlo path source is ``exits.model.path_vol: m1`` (an ``exits`` overlay) and the
``m1_direction`` stance proxy is a robustness-grid stance (:mod:`arc.backtest.stance`).
None of this reaches live ranking or sizing: the sim wraps them (:mod:`arc.backtest.ml_arms`).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

_FORBID = ConfigDict(extra="forbid", frozen=True)

MlRanker = Literal["incumbent", "ml_er"]
ML_FAMILIES: tuple[str, ...] = ("m1", "credit", "debit")


class MlFilter(BaseModel):
    """``backtest.ml_filter``: both legs ``null`` = off."""

    model_config = _FORBID

    min_e_r: float | None = Field(
        None, description="Drop a candidate whose M2 e_r is below this (R units); null = off"
    )
    max_p_stop: float | None = Field(
        None, gt=0.0, le=1.0, description="Drop a candidate whose M2 p_stop is above this"
    )

    @property
    def off(self) -> bool:
        return self.min_e_r is None and self.max_p_stop is None


class MlSize(BaseModel):
    """``backtest.ml_size``: ``off`` (default) or ``shrink``."""

    model_config = _FORBID

    mode: Literal["off", "shrink"] = "off"
    floor: float = Field(0.5, gt=0.0, le=1.0, description="Smallest scale of the D18 size")
    cap: float = Field(1.0, gt=0.0, le=1.0, description="Largest scale (≤ 1: never upsizes)")

    @model_validator(mode="after")
    def _order(self) -> MlSize:
        if self.floor > self.cap:
            msg = "ml_size: floor must be <= cap"
            raise ValueError(msg)
        return self

    @property
    def is_default(self) -> bool:
        return self.mode == "off" and self.floor == 0.5 and self.cap == 1.0  # noqa: PLR2004


def ml_versions_ok(v: dict[str, str]) -> dict[str, str]:
    bad = sorted(set(v) - set(ML_FAMILIES))
    if bad:
        msg = f"ml_versions: unknown family {bad} (one of {', '.join(ML_FAMILIES)})"
        raise ValueError(msg)
    return v
