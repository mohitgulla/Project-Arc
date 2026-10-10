"""E17.1 (D77): regime v2 — vol-scaled trend z, per-ticker vol state, rolling fit.

The fixture is 300 real SPY daily closes (``tests/fixtures/regime/spy_daily_closes.csv``,
2025-07-18 .. 2026-09-25). Expected values are computed in this file by plain loops over
``math``/``statistics``, never by the module under test.
"""

from __future__ import annotations

import csv
import datetime as dt
import json
import math
import statistics
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from arc.config import ArcSettings
from arc.context.kinds import KINDS, RegimePayload, validate_payload
from arc.features._series import InsufficientHistoryError
from arc.features.regime import (
    V2_FIELDS,
    Regime,
    RegimeFeatures,
    VolState,
    classify_z,
    estimate_regime,
    label_regimes_v2,
    label_vol,
    label_vol_v2,
    margin_to_threshold,
    percentile_rank,
    run_length,
    vol_scaled_z,
    vol_state_fixed,
    vol_state_from_pct,
)
from arc.features.snapshot import build_snapshot
from arc.personas.builders import regime_line
from arc.pipeline.steps import regime_history_days, regime_kwargs

FIXTURES = Path(__file__).parent / "fixtures" / "regime"
V2 = {"model": "v2", "fit_window": 252, "alpha": 0.5}


def _fixture() -> pd.Series:
    with (FIXTURES / "spy_daily_closes.csv").open() as fh:
        rows = list(csv.DictReader(fh))
    return pd.Series(
        [float(r["close"]) for r in rows],
        index=[dt.date.fromisoformat(r["date"]) for r in rows],
        dtype=float,
    )


def _closes(values: list[float] | np.ndarray, start: dt.date = dt.date(2024, 1, 2)) -> pd.Series:
    idx = [d.date() for d in pd.bdate_range(start, periods=len(values))]
    return pd.Series(list(values), index=idx, dtype=float)


def _gbm(n: int, seed: int = 7, drift: float = 0.0, vol: float = 0.2) -> pd.Series:
    rng = np.random.default_rng(seed)
    rets = rng.normal(drift / 252, vol / np.sqrt(252), n - 1)
    return _closes(100.0 * np.exp(np.concatenate([[0.0], np.cumsum(rets)])))


# ---------------------------------------------------------------------------
# Plain-loop reference implementation (independent of arc.features.regime)
# ---------------------------------------------------------------------------


def _ref_z(c: list[float], t: int, lookback: int = 20, win: int = 60) -> float:
    rets = [math.log(c[k] / c[k - 1]) for k in range(t - win + 1, t + 1)]
    sigma = statistics.stdev(rets)
    r = c[t] / c[t - lookback] - 1.0
    return r / (sigma * math.sqrt(lookback))


def _ref_label(z: float) -> str:
    return "bull" if z >= 1.0 else "bear" if z <= -1.0 else "sideways"


def _ref_rv(c: list[float], t: int, lookback: int = 20) -> float:
    rets = [math.log(c[k] / c[k - 1]) for k in range(t - lookback + 1, t + 1)]
    return statistics.stdev(rets) * math.sqrt(252)


def _ref_pct(window: list[float], v: float) -> float:
    below = sum(1 for x in window if x < v)
    equal = sum(1 for x in window if x == v)
    return 100.0 * (below + equal / 2) / len(window)


# ---------------------------------------------------------------------------
# Hand-checked fixture
# ---------------------------------------------------------------------------


