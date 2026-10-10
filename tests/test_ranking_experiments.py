"""E7.5a: ranking-backtest experiments as pure config.

Covers the net-EV ÷ cost filter (experiment c), the experiment overlays
(``config/experiments/*.yaml``), the regime-conditional menu (a), the DTE window
override (b), measured per-kind slippage from the scorecard, and ``rank-compare``.
Each experiment file gets a smoke run on the synthetic fixture dataset of
``tests/test_ranking.py``.
"""

from __future__ import annotations

import argparse
import datetime as dt
from pathlib import Path

import pandas as pd
import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from arc.backtest.cli import add_backtest_parser, run_rank_compare_cli
from arc.backtest.costs import load_cost_model
from arc.backtest.experiments import compare_dirs, load_runs
from arc.backtest.rank_report import run_rank_report
from arc.backtest.ranking import (
    BootstrapSpec,
    kind_costs,
    load_ranking_file,
    slippage_from_scorecard,
)
from arc.backtest.strategies import StrategyKind, StrategySpec
from arc.config import ArcSettings
from arc.journal.scorecard import KindSlippage
from arc.models import StructureKind
from arc.scanner.rank import (
    Ranker,
    RankFilters,
    RankInputs,
    load_ranking_config,
    passes_cost_filter,
    passes_filters,
    rank,
)
from tests.test_ranking import synth  # noqa: F401  (module-scoped synthetic dataset)

REPO = Path(__file__).resolve().parent.parent
EXPERIMENTS = sorted((REPO / "config" / "experiments").glob("*.yaml"))


def _c(key: str, **kw: object) -> RankInputs:
    base: dict[str, object] = {
        "key": key,
        "credit": False,
        "vertical": True,
        "ev_proxy": 0.0,
        "managed_net_ev": 10.0,
        "managed_pop": 0.5,
        "est_cost": 5.0,
    }
    base.update(kw)
    return RankInputs.model_validate(base)


# ---------------------------------------------------------------------------
# (c) Net EV ÷ estimated cost filter
# ---------------------------------------------------------------------------


def test_cost_filter_off_by_default_and_live_config_unchanged() -> None:
    assert RankFilters().min_net_ev_to_cost is None
    assert load_ranking_config().filters.min_net_ev_to_cost is None
    assert passes_filters(_c("a", est_cost=None), RankFilters())


def test_cost_filter_threshold_and_fail_closed() -> None:
    f = RankFilters(min_net_ev_to_cost=1.0)
    assert passes_filters(_c("ok", managed_net_ev=5.0, est_cost=5.0), f)  # ratio 1.0
    assert not passes_filters(_c("thin", managed_net_ev=4.99, est_cost=5.0), f)
    assert not passes_filters(_c("unknown", est_cost=None), f)  # no estimate → out
    assert passes_filters(_c("free", managed_net_ev=0.01, est_cost=0.0), f)
    assert not passes_cost_filter(_c("nev", managed_net_ev=None), 1.0)
    assert passes_cost_filter(_c("any", est_cost=None), None)


def test_cost_filter_runs_before_every_ranker() -> None:
    f = RankFilters(min_net_ev_to_cost=2.0)
    menu = [
        _c("thin", managed_net_ev=50.0, est_cost=40.0, credit_width=0.9),  # 1.25x
        _c("fat", managed_net_ev=20.0, est_cost=5.0, credit_width=0.1),  # 4x
    ]
    for r in (Ranker.MANAGED_NET_EV, Ranker.EV_PROXY, Ranker.DEBIT_WIDTH):
        assert [c.key for c in rank(menu, r, filters=f)] == ["fat"]


@given(
    nev=st.floats(-1e4, 1e4, allow_nan=False),
    cost=st.floats(0.0, 1e4, allow_nan=False),
    k=st.floats(0.0, 10.0, allow_nan=False),
)
def test_cost_filter_monotone_in_threshold(nev: float, cost: float, k: float) -> None:
    """Raising the threshold never lets a candidate back in."""
    c = _c("x", managed_net_ev=nev, est_cost=cost)
    if passes_cost_filter(c, k + 0.5):
        assert passes_cost_filter(c, k)
    if cost > 0:
        assert passes_cost_filter(c, k) == (nev / cost >= k)


