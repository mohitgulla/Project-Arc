"""E27.5 (D126): ML arm options in the backtest (model_asof, ml_er / ml_filter /
ml_size, the m1_direction stance proxy, the m1 path source)."""

from __future__ import annotations

import ast
import datetime as dt
import json
import math
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pandas as pd
import pytest
from hypothesis import given
from hypothesis import strategies as st

from arc.backtest import ml_arms as ma
from arc.backtest.ml_config import MlFilter, MlSize
from arc.backtest.stance import proxy_labels
from arc.ml import registry as reg
from arc.ml import validate as v

REPO = Path(__file__).resolve().parents[1]


def _sessions(start: dt.date, end: dt.date) -> list[dt.date]:
    return [d.date() for d in pd.bdate_range(start, end)]


SESSIONS = _sessions(dt.date(2019, 6, 1), dt.date(2024, 6, 30))


def _version(root: Path, family: str, version: str, *, folds: bool = True) -> Path:
    vdir = root / family / version
    vdir.mkdir(parents=True)
    man: dict[str, Any] = {"train_window": {"start": "2020-01-01", "end": "2023-12-31"}}
    if folds:
        man["folds"] = ["test2022", "test2023"]
        for f in man["folds"]:
            (vdir / "folds" / f).mkdir(parents=True)
    (vdir / "manifest.json").write_text(json.dumps(man))
    return vdir


# ---------------------------------------------------------------------------
# model_asof: no leak
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("family", ["credit", "debit", "m1"])
def test_a_2022_session_never_gets_a_model_trained_on_2022_labels(tmp_path, family):
    _version(tmp_path, family, "v1")
    table = reg.asof_table(tmp_path, family, SESSIONS)
    for d in [s for s in SESSIONS if s.year == 2022]:
        m = reg.model_asof(tmp_path, family, d, table=table)
        assert m is not None and m.fold == "test2022"
        assert m.labels_before < dt.date(2022, 1, 1)  # every label ended in 2021
        assert m.labels_before <= d
    # the purge bound is the trainer's embargo before the block
    m = reg.model_asof(tmp_path, family, dt.date(2022, 6, 1), table=table)
    emb = reg.LABEL_EMBARGO.get(family, v.EMBARGO)
    assert m is not None and m.labels_before == v.embargo_start(SESSIONS, dt.date(2022, 1, 1), emb)


def test_model_asof_by_year_and_before_the_first_fold(tmp_path):
    _version(tmp_path, "credit", "v1")
    t = reg.asof_table(tmp_path, "credit", SESSIONS)
    assert reg.model_asof(tmp_path, "credit", dt.date(2021, 6, 1), table=t) is None
    m23 = reg.model_asof(tmp_path, "credit", dt.date(2023, 3, 1), table=t)
    assert m23 is not None and m23.fold == "test2023" and m23.labels_before.year == 2022
    fin = reg.model_asof(tmp_path, "credit", dt.date(2024, 3, 1), table=t)
    assert fin is not None and fin.fold is None and fin.labels_before == dt.date(2024, 1, 1)
    # without a table it reads the manifest itself (same answer)
    again = reg.model_asof(tmp_path, "credit", dt.date(2023, 3, 1), sessions=SESSIONS)
    assert again == m23


@given(st.dates(dt.date(2019, 1, 1), dt.date(2024, 6, 30)))
def test_model_asof_never_serves_labels_on_or_after_the_session(d):
    root = Path(__file__).parent / "_ml_asof_fixture"
    table = [
        reg.ModelAsOf(
            family="credit",
            version="v1",
            fold=f"test{y}",
            path=str(root),
            labels_before=v.embargo_start(SESSIONS, dt.date(y, 1, 1)),
            serves_from=dt.date(y, 1, 1),
            serves_to=dt.date(y, 12, 31),
        )
        for y in (2022, 2023)
    ]
    m = reg.model_asof(root, "credit", d, table=table)
    if m is not None:
        assert m.labels_before <= d and m.serves_from <= d
        assert m.labels_before.year < d.year


