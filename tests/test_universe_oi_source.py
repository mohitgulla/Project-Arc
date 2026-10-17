"""E2.5b: the universe liquidity screen reads Cboe open interest (Alpaca fallback).

The production guard (no injected ``market_factory``) wraps its chain market in
:class:`~arc.data.oi.CboeOpenInterestMarket`, like the scanner (E2.5, D116); an
injected factory is used as given; a Cboe failure falls back to fresh Alpaca OI and
never fails the screen. Offline: Alpaca is faked, the Cboe body is built here.
"""

from __future__ import annotations

import datetime as dt
import json
from types import SimpleNamespace
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st
from structlog.testing import capture_logs

from arc.config import ArcSettings
from arc.data.base import OptionContract
from arc.data.oi import CboeOpenInterestMarket
from arc.universe.cli import _metrics_line
from arc.universe.guard import UniverseGuard
from arc.universe.screen import LiquidityMetrics, screen_oi_source
from arc.utils.calendar import ET

NOW = dt.datetime(2026, 10, 16, 11, 0, tzinfo=ET)  # Fri session; Alpaca cutoff Thu Oct 15
FRESH = dt.date(2026, 10, 15)
STALE = dt.date(2026, 10, 14)  # two sessions back: Alpaca OI not trusted
EXP = dt.date(2026, 11, 20)  # 35 DTE, in the screen window


def _settings() -> ArcSettings:
    return ArcSettings(_env_file=None, universe=["SPY", "NVDA", "XOM"])  # type: ignore[call-arg]


def _occ(strike: float, right: str) -> str:
    return f"X{EXP:%y%m%d}{right[0].upper()}{int(strike * 1000):08d}"


def _chain(alpaca_oi: int | None, oi_date: dt.date | None) -> list[OptionContract]:
    return [
        OptionContract(
            symbol=_occ(k, r),
            underlying="X",
            expiration=EXP,
            strike=k,
            option_type=r,
            bid=1.0,
            ask=1.1,
            open_interest=alpaca_oi,
            open_interest_date=oi_date,
        )
        for k in (90.0, 95.0, 100.0, 105.0, 110.0)
        for r in ("call", "put")
    ]


def _cboe_body(oi_each: int) -> bytes:
    return json.dumps(
        {
            "data": {
                "symbol": "X",
                "current_price": 100.0,
                "options": [
                    {"option": c.symbol, "bid": 1.0, "ask": 1.1, "open_interest": oi_each}
                    for c in _chain(None, None)
                ],
            }
        }
    ).encode()


class _FakeAlpaca:
    """Stands in for ``AlpacaMarketData`` (chain carries Alpaca OI + its date)."""

    chain: list[OptionContract] = []
    made: list[Any] = []

    def __init__(self, data_feed: Any = None, **_: Any) -> None:
        _FakeAlpaca.made.append(data_feed)

    def underlying_quote(self, symbol: str) -> Any:
        return SimpleNamespace(bid=99.99, ask=100.01)

    def history_bars(self, symbol: str, start: dt.date, end: dt.date, *a: Any, **k: Any) -> Any:
        days = [start + dt.timedelta(days=i) for i in range((end - start).days + 1)]
        return [
            SimpleNamespace(timestamp=dt.datetime.combine(d, dt.time(16)), volume=5e6, close=100.0)
            for d in days
        ]

    def option_chain(self, symbol: str, lo: dt.date, hi: dt.date) -> list[OptionContract]:
        return list(_FakeAlpaca.chain)


@pytest.fixture
def fake_alpaca(monkeypatch: pytest.MonkeyPatch) -> type[_FakeAlpaca]:
    import arc.data.alpaca as alpaca_mod

    _FakeAlpaca.made = []
    _FakeAlpaca.chain = []
    monkeypatch.setattr(alpaca_mod, "AlpacaMarketData", _FakeAlpaca)
    return _FakeAlpaca


def _guard() -> UniverseGuard:
    return UniverseGuard.from_settings(_settings(), now=NOW, load_master=False)