# ---------------------------------------------------------------------------
# Overlays and experiment knobs
# ---------------------------------------------------------------------------


def test_experiment_files_exist_and_each_changes_one_thing() -> None:
    base = load_ranking_file()
    names = {p.stem for p in EXPERIMENTS}
    e75a = {"e75a_a_regime_menu", "e75a_b_short_dte", "e75a_c_ev_cost"}
    # E7.5b's overlay is a measure comparison (menu shape + tilt), not a one-knob E7.5a test
    assert names == e75a | {"e75b_unified_measure"}
    for p in (x for x in EXPERIMENTS if x.stem in e75a):
        cfg = load_ranking_file(None, [p])
        diffs = []
        if cfg.backtest.stance_menus != base.backtest.stance_menus:
            diffs.append("stance_menus")
        if cfg.backtest.dte_windows != base.backtest.dte_windows:
            diffs.append("dte_windows")
        if cfg.ranking.filters != base.ranking.filters:
            diffs.append("filters")
        if cfg.backtest.slippage_by_kind != base.backtest.slippage_by_kind:
            diffs.append("slippage")
        rest_b = base.backtest.model_dump(exclude={"stance_menus", "dte_windows"})
        rest_e = cfg.backtest.model_dump(exclude={"stance_menus", "dte_windows"})
        assert rest_b == rest_e, p.name
        assert len(diffs) == 1, (p.name, diffs)


def test_e75b_overlay_is_live_shaped_and_tilted() -> None:
    from arc.backtest.strategies import ExpiryMode
    from arc.scanner.rank import Ranker

    base = load_ranking_file()
    cfg = load_ranking_file(None, [REPO / "config/experiments/e75b_unified_measure.yaml"])
    assert cfg.backtest.direction_tilt == 0.25 and base.backtest.direction_tilt == 0.0
    for prof, specs in cfg.backtest.menus.items():
        assert all(m.expiry_mode is ExpiryMode.ALL for m in specs), prof
        # same kinds and anchor deltas as the E7.5 menu: only the expiry shape changes
        strip = [m.model_dump(exclude={"expiry_mode"}) for m in specs]
        assert strip == [m.model_dump(exclude={"expiry_mode"}) for m in base.backtest.menus[prof]]
    assert {Ranker.MANAGED_NET_EV_TILTED, Ranker.RORC_DAY_TILTED} <= set(cfg.ranking.rankers)
    assert cfg.ranking.filters == base.ranking.filters


def test_experiment_a_content() -> None:
    cfg = load_ranking_file(None, [REPO / "config/experiments/e75a_a_regime_menu.yaml"])
    m = cfg.backtest.stance_menus
    assert m["margin"] == {"bull": [], "bear": [], "sideways": [StrategyKind.IRON_CONDOR]}
    assert StrategyKind.BEAR_PUT not in m["cash_debit"]["bear"]
    assert m["cash_debit"]["bear"] == [StrategyKind.LONG_PUT]


def test_overlay_deep_merges_and_rejects_unknown_keys(tmp_path: Path) -> None:
    o = tmp_path / "o.yaml"
    o.write_text("experiment: x\nranking:\n  filters:\n    min_managed_pop: 0.4\n")
    cfg = load_ranking_file(None, [o])
    assert cfg.ranking.filters.min_managed_pop == 0.4
    assert cfg.ranking.filters.enabled is True  # sibling keys kept
    assert cfg.backtest.menus == load_ranking_file().backtest.menus
    bad = tmp_path / "bad.yaml"
    bad.write_text("backtest:\n  not_a_knob: 1\n")
    with pytest.raises(ValidationError):
        load_ranking_file(None, [bad])


