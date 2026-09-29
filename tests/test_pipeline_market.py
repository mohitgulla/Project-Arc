"""E5.2a: propose re-pricing fails closed on missing IV (PLAN §6.8).

A structure whose re-pricing lacks implied volatility on any leg must never reach
the gate with all-zero Greeks (Sentinel key trading-safety:missing-iv-zero-greeks).
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal as D
from typing import TYPE_CHECKING, Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from arc.broker.base import AccountInfo, BrokerPosition
from arc.config import ArcSettings
from arc.data.base import OptionContract, UnderlyingQuote
from arc.models import LegIntent
from arc.pipeline import FIXTURE_NOW, PipelineEnv
from arc.pipeline.market import (
    PortfolioError,
    PricedStructure,
    account_snapshot,
    build_portfolio,
    limit_price,
    market_snapshot,
    next_earnings,
    price_structure,
)
from arc.pipeline.runner import open_db
from arc.routines.config import load_routines
from arc.structures import format_occ
from arc.utils.calendar import ET

if TYPE_CHECKING:
    from collections.abc import Callable

EXP = dt.date(2026, 11, 20)
AS_OF = dt.date(2026, 10, 9)
NOW = dt.datetime(2026, 10, 9, 10, 0, tzinfo=ET)
LP = format_occ("SPY", EXP, "put", 565)
SP = format_occ("SPY", EXP, "put", 570)
BULL_PUT = [(SP, LegIntent.SHORT, 1), (LP, LegIntent.LONG, 1)]


def contract(sym: str, bid: float, ask: float, iv: float | None) -> OptionContract:
    strike = 565.0 if sym == LP else 570.0
    return OptionContract(
        symbol=sym,
        underlying="SPY",
        expiration=EXP,
        strike=strike,
        option_type="put",
        bid=bid,
        ask=ask,
        mid=(bid + ask) / 2,
        implied_volatility=iv,
        quote_timestamp=NOW - dt.timedelta(seconds=5),
    )


class FakeMarket:
    """Two SPY puts; ``ivs`` sets each leg's implied volatility."""

    def __init__(self, ivs: dict[str, float | None], spot: float = 580.0) -> None:
        self.chain = [
            contract(LP, 1.20, 1.30, ivs.get(LP)),
            contract(SP, 2.05, 2.15, ivs.get(SP)),
        ]
        self.spot = spot

    def option_chain(self, underlying: str, start: dt.date, end: dt.date) -> list[OptionContract]:
        return [c for c in self.chain if start <= c.expiration <= end]

    def underlying_quote(self, symbol: str) -> UnderlyingQuote:
        return UnderlyingQuote(
            symbol=symbol, bid=self.spot - 0.01, ask=self.spot + 0.01, mid=self.spot, timestamp=NOW
        )


def price(market: Any, **kw: Any) -> PricedStructure:
    return price_structure(market, BULL_PUT, as_of=AS_OF, r=0.04, **kw)


# ---------------------------------------------------------------------------
# price_structure
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", [None, 0.0, -0.1, float("nan")])
@pytest.mark.parametrize("leg", [LP, SP])
def test_price_structure_rejects_missing_iv(leg: str, bad: float | None) -> None:
    ivs: dict[str, float | None] = {LP: 0.2, SP: 0.19, leg: bad}
    with pytest.raises(LookupError, match=leg):
        price(FakeMarket(ivs))


@settings(max_examples=50, suppress_health_check=[HealthCheck.too_slow])
@given(
    iv_long=st.floats(min_value=0.05, max_value=2.0, exclude_min=True, exclude_max=True),
    iv_short=st.floats(min_value=0.05, max_value=2.0, exclude_min=True, exclude_max=True),
)
def test_price_structure_greeks_nonzero_with_iv(iv_long: float, iv_short: float) -> None:
    priced = price(FakeMarket({LP: iv_long, SP: iv_short}))
    g = priced.structure.greeks
    # A defined-risk vertical with real IVs always carries some Greek exposure.
    assert (g.delta, g.gamma, g.vega, g.theta) != (0.0, 0.0, 0.0, 0.0)
    assert priced.structure.net_debit_credit == D("-0.85")
    assert set(priced.contracts) == {LP, SP}
    assert priced.spot == 580.0
    assert priced.spot_as_of == NOW
    assert priced.leg_spreads() == pytest.approx({LP: 0.10, SP: 0.10})


def test_price_structure_exit_path_tolerates_missing_iv() -> None:
    """Exits need mids only; a missing IV must never block a close (Greeks stay zero)."""
    priced = price(FakeMarket({LP: None, SP: 0.19}), require_iv=False)
    g = priced.structure.greeks
    assert (g.delta, g.vega) == (0.0, 0.0)
    assert priced.structure.net_debit_credit == D("-0.85")
    # With every IV present, the exit path still computes Greeks.
    full = price(FakeMarket({LP: 0.2, SP: 0.19}), require_iv=False)
    assert full.structure.greeks.vega != 0.0


def test_price_structure_missing_quote() -> None:
    m = FakeMarket({LP: 0.2, SP: 0.19})
    m.chain = [m.chain[0]]  # short leg not in the chain
    with pytest.raises(LookupError, match=f"no usable quote for {SP}"):
        price(m)
    m = FakeMarket({LP: 0.2, SP: 0.19})
    m.chain[1] = m.chain[1].model_copy(update={"bid": None})
    with pytest.raises(LookupError, match="no usable quote"):
        price(m)


# ---------------------------------------------------------------------------
# market_snapshot / limit_price / account_snapshot / next_earnings
# ---------------------------------------------------------------------------


