"""E27.2 M1: targets, conformal, metrics, baselines, predict purity, MC hook."""

from __future__ import annotations

import ast
import datetime as dt
import math
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from arc.ml.features.compute import M1_QUANTILES, qtag
from arc.ml.m1 import conformal, metrics
from arc.ml.m1.baselines import HarIv, iv_lognormal_quantiles
from arc.ml.m1.predict import (
    M1Manifest,
    M1Model,
    TargetModel,
    direction,
    fit_nu,
    horizon_for_dte,
    m1_trend_labels,
    p_up_and_abs,
    path_params,
    publish,
    quantile_grid,
)
from arc.ml.m1.targets import ticker_targets
from arc.ml.m1.train import FOLDS, cutoff, purged_outside, session_index

LV = list(M1_QUANTILES)
REPO = Path(__file__).resolve().parents[1]


def _px(n: int = 120, seed: int = 3) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    days = pd.bdate_range("2021-01-04", periods=n).date
    c = 100 * np.exp(np.cumsum(rng.normal(0, 0.015, n)))
    return pd.DataFrame(
        {"close": c, "high": c * 1.01, "low": c * 0.99}, index=pd.Index(days, name="date")
    )


# ---------------------------------------------------------------------------
# Targets
# ---------------------------------------------------------------------------


def test_targets_values() -> None:
    px = _px()
    sig = pd.Series(0.3, index=px.index)
    t = ticker_targets(px, sigma_ref=sig, iv30=sig, horizons=(5,))
    c = px["close"].to_numpy()
    assert t["log_ret_h5"].iloc[10] == pytest.approx(math.log(c[15] / c[10]))
    r = np.diff(np.log(c))
    assert t["rv_h5"].iloc[10] == pytest.approx(math.sqrt(252 / 5 * np.sum(r[10:15] ** 2)))
    up = np.max(np.log(px["high"].to_numpy()[11:16] / c[10]))
    assert t["max_up_h5"].iloc[10] == pytest.approx(up / (0.3 * math.sqrt(5 / 252)))
    mvi = abs(math.log(c[15] / c[10])) / (0.3 * math.sqrt(5 / 252))
    assert t["move_vs_implied_h5"].iloc[10] == pytest.approx(mvi)
    assert t["end_h5"].iloc[10] == px.index[15]
    assert t["log_ret_h5"].iloc[-5:].isna().all()  # window runs past the data
    assert t["log_ret_h5"].iloc[:-5].notna().all()


def test_targets_no_lookahead_mutation() -> None:
    """Mutating sessions after t+h never changes the label at t; mutating t+h does."""
    px = _px()
    base = ticker_targets(px, horizons=(10,))
    t = 40
    mut = px.copy()
    mut.iloc[t + 11 :, :] *= 3.0
    after = ticker_targets(mut, horizons=(10,))
    cols = ["log_ret_h10", "rv_h10"]
    pd.testing.assert_frame_equal(base[cols].iloc[: t + 1], after[cols].iloc[: t + 1])
    mut2 = px.copy()
    mut2.iloc[t + 10, :] *= 1.5
    assert ticker_targets(mut2, horizons=(10,))["log_ret_h10"].iloc[t] != pytest.approx(
        base["log_ret_h10"].iloc[t]
    )
    # and the cut: truncating the series at t+h keeps the label, at t+h-1 drops it
    assert ticker_targets(px.iloc[: t + 11], horizons=(10,))["log_ret_h10"].iloc[t] == (
        pytest.approx(base["log_ret_h10"].iloc[t])
    )
    assert np.isnan(ticker_targets(px.iloc[: t + 10], horizons=(10,))["log_ret_h10"].iloc[t])


def test_targets_gap_in_window_is_nan() -> None:
    px = _px()
    px.iloc[50, px.columns.get_loc("close")] = np.nan
    t = ticker_targets(px, horizons=(5,))
    assert t["rv_h5"].iloc[45:50].isna().all()


# ---------------------------------------------------------------------------
# Folds: purge + embargo
# ---------------------------------------------------------------------------