def test_window_for_override() -> None:
    bt = load_ranking_file(None, [REPO / "config/experiments/e75a_b_short_dte.yaml"]).backtest
    assert bt.window_for("margin", (30, 45)) == (21, 30)
    assert bt.window_for("cash_long_only", (30, 60)) == (30, 60)


def test_kind_costs_maps_strategy_to_structure_kind() -> None:
    cost = load_cost_model()
    specs = [
        StrategySpec(kind=StrategyKind.BULL_PUT),
        StrategySpec(kind=StrategyKind.IRON_CONDOR),
        StrategySpec(kind=StrategyKind.LONG_CALL),
    ]
    out = kind_costs(specs, cost, {StructureKind.VERTICAL_CREDIT: 0.4})
    assert set(out) == {"bull_put"} and out["bull_put"].slippage_frac == 0.4


def test_slippage_from_scorecard() -> None:
    by_kind = {
        "iron_condor": KindSlippage(fills=8, realised_usd=40.0, spread_usd=100.0),
        "vertical_debit": KindSlippage(fills=2, realised_usd=10.0, spread_usd=20.0),
        "long_call": KindSlippage(fills=9, realised_usd=-5.0, spread_usd=50.0),
        "other": KindSlippage(fills=9, realised_usd=5.0, spread_usd=0.0),
    }
    out = slippage_from_scorecard(by_kind, min_fills=5)
    assert out == {StructureKind.IRON_CONDOR: 0.4, StructureKind.LONG_CALL: 0.0}


# ---------------------------------------------------------------------------
# Smoke runs on the synthetic fixture dataset
# ---------------------------------------------------------------------------


def _kw(data: dict[str, object], cfg: object) -> dict[str, object]:
    return {
        "store": data["store"],
        "closes_by_ticker": {"TST": data["closes"]},
        "tickers": ["TST"],
        "start": dt.date(2024, 4, 1),
        "end": dt.date(2024, 5, 10),
        "profiles": ["margin", "cash_debit"],
        "rankers": [Ranker.MANAGED_NET_EV],
        "cfg": cfg,
        "cost": load_cost_model(),
        "settings": ArcSettings(_env_file=None),  # type: ignore[call-arg]
        "provider": "synth",
        "charts": False,
    }


def _small(cfg: object, *, filters_off: bool = True) -> object:
    bt = cfg.backtest.model_copy(  # type: ignore[attr-defined]
        update={"n_paths": 200, "slippage_grid": [0.25], "bootstrap": BootstrapSpec(resamples=100)}
    )
    upd: dict[str, object] = {"backtest": bt}
    if filters_off:
        f = cfg.ranking.filters.model_copy(  # type: ignore[attr-defined]
            update={"min_managed_net_ev": -1e9}
        )
        upd["ranking"] = cfg.ranking.model_copy(update={"filters": f})  # type: ignore[attr-defined]
    return cfg.model_copy(update=upd)  # type: ignore[attr-defined]


@pytest.fixture(scope="module")
def baseline_dir(
    synth: dict[str, object],  # noqa: F811
    tmp_path_factory: pytest.TempPathFactory,
) -> Path:
    out = tmp_path_factory.mktemp("e75a") / "baseline"
    run_rank_report(out_dir=out, **_kw(synth, _small(load_ranking_file())))  # type: ignore[arg-type]
    return out


