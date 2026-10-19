"""E7.20 (D125): the robust study protocol (stance proxies × slippage grid)."""

from __future__ import annotations

import datetime as dt
import json
from typing import TYPE_CHECKING, Any

import pandas as pd
import pytest
import yaml

from arc.backtest.menucache import MenuCache, MenuCacheContext, config_hash
from arc.backtest.rank_report import _stances
from arc.backtest.runner import estimate_s, grid_cells_for
from arc.backtest.sim import SimSettings, run_sim, sim_outcome, sim_outcomes
from arc.backtest.stance import ANY, flip_weeks, labels_needed, proxy_labels
from arc.backtest.study import (
    StudyRobustness,
    StudySpec,
    cell_id,
    grid_plan,
    grid_verdict,
    load_ledger,
    study_hash,
    worst_cells,
)
from arc.config import ArcSettings
from tests.test_backtest_sim import (
    COST,
    DAYS,
    DECIDE,
    EXITS,
    NO_FILTERS,
    WINDOWS,
    _cfg,
    _days,
    _settings,
)
from tests.test_backtest_sim import view as view  # noqa: F401 - module fixture
from tests.test_backtest_study import (
    _entry,
    _run,
    _spec,
)
from tests.test_backtest_study import data_dir as data_dir  # noqa: F401 - fixture
from tests.test_backtest_study import ranking_path as ranking_path  # noqa: F401 - fixture

if TYPE_CHECKING:
    from pathlib import Path

    from arc.backtest.data import HistoryView


# ---------------------------------------------------------------------------
# Stance proxies (pure, seeded)
# ---------------------------------------------------------------------------

_D = [dt.date(2024, 1, 1) + dt.timedelta(days=i) for i in range(400)]
_TREND = pd.Series(["bull", "bear", "sideways", "unknown"] * 100, index=_D, dtype=object)


def test_trend_v2_and_both_and_lagged() -> None:
    assert proxy_labels("trend_v2", _TREND, ticker="AAA").equals(_TREND.astype(str))
    both = proxy_labels("both", _TREND, ticker="AAA")
    assert set(both) == {ANY} and list(both.index) == _D
    lag = proxy_labels("lagged_5", _TREND, ticker="AAA")
    assert list(lag.iloc[:5]) == ["unknown"] * 5
    assert list(lag.iloc[5:]) == list(_TREND.iloc[:-5])


def test_market_v2_uses_the_market_label_and_needs_it() -> None:
    mkt = pd.Series("bear", index=_D[::2], dtype=object)
    out = proxy_labels("market_v2", _TREND, ticker="AAA", market=mkt)
    assert set(out.iloc[::2]) == {"bear"} and set(out.iloc[1::2]) == {"unknown"}
    with pytest.raises(ValueError, match="market reference"):
        proxy_labels("market_v2", _TREND, ticker="AAA")


def test_noisy_is_deterministic_weekly_and_near_p() -> None:
    a = proxy_labels("noisy_0.35", _TREND, ticker="AAA", seed_key="h1")
    b = proxy_labels("noisy_0.35", _TREND, ticker="AAA", seed_key="h1")
    assert a.equals(b)  # reproduces exactly
    c = proxy_labels("noisy_0.35", _TREND, ticker="AAA", seed_key="h2")
    assert not a.equals(c)  # the seed (spec hash) matters
    assert not a.equals(proxy_labels("noisy_0.35", _TREND, ticker="ZZZ", seed_key="h1"))
    weeks = flip_weeks(_D, ticker="AAA", p=0.35, seed_key="h1")
    pooled = [
        v != 0
        for i in range(100)
        for v in flip_weeks(_D, ticker=f"T{i}", p=0.35, seed_key="h1").values()
    ]
    assert abs(sum(pooled) / len(pooled) - 0.35) < 0.03  # ~p over 5,800 ticker-weeks
    for d, lab in _TREND.items():  # flips are per ISO week, never per day
        iso = d.isocalendar()
        f = weeks[(iso[0], iso[1])]
        got = a[d]
        if lab == "unknown" or f == 0:
            assert got == lab
        elif lab == "sideways":
            assert got == ("bull" if f == 1 else "bear")
        else:
            assert got == {"bull": "bear", "bear": "bull"}[lab]


