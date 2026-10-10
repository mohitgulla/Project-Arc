"""E17.3 (D77): the backtest harness's ``regime_model: v1 | v2`` switch and vol gate.

- v1 is the E7.2 / E7.5 labelling, unchanged (exact equality with ``label_trend`` /
  ``label_vol``), and a v1 ranking-backtest run writes byte-identical output with or
  without the new knobs at their defaults.
- v2 labels are the live :mod:`arc.features.regime` v2 labeller's, never look ahead,
  and are ``unknown`` only during their warm-up.
- the vol gate drops only the gated structure, only on days outside its vol labels.
"""

from __future__ import annotations

import datetime as dt
import math
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from arc.backtest import regime as bt_regime
from arc.backtest.costs import load_cost_model
from arc.backtest.rank_report import run_rank_report
from arc.backtest.ranking import (
    BootstrapSpec,
    apply_stance,
    labels_for,
    load_ranking_file,
)
from arc.backtest.report import label_closes_for
from arc.backtest.strategies import StrategyKind
from arc.backtest.underlying import UnderlyingStore
from arc.config import ArcSettings
from arc.features.regime import label_regimes_v2, label_vol_v2
from arc.scanner.rank import Ranker, RankFilters
from tests.test_ranking import _rows  # synthetic BSM chain rows (flat vol)

REPO = Path(__file__).resolve().parent.parent
FIXTURE_2Y = REPO / "tests" / "fixtures" / "regime" / "spy_daily_closes_2y.csv"


def _spy() -> pd.Series:
    df = pd.read_csv(FIXTURE_2Y)
    return pd.Series(
        df["close"].to_numpy(dtype=float),
        index=[dt.date.fromisoformat(d) for d in df["date"]],
    )


# ---------------------------------------------------------------------------
# Labels
# ---------------------------------------------------------------------------


def test_v1_labels_are_the_e75_labels() -> None:
    s = _spy()
    trend, vol = labels_for(s)
    pd.testing.assert_series_equal(trend, bt_regime.label_trend(s))
    pd.testing.assert_series_equal(vol, bt_regime.label_vol(s))
    t2, v2 = labels_for(s, "v1")
    pd.testing.assert_series_equal(t2, trend)
    pd.testing.assert_series_equal(v2, vol)


def test_v2_labels_match_the_live_labeller() -> None:
    s = _spy()
    trend, vol = labels_for(s, "v2")
    assert list(trend.index) == list(s.index) == list(vol.index)
    live = label_regimes_v2(s)
    assert (trend.loc[list(live.index)] == [str(x) for x in live]).all()
    assert (trend.drop(index=list(live.index)) == "unknown").all()
    lv = label_vol_v2(s)
    assert list(vol.loc[lv.dates]) == [str(x) for x in lv.state]
    assert set(trend) <= {"bull", "bear", "sideways", "unknown"}
    assert set(vol) <= {"low", "mid", "high", "unknown"}
    # v2 actually disagrees with v1 on some days (otherwise the report is moot)
    t1, _ = labels_for(s, "v1")
    both = (t1 != "unknown") & (trend != "unknown")
    assert 0 < int((t1[both] != trend[both]).sum()) < int(both.sum())


@pytest.mark.parametrize("model", ["v1", "v2"])
def test_labels_never_look_ahead(model: str) -> None:
    s = _spy()
    cut = s.index[400]
    full_t, full_v = labels_for(s, model)  # type: ignore[arg-type]
    past_t, past_v = labels_for(s[s.index <= cut], model)  # type: ignore[arg-type]
    shocked = s.copy()
    shocked[shocked.index > cut] *= 0.5
    sh_t, sh_v = labels_for(shocked, model)  # type: ignore[arg-type]
    idx = list(past_t.index)
    assert list(full_t.loc[idx]) == list(past_t) == list(sh_t.loc[idx])
    assert list(full_v.loc[idx]) == list(past_v) == list(sh_v.loc[idx])


def test_unsorted_input_is_sorted() -> None:
    s = _spy()
    a = labels_for(s, "v2")
    b = labels_for(s.iloc[::-1], "v2")
    pd.testing.assert_series_equal(a[0], b[0])
    pd.testing.assert_series_equal(a[1], b[1])