class TestFixture:
    def test_z_and_labels_match_plain_loop(self) -> None:
        s = _fixture()
        c = list(s)
        z = vol_scaled_z(s)
        labels = label_regimes_v2(s)
        assert len(z) == len(c) - 60
        for t in range(60, len(c)):
            ref = _ref_z(c, t)
            assert z.iloc[t - 60] == pytest.approx(ref, rel=1e-9, abs=1e-12)
            assert z.index[t - 60] == s.index[t]
            assert labels.iloc[t - 60].value == _ref_label(ref)

    def test_vol_percentiles_match_plain_loop(self) -> None:
        s = _fixture()
        c = list(s)
        rv = [_ref_rv(c, t) for t in range(20, len(c))]
        v = label_vol_v2(s)
        assert len(v.rv) == len(rv)
        for k, ref in enumerate(rv):
            assert v.rv[k] == pytest.approx(ref, rel=1e-9)
            if k + 1 >= 252:
                pct = _ref_pct(v.rv[k - 251 : k + 1], v.rv[k])
                assert v.pct_rank[k] == pytest.approx(pct)
                exp = "low" if pct < 100 / 3 else "high" if pct >= 200 / 3 else "mid"
                assert v.state[k].value == exp and v.source[k] == "percentile"
            else:
                exp = "low" if ref < 0.12 else "high" if ref >= 0.20 else "mid"
                assert v.pct_rank[k] is None and v.source[k] == "fixed"
                assert v.state[k].value == exp

    def test_estimate_regime_v2_fields(self) -> None:
        s = _fixture()
        c = list(s)
        t = len(c) - 1
        f = estimate_regime(s, s.index[-1], **V2)
        zs = [_ref_z(c, k) for k in range(60, len(c))]
        labels = [_ref_label(z) for z in zs]
        run = 1
        while run < len(labels) and labels[-1 - run] == labels[-1]:
            run += 1
        assert f.model == "v2"
        assert f.current.value == labels[-1]
        assert f.z == pytest.approx(zs[-1])
        assert f.trailing_return == pytest.approx(c[t] / c[t - 20] - 1)
        assert f.run_length == run
        assert f.margin_z == pytest.approx(min(abs(zs[-1] - 1), abs(zs[-1] + 1)))
        assert f.fit_window == min(252, len(labels)) == 240
        assert f.n_transitions == 239
        # rolling fit with Laplace 0.5: P[sideways][sideways] from plain counts
        idx = {"bear": 0, "sideways": 1, "bull": 2}
        counts = [[0.5] * 3 for _ in range(3)]
        fit = labels[-252:]
        for a, b in zip(fit[:-1], fit[1:], strict=True):
            counts[idx[a]][idx[b]] += 1
        row = counts[idx[labels[-1]]]
        assert f.stickiness == pytest.approx(row[idx[labels[-1]]] / sum(row))
        rv_now = _ref_rv(c, t)
        assert f.rv20 == pytest.approx(rv_now)
        assert f.vol_label_source == "percentile"
        rvs = [_ref_rv(c, k) for k in range(t - 251, t + 1)]
        assert f.rv20_pct_rank == pytest.approx(_ref_pct(rvs, rvs[-1]), abs=1e-9)
        assert f.vol_state is not None and f.vol_transition_matrix is not None
        assert f.vol_stickiness == pytest.approx(f.vol_transition_matrix[f.vol_state][f.vol_state])

    def test_v1_is_byte_identical_to_main(self) -> None:
        """Golden: the rollback path serialises exactly as the pre-v2 code did."""
        s = _fixture()
        golden = (FIXTURES / "golden_v1_snapshot.json").read_text()
        for kw in (None, {"model": "v1"}, regime_kwargs(ArcSettings(regime_model="v1"))):
            snap = build_snapshot("SPY", s, s.index[-1], regime_kwargs=kw)
            dump = snap.model_dump(mode="json")
            assert dump.pop("technicals") is None  # E16.2 additive field (closes only: none)
            text = json.dumps(dump, indent=2, sort_keys=True) + "\n"
            assert text == golden

    def test_backtest_vol_label_is_the_features_one(self) -> None:
        from arc.backtest import regime as bt

        assert bt.label_vol is label_vol
        s = _fixture()
        out = label_vol(s)
        c = list(s)
        assert (out.iloc[:20] == "unknown").all()
        for t in range(20, len(c)):
            rv = _ref_rv(c, t)
            assert out.iloc[t] == ("low" if rv < 0.12 else "high" if rv >= 0.20 else "mid")


