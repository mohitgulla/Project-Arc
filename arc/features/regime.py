"""Markov 3-state market regime (bear / sideways / bull).

Method (PLAN.md §4 E4.3, §7 "Markov regime"):

1. **Labelling.** Day *t* is labelled from the trailing 20-session return
   ``r_t = close_t / close_{t-20} - 1``: ``bull`` if ``r_t >= +5%``,
   ``bear`` if ``r_t <= -5%``, otherwise ``sideways``. The label uses only
   closes up to and including *t*, so it is known at the close of *t*.
2. **Transition matrix.** ``P[i, j]`` = share of observed transitions
   ``label_{t} -> label_{t+step}`` that go from state *i* to state *j*,
   estimated only from labels dated on or before ``as_of``.
3. **Derived quantities.** Stickiness (``P[i, i]``), expected run length
   (``1 / (1 - P[i, i])``), n-step forecast (row of ``P^n`` for the current
   state) and the stationary distribution.

Walk-forward safety: :func:`estimate_regime` truncates every input to
``as_of`` before doing anything else, so appending or altering data after
``as_of`` cannot change its output (property-tested).

State order in every matrix / vector is :data:`STATES` = (bear, sideways, bull).

**Regime v2 (PLAN D77, E17.1)** — ``estimate_regime(..., model="v2")``:

1. **Vol-scaled trend.** ``z_t = r20_t / (sigma_t * sqrt(20))`` where ``sigma_t`` is
   the sample stdev of daily log returns over the trailing ``vol_scale_window`` (60)
   sessions ending at *t*. ``bull`` if ``z >= +trend_z`` (1.0), ``bear`` if
   ``z <= -trend_z``, else ``sideways``. A flat window (``sigma = 0``) gives ``z = 0``.
2. **Vol state** (:class:`VolState`). ``rv20`` = annualised 20-session realised vol;
   its percentile rank within the ticker's own trailing ``vol_rank_window`` (252)
   ``rv20`` values gives ``low`` (< 33.3), ``mid`` or ``high`` (>= 66.7). Before a full
   window exists, the fixed 12 % / 20 % buckets apply (``vol_label_source: fixed``).
3. **Rolling fit.** Trend and vol chains are fit on the last ``fit_window`` (252)
   labels with Laplace ``alpha`` (0.5).
4. **Confirmation.** ``run_length`` = sessions the current trend label has held
   (>= 1); ``margin_z`` = distance from ``z`` to the nearest threshold (>= 0).

v1 (the original return-threshold model) is the rollback path; its serialised
output is byte-identical to the pre-v2 code (golden-tested).
"""

from __future__ import annotations

import datetime as dt  # noqa: TC003 — used at runtime in pydantic models
import math
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Literal

import numpy as np
import pandas as pd
from pydantic import BaseModel, Field, SerializerFunctionWrapHandler, model_serializer

from arc.features._series import InsufficientHistoryError, to_daily_series, truncate

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

LOOKBACK_DAYS = 20
BULL_THRESHOLD = 0.05
BEAR_THRESHOLD = -0.05
DEFAULT_HORIZONS: tuple[int, ...] = (1, 5, 20)

# v2 defaults (PLAN D77); live values come from ``ArcSettings.regime_*``.
RegimeModel = Literal["v1", "v2"]
REGIME_MODELS: tuple[str, ...] = ("v1", "v2")
V2_TREND_Z = 1.0
V2_VOL_SCALE_WINDOW = 60
V2_VOL_RANK_WINDOW = 252
V2_FIT_WINDOW = 252
V2_ALPHA = 0.5

# Fixed realised-vol buckets (annualised): the backtest's vol regime and the v2
# warm-up fallback. One implementation; ``arc.backtest.regime`` imports it.
VOL_LOW = 0.12
VOL_HIGH = 0.20
# Percentile-rank cut-offs for the per-ticker vol state.
VOL_PCT_LOW = 100.0 / 3.0
VOL_PCT_HIGH = 200.0 / 3.0
TRADING_DAYS = 252.0
_FLAT_SIGMA = 1e-12


class Regime(StrEnum):
    """Market regime label."""

    BEAR = "bear"
    SIDEWAYS = "sideways"
    BULL = "bull"


class VolState(StrEnum):
    """Realised-vol state (v2)."""

    LOW = "low"
    MID = "mid"
    HIGH = "high"