def test_market_snapshot_drops_untimestamped_quotes() -> None:
    m = FakeMarket({LP: 0.2, SP: 0.19})
    contracts = {c.symbol: c for c in m.chain}
    contracts[LP] = contracts[LP].model_copy(update={"quote_timestamp": None})
    snap = market_snapshot(contracts, {"SPY": None})
    assert set(snap.quotes) == {SP}
    assert snap.quotes[SP].bid == D("2.05")
    assert snap.next_earnings == {"SPY": None}


def test_limit_price_and_account_snapshot() -> None:
    assert limit_price(D("-1.6555"), 0.01) == D("-1.65")
    info = AccountInfo(
        account_id="x", equity=D("1000"), buying_power=D("1000"), cash=D("1000"), last_equity=None
    )
    assert account_snapshot(info, NOW).last_equity == D(0)


def test_next_earnings_skips_bad_urls() -> None:
    conn = open_db(":memory:", copy=False)
    conn.execute(
        "INSERT INTO raw_docs (id, source, url, published_at, content_hash, text, "
        "tickers_hint, ingested_at) VALUES ('e0', 'earnings', "
        "'https://finnhub.io/calendar/earnings/AAPL/not-a-date', ?, 'h0', '', '[]', ?)",
        (NOW.isoformat(), NOW.isoformat()),
    )
    assert next_earnings(conn, ["AAPL", "SPY"], AS_OF) == {"SPY": None}


# ---------------------------------------------------------------------------
# build_portfolio: open positions also fail closed on missing IV
# ---------------------------------------------------------------------------


def positions() -> list[BrokerPosition]:
    return [
        BrokerPosition(symbol=SP, qty=D("-1"), side="short", avg_entry_price=D("2.10")),
        BrokerPosition(symbol=LP, qty=D("1"), side="long", avg_entry_price=D("1.25")),
    ]


def test_build_portfolio_prices_greeks() -> None:
    conn = open_db(":memory:", copy=False)
    pf = build_portfolio(
        conn, positions(), FakeMarket({LP: 0.2, SP: 0.19}), now=NOW, wash_sale_days=30, r=0.04
    )
    assert [p.underlying for p in pf.positions] == ["SPY"]
    assert pf.greeks.vega != 0.0
    assert pf.legs == {SP: -1, LP: 1}


def test_build_portfolio_missing_iv_blocks() -> None:
    conn = open_db(":memory:", copy=False)
    with pytest.raises(PortfolioError, match="no IV"):
        build_portfolio(
            conn, positions(), FakeMarket({LP: None, SP: 0.19}), now=NOW, wash_sale_days=30, r=0.04
        )


def test_build_portfolio_pricing_error_wrapped() -> None:
    class Down(FakeMarket):
        def underlying_quote(self, symbol: str) -> UnderlyingQuote:
            raise RuntimeError("feed down")

    conn = open_db(":memory:", copy=False)
    with pytest.raises(PortfolioError, match="cannot price open SPY position: feed down"):
        build_portfolio(
            conn, positions(), Down({LP: 0.2, SP: 0.19}), now=NOW, wash_sale_days=30, r=0.04
        )


def test_build_portfolio_bad_position_wrapped() -> None:
    conn = open_db(":memory:", copy=False)
    fractional = [BrokerPosition(symbol=LP, qty=D("1.5"), side="long")]
    with pytest.raises(PortfolioError, match="non-integral"):
        build_portfolio(
            conn, fractional, FakeMarket({LP: 0.2, SP: 0.19}), now=NOW, wash_sale_days=30, r=0.04
        )


# ---------------------------------------------------------------------------
# propose(): a missing IV is a reprice_failed skip, visible in the heartbeat
# ---------------------------------------------------------------------------


class StripIV:
    """Recorded fixture market with the IV removed from the given contracts."""

    def __init__(self, inner: Any, drop: Callable[[str], bool]) -> None:
        self.inner = inner
        self.drop = drop

    def option_chain(self, underlying: str, start: dt.date, end: dt.date) -> list[OptionContract]:
        return [
            c.model_copy(update={"implied_volatility": None}) if self.drop(c.symbol) else c
            for c in self.inner.option_chain(underlying, start, end)
        ]

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)


def test_propose_skips_reprice_failed_on_missing_iv(monkeypatch: pytest.MonkeyPatch) -> None:
    from arc.ingest.scout import load_fixture_docs
    from arc.pipeline.runner import run_propose
    from arc.routines.heartbeat import RecordingNotifier

    monkeypatch.delenv("ARC_GATE_SECRET", raising=False)
    # margin profile: the fixture's SPY iron condor is only built there (cash_debit is D25 default)
    cfg = ArcSettings(_env_file=None, account_profile="margin")  # type: ignore[call-arg]
    env = PipelineEnv.fixtures()
    leg = "SPY261030P00740000"  # a leg of the fixture's SPY iron condor
    # Only the propose-time re-pricing loses the IV: the scanner menu was built earlier.
    stripped = StripIV(env.market, lambda s: s == leg)
    real_price = price_structure

    def reprice(market: Any, *a: Any, **kw: Any) -> PricedStructure:
        return real_price(stripped, *a, **kw)

    monkeypatch.setattr("arc.pipeline.steps.price_structure", reprice)
    conn = open_db(":memory:", copy=False)
    load_fixture_docs(conn)
    report = run_propose(
        conn, cfg, load_routines(), env, now=FIXTURE_NOW, notifier=RecordingNotifier()
    )

    propose = next(o for o in report.outcomes if o.job == "propose")
    assert propose.status == "ok"
    assert not report.proposals  # never reached the gate
    assert f"reprice failed (no implied volatility for {leg}" in (propose.summary or "")
    rows = conn.execute(
        "SELECT reason_code, reason_text FROM decisions WHERE reason_code = ?",
        ("reprice_failed",),
    ).fetchall()
    assert len(rows) == 1
    assert leg in rows[0]["reason_text"]
