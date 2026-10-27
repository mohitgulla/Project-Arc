"""E27.3 harness / calibration / registry / config tests (no lightgbm needed)."""

from __future__ import annotations

import datetime as dt
import json
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import numpy as np
import pandas as pd
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from arc.ml import registry as reg
from arc.ml import validate as v
from arc.ml.calibrate import FamilyCalibrator, Isotonic, fit_family, fit_isotonic
from arc.ml.m2 import dataset as ds
from arc.ml.m2 import replay as rp
from arc.ml.m2.config import RULE_KIND, load_config

if TYPE_CHECKING:
    from pathlib import Path

NOW = dt.datetime(2026, 10, 26, 12, 0)
OWNER = "U_OWNER"


def _sessions(start: dt.date, end: dt.date) -> list[dt.date]:
    out, d = [], start
    while d <= end:
        if d.weekday() < 5:  # noqa: PLR2004
            out.append(d)
        d += dt.timedelta(days=1)
    return out


SESSIONS = _sessions(dt.date(2019, 6, 1), dt.date(2024, 6, 30))


# ---------------------------------------------------------------------------
# Splitter: purge + embargo
# ---------------------------------------------------------------------------


@settings(max_examples=150, deadline=None)
@given(
    rows=st.lists(
        st.tuples(st.integers(0, len(SESSIONS) - 80), st.integers(0, 70)),
        min_size=1,
        max_size=60,
    ),
    year=st.sampled_from([2021, 2022, 2023]),
    embargo=st.integers(0, 15),
)
def test_purged_train_rows_never_reach_the_test_block(rows, year, embargo):
    dates = [SESSIONS[i] for i, _ in rows]
    ends = [SESSIONS[i + h] for i, h in rows]
    df = pd.DataFrame({"date": dates, "label_end_date": ends})
    start = dt.date(year, 1, 1)
    ok = v.purged_before(df["date"], df["label_end_date"], SESSIONS, start, embargo=embargo)
    cut = v.embargo_start(SESSIONS, start, embargo)
    for keep, d, e in zip(ok, dates, ends, strict=True):
        if keep:
            assert d < start
            assert e < cut  # the label window ends before the embargo, so before the block
            assert e < start
        else:
            assert d >= start or e >= cut


def test_embargo_is_ten_sessions_before_the_block():
    start = dt.date(2022, 1, 3)
    cut = v.embargo_start(SESSIONS, start)
    i = SESSIONS.index(start)
    assert cut == SESSIONS[i - 10]
    assert v.EMBARGO == 10  # noqa: PLR2004


def test_walk_forward_folds_and_masks():
    folds = v.walk_forward()
    assert [f.name for f in folds] == ["test2022", "test2023"]
    df = pd.DataFrame(
        {
            "date": [dt.date(2021, 12, 1), dt.date(2021, 12, 30), dt.date(2022, 3, 1)],
            "label_end_date": [dt.date(2021, 12, 10), dt.date(2022, 1, 20), dt.date(2022, 4, 1)],
        }
    )
    tr = folds[0].train_mask(df, SESSIONS)
    te = folds[0].test_mask(df)
    assert tr.tolist() == [True, False, False]  # row 2's label crosses into 2022
    assert te.tolist() == [False, False, True]


def test_inner_split_uses_the_same_purge_and_never_shuffles():
    days = [d for d in SESSIONS if dt.date(2020, 1, 1) <= d <= dt.date(2021, 12, 31)]
    df = pd.DataFrame({"date": days, "label_end_date": [d + dt.timedelta(days=30) for d in days]})
    train = np.ones(len(df), dtype=bool)
    fit, val = v.inner_split(df, train, SESSIONS, val_start=dt.date(2021, 1, 1))
    d = pd.to_datetime(df["date"]).dt.date
    assert (d[val] >= dt.date(2021, 1, 1)).all()
    assert not (fit & val).any()
    cut = v.embargo_start(SESSIONS, dt.date(2021, 1, 1))
    assert (pd.to_datetime(df.loc[fit, "label_end_date"]).dt.date < cut).all()
    # time-ordered: every fit row precedes every val row
    assert d[fit].max() < d[val].min()


