"""E11.3 / D70: live opens capped until the live scorecard gate is met (gate rule)."""

from __future__ import annotations

from decimal import Decimal as D
from pathlib import Path
from unittest import mock

from hypothesis import given
from hypothesis import strategies as st

from arc.config import ArcSettings
from arc.gate import rules as R
from arc.gate.rules import RuleCode, check_live_size_cap
from arc.models import Sizing
from tests.test_gate import acct, codes, make_proposal, run

_FAKE_LIVE = Path(__file__)


def live_cfg(**kw: object) -> ArcSettings:
    with mock.patch("arc.config._LIVE_ENV_PATH", _FAKE_LIVE):
        return ArcSettings(  # type: ignore[call-arg]
            _env_file=None, env="live", account_profile="margin", **kw
        )


def paper_cfg(**kw: object) -> ArcSettings:
    return ArcSettings(_env_file=None, account_profile="margin", **kw)  # type: ignore[call-arg]


def sized(n: int):
    return make_proposal(sizing=Sizing(contracts=n, notional=D(415) * n, pct_equity=0.004 * n))


CAP = RuleCode.LIVE_SIZE_CAP.value


def test_live_open_above_cap_rejected() -> None:
    d = run(sized(2), acct(live_gate_met=False), config=live_cfg())
    assert CAP in codes(d)
    assert "2 contracts > live cap 1 while the live scorecard gate is not met" in str(d.violations)
    assert CAP not in codes(run(sized(1), acct(live_gate_met=False), config=live_cfg()))


def test_cap_lifted_when_gate_met() -> None:
    assert CAP not in codes(run(sized(2), acct(live_gate_met=True), config=live_cfg()))


def test_unknown_gate_status_caps() -> None:
    d = run(sized(2), acct(), config=live_cfg())  # live_gate_met defaults to None
    assert CAP in codes(d) and "gate is unknown" in str(d.violations)


def test_paper_never_capped() -> None:
    assert CAP not in codes(run(sized(5), acct(live_gate_met=False), config=paper_cfg()))
    assert CAP not in codes(run(sized(5), acct(), config=paper_cfg()))


def test_closes_never_capped() -> None:
    from arc.gate.inputs import Portfolio
    from tests.test_gate import NOW, mkt

    d = R.evaluate(
        sized(5),
        acct(live_gate_met=False),
        Portfolio(),
        live_cfg(),
        market=mkt(),
        now=NOW,
        closing=True,
    )
    assert d.violations  # the closing rules reject this (no held legs) ...
    assert CAP not in codes(d)  # ... but the live cap never runs on a close


def test_configurable_cap() -> None:
    c = live_cfg(live_max_contracts_until_gate=3)
    assert CAP not in codes(run(sized(3), acct(live_gate_met=False), config=c))
    assert CAP in codes(run(sized(4), acct(live_gate_met=False), config=c))


@given(
    contracts=st.integers(min_value=1, max_value=100),
    cap=st.integers(min_value=1, max_value=100),
    met=st.sampled_from([True, False, None]),
)
def test_cap_monotone(contracts: int, cap: int, met: bool | None) -> None:
    c = live_cfg(live_max_contracts_until_gate=cap)
    v = check_live_size_cap(sized(contracts), acct(live_gate_met=met), c)
    assert bool(v) == (contracts > cap and met is not True)
    assert check_live_size_cap(sized(contracts), acct(live_gate_met=met), paper_cfg()) == []
