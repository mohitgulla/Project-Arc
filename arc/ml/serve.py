"""Live shadow scoring (E27.4, D126): load a registered M2 bundle and score menu rows.

``score(rows, bundle) → list[MlScore]`` is a pure function. Bundles load through
:func:`load_bundle`, which re-hashes every file the manifest lists (registry
:func:`~arc.ml.registry.verify`), refuses a bundle built on another feature schema than
the running ``FEATURE_SCHEMA_VERSION`` and evaluates the boosters with the pure-numpy
:class:`~arc.ml.lgbm_text.TextBooster` (``lightgbm`` / OpenMP are training-only
dependencies). Every failure is **fail open**: :func:`load_bundle_or_reason` returns a
reason string and :func:`score` returns ``status="unavailable"`` rows, so the trade path
never blocks on ML. Scores are context only: nothing under ``arc/gate/``, sizing,
ranking or persona prompts reads them (import contract + tests).
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

import numpy as np
from pydantic import BaseModel, ConfigDict, Field

from arc.ml.calibrate import FamilyCalibrator
from arc.ml.lgbm_text import LgbmTextError, TextBooster
from arc.ml.m2.config import FAMILIES, FAMILY_PROFILE
from arc.ml.m2.predict import OUTPUTS, score_rows
from arc.ml.registry import RegistryError, load_index, verify

if TYPE_CHECKING:
    from pathlib import Path

    import pandas as pd

ScoreStatus = Literal["ok", "unavailable"]
# Preference when the family has no champion: a shadow version, then the newest candidate.
_STATUS_RANK = {"champion": 0, "shadow": 1, "candidate": 2}


class BundleError(RuntimeError):
    """The bundle is missing, changed on disk, or built on another feature schema."""


class MlScore(BaseModel):
    """One menu candidate's shadow score (the ``ml_scores`` context row)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str = Field(..., description="Candidate key (kind|legs), as on the menu / ledger")
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


@dataclass(frozen=True)
class Bundle:
    family: str
    version: str
    manifest: dict[str, Any]
    boosters: dict[str, TextBooster]
    calib: dict[str, FamilyCalibrator]
    features: list[str]
    vocab: dict[str, list[str]]

    @property
    def feature_schema(self) -> str:
        return str(self.manifest.get("feature_schema") or "")


def family_for_profile(profile: str) -> str:
    """The M2 family trained on *profile*'s ledger (``margin`` → credit); credit else."""
    for fam in FAMILIES:
        if FAMILY_PROFILE.get(fam) == profile:
            return fam
    return FAMILIES[0]


def pick_version(root: Path, family: str) -> str:
    """Champion, else the newest shadow, else the newest candidate (retired never)."""
    idx = load_index(root, family)
    if idx.champion:
        return idx.champion
    live = [(v, e) for v, e in idx.versions.items() if e.status in _STATUS_RANK]
    if not live:
        msg = f"{family}: no registered version"
        raise BundleError(msg)
    live.sort(key=lambda ve: (_STATUS_RANK[ve[1].status], _neg(ve[1].created)))
    return live[0][0]


def _neg(created: str) -> tuple[int, ...]:
    return tuple(-ord(c) for c in created)  # newest first under an ascending sort


def load_bundle(
    root: Path, family: str, version: str | None = None, *, running_schema: str | None = None
) -> Bundle:
    """Verify then load a registered M2 version (no ``lightgbm`` needed)."""
    if running_schema is None:
        from arc.ml.features.registry import _running_version  # noqa: PLC0415

        running_schema = _running_version()
    try:
        version = version or pick_version(root, family)
        verify(root, family, version)
    except (RegistryError, OSError, ValueError) as e:
        raise BundleError(str(e)) from e
    vdir = root / family / version
    manifest = json.loads((vdir / "manifest.json").read_text())
    schema = manifest.get("feature_schema")
    if schema != running_schema:
        msg = f"{family}/{version}: feature schema {schema} != running {running_schema}"
        raise BundleError(msg)
    try:
        boosters = {p.stem: TextBooster.from_file(p) for p in sorted((vdir / "lgbm").glob("*.txt"))}
        calib = {
            k: FamilyCalibrator.model_validate(c)
            for k, c in json.loads((vdir / "calibrators.json").read_text()).items()
        }
        features = json.loads((vdir / "features.json").read_text())
        vocab = json.loads((vdir / "vocab.json").read_text())
    except (LgbmTextError, OSError, ValueError) as e:
        raise BundleError(f"{family}/{version}: {e}") from e
    need = {"win", "stop", "r", "q10", "q50", "q90", "days", "r_win", "r_loss"}
    if not need <= boosters.keys():
        msg = f"{family}/{version}: boosters missing {sorted(need - boosters.keys())}"
        raise BundleError(msg)
    return Bundle(family, version, manifest, boosters, calib, features, vocab)


