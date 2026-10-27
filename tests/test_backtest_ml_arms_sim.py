"""E27.5 (D126): the ML arm hooks on the synthetic sim fixtures (walk + build + study)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from tests.test_backtest_e720 import _build, _trades, _walk
from tests.test_backtest_sim import EXITS
from tests.test_backtest_sim import view as view  # noqa: F401 - module fixture
from tests.test_backtest_study import _run, _spec
from tests.test_backtest_study import data_dir as data_dir  # noqa: F401 - fixture
from tests.test_backtest_study import ranking_path as ranking_path  # noqa: F401 - fixture

if TYPE_CHECKING:
    from pathlib import Path

    from arc.backtest.data import HistoryView


def test_size_override_shrinks_only_and_none_is_the_plain_walk(view: HistoryView) -> None:  # noqa: F811
    tk = {t: _build(view, t) for t in ("AAA", "ZZZ")}
    base = _walk(tk)
    assert len(base.trades) > 0
    assert _trades(_walk(tk, size_override=None)) == _trades(base)
    assert _trades(_walk(tk, size_override=lambda sc, n: n)) == _trades(base)
    big = _walk(tk, size_override=lambda sc, n: n * 10)  # capped at the D18 size
    assert _trades(big) == _trades(base)
    one = _walk(tk, size_override=lambda sc, n: 1)
    assert (one.trades["contracts"] == 1).all()


def test_m1_rows_move_the_paths_only_under_path_vol_m1(view: HistoryView) -> None:  # noqa: F811
    from arc.ml.m1.predict import M1_QUANTILES, qtag

    sessions = sorted(view.load("AAA").closes.index)

    def row(sig: float) -> dict[str, float]:
        out: dict[str, float] = {}
        z = {0.05: -1.645, 0.1: -1.2816, 0.25: -0.6745, 0.5: 0.0, 0.75: 0.6745}
        z |= {0.9: 1.2816, 0.95: 1.645}
        for h in (5, 10, 21, 42):
            out[f"m1_rv_h{h}"] = sig
            for p in M1_QUANTILES:
                out[f"m1_q{qtag(p)}_h{h}"] = sig * (h / 252) ** 0.5 * z[p]
        return out

    rows = {d: row(0.9) for d in sessions}

    def build(exits: Any, **kw: Any) -> Any:
        from arc.backtest import sim as sim_mod

        real = sim_mod.sim_ticker_from_chains

        def with_exits(*a: Any, **k: Any) -> Any:
            k["exits"] = exits
            return real(*a, **k)

        sim_mod.sim_ticker_from_chains = with_exits  # type: ignore[assignment]
        try:
            return _build(view, "AAA", **kw)
        finally:
            sim_mod.sim_ticker_from_chains = real

    plain = build(EXITS)
    ignored = build(EXITS, m1_rows=rows)  # base exits: path_vol is not m1

    def evs(tk: Any) -> list[float | None]:
        return [c.candidate.inputs.managed_net_ev for m in tk.menus["margin"].values() for c in m]

    assert evs(ignored) == evs(plain)
    m1_exits = EXITS.model_copy(update={"model": EXITS.model.model_copy(update={"path_vol": "m1"})})
    no_rows = build(m1_exits)
    with_rows = build(m1_exits, m1_rows=rows)
    assert evs(no_rows) and evs(with_rows) != evs(no_rows)


def test_ml_arms_refuse_holdout(
    data_dir: Path,  # noqa: F811
    ranking_path: Path,  # noqa: F811
    tmp_path: Path,
) -> None:
    from arc.backtest.study import StudyRefusedError

    arms = {
        "control": {"ranker": "debit_width"},
        "treatments": {"t1": {"overlay": {"ranking": {"backtest": {"ml_ranker": "ml_er"}}}}},
    }
    s = _spec(arms=arms)
    with pytest.raises(StudyRefusedError, match="dev-only"):
        _run(s, data_dir, ranking_path, tmp_path, phase="holdout")
    s2 = _spec(robustness={"stance": ["trend_v2", "m1_direction"], "slippage_x": [0.25]})
    with pytest.raises(StudyRefusedError, match="dev-only"):
        _run(s2, data_dir, ranking_path, tmp_path, phase="holdout")


def test_ml_er_study_without_models_names_the_missing_family(
    data_dir: Path,  # noqa: F811
    ranking_path: Path,  # noqa: F811
    tmp_path: Path,
) -> None:
    from arc.ml.registry import RegistryError

    arms = {
        "control": {"ranker": "debit_width"},
        "treatments": {"t1": {"overlay": {"ranking": {"backtest": {"ml_ranker": "ml_er"}}}}},
    }
    with pytest.raises(RegistryError, match="no debit model"):
        _run(_spec(arms=arms), data_dir, ranking_path, tmp_path, ml_root=tmp_path / "ml")


def test_m1_direction_grid_cell_without_m1_models_reads_unknown(
    data_dir: Path,  # noqa: F811
    ranking_path: Path,  # noqa: F811
    tmp_path: Path,
) -> None:
    (tmp_path / "ml" / "models" / "m1" / "m1-x").mkdir(parents=True)
    (tmp_path / "ml" / "models" / "m1" / "m1-x" / "manifest.json").write_text("{}")
    rob = {"stance": ["trend_v2", "m1_direction"], "slippage_x": [0.25]}
    e = _run(_spec(robustness=rob), data_dir, ranking_path, tmp_path, ml_root=tmp_path / "ml")
    assert e.cells is not None
    cells = e.cells["cash_debit"]["t1"]
    # no servable M1 model: every session reads "unknown", which under the fixture's
    # stance "none" trades the whole menu, exactly like the unlabelled control build
    m1c, ctl = cells["m1_direction@0.25"], cells["trend_v2@0.25"]
    assert m1c["trades_control"] == ctl["trades_control"]
    assert m1c["net_pnl_control"] == ctl["net_pnl_control"]
