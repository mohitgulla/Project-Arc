"""E27.4: the pure-numpy LightGBM text evaluator (:mod:`arc.ml.lgbm_text`).

Hand-built model texts pin the split/missing/categorical rules without ``lightgbm``;
the parity test trains real boosters and is skipped where ``lightgbm`` can't load
(CI and production have no ``ml`` extra / OpenMP).
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from arc.ml.lgbm_text import LgbmTextError, TextBooster

_HEAD = (
    "tree\nversion=v4\nnum_class=1\nnum_tree_per_iteration=1\nlabel_index=0\n"
    "max_feature_idx=1\nobjective={obj}\nfeature_names=a b\n\n"
)


def _tree(i: int, **kw: str) -> str:
    base = {"num_leaves": "2", "num_cat": "0", "is_linear": "0", "shrinkage": "1"}
    base.update(kw)
    return f"Tree={i}\n" + "".join(f"{k}={v}\n" for k, v in base.items()) + "\n"


def _model(trees: list[str], obj: str = "regression") -> str:
    return _HEAD.format(obj=obj) + "".join(trees) + "end of trees\n"


_NUM_TREES = [
    # f0 <= 0.5 → 1 else 2; missing None (NaN → 0.0), default-left bit set
    _tree(
        0,
        split_feature="0",
        threshold="0.5",
        decision_type="2",
        left_child="-1",
        right_child="-2",
        leaf_value="1 2",
    ),
    # f1 <= 1.0 → 10 else 20; missing NaN (bits 2-3 = 2), default right
    _tree(
        1,
        split_feature="1",
        threshold="1.0",
        decision_type="8",
        left_child="-1",
        right_child="-2",
        leaf_value="10 20",
    ),
]


def test_numerical_splits_and_missing_rules() -> None:
    b = TextBooster(_model(_NUM_TREES))
    assert b.num_trees() == 2
    assert b.feature_names == ["a", "b"]
    x = np.array([[0.0, 0.0], [1.0, 2.0], [math.nan, math.nan], [0.5, 1.0]])
    np.testing.assert_allclose(b.predict(x), [11.0, 22.0, 21.0, 11.0])


def test_missing_zero_goes_default_direction() -> None:
    # missing Zero (bits 2-3 = 1) with default-left: 0.0 and NaN both go left
    t = _tree(
        0,
        split_feature="0",
        threshold="-1.0",
        decision_type=str((1 << 2) | 2),
        left_child="-1",
        right_child="-2",
        leaf_value="5 7",
    )
    b = TextBooster(_model([t]))
    x = np.array([[0.0, 0.0], [math.nan, 0.0], [3.0, 0.0], [-2.0, 0.0]])
    np.testing.assert_allclose(b.predict(x), [5.0, 5.0, 7.0, 5.0])


def test_categorical_split() -> None:
    # categories {0, 2} go left (bitset 0b101); 1, unseen, negative and NaN go right
    t = _tree(
        0,
        num_cat="1",
        split_feature="0",
        threshold="0",
        decision_type="1",
        left_child="-1",
        right_child="-2",
        leaf_value="-1 1",
        cat_boundaries="0 1",
        cat_threshold="5",
    )
    b = TextBooster(_model([t]))
    x = np.array([[0.0, 0], [1.0, 0], [2.0, 0], [40.0, 0], [-1.0, 0], [math.nan, 0]])
    np.testing.assert_allclose(b.predict(x), [-1, 1, -1, 1, 1, 1])


def test_binary_sigmoid_and_single_leaf_tree() -> None:
    t = _tree(0, num_leaves="1", leaf_value="0.4")
    b = TextBooster(_model([t, t], obj="binary sigmoid:2"))
    out = b.predict(np.zeros((3, 2)))
    np.testing.assert_allclose(out, 1.0 / (1.0 + np.exp(-2.0 * 0.8)))


@pytest.mark.parametrize(
    ("text", "match"),
    [
        (_model(_NUM_TREES).replace("num_class=1", "num_class=3"), "multiclass"),
        (_model([_tree(0, num_leaves="1", leaf_value="1", is_linear="1")]), "linear"),
        (_model(_NUM_TREES, obj="lambdarank"), "lambdarank"),
        (_HEAD.format(obj="regression") + "end of trees\n", "no trees"),
    ],
)
def test_refuses_unsupported_models(text: str, match: str) -> None:
    with pytest.raises(LgbmTextError, match=match):
        TextBooster(text)


def _lightgbm():  # noqa: ANN202
    try:
        import lightgbm  # noqa: PLC0415
    except (ImportError, OSError) as e:  # OSError: OpenMP missing (macOS without libomp)
        pytest.skip(f"lightgbm unavailable: {e}")
    return lightgbm


@pytest.mark.parametrize(
    "params",
    [
        {"objective": "binary"},
        {"objective": "huber"},
        {"objective": "quantile", "alpha": 0.1},
        {"objective": "regression"},
    ],
)
def test_matches_lightgbm(params: dict[str, object], tmp_path) -> None:  # noqa: ANN001
    lgb = _lightgbm()
    rng = np.random.default_rng(7)
    n = 3000
    x = rng.normal(size=(n, 5))
    x[:, 3] = rng.integers(0, 6, size=n)  # categorical
    x[rng.random(n) < 0.1, 0] = np.nan
    x[rng.random(n) < 0.1, 1] = 0.0
    y = x[:, 2] + (x[:, 3] % 2) + np.nan_to_num(x[:, 0]) + rng.normal(scale=0.3, size=n)
    if params["objective"] == "binary":
        y = (y > 0).astype(float)
    ds = lgb.Dataset(x, label=y, categorical_feature=[3], free_raw_data=False)
    bst = lgb.train(
        {**params, "verbose": -1, "num_leaves": 15, "seed": 1, "deterministic": True},
        ds,
        num_boost_round=40,
    )
    path = tmp_path / "m.txt"
    bst.save_model(str(path))
    xt = rng.normal(size=(500, 5))
    xt[:, 3] = rng.integers(-1, 8, size=500)
    xt[rng.random(500) < 0.2, 0] = np.nan
    xt[rng.random(500) < 0.2, 3] = np.nan
    np.testing.assert_allclose(
        TextBooster.from_file(path).predict(xt), bst.predict(xt), rtol=0, atol=1e-12
    )
