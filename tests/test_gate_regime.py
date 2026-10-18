"""E28.2 / D131: gate rule ``short_premium_regime`` (SPY below its SMA blocks new bull puts
and iron condors). Pure; runs under ``make test-gate`` (100% branch coverage of
``arc.gate``)."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal as D
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from arc.config import ArcSettings
from arc.gate import (
    AccountSnapshot,
    MarketSnapshot,
    MarketTrend,
    Portfolio,
    Quote,
    RuleCode,
    evaluate,
)
from arc.gate import rules as R
from arc.models import Proposal, QuantMetrics, Sizing, Structure
from arc.structures import credit_vertical, debit_vertical, iron_condor, long_put
from arc.utils.calendar import ET

AS_OF = dt.date(2026, 10, 16)  # Friday
NOW = dt.datetime(2026, 10, 16, 11, 0, tzinfo=ET)
EXP = dt.date(2026, 11, 20)
CODE = RuleCode.SHORT_PREMIUM_REGIME.value


def cfg(**kw: Any) -> ArcSettings:
    base: dict[str, Any] = {"account_profile": "margin", "regime_gate_enabled": True}
    return ArcSettings(_env_file=None, **(base | kw))  # type: ignore[call-arg]


def trend(close: str, sma: str = "600", *, ma: int = 200, as_of: dt.date = AS_OF) -> MarketTrend:
    return MarketTrend(ticker="SPY", close=D(close), ma=ma, sma=D(sma), as_of=as_of)


def bull_put() -> Structure:
    return credit_vertical(
        "p", "XYZ", EXP, short_strike=95, short_premium="2.00", long_strike=90,
        long_premium="0.80", as_of=AS_OF,
    )  # fmt: skip


def bear_call() -> Structure:
    return credit_vertical(
        "c", "XYZ", EXP, short_strike=105, short_premium="2.00", long_strike=110,
        long_premium="0.80", as_of=AS_OF,
    )  # fmt: skip


def condor() -> Structure:
    return iron_condor(
        "XYZ", EXP, long_put_strike=85, long_put_premium="0.50", short_put_strike=90,
        short_put_premium="1.20", short_call_strike=110, short_call_premium="1.20",
        long_call_strike=115, long_call_premium="0.50", as_of=AS_OF,
    )  # fmt: skip


def bull_call() -> Structure:
    return debit_vertical(
        "c", "XYZ", EXP, long_strike=100, long_premium="3.00", short_strike=105,
        short_premium="1.20", as_of=AS_OF,
    )  # fmt: skip


def lput() -> Structure:
    return long_put("XYZ", EXP, 95, "2.00", as_of=AS_OF)


def proposal(s: Structure) -> Proposal:
    return Proposal(
        candidate_id="c1",
        structure=s,
        thesis="t",
        quant=QuantMetrics(pop=0.6, ev=D("1"), cost_bps=1.0),
        sizing=Sizing(contracts=1, notional=D("500"), pct_equity=0.005),
        expires_at=NOW + dt.timedelta(minutes=10),
    )


def check(s: Structure, t: MarketTrend | None, config: ArcSettings | None = None) -> list[str]:
    p = proposal(s)
    market = MarketSnapshot(market_trend=t)
    got = R.check_short_premium_regime(p, R.derive(p), market, config or cfg(), NOW)
    return [str(v) for v in got]


class TestKind:
    @pytest.mark.parametrize(
        ("build", "kind"),
        [
            (bull_put, "bull_put"),
            (bear_call, "bear_call"),
            (condor, "iron_condor"),
            (bull_call, None),
            (lput, None),
        ],
    )
    def test_short_premium_kind(self, build: Any, kind: str | None) -> None:
        p = proposal(build())
        assert R.short_premium_kind(p, R.derive(p)) == kind

    def test_blocked_kinds_choices(self) -> None:
        assert R.regime_blocked_kinds(cfg()) == {"bull_put", "iron_condor"}
        assert R.regime_blocked_kinds(cfg(regime_gate_blocked_kinds="bull_put")) == {"bull_put"}


class TestThreshold:
    def test_below_sma_blocks_bull_put(self) -> None:
        (v,) = check(bull_put(), trend("599.99"))
        assert v.startswith(f"{CODE}: SPY close 599.99 < SMA200 600.00 on 2026-10-16")
        assert v.endswith("no new bull_put")

    def test_below_sma_blocks_condor(self) -> None:
        (v,) = check(condor(), trend("550"))
        assert v.endswith("no new iron_condor")

    def test_at_or_above_sma_passes(self) -> None:
        assert check(bull_put(), trend("600")) == []
        assert check(condor(), trend("650")) == []

    def test_bear_call_and_debit_kinds_pass_below(self) -> None:
        for build in (bear_call, bull_call, lput):
            assert check(build(), trend("500")) == []
            assert check(build(), None) == []  # never fails closed for other kinds

    def test_blocked_kinds_subset(self) -> None:
        only_puts = cfg(regime_gate_blocked_kinds="bull_put")
        assert check(condor(), trend("500"), only_puts) == []
        assert check(bull_put(), trend("500"), only_puts) != []

    def test_positive_buffer_blocks_just_above(self) -> None:
        c = cfg(regime_gate_buffer_pct=0.01)  # threshold 606
        (v,) = check(bull_put(), trend("605"), c)
        assert "x (1 +1.00%) = 606.00" in v
        assert check(bull_put(), trend("606"), c) == []

    def test_negative_buffer_lets_a_dip_through(self) -> None:
        c = cfg(regime_gate_buffer_pct=-0.02)  # threshold 588
        assert check(bull_put(), trend("590"), c) == []
        (v,) = check(bull_put(), trend("587.99"), c)
        assert "x (1 -2.00%) = 588.00" in v

    @given(
        close=st.decimals(min_value=1, max_value=2000, places=2),
        sma=st.decimals(min_value=1, max_value=2000, places=2),
        buf=st.sampled_from([-0.05, -0.01, 0.0, 0.01, 0.05]),
    )
    def test_property_blocks_iff_below_threshold(self, close: D, sma: D, buf: float) -> None:
        c = cfg(regime_gate_buffer_pct=buf)
        t = trend(str(close), str(sma))
        blocked = bool(check(bull_put(), t, c))
        assert blocked == (close < sma * (1 + D(str(buf))))


class TestFailClosed:
    def test_missing_input(self) -> None:
        (v,) = check(bull_put(), None)
        assert v == f"{CODE}: regime input missing: no market close vs SMA200: no new bull_put"

    def test_input_for_another_ma(self) -> None:
        (v,) = check(bull_put(), trend("700", ma=50))
        assert "regime input missing: no market close vs SMA200" in v

    def test_two_sessions_old_is_fresh(self) -> None:
        # Fri NOW; Wed close: Thu + Fri = 2 sessions after it
        assert check(bull_put(), trend("700", as_of=dt.date(2026, 10, 14))) == []

    def test_three_sessions_old_is_stale(self) -> None:
        (v,) = check(bull_put(), trend("700", as_of=dt.date(2026, 10, 13)))
        assert "regime input missing: SPY close of 2026-10-13 is stale (3 sessions; max 2)" in v

    def test_weekend_gap_is_not_stale(self) -> None:
        monday = dt.datetime(2026, 10, 19, 10, 0, tzinfo=ET)
        p = proposal(bull_put())
        t = trend("700", as_of=dt.date(2026, 10, 15))  # Thu close: Fri + Mon = 2
        got = R.check_short_premium_regime(
            p, R.derive(p), MarketSnapshot(market_trend=t), cfg(), monday
        )
        assert got == []

    def test_future_dated_input_fails_closed(self) -> None:
        (v,) = check(bull_put(), trend("700", as_of=dt.date(2026, 10, 19)))
        assert "regime input missing: SPY close dated 2026-10-19" in v

    def test_closed_reason_is_none_when_open(self) -> None:
        assert R.regime_gate_closed(trend("700"), cfg(), NOW) is None
        assert R.regime_gate_closed(None, cfg(), NOW) is not None


class TestSwitch:
    def test_off_never_fires(self) -> None:
        off = cfg(regime_gate_enabled=False)
        assert check(bull_put(), trend("1"), off) == []
        assert check(condor(), None, off) == []

    def test_settings_choices(self) -> None:
        with pytest.raises(ValueError, match="regime_gate_ma must be one of"):
            cfg(regime_gate_ma=150)
        assert cfg(regime_gate_ma=50).regime_gate_ma == 50
        with pytest.raises(ValueError):
            cfg(regime_gate_buffer_pct=0.06)
        with pytest.raises(ValueError):
            cfg(regime_gate_blocked_kinds="bear_call")

    def test_shipped_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ARC_REGIME_GATE_ENABLED", raising=False)
        s = ArcSettings(_env_file=None)  # type: ignore[call-arg]
        assert s.regime_gate_enabled is True
        assert s.regime_gate_ma == 200
        assert s.regime_gate_buffer_pct == 0.0
        assert s.regime_gate_blocked_kinds == "bull_put,iron_condor"


def gate(s: Structure, t: MarketTrend | None, config: ArcSettings, *, closing: bool = False) -> Any:
    quotes = {
        leg.occ_symbol: Quote(
            bid=leg.premium - D("0.05"),  # type: ignore[operator]
            ask=leg.premium + D("0.05"),  # type: ignore[operator]
            as_of=NOW - dt.timedelta(seconds=5),
        )
        for leg in s.legs
    }
    account = AccountSnapshot(
        equity=D("100000"),
        last_equity=D("100000"),
        settled_cash=D("100000"),
        options_buying_power=D("100000"),
        as_of=NOW - dt.timedelta(seconds=5),
    )
    market = MarketSnapshot(
        quotes=quotes,
        next_earnings={"XYZ": None},
        underlying_spot={"XYZ": D("100")},
        market_trend=t,
    )
    return evaluate(
        proposal(s), account, Portfolio(), config, market=market, now=NOW, closing=closing
    )


def codes(decision: Any) -> set[str]:
    return {v.split(":", 1)[0] for v in decision.violations}


class TestEvaluate:
    def test_open_bull_put_below_sma_is_refused(self) -> None:
        assert CODE in codes(gate(bull_put(), trend("500"), cfg()))

    def test_open_bull_put_above_sma_has_no_regime_violation(self) -> None:
        assert CODE not in codes(gate(bull_put(), trend("700"), cfg()))

    def test_bear_call_below_sma_passes_the_rule(self) -> None:
        assert CODE not in codes(gate(bear_call(), trend("500"), cfg()))

    def test_closing_is_exempt(self) -> None:
        assert CODE not in codes(gate(bull_put(), None, cfg(), closing=True))

    def test_off_equals_pre_d131_gate(self) -> None:
        # off: the decision is identical whatever the trend says (rollback = byte-identical)
        off = cfg(regime_gate_enabled=False)
        a = gate(bull_put(), None, off)
        b = gate(bull_put(), trend("1"), off)
        assert a.violations == b.violations and a.passed == b.passed
        assert CODE not in codes(a)
