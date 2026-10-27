"""A pure-numpy evaluator for LightGBM text model files (E27.4, D126).

The live loop scores models without the ``ml`` extra (``lightgbm`` + OpenMP are a
training dependency only, OPS § E27.3): :class:`TextBooster` reads the ``model.txt``
format LightGBM writes (``Booster.save_model``) and reproduces ``Booster.predict`` for
the parts our models use: numerical and categorical splits, the missing-value rules
(``decision_type`` bits: categorical, default-left, missing None / Zero / NaN), and the
``binary`` sigmoid output transform. Linear trees and multiclass models are refused.

Exactness is tested against ``lightgbm`` on the registered bundles
(``tests/test_ml_lgbm_text.py``, skipped where ``lightgbm`` can't load).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from collections.abc import Iterator

_CAT_MASK = 1
_DEFAULT_LEFT_MASK = 2
_MISSING_ZERO = 1
_MISSING_NAN = 2
_ZERO_THRESHOLD = 1e-35


class LgbmTextError(ValueError):
    """The model text is malformed or uses a feature this evaluator doesn't support."""


@dataclass(frozen=True)
class _Tree:
    leaf_value: np.ndarray
    split_feature: np.ndarray
    threshold: np.ndarray
    decision_type: np.ndarray
    left: np.ndarray
    right: np.ndarray
    cat_boundaries: np.ndarray
    cat_threshold: np.ndarray

    def predict(self, x: np.ndarray) -> np.ndarray:
        n = x.shape[0]
        if self.split_feature.size == 0:  # a single-leaf tree
            return np.full(n, float(self.leaf_value[0]))
        node = np.zeros(n, dtype=np.int64)
        active = np.ones(n, dtype=bool)
        while active.any():
            idx = np.nonzero(active)[0]
            nd = node[idx]
            fval = x[idx, self.split_feature[nd]]
            dtype = self.decision_type[nd]
            go_left = np.zeros(idx.size, dtype=bool)
            cat = (dtype & _CAT_MASK) != 0
            if (~cat).any():
                num = ~cat
                go_left[num] = self._numerical(fval[num], nd[num], dtype[num])
            if cat.any():
                go_left[cat] = self._categorical(fval[cat], nd[cat])
            nxt = np.where(go_left, self.left[nd], self.right[nd])
            node[idx] = nxt
            active[idx] = nxt >= 0
        return self.leaf_value[~node]

    def _numerical(self, fval: np.ndarray, nd: np.ndarray, dtype: np.ndarray) -> np.ndarray:
        missing = (dtype >> 2) & 3
        default_left = (dtype & _DEFAULT_LEFT_MASK) != 0
        isnan = np.isnan(fval)
        v = np.where(isnan & (missing != _MISSING_NAN), 0.0, fval)
        is_missing = ((missing == _MISSING_ZERO) & (np.abs(v) <= _ZERO_THRESHOLD)) | (
            (missing == _MISSING_NAN) & np.isnan(v)
        )
        with np.errstate(invalid="ignore"):
            le = v <= self.threshold[nd]
        return np.where(is_missing, default_left, le)

    def _categorical(self, fval: np.ndarray, nd: np.ndarray) -> np.ndarray:
        out = np.zeros(fval.size, dtype=bool)
        for i, (f, k) in enumerate(zip(fval, nd, strict=True)):
            if math.isnan(f):
                continue
            iv = int(f)
            if iv < 0:
                continue
            ci = int(self.threshold[k])
            lo, hi = int(self.cat_boundaries[ci]), int(self.cat_boundaries[ci + 1])
            word = iv // 32
            if word >= hi - lo:
                continue
            out[i] = bool((int(self.cat_threshold[lo + word]) >> (iv % 32)) & 1)
        return out


def _blocks(text: str) -> Iterator[dict[str, str]]:
    cur: dict[str, str] | None = None
    for line in text.splitlines():
        if line.startswith("Tree="):
            if cur is not None:
                yield cur
            cur = {}
            continue
        if line.startswith("end of trees"):
            break
        if cur is not None and "=" in line:
            k, _, v = line.partition("=")
            cur[k] = v
    if cur is not None:
        yield cur


def _arr(block: dict[str, str], key: str, dtype: type) -> np.ndarray:
    raw = block.get(key, "").strip()
    return np.array(raw.split(), dtype=dtype) if raw else np.array([], dtype=dtype)


class TextBooster:
    """A LightGBM model from its text dump; :meth:`predict` matches ``Booster.predict``."""

    def __init__(self, text: str) -> None:
        header: dict[str, str] = {}
        for line in text.splitlines():
            if line.startswith("Tree="):
                break
            if "=" in line:
                k, _, v = line.partition("=")
                header[k] = v
        if header.get("num_class", "1") != "1" or header.get("num_tree_per_iteration", "1") != "1":
            msg = "multiclass LightGBM models are not supported"
            raise LgbmTextError(msg)
        self.objective = header.get("objective", "")
        self.feature_names = header.get("feature_names", "").split()
        self.n_features = int(header.get("max_feature_idx", "-1")) + 1
        self.trees: list[_Tree] = []
        for b in _blocks(text):
            if b.get("is_linear", "0") != "0":
                msg = "linear trees are not supported"
                raise LgbmTextError(msg)
            self.trees.append(
                _Tree(
                    leaf_value=_arr(b, "leaf_value", float),
                    split_feature=_arr(b, "split_feature", np.int64),
                    threshold=_arr(b, "threshold", float),
                    decision_type=_arr(b, "decision_type", np.int64),
                    left=_arr(b, "left_child", np.int64),
                    right=_arr(b, "right_child", np.int64),
                    cat_boundaries=_arr(b, "cat_boundaries", np.int64),
                    cat_threshold=_arr(b, "cat_threshold", np.int64),
                )
            )
        if not self.trees:
            msg = "no trees in the model text"
            raise LgbmTextError(msg)
        self._sigmoid: float | None = None
        obj = self.objective.split()
        if obj and obj[0] == "binary":
            sig = next((o.split(":", 1)[1] for o in obj[1:] if o.startswith("sigmoid:")), "1")
            self._sigmoid = float(sig)
        elif obj and obj[0] not in ("regression", "regression_l1", "huber", "quantile", "fair"):
            msg = f"objective {obj[0]!r} is not supported"
            raise LgbmTextError(msg)

    @classmethod
    def from_file(cls, path: Path | str) -> TextBooster:
        return cls(Path(path).read_text())

    def num_trees(self) -> int:
        return len(self.trees)

    def predict(self, x: np.ndarray) -> np.ndarray:
        """Scores for the rows of *x* (``n × n_features``, NaN = missing)."""
        x = np.asarray(x, dtype=np.float64)
        if x.ndim != 2 or x.shape[1] != self.n_features:  # noqa: PLR2004
            msg = f"expected {self.n_features} feature columns, got {x.shape}"
            raise LgbmTextError(msg)
        raw = np.zeros(x.shape[0], dtype=np.float64)
        for t in self.trees:
            raw += t.predict(x)
        if self._sigmoid is not None:
            return 1.0 / (1.0 + np.exp(-self._sigmoid * raw))
        return raw