STATES: tuple[Regime, ...] = (Regime.BEAR, Regime.SIDEWAYS, Regime.BULL)
_INDEX = {s: i for i, s in enumerate(STATES)}
N_STATES = len(STATES)
VOL_STATES: tuple[VolState, ...] = (VolState.LOW, VolState.MID, VolState.HIGH)
_VOL_INDEX = {s: i for i, s in enumerate(VOL_STATES)}


# ---------------------------------------------------------------------------
# Labelling
# ---------------------------------------------------------------------------


def classify_return(
    r: float, *, bull: float = BULL_THRESHOLD, bear: float = BEAR_THRESHOLD
) -> Regime:
    """Map a trailing return to a regime (``>= bull`` bull, ``<= bear`` bear)."""
    if not bear < bull:
        raise ValueError("bear threshold must be below bull threshold")
    if r >= bull:
        return Regime.BULL
    if r <= bear:
        return Regime.BEAR
    return Regime.SIDEWAYS


def trailing_returns(closes: pd.Series, lookback: int = LOOKBACK_DAYS) -> pd.Series:
    """Trailing *lookback*-session simple returns; the first *lookback* rows are dropped."""
    if lookback < 1:
        raise ValueError("lookback must be >= 1")
    s = to_daily_series(closes, name="close")
    if (s <= 0).any():
        raise ValueError("closes must be strictly positive")
    return (s / s.shift(lookback) - 1.0).dropna().rename("trailing_return")


def label_regimes(
    closes: pd.Series,
    *,
    lookback: int = LOOKBACK_DAYS,
    bull: float = BULL_THRESHOLD,
    bear: float = BEAR_THRESHOLD,
) -> pd.Series:
    """Per-day regime labels (series of :class:`Regime`, indexed by date)."""
    rets = trailing_returns(closes, lookback)
    # dtype=object keeps Regime members intact: with pyarrow installed, pandas 3
    # infers an arrow string dtype for str-enum values and drops the enum type.
    out = rets.astype(object).rename("regime")
    out[:] = [classify_return(float(r), bull=bull, bear=bear) for r in rets]
    return out


# ---------------------------------------------------------------------------
# v2 labelling (D77): vol-scaled trend z, per-ticker realised-vol state
# ---------------------------------------------------------------------------


def _positive_closes(closes: pd.Series) -> pd.Series:
    s = to_daily_series(closes, name="close")
    if (s <= 0).any():
        raise ValueError("closes must be strictly positive")
    return s


def _window_std(values: np.ndarray, window: int) -> np.ndarray:
    """Sample stdev (ddof=1) of every full trailing *window*; computed per window, so a
    value never depends on data before its window (no running-sum drift)."""
    if len(values) < window:
        return np.empty(0)
    return np.lib.stride_tricks.sliding_window_view(values, window).std(axis=1, ddof=1)


def vol_scaled_z(
    closes: pd.Series,
    *,
    lookback: int = LOOKBACK_DAYS,
    vol_window: int = V2_VOL_SCALE_WINDOW,
) -> pd.Series:
    """``z_t = r_t / (sigma_t * sqrt(lookback))`` per date (warm-up rows dropped).

    ``r_t`` is the trailing *lookback*-session simple return and ``sigma_t`` the sample
    stdev of the *vol_window* daily log returns ending at *t*. A flat window
    (``sigma_t`` ~ 0) gives ``z = 0`` instead of dividing by zero.
    """
    if lookback < 1 or vol_window < 2:  # noqa: PLR2004 - a stdev needs two returns
        raise ValueError("lookback must be >= 1 and vol_window >= 2")
    s = _positive_closes(closes)
    c = s.to_numpy(dtype=float)
    first = max(lookback, vol_window)
    if len(c) <= first:
        return pd.Series([], index=s.index[:0], dtype=float, name="z")
    # log_ret[k] = ln(c[k+1] / c[k]); the ratio first, so a price rescale is exact.
    log_ret = np.log(c[1:] / c[:-1])
    sig = _window_std(log_ret, vol_window)  # sig[j] ends at close index j + vol_window
    idx = np.arange(first, len(c))
    sigma = sig[idx - vol_window]
    r = c[idx] / c[idx - lookback] - 1.0
    scale = sigma * math.sqrt(lookback)
    z = np.where(sigma > _FLAT_SIGMA, r / np.where(sigma > _FLAT_SIGMA, scale, 1.0), 0.0)
    return pd.Series(z, index=s.index[idx], name="z")


