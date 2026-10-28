"""E27.4 (D126): the shadow-score data contracts (pydantic only, no numpy/pandas).

:class:`MlScore` is one menu candidate's M2 score; :class:`MlScoresPayload` is the
``ml_scores`` context entry that ``quant.open`` writes. Kept dependency-light so the
context registry (:mod:`arc.context.kinds`), the Tower and the journal can read the
contract without importing the model code. Shadow only: nothing here is a gate input.
"""

from __future__ import annotations

import datetime as dt  # noqa: TC003 - pydantic field
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ScoreStatus = Literal["ok", "unavailable"]
PanelSource = Literal["stored", "computed", "none"]


class MlScore(BaseModel):
    """One menu candidate's shadow score (the ``ml_scores`` context row)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str = Field(..., description="Menu item id: its legs, `+OCC,-OCC` sorted (legs_key)")
    ticker: str
    kind: str
    sleeve: str | None = None
    status: ScoreStatus
    reason: str | None = Field(None, description="Why unavailable (bundle / row error)")
    p_win: float | None = None
    p_stop: float | None = None
    e_r: float | None = Field(None, description="E[R], realised P&L / max loss")
    r_q10: float | None = None
    r_q50: float | None = None
    r_q90: float | None = None
    e_days: float | None = None
    family: str | None = None
    model_version: str | None = None
    feature_schema: str | None = None
    feature_digest: str | None = Field(None, description="12-hex sha256 of the model inputs")
    missing_features: int = Field(0, description="Model features that were NaN on this row")


class ScoredCandidate(MlScore):
    """An :class:`MlScore` plus the live and M2 ranks within its ticker's menu."""

    rank_live: int | None = Field(None, description="Position on the live menu (1 = top)")
    rank_m2: int | None = Field(None, description="Rank M2 would give it by e_r (1 = best)")
    managed_net_ev: float | None = Field(None, description="Live ranker key, $ per unit")


class M1Summary(BaseModel):
    """One ticker's M1 read at the panel date (E27.2), context only."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ticker: str
    version: str
    p_up_h21: float | None = None
    q10_h21: float | None = None
    q50_h21: float | None = None
    q90_h21: float | None = None
    rv_h21: float | None = None
    move_vs_implied_h21: float | None = None
    direction: float | None = Field(None, description="+1 up / 0 flat / -1 down (21 sessions)")


class MlScoresPayload(BaseModel):
    """The ``ml_scores`` context entry (one per ``quant.open`` run, subject session)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    as_of: dt.date
    family: str | None = None
    model_version: str | None = None
    feature_schema: str | None = None
    m1_version: str | None = None
    panel_date: dt.date | None = Field(None, description="Session the panel features are from")
    panel_source: PanelSource = "none"
    status: Literal["ok", "partial", "unavailable"]
    reason: str | None = Field(None, description="Why unavailable (bundle refusal, error)")
    n: int = 0
    n_ok: int = 0
    latency_ms: int = 0
    scores: list[ScoredCandidate] = Field(default_factory=list)
    m1: list[M1Summary] = Field(default_factory=list)

    def by_key(self) -> dict[str, ScoredCandidate]:
        return {s.key: s for s in self.scores}


def legs_key(legs: list[tuple[str, int]] | list[tuple[str, str]]) -> str:
    """``+OCC,-OCC`` sorted: a menu item's id in ``ml_scores`` (side ``+1``/``long`` or
    ``-1``/``short``), so the propose step finds a chosen structure's score by its legs."""
    out = []
    for sym, side in legs:
        s = side if isinstance(side, int) else (1 if str(side).lower() == "long" else -1)
        out.append(f"{'+' if s > 0 else '-'}{sym.replace(' ', '')}")
    return ",".join(sorted(out))
