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
"""

from __future__ import annotations

import datetime as dt  # noqa: TC003 — used at runtime in pydantic models
from enum import StrEnum
from typing import TYPE_CHECKING

import numpy as np
from pydantic import BaseModel, Field

from arc.features._series import InsufficientHistoryError, to_daily_series, truncate

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    import pandas as pd

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

LOOKBACK_DAYS = 20
BULL_THRESHOLD = 0.05
BEAR_THRESHOLD = -0.05
DEFAULT_HORIZONS: tuple[int, ...] = (1, 5, 20)


class Regime(StrEnum):
    """Market regime label."""

    BEAR = "bear"
    SIDEWAYS = "sideways"
    BULL = "bull"


STATES: tuple[Regime, ...] = (Regime.BEAR, Regime.SIDEWAYS, Regime.BULL)
_INDEX = {s: i for i, s in enumerate(STATES)}
N_STATES = len(STATES)


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
    """
    _check_stochastic(p)
    q = 0.5 * (np.eye(N_STATES) + p)
    for _ in range(max_squarings):
        q_next = q @ q
        if np.max(np.abs(q_next - q)) < tol:
            q = q_next
            break
        q = q_next
    pi = np.full(N_STATES, 1.0 / N_STATES) @ q
    pi = np.clip(pi, 0.0, None)
    return pi / pi.sum()


# ---------------------------------------------------------------------------
# Structured output
# ---------------------------------------------------------------------------


def _as_dict(vec: Sequence[float] | np.ndarray) -> dict[Regime, float]:
    return {s: float(vec[i]) for i, s in enumerate(STATES)}


class RegimeForecast(BaseModel):
    """Regime distribution *horizon* steps ahead."""

    horizon: int = Field(..., ge=0, description="Steps ahead (1 step = `step` sessions)")
    probabilities: dict[Regime, float]


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


def estimate_regime(
    closes: pd.Series,
    as_of: dt.date,
    *,
    lookback: int = LOOKBACK_DAYS,
    bull: float = BULL_THRESHOLD,
    bear: float = BEAR_THRESHOLD,
    fit_window: int | None = None,
    step: int = 1,
    alpha: float = 0.0,
    horizons: Sequence[int] = DEFAULT_HORIZONS,
) -> RegimeFeatures:
    """Fit the regime chain on data up to *as_of* and describe the current state.

    ``fit_window`` limits the fit to the most recent N labels (``None`` =
    all history up to ``as_of``). Raises :class:`InsufficientHistoryError`
    if there is not at least one transition to fit.
    """
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