def classify_z(z: float, *, trend_z: float = V2_TREND_Z) -> Regime:
    """``bull`` if ``z >= +trend_z``, ``bear`` if ``z <= -trend_z``, else sideways."""
    if trend_z <= 0:
        raise ValueError("trend_z must be > 0")
    if z >= trend_z:
        return Regime.BULL
    if z <= -trend_z:
        return Regime.BEAR
    return Regime.SIDEWAYS


def label_regimes_v2(
    closes: pd.Series,
    *,
    lookback: int = LOOKBACK_DAYS,
    trend_z: float = V2_TREND_Z,
    vol_window: int = V2_VOL_SCALE_WINDOW,
) -> pd.Series:
    """Per-day v2 trend labels (series of :class:`Regime`) from :func:`vol_scaled_z`."""
    z = vol_scaled_z(closes, lookback=lookback, vol_window=vol_window)
    out = z.astype(object).rename("regime")
    out[:] = [classify_z(float(v), trend_z=trend_z) for v in z]
    return out


def realised_vol(closes: pd.Series, lookback: int = LOOKBACK_DAYS) -> pd.Series:
    """Annualised *lookback*-session realised vol of daily log returns (NaN in warm-up).

    Each value is the sample stdev of exactly its own window (no running-sum drift), so
    a flat window is exactly 0 and equal windows give equal values (stable ranks).
    """
    c = closes.to_numpy(dtype=float)
    out = np.full(len(c), np.nan)
    if len(c) > lookback:
        lr = np.log(c[1:] / c[:-1])
        out[lookback:] = _window_std(lr, lookback) * np.sqrt(TRADING_DAYS)
    return pd.Series(out, index=closes.index, name="rv")


def label_vol(closes: pd.Series, lookback: int = LOOKBACK_DAYS) -> pd.Series:
    """Fixed-bucket vol label per date: ``low`` < 12 % <= ``mid`` < 20 % <= ``high``.

    ``unknown`` during warm-up. The backtest's vol regime and the v2 warm-up fallback
    (before a ticker has a full percentile window) both use this one implementation.
    """
    rv = realised_vol(closes, lookback)
    out = np.where(rv < VOL_LOW, "low", np.where(rv >= VOL_HIGH, "high", "mid"))
    out = np.where(rv.isna(), "unknown", out)
    return pd.Series(out, index=closes.index, name="vol")


def percentile_rank(window: Sequence[float] | np.ndarray, value: float) -> float:
    """Percentile of *value* within *window*: ``100 * (#below + #equal / 2) / n``.

    Always in ``[0, 100]``; a constant window gives 50.
    """
    arr = np.asarray(window, dtype=float)
    if arr.size == 0:
        raise ValueError("window must be non-empty")
    below = float(np.sum(arr < value))
    equal = float(np.sum(arr == value))
    return 100.0 * (below + 0.5 * equal) / float(arr.size)


def vol_state_from_pct(pct: float) -> VolState:
    """``low`` below the 33.3rd percentile, ``high`` from the 66.7th, else ``mid``."""
    if pct < VOL_PCT_LOW:
        return VolState.LOW
    if pct >= VOL_PCT_HIGH:
        return VolState.HIGH
    return VolState.MID


def vol_state_fixed(rv: float) -> VolState:
    """Fixed 12 % / 20 % buckets (same cut-offs as :func:`label_vol`)."""
    if rv < VOL_LOW:
        return VolState.LOW
    if rv >= VOL_HIGH:
        return VolState.HIGH
    return VolState.MID


class VolLabels(BaseModel):
    """Per-day v2 vol read (internal; one entry per date with an rv20)."""

    rv: list[float]
    pct_rank: list[float | None]
    state: list[VolState]
    source: list[Literal["percentile", "fixed"]]
    dates: list[dt.date]