# ---------------------------------------------------------------------------
# Weights and sampling
# ---------------------------------------------------------------------------


def test_weights_uniqueness_and_per_day_normaliser():
    df = pd.DataFrame(
        {
            "date": [dt.date(2021, 1, 4)] * 3 + [dt.date(2021, 1, 5)],
            "n_concurrent": [0, 1, 3, 0],
            "sample_weight": [1.0, 1.0, 1.0, 1.0],
        }
    )
    w = v.sample_weights(df)
    # every day counts equally
    assert w[:3].sum() == pytest.approx(w[3:].sum())
    # within a day, weight ∝ 1 / (1 + n_concurrent)
    assert w[0] / w[1] == pytest.approx(2.0)
    assert w[0] / w[2] == pytest.approx(4.0)
    assert w.mean() == pytest.approx(1.0)


def test_weights_include_inverse_sampling():
    df = pd.DataFrame({"date": [dt.date(2021, 1, 4)] * 2, "n_concurrent": [0, 0]})
    w = v.sample_weights(df, np.array([1.0, 3.0]))
    assert w[1] / w[0] == pytest.approx(3.0)


def test_stratified_day_sample_is_deterministic_and_keeps_strata():
    n = 400
    df = pd.DataFrame(
        {
            "date": [dt.date(2021, 1, 4)] * n,
            "ticker": [f"T{i % 7}" for i in range(n)],
            "key": [f"k{i}" for i in range(n)],
            "kind": ["bull_call" if i % 4 else "long_put" for i in range(n)],
            "sleeve": ["short" if i % 2 else "long" for i in range(n)],
        }
    )
    u = v.row_uniform(df["date"], df["ticker"], df["key"])
    keep, inv = v.stratified_day_sample(df, 100, u)
    keep2, _ = v.stratified_day_sample(df, 100, v.row_uniform(df["date"], df["ticker"], df["key"]))
    assert (keep == keep2).all()
    assert 90 <= keep.sum() <= 110  # noqa: PLR2004
    for _, g in df.assign(k=keep, inv=inv).groupby(["kind", "sleeve"]):
        assert g.k.sum() >= 1
        # inverse weights restore the stratum's size
        assert g.loc[g.k, "inv"].sum() == pytest.approx(len(g))
    all_keep, all_inv = v.stratified_day_sample(df, None, u)
    assert all_keep.all() and (all_inv == 1).all()


def test_kish_n_eff():
    assert v.kish_n_eff(np.ones(10)) == pytest.approx(10.0)
    assert v.kish_n_eff(np.array([1.0, 0.0, 0.0])) == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def test_prob_metrics_on_known_values():
    y = np.array([1, 0, 1, 0], dtype=float)
    p = np.array([0.9, 0.1, 0.8, 0.3])
    m = v.prob_metrics(y, p)
    assert m["auc"] == pytest.approx(1.0)
    assert m["brier"] == pytest.approx(np.mean((p - y) ** 2))
    assert m["log_loss"] == pytest.approx(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))
    assert m["base_rate"] == pytest.approx(0.5)


def test_daily_ic_and_newey_west():
    rng = np.random.default_rng(0)
    dates = np.repeat(np.arange(60), 30)
    y = rng.normal(size=len(dates))
    ic = v.daily_ic(dates, y + 0.1 * rng.normal(size=len(y)), y)
    assert len(ic) == 60  # noqa: PLR2004
    assert ic.mean() > 0.9  # noqa: PLR2004
    assert v.newey_west_t(ic.to_numpy()) > 10  # noqa: PLR2004
    assert v.decile_spread(dates, y, y) > 0
    noise = v.daily_ic(dates, rng.normal(size=len(y)), y)
    assert abs(v.newey_west_t(noise.to_numpy())) < 4  # noqa: PLR2004


