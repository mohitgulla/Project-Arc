"""E27.6 pre-registered challenger specs (D126).

Every architecture and optimiser setting is fixed here before any dev result is seen;
there is no per-challenger grid search (M2 searched six LightGBM points, the
challengers get the published defaults of Gorishniy et al. 2021 for ResNet-MLP and
FT-Transformer, scaled to this host's 6 h budget). The training rows, folds, purge,
embargo, weights, label slippage, subsample cap, Huber delta, ``τ_stop`` percentile and
calibration settings are the registered M2 version's own (read from its manifest), so
the comparison is like for like: :func:`arc.ml.dl.pipeline.family_frame` refuses a
frame whose data digest differs from the registered version's.

Pre-registered decision rule ``dl_promote`` (per family, both dev test years):
the challenger's calibrated ``p_win`` log-loss is below M2's, its ``e_r`` per-day IC
is above M2's with Newey-West t > 2, and its ``ml_er`` decision-value replay beats
M2's ``ml_er`` replay on net P&L **and** max drawdown. A challenger that passes is
also checked against the absolute ``m2_promote`` rule (it must beat the logistic
baseline, managed PoP and the incumbent replay), because only a model that passes
that may become ``shadow`` and have the holdout read (D126/D133).

The stacker's "best DL challenger" per fold is the one with the highest ``e_r``
per-day IC on that fold's inner-validation block (the last train year), so the
choice reads no test-year label.
"""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

RULE_KIND = "dl_promote"
PROTOCOL_REV = 1

CHALLENGERS: tuple[str, ...] = ("mlp", "ft", "temporal", "surface")
STACK = "stack"
Device = Literal["cpu", "mps", "auto"]


class NetSpec(BaseModel):
    """One challenger's architecture + optimiser (pre-registered)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    trunk: Literal["resmlp", "ft"] = "resmlp"
    aux: Literal["none", "sequence", "surface"] = "none"
    seeds: int = Field(default=3, ge=1, description="seed ensemble averaged into the challenger")
    d_hidden: int = 256
    n_blocks: int = 3
    dropout: float = 0.15
    d_token: int = 32
    ft_layers: int = 2
    ft_heads: int = 4
    d_aux: int = Field(default=32, description="aux encoder embedding width")
    lr: float = 1e-3
    weight_decay: float = 1e-5
    batch: int = 2048
    max_epochs: int = 30
    patience: int = 4
    device: Device = "cpu"


def _default_nets() -> dict[str, NetSpec]:
    return {
        "mlp": NetSpec(trunk="resmlp", seeds=5),
        "ft": NetSpec(trunk="ft", seeds=3, batch=1024, lr=3e-4, max_epochs=20, device="auto"),
        "temporal": NetSpec(trunk="resmlp", aux="sequence", seeds=3, device="auto"),
        "surface": NetSpec(trunk="resmlp", aux="surface", seeds=3, device="auto"),
    }


class LossWeights(BaseModel):
    """Multi-task loss: Σ weight × per-head loss (heads mirror M2's outputs)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    win: float = 1.0
    stop: float = 1.0
    r: float = 1.0
    quantiles: float = 1.0
    days: float = 0.1
    conditional: float = 0.5


class DLConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    seed: int = 20261027
    torch_threads: int = Field(default=6, ge=1, le=6)
    quantile_bins: int = Field(default=1000, ge=10)
    seq_window: int = Field(default=60, ge=5, description="sessions per temporal window (card)")
    #: surface → move_vs_implied test (M1 side): horizon and seeds
    m1_horizon: int = 21
    m1_seeds: int = Field(default=3, ge=1)
    nets: dict[str, NetSpec] = Field(default_factory=_default_nets)
    loss: LossWeights = Field(default_factory=LossWeights)
    #: the M2 versions the challengers are compared with (data digest checked)
    m2_versions: dict[str, str] = Field(
        default_factory=lambda: {"credit": "m2-05fa82807a93", "debit": "m2-04e58916e118"}
    )
    #: E27.2 M1 version whose OOS ``m1_move_vs_implied_h21`` is the surface→M1 reference
    m1_version: str = "m1-53833784f276"

    def digest(self) -> str:
        body = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(body.encode()).hexdigest()[:12]
