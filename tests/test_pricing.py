"""Tests for arc.pricing.bs — QuantLib cross-checks + Hypothesis property tests.

QuantLib tolerance: 1e-6 price, 1e-4 Greeks (per acceptance criteria).
"""

from __future__ import annotations

import math

import numpy as np
import pytest
import QuantLib as ql
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from arc.pricing.bs import (
    BSMInputs,
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

# ---------------------------------------------------------------------------
# Helpers — QuantLib reference pricer
# ---------------------------------------------------------------------------


def _ql_greeks(
    S: float, K: float, t: float, r: float, q: float, sigma: float, flag: str
) -> tuple[dict[str, float], float]:
    """Return (QuantLib reference Greeks dict, ql_year_fraction).

    QuantLib rounds t to integer days, so the actual year fraction may
    differ from the input ``t``.  Callers should use the returned year
    fraction when constructing ``BSMInputs`` for comparison.
    """
    today = ql.Date.todaysDate()
    n_days = int(round(t * 365))
    maturity = today + ql.Period(n_days, ql.Days)
    dc = ql.Actual365Fixed()
    calendar = ql.NullCalendar()
    ql_t = dc.yearFraction(today, maturity)  # exact QL year fraction

    spot_handle = ql.QuoteHandle(ql.SimpleQuote(S))
    flat_ts = ql.YieldTermStructureHandle(ql.FlatForward(today, r, dc))
    div_ts = ql.YieldTermStructureHandle(ql.FlatForward(today, q, dc))
    vol_ts = ql.BlackVolTermStructureHandle(ql.BlackConstantVol(today, calendar, sigma, dc))

    bsm_process = ql.BlackScholesMertonProcess(spot_handle, div_ts, flat_ts, vol_ts)
    engine = ql.AnalyticEuropeanEngine(bsm_process)

    option_type = ql.Option.Call if flag == "c" else ql.Option.Put
    payoff = ql.PlainVanillaPayoff(option_type, K)
    exercise = ql.EuropeanExercise(maturity)
    option = ql.VanillaOption(payoff, exercise)
    option.setPricingEngine(engine)

    return {
        "price": option.NPV(),
        "delta": option.delta(),
        "gamma": option.gamma(),
        "vega": option.vega(),  # QL vega is per-unit σ (∂V/∂σ)
        "theta": option.thetaPerDay(),
        "rho": option.rho(),  # QL rho is per-unit r (∂V/∂r)
    }, ql_t


# ---------------------------------------------------------------------------
# Fixture: common test cases
# ---------------------------------------------------------------------------

_CASES = [
    # (S, K, t, r, q, sigma, flag) — diverse moneyness, term, div
    (100.0, 100.0, 0.5, 0.05, 0.0, 0.20, "c"),  # ATM call, no div
    (100.0, 100.0, 0.5, 0.05, 0.0, 0.20, "p"),  # ATM put, no div
    (100.0, 90.0, 0.25, 0.03, 0.01, 0.30, "c"),  # ITM call, div
    (100.0, 110.0, 1.0, 0.04, 0.02, 0.25, "p"),  # ITM put, div
    (50.0, 55.0, 0.1, 0.02, 0.0, 0.40, "c"),  # OTM call, short term
    (200.0, 180.0, 0.75, 0.06, 0.03, 0.15, "p"),  # OTM put, div
    (49.0, 50.0, 0.3846, 0.05, 0.0, 0.20, "c"),  # Hull textbook
]


@pytest.fixture(params=_CASES, ids=[f"S{c[0]}K{c[1]}t{c[2]}q{c[4]}{c[6]}" for c in _CASES])
def case(request):
    S, K, t, r, q, sigma, flag = request.param
    ql_ref, ql_t = _ql_greeks(S, K, t, r, q, sigma, flag)
    # Use QL's exact year fraction so day-count discretisation doesn't cause spurious diffs
    inp = BSMInputs(S=S, K=K, t=ql_t, r=r, q=q, sigma=sigma, flag=OptionKind(flag))
    return inp, ql_ref


# ---------------------------------------------------------------------------
# QuantLib cross-check tests
# ---------------------------------------------------------------------------


class TestQuantLibCrossCheck:
    """Compare arc.pricing against QuantLib with tight tolerances."""

    def test_price_matches_ql(self, case):
        inp, ql_ref = case
        arc_price = price(inp)
        assert abs(arc_price - ql_ref["price"]) < 1e-6, (
            f"price: arc={arc_price:.10f}  ql={ql_ref['price']:.10f}"
        )

    def test_delta_matches_ql(self, case):
        inp, ql_ref = case
        arc_delta = delta(inp)
        assert abs(arc_delta - ql_ref["delta"]) < 1e-4, (
            f"delta: arc={arc_delta:.10f}  ql={ql_ref['delta']:.10f}"
        )

    def test_gamma_matches_ql(self, case):
        inp, ql_ref = case
        arc_gamma = gamma(inp)
        assert abs(arc_gamma - ql_ref["gamma"]) < 1e-4, (
            f"gamma: arc={arc_gamma:.10f}  ql={ql_ref['gamma']:.10f}"
        )

    def test_vega_matches_ql(self, case):
        inp, ql_ref = case
        arc_vega = vega(inp)
        assert abs(arc_vega - ql_ref["vega"]) < 1e-4, (
            f"vega: arc={arc_vega:.10f}  ql={ql_ref['vega']:.10f}"
        )

    def test_theta_matches_ql(self, case):
        inp, ql_ref = case
        arc_theta = theta(inp)
        assert abs(arc_theta - ql_ref["theta"]) < 1e-4, (
            f"theta: arc={arc_theta:.10f}  ql={ql_ref['theta']:.10f}"
        )

    def test_rho_matches_ql(self, case):
        inp, ql_ref = case
        arc_rho = rho(inp)
        assert abs(arc_rho - ql_ref["rho"]) < 1e-4, (
            f"rho: arc={arc_rho:.10f}  ql={ql_ref['rho']:.10f}"
        )

    def test_greeks_bundle_matches_ql(self, case):
        """greeks() all-in-one should match individual functions and QL."""
        inp, ql_ref = case
        g = greeks(inp)
        assert abs(g.price - ql_ref["price"]) < 1e-6
        assert abs(g.delta - ql_ref["delta"]) < 1e-4
        assert abs(g.gamma - ql_ref["gamma"]) < 1e-4
        assert abs(g.vega - ql_ref["vega"]) < 1e-4
        assert abs(g.theta - ql_ref["theta"]) < 1e-4
        assert abs(g.rho - ql_ref["rho"]) < 1e-4


# ---------------------------------------------------------------------------
# IV round-trip tests
# ---------------------------------------------------------------------------


class TestImpliedVolatility:
    """IV solve should round-trip: price → IV → price."""

    @pytest.mark.parametrize("case_tuple", _CASES)
    def test_iv_roundtrip(self, case_tuple):
        S, K, t, r, q, sigma, flag = case_tuple
        inp = BSMInputs(S=S, K=K, t=t, r=r, q=q, sigma=sigma, flag=OptionKind(flag))
        p = price(inp)
        iv = implied_volatility(p, S, K, t, r, q, flag)
        assert abs(iv - sigma) < 1e-4, f"IV roundtrip: σ_in={sigma}  σ_out={iv}"

    def test_iv_raises_on_impossible_price(self):
        """Price below intrinsic should raise ValueError."""
        with pytest.raises(ValueError, match="IV solve failed"):
            # Call with price = 0 (below intrinsic for deep ITM)
            implied_volatility(0.0, 100.0, 50.0, 0.5, 0.05, 0.0, "c")


# ---------------------------------------------------------------------------
# Vanna / Volga tests (finite-difference cross-check)
# ---------------------------------------------------------------------------


class TestVannaVolga:
    """Validate analytic Vanna/Volga against finite differences."""

    @pytest.mark.parametrize("case_tuple", _CASES)
    def test_vanna_finite_diff(self, case_tuple):
        """Vanna ≈ ΔΔ/Δσ via finite difference on delta."""
        S, K, t, r, q, sigma, flag = case_tuple
        inp = BSMInputs(S=S, K=K, t=t, r=r, q=q, sigma=sigma, flag=OptionKind(flag))
        analytic_vanna = vanna(inp)

        d_sigma = 1e-5
        inp_up = BSMInputs(S=S, K=K, t=t, r=r, q=q, sigma=sigma + d_sigma, flag=OptionKind(flag))
        inp_dn = BSMInputs(S=S, K=K, t=t, r=r, q=q, sigma=sigma - d_sigma, flag=OptionKind(flag))
        fd_vanna = (delta(inp_up) - delta(inp_dn)) / (2 * d_sigma)

        assert abs(analytic_vanna - fd_vanna) < 1e-4, (
            f"vanna: analytic={analytic_vanna:.8f}  fd={fd_vanna:.8f}"
        )

    @pytest.mark.parametrize("case_tuple", _CASES)
    def test_volga_finite_diff(self, case_tuple):
        """Volga ≈ Δν/Δσ via finite difference on vega."""
        S, K, t, r, q, sigma, flag = case_tuple
        inp = BSMInputs(S=S, K=K, t=t, r=r, q=q, sigma=sigma, flag=OptionKind(flag))
        analytic_volga = volga(inp)

        d_sigma = 1e-5
        inp_up = BSMInputs(S=S, K=K, t=t, r=r, q=q, sigma=sigma + d_sigma, flag=OptionKind(flag))
        inp_dn = BSMInputs(S=S, K=K, t=t, r=r, q=q, sigma=sigma - d_sigma, flag=OptionKind(flag))
        fd_volga = (vega(inp_up) - vega(inp_dn)) / (2 * d_sigma)

        assert abs(analytic_volga - fd_volga) < 1e-3, (
            f"volga: analytic={analytic_volga:.8f}  fd={fd_volga:.8f}"
        )


# ---------------------------------------------------------------------------
# Hypothesis property tests
# ---------------------------------------------------------------------------

# Strategy for BSM inputs in a reasonable range
bsm_floats = st.floats(min_value=10.0, max_value=500.0)
strike_floats = st.floats(min_value=10.0, max_value=500.0)
time_floats = st.floats(min_value=0.01, max_value=2.0)
rate_floats = st.floats(min_value=-0.02, max_value=0.15)
div_floats = st.floats(min_value=0.0, max_value=0.08)
vol_floats = st.floats(min_value=0.05, max_value=1.5)
flag_strat = st.sampled_from([OptionKind.CALL, OptionKind.PUT])


@st.composite
def bsm_inputs(draw):
    return BSMInputs(
        S=draw(bsm_floats),
        K=draw(strike_floats),
        t=draw(time_floats),
        r=draw(rate_floats),
        q=draw(div_floats),
        sigma=draw(vol_floats),
        flag=draw(flag_strat),
    )


class TestPropertyTests:
    """Hypothesis property tests for option pricing invariants."""

    @given(data=bsm_inputs())
    @settings(max_examples=200, deadline=None)
    def test_put_call_parity(self, data: BSMInputs):
        """C - P = S·e^{-qt} - K·e^{-rt} (put-call parity)."""
        call_inp = BSMInputs(
            S=data.S,
            K=data.K,
            t=data.t,
            r=data.r,
            q=data.q,
            sigma=data.sigma,
            flag=OptionKind.CALL,
        )
        put_inp = BSMInputs(
            S=data.S,
            K=data.K,
            t=data.t,
            r=data.r,
            q=data.q,
            sigma=data.sigma,
            flag=OptionKind.PUT,
        )
        c = price(call_inp)
        p = price(put_inp)
        parity_rhs = data.S * math.exp(-data.q * data.t) - data.K * math.exp(-data.r * data.t)
        # Tolerance relative to max component
        tol = max(1e-8, 1e-8 * max(abs(c), abs(p), abs(parity_rhs)))
        assert abs((c - p) - parity_rhs) < tol, (
            f"PCP violated: C-P={c - p:.10f}  Se^-qt - Ke^-rt={parity_rhs:.10f}"
        )

    @given(data=bsm_inputs())
    @settings(max_examples=200, deadline=None)
    def test_monotonicity_in_sigma(self, data: BSMInputs):
        """Option price increases with σ (long options)."""
        assume(data.sigma < 1.45)  # room to bump
        inp1 = data
        inp2 = BSMInputs(
            S=data.S,
            K=data.K,
            t=data.t,
            r=data.r,
            q=data.q,
            sigma=data.sigma + 0.05,
            flag=data.flag,
        )
        p1 = price(inp1)
        p2 = price(inp2)
        assert p2 >= p1 - 1e-10, f"price should increase with σ: p(σ)={p1}  p(σ+0.05)={p2}"

    @given(data=bsm_inputs())
    @settings(max_examples=200, deadline=None)
    def test_delta_bounds(self, data: BSMInputs):
        """Call delta ∈ [0, 1], put delta ∈ [-1, 0] (for q ≥ 0)."""
        d = delta(data)
        if data.flag == OptionKind.CALL:
            assert -1e-10 <= d <= 1.0 + 1e-10, f"Call delta out of [0,1]: {d}"
        else:
            assert -1.0 - 1e-10 <= d <= 1e-10, f"Put delta out of [-1,0]: {d}"

    @given(data=bsm_inputs())
    @settings(max_examples=200, deadline=None)
    def test_gamma_non_negative(self, data: BSMInputs):
        """Gamma ≥ 0 for long options (same for calls and puts)."""
        g = gamma(data)
        assert g >= -1e-12, f"Gamma negative: {g}"

    @given(data=bsm_inputs())
    @settings(max_examples=200, deadline=None)
    def test_vega_non_negative(self, data: BSMInputs):
        """Vega ≥ 0 for long European options."""
        v = vega(data)
        assert v >= -1e-12, f"Vega negative: {v}"

    @given(data=bsm_inputs())
    @settings(max_examples=200, deadline=None)
    def test_price_non_negative(self, data: BSMInputs):
        """Option price ≥ 0."""
        p = price(data)
        assert p >= -1e-10, f"Negative price: {p}"

    @given(data=bsm_inputs())
    @settings(max_examples=200, deadline=None)
    def test_greeks_bundle_consistency(self, data: BSMInputs):
        """greeks() bundle matches individual function outputs."""
        g = greeks(data)
        assert abs(g.price - price(data)) < 1e-12
        assert abs(g.delta - delta(data)) < 1e-12
        assert abs(g.gamma - gamma(data)) < 1e-12
        assert abs(g.vega - vega(data)) < 1e-12
        assert abs(g.theta - theta(data)) < 1e-12
        assert abs(g.rho - rho(data)) < 1e-12
        assert abs(g.vanna - vanna(data)) < 1e-12
        assert abs(g.volga - volga(data)) < 1e-12


# ---------------------------------------------------------------------------
# Vectorised pricing tests
# ---------------------------------------------------------------------------


class TestVectorised:
    """Tests for the vectorised pricing path."""

    def test_vectorised_matches_scalar(self):
        flags = np.array(["c", "p", "c"])
        S = np.array([100.0, 100.0, 50.0])
        K = np.array([100.0, 110.0, 55.0])
        t = np.array([0.5, 1.0, 0.25])
        r = np.array([0.05, 0.04, 0.02])
        sigma = np.array([0.20, 0.25, 0.30])
        q = np.array([0.0, 0.02, 0.01])

        vec_prices = price_vectorized(flags, S, K, t, r, sigma, q)

        for i in range(len(flags)):
            inp = BSMInputs(
                S=S[i],
                K=K[i],
                t=t[i],
                r=r[i],
                q=q[i],
                sigma=sigma[i],
                flag=OptionKind(flags[i]),
            )
            scalar_p = price(inp)
            assert abs(vec_prices[i] - scalar_p) < 1e-4, (
                f"[{i}] vec={vec_prices[i]:.10f}  scalar={scalar_p:.10f}"
            )