# ---------------------------------------------------------------------------
# Units
# ---------------------------------------------------------------------------


class TestUnits:
    @pytest.mark.parametrize(
        ("z", "expected"),
        [(1.0, Regime.BULL), (0.999, Regime.SIDEWAYS), (-1.0, Regime.BEAR), (0.0, Regime.SIDEWAYS)],
    )
    def test_classify_z(self, z: float, expected: Regime) -> None:
        assert classify_z(z) is expected

    def test_classify_z_rejects_bad_threshold(self) -> None:
        with pytest.raises(ValueError, match="trend_z"):
            classify_z(0.5, trend_z=0.0)

    def test_percentile_rank(self) -> None:
        assert percentile_rank([1, 2, 3, 4], 4) == pytest.approx(87.5)
        assert percentile_rank([5, 5, 5], 5) == 50.0
        assert percentile_rank([1, 2, 3], 0) == 0.0
        with pytest.raises(ValueError, match="non-empty"):
            percentile_rank([], 1.0)

    @pytest.mark.parametrize(
        ("pct", "state"),
        [(0.0, VolState.LOW), (33.3, VolState.LOW), (33.34, VolState.MID), (66.7, VolState.HIGH)],
    )
    def test_vol_state_from_pct(self, pct: float, state: VolState) -> None:
        assert vol_state_from_pct(pct) is state

    @pytest.mark.parametrize(
        ("rv", "state"), [(0.1199, VolState.LOW), (0.12, VolState.MID), (0.20, VolState.HIGH)]
    )
    def test_vol_state_fixed(self, rv: float, state: VolState) -> None:
        assert vol_state_fixed(rv) is state

    def test_run_length_and_margin(self) -> None:
        assert run_length(["a"]) == 1
        assert run_length(["b", "a", "a", "a"]) == 3
        with pytest.raises(ValueError, match="non-empty"):
            run_length([])
        assert margin_to_threshold(0.4) == pytest.approx(0.6)
        assert margin_to_threshold(1.3) == pytest.approx(0.3)
        assert margin_to_threshold(-0.9) == pytest.approx(0.1)

    def test_bad_inputs(self) -> None:
        s = _gbm(200)
        with pytest.raises(ValueError, match="vol_window"):
            vol_scaled_z(s, vol_window=1)
        with pytest.raises(ValueError, match="rank_window"):
            label_vol_v2(s, rank_window=1)
        with pytest.raises(ValueError, match="positive"):
            vol_scaled_z(_closes([1.0, 0.0, 2.0]))
        with pytest.raises(ValueError, match="alpha"):
            estimate_regime(s, s.index[-1], model="v2", alpha=-1)
        with pytest.raises(ValueError, match="step"):
            estimate_regime(s, s.index[-1], model="v2", step=0)
        with pytest.raises(ValueError, match="fit_window"):
            estimate_regime(s, s.index[-1], model="v2", fit_window=1)
        with pytest.raises(ValueError, match="unknown regime model"):
            estimate_regime(s, s.index[-1], model="v3")  # type: ignore[arg-type]

    def test_short_history(self) -> None:
        assert vol_scaled_z(_gbm(60)).empty
        s = _gbm(61)  # one z label, no transition yet
        assert len(vol_scaled_z(s)) == 1
        with pytest.raises(InsufficientHistoryError, match="regime v2"):
            estimate_regime(s, s.index[-1], model="v2")
        f = estimate_regime(_gbm(62), _gbm(62).index[-1], model="v2")
        assert f.n_transitions == 1 and f.vol_label_source == "fixed"
        assert f.rv20_pct_rank is None and f.vol_state is not None

    def test_vol_scaled_labels_are_comparable_across_vol(self) -> None:
        """The same z path labels the same at 12% and 60% annual vol (D77's point)."""
        rng = np.random.default_rng(3)
        shocks = rng.normal(0, 1, 400)
        lo = _closes(100 * np.exp(np.cumsum(shocks * 0.12 / np.sqrt(252))))
        hi = _closes(100 * np.exp(np.cumsum(shocks * 0.60 / np.sqrt(252))))
        a = [x.value for x in label_regimes_v2(lo)]
        b = [x.value for x in label_regimes_v2(hi)]
        agree = sum(x == y for x, y in zip(a, b, strict=True)) / len(a)
        assert agree > 0.9  # noqa: PLR2004 - log vs simple return differ only at the edges