def test_split_masks_all_stocks_etfs():
    df = pd.DataFrame({"ctx_etf": [True, False, None]})
    m = v.split_masks(df)
    assert m["All"].tolist() == [True, True, True]
    assert m["ETFs"].tolist() == [True, False, False]
    assert m["Stocks"].tolist() == [False, True, True]


# ---------------------------------------------------------------------------
# Calibration
# ---------------------------------------------------------------------------


@settings(max_examples=60, deadline=None)
@given(st.integers(0, 10_000))
def test_isotonic_is_monotone_and_json_roundtrips(seed):
    rng = np.random.default_rng(seed)
    s = rng.normal(size=300)
    y = (rng.uniform(size=300) < 1 / (1 + np.exp(-2 * s))).astype(float)
    w = rng.uniform(0.2, 3.0, size=300)
    iso = fit_isotonic(s, y, w)
    grid = np.linspace(-4, 4, 200)
    p = iso.predict(grid)
    assert (np.diff(p) >= -1e-12).all()
    assert (p > 0).all() and (p < 1).all()
    again = Isotonic.model_validate_json(iso.model_dump_json())
    assert np.array_equal(again.predict(grid), p)


def test_isotonic_improves_a_miscalibrated_score():
    rng = np.random.default_rng(1)
    s = rng.uniform(size=5000)
    y = (rng.uniform(size=5000) < s**3).astype(float)  # score over-predicts
    iso = fit_isotonic(s, y)
    assert v.ece(y, iso.predict(s)) < v.ece(y, s) / 3


def test_per_kind_calibrator_only_when_kind_ece_is_twice_the_family():
    rng = np.random.default_rng(2)
    n = 6000
    s = rng.uniform(size=n)
    kinds = np.where(np.arange(n) % 2 == 0, "bull_call", "long_put")
    p_true = np.where(kinds == "bull_call", s, np.clip(s * 0.3, 0, 1))
    y = (rng.uniform(size=n) < p_true).astype(float)
    cal, rep = fit_family(s, y, None, kinds)
    assert set(cal.kinds) == {k for k, r in rep.items() if r["per_kind"] == 1.0}
    assert cal.kinds  # the two kinds disagree strongly: per-kind maps fitted
    # shrinkage keeps a per-kind prediction between the family and the kind map
    k = "long_put"
    fam_p = cal.family.predict(s[:5])
    kind_p = cal.kinds[k].predict(s[:5])
    out = cal.predict(s[:5], np.array([k] * 5))
    lo, hi = np.minimum(fam_p, kind_p), np.maximum(fam_p, kind_p)
    assert ((out >= lo - 1e-12) & (out <= hi + 1e-12)).all()
    same = rng.uniform(size=n)
    y2 = (rng.uniform(size=n) < same).astype(float)
    cal2, _ = fit_family(same, y2, None, kinds)
    assert cal2.kinds == {}
    assert FamilyCalibrator.model_validate_json(cal.model_dump_json()) == cal


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def _version(root: Path, family: str = "credit", version: str = "m2-aaaa") -> Path:
    vdir = root / family / version
    (vdir / "lgbm").mkdir(parents=True)
    (vdir / "lgbm" / "win.txt").write_text("tree\n")
    (vdir / "calibrators.json").write_text("{}\n")
    files = reg.hash_files(vdir, ["lgbm/win.txt", "calibrators.json"])
    (vdir / "manifest.json").write_text(json.dumps({"files": files}))
    return vdir