def test_unknown_and_reserved_proxies_are_refused() -> None:
    with pytest.raises(ValueError, match="unknown stance proxy"):
        proxy_labels("noisy_0.9", _TREND, ticker="AAA")
    with pytest.raises(ValueError, match="E7.22"):
        proxy_labels("live_llm", _TREND, ticker="AAA")


def test_labels_needed() -> None:
    assert labels_needed(["trend_v2"]) == frozenset()
    assert labels_needed(["trend_v2", "lagged_5"]) == {"bull", "bear", "sideways"}
    assert labels_needed(["trend_v2", "both"]) == {"bull", "bear", "sideways", ANY}


# ---------------------------------------------------------------------------
# Outcomes at several fills share exit timing
# ---------------------------------------------------------------------------


def test_sim_outcomes_equal_one_sim_outcome_per_fill(view: HistoryView) -> None:  # noqa: F811
    h = view.load("AAA")
    from tests.test_backtest_sim import _a_candidate

    c = _a_candidate(view)
    kw: dict[str, Any] = {
        "chains": h.chains,
        "closes": h.closes,
        "exits": EXITS,
        "settings": _settings(),
        "spread_rule": True,
    }
    xs = [0.0, 0.25, 0.5]
    costs = [COST.model_copy(update={"slippage_frac": x}) for x in xs]
    many = sim_outcomes(c, fills=[(k, k) for k in costs], **kw)
    for k, got in zip(costs, many, strict=True):
        one = sim_outcome(c, cost=k, fill_cost=k, **kw)
        assert got is not None and one is not None
        assert got.model_dump() == one.model_dump()
    pnl = [o.outcome.trade.pnl for o in many if o is not None]
    assert pnl[0] >= pnl[1] >= pnl[2]  # more slippage never helps


# ---------------------------------------------------------------------------
# Stance-tagged menus: the control cell reproduces the untagged build
# ---------------------------------------------------------------------------


def _trend_cfg():  # noqa: ANN202 - RankingFile
    cfg = _cfg()
    return cfg.model_copy(update={"backtest": cfg.backtest.model_copy(update={"stance": "trend"})})


def _build(view: HistoryView, t: str, **kw: Any):  # noqa: ANN202, F811
    from arc.backtest.sim import sim_ticker_from_chains
    from arc.scanner.rank import Ranker

    cfg = _trend_cfg()
    h = view.load(t)
    s = _settings("margin")
    # 80 sessions of warm-up history before the fixture, so the trend labels are known
    first = h.closes.sort_index()
    pre_days = pd.bdate_range(end=first.index[0] - dt.timedelta(days=1), periods=80).date
    pre = pd.Series(
        [float(first.iloc[0]) * (0.9 + 0.1 * i / 79) for i in range(80)], index=list(pre_days)
    )
    label_closes = pd.concat([pre, first])
    return sim_ticker_from_chains(
        t,
        h.chains,
        h.closes,
        decision_days=DECIDE,
        days=_days(100.0),
        cfg=cfg,
        exits=EXITS,
        cost=COST,
        settings={"margin": s},
        windows={"margin": WINDOWS["margin"]},
        rankers={"margin": [Ranker.CREDIT_WIDTH]},
        stances={"margin": _stances("margin", cfg, ArcSettings())},
        sim=SimSettings(regime_model="v1"),
        label_closes=label_closes,
        **kw,
    )


def _walk(tickers: dict, **kw: Any):  # noqa: ANN202
    from arc.scanner.rank import Ranker

    return run_sim(
        tickers,
        ranker=Ranker.CREDIT_WIDTH,
        profile="margin",
        settings=_settings("margin"),
        sim=SimSettings(momentum_size=0),
        filters=NO_FILTERS,
        vrp_threshold=0.0,
        sessions=DAYS,
        end=DECIDE[-1],
        **kw,
    )


def _trades(run: Any) -> list[tuple[object, ...]]:
    t = run.trades
    if t.empty:
        return []
    cols = ["entry_date", "underlying", "legs", "contracts", "pnl"]
    return sorted(map(tuple, t[cols].to_numpy().tolist()))