def load_bundle_or_reason(root: Path, family: str, **kw: Any) -> Bundle | str:
    """:func:`load_bundle`, returning the refusal reason instead of raising (fail open)."""
    try:
        return load_bundle(root, family, **kw)
    except BundleError as e:
        return str(e)
    except Exception as e:  # noqa: BLE001 - fail open: any load error means no scores
        return f"{type(e).__name__}: {e}"


def feature_digest(values: np.ndarray) -> str:
    """12-hex sha256 of one row's model inputs (NaN-stable, 10 significant digits)."""
    parts = ["nan" if math.isnan(v) else f"{v:.10g}" for v in values.tolist()]
    return hashlib.sha256(",".join(parts).encode()).hexdigest()[:12]


def _ident(row: Any) -> dict[str, Any]:
    sleeve = row.get("sleeve")
    return {
        "key": str(row["key"]),
        "ticker": str(row["ticker"]),
        "kind": str(row["kind"]),
        "sleeve": None if sleeve is None or sleeve != sleeve else str(sleeve),
    }


def unavailable(rows: pd.DataFrame, reason: str) -> list[MlScore]:
    return [
        MlScore(**_ident(r), status="unavailable", reason=reason)
        for r in rows.to_dict(orient="records")
    ]


def score(rows: pd.DataFrame, bundle: Bundle | str) -> list[MlScore]:
    """Score candidate *rows* (ledger id columns + model features) with *bundle*.

    *bundle* may be the refusal reason from :func:`load_bundle_or_reason`; then every
    row comes back ``unavailable`` with that reason. Never raises on bad input.
    """
    if rows.empty:
        return []
    if isinstance(bundle, str):
        return unavailable(rows, bundle)
    from arc.ml.m2 import dataset as ds  # noqa: PLC0415

    try:
        x = ds.design(rows, bundle.features, bundle.vocab).to_numpy(dtype=np.float64)
        out = score_rows(bundle.boosters, bundle.calib, bundle.features, bundle.vocab, rows)
    except Exception as e:  # noqa: BLE001 - fail open
        return unavailable(rows, f"score error {type(e).__name__}: {e}")
    recs = rows.to_dict(orient="records")
    res: list[MlScore] = []
    for i, r in enumerate(recs):
        vals = {k: float(out[k].iloc[i]) for k in OUTPUTS}
        if not all(math.isfinite(v) for v in vals.values()):
            res.append(MlScore(**_ident(r), status="unavailable", reason="non-finite score"))
            continue
        res.append(
            MlScore(
                **_ident(r),
                status="ok",
                **{k: round(v, 6) for k, v in vals.items()},
                family=bundle.family,
                model_version=bundle.version,
                feature_schema=bundle.feature_schema,
                feature_digest=feature_digest(x[i]),
                missing_features=int(np.isnan(x[i]).sum()),
            )
        )
    return res


def m2_ranks(scores: list[MlScore]) -> dict[str, int]:
    """Rank (1 = best) M2 would give each scored key within its ticker, by ``e_r``."""
    by_t: dict[str, list[MlScore]] = {}
    for s in scores:
        if s.status == "ok" and s.e_r is not None:
            by_t.setdefault(s.ticker, []).append(s)
    ranks: dict[str, int] = {}
    for items in by_t.values():
        for n, s in enumerate(sorted(items, key=lambda s: (-(s.e_r or 0.0), s.key)), 1):
            ranks[s.key] = n
    return ranks