def test_registry_hash_verification(tmp_path):
    vdir = _version(tmp_path)
    assert set(reg.verify(tmp_path, "credit", "m2-aaaa")) == {"lgbm/win.txt", "calibrators.json"}
    (vdir / "lgbm" / "win.txt").write_text("tampered\n")
    with pytest.raises(reg.RegistryError, match="sha256"):
        reg.verify(tmp_path, "credit", "m2-aaaa")
    (vdir / "lgbm" / "win.txt").unlink()
    with pytest.raises(reg.RegistryError, match="missing"):
        reg.verify(tmp_path, "credit", "m2-aaaa")


def test_registry_lifecycle_and_owner_only_promotion(tmp_path):
    _version(tmp_path, version="m2-a")
    _version(tmp_path, version="m2-b")
    e = reg.register(tmp_path, "credit", "m2-a", now=NOW, rule=RULE_KIND, rule_passed=False)
    assert e.status == "candidate"
    with pytest.raises(reg.RegistryError, match="only a shadow"):
        reg.promote(tmp_path, "credit", "m2-a", actor=OWNER, owner=OWNER, now=NOW)
    e = reg.register(tmp_path, "credit", "m2-a", now=NOW, rule=RULE_KIND, rule_passed=True)
    assert e.status == "shadow"  # automatic on the rule
    with pytest.raises(reg.RegistryError, match="not the owner"):
        reg.promote(tmp_path, "credit", "m2-a", actor="U_SOMEONE", owner=OWNER, now=NOW)
    with pytest.raises(reg.RegistryError, match="no holdout read"):
        reg.promote(tmp_path, "credit", "m2-a", actor=OWNER, owner=OWNER, now=NOW)
    hk = {"now": NOW, "code_sha": "abc", "reason": "final"}
    reg.authorize_holdout_read(tmp_path, "credit", "m2-a", **hk)
    idx = reg.promote(tmp_path, "credit", "m2-a", actor=OWNER, owner=OWNER, now=NOW)
    assert idx.champion == "m2-a"
    reg.register(tmp_path, "credit", "m2-b", now=NOW, rule=RULE_KIND, rule_passed=True)
    reg.authorize_holdout_read(
        tmp_path, "credit", "m2-b", actor=OWNER, owner=OWNER, ack=reg.HOLDOUT_REREAD_ACK, **hk
    )
    idx = reg.promote(tmp_path, "credit", "m2-b", actor=OWNER, owner=OWNER, now=NOW, reason="r")
    assert idx.champion == "m2-b"
    assert idx.versions["m2-a"].status == "retired"
    # re-registering never demotes a champion
    reg.register(tmp_path, "credit", "m2-b", now=NOW, rule=RULE_KIND, rule_passed=False)
    assert reg.load_index(tmp_path, "credit").versions["m2-b"].status == "champion"
    log = [json.loads(x) for x in (tmp_path / "credit" / reg.PROMOTIONS).read_text().splitlines()]
    assert [r["new"] for r in log] == [
        "candidate",
        "shadow",
        "champion",
        "shadow",
        "retired",
        "champion",
    ]
    # models.json is plain JSON the Tower can read with the stdlib
    raw = json.loads((tmp_path / "credit" / reg.INDEX).read_text())
    assert raw["champion"] == "m2-b"


def test_holdout_read_once_then_owner_ack_only(tmp_path):
    _version(tmp_path)
    kw = {"now": NOW, "code_sha": "abc", "reason": "E27.3 final evaluation"}
    assert reg.authorize_holdout_read(tmp_path, "credit", "m2-aaaa", **kw) == 1
    with pytest.raises(reg.HoldoutReadRefusedError, match="re-read"):
        reg.authorize_holdout_read(tmp_path, "credit", "m2-aaaa", **kw)
    with pytest.raises(reg.HoldoutReadRefusedError):
        reg.authorize_holdout_read(
            tmp_path,
            "credit",
            "m2-aaaa",
            actor="U_X",
            owner=OWNER,
            ack=reg.HOLDOUT_REREAD_ACK,
            **kw,
        )
    with pytest.raises(reg.HoldoutReadRefusedError):
        reg.authorize_holdout_read(
            tmp_path, "credit", "m2-aaaa", actor=OWNER, owner=OWNER, ack="ok", **kw
        )
    n = reg.authorize_holdout_read(
        tmp_path, "credit", "m2-aaaa", actor=OWNER, owner=OWNER, ack=reg.HOLDOUT_REREAD_ACK, **kw
    )
    assert n == 2  # noqa: PLR2004
    assert len(reg.holdout_reads(tmp_path, "credit")) == 2  # noqa: PLR2004
    # the other family has its own budget
    assert reg.authorize_holdout_read(tmp_path, "debit", "m2-x", **kw) == 1
    with pytest.raises(reg.HoldoutReadRefusedError, match="reason"):
        reg.authorize_holdout_read(tmp_path, "debit", "m2-x", now=NOW, code_sha="a", reason="")