def test_purge_and_embargo() -> None:
    s = list(pd.bdate_range("2021-01-01", "2023-12-31").date)
    plan = FOLDS[0]
    cut = plan.test_cut(s, 21)
    i_test = next(k for k, d in enumerate(s) if d >= plan.test_start)
    assert s.index(cut) == i_test - 21 - 5
    assert cutoff(s, dt.date(2021, 1, 1), 42) == s[0]
    # two-sided purge for cross-fitting
    df = pd.DataFrame({"date": s})
    si = session_index(df, s)
    keep = purged_outside(si, s, s[100], s[200], 10)
    assert not keep[85:215].any()
    assert keep[84] and keep[215]
    a, b = plan.cal_halves(s, 21)
    assert a[1] == b[0] and b[1] == cut


# ---------------------------------------------------------------------------
# Conformal
# ---------------------------------------------------------------------------


def test_cqr_coverage_on_synthetic_data() -> None:
    """A biased, too-narrow forecast hits nominal coverage per level after CQR."""
    rng = np.random.default_rng(11)
    n = 20_000
    sigma = rng.uniform(0.5, 2.0, 2 * n)
    y = rng.standard_t(5, 2 * n) * sigma
    z = np.array([-1.64, -1.28, -0.67, 0.0, 0.67, 1.28, 1.64])
    q = 0.3 + 0.6 * sigma[:, None] * z[None, :]  # biased and 40% too narrow
    offs = conformal.fit_offsets(q[:n], y[:n], LV)
    cal = conformal.apply_offsets(q[n:], offs, LV)
    raw_cov = metrics.coverage(y[n:], q[n:])
    cov = metrics.coverage(y[n:], cal)
    assert np.max(np.abs(raw_cov - LV)) > 0.05
    assert np.max(np.abs(cov - LV)) < 0.015


def test_conformal_level_and_empty() -> None:
    assert conformal.conformal_level(0.9, 9) == 1.0
    assert conformal.conformal_level(0.1, 9) == pytest.approx(1 / 9)
    assert conformal.conformal_level(0.5, 0) == 0.5
    offs = conformal.fit_offsets(np.full((3, 7), np.nan), np.ones(3), LV)
    assert set(offs.values()) == {0.0}


@settings(max_examples=50, deadline=None)
@given(st.lists(st.floats(-1, 1, allow_nan=False), min_size=7, max_size=7), st.integers(0, 5))
def test_quantiles_monotone_after_sorting(vals: list[float], seed: int) -> None:
    q = np.array([vals, vals[::-1]])
    srt, rate = conformal.sort_quantiles(q)
    assert (np.diff(srt, axis=1) >= 0).all()
    assert 0.0 <= rate <= 1.0
    offs = {p: float(np.random.default_rng(seed).normal()) for p in LV}
    assert (np.diff(conformal.apply_offsets(q, offs, LV), axis=1) >= 0).all()


# ---------------------------------------------------------------------------
# Metrics and baselines
# ---------------------------------------------------------------------------


def test_pinball_and_dm() -> None:
    pb = metrics.pinball(np.array([1.0]), np.array([[0.0, 2.0]]), [0.1, 0.9])
    assert pb.tolist() == [[pytest.approx(0.1), pytest.approx(0.1)]]
    rng = np.random.default_rng(1)
    better = rng.normal(-0.5, 1, 400)
    stat, p = metrics.dm_test(better, 5)
    assert stat < 0 and p < 0.01
    assert math.isnan(metrics.dm_test(np.ones(3), 5)[0])
    assert math.isnan(metrics.dm_test(np.zeros(50), 5)[0])
    assert metrics.qlike(np.array([0.2]), np.array([0.2]))[0] == pytest.approx(0.0)


def test_iv_lognormal_and_har_iv() -> None:
    q = iv_lognormal_quantiles(np.array([0.2]), 21)
    s = 0.2 * math.sqrt(21 / 252)
    assert q[0, 3] == pytest.approx(-0.5 * s * s)
    assert q[0, 6] - q[0, 3] == pytest.approx(1.6449 * s, rel=1e-3)
    rng = np.random.default_rng(2)
    n = 5000
    har = rng.uniform(0.1, 0.6, n)
    iv = har * rng.uniform(0.9, 1.3, n)
    rv = np.exp(0.1 + 0.5 * np.log(har) + 0.5 * np.log(iv) + rng.normal(0, 0.2, n))
    df = pd.DataFrame({"rv_h5": rv, "har_rv_h5": har, "iv30": iv})
    m = HarIv.fit(df, 5)
    assert m.coef[1] == pytest.approx(0.5, abs=0.05)
    qq = m.quantiles(har[:10], np.r_[iv[:5], [np.nan] * 5])
    assert np.isfinite(qq).all() and (np.diff(qq, axis=1) > 0).all()


