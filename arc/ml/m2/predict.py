"""M2 scoring (E27.3): load a registered version and score candidate rows.

:func:`score_rows` is the one scoring function (the trainer, the replays and a future
live scorer all call it), so a stored model and an in-memory fold model can't drift.
Outputs (:data:`OUTPUTS`): calibrated ``p_win`` / ``p_stop``, ``e_r`` (Huber
regressor), sorted ``r_q10/q50/q90``, ``e_days``, plus the cross-check ``e_r_mix`` =
``p_win × E[r|win] + (1 − p_win) × E[r|loss]`` and the raw classifier scores.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import numpy as np
import pandas as pd

from arc.ml.calibrate import FamilyCalibrator
from arc.ml.m2 import dataset as ds

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

OUTPUTS = ("p_win", "p_stop", "e_r", "r_q10", "r_q50", "r_q90", "e_days")


def score_rows(
    boosters: Mapping[str, Any],
    calib: Mapping[str, FamilyCalibrator],
    features: Sequence[str],
    vocab: dict[str, list[str]],
    d: pd.DataFrame,
) -> pd.DataFrame:
    x = ds.design(d, features, vocab).to_numpy(dtype=np.float64)
    kinds = d["kind"].astype(str).to_numpy()
    raw = {t: np.asarray(b.predict(x), dtype=float) for t, b in boosters.items()}
    out = pd.DataFrame(index=d.index)
    out["p_win"] = calib["win"].predict(raw["win"], kinds)
    out["p_stop"] = calib["stop"].predict(raw["stop"], kinds)
    out["e_r"] = raw["r"]
    qs = np.sort(np.vstack([raw["q10"], raw["q50"], raw["q90"]]), axis=0)
    out["r_q10"], out["r_q50"], out["r_q90"] = qs[0], qs[1], qs[2]
    out["e_days"] = np.maximum(raw["days"], 0.0)
    out["e_r_mix"] = out["p_win"] * raw["r_win"] + (1.0 - out["p_win"]) * raw["r_loss"]
    out["raw_win"] = raw["win"]
    out["raw_stop"] = raw["stop"]
    return out


@dataclass
class M2Model:
    manifest: dict[str, Any]
    boosters: dict[str, Any]
    calib: dict[str, FamilyCalibrator]
    features: list[str]
    vocab: dict[str, list[str]]

    @property
    def tau_stop(self) -> float:
        return float(self.manifest["tau_stop"])

    def score(self, d: pd.DataFrame) -> pd.DataFrame:
        return score_rows(self.boosters, self.calib, self.features, self.vocab, d)


def load_dir(vdir: Path, manifest: dict[str, Any] | None = None) -> M2Model:
    """A model directory (registered version or a ``folds/<name>`` dir)."""
    from arc.ml.m2.train import load_lightgbm

    lgb = load_lightgbm()

    if manifest is None:
        mp = vdir / "manifest.json"
        manifest = (
            json.loads(mp.read_text())
            if mp.is_file()
            else json.loads((vdir / "meta.json").read_text())
        )
    root = vdir if (vdir / "features.json").is_file() else vdir.parent.parent
    boosters = {
        p.stem: lgb.Booster(model_file=str(p)) for p in sorted((vdir / "lgbm").glob("*.txt"))
    }
    calib = {
        k: FamilyCalibrator.model_validate(c)
        for k, c in json.loads((vdir / "calibrators.json").read_text()).items()
    }
    return M2Model(
        manifest=manifest,
        boosters=boosters,
        calib=calib,
        features=json.loads((root / "features.json").read_text()),
        vocab=json.loads((root / "vocab.json").read_text()),
    )


def load_model(root: Path, family: str, version: str, *, verify: bool = True) -> M2Model:
    """A registered version; its file hashes are checked first (registry)."""
    from arc.ml.registry import verify as verify_hashes

    if verify:
        verify_hashes(root, family, version)
    return load_dir(root / family / version)
