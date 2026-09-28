"""Black-Scholes-Merton pricing, IV solving, and Greeks.

Implements BSM with continuous dividend yield *q*. First-order Greeks
(Δ Γ ν Θ ρ) via analytical formulas; second-order Greeks Vanna and Volga
via closed-form expressions.  IV solving delegates to py_vollib's
*lets-be-rational* algorithm.

QuantLib cross-checks live in the test suite (tolerance 1e-6 price,
1e-4 Greeks).

**American-style caveat**: all formulas here assume *European* exercise.
American puts (and calls on dividend-paying stocks near ex-date) can
trade above the European value.  Downstream consumers should document
the European-pricing assumption when the underlier pays dividends; the
chain scanner (E2.3) will flag large American–European divergence when
it has access to broker-quoted Greeks.

Convention notes (per-unit unless stated):
  - *theta* is per **calendar day** (annual / 365).
  - All other Greeks are per-unit changes in the input
    (e.g. vega is per 1.0 change in σ, not per 1%).
"""

from __future__ import annotations

import math
from enum import StrEnum

import numpy as np
import structlog
from pydantic import BaseModel, Field
from scipy.stats import norm

log = structlog.get_logger()

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_CALENDAR_DAYS_PER_YEAR = 365.0
_MIN_TIME = 1e-10  # avoid division by zero near expiry
_MIN_VOL = 1e-10

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


class OptionKind(StrEnum):
    """European option type."""

    CALL = "c"
    PUT = "p"


class BSMInputs(BaseModel):
    """Inputs for BSM pricing / Greeks."""

    S: float = Field(..., gt=0, description="Underlying spot price")
    K: float = Field(..., gt=0, description="Strike price")
    t: float = Field(..., gt=0, description="Time to expiration in years")
    r: float = Field(..., description="Risk-free interest rate (annualised, continuous)")
    q: float = Field(0.0, description="Continuous dividend yield")
    sigma: float = Field(..., gt=0, description="Annualised implied volatility")
    flag: OptionKind = Field(..., description="'c' for call, 'p' for put")


class GreeksResult(BaseModel):
    """All Greeks for a single option."""

    price: float
    delta: float
    gamma: float
    vega: float  # per-unit σ
    theta: float  # per calendar day
    rho: float  # per-unit r
    vanna: float  # ∂²V/∂σ∂S
    volga: float  # ∂²V/∂σ² (a.k.a. vomma)


# ---------------------------------------------------------------------------
# Internal helpers — BSM d1/d2
# ---------------------------------------------------------------------------


def _d1d2(S: float, K: float, t: float, r: float, q: float, sigma: float) -> tuple[float, float]:
    """Return (d1, d2) for Black-Scholes-Merton."""
    t = max(t, _MIN_TIME)
    sigma = max(sigma, _MIN_VOL)
    sqrt_t = math.sqrt(t)
    d1 = (math.log(S / K) + (r - q + 0.5 * sigma * sigma) * t) / (sigma * sqrt_t)
    d2 = d1 - sigma * sqrt_t
    return d1, d2


def _npdf(x: float) -> float:
    """Standard normal PDF."""
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


# ---------------------------------------------------------------------------
# Pricing
# ---------------------------------------------------------------------------


def price(inputs: BSMInputs) -> float:
    """BSM European option price with continuous dividend yield.

    Parameters
    ----------
    inputs : BSMInputs
        Option parameters (S, K, t, r, q, sigma, flag).

    Returns
    -------
    float
        Theoretical option price.
    """
    S, K, t, r, q, sigma, flag = (
        inputs.S,
        inputs.K,
        inputs.t,
        inputs.r,
        inputs.q,
        inputs.sigma,
        inputs.flag,
    )
    d1, d2 = _d1d2(S, K, t, r, q, sigma)
    df_q = math.exp(-q * t)
    df_r = math.exp(-r * t)

    if flag == OptionKind.CALL:
        return S * df_q * norm.cdf(d1) - K * df_r * norm.cdf(d2)
    return K * df_r * norm.cdf(-d2) - S * df_q * norm.cdf(-d1)


# ---------------------------------------------------------------------------
# Implied volatility (via py_vollib lets-be-rational)
# ---------------------------------------------------------------------------


def implied_volatility(
    price_: float,
    S: float,
    K: float,
    t: float,
    r: float,
    q: float,
    flag: str | OptionKind,
) -> float:
    """Solve for BSM implied volatility.

    Uses py_vollib's *lets-be-rational* algorithm with the Merton dividend
    adjustment (S_adj = S * exp(-q * t)) so the standard BS IV solver gives
    the correct BSM IV.

    Parameters
    ----------
    price_ : float
        Observed market price of the option.
    S, K, t, r, q : float
        Spot, strike, time to expiry (years), risk-free rate,
        continuous dividend yield.
    flag : str
        ``'c'`` or ``'p'``.

    Returns
    -------
    float
        Annualised implied volatility.

    Raises
    ------
    ValueError
        If the solver fails to converge (e.g. price below intrinsic).
    """
    from vollib.black_scholes.implied_volatility import implied_volatility as _vollib_iv

    flag = str(flag)
    s_adj = S * math.exp(-q * t)
    try:
        return _vollib_iv(price_, s_adj, K, t, r, flag)
    except Exception as exc:
        msg = f"IV solve failed: {exc}"
        raise ValueError(msg) from exc