# ---------------------------------------------------------------------------
# Predict: quantile function, direction, purity
# ---------------------------------------------------------------------------


def _normal_q(mu: float, s: float) -> np.ndarray:
    z = np.array([-1.6449, -1.2816, -0.6745, 0.0, 0.6745, 1.2816, 1.6449])
    return (mu + s * z)[None, :]


def test_quantile_function_normal_and_fat() -> None:
    q = _normal_q(0.0, 0.05)
    assert fit_nu(q)[0] > 50  # a normal tail ratio → large ν
    u, g = quantile_grid(q)
    assert (np.diff(g, axis=1) >= 0).all()
    pu, ea = p_up_and_abs(q)
    assert pu[0] == pytest.approx(0.5, abs=0.01)
    assert ea[0] == pytest.approx(0.05 * math.sqrt(2 / math.pi), rel=0.03)
    fat = q.copy()
    fat[0, [0, 6]] *= 1.6
    assert fit_nu(fat)[0] < 10


def test_direction_rule() -> None:
    q = np.vstack([_normal_q(0.03, 0.05), _normal_q(-0.03, 0.05), _normal_q(0.005, 0.05)])
    assert direction(q).tolist() == [1.0, -1.0, 0.0]
    lab = m1_trend_labels(pd.Series([1.0, -1.0, 0.0, np.nan]))
    assert lab.tolist() == ["bull", "bear", "sideways", "unknown"]


def _baseline_model() -> M1Model:
    zero = {qtag(p): 0.0 for p in LV}
    har = HarIv(
        h=5, coef=(0.0, 1.0, 0.0), coef_no_iv=(0.0, 1.0),
        resid_q=tuple(np.linspace(-0.3, 0.3, 7)), resid_q_no_iv=tuple(np.linspace(-0.3, 0.3, 7)),
    )  # fmt: skip
    targets = []
    for h in (5, 10, 21, 42):
        hh = har.model_copy(update={"h": h})
        targets += [
            TargetModel(kind="log_ret", h=h, source="baseline", offsets=zero),
            TargetModel(kind="rv", h=h, source="baseline", offsets=zero, har_iv=hh),
        ]
    man = M1Manifest(
        version="t", feature_schema="x", features=[], targets=targets, lgb_params={},
        num_rounds=0, train_window={}, calibration_window={}, code_sha="", config_hashes={},
        data_digest="",
    )  # fmt: skip
    return M1Model(man, {})


def _feature_frame() -> pd.DataFrame:
    rows = {"date": [dt.date(2023, 3, 1)] * 3, "ticker": ["A", "B", "C"]}
    rows["iv30"] = [0.3, 0.25, np.nan]
    rows["rv20"] = [0.28, 0.2, 0.4]
    for h in (5, 10, 21, 42):
        rows[f"har_rv_h{h}"] = [0.27, 0.22, 0.35]
        s = 0.27 * math.sqrt(h / 252)
        z = [-1.6, -1.3, -0.7, 0.01, 0.7, 1.3, 1.7]
        for p, zz in zip(LV, z, strict=True):
            rows[f"hq_q{qtag(p)}_h{h}"] = [s * zz, s * zz, np.nan]
    return pd.DataFrame(rows)


