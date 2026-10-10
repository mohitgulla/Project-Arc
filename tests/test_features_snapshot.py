"""Tests for arc.features.snapshot — FeatureSnapshot assembly and serialisation."""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass

import numpy as np
import pandas as pd
import pytest

from arc.features import (
    FeatureSnapshot,
    Regime,
    build_snapshot,
    build_snapshot_from_bars,
    snapshots_to_json,
)
from arc.features._series import closes_from_bars, to_daily_series, truncate
from arc.utils.calendar import ET


def _closes(n: int, seed: int = 11) -> pd.Series:
    rng = np.random.default_rng(seed)
    idx = [d.date() for d in pd.bdate_range("2025-01-02", periods=n)]
    return pd.Series(100 * np.exp(np.cumsum(rng.normal(0, 0.012, n))), index=idx)


@dataclass
class _Bar:
    timestamp: dt.datetime
    close: float


class TestBuildSnapshot:
    def test_complete_snapshot(self) -> None:
        closes = _closes(300)
        as_of = closes.index[-1]
        iv = pd.Series(np.linspace(0.15, 0.25, 300), index=closes.index)
        snap = build_snapshot("spy", closes, as_of, iv_history=iv)
        assert snap.ticker == "SPY"
        assert snap.is_complete
        assert snap.warnings == []
        assert snap.last_close == pytest.approx(float(closes.iloc[-1]))
        assert snap.regime is not None and snap.regime.current in set(Regime)
        assert snap.vol.iv_rank == 1.0
        assert FeatureSnapshot.model_validate_json(snap.model_dump_json()) == snap

    def test_short_history_degrades_gracefully(self) -> None:
        closes = _closes(15)
        snap = build_snapshot("AAPL", closes, closes.index[-1])
        assert snap.regime is None
        assert not snap.is_complete
        assert any(w.startswith("regime:") for w in snap.warnings)
        assert any(w.startswith("vol:") for w in snap.warnings)

    def test_no_close_on_as_of_warns(self) -> None:
        closes = _closes(100)
        saturday = next(d for d in closes.index if d.weekday() == 4) + dt.timedelta(days=1)
        snap = build_snapshot("QQQ", closes, saturday)
        assert any("no close on" in w for w in snap.warnings)

    def test_empty_closes(self) -> None:
        snap = build_snapshot("X", pd.Series(dtype=float), dt.date(2025, 1, 2))
        assert snap.last_close is None and snap.regime is None

    def test_no_look_ahead(self) -> None:
        closes = _closes(300)
        iv = pd.Series(np.linspace(0.15, 0.25, 300), index=closes.index)
        as_of = closes.index[200]
        base = build_snapshot("SPY", closes, as_of, iv_history=iv)
        c2, iv2 = closes.copy(), iv.copy()
        c2.iloc[201:] *= 0.5
        iv2.iloc[201:] = 0.9
        assert build_snapshot("SPY", c2, as_of, iv_history=iv2) == base

    def test_regime_kwargs_passed(self) -> None:
        closes = _closes(300)
        snap = build_snapshot("SPY", closes, closes.index[-1], regime_kwargs={"fit_window": 50})
        assert snap.regime is not None and snap.regime.n_transitions == 49


class TestBars:
    def test_from_bars_uses_et_date(self) -> None:
        closes = _closes(80)
        # Alpaca daily bars are stamped at 04:00/05:00 UTC -> previous-evening ET risk;
        # use 20:00 UTC (16:00 ET) so the ET date equals the session date.
        bars = [
            _Bar(dt.datetime.combine(d, dt.time(20, 0), tzinfo=dt.UTC), float(c))
            for d, c in closes.items()
        ]
        s = closes_from_bars(bars)
        assert list(s.index) == list(closes.index)
        snap = build_snapshot_from_bars("iwm", bars, closes.index[-1])
        # close-only bars: no technicals (E16.2), flagged as a warning, otherwise identical
        assert snap.technicals is None and snap.warnings[-1].startswith("technicals:")
        plain = build_snapshot("IWM", closes, closes.index[-1])
        assert snap.model_copy(update={"warnings": plain.warnings}) == plain

    def test_utc_midnight_maps_to_prior_et_date(self) -> None:
        bar = _Bar(dt.datetime(2025, 3, 4, 3, 0, tzinfo=dt.UTC), 1.0)
        assert closes_from_bars([bar]).index[0] == dt.date(2025, 3, 3)


class TestSeriesHelpers:
    def test_dedupe_and_sort(self) -> None:
        idx = [dt.date(2025, 1, 3), dt.date(2025, 1, 2), dt.date(2025, 1, 3)]
        s = to_daily_series(pd.Series([1.0, 2.0, 3.0], index=idx))
        assert list(s.index) == [dt.date(2025, 1, 2), dt.date(2025, 1, 3)]
        assert s.iloc[-1] == 3.0

    def test_timestamp_and_naive_datetime_index(self) -> None:
        idx = [pd.Timestamp("2025-01-02 10:00", tz=ET), dt.datetime(2025, 1, 3, 9, 0)]
        s = to_daily_series(pd.Series([1.0, 2.0], index=idx))
        assert list(s.index) == [dt.date(2025, 1, 2), dt.date(2025, 1, 3)]

    def test_bad_index(self) -> None:
        with pytest.raises(TypeError):
            to_daily_series(pd.Series([1.0], index=["x"]))

    def test_truncate_empty(self) -> None:
        assert truncate(pd.Series(dtype=float), dt.date(2025, 1, 1)).empty


class TestJson:
    def test_snapshots_to_json_keyed_by_ticker(self) -> None:
        closes = _closes(120)
        snaps = [build_snapshot(t, closes, closes.index[-1]) for t in ("SPY", "QQQ")]
        payload = json.loads(snapshots_to_json(snaps))
        assert set(payload) == {"SPY", "QQQ"}
        assert payload["SPY"]["schema_version"] == 1
        assert "transition_matrix" in payload["SPY"]["regime"]
        assert payload["SPY"]["vol"]["hv20"] is not None

    def test_research_input_accepts_snapshot_json(self) -> None:
        from arc.personas.builders import ResearchInput, build_research_prompt

        closes = _closes(120)
        js = snapshots_to_json([build_snapshot("SPY", closes, closes.index[-1])])
        prompt = build_research_prompt(
            ResearchInput(
                candidates_json="[]",
                regime_features_json=js,
                portfolio_summary="flat",
                scan_date=str(closes.index[-1]),
            )
        )
        assert '"SPY"' in prompt and "stickiness" in prompt
