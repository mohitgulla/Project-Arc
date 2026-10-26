"""E27.3 M2 trainer / scorer / replay-rule tests on a synthetic ledger + feature store."""

from __future__ import annotations

import datetime as dt
import json
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import numpy as np
import pandas as pd
import pytest

from arc.ml import registry as reg
from arc.ml.m2 import dataset as ds
from arc.ml.m2 import replay as rp
from arc.ml.m2.config import GridPoint, M2Config
from arc.utils.calendar import sessions_between

if TYPE_CHECKING:
    from pathlib import Path

pytest.importorskip("lightgbm")

NOW = dt.datetime(2026, 10, 26, 12, 0)
TICKERS = ("AAA", "BBB", "ETF1")
KINDS = ("bull_call", "bear_put", "long_call")


def _write_store(root: Path, *, per_day: int = 12, step: int = 3) -> tuple[Path, Path]:
    """Ledger v1 (cash_debit) + feature panels with a learnable signal."""
    from arc.ml.features.registry import schema_version
    from arc.ml.m1.train import model_features

    rng = np.random.default_rng(7)
    days = sessions_between(dt.date(2020, 1, 1), dt.date(2023, 12, 31))[::step]
    ledger = root / "ledger" / "v1"
    froot = root / "features"
    fdir = froot / f"v{schema_version()}"
    fdir.mkdir(parents=True)
    pcols = model_features()
    by_year: dict[tuple[int, str], list[dict]] = {}
    for d in days:
        panel = {"date": [d] * len(TICKERS), "ticker": list(TICKERS)}
        for c in pcols:
            panel[c] = rng.normal(size=len(TICKERS))
        pd.DataFrame(panel).to_parquet(fdir / f"date={d.isoformat()}.parquet", index=False)
        for t in TICKERS:
            for i in range(per_day):
                pop = float(rng.uniform(0.2, 0.8))
                win = rng.uniform() < pop
                r = (0.8 if win else -0.6) + 0.1 * rng.normal()
                end = d + dt.timedelta(days=int(rng.integers(5, 30)))
                ml = float(rng.uniform(100, 400))
                by_year.setdefault((d.year, t), []).append(
                    {
                        "date": d,
                        "ticker": t,
                        "key": f"{t}|{d}|{i}",
                        "kind": KINDS[i % 3],
                        "sleeve": "swing",
                        "direction": "bull" if i % 2 else "bear",
                        "credit": False,
                        "dte": 30.0,
                        "anchor_delta": 0.4,
                        "width_pct": 0.05,
                        "static_pop": pop,
                        "static_net_ev": (pop - 0.5) * ml,
                        "managed_pop": pop,
                        "managed_net_ev": (pop - 0.5) * ml,
                        "ev_proxy": (pop - 0.5) * ml,
                        "net_mid": ml / 100,
                        "max_loss_mid": ml,
                        "max_gain_mid": ml,
                        "est_cost": 4.0,
                        "expected_days_held": 10.0,
                        "be_atr": 0.5,
                        "be_sigma": 0.5,
                        "spread_to_premium": 0.05,
                        "leg_oi_min": 500.0,
                        "leg_volume_min": 50.0,
                        "shorts_sellable": 1.0,
                        "credit_width": np.nan,
                        "debit_width": 0.3,
                        "rorc_day": 0.01,
                        "vrp": 0.0,
                        "ctx_trend": "up",
                        "ctx_vol": "normal",
                        "ctx_filter_reason": "" if i % 4 else "anti_chase",
                        "ctx_passes_filters": bool(i % 4),
                        "ctx_credit_only": False,
                        "ctx_entry_spread_ok": True,
                        "ctx_earnings_blackout": False,
                        "ctx_regime_blocked": False,
                        "ctx_liquidity": 1.0,
                        "ctx_etf": t == "ETF1",
                        "label_win_x025": bool(win),
                        "label_r_net_x025": r,
                        "label_hit_stop": bool(not win and rng.uniform() < 0.5),
                        "label_days_held": float(rng.integers(1, 20)),
                        "label_exit_reason": "tp" if win else "stop",
                        "label_end_date": end,
                        "label_crosses_holdout": end >= dt.date(2024, 1, 1),
                        "n_concurrent": int(rng.integers(0, 5)),
                        "sample_weight": 1.0,
                        "spec": "{}",
                        "split": "dev",
                    }
                )
    for (y, t), rows in by_year.items():
        p = ledger / "profile=cash_debit" / f"year={y}"
        p.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(rows).to_parquet(p / f"part-{t}.parquet", index=False)
    return ledger, froot


