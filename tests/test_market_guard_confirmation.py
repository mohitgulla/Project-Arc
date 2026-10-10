"""E17.2 (D77): the D33 transitional check on SPY confirmation instead of stickiness.

Paths: flip day (run < min), near-threshold (margin z < min), confirmed, legacy
fallback (v1 / old rows without run_length / margin_z), missing regime. Plus a
2-year replay over real SPY closes (``tests/fixtures/regime/spy_daily_closes_2y.csv``,
the backtest cache's raw daily closes 2023-12-04 .. 2026-09-25).
"""

from __future__ import annotations

import csv
import datetime as dt
import json
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from arc.config import ArcSettings
from arc.context.store import ContextStore
from arc.context.ttl import to_db
from arc.control.registry import Direction, Risk, direction, lookup
from arc.features.regime import classify_z, margin_to_threshold, vol_scaled_z
from arc.pipeline import FIXTURE_NOW
from arc.pipeline.market_guard import SpyRegime, market_guard, transitional_reason
from arc.pipeline.runner import open_db
from arc.positions.portfolio import MarketGuard

FIXTURE_2Y = Path(__file__).parent / "fixtures" / "regime" / "spy_daily_closes_2y.csv"
REPLAY_SESSIONS = 504  # ~2 years


def _settings(**kw: object) -> ArcSettings:
    base: dict[str, object] = {"_env_file": None, "no_trade_require_vix": False}
    base.update(kw)
    return ArcSettings(**base)  # type: ignore[arg-type]


def _guard(regime: dict[str, Any] | None, settings: ArcSettings | None = None) -> MarketGuard:
    conn = open_db(":memory:", copy=False)
    if regime is not None:
        conn.execute(
            "INSERT INTO context_entries (id, kind, subject, payload, schema_version, "
            "produced_by, created_at, valid_from, status) VALUES (?, 'regime', 'SPY', ?, 2, "
            "'test', ?, ?, 'active')",
            ("ctx-regime-e172", json.dumps({"regime": regime}), to_db(FIXTURE_NOW),
             to_db(FIXTURE_NOW)),
        )  # fmt: skip
        conn.commit()
    snap = ContextStore(conn).snapshot(FIXTURE_NOW)
    return market_guard(snap, settings or _settings(), now=FIXTURE_NOW)


V2 = {"model": "v2", "current": "sideways", "stickiness": 0.98}


class TestConfirmationGuard:
    def test_flip_day_blocks(self) -> None:
        g = _guard({**V2, "run_length": 1, "margin_z": 0.60})
        assert not g.opens_allowed and g.reason_code == "market_unclear"
        assert g.regime_check == "confirmation"
        assert g.reasons == ["SPY regime sideways transitional (run 1d < 3, margin z 0.60 >= 0.25)"]
        assert g.regime_run_length == 1 and g.regime_margin_z == pytest.approx(0.60)

    def test_near_threshold_blocks(self) -> None:
        g = _guard({**V2, "run_length": 9, "margin_z": 0.12})
        assert not g.opens_allowed
        assert g.reasons == ["SPY regime sideways transitional (run 9d >= 3, margin z 0.12 < 0.25)"]

    def test_both_conditions_card_text(self) -> None:
        g = _guard({**V2, "run_length": 1, "margin_z": 0.12})
        assert g.reasons == ["SPY regime sideways transitional (run 1d < 3, margin z 0.12 < 0.25)"]

    def test_confirmed_allows(self) -> None:
        g = _guard({**V2, "stickiness": 0.10, "run_length": 3, "margin_z": 0.25})
        # the legacy stickiness test is NOT applied to a v2 entry
        assert g.opens_allowed and g.reasons == [] and g.regime_check == "confirmation"
        assert "regime" in g.checked

    def test_thresholds_come_from_settings(self) -> None:
        reg = {**V2, "run_length": 4, "margin_z": 0.30}
        assert _guard(reg).opens_allowed
        assert not _guard(reg, _settings(regime_guard_min_run=5)).opens_allowed
        assert not _guard(reg, _settings(regime_guard_min_margin_z=0.5)).opens_allowed

    def test_legacy_fallback_blocks_and_allows(self) -> None:
        low = _guard({"current": "sideways", "stickiness": 0.40})
        assert not low.opens_allowed and low.regime_check == "legacy"
        assert low.reasons == ["SPY regime sideways transitional (stickiness 0.40 < 0.55)"]
        assert low.regime_run_length is None and low.regime_margin_z is None
        high = _guard({"current": "bull", "stickiness": 0.98})
        assert high.opens_allowed and high.regime_check == "legacy"

    def test_legacy_when_only_one_field(self) -> None:
        g = _guard({"current": "bull", "stickiness": 0.40, "run_length": 5})
        assert g.regime_check == "legacy" and not g.opens_allowed

    def test_legacy_without_stickiness_allows(self) -> None:
        g = _guard({"current": "bull"})
        assert g.opens_allowed and g.regime_check == "legacy" and g.regime_stickiness is None

    @pytest.mark.parametrize("regime", [None, {}, {"current": ""}, {"current": None}])
    def test_missing_regime_skips_check(self, regime: dict[str, Any] | None) -> None:
        g = _guard(regime)
        assert g.opens_allowed and g.regime is None and g.regime_check is None
        assert "regime" not in g.checked

    def test_non_dict_regime_skips(self) -> None:
        conn = open_db(":memory:", copy=False)
        conn.execute(
            "INSERT INTO context_entries (id, kind, subject, payload, schema_version, "
            "produced_by, created_at, valid_from, status) VALUES ('x', 'regime', 'SPY', ?, 1, "
            "'test', ?, ?, 'active')",
            (json.dumps({"regime": "bull"}), to_db(FIXTURE_NOW), to_db(FIXTURE_NOW)),
        )
        conn.commit()
        g = market_guard(ContextStore(conn).snapshot(FIXTURE_NOW), _settings(), now=FIXTURE_NOW)
        assert g.regime is None and g.opens_allowed

    def test_malformed_numbers_fall_back(self) -> None:
        g = _guard({"current": "bull", "stickiness": "x", "run_length": True, "margin_z": "y"})
        assert g.regime_check == "legacy" and g.opens_allowed
        assert g.regime_stickiness is None and g.regime_run_length is None

    def test_missing_vix_still_fails_closed(self) -> None:
        g = _guard({**V2, "run_length": 10, "margin_z": 0.9}, _settings(no_trade_require_vix=True))
        assert not g.opens_allowed and g.reason_code == "market_data_missing"

    def test_old_guard_json_still_validates(self) -> None:
        """Stored shortlist payloads from before E17.2 carry no confirmation fields."""
        old = {"opens_allowed": True, "regime": "bull", "regime_stickiness": 0.9, "checked": []}
        g = MarketGuard.model_validate(old)
        assert g.regime_run_length is None and g.regime_check is None