def test_pick_version_takes_the_newest_registered_never_the_champion(tmp_path):
    for name in ("m2-a", "m2-b"):
        vdir = tmp_path / "credit" / name
        (vdir / "lgbm").mkdir(parents=True)
        (vdir / "lgbm" / "win.txt").write_text("t\n")
        files = reg.hash_files(vdir, ["lgbm/win.txt"])
        (vdir / "manifest.json").write_text(json.dumps({"files": files}))
    now = dt.datetime(2026, 10, 1, 12, 0)
    reg.register(tmp_path, "credit", "m2-a", now=now, rule="r", rule_passed=False)
    reg.register(
        tmp_path, "credit", "m2-b", now=now + dt.timedelta(hours=1), rule="r", rule_passed=False
    )
    assert reg.pick_version(tmp_path, "credit") == "m2-b"
    assert reg.pick_version(tmp_path, "credit", "m2-a") == "m2-a"
    with pytest.raises(reg.RegistryError, match="no manifest"):
        reg.pick_version(tmp_path, "credit", "m2-zzz")
    # M1 has no index: last version dir by name
    _version(tmp_path, "m1", "m1-0001", folds=False)
    _version(tmp_path, "m1", "m1-0002", folds=False)
    assert reg.pick_version(tmp_path, "m1") == "m1-0002"
    with pytest.raises(reg.RegistryError, match="no debit model"):
        reg.pick_version(tmp_path, "debit")


def test_serving_plan_never_scores_holdout_and_groups_by_fold(tmp_path):
    _version(tmp_path, "credit", "v1")
    days = [dt.date(2021, 12, 30), dt.date(2022, 5, 2), dt.date(2023, 5, 1), dt.date(2024, 2, 1)]
    plan = ma.serving_plan(tmp_path, "credit", days, None)
    served = {m.fold: ds for m, ds in plan.values()}
    assert served == {"test2022": [dt.date(2022, 5, 2)], "test2023": [dt.date(2023, 5, 1)]}


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            out |= {f"{node.module}.{a.name}" for a in node.names}
        elif isinstance(node, ast.Import):
            out |= {a.name for a in node.names}
    return out


def test_the_sim_reads_models_only_through_model_asof():
    """No backtest module reaches a champion pointer or a model loader except via
    ``arc.backtest.ml_arms``, and that module picks its models with ``model_asof``."""
    banned = ("champion", "load_index", "arc.ml.m2.predict.load_dir", "arc.ml.m1.predict.load")
    for p in sorted((REPO / "arc" / "backtest").glob("*.py")):
        names = _imports(p)
        src = p.read_text()
        if p.name == "ml_arms.py":
            assert "arc.ml.registry.model_asof" in names
            assert not [n for n in names if "champion" in n or n.endswith(".load_index")]
            attrs = {n.attr for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Attribute)}
            assert "champion" not in attrs
            continue
        hit = sorted(n for n in names if any(b in n for b in banned))
        assert not hit, f"{p.name} imports {hit}"
        assert "arc.ml.registry" not in " ".join(names), p.name
        assert ".champion" not in src, p.name


# ---------------------------------------------------------------------------
# Ranker / filter / size math
# ---------------------------------------------------------------------------


def test_ml_er_key_sorts_higher_e_r_first_and_drops_unscored():
    k = (0, -3.0, 1.5, "k")
    a, b = ma.ml_er_key(k, 0.4), ma.ml_er_key(k, 0.1)
    assert a is not None and b is not None and a < b
    assert a[2:] == k[2:]  # incumbent tie-breaks kept
    assert ma.ml_er_key(k, None) is None
    assert ma.ml_er_key(k, math.nan) is None


@pytest.mark.parametrize(
    ("er", "ps", "f", "keep"),
    [
        (0.1, 0.2, MlFilter(), True),
        (-0.1, 0.2, MlFilter(min_e_r=0.0), False),
        (0.0, 0.2, MlFilter(min_e_r=0.0), True),
        (0.1, 0.6, MlFilter(max_p_stop=0.5), False),
        (0.1, 0.5, MlFilter(max_p_stop=0.5), True),
        (None, None, MlFilter(min_e_r=0.0, max_p_stop=0.1), True),
        (math.nan, 0.1, MlFilter(min_e_r=0.0), False),
    ],
)
def test_ml_filter(er, ps, f, keep):
    assert ma.passes_ml_filter(er, ps, f) is keep


