"""Option structures: legs, payoffs, max gain/loss, breakevens, net Greeks.

Public API:
    - OCC symbology: :func:`parse_occ`, :func:`format_occ`, :class:`OccSymbol`
    - Builders (PLAN D4 whitelist): :func:`long_call`, :func:`long_put`,
      :func:`debit_vertical`, :func:`credit_vertical`, :func:`iron_condor`
    - Analytics: :func:`analyze`, :func:`payoff_at`, :func:`payoff_grid`,
      :func:`strike_grid`, :func:`max_gain_loss`, :func:`breakevens`,
      :func:`net_debit_credit`, :func:`net_greeks`, :func:`classify`,
      :func:`is_defined_risk`, :func:`assert_defined_risk`,
      :func:`buying_power`, :class:`MarketInputs`, :class:`UndefinedRiskError`
"""

from arc.structures.analytics import (
    CONTRACT_MULTIPLIER,
    MarketInputs,
    UndefinedRiskError,
    analyze,
    assert_defined_risk,
    breakevens,
    buying_power,
    classify,
    is_defined_risk,
    max_gain_loss,
    net_debit_credit,
    net_greeks,
    payoff_at,
    payoff_grid,
    strike_grid,
)
from arc.structures.builders import (
    credit_vertical,
    debit_vertical,
    iron_condor,
    long_call,
    long_put,
)
from arc.structures.occ import OccSymbol, format_occ, parse_occ

__all__ = [
    "CONTRACT_MULTIPLIER",
    "MarketInputs",
    "OccSymbol",
    "UndefinedRiskError",
    "analyze",
    "assert_defined_risk",
    "breakevens",
    "buying_power",
    "classify",
    "credit_vertical",
    "debit_vertical",
    "format_occ",
    "iron_condor",
    "is_defined_risk",
    "long_call",
    "long_put",
    "max_gain_loss",
    "net_debit_credit",
    "net_greeks",
    "parse_occ",
    "payoff_at",
    "payoff_grid",
    "strike_grid",
]