# ---------------------------------------------------------------------------
# Properties (hypothesis)
# ---------------------------------------------------------------------------

_series = st.lists(
    st.floats(min_value=-0.08, max_value=0.08, allow_nan=False), min_size=90, max_size=330
).map(lambda r: _closes(100.0 * np.exp(np.cumsum([0.0, *r]))))


class TestProperties:
    @settings(max_examples=40, deadline=None)
    @given(s=_series, tail=st.lists(st.floats(0.1, 1e4), min_size=1, max_size=30), cut=st.data())
    def test_no_lookahead(self, s: pd.Series, tail: list[float], cut: st.DataObject) -> None:
        k = cut.draw(st.integers(min_value=62, max_value=len(s) - 1))
        as_of = s.index[k]
        base = estimate_regime(s, as_of, **V2)
        mutated = s.copy()
        mutated.iloc[k + 1 :] = mutated.iloc[k + 1 :] * 3.0
        appended = pd.concat([s, _closes(tail, start=s.index[-1] + dt.timedelta(days=3))])
        assert estimate_regime(mutated, as_of, **V2) == base
        assert estimate_regime(appended, as_of, **V2) == base

    @settings(max_examples=40, deadline=None)
    @given(s=_series, scale=st.sampled_from([0.01, 0.5, 3.0, 1000.0]))
    def test_scale_invariant_labels(self, s: pd.Series, scale: float) -> None:
        a = estimate_regime(s, s.index[-1], **V2)
        b = estimate_regime(s * scale, s.index[-1], **V2)
        assert a.current == b.current and a.vol_state == b.vol_state
        assert a.run_length == b.run_length
        assert [x.value for x in label_regimes_v2(s)] == [
            x.value for x in label_regimes_v2(s * scale)
        ]

    @settings(max_examples=40, deadline=None)
    @given(s=_series)
    def test_bounds_and_stochastic(self, s: pd.Series) -> None:
        f = estimate_regime(
            s, s.index[-1], model="v2", fit_window=252, alpha=0.5, vol_rank_window=60
        )
        assert f.rv20_pct_rank is None or 0.0 <= f.rv20_pct_rank <= 100.0
        assert all(p is None or 0.0 <= p <= 100.0 for p in label_vol_v2(s, rank_window=60).pct_rank)
        for row in f.transition_matrix.values():
            assert sum(row.values()) == pytest.approx(1.0) and min(row.values()) >= 0
        assert f.vol_transition_matrix is not None
        for row in f.vol_transition_matrix.values():
            assert sum(row.values()) == pytest.approx(1.0) and min(row.values()) >= 0
        assert f.margin_z is not None and f.margin_z >= 0
        assert f.run_length is not None and f.run_length >= 1

    @pytest.mark.parametrize("n", [62, 100, 300])
    def test_flat_series_is_sideways_without_dividing_by_zero(self, n: int) -> None:
        s = _closes([50.0] * n)
        with np.errstate(all="raise"):
            f = estimate_regime(s, s.index[-1], **V2)
        assert f.current is Regime.SIDEWAYS and f.z == 0.0
        assert f.rv20 == 0.0 and f.margin_z == pytest.approx(1.0)
        assert f.run_length == n - 60
        assert all(math.isfinite(x) for x in f.stationary.values())


# ---------------------------------------------------------------------------
# Serialisation, context kind, settings, rendering
# ---------------------------------------------------------------------------