def test_ml_size_scale_and_shrink():
    s = MlSize(mode="shrink", floor=0.5, cap=1.0)
    assert ma.size_scale(0.2, 0.4, s) == 0.5
    assert ma.size_scale(0.1, 0.4, s) == 0.5  # floored
    assert ma.size_scale(0.3, 0.4, s) == pytest.approx(0.75)
    assert ma.size_scale(1.0, 0.4, s) == 1.0  # capped, never above D18
    assert ma.size_scale(-0.5, 0.4, s) == 0.5
    assert ma.size_scale(None, 0.4, s) == 1.0
    assert ma.size_scale(0.3, None, s) == 1.0
    assert ma.size_scale(0.3, 0.4, MlSize()) == 1.0  # off
    assert ma.shrink_contracts(4, 0.75) == 3
    assert ma.shrink_contracts(1, 0.5) == 1
    assert ma.shrink_contracts(3, 0.5) == 1
    assert ma.shrink_contracts(0, 0.5) == 0
    with pytest.raises(ValueError, match="floor"):
        MlSize(mode="shrink", floor=0.9, cap=0.8)


@given(
    st.integers(0, 50),
    st.floats(-2, 2, allow_nan=False),
    st.floats(0.01, 2),
    st.floats(0.05, 1.0),
)
def test_shrink_never_upsizes(n, er, med, floor):
    s = MlSize(mode="shrink", floor=floor, cap=1.0)
    scale = ma.size_scale(er, med, s)
    assert floor <= scale <= 1.0
    m = ma.shrink_contracts(n, scale)
    assert m <= n and (n == 0 or m >= 1)


def test_median_positive():
    assert ma.median_positive([-1.0, 0.0, 0.2, 0.4, math.nan]) == pytest.approx(0.3)
    assert ma.median_positive([0.5, -0.2, 0.1, 0.9]) == 0.5
    assert ma.median_positive([-1.0]) is None


def _sc(day: dt.date, key: str, t: str = "A") -> Any:
    return SimpleNamespace(
        candidate=SimpleNamespace(day=day, underlying=t, inputs=SimpleNamespace(key=key))
    )


def test_hooks_rank_filter_size_and_leave_unserved_sessions_alone():
    d, off = dt.date(2022, 3, 1), dt.date(2021, 3, 1)
    rows = pd.DataFrame({"date": [d] * 3, "ticker": ["A"] * 3, "key": ["k1", "k2", "k3"]})
    sc = pd.DataFrame({"e_r": [0.4, 0.1, -0.2], "p_stop": [0.1, 0.2, 0.7]})
    s = ma.MlScores(family="credit")
    s.add(rows, sc)
    s.served = {d}
    k = (0, -1.0, 0.0, "x")
    h = ma.MlHooks(s, ranker="ml_er", flt=MlFilter(min_e_r=0.0), size=MlSize(mode="shrink"))
    a, b = h.key_override(_sc(d, "k1"), k), h.key_override(_sc(d, "k2"), k)
    assert a is not None and b is not None and a < b
    assert h.key_override(_sc(d, "k3"), k) is None  # e_r < 0 filtered
    assert h.key_override(_sc(d, "nope"), k) is None  # unscored under ml_er
    assert h.key_override(_sc(off, "k1"), k) == k  # no model: control walk
    assert (h.hit, h.miss, h.dropped) == (3, 1, 2)
    # median positive = 0.25: k1 scale clip(1.6) = 1, k2 clip(0.4) = 0.5
    assert h.size_override(_sc(d, "k1"), 4) == 4
    assert h.size_override(_sc(d, "k2"), 4) == 2
    assert h.size_override(_sc(off, "k2"), 4) == 4
    assert h.resized == 1
    summ = h.summary()
    assert summ["coverage"] == 0.75 and summ["dropped"] == 2
    inc = ma.MlHooks(s, ranker="incumbent", flt=MlFilter(min_e_r=0.0), size=MlSize())
    assert inc.key_override(_sc(d, "k1"), k) == k
    assert inc.key_override(_sc(d, "nope"), k) == k  # filter leaves unscored alone
    assert inc.size_override(_sc(d, "k2"), 4) == 4


def test_family_for_profiles():
    assert ma.family_for("margin") == "credit"
    assert ma.family_for("cash_debit") == "debit"
    with pytest.raises(ma.MlArmError, match="no M2 family"):
        ma.family_for("cash_long_only")


# ---------------------------------------------------------------------------
# Config defaults leave existing hashes unchanged
# ---------------------------------------------------------------------------