def test_tagged_build_control_cell_equals_the_untagged_walk(view: HistoryView) -> None:  # noqa: F811
    plain = {t: _build(view, t) for t in ("AAA", "ZZZ")}
    labels = labels_needed(["trend_v2", "noisy_0.35", "both"])
    tagged = {t: _build(view, t, stance_labels=labels, alt_x=(0.0, 0.5)) for t in ("AAA", "ZZZ")}
    base = _walk(plain)
    assert len(base.trades) > 0
    assert _trades(_walk(tagged)) == _trades(base)  # trend_v2 @ control x
    assert _trades(_walk(tagged, fill_x=0.25)) == _trades(base)  # x not carried = control
    # every kept candidate carries outcomes at the alternative x and its stance tags
    sc = next(iter(next(iter(tagged["AAA"].menus["margin"].values()))))
    assert set(sc.alt) == {0.0, 0.5} and sc.stances
    tags = {
        s
        for tk in tagged.values()
        for m in tk.menus["margin"].values()
        for c in m
        for s in c.stances or ()
    }
    assert ANY in tags and tags & {"bull", "bear", "sideways"}
    # another proxy / fill = a different, deterministic walk
    both = {t: pd.Series(ANY, index=tagged[t].trend.index) for t in tagged}
    w1, w2 = _walk(tagged, stance_of=both), _walk(tagged, stance_of=both)
    assert _trades(w1) == _trades(w2)
    hi = _walk(tagged, fill_x=0.5)
    lo = _walk(tagged, fill_x=0.0)
    assert float(lo.trades["pnl"].sum()) >= float(hi.trades["pnl"].sum())


def test_stance_and_x_do_not_change_the_menu_cache_key(
    view: HistoryView,  # noqa: F811
    tmp_path: Path,
) -> None:
    from arc.backtest.sim import config_hash as ch  # noqa: F401 - same helper as menucache

    ctx = MenuCacheContext(
        marks="quoted", exit_hash=config_hash(EXITS), n_paths=200, seed=7, provider="t"
    )
    c1 = MenuCache(tmp_path)
    _build(view, "AAA", cache=c1, cache_ctx=ctx)
    files = sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*.parquet"))
    assert files and c1.hits == 0
    c2 = MenuCache(tmp_path)
    _build(
        view,
        "AAA",
        cache=c2,
        cache_ctx=ctx,
        stance_labels=labels_needed(["trend_v2", "lagged_5", "both"]),
        alt_x=(0.0, 0.5),
    )
    assert c2.misses == 0 and c2.hits == c1.misses  # same menu files, all served
    assert sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*.parquet")) == files


def test_menu_cache_key_path_golden() -> None:
    """The pre-E7.20 key path is unchanged: cached E7.18/E7.19 menus keep resolving."""
    ctx = MenuCacheContext(marks="quoted:0.35:drop", exit_hash="e", n_paths=2000, seed=7)
    key = ctx.key("aaa", dt.date(2023, 3, 1), menu_hash=config_hash(["spec"], 0.25, None, 0.0))
    assert config_hash(["spec"], 0.25, None, 0.0) == GOLDEN_MENU_HASH
    assert key.digest() == GOLDEN_KEY_DIGEST


GOLDEN_MENU_HASH = "6b1a3d215c9bbc85"
GOLDEN_KEY_DIGEST = "28a7fde80df5c9fd"


# ---------------------------------------------------------------------------
# Spec, hash, verdict
# ---------------------------------------------------------------------------


def test_spec_without_robustness_keeps_its_hash() -> None:
    from arc.experiments.models import document_hash

    s = _spec()
    doc = s.model_dump(mode="json")
    doc.pop("robustness")
    rule = doc["decision_rule"]
    for k in ("ci_floor_frac_of_base", "dd_tolerance", "verbatim"):
        if rule.get(k) is None:
            rule.pop(k, None)
    assert study_hash(s) == document_hash(doc)


def test_grid_is_in_the_spec_hash() -> None:
    a = _spec(robustness={})
    b = _spec(robustness={"slippage_x": [0.0, 0.25]})
    c = _spec(robustness={"stance": ["trend_v2", "noisy_0.2"]})
    hashes = {study_hash(_spec()), study_hash(a), study_hash(b), study_hash(c)}
    assert len(hashes) == 4
    assert a.robustness is not None
    assert a.robustness.stance == ["trend_v2", "noisy_0.35", "lagged_5", "market_v2"]
    assert a.robustness.slippage_x == [0.0, 0.25, 0.5] and len(a.robustness.cells()) == 12


@pytest.mark.parametrize(
    ("rob", "match"),
    [
        ({"stance": ["noisy_0.2"]}, "trend_v2"),
        ({"stance": ["trend_v2", "live_llm"]}, "E7.22"),
        ({"slippage_x": [0.25, 0.25]}, "distinct"),
        ({"slippage_x": [1.5]}, "distinct"),
        ({"stance": ["trend_v2", "noisy_9"]}, "stance"),
    ],
)
def test_bad_grids_are_refused(rob: dict[str, Any], match: str) -> None:
    with pytest.raises(ValueError, match=match):
        _spec(robustness=rob)