# ---------------------------------------------------------------------------
# First-order Greeks
# ---------------------------------------------------------------------------


def delta(inputs: BSMInputs) -> float:
    """BSM delta (∂V/∂S)."""
    d1, _ = _d1d2(inputs.S, inputs.K, inputs.t, inputs.r, inputs.q, inputs.sigma)
    df_q = math.exp(-inputs.q * inputs.t)
    if inputs.flag == OptionKind.CALL:
        return df_q * norm.cdf(d1)
    return df_q * (norm.cdf(d1) - 1.0)


def gamma(inputs: BSMInputs) -> float:
    """BSM gamma (∂²V/∂S²).  Always ≥ 0."""
    d1, _ = _d1d2(inputs.S, inputs.K, inputs.t, inputs.r, inputs.q, inputs.sigma)
    df_q = math.exp(-inputs.q * inputs.t)
    sigma = max(inputs.sigma, _MIN_VOL)
    t = max(inputs.t, _MIN_TIME)
    return df_q * _npdf(d1) / (inputs.S * sigma * math.sqrt(t))


def vega(inputs: BSMInputs) -> float:
    """BSM vega (∂V/∂σ).  Per-unit σ change."""
    d1, _ = _d1d2(inputs.S, inputs.K, inputs.t, inputs.r, inputs.q, inputs.sigma)
    df_q = math.exp(-inputs.q * inputs.t)
    t = max(inputs.t, _MIN_TIME)
    return inputs.S * df_q * math.sqrt(t) * _npdf(d1)


def theta(inputs: BSMInputs) -> float:
    """BSM theta (∂V/∂t), returned as change per **calendar day**."""
    S, K, t, r, q, sigma, flag = (
        inputs.S,
        inputs.K,
        inputs.t,
        inputs.r,
        inputs.q,
        inputs.sigma,
        inputs.flag,
    )
    d1, d2 = _d1d2(S, K, t, r, q, sigma)
    df_q = math.exp(-q * t)
    df_r = math.exp(-r * t)
    sqrt_t = math.sqrt(max(t, _MIN_TIME))

    # Common term: time-decay component
    term1 = -S * df_q * _npdf(d1) * sigma / (2.0 * sqrt_t)

    if flag == OptionKind.CALL:
        annual = term1 + q * S * df_q * norm.cdf(d1) - r * K * df_r * norm.cdf(d2)
    else:
        annual = term1 - q * S * df_q * norm.cdf(-d1) + r * K * df_r * norm.cdf(-d2)

    return annual / _CALENDAR_DAYS_PER_YEAR


def rho(inputs: BSMInputs) -> float:
    """BSM rho (∂V/∂r).  Per-unit r change."""
    _, d2 = _d1d2(inputs.S, inputs.K, inputs.t, inputs.r, inputs.q, inputs.sigma)
    df_r = math.exp(-inputs.r * inputs.t)
    if inputs.flag == OptionKind.CALL:
        return inputs.K * inputs.t * df_r * norm.cdf(d2)
    return -inputs.K * inputs.t * df_r * norm.cdf(-d2)


# ---------------------------------------------------------------------------
# Second-order Greeks: Vanna and Volga
# ---------------------------------------------------------------------------


def vanna(inputs: BSMInputs) -> float:
    """Vanna: ∂²V/∂σ∂S = ∂Δ/∂σ = ∂ν/∂S.

    Analytic formula: -e^{-qt} · n(d₁) · d₂ / σ
    """
    S, K, t, r, q, sigma = inputs.S, inputs.K, inputs.t, inputs.r, inputs.q, inputs.sigma
    d1, d2 = _d1d2(S, K, t, r, q, sigma)
    df_q = math.exp(-q * t)
    sigma = max(sigma, _MIN_VOL)
    return -df_q * _npdf(d1) * d2 / sigma


def volga(inputs: BSMInputs) -> float:
    """Volga (vomma): ∂²V/∂σ² = ∂ν/∂σ.

    Analytic formula: vega · d₁ · d₂ / σ
    """
    v = vega(inputs)
    d1, d2 = _d1d2(inputs.S, inputs.K, inputs.t, inputs.r, inputs.q, inputs.sigma)
    sigma = max(inputs.sigma, _MIN_VOL)
    return v * d1 * d2 / sigma