#: study_hash / arm_set_hash of every committed BT spec before E27.5 (main 38c2ee49).
PRE_E275_HASHES = {
    "BT-1": ("f6dec356b185", "da03b0a634c0"),
    "BT-2": ("a5bc56346dc9", "8020aae9714a"),
    "BT-3": ("6bd6ea4a7b93", "924a715d3ba9"),
    "BT-4": ("308767167157", "26a00620f663"),
    "BT-5": ("46e247a2a6e6", "b0111115e027"),
    "BT-6": ("ff84836ecd11", "b5e94561540b"),
    "BT-7": ("0224b099ec9c", "2084785ee1d4"),
    "BT-8": ("bd19b7413a6e", "e5a6ebe45788"),
}
#: config_hash(load_ranking_file()) before E27.5: menu-cache keys hash this.
PRE_E275_RANKING_HASH = "30b8d4ae0bacca99"


def test_defaults_leave_study_and_config_hashes_unchanged():
    from arc.backtest.menucache import config_hash
    from arc.backtest.ranking import load_ranking_file
    from arc.backtest.study import STUDIES_DIR, arm_set_hash, load_study, study_hash

    got = {}
    for p in sorted(STUDIES_DIR.glob("BT-*.yaml")):
        s = load_study(p)
        if s.id in PRE_E275_HASHES:
            got[s.id] = (study_hash(s)[:12], arm_set_hash(s)[:12])
    assert got == PRE_E275_HASHES
    cfg = load_ranking_file()
    assert config_hash(cfg) == PRE_E275_RANKING_HASH
    dump = cfg.backtest.model_dump()
    assert not {"ml_ranker", "ml_filter", "ml_size", "ml_versions"} & set(dump)
    assert not cfg.backtest.uses_m2


def test_ml_options_parse_dump_and_hash_when_set():
    from arc.backtest.menucache import config_hash
    from arc.backtest.ranking import RankingFile, load_ranking_file

    base = load_ranking_file()
    raw = base.model_dump(mode="json")
    raw["backtest"] |= {
        "ml_ranker": "ml_er",
        "ml_filter": {"min_e_r": 0.0},
        "ml_size": {"mode": "shrink"},
        "ml_versions": {"credit": "m2-x"},
    }
    cfg = RankingFile.model_validate(raw)
    assert cfg.backtest.uses_m2 and cfg.backtest.ml_filter.min_e_r == 0.0
    assert config_hash(cfg) != config_hash(base)
    assert cfg.backtest.model_dump()["ml_ranker"] == "ml_er"
    raw["backtest"]["ml_versions"] = {"gamma": "x"}
    with pytest.raises(ValueError, match="unknown family"):
        RankingFile.model_validate(raw)


def test_a_study_arm_can_select_the_ml_options():
    from arc.backtest.study import StudySpec, arm_configs

    spec = StudySpec.model_validate(
        {
            "id": "BT-99",
            "hypothesis": "ml arms",
            "owner": "owner",
            "created": "2026-10-27",
            "arms": {
                "control": {},
                "treatments": {
                    "t1": {"overlay": {"ranking": {"backtest": {"ml_ranker": "ml_er"}}}},
                    "t2": {
                        "overlay": {
                            "ranking": {"backtest": {"ml_filter": {"min_e_r": 0.0}}},
                            "exits": {"model": {"path_vol": "m1"}},
                        }
                    },
                },
            },
            "profiles": ["margin"],
            "universe_ladder": ["c125"],
            "period": {
                "dev_start": "2022-01-03",
                "dev_end": "2023-06-30",
                "holdout_start": "2024-01-02",
                "holdout_end": "2024-06-28",
            },
        }
    )
    c0, e0, _ = arm_configs(spec, "control")
    c1, _, _ = arm_configs(spec, "t1")
    c2, e2, _ = arm_configs(spec, "t2")
    assert not c0.backtest.uses_m2 and c1.backtest.ml_ranker == "ml_er"
    assert c2.backtest.ml_filter.min_e_r == 0.0 and e2.model.path_vol == "m1"
    assert e0.model.path_vol != "m1"


