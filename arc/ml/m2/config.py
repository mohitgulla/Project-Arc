"""M2 training configuration (E27.3) and the ``--overlay <yaml>`` override (D133).

:class:`M2Config` is the whole pre-registered training spec: label slippage, the fixed
hyper-parameter grid, boosting limits, monotone constraints, the feature add/drop
lists, the calibration rule and the subsample cap. An E27.7 ``ML-<n>`` study passes an
overlay YAML whose keys deep-merge onto the defaults (:func:`load_config`); unknown
keys are refused, so a study can't smuggle in a setting the trainer ignores. The
promote rule is not configurable: it is the registered rule kind :data:`RULE_KIND`.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from pathlib import Path

RULE_KIND = "m2_promote"
Family = Literal["credit", "debit"]
FAMILIES: tuple[str, ...] = ("credit", "debit")
#: Profile family -> the ledger profile whose rows train it (card: credit = margin kinds,
#: debit = cash kinds).
FAMILY_PROFILE: dict[str, str] = {"credit": "margin", "debit": "cash_debit"}


class GridPoint(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    num_leaves: int = Field(..., ge=2)
    min_data_in_leaf: int = Field(..., ge=1)


class Calibration(BaseModel):
    model_config = ConfigDict(extra="forbid")

    method: Literal["isotonic"] = "isotonic"
    kind_ece_ratio: float = Field(
        2.0, gt=0, description="per-kind map when kind ECE > ratio x family ECE"
    )
    shrink_k: float = Field(2000.0, ge=0, description="per-kind shrinkage n_eff / (n_eff + k)")


class M2Config(BaseModel):
    """The pre-registered M2 training spec (defaults = the card)."""

    model_config = ConfigDict(extra="forbid")

    label_x: str = Field("x025", pattern=r"^x\d{3}$", description="ledger label slippage suffix")
    grid: list[GridPoint] = Field(
        default_factory=lambda: [
            GridPoint(num_leaves=n, min_data_in_leaf=m) for n in (15, 31, 63) for m in (200, 1000)
        ]
    )
    learning_rate: float = 0.03
    feature_fraction: float = 0.7
    bagging_fraction: float = 0.7
    bagging_freq: int = 1
    lambda_l2: float = 1.0
    max_bin: int = 63
    huber_alpha: float = Field(1.0, gt=0, description="Huber delta on r_net (units of max loss)")
    max_rounds: int = Field(1500, ge=1)
    early_stopping: int = Field(100, ge=1)
    seed: int = 20261026
    num_threads: int = Field(6, ge=1, le=6, description="fixed: dumps are byte-identical per count")
    #: feature -> +1 / -1 for the e_r regressor where finance fixes the sign (documented
    #: in docs/RESEARCH/ml-m2.md); the card reports the IC with and without.
    monotone_r: dict[str, int] = Field(
        default_factory=lambda: {
            "cand_spread_over_premium": -1,
            "cost_per_risk": -1,
        }
    )
    monotone_default: bool = Field(False, description="ship the constrained r model as the default")
    add_features: list[str] = Field(default_factory=list)
    drop_features: list[str] = Field(default_factory=list)
    calibration: Calibration = Field(default_factory=Calibration)
    cap_per_day: int | None = Field(
        600, ge=1, description="per-day stratified subsample (None = all)"
    )
    #: τ for the p_stop filter replay: this percentile of the train-window p_stop.
    p_stop_pct: float = Field(80.0, gt=0, lt=100)

    def lgb_base(self) -> dict[str, Any]:
        return {
            "learning_rate": self.learning_rate,
            "feature_fraction": self.feature_fraction,
            "bagging_fraction": self.bagging_fraction,
            "bagging_freq": self.bagging_freq,
            "lambda_l2": self.lambda_l2,
            "max_bin": self.max_bin,
            "seed": self.seed,
            "deterministic": True,
            "force_row_wise": True,
            "num_threads": self.num_threads,
            "verbose": -1,
        }

    def digest(self) -> str:
        body = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(body.encode()).hexdigest()[:12]


def _merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict) and k not in ("monotone_r",):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(overlay: Path | None = None) -> M2Config:
    """Defaults, deep-merged with the *overlay* YAML when given (unknown keys refused)."""
    base = M2Config()
    if overlay is None:
        return base
    import yaml

    raw = yaml.safe_load(overlay.read_text()) or {}
    if not isinstance(raw, dict):
        msg = f"{overlay}: an M2 overlay must be a mapping"
        raise ValueError(msg)
    if raw.get("decision_rule", RULE_KIND) != RULE_KIND:
        msg = f"{overlay}: decision_rule is fixed to {RULE_KIND!r}"
        raise ValueError(msg)
    raw.pop("decision_rule", None)
    return M2Config.model_validate(_merge(base.model_dump(mode="json"), raw))