# ---------------------------------------------------------------------------
# All-in-one
# ---------------------------------------------------------------------------


def greeks(inputs: BSMInputs) -> GreeksResult:
    """Compute price and all Greeks in one pass (re-uses d1/d2).

    Returns a :class:`GreeksResult` with price, Δ, Γ, ν, Θ, ρ, vanna, volga.
    """
    S, K, t_raw, r, q, sigma_raw, flag = (
        inputs.S,
        inputs.K,
        inputs.t,
        inputs.r,
        inputs.q,
        inputs.sigma,
        inputs.flag,
    )
    t = max(t_raw, _MIN_TIME)
    sigma = max(sigma_raw, _MIN_VOL)
    sqrt_t = math.sqrt(t)

    d1, d2 = _d1d2(S, K, t, r, q, sigma)
    df_q = math.exp(-q * t)
    df_r = math.exp(-r * t)
    nd1 = _npdf(d1)  # standard normal PDF at d1

    # Price
    if flag == OptionKind.CALL:
        Nd1 = norm.cdf(d1)
        Nd2 = norm.cdf(d2)
        p = S * df_q * Nd1 - K * df_r * Nd2
    else:
        Nmd1 = norm.cdf(-d1)
        Nmd2 = norm.cdf(-d2)
        p = K * df_r * Nmd2 - S * df_q * Nmd1

    # Delta
    if flag == OptionKind.CALL:  # noqa: SIM108
        d = df_q * norm.cdf(d1)
    else:
        d = df_q * (norm.cdf(d1) - 1.0)

    # Gamma (same for call/put)
    g = df_q * nd1 / (S * sigma * sqrt_t)

    # Vega (per-unit σ)
    v = S * df_q * sqrt_t * nd1

    # Theta (per calendar day)
    term1 = -S * df_q * nd1 * sigma / (2.0 * sqrt_t)
    if flag == OptionKind.CALL:
        th_annual = term1 + q * S * df_q * norm.cdf(d1) - r * K * df_r * norm.cdf(d2)
    else:
        th_annual = term1 - q * S * df_q * norm.cdf(-d1) + r * K * df_r * norm.cdf(-d2)
    th = th_annual / _CALENDAR_DAYS_PER_YEAR

    # Rho (per-unit r)
    if flag == OptionKind.CALL:  # noqa: SIM108
        rh = K * t * df_r * norm.cdf(d2)
    else:
        rh = -K * t * df_r * norm.cdf(-d2)

    # Vanna: -e^{-qt} · n(d1) · d2 / σ
    van = -df_q * nd1 * d2 / sigma

    # Volga: vega · d1 · d2 / σ
    vol = v * d1 * d2 / sigma

    return GreeksResult(
        price=p,
        delta=d,
        gamma=g,
        vega=v,
        theta=th,
        rho=rh,
        vanna=van,
        volga=vol,
    )


# ---------------------------------------------------------------------------
# Vectorised pricing (pure numpy — no numba dependency)
# ---------------------------------------------------------------------------


def price_vectorized(
    flag: np.ndarray,
    S: np.ndarray,
    K: np.ndarray,
    t: np.ndarray,
    r: np.ndarray,
    sigma: np.ndarray,
    q: np.ndarray | float = 0.0,
) -> np.ndarray:
    """Vectorised BSM price using numpy (no numba).

    All array arguments must broadcast together.  Dividend yield *q*
    is handled via the Merton adjustment.

    Parameters
    ----------
    flag : array of ``'c'`` / ``'p'``
    S, K, t, r, sigma, q : float or array
        Standard BSM inputs.

    Returns
    -------
    np.ndarray
        Option prices.
    """
    S = np.asarray(S, dtype=float)
    K = np.asarray(K, dtype=float)
    t = np.asarray(t, dtype=float)
    r = np.asarray(r, dtype=float)
    sigma = np.asarray(sigma, dtype=float)
    q = np.asarray(q, dtype=float)
    flag = np.asarray(flag)

    t = np.maximum(t, _MIN_TIME)
    sigma = np.maximum(sigma, _MIN_VOL)
    sqrt_t = np.sqrt(t)

    d1 = (np.log(S / K) + (r - q + 0.5 * sigma**2) * t) / (sigma * sqrt_t)
    d2 = d1 - sigma * sqrt_t

    df_q = np.exp(-q * t)
    df_r = np.exp(-r * t)

    from scipy.stats import norm as _norm

    Nd1 = _norm.cdf(d1)
    Nd2 = _norm.cdf(d2)

    call_price = S * df_q * Nd1 - K * df_r * Nd2
    put_price = K * df_r * (1.0 - Nd2) - S * df_q * (1.0 - Nd1)

    is_call = np.char.equal(flag, "c")
    return np.where(is_call, call_price, put_price)