def test_grid_needs_the_d25_rule() -> None:
    with pytest.raises(ValueError, match="d25_challenge"):
        _spec(
            robustness={},
            decision_rule={"kind": "keep_control", "dd_tolerance": 0.1},
        )


def _cell(diff: float, hi: float) -> dict[str, float]:
    return {"pnl_diff": diff, "ci_lo": diff - 10, "ci_hi": hi}


ROB = StudyRobustness()


def test_grid_verdict_switch_fragile_keep() -> None:
    cells = {f"c{i}": _cell(5.0, 20.0) for i in range(8)}
    cells["c8"] = _cell(-1.0, 3.0)
    g = grid_verdict({"switch": True}, cells, ROB)  # 8/9 = 89 % >= 75 %
    assert g["call"] == "switch" and g["cells_won"] == 8 and g["worst_cell"] == "c8"
    # one cell with its CI wholly below 0 -> fragile, even at 89 %
    g = grid_verdict({"switch": True}, cells | {"c8": _cell(-30.0, -1.0)}, ROB)
    assert g["call"] == "fragile" and g["cells_ci_below_0"] == ["c8"]
    # too few cells won -> fragile
    few = {f"c{i}": _cell(5.0 if i < 6 else -1.0, 4.0) for i in range(9)}  # 6/9 = 67 %
    assert grid_verdict({"switch": True}, few, ROB)["call"] == "fragile"
    # the control cell does not switch -> keep, whatever the grid says
    assert grid_verdict({"switch": False}, cells, ROB)["call"] == "keep"
    # exactly the threshold passes (3 of 4 = 75 %)
    four = {"a": _cell(1, 2), "b": _cell(1, 2), "c": _cell(1, 2), "d": _cell(-1, 2)}
    assert grid_verdict({"switch": True}, four, ROB)["call"] == "switch"


def test_worst_cells_and_grid_plan_on_later_rungs() -> None:
    s = _spec(robustness={"stance": ["trend_v2", "lagged_5"], "slippage_x": [0.0, 0.25]})
    assert grid_plan(s, phase="dev", universe="c125", entries=[]) == ("full", s.robustness.cells())  # type: ignore[union-attr]
    scope, cells = grid_plan(s, phase="holdout", universe="c500", entries=[])
    assert scope.startswith("full (no first-rung")
    row = _entry(
        s,
        cells={
            "cash_debit": {
                "t1": {
                    "trend_v2@0.25": _cell(-50.0, 1.0),  # the control cell is never "worst"
                    "trend_v2@0": _cell(10.0, 20.0),
                    "lagged_5@0": _cell(-5.0, 3.0),
                    "lagged_5@0.25": _cell(-9.0, 3.0),
                }
            }
        },
        grid={"scope": "full", "control_cell": "trend_v2@0.25"},
    )
    assert worst_cells(row, "trend_v2@0.25") == ["lagged_5@0.25"]
    for phase, uni in (("dev", "c500"), ("holdout", "c500")):
        scope, cells = grid_plan(s, phase=phase, universe=uni, entries=[row])
        assert (scope, cells) == ("control+worst", [("lagged_5", 0.25)])
    # a row of another spec hash is not reused
    other = row.model_copy(update={"spec_hash": "x"})
    assert grid_plan(s, phase="holdout", universe="c500", entries=[other])[0] != "control+worst"


def test_runner_estimate_scales_by_measured_walk_per_cell() -> None:
    s = _spec(robustness={})
    row = _entry(s, runtime_s=1000.0, grid={"walk_s": 400.0, "walk_s_per_cell": 40.0})
    assert estimate_s([row], s.id, "dev", "c125", 1.0) == 1000.0  # no grid: as measured
    assert estimate_s([row], s.id, "dev", "c125", 1.0, cells=2) == 600.0 + 80.0
    plain = _entry(s, runtime_s=500.0)
    assert estimate_s([plain], s.id, "dev", "c125", 1.0, cells=11) == 500.0
    assert grid_cells_for(_spec(), "dev", "c125", []) is None
    assert grid_cells_for(s, "dev", "c125", []) == 11  # 12 cells minus the control