class TestWiring:
    def test_v1_dump_omits_v2_fields_and_v2_dump_has_them(self) -> None:
        s = _fixture()
        v1 = estimate_regime(s, s.index[-1]).model_dump(mode="json")
        v2 = estimate_regime(s, s.index[-1], **V2).model_dump(mode="json")
        assert not set(V2_FIELDS) & set(v1)
        assert set(V2_FIELDS) <= set(v2) and v2["model"] == "v2"
        assert RegimeFeatures.model_validate(v2) == estimate_regime(s, s.index[-1], **V2)

    def test_context_kind_v3_round_trip_and_v2_rows_load(self) -> None:
        assert KINDS["regime"].schema_version >= 3  # noqa: PLR2004 - E16.2 made it v4
        s = _fixture()
        snap = build_snapshot("SPY", s, s.index[-1], regime_kwargs=V2)
        back = validate_payload("regime", RegimePayload.model_validate(snap.model_dump()))
        assert back.model_dump(mode="json") == snap.model_dump(mode="json")
        stored = json.loads((FIXTURES / "stored_regime_v2_spy.json").read_text())
        old = validate_payload("regime", stored)
        assert isinstance(old, RegimePayload) and old.regime is not None
        assert old.regime.model == "v1" and old.regime.z is None
        assert old.model_dump(mode="json")["regime"] == stored["regime"]

    def test_settings_defaults_and_kwargs(self) -> None:
        s = ArcSettings()
        assert s.regime_model == "v2"
        assert regime_kwargs(s) == {
            "model": "v2",
            "trend_z": 1.0,
            "vol_scale_window": 60,
            "vol_rank_window": 252,
            "fit_window": 252,
            "alpha": 0.5,
        }
        assert regime_kwargs(ArcSettings(regime_model="v1")) == {"model": "v1"}
        assert regime_history_days(ArcSettings(regime_model="v1")) == 400  # noqa: PLR2004
        # 60 + 252 + 1 sessions -> 470 calendar days; never below the v1 fetch
        assert regime_history_days(s) == 470  # noqa: PLR2004
        # a short fit window: the 252-session vol rank window (+21) sets the fetch
        assert regime_history_days(ArcSettings(regime_fit_window=20)) == 410  # noqa: PLR2004
        small = ArcSettings(regime_fit_window=20, regime_vol_rank_window=60)
        assert regime_history_days(small) == 400  # noqa: PLR2004

    def test_registry_entries(self) -> None:
        from arc.control.registry import REGISTRY

        assert REGISTRY["regime.model"].choices == ("v1", "v2")
        assert REGISTRY["regime.trend_z"].field == "regime_trend_z"
        for k in ("vol_scale_window", "vol_rank_window", "fit_window", "alpha"):
            assert REGISTRY[f"regime.{k}"].field == f"regime_{k}"

    def test_regime_line_v2_and_v1(self) -> None:
        s = _fixture()
        snap = build_snapshot("SPY", s, s.index[-1], regime_kwargs=V2).model_dump(mode="json")
        line = regime_line("SPY", snap)
        assert line.startswith("SPY · sideways z+0.0 (run 20d) · vol mid (p34) · 5d bull ")
        assert "(stick" not in line and "close 771.35" in line
        v1 = json.loads((FIXTURES / "golden_v1_snapshot.json").read_text())
        assert regime_line("SPY", v1) == (
            "SPY · sideways (stick 0.98) · 5d bull 0.05/side 0.94/bear 0.02 · ret20 +0.0% · "
            "iv n/a hv20 0.11 iv/hv20 n/a · ivr n/a · close 771.35"
        )

    def test_regime_line_warm_up_shows_fixed_bucket(self) -> None:
        s = _gbm(120, vol=0.4)
        snap = build_snapshot("NEW", s, s.index[-1], regime_kwargs=V2).model_dump(mode="json")
        line = regime_line("NEW", snap)
        assert " fixed) · " in line and "vol " in line
