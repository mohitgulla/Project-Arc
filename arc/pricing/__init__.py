"""Black-Scholes-Merton pricing and Greeks (py_vollib, QuantLib cross-check).

Public API:
    - :func:`price` — BSM European option price
    - :func:`implied_volatility` — IV solve via lets-be-rational
    - :func:`delta`, :func:`gamma`, :func:`vega`, :func:`theta`, :func:`rho`
    - :func:`vanna`, :func:`volga` — analytic second-order Greeks
    - :func:`greeks` — all of the above in one call
    - :func:`price_vectorized` — batch pricing via py_vollib_vectorized
    - :class:`BSMInputs`, :class:`GreeksResult`, :class:`OptionKind`
"""

from arc.pricing.bs import (
    BSMInputs,
    GreeksResult,
    OptionKind,
    delta,
    gamma,
    greeks,
    implied_volatility,
    price,
    price_vectorized,
    rho,
    theta,
    vanna,
    vega,
    volga,
)

__all__ = [
    "BSMInputs",
    "GreeksResult",
    "OptionKind",
    "delta",
    "gamma",
    "greeks",
    "implied_volatility",
    "price",
    "price_vectorized",
    "rho",
    "theta",
    "vanna",
    "vega",
    "volga",
]