def label_vol_v2(
    closes: pd.Series,
    *,
    lookback: int = LOOKBACK_DAYS,
    rank_window: int = V2_VOL_RANK_WINDOW,
) -> VolLabels:
    """Per-day vol state: percentile of rv20 in its own trailing *rank_window* values.

    Dates before a full window fall back to the fixed buckets (``source: fixed``).
    """
    if rank_window < 2:  # noqa: PLR2004
        raise ValueError("rank_window must be >= 2")
    s = _positive_closes(closes)
    rv = realised_vol(s, lookback).dropna()
    vals = rv.to_numpy(dtype=float)
    pct: list[float | None] = []
    state: list[VolState] = []
    source: list[Literal["percentile", "fixed"]] = []
    for k, v in enumerate(vals):
        if k + 1 >= rank_window:
            p = percentile_rank(vals[k + 1 - rank_window : k + 1], v)
            pct.append(p)
            state.append(vol_state_from_pct(p))
            source.append("percentile")
        else:
            pct.append(None)
            state.append(vol_state_fixed(v))
            source.append("fixed")
    return VolLabels(
        rv=[float(v) for v in vals],
        pct_rank=pct,
        state=state,
        source=source,
        dates=list(rv.index),
    )


def run_length(labels: Sequence[object]) -> int:
    """Sessions the last label has held (>= 1 for a non-empty sequence)."""
    if not labels:
        raise ValueError("labels must be non-empty")
    last = labels[-1]
    n = 0
    for x in reversed(labels):
        if x != last:
            break
        n += 1
    return n


def margin_to_threshold(z: float, trend_z: float = V2_TREND_Z) -> float:
    """Distance from *z* to the nearest of ``+trend_z`` / ``-trend_z`` (>= 0)."""
    return min(abs(z - trend_z), abs(z + trend_z))


# ---------------------------------------------------------------------------
# Markov chain
# ---------------------------------------------------------------------------


def transition_counts(labels: Iterable[Regime], step: int = 1) -> np.ndarray:
    """3x3 count matrix of transitions ``labels[k] -> labels[k + step]``."""
    if step < 1:
        raise ValueError("step must be >= 1")
    seq = [_INDEX[Regime(x)] for x in labels]
    counts = np.zeros((N_STATES, N_STATES), dtype=float)
    for a, b in zip(seq[:-step], seq[step:], strict=True):
        counts[a, b] += 1.0
    return counts


def transition_matrix(labels: Iterable[Regime], *, step: int = 1, alpha: float = 0.0) -> np.ndarray:
    """Row-stochastic transition matrix estimated from *labels*.

    ``alpha`` is an additive (Laplace) pseudo-count per cell. A state that
    was never left (row total 0 after smoothing) gets a uniform row, so the
    result is always a valid stochastic matrix.
    """
    if alpha < 0:
        raise ValueError("alpha must be >= 0")
    counts = transition_counts(labels, step) + alpha
    totals = counts.sum(axis=1, keepdims=True)
    uniform = np.full((N_STATES, N_STATES), 1.0 / N_STATES)
    with np.errstate(invalid="ignore", divide="ignore"):
        probs = np.where(totals > 0, counts / np.where(totals > 0, totals, 1.0), uniform)
    return probs


def _check_stochastic(p: np.ndarray) -> None:
    if p.shape != (N_STATES, N_STATES):
        raise ValueError(f"transition matrix must be {N_STATES}x{N_STATES}")
    if (p < -1e-12).any() or not np.allclose(p.sum(axis=1), 1.0, atol=1e-9):
        raise ValueError("transition matrix must be row-stochastic")


def stickiness(p: np.ndarray) -> dict[Regime, float]:
    """Probability of staying in each state for one more step (``P[i, i]``)."""
    _check_stochastic(p)
    return {s: float(p[i, i]) for i, s in enumerate(STATES)}


def expected_duration(p: np.ndarray) -> dict[Regime, float | None]:
    """Expected run length in steps, ``1 / (1 - P[i, i])``; ``None`` if absorbing."""
    _check_stochastic(p)
    out: dict[Regime, float | None] = {}
    for i, s in enumerate(STATES):
        leave = 1.0 - float(p[i, i])
        out[s] = None if leave <= 1e-12 else 1.0 / leave
    return out


def forecast(p: np.ndarray, current: Regime, n: int) -> np.ndarray:
    """Distribution over states *n* steps ahead, starting from *current*."""
    _check_stochastic(p)
    if n < 0:
        raise ValueError("n must be >= 0")
    start = np.zeros(N_STATES)
    start[_INDEX[Regime(current)]] = 1.0
    return start @ np.linalg.matrix_power(p, n)