class TestDefaultMarket:
    def test_default_chain_market_is_cboe_wrapped(self, fake_alpaca: Any) -> None:
        g = _guard()
        market, adv = g._markets()  # noqa: SLF001 - the production wiring under test
        assert isinstance(market, CboeOpenInterestMarket)
        assert isinstance(market.inner, _FakeAlpaca)
        # ADV stays the bare SIP adapter (E23.0a), not wrapped
        assert isinstance(adv, _FakeAlpaca) and not isinstance(adv, CboeOpenInterestMarket)
        assert fake_alpaca.made == [None, "sip"]
        assert g._markets()[0] is market  # noqa: SLF001 - built once per run

    def test_cboe_primary(self, fake_alpaca: Any) -> None:
        fake_alpaca.chain = _chain(1, STALE)  # stale, sparse Alpaca OI
        g = _guard()
        market, _ = g._markets()  # noqa: SLF001
        urls: list[str] = []
        market._get = lambda url: urls.append(url) or _cboe_body(400)  # noqa: SLF001
        with capture_logs() as logs:
            res = g.screen("X", "loose")
        m = res.metrics
        assert m.error is None
        assert m.oi_source == "cboe"
        assert m.atm_open_interest == 400 * 6  # 3 near-ATM strikes x call/put
        assert res.passed
        assert len(urls) == 1 and "/X.json" in urls[0]
        ev = [e for e in logs if e["event"] == "universe.screen"]
        assert ev and ev[0]["oi_source"] == "cboe" and ev[0]["atm_oi"] == 2400

    def test_cboe_failure_falls_back_to_fresh_alpaca(self, fake_alpaca: Any) -> None:
        fake_alpaca.chain = _chain(150, FRESH)
        g = _guard()
        market, _ = g._markets()  # noqa: SLF001

        def boom(url: str) -> bytes:
            raise ConnectionError("cdn down")

        market._get = boom  # noqa: SLF001
        res = g.screen("X", "loose")
        assert res.metrics.error is None
        assert res.metrics.oi_source == "alpaca"
        assert res.metrics.atm_open_interest == 150 * 6

    def test_cboe_failure_and_stale_alpaca_is_unknown(self, fake_alpaca: Any) -> None:
        fake_alpaca.chain = _chain(150, STALE)
        g = _guard()
        market, _ = g._markets()  # noqa: SLF001
        market._get = lambda url: b"not json"  # noqa: SLF001
        res = g.screen("X", "loose")
        assert res.metrics.error is None  # the screen never blocks on Cboe
        assert res.metrics.atm_open_interest is None and res.metrics.oi_source is None
        assert not res.passed


class TestInjectedMarket:
    def test_injected_factory_not_rewrapped(self, fake_alpaca: Any) -> None:
        inner = _FakeAlpaca()
        wrapped = CboeOpenInterestMarket(inner, get=lambda u: _cboe_body(9), today=NOW.date)  # type: ignore[arg-type]
        for given_market in (inner, wrapped):
            g = UniverseGuard.from_settings(
                _settings(), now=NOW, load_master=False, market_factory=lambda m=given_market: m
            )
            market, adv = g._markets()  # noqa: SLF001
            assert market is given_market and adv is given_market

    def test_bare_injected_market_reports_no_source(self, fake_alpaca: Any) -> None:
        fake_alpaca.chain = _chain(300, FRESH)  # bare chain: no oi_source set
        inner = _FakeAlpaca()
        g = UniverseGuard.from_settings(
            _settings(), now=NOW, load_master=False, market_factory=lambda: inner
        )
        res = g.screen("X", "loose")
        assert res.metrics.atm_open_interest == 1800 and res.metrics.oi_source is None


class TestSourceLabel:
    def test_labels(self) -> None:
        assert screen_oi_source([]) is None
        assert screen_oi_source([None, None]) is None
        assert screen_oi_source(["cboe", "cboe", None]) == "cboe"
        assert screen_oi_source(["alpaca"]) == "alpaca"
        assert screen_oi_source(["cboe", "alpaca"]) == "mixed"

    @given(st.lists(st.sampled_from(["cboe", "alpaca", None])))
    def test_label_property(self, srcs: list[str | None]) -> None:
        got = screen_oi_source(srcs)
        known = {s for s in srcs if s is not None}
        assert got == (None if not known else "mixed" if len(known) > 1 else known.pop())

    def test_metrics_round_trip_and_cli_line(self) -> None:
        m = LiquidityMetrics(
            ticker="X", as_of=NOW.date(), price=100.0, atm_open_interest=2400, oi_source="mixed"
        )
        dumped = m.model_dump(mode="json")
        assert dumped["oi_source"] == "mixed"
        assert LiquidityMetrics.model_validate(dumped) == m
        assert "OI 2400 (mixed)" in _metrics_line(dumped)
        assert "OI - (-)" in _metrics_line(
            LiquidityMetrics(ticker="X", as_of=NOW.date()).model_dump(mode="json")
        )