# ---------------------------------------------------------------------------
# Candidate features, replay overrides and the promote rule
# ---------------------------------------------------------------------------


def test_candidate_features_are_scale_free():
    d = pd.DataFrame(
        {c: [1.0, 1.0] for c in ds.LEDGER_FEATURE_COLS if c not in ("ctx_trend", "ctx_vol")}
    )
    d["ctx_trend"] = "up"
    d["ctx_vol"] = "normal"
    d["direction"] = "bull"
    d["ctx_filter_reason"] = ""
    d["static_net_ev"] = [10.0, 100.0]
    d["max_loss_mid"] = [100.0, 1000.0]  # 10x the size, same structure in R
    f = ds.candidate_features(d)
    assert f["static_ev_per_risk"].iloc[0] == pytest.approx(f["static_ev_per_risk"].iloc[1])
    assert "ticker" not in f.columns


def test_score_map_override_ranks_by_e_r_and_drops_unscored():
    rows = pd.DataFrame(
        {"date": [dt.date(2022, 1, 3)] * 2, "ticker": ["A", "A"], "key": ["k1", "k2"]}
    )
    s = pd.DataFrame({"e_r": [0.2, -0.1], "p_stop": [0.1, 0.9]})
    sm = rp.score_map(rows, s)

    def sc(key: str) -> Any:
        return SimpleNamespace(
            candidate=SimpleNamespace(
                day=dt.date(2022, 1, 3), underlying="A", inputs=SimpleNamespace(key=key)
            )
        )

    by_er = rp.Override("ml_er", sm, tau=0.5)
    k = (0, -1.0, 0, 0)
    a, b = by_er(sc("k1"), k), by_er(sc("k2"), k)
    assert a is not None and b is not None and a < b  # ascending sort: higher e_r first
    assert by_er(sc("missing"), k) is None
    assert (by_er.hit, by_er.miss) == (2, 1)
    pos = rp.Override("ml_er_pos", sm, tau=0.5)
    assert pos(sc("k2"), k) is None
    assert pos(sc("k1"), k) == k  # incumbent order kept
    assert pos(sc("missing"), k) == k  # unscored keeps the incumbent's decision
    pstop = rp.Override("ml_pstop", sm, tau=0.5)
    assert pstop(sc("k2"), k) is None
    assert pstop(sc("k1"), k) == k


def test_rule_needs_every_dev_year():
    good = {
        "fold": "test2022",
        "beats_logistic": True,
        "beats_managed_pop": True,
        "ic_pass": True,
    }
    rep = pd.DataFrame(
        [
            {"year": y, "replay_pass": True, "net_incumbent": 0.0, "net_ml_er": 1.0}
            | {"dd_incumbent": -10.0, "dd_ml_er": -5.0}
            for y in (2022, 2023)
        ]
    )
    checks = pd.DataFrame([good, dict(good, fold="test2023")])
    assert rp.rule_passed(rp.apply_rule(checks, rep))
    bad = rep.copy()
    bad.loc[1, "replay_pass"] = False
    assert not rp.rule_passed(rp.apply_rule(checks, bad))
    assert not rp.rule_passed(rp.apply_rule(checks.iloc[:1], rep))
    worse = checks.copy()
    worse.loc[0, "beats_managed_pop"] = False
    assert not rp.rule_passed(rp.apply_rule(worse, rep))