# ---------------------------------------------------------------------------
# Vol gate
# ---------------------------------------------------------------------------


def _cand(kind: str) -> object:
    return type("C", (), {"kind": kind})()


def test_vol_gate_drops_only_the_gated_kind_outside_its_labels() -> None:
    d1, d2, d3, d4 = (dt.date(2025, 1, d) for d in (2, 3, 6, 7))
    menus = {d: [_cand("iron_condor"), _cand("bull_put")] for d in (d1, d2, d3, d4)}
    trend = pd.Series({d: "sideways" for d in (d1, d2, d3, d4)})
    kinds = {"sideways": frozenset({"iron_condor", "bull_put"})}
    vol = pd.Series({d1: "low", d2: "mid", d3: "high"})  # d4 unknown
    gate = {"iron_condor": frozenset({"low", "mid"})}
    out = apply_stance(menus, trend, kinds, vol=vol, vol_gate=gate)  # type: ignore[arg-type]
    assert [c.kind for c in out[d1]] == ["iron_condor", "bull_put"]
    assert [c.kind for c in out[d2]] == ["iron_condor", "bull_put"]
    assert [c.kind for c in out[d3]] == ["bull_put"]
    assert [c.kind for c in out[d4]] == ["bull_put"]
    # no vol series: every gated kind is "unknown" vol, so it is dropped
    no_vol = apply_stance(menus, trend, kinds, vol_gate=gate)  # type: ignore[arg-type]
    assert all([c.kind for c in m] == ["bull_put"] for m in no_vol.values())
    # no gate: the stance menu, unchanged
    plain = apply_stance(menus, trend, kinds, vol=vol)  # type: ignore[arg-type]
    assert plain == apply_stance(menus, trend, kinds)  # type: ignore[arg-type]
    assert all(len(m) == 2 for m in plain.values())


def test_overlays_set_only_their_knob() -> None:
    base = load_ranking_file()
    assert base.backtest.regime_model == "v1" and base.backtest.vol_gate == {}
    v2 = load_ranking_file(None, [REPO / "config/experiments/e173_regime_v2.yaml"])
    assert v2.backtest.regime_model == "v2"
    assert v2.backtest.model_dump(exclude={"regime_model"}) == base.backtest.model_dump(
        exclude={"regime_model"}
    )
    g = load_ranking_file(None, [REPO / "config/experiments/e173_vol_gate.yaml"])
    assert g.backtest.vol_gate == {"margin": {StrategyKind.IRON_CONDOR: ["low", "mid"]}}
    assert g.backtest.model_dump(exclude={"vol_gate"}) == base.backtest.model_dump(
        exclude={"vol_gate"}
    )


# ---------------------------------------------------------------------------
# Label history (warm-up) loader
# ---------------------------------------------------------------------------


def test_label_closes_for(tmp_path: Path) -> None:
    s = _spy()
    UnderlyingStore(tmp_path).write("SPY", s)
    start, end = dt.date(2025, 6, 2), dt.date(2025, 9, 30)
    assert label_closes_for(["SPY"], start, end, tmp_path, None, 0) is None
    out = label_closes_for(["SPY"], start, end, tmp_path, None, 400)
    assert out is not None
    got = out["SPY"]
    assert min(got.index) >= start - dt.timedelta(days=400)
    assert max(got.index) <= end + dt.timedelta(days=70)
    assert len(got) > 300


def test_label_closes_for_adjusted(tmp_path: Path) -> None:
    from arc.backtest.entry_filter import OhlcStore

    s = _spy()
    df = pd.DataFrame({"open": s, "high": s, "low": s, "close": s, "vwap": s}, index=list(s.index))
    OhlcStore(tmp_path).write("SPY", df)
    start, end = dt.date(2025, 6, 2), dt.date(2025, 9, 30)
    out = label_closes_for(["SPY"], start, end, tmp_path, None, 400, adjusted=True)
    assert out is not None
    got = out["SPY"]
    assert max(got.index) <= end and min(got.index) >= start - dt.timedelta(days=400)
    assert np.allclose(got.to_numpy(), s.loc[list(got.index)].to_numpy())