def _cfg() -> M2Config:
    return M2Config(
        grid=[GridPoint(num_leaves=7, min_data_in_leaf=20)],
        max_rounds=40,
        early_stopping=5,
        cap_per_day=None,
        num_threads=2,
    )


@pytest.fixture(scope="module")
def trained(tmp_path_factory: pytest.TempPathFactory):
    from arc.ml.m2.train import train_family

    root = tmp_path_factory.mktemp("m2")
    ledger, froot = _write_store(root)
    runs = []
    for out in ("a", "b"):
        runs.append(
            train_family(
                "debit",
                _cfg(),
                ledger_root=ledger,
                features_root=froot,
                out_root=root / out,
                code_sha="test",
                now=NOW,
                monotone_check=False,
            )
        )
    return root, ledger, froot, runs


def test_train_writes_a_versioned_reproducible_model(trained):
    root, _, _, (a, b) = trained
    assert a.version == b.version
    files = json.loads((a.vdir / "manifest.json").read_text())["files"]
    assert any(f.startswith("lgbm/") for f in files)
    for f in files:  # byte-identical on rerun
        assert (a.vdir / f).read_bytes() == (b.vdir / f).read_bytes(), f
    for name in ("metrics", "checks", "reliability", "configs_tried", "gain"):
        assert (a.vdir / f"eval_{name}.csv").is_file()
    assert set(json.loads((a.vdir / "fold_taus.json").read_text())) == {"test2022", "test2023"}
    reg.register(root / "a", "debit", a.version, now=NOW, rule="m2_promote", rule_passed=False)
    assert set(reg.verify(root / "a", "debit", a.version)) == set(files)


def test_scores_are_calibrated_sorted_and_reload_identically(trained):
    from arc.ml.m2.predict import OUTPUTS, load_dir
    from arc.ml.m2.train import build_frame

    _, ledger, froot, (a, _) = trained
    m = load_dir(a.vdir)
    fr = build_frame(_cfg(), "debit", ledger, froot, m.manifest["feature_schema"])
    d = fr.df[pd.to_datetime(fr.df["date"]).dt.year == 2023]  # noqa: PLR2004
    s = m.score(d)
    assert list(s.columns[: len(OUTPUTS)]) == list(OUTPUTS)
    assert ((s.p_win > 0) & (s.p_win < 1)).all()
    assert ((s.p_stop > 0) & (s.p_stop < 1)).all()
    assert (s.r_q10 <= s.r_q50).all() and (s.r_q50 <= s.r_q90).all()
    assert (s.e_days >= 0).all()
    # signal: p_win tracks managed_pop, the label's true win probability
    assert np.corrcoef(s.p_win, d["managed_pop"].to_numpy(dtype=float))[0, 1] > 0.5  # noqa: PLR2004
    again = load_dir(a.vdir).score(d)
    pd.testing.assert_frame_equal(s, again)
    oos = pd.read_parquet(a.vdir / "oos_scores.parquet")
    assert set(oos["fold"]) == {"test2022", "test2023"}
    assert oos["date"].min() >= dt.date(2022, 1, 1)


def test_holdout_rows_are_never_in_dev_training(trained):
    _, ledger, _, _ = trained
    df = ds.load_family(ledger, "cash_debit", [2023], "x025", cap_per_day=None)
    assert not df["label_crosses_holdout"].any()
    scoring = ds.load_family(
        ledger, "cash_debit", [2023], "x025", cap_per_day=None, for_scoring=True
    )
    assert scoring["label_crosses_holdout"].any()
    assert len(scoring) > len(df)


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


# ---------------------------------------------------------------------------
# Replay overrides and the promote rule
# ---------------------------------------------------------------------------


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