def test_replay_check_needs_net_and_drawdown():
    def rep(net_ml: float, dd_ml: float) -> pd.DataFrame:
        return pd.DataFrame(
            [
                {"family": "debit", "year": 2022, "use": "incumbent"}
                | {"net_pnl": 100.0, "max_dd_pct": -20.0},
                {"family": "debit", "year": 2022, "use": "ml_er"}
                | {"net_pnl": net_ml, "max_dd_pct": dd_ml},
            ]
        )

    assert rp.replay_checks(rep(150.0, -15.0)).replay_pass.iloc[0]
    assert not rp.replay_checks(rep(150.0, -25.0)).replay_pass.iloc[0]  # deeper DD
    assert not rp.replay_checks(rep(90.0, -15.0)).replay_pass.iloc[0]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_registry_list_verify_and_promote_refusals(tmp_path, monkeypatch, capsys):
    from arc.cli import main
    from arc.ml import cli as mlcli

    _version(tmp_path, version="m2-a")
    reg.register(tmp_path, "credit", "m2-a", now=NOW, rule=RULE_KIND, rule_passed=True)
    monkeypatch.setattr(mlcli, "_owner_id", lambda: OWNER)
    assert main(["ml", "registry", "list", "--out", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "credit: champion -" in out and "m2-a  shadow" in out
    assert main(["ml", "registry", "verify", "credit", "m2-a", "--out", str(tmp_path)]) == 0
    base = ["ml", "promote", "credit", "m2-a", "--out", str(tmp_path)]
    assert main([*base, "--actor", "U_X"]) == 2  # noqa: PLR2004
    assert "not the owner" in capsys.readouterr().err
    assert main([*base, "--actor", OWNER]) == 2  # noqa: PLR2004
    assert "no holdout read" in capsys.readouterr().err
    reg.authorize_holdout_read(tmp_path, "credit", "m2-a", now=NOW, code_sha="a", reason="r")
    assert main([*base, "--actor", OWNER]) == 0
    assert reg.load_index(tmp_path, "credit").champion == "m2-a"
    (tmp_path / "credit" / "m2-a" / "calibrators.json").write_text("{1}\n")
    assert main(["ml", "registry", "verify", "credit", "m2-a", "--out", str(tmp_path)]) == 1


# ---------------------------------------------------------------------------
# Config overlay (D133)
# ---------------------------------------------------------------------------


def test_overlay_merges_and_refuses_unknown_keys(tmp_path):
    base = load_config(None)
    assert len(base.grid) == 6  # noqa: PLR2004 - leaves {15,31,63} x min_data {200,1000}
    assert base.learning_rate == pytest.approx(0.03)
    p = tmp_path / "o.yaml"
    p.write_text(
        "decision_rule: m2_promote\nlearning_rate: 0.05\ncalibration:\n  shrink_k: 500\n"
        "drop_features: [vrp]\ngrid:\n  - {num_leaves: 7, min_data_in_leaf: 50}\n"
    )
    c = load_config(p)
    assert c.learning_rate == pytest.approx(0.05)
    assert c.calibration.shrink_k == pytest.approx(500)
    assert c.calibration.kind_ece_ratio == pytest.approx(2.0)
    assert c.drop_features == ["vrp"]
    assert len(c.grid) == 1
    assert c.digest() != base.digest()
    p.write_text("leraning_rate: 0.05\n")
    with pytest.raises(ValueError, match="leraning_rate"):
        load_config(p)
    p.write_text("decision_rule: something_easier\n")
    with pytest.raises(ValueError, match="m2_promote"):
        load_config(p)
