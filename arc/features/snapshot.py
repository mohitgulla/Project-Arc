"""FeatureSnapshot: per-ticker, per-day structured inputs for Research / Quant.

A snapshot bundles :class:`~arc.features.regime.RegimeFeatures` and
:class:`~arc.features.vol.VolFeatures` for one underlying at one session
close. It is pure data (pydantic, JSON-serialisable) and carries no prompt
text; persona builders serialise it with :func:`snapshots_to_json` into
``ResearchInput.regime_features_json``.

Building a snapshot never looks past ``as_of``: every input series is
truncated first, so the same call gives the same result whether it runs
live or in a backtest with data that extends past ``as_of``.
"""

from __future__ import annotations

import datetime as dt  # noqa: TC003 — used at runtime in pydantic models
import json
from typing import TYPE_CHECKING, Literal

import structlog
from pydantic import BaseModel, Field

from arc.features._series import (
    InsufficientHistoryError,
    closes_from_bars,
    to_daily_series,
    truncate,
)
from arc.features.regime import RegimeFeatures, estimate_regime
from arc.features.vol import MIN_IV_HISTORY, VolFeatures, compute_vol_features

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    import pandas as pd

    from arc.features._series import BarLike

log = structlog.get_logger(__name__)

SCHEMA_VERSION: Literal[1] = 1


class FeatureSnapshot(BaseModel):
    """Regime + volatility features for one ticker as of one session close."""

    schema_version: Literal[1] = SCHEMA_VERSION
    ticker: str = Field(..., description="Underlying symbol, e.g. 'SPY'")
    as_of: dt.date = Field(..., description="Session date (ET); uses data up to that close only")
    last_close: float | None = None
    regime: RegimeFeatures | None = Field(None, description="None if history is too short")
    vol: VolFeatures
    warnings: list[str] = Field(default_factory=list)

    @property
    def is_complete(self) -> bool:
        """True when regime and every vol field were computable."""
        return self.regime is not None and not self.vol.missing


def build_snapshot(
    ticker: str,
    closes: pd.Series,
    as_of: dt.date,
    *,
    iv_history: pd.Series | None = None,
    current_iv: float | None = None,
    regime_kwargs: Mapping[str, object] | None = None,
    min_iv_obs: int = MIN_IV_HISTORY,
) -> FeatureSnapshot:
    """Build a :class:`FeatureSnapshot` from a daily close series (and optional IV)."""
    series = to_daily_series(closes, name="close")
    upto = truncate(series, as_of)
    warnings: list[str] = []

    regime: RegimeFeatures | None = None
    try:
        regime = estimate_regime(series, as_of, **dict(regime_kwargs or {}))  # type: ignore[arg-type]
    except InsufficientHistoryError as exc:
        warnings.append(f"regime: {exc}")

    vol = compute_vol_features(
        series, as_of, iv_history=iv_history, current_iv=current_iv, min_iv_obs=min_iv_obs
    )
    warnings.extend(f"vol: {m}" for m in vol.missing)

    last_close = float(upto.iloc[-1]) if len(upto) else None
    if len(upto) and upto.index[-1] != as_of:
        warnings.append(f"no close on {as_of}; latest close is {upto.index[-1]}")

    snap = FeatureSnapshot(
        ticker=ticker.upper(),
        as_of=as_of,
        last_close=last_close,
        regime=regime,
        vol=vol,
        warnings=warnings,
    )
    log.debug(
        "features.snapshot",
        ticker=snap.ticker,
        as_of=str(as_of),
        regime=regime.current if regime else None,
        complete=snap.is_complete,
    )
    return snap


def build_snapshot_from_bars(
    ticker: str,
    bars: Iterable[BarLike],
    as_of: dt.date,
    *,
    iv_history: pd.Series | None = None,
    current_iv: float | None = None,
    regime_kwargs: Mapping[str, object] | None = None,
    min_iv_obs: int = MIN_IV_HISTORY,
) -> FeatureSnapshot:
    """:func:`build_snapshot` from OHLCV bars (e.g. ``MarketDataProvider.history_bars``)."""
    return build_snapshot(
        ticker,
        closes_from_bars(bars),
        as_of,
        iv_history=iv_history,
        current_iv=current_iv,
        regime_kwargs=regime_kwargs,
        min_iv_obs=min_iv_obs,
    )


def snapshots_to_json(snapshots: Iterable[FeatureSnapshot], *, indent: int | None = 2) -> str:
    """Serialise snapshots as a JSON object keyed by ticker (Research input)."""
    payload = {s.ticker: s.model_dump(mode="json") for s in snapshots}
    return json.dumps(payload, indent=indent, sort_keys=True)