def test_publish_is_pure_and_complete() -> None:
    model = _baseline_model()
    df = _feature_frame()
    before = df.copy(deep=True)
    out1 = publish(model, df)
    out2 = publish(model, df)
    pd.testing.assert_frame_equal(df, before)  # no mutation
    pd.testing.assert_frame_equal(out1, out2)  # deterministic
    for h in (5, 10, 21, 42):
        q = out1[[f"m1_q{qtag(p)}_h{h}" for p in LV]].to_numpy()[:2]
        assert (np.diff(q, axis=1) >= 0).all()
        assert out1[f"m1_rv_h{h}"].gt(0).all()
    assert out1["m1_move_vs_implied_h21"].isna().tolist() == [False, False, True]
    # row C has no HQ history and no IV: log-return quantiles fall back to a zero-drift
    # lognormal at the realised-vol scale (HAR → rv20), so they stay finite
    q_c = out1.loc[2, [f"m1_q{qtag(p)}_h21" for p in LV]].to_numpy(dtype=float)
    assert np.isfinite(q_c).all() and (np.diff(q_c) > 0).all()
    assert out1.loc[2, "m1_q50_h21"] < 0  # −½σ²τ
    assert set(out1["m1_direction"].dropna()) <= {-1.0, 0.0, 1.0}


def test_predict_module_has_no_io() -> None:
    """predict.py imports no I/O, clock or network modules and calls no file reads."""
    src = (REPO / "arc/ml/m1/predict.py").read_text()
    tree = ast.parse(src)
    mods = {
        (n.module or "") if isinstance(n, ast.ImportFrom) else a.name
        for n in ast.walk(tree)
        if isinstance(n, (ast.Import, ast.ImportFrom))
        for a in n.names
    }
    banned = {"os", "pathlib", "sqlite3", "requests", "httpx", "time", "socket", "lightgbm"}
    assert not {m.split(".")[0] for m in mods} & banned
    for word in ("open(", "read_parquet", "read_text", ".now(", "today("):
        assert word not in src


def test_gate_never_imports_m1() -> None:
    for p in (REPO / "arc/gate").rglob("*.py"):
        assert "arc.ml" not in p.read_text(), p


def test_path_params() -> None:
    df = _feature_frame()
    out = publish(_baseline_model(), df)
    pp = path_params(out.iloc[0], dte=30)
    assert pp.horizon == horizon_for_dte(30) == 21
    assert pp.sigma == pytest.approx(out.loc[0, "m1_rv_h21"])
    med = out.loc[0, "m1_q50_h21"]
    assert pp.drift == pytest.approx(med / (21 / 252) + 0.5 * pp.sigma**2)
    assert pp.nu > 2
    assert horizon_for_dte(7) == 5 and horizon_for_dte(60) == 42


# ---------------------------------------------------------------------------
# MC hook (arc/exits/model.py)
# ---------------------------------------------------------------------------


def _mc(**kw: object) -> dict:
    from tests import exits_v1_golden as golden

    st = golden.structures()["bull_put"]
    res = golden.model_exits(
        st, golden.V1_CREDIT, spot=100.0, iv=0.20, r=0.04, cost=golden.COST,
        realized_vol=0.16, **kw,
    )  # fmt: skip
    return res.model_dump(mode="json")


def test_mc_hook_default_path_byte_identical() -> None:
    """Default knobs: passing M1 params changes nothing; the v1 golden still holds
    (tests/test_exits_v2.py::test_rollback_values_reproduce_main_exactly)."""
    from arc.exits import ExitModelConfig
    from arc.exits.model import M1PathParams
    from tests import exits_v1_golden as golden

    pp = M1PathParams(sigma=0.3, drift=0.5, nu=4.0)
    cfg = golden.CFG
    assert cfg.path_vol == "realized_forecast" and cfg.drift_source == "none"
    assert _mc(cfg=cfg) == _mc(cfg=cfg, m1=pp)
    # the new knob is omitted from dumps at its default (config hashes unchanged)
    assert "drift_source" not in ExitModelConfig().model_dump()
    assert ExitModelConfig(drift_source="m1_median").model_dump()["drift_source"] == "m1_median"


def test_mc_hook_m1_source() -> None:
    from arc.exits.model import M1PathParams
    from tests import exits_v1_golden as golden

    cfg = golden.CFG.model_copy(update={"path_vol": "m1"})
    pp = M1PathParams(sigma=0.35, drift=0.0, nu=4.0)
    no_m1 = _mc(cfg=cfg)
    assert no_m1 == _mc(cfg=golden.CFG)  # without params: falls back to realized_forecast
    got = _mc(cfg=cfg, m1=pp)
    assert got["path_vol_source"] == "m1"
    assert got["path_vol"] == pytest.approx(0.35)
    assert got["managed"] != no_m1["managed"]
    drift_cfg = cfg.model_copy(update={"drift_source": "m1_median"})
    up = _mc(cfg=drift_cfg, m1=pp.__class__(sigma=0.35, drift=1.0, nu=4.0))
    assert up["static"]["pop"] > got["static"]["pop"]  # bull put likes upward drift