def stationary_distribution(
    p: np.ndarray, *, tol: float = 1e-13, max_squarings: int = 64
) -> np.ndarray:
    """Stationary distribution ``pi`` with ``pi P = pi``, ``sum(pi) = 1``.

    Computed as ``lim_k u Q^k`` for the lazy chain ``Q = (I + P) / 2`` from
    the uniform start ``u``. ``Q`` has exactly the stationary distributions
    of ``P`` and is aperiodic, so the limit exists for every stochastic
    matrix (periodic or reducible). When ``P`` is irreducible the result is
    the unique stationary distribution; otherwise it is the long-run
    occupancy from a uniform start. Uses repeated squaring, so convergence
    is fast even for very sticky chains.

    Each squaring re-projects ``Q`` onto the row-stochastic matrices. Without
    that, float rounding in the row sums compounds as ``(1 + eps) ** (2 ** k)``
    whenever ``tol`` is not reached (e.g. reducible chains with slow transient
    states) and overflows to ``inf``/NaN. The result is therefore always
    finite, non-negative and sums to 1.
    """
    _check_stochastic(p)
    q = _row_normalise(0.5 * (np.eye(N_STATES) + np.clip(p, 0.0, None)))
    for _ in range(max_squarings):
        q_next = _row_normalise(q @ q)
        converged = np.max(np.abs(q_next - q)) < tol
        q = q_next
        if converged:
            break
    pi = np.full(N_STATES, 1.0 / N_STATES) @ q
    return pi / pi.sum()


def _row_normalise(q: np.ndarray) -> np.ndarray:
    """Clip tiny negatives and rescale rows to sum to exactly 1 (rows of ``Q`` are never 0)."""
    q = np.clip(q, 0.0, None)
    return q / q.sum(axis=1, keepdims=True)


# ---------------------------------------------------------------------------
# Structured output
# ---------------------------------------------------------------------------


def _as_dict(vec: Sequence[float] | np.ndarray) -> dict[Regime, float]:
    return {s: float(vec[i]) for i, s in enumerate(STATES)}


class RegimeForecast(BaseModel):
    """Regime distribution *horizon* steps ahead."""

    horizon: int = Field(..., ge=0, description="Steps ahead (1 step = `step` sessions)")
    probabilities: dict[Regime, float]


# Fields only a v2 read carries. A v1 read leaves them out of its serialised form, so
# the rollback path (and every pre-v2 stored row) is byte-identical to the old model.
V2_FIELDS: tuple[str, ...] = (
    "model",
    "z",
    "trend_z",
    "vol_state",
    "vol_label_source",
    "rv20",
    "rv20_pct_rank",
    "vol_transition_matrix",
    "vol_stickiness",
    "run_length",
    "margin_z",
    "fit_window",
)