# ---------------------------------------------------------------------------
# Ranking backtest: v1 byte-identical, v2 + label history + gate run end to end
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def synth(tmp_path_factory: pytest.TempPathFactory) -> dict[str, object]:
    from arc.data.history.store import ParquetHistoryStore
    from arc.utils.calendar import sessions_between

    root = tmp_path_factory.mktemp("rank173")
    days = sessions_between(dt.date(2023, 1, 3), dt.date(2024, 6, 28))
    closes = pd.Series(
        [100.0 * math.exp(0.0004 * i + 0.05 * math.sin(i / 9.0)) for i in range(len(days))],
        index=days,
        dtype=float,
    )
    store = ParquetHistoryStore(root)
    for d in (d for d in days if d >= dt.date(2024, 4, 1)):
        store.write_day("synth", "TST", d, _rows(d, float(closes[d])))
    return {"store": store, "closes": closes}


def _kw(synth: dict[str, object], cfg: object) -> dict[str, object]:
    closes: pd.Series = synth["closes"]  # type: ignore[assignment]
    return {
        "store": synth["store"],
        "closes_by_ticker": {"TST": closes[closes.index >= dt.date(2024, 1, 31)]},
        "tickers": ["TST"],
        "start": dt.date(2024, 4, 1),
        "end": dt.date(2024, 5, 10),
        "profiles": ["margin", "cash_debit"],
        "rankers": [Ranker.CREDIT_WIDTH, Ranker.DEBIT_WIDTH],
        "cfg": cfg,
        "cost": load_cost_model(),
        "settings": ArcSettings(),
        "provider": "synth",
        "charts": False,
    }


def _cfg(**bt: object) -> object:
    cfg = load_ranking_file()
    b = cfg.backtest.model_copy(
        update={
            "n_paths": 200,
            "slippage_grid": [0.25],
            "bootstrap": BootstrapSpec(resamples=100),
            **bt,
        }
    )
    return cfg.model_copy(
        update={
            "backtest": b,
            "ranking": cfg.ranking.model_copy(update={"filters": RankFilters(enabled=False)}),
        }
    )


def _files(d: Path) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in sorted(d.iterdir())}


def test_v1_run_is_byte_identical_with_explicit_defaults(
    synth: dict[str, object], tmp_path: Path
) -> None:
    """Explicit regime_model v1 / empty gate / no label history == the E7.5 run."""
    run_rank_report(out_dir=tmp_path / "a", **_kw(synth, _cfg()))  # type: ignore[arg-type]
    run_rank_report(
        out_dir=tmp_path / "b",
        label_closes_by_ticker=None,
        **_kw(synth, _cfg(regime_model="v1", vol_gate={})),  # type: ignore[arg-type]
    )
    a, b = _files(tmp_path / "a"), _files(tmp_path / "b")
    assert a == b and "trades.csv" in a
    assert "Regime labels" not in (tmp_path / "a" / "report.md").read_text()


def test_v2_run_with_label_history_and_gate(synth: dict[str, object], tmp_path: Path) -> None:
    closes: pd.Series = synth["closes"]  # type: ignore[assignment]
    hist = {"TST": closes}
    gate = {"margin": {StrategyKind.IRON_CONDOR: ["low", "mid"]}}
    frames = run_rank_report(
        out_dir=tmp_path / "v2",
        label_closes_by_ticker=hist,
        **_kw(synth, _cfg(regime_model="v2", vol_gate=gate)),  # type: ignore[arg-type]
    )
    report = (tmp_path / "v2" / "report.md").read_text()
    assert "Regime labels (E17.3, D77): v2" in report
    assert "Vol gate (E17.3): margin: iron_condor only in low|mid vol" in report
    t = frames["trades"]
    expected_trend, expected_vol = labels_for(closes, "v2")
    assert len(t) > 0
    for r in t.to_dict("records"):
        assert r["trend"] == expected_trend[r["entry_date"]]
        assert r["vol"] == expected_vol[r["entry_date"]]
        if r["profile"] == "margin" and r["kind"] == "iron_condor":
            assert r["vol"] in ("low", "mid")
