"""E27.4: live shadow scoring bundle (:mod:`arc.ml.serve`) — verification, refusal,
fail-open. Fixture bundles are hand-built text models (no ``lightgbm`` needed)."""

from __future__ import annotations

import datetime as dt
import json
import math
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from arc.ml import serve
from arc.ml.registry import hash_files, register

if TYPE_CHECKING:
    from pathlib import Path

SCHEMA = "abc123abc123"
NOW = dt.datetime(2026, 10, 27, 10, tzinfo=ZoneInfo("America/New_York"))
_BOOSTERS = ("win", "stop", "r", "q10", "q50", "q90", "days", "r_win", "r_loss")


def _text(leaves: str, obj: str) -> str:
    return (
        "tree\nversion=v4\nnum_class=1\nnum_tree_per_iteration=1\nmax_feature_idx=0\n"
        f"objective={obj}\nfeature_names=a\n\nTree=0\nnum_leaves=2\nnum_cat=0\n"
        f"split_feature=0\nthreshold=0.5\ndecision_type=2\nleft_child=-1\nright_child=-2\n"
        f"leaf_value={leaves}\nis_linear=0\nshrinkage=1\n\nend of trees\n"
    )


def _bundle(root: Path, family: str = "credit", version: str = "m2-aaa", schema: str = SCHEMA):
    vdir = root / family / version
    (vdir / "lgbm").mkdir(parents=True)
    for name in _BOOSTERS:
        binary = name in ("win", "stop")
        leaves = "-1 1" if binary else ("0.1 -0.2" if name != "days" else "3 5")
        (vdir / "lgbm" / f"{name}.txt").write_text(
            _text(leaves, "binary sigmoid:1" if binary else "huber")
        )
    iso = {"x": [0.0, 1.0], "y": [0.2, 0.8], "n_eff": 100.0}
    (vdir / "calibrators.json").write_text(
        json.dumps({"win": {"family": iso}, "stop": {"family": iso}})
    )
    (vdir / "features.json").write_text(json.dumps(["a"]))
    (vdir / "vocab.json").write_text(json.dumps({}))
    names = ["calibrators.json", "features.json", "vocab.json"] + [
        f"lgbm/{b}.txt" for b in _BOOSTERS
    ]
    manifest = {"feature_schema": schema, "family": family, "files": hash_files(vdir, names)}
    (vdir / "manifest.json").write_text(json.dumps(manifest))
    register(
        root, family, version, now=NOW, rule="m2_promote", rule_passed=False, feature_schema=schema
    )
    return vdir


def _rows() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "key": ["bull_put|x", "bull_put|y"],
            "ticker": ["AAPL", "AAPL"],
            "kind": ["bull_put", "bull_put"],
            "sleeve": ["short", None],
            "a": [0.0, 1.0],
        }
    )


def test_scores_ok_with_versions_and_digest(tmp_path: Path) -> None:
    _bundle(tmp_path)
    b = serve.load_bundle(tmp_path, "credit", running_schema=SCHEMA)
    out = serve.score(_rows(), b)
    assert [s.status for s in out] == ["ok", "ok"]
    s0, s1 = out
    assert s0.model_version == "m2-aaa" and s0.feature_schema == SCHEMA and s0.family == "credit"
    assert s0.feature_digest and s0.feature_digest != s1.feature_digest
    assert s0.e_r == pytest.approx(0.1) and s1.e_r == pytest.approx(-0.2)
    assert s0.e_days == pytest.approx(3.0)
    assert s0.p_win is not None and 0 < s0.p_win < 1
    assert s1.sleeve is None
    assert serve.m2_ranks(out) == {"bull_put|x": 1, "bull_put|y": 2}


def test_refuses_changed_file(tmp_path: Path) -> None:
    vdir = _bundle(tmp_path)
    (vdir / "lgbm" / "win.txt").write_text(_text("-5 5", "binary sigmoid:1"))
    got = serve.load_bundle_or_reason(tmp_path, "credit", running_schema=SCHEMA)
    assert isinstance(got, str) and "sha256" in got
    out = serve.score(_rows(), got)
    assert {s.status for s in out} == {"unavailable"} and all(
        "sha256" in (s.reason or "") for s in out
    )


def test_refuses_schema_mismatch(tmp_path: Path) -> None:
    _bundle(tmp_path, schema="000000000000")
    got = serve.load_bundle_or_reason(tmp_path, "credit", running_schema=SCHEMA)
    assert isinstance(got, str) and "feature schema" in got


def test_missing_bundle_is_unavailable(tmp_path: Path) -> None:
    got = serve.load_bundle_or_reason(tmp_path, "credit", running_schema=SCHEMA)
    assert isinstance(got, str)
    assert serve.score(_rows(), got)[0].status == "unavailable"
    assert serve.score(_rows().iloc[:0], got) == []


def test_bad_rows_fail_open(tmp_path: Path) -> None:
    _bundle(tmp_path)
    b = serve.load_bundle(tmp_path, "credit", running_schema=SCHEMA)
    out = serve.score(_rows().drop(columns=["a"]), b)
    assert {s.status for s in out} == {"unavailable"}
    assert out[0].reason and out[0].reason.startswith("score error")


def test_nan_features_score_and_are_counted(tmp_path: Path) -> None:
    _bundle(tmp_path)
    b = serve.load_bundle(tmp_path, "credit", running_schema=SCHEMA)
    rows = _rows()
    rows.loc[0, "a"] = math.nan
    out = serve.score(rows, b)
    assert out[0].status == "ok" and out[0].missing_features == 1


def test_family_for_profile() -> None:
    assert serve.family_for_profile("margin") == "credit"
    assert serve.family_for_profile("cash_debit") == "debit"