class RegimeFeatures(BaseModel):
    """Markov regime state for one underlying as of one session close."""

    as_of: dt.date
    current: Regime
    trailing_return: float = Field(..., description="Trailing lookback return at as_of")
    lookback_days: int
    step: int = Field(..., description="Sessions between the two labels of a transition")
    n_transitions: int = Field(..., description="Transitions used to fit the matrix")
    transition_matrix: dict[Regime, dict[Regime, float]] = Field(
        ..., description="P[from][to], rows sum to 1"
    )
    stickiness: float = Field(..., description="P[current][current]")
    stickiness_by_state: dict[Regime, float]
    expected_duration: float | None = Field(
        ..., description="Expected remaining run length of current state in steps; None = absorbing"
    )
    forecasts: list[RegimeForecast]
    stationary: dict[Regime, float]
    # -- v2 (D77, E17.1); defaulted so v1 / pre-v2 rows load unchanged ---------------
    model: RegimeModel = Field(
        default="v1", description="Labelling model: v1 = +/-5% trailing return, v2 = vol-scaled z"
    )
    z: float | None = Field(default=None, description="v2: r20 / (sigma60 * sqrt(20)) at as_of")
    trend_z: float | None = Field(default=None, description="v2: |z| threshold for bull / bear")
    vol_state: VolState | None = Field(default=None, description="v2: realised-vol state at as_of")
    vol_label_source: Literal["percentile", "fixed"] | None = Field(
        None, description="v2: percentile of own rv20 history, or fixed 12%/20% (warm-up)"
    )
    rv20: float | None = Field(default=None, description="v2: annualised 20-session realised vol")
    rv20_pct_rank: float | None = Field(
        None, ge=0.0, le=100.0, description="v2: rv20 percentile in the trailing rank window"
    )
    vol_transition_matrix: dict[VolState, dict[VolState, float]] | None = Field(
        None, description="v2: vol-state P[from][to], rows sum to 1"
    )
    vol_stickiness: float | None = Field(default=None, description="v2: P[vol_state][vol_state]")
    run_length: int | None = Field(
        None, ge=1, description="v2: sessions the current trend label has held"
    )
    margin_z: float | None = Field(
        None, ge=0.0, description="v2: distance from z to the nearest threshold"
    )
    fit_window: int | None = Field(default=None, description="v2: labels used to fit the chains")

    @model_serializer(mode="wrap")
    def _drop_v2_fields_for_v1(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        data: dict[str, Any] = handler(self)
        if self.model == "v1":
            for k in V2_FIELDS:
                data.pop(k, None)
        return data


def _fit(seq: Sequence[int], n_states: int, *, step: int, alpha: float) -> tuple[np.ndarray, int]:
    """Row-stochastic matrix from integer state indices (same rule as
    :func:`transition_matrix`), plus the number of transitions used."""
    counts = np.zeros((n_states, n_states), dtype=float)
    for a, b in zip(seq[:-step], seq[step:], strict=True):
        counts[a, b] += 1.0
    counts += alpha
    totals = counts.sum(axis=1, keepdims=True)
    uniform = np.full((n_states, n_states), 1.0 / n_states)
    with np.errstate(invalid="ignore", divide="ignore"):
        probs = np.where(totals > 0, counts / np.where(totals > 0, totals, 1.0), uniform)
    return probs, max(len(seq) - step, 0)


def estimate_regime(
    closes: pd.Series,
    as_of: dt.date,
    *,
    model: RegimeModel = "v1",
    lookback: int = LOOKBACK_DAYS,
    bull: float = BULL_THRESHOLD,
    bear: float = BEAR_THRESHOLD,
    fit_window: int | None = None,
    step: int = 1,
    alpha: float = 0.0,
    horizons: Sequence[int] = DEFAULT_HORIZONS,
    trend_z: float = V2_TREND_Z,
    vol_scale_window: int = V2_VOL_SCALE_WINDOW,
    vol_rank_window: int = V2_VOL_RANK_WINDOW,
) -> RegimeFeatures:
    """Fit the regime chain on data up to *as_of* and describe the current state.

    ``model="v1"`` (the rollback path) labels by the +/-5 % trailing return;
    ``bull``/``bear`` apply and ``trend_z``/``vol_*`` are ignored. ``model="v2"`` labels by
    the vol-scaled z, adds the vol state and the confirmation fields; ``bull``/``bear``
    are ignored. Callers pass the live knobs (``ArcSettings.regime_*``).

    ``fit_window`` limits the fit to the most recent N labels (``None`` =
    all history up to ``as_of``). Raises :class:`InsufficientHistoryError`
    if there is not at least one transition to fit.
    """
    if model == "v2":
        return _estimate_v2(
            closes,
            as_of,
            lookback=lookback,
            fit_window=fit_window,
            step=step,
            alpha=alpha,
            horizons=horizons,
            trend_z=trend_z,
            vol_scale_window=vol_scale_window,
            vol_rank_window=vol_rank_window,
        )
    if model != "v1":
        raise ValueError(f"unknown regime model {model!r}; expected one of {REGIME_MODELS}")
    history = truncate(to_daily_series(closes, name="close"), as_of)
    labels = label_regimes(history, lookback=lookback, bull=bull, bear=bear)
    if fit_window is not None:
        if fit_window < step + 1:
            raise ValueError("fit_window must be > step")
        labels = labels.iloc[-fit_window:]
    n_trans = max(len(labels) - step, 0)
    if n_trans < 1:
        raise InsufficientHistoryError(
            f"need at least {lookback + step + 1} closes up to {as_of}, got {len(history)}"
        )

    seq = list(labels)
    current = seq[-1]
    p = transition_matrix(seq, step=step, alpha=alpha)
    stick = stickiness(p)
    dur = expected_duration(p)

    return RegimeFeatures(
        as_of=as_of,
        current=current,
        trailing_return=float(trailing_returns(history, lookback).iloc[-1]),
        lookback_days=lookback,
        step=step,
        n_transitions=n_trans,
        transition_matrix={s: _as_dict(p[i]) for i, s in enumerate(STATES)},
        stickiness=stick[current],
        stickiness_by_state=stick,
        expected_duration=dur[current],
        forecasts=[
            RegimeForecast(horizon=h, probabilities=_as_dict(forecast(p, current, h)))
            for h in horizons
        ],
        stationary=_as_dict(stationary_distribution(p)),
    )


def _estimate_v2(  # noqa: PLR0913 - every knob is config (D77)
    closes: pd.Series,
    as_of: dt.date,
    *,
    lookback: int,
    fit_window: int | None,
    step: int,
    alpha: float,
    horizons: Sequence[int],
    trend_z: float,
    vol_scale_window: int,
    vol_rank_window: int,
) -> RegimeFeatures:
    """Regime v2 (D77): see the module docstring."""
    if alpha < 0:
        raise ValueError("alpha must be >= 0")
    if step < 1:
        raise ValueError("step must be >= 1")
    if fit_window is not None and fit_window < step + 1:
        raise ValueError("fit_window must be > step")
    history = truncate(_positive_closes(closes), as_of)
    z = vol_scaled_z(history, lookback=lookback, vol_window=vol_scale_window)
    labels = [classify_z(float(v), trend_z=trend_z) for v in z]
    fit_labels = labels[-fit_window:] if fit_window is not None else labels
    if max(len(fit_labels) - step, 0) < 1:
        need = max(lookback, vol_scale_window) + step + 1
        raise InsufficientHistoryError(
            f"need at least {need} closes up to {as_of} for regime v2, got {len(history)}"
        )
    current = labels[-1]
    p, n_trans = _fit([_INDEX[s] for s in fit_labels], N_STATES, step=step, alpha=alpha)
    stick = stickiness(p)
    dur = expected_duration(p)
    z_now = float(z.iloc[-1])

    vol = label_vol_v2(history, lookback=lookback, rank_window=vol_rank_window)
    vol_seq = vol.state[-fit_window:] if fit_window is not None else vol.state
    vp, _ = _fit([_VOL_INDEX[s] for s in vol_seq], len(VOL_STATES), step=step, alpha=alpha)
    vol_now = vol.state[-1]

    return RegimeFeatures(
        as_of=as_of,
        current=current,
        trailing_return=float(history.iloc[-1] / history.iloc[-1 - lookback] - 1.0),
        lookback_days=lookback,
        step=step,
        n_transitions=n_trans,
        transition_matrix={s: _as_dict(p[i]) for i, s in enumerate(STATES)},
        stickiness=stick[current],
        stickiness_by_state=stick,
        expected_duration=dur[current],
        forecasts=[
            RegimeForecast(horizon=h, probabilities=_as_dict(forecast(p, current, h)))
            for h in horizons
        ],
        stationary=_as_dict(stationary_distribution(p)),
        model="v2",
        z=z_now,
        trend_z=trend_z,
        vol_state=vol_now,
        vol_label_source=vol.source[-1],
        rv20=vol.rv[-1],
        rv20_pct_rank=vol.pct_rank[-1],
        vol_transition_matrix={
            a: {b: float(vp[i, j]) for j, b in enumerate(VOL_STATES)}
            for i, a in enumerate(VOL_STATES)
        },
        vol_stickiness=float(vp[_VOL_INDEX[vol_now], _VOL_INDEX[vol_now]]),
        run_length=run_length(labels),
        margin_z=margin_to_threshold(z_now, trend_z),
        fit_window=len(fit_labels),
    )


def walk_forward(
    closes: pd.Series, dates: Iterable[dt.date], **kwargs: object
) -> list[RegimeFeatures]:
    """:func:`estimate_regime` for each date, each fit only on data up to that date.

    Dates with insufficient history are skipped.
    """
    out: list[RegimeFeatures] = []
    for d in dates:
        try:
            out.append(estimate_regime(closes, d, **kwargs))  # type: ignore[arg-type]
        except InsufficientHistoryError:
            continue
    return out