def test_walk_only_ml_keys_share_the_control_build():
    from arc.backtest.study import _build_overlay

    assert _build_overlay({}) == {}
    assert _build_overlay({"ranking": {"backtest": {"ml_ranker": "ml_er"}}}) == {}
    mixed = {
        "ranking": {"backtest": {"ml_size": {"mode": "shrink"}, "n_paths": 9}, "x": 1},
        "exits": {"model": {"path_vol": "m1"}},
    }
    assert _build_overlay(mixed) == {
        "ranking": {"backtest": {"n_paths": 9}, "x": 1},
        "exits": {"model": {"path_vol": "m1"}},
    }
    pinned = {"ranking": {"backtest": {"ml_versions": {"m1": "v"}}}}
    assert _build_overlay(pinned) == pinned  # a pinned M1 version changes the build


# ---------------------------------------------------------------------------
# m1_direction stance proxy + M1 outputs
# ---------------------------------------------------------------------------


def test_m1_direction_stance_proxy():
    days = [dt.date(2022, 1, 3) + dt.timedelta(days=i) for i in range(5)]
    trend = pd.Series(["bull"] * 5, index=days, dtype=object)
    m1 = pd.Series([1.0, -1.0, 0.0, math.nan], index=days[:4])
    got = proxy_labels("m1_direction", trend, ticker="A", m1=m1)
    assert list(got) == ["bull", "bear", "sideways", "unknown", "unknown"]
    none = proxy_labels("m1_direction", trend, ticker="A")
    assert set(none) == {"unknown"}


def test_m1_direction_is_in_the_grid_enum():
    from arc.backtest.study import StudyRobustness

    r = StudyRobustness.model_validate({"stance": ["trend_v2", "m1_direction"]})
    assert "m1_direction" in r.stance


def _m1_version(root: Path) -> None:
    vdir = _version(root / "models", "m1", "m1-v", folds=False)
    man = json.loads((vdir / "manifest.json").read_text())
    man["train_window"] = {"start": "2020-07-01", "end": "2023-12-31"}
    (vdir / "manifest.json").write_text(json.dumps(man))
    rows = []
    for d in (dt.date(2021, 6, 1), dt.date(2022, 3, 1), dt.date(2023, 3, 1)):
        for t, dirn in (("AAA", 1.0), ("ZZZ", -1.0)):
            rows.append({"date": d, "ticker": t, "m1_direction": dirn, "m1_rv_h21": 0.3})
    pd.DataFrame(rows).to_parquet(vdir / "oos_m1_features.parquet")


def test_load_m1_outputs_serves_only_fold_years(tmp_path):
    _m1_version(tmp_path)
    days = [dt.date(2021, 6, 1), dt.date(2022, 3, 1), dt.date(2023, 3, 1), dt.date(2024, 3, 1)]
    out = ma.load_m1_outputs(ml_root=tmp_path, tickers=["aaa", "ZZZ"], sessions=days)
    assert set(out) == {"AAA", "ZZZ"}
    assert list(out["AAA"].index) == [dt.date(2022, 3, 1), dt.date(2023, 3, 1)]
    dirs = ma.m1_direction_of(out)
    assert list(dirs["ZZZ"]) == [-1.0, -1.0]
    rows = ma.m1_rows_of(out["AAA"])
    assert rows is not None and rows[dt.date(2022, 3, 1)]["m1_rv_h21"] == 0.3
    assert ma.m1_rows_of(None) is None
    assert ma.load_m1_outputs(ml_root=tmp_path, tickers=["AAA"], sessions=[days[0]]) == {}


def test_m1_params_from_a_row_and_fallback():
    from arc.backtest.ranking import _m1_params
    from arc.ml.m1.predict import M1_QUANTILES, qtag

    assert _m1_params(None, 30) is None
    assert _m1_params({"m1_rv_h21": 0.3}, 30) is None  # incomplete row: HV fallback
    row: dict[str, float] = {}
    for h in (5, 10, 21, 42):
        row[f"m1_rv_h{h}"] = 0.25
        for p in M1_QUANTILES:
            z = {0.05: -1.645, 0.1: -1.2816, 0.25: -0.6745, 0.5: 0.0}.get(p)
            z = z if z is not None else -{0.75: -0.6745, 0.9: -1.2816, 0.95: -1.645}[p]
            row[f"m1_q{qtag(p)}_h{h}"] = 0.25 * math.sqrt(h / 252) * z
    got = _m1_params(row, 30)
    assert got is not None and got.sigma == 0.25 and got.nu > 0