class TestSettingsAndRegistry:
    def test_defaults(self) -> None:
        s = _settings()
        assert s.regime_guard_min_run == 3
        assert s.regime_guard_min_margin_z == pytest.approx(0.25)
        assert s.no_trade_transitional_min_confidence == pytest.approx(0.55)

    @pytest.mark.parametrize(
        ("key", "lo", "hi"),
        [("regime.guard_min_run", 1, 10), ("regime.guard_min_margin_z", 0.0, 1.0)],
    )
    def test_registry_bounds_and_direction(self, key: str, lo: float, hi: float) -> None:
        t = lookup(key)
        assert t.min == lo and t.max == hi and t.risk is Risk.DOWN
        # raising either is the safer direction
        assert direction(t, lo, hi) is Direction.SAFER
        assert direction(t, hi, lo) is Direction.RISKIER


# ---------------------------------------------------------------------------
# 2-year replay
# ---------------------------------------------------------------------------


def _closes() -> pd.Series:
    with FIXTURE_2Y.open() as fh:
        rows = list(csv.DictReader(fh))
    return pd.Series(
        [float(r["close"]) for r in rows],
        index=[dt.date.fromisoformat(r["date"]) for r in rows],
    )


def replay(settings: ArcSettings, sessions: int = REPLAY_SESSIONS) -> list[tuple[Any, bool]]:
    """(date, blocked) for the last *sessions* sessions under the confirmation rule."""
    z = vol_scaled_z(_closes(), vol_window=settings.regime_vol_scale_window)
    labels = [classify_z(float(v), trend_z=settings.regime_trend_z) for v in z]
    out: list[tuple[Any, bool]] = []
    run = 0
    for i, (d, zz) in enumerate(z.items()):
        run = run + 1 if i and labels[i - 1] == labels[i] else 1
        reg = SpyRegime(
            current=str(labels[i]),
            run_length=run,
            margin_z=margin_to_threshold(float(zz), settings.regime_trend_z),
        )
        check, why = transitional_reason(reg, settings)
        assert check == "confirmation"
        out.append((d, why is not None))
    return out[-sessions:]


def test_replay_fixture_covers_two_years() -> None:
    assert len(replay(_settings())) == REPLAY_SESSIONS


def test_replay_guard_fires_sometimes_but_not_most_days() -> None:
    rows = replay(_settings())
    share = sum(b for _, b in rows) / len(rows)
    assert 0 < share < 0.25, f"blocked share {share:.1%}"