@pytest.mark.parametrize("path", EXPERIMENTS, ids=lambda p: p.stem)
def test_experiment_smoke_on_fixture_dataset(
    path: Path,
    synth: dict[str, object],  # noqa: F811
    baseline_dir: Path,
    tmp_path: Path,
) -> None:
    cfg = _small(load_ranking_file(None, [path]))
    frames = run_rank_report(out_dir=tmp_path, **_kw(synth, cfg))  # type: ignore[arg-type]
    report = (tmp_path / "report.md").read_text()
    assert "## Verdict" in report and (tmp_path / "equity.csv").exists()
    t = frames["trades"]
    if path.stem == "e75a_a_regime_menu" and len(t):
        m = t[t.profile == "margin"]
        assert set(m["kind"]) <= {"iron_condor"} and set(m["trend"]) <= {"sideways"}
        assert "bear_put" not in set(t[t.profile == "cash_debit"]["kind"])
        assert "Regime-conditional menu" in report
    if path.stem == "e75a_b_short_dte":
        assert "margin 21–30" in report and "cash_debit 21–35" in report
        if len(t):
            dte = (pd.to_datetime(t["expiration"]) - pd.to_datetime(t["entry_date"])).dt.days
            assert dte.max() <= 35
    if path.stem == "e75a_c_ev_cost":
        assert "Net EV ÷ est. cost filter: ≥ 1" in report
    cmp_ = compare_dirs(baseline_dir, tmp_path, load_ranking_file(), name=path.stem)
    assert set(cmp_["profile"]) == {"margin", "cash_debit"}
    assert {"pnl_diff", "ci_lo", "ci_hi", "switch", "subperiods_won"} <= set(cmp_.columns)


def test_cost_filter_only_removes_trades(
    synth: dict[str, object],  # noqa: F811
    tmp_path: Path,
) -> None:
    """With the filter at a huge ratio nothing passes; the report still renders."""
    cfg = _small(load_ranking_file(None, [REPO / "config/experiments/e75a_c_ev_cost.yaml"]))
    f = cfg.ranking.filters.model_copy(update={"min_net_ev_to_cost": 1e9})  # type: ignore[attr-defined]
    cfg = cfg.model_copy(update={"ranking": cfg.ranking.model_copy(update={"filters": f})})  # type: ignore[attr-defined]
    frames = run_rank_report(out_dir=tmp_path, **_kw(synth, cfg))  # type: ignore[arg-type]
    assert frames["trades"].empty


def test_measured_slippage_reaches_the_base_run(
    synth: dict[str, object],  # noqa: F811
    baseline_dir: Path,
    tmp_path: Path,
) -> None:
    """A higher measured x for every kind costs more than costs.yaml's x (base run)."""
    worse = {k: 0.9 for k in StructureKind if k is not StructureKind.OTHER}
    cfg = _small(load_ranking_file())
    bt = cfg.backtest.model_copy(update={"slippage_by_kind": worse})  # type: ignore[attr-defined]
    cfg = cfg.model_copy(update={"backtest": bt})  # type: ignore[attr-defined]
    run_rank_report(out_dir=tmp_path, **_kw(synth, cfg))  # type: ignore[arg-type]
    assert "Measured slippage x by structure" in (tmp_path / "report.md").read_text()
    base, exp = load_runs(baseline_dir), load_runs(tmp_path)
    checked = 0
    for key in base:
        tb, te = base[key].trades, exp[key].trades
        if len(tb) and len(te):
            common = tb.merge(te, on=["underlying", "entry_date", "legs"], suffixes=("_b", "_e"))
            checked += len(common)
            # same legs, worse fills: pay more (or collect less) at entry, earn less
            assert (common["entry_net_e"] >= common["entry_net_b"] - 1e-9).all()
            assert (common["pnl_unit_e"] <= common["pnl_unit_b"] + 1e-9).all()
    assert checked > 0


def test_rank_compare_cli(baseline_dir: Path, tmp_path: Path) -> None:
    parser = argparse.ArgumentParser()
    add_backtest_parser(parser.add_subparsers(dest="cmd"))
    args = parser.parse_args(
        [
            "backtest",
            "rank-compare",
            "--baseline",
            str(baseline_dir),
            "--experiment",
            str(baseline_dir),
            "--out",
            str(tmp_path),
        ]
    )
    assert run_rank_compare_cli(args) == 0
    df = pd.read_csv(tmp_path / "compare.csv")
    assert (df["pnl_diff"] == 0).all() and not df["switch"].any()  # a run vs itself