def test_simulate_student_t_shocks() -> None:
    from arc.exits.model import simulate
    from tests import exits_v1_golden as golden

    with pytest.raises(ValueError, match="shock_nu"):
        simulate(
            [], None, spot=100.0, iv=0.2, r=0.0, dte=5, cost=golden.COST, cfg=golden.CFG,
            shock_nu=2.0,
        )  # type: ignore[arg-type]  # fmt: skip


def test_targets_short_series() -> None:
    t = ticker_targets(_px(n=8), horizons=(5, 21))
    assert t["log_ret_h21"].isna().all() and t["log_ret_h5"].notna().sum() == 3
    assert ticker_targets(_px(n=1), horizons=(5,))["rv_h5"].isna().all()


def _synthetic_panel(n_days: int = 160, n_tick: int = 12) -> pd.DataFrame:
    rng = np.random.default_rng(5)
    days = [d for d in pd.bdate_range("2020-07-01", periods=n_days).date]
    recs = []
    for k in range(n_tick):
        for d in days:
            recs.append({"date": d, "ticker": f"T{k}", "is_etf": k % 4 == 0, "pit_member": True})
    df = pd.DataFrame(recs)
    n = len(df)
    df["iv30"] = rng.uniform(0.15, 0.6, n)
    df["rv20"] = df["iv30"] * rng.uniform(0.7, 1.1, n)
    df["f_mom"] = rng.normal(size=n)
    df["har_rv_h5"] = df["rv20"]
    df["log_ret_h5"] = 0.002 * df["f_mom"] + rng.normal(0, 1, n) * df["rv20"] * math.sqrt(5 / 252)
    df["rv_h5"] = df["rv20"] * np.exp(rng.normal(0, 0.2, n))
    return df


def test_model_artefacts_roundtrip_and_byte_identical(tmp_path: Path) -> None:
    pytest.importorskip("lightgbm")
    from arc.ml.m1 import train

    df = _synthetic_panel()
    feats = ["f_mom", "iv30", "rv20", "har_rv_h5"]
    params = {**train.LGB_PARAMS, "min_data_in_leaf": 50}

    def build(out: Path) -> tuple[train.M1Manifest, train.FitSet]:
        fs = train.fit_set(df, lambda _h: np.ones(len(df), bool), feats, horizons=(5,),
                           params=params, rounds=20)  # fmt: skip
        zero = {qtag(p): 0.01 for p in LV}
        targets = [
            TargetModel(kind="log_ret", h=5, source="lgbm", offsets=zero),
            TargetModel(kind="rv", h=5, source="lgbm", offsets=zero, har_iv=fs.har_iv[5]),
        ]
        man = train.write_model(
            out, fs, targets, features=feats, digest=train.data_digest(df, feats),
            params=params, rounds=20, code_sha="x", feature_manifest_sha="y",
            train_window={}, calibration_window={},
        )  # fmt: skip
        return man, fs

    m1, _ = build(tmp_path / "a")
    m2, _ = build(tmp_path / "b")
    a, b = tmp_path / "a" / m1.version, tmp_path / "b" / m2.version
    assert m1.version == m2.version
    for f in sorted(p.relative_to(a) for p in a.rglob("*") if p.is_file()):
        assert (a / f).read_bytes() == (b / f).read_bytes(), f
    model = train.load_model(a, check_schema=False)
    q = __import__("arc.ml.m1.predict", fromlist=["x"]).predict_quantiles(model, df.head(50))
    assert (np.diff(q[("log_ret", 5)], axis=1) >= 0).all()
    assert (q[("rv", 5)] > 0).all()
    # a tampered dump is refused
    dump = next((a / "lgbm").glob("*.txt"))
    dump.write_text(dump.read_text() + " ")
    with pytest.raises(ValueError, match="sha256"):
        train.load_model(a, check_schema=False)
