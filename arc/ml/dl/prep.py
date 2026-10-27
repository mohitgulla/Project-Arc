"""Input preparation for the DL challengers (E27.6): quantile-normalised numeric inputs,
missing flags and integer category codes, fitted on a fold's training rows only.

:class:`QuantileNormalizer` maps each numeric feature through its empirical CDF on the
training rows (``bins`` quantiles, linear interpolation, ties collapsed) and then the
standard-normal inverse CDF, clipped to ±5 (Gorishniy et al. 2021's
"quantile-normalised inputs"). A NaN input becomes 0 plus a 1 in the feature's missing
flag. A feature constant on the training rows maps to 0. The fit is a pure function of
the training rows, so the same rows give the same normaliser (stored in the challenger's
artefacts as JSON).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict
from scipy.special import ndtri

from arc.ml.m2 import dataset as ds

if TYPE_CHECKING:
    from collections.abc import Sequence

CLIP = 5.0


class QuantileNormalizer(BaseModel):
    """Per-feature quantile knots (fitted on training rows)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    features: list[str]
    knots: list[list[float]]

    @classmethod
    def fit(cls, x: np.ndarray, features: Sequence[str], bins: int = 1000) -> QuantileNormalizer:
        qs = np.linspace(0.0, 1.0, bins + 1)
        knots: list[list[float]] = []
        for j in range(x.shape[1]):
            col = x[:, j]
            col = col[np.isfinite(col)]
            if col.size == 0:
                knots.append([])
                continue
            k = np.unique(np.quantile(col, qs))
            knots.append([float(v) for v in k])
        return cls(features=list(features), knots=knots)

    def transform(self, x: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """(normalised values float32, missing flags float32), both ``(n, n_features)``."""
        n, f = x.shape
        out = np.zeros((n, f), dtype=np.float32)
        miss = ~np.isfinite(x)
        for j in range(f):
            k = self.knots[j]
            if len(k) < 2:  # noqa: PLR2004 - empty or constant on the training rows
                continue
            kn = np.asarray(k)
            u = np.interp(np.nan_to_num(x[:, j], nan=kn[0]), kn, np.linspace(0, 1, len(kn)))
            eps = 0.5 / len(kn)
            z = ndtri(np.clip(u, eps, 1.0 - eps))
            out[:, j] = np.clip(z, -CLIP, CLIP).astype(np.float32)
        out[miss] = 0.0
        return out, miss.astype(np.float32)


class TabularInputs(BaseModel):
    """What a fitted challenger needs to turn frame rows into tensors."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    numeric: list[str]
    categorical: list[str]
    vocab: dict[str, list[str]]
    normalizer: QuantileNormalizer

    @property
    def cardinalities(self) -> list[int]:
        """Embedding sizes: vocab + 1 (index 0 = missing / unseen)."""
        return [len(self.vocab.get(c.removeprefix("cat_"), [])) + 1 for c in self.categorical]


def split_features(features: Sequence[str]) -> tuple[list[str], list[str]]:
    num = [f for f in features if not f.startswith("cat_")]
    cat = [f for f in features if f.startswith("cat_")]
    return num, cat


def numeric_matrix(df: pd.DataFrame, numeric: Sequence[str]) -> np.ndarray:
    return df[list(numeric)].apply(pd.to_numeric, errors="coerce").to_numpy(dtype=np.float64)


def category_codes(df: pd.DataFrame, categorical: Sequence[str], vocab: dict[str, list[str]]):
    """``(n, n_cat)`` int64 codes, 0 = missing / unseen (shifted from the M2 encoding)."""
    enc = ds.encode_categories(df, vocab)
    out = np.zeros((len(df), len(categorical)), dtype=np.int64)
    for j, c in enumerate(categorical):
        v = enc[c].to_numpy(dtype=float)
        out[:, j] = np.where(np.isfinite(v), v + 1, 0).astype(np.int64)
    return out


def fit_inputs(
    df: pd.DataFrame,
    train: np.ndarray,
    features: Sequence[str],
    vocab: dict[str, list[str]],
    bins: int = 1000,
) -> TabularInputs:
    num, cat = split_features(features)
    norm = QuantileNormalizer.fit(numeric_matrix(df[train], num), num, bins=bins)
    return TabularInputs(numeric=num, categorical=cat, vocab=vocab, normalizer=norm)


def transform(inputs: TabularInputs, df: pd.DataFrame) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(numeric z float32, missing flags float32, category codes int64)."""
    z, miss = inputs.normalizer.transform(numeric_matrix(df, inputs.numeric))
    return z, miss, category_codes(df, inputs.categorical, inputs.vocab)