# ---------------------------------------------------------------------------
# End to end: run_study with a grid
# ---------------------------------------------------------------------------


def test_run_study_with_a_grid_writes_cells_and_the_heat_table(
    data_dir: Path,  # noqa: F811
    ranking_path: Path,  # noqa: F811
    tmp_path: Path,
) -> None:
    rob = {"stance": ["trend_v2", "noisy_0.35", "lagged_5", "both"], "slippage_x": [0.0, 0.25]}
    s = _spec(robustness=rob)
    e = _run(s, data_dir, ranking_path, tmp_path)
    assert e.cells is not None and e.grid is not None
    ids = [cell_id(a, x) for a in rob["stance"] for x in rob["slippage_x"]]
    assert e.grid["scope"] == "full" and e.grid["cells"] == ids
    assert e.grid["control_cell"] == "trend_v2@0.25"
    assert e.grid["walk_s_per_cell"] is not None and e.grid["build_s"] >= 0
    assert set(e.cells["cash_debit"]["t1"]) == set(ids)
    v = e.verdicts["cash_debit"]["t1"]
    assert v["robust_call"] in ("switch", "fragile", "keep")
    assert v["switch"] == (v["robust_call"] == "switch")
    # the control cell's verdict fields still describe the control walk (old readers)
    ctrl = e.cells["cash_debit"]["t1"]["trend_v2@0.25"]
    assert ctrl["pnl_diff"] == v["pnl_diff"] and ctrl["ci_hi"] == v["ci_hi"]
    assert (
        ctrl["net_pnl_control"] == e.metrics["cash_debit"]["control"]["net_pnl"]
        and ctrl["trades_control"] == e.metrics["cash_debit"]["control"]["trades"]
    )
    md = (tmp_path / "reports" / "BT-9.md").read_text()
    assert "## Robustness grid (D125)" in md and "robust: **" in md and "walk-only" in md
    payload = json.loads((tmp_path / "reports" / "BT-9.json").read_text())
    assert payload["robustness"]["grid"]["cells"] == ids
    # the ledger line round-trips with the cells
    assert load_ledger(tmp_path / "ledger.jsonl")[0] == e


def test_run_study_without_a_grid_writes_the_old_ledger_line(
    data_dir: Path,  # noqa: F811
    ranking_path: Path,  # noqa: F811
    tmp_path: Path,
) -> None:
    e = _run(_spec(), data_dir, ranking_path, tmp_path)
    assert e.cells is None and e.grid is None
    raw = json.loads((tmp_path / "ledger.jsonl").read_text().splitlines()[0])
    assert "cells" not in raw and "grid" not in raw
    assert "robust_call" not in raw["verdicts"]["cash_debit"]["t1"]
    md = (tmp_path / "reports" / "BT-9.md").read_text()
    assert "Robustness grid" not in md


def test_market_v2_without_spy_closes_is_refused(
    data_dir: Path,  # noqa: F811
    ranking_path: Path,  # noqa: F811
    tmp_path: Path,
) -> None:
    from arc.backtest.study import StudyRefusedError

    s = _spec(robustness={"stance": ["trend_v2", "market_v2"], "slippage_x": [0.25]})
    with pytest.raises(StudyRefusedError, match="SPY"):
        _run(s, data_dir, ranking_path, tmp_path)


def test_default_grid_yaml_round_trips() -> None:
    s = _spec(robustness={})
    again = StudySpec.model_validate(yaml.safe_load(yaml.safe_dump(s.model_dump(mode="json"))))
    assert study_hash(again) == study_hash(s)


# ---------------------------------------------------------------------------
# E7.18's spread-to-premium cap (the demonstration treatment)
# ---------------------------------------------------------------------------


def test_spread_premium_filter(view: HistoryView) -> None:  # noqa: F811
    from arc.backtest.entry_filter import apply_spread_premium_filter, spread_premium_frac
    from tests.test_backtest_sim import _a_candidate

    c = _a_candidate(view)
    f = spread_premium_frac(c)
    net = abs(sum(p.side * p.mid for p in c.picks))
    assert f is not None and f == pytest.approx(sum(p.spread for p in c.picks) / net)
    d = c.day
    kept, n = apply_spread_premium_filter({d: [c]}, f + 1e-9)
    assert kept[d] == [c] and n == 0
    kept, n = apply_spread_premium_filter({d: [c]}, f * 0.99)
    assert kept[d] == [] and n == 1
