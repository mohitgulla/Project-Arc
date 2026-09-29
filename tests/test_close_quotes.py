"""E6.2a: the close quote check (fresh, synced, tight, on the strike curve) and its wiring.

The live evidence is pinned in ``tests/data/e62a_spy_indicative_reads.json``: two
reads of the SPY Oct-30 call chain from Alpaca's indicative feed, 5 minutes
apart. Every leg in both reads is fresh (quote <= 2 s old), synced and tight
(spreads <= 0.4%), yet in the ``bad`` read the 768C/769C vertical's mid is ~$0.25
away from what the neighbouring strikes say it is worth. A close priced off
that read starts its band outside the market (the 2026-09-29 cleanup miss).
"""

from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal as D
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given
from hypothesis import settings as hsettings
from hypothesis import strategies as st

from arc.config import ArcSettings
from arc.data.base import OptionContract, UnderlyingQuote
from arc.gate.inputs import MarketSnapshot, Quote
from arc.gate.rules import combo_nbbo, price_band
from arc.models import Leg, LegIntent
from arc.pipeline.market import (
    LegQuote,
    close_quote_sanity,
    curve_mid,
    limit_price,
    market_snapshot,
    price_structure,
)

FIXTURE = Path(__file__).parent / "data" / "e62a_spy_indicative_reads.json"
EXP = dt.date(2026, 10, 30)
C768 = "SPY261030C00768000"
C769 = "SPY261030C00769000"
CLOSE = [(C768, LegIntent.SHORT, 1), (C769, LegIntent.LONG, 1)]  # close a long 768/769 call spread
NOW = dt.datetime(2026, 9, 29, 11, 0, tzinfo=dt.UTC)


def cfg(**kw: Any) -> ArcSettings:
    return ArcSettings(_env_file=None, **kw)  # type: ignore[arg-type]


def lq(sym: str, bid: float, ask: float, *, side: LegIntent = LegIntent.LONG, age: float = 1.0,
       curve: float | None = None, ratio: int = 1) -> LegQuote:  # fmt: skip
    mid = (bid + ask) / 2
    return LegQuote(
        symbol=sym, side=side, ratio=ratio, bid=bid, ask=ask, mid=mid,
        quote_ts=NOW - dt.timedelta(seconds=age),
        spread_pct=(ask - bid) / mid if mid else None, curve_mid=curve,
    )  # fmt: skip


def sane() -> list[LegQuote]:
    return [
        lq(C768, 11.01, 11.04, side=LegIntent.SHORT, curve=11.03),
        lq(C769, 10.35, 10.36, curve=10.40),
    ]


def codes(problems: list[str]) -> list[str]:
    return [p.split(":", 1)[0] for p in problems]


# ---------------------------------------------------------------------------
# close_quote_sanity (pure)
# ---------------------------------------------------------------------------


def test_sane_quotes_pass() -> None:
    assert close_quote_sanity(sane(), NOW, cfg()) == []


def test_stale_leg_blocks_with_its_own_timestamp() -> None:
    q = sane()
    q[1] = lq(C769, 10.35, 10.36, age=61, curve=10.40)
    out = close_quote_sanity(q, NOW, cfg(close_quote_max_skew_seconds=600))
    assert codes(out) == ["stale"]
    assert C769 in out[0] and "61s old (max 60s" in out[0]


def test_future_quote_is_stale() -> None:
    q = sane()
    q[0] = lq(C768, 11.01, 11.04, side=LegIntent.SHORT, age=-5, curve=11.03)
    assert codes(close_quote_sanity(q, NOW, cfg())) == ["stale"]


def test_skewed_legs_block() -> None:
    q = sane()
    q[1] = lq(C769, 10.35, 10.36, age=40, curve=10.40)  # fresh, but 39 s after the other leg
    out = close_quote_sanity(q, NOW, cfg())
    assert codes(out) == ["skew"]
    assert "39s apart (max 30s)" in out[0]


def test_wide_spread_blocks() -> None:
    q = sane()
    q[0] = lq(C768, 10.40, 11.70, side=LegIntent.SHORT, curve=11.03)  # 11.8% of mid, $1.30
    out = close_quote_sanity(q, NOW, cfg())
    assert codes(out) == ["spread"]
    assert C768 in out[0]


def test_cheap_wing_passes_on_the_dollar_cap() -> None:
    q = [lq("SPY261030C00800000", 0.05, 0.10), lq("SPY261030C00805000", 0.01, 0.03)]
    assert close_quote_sanity(q, NOW, cfg()) == []  # 67% / 100% of mid, but <= $0.10
    assert codes(close_quote_sanity(q, NOW, cfg(close_quote_max_spread_abs=0.02))) == ["spread"]


def test_missing_or_crossed_quote_blocks() -> None:
    q = sane()
    q[1] = LegQuote(symbol=C769, side=LegIntent.LONG, ratio=1, bid=None, ask=10.36, mid=None)
    assert codes(close_quote_sanity(q, NOW, cfg())) == ["missing"]
    q[1] = lq(C769, 10.40, 10.30, curve=10.40)  # crossed
    assert "missing" in codes(close_quote_sanity(q, NOW, cfg()))


def test_combo_off_the_strike_curve_blocks() -> None:
    q = sane()
    q[1] = lq(C769, 10.10, 10.12, curve=10.40)  # vertical mid 0.925 vs curve 0.63
    out = close_quote_sanity(q, NOW, cfg())
    assert codes(out) == ["off_curve"]
    assert "(max $0.15)" in out[0]


def test_curve_check_skipped_without_a_curve_value() -> None:
    q = sane()
    q[1] = lq(C769, 10.10, 10.12, curve=None)  # chain edge: no neighbours on one side
    assert close_quote_sanity(q, NOW, cfg()) == []


def test_every_problem_is_reported_at_once() -> None:
    q = [
        lq(C768, 10.0, 12.0, side=LegIntent.SHORT, age=200, curve=11.0),
        lq(C769, 10.35, 10.36, curve=10.40),
    ]
    assert sorted(codes(close_quote_sanity(q, NOW, cfg()))) == [
        "skew", "spread", "stale"
    ]  # fmt: skip


def test_thresholds_are_config_driven() -> None:
    q = sane()
    q[1] = lq(C769, 10.35, 10.36, age=90, curve=10.40)
    loose = cfg(close_quote_max_age_seconds=120, close_quote_max_skew_seconds=120)
    assert close_quote_sanity(q, NOW, loose) == []


def test_control_panel_override_reaches_the_check() -> None:
    from arc.control.service import ControlService
    from arc.store.db import connect
    from arc.store.migrate import migrate

    conn = connect(":memory:")
    migrate(conn)
    owner = "U0OWNER001"
    svc = ControlService(conn, base=cfg(approver_slack_user_ids=[owner]), now=lambda: NOW)
    q = sane()
    q[1] = lq(C769, 10.35, 10.36, age=45, curve=10.40)
    assert codes(close_quote_sanity(q, NOW, svc.settings())) == ["skew"]
    res = svc.set("close_quote.max_skew_seconds", "60", actor=owner, source="slack")
    assert res.outcome == "pending"  # a looser guard is the riskier direction
    assert res.pending is not None
    assert svc.confirm(res.pending.code, actor=owner, source="slack").outcome == "applied"
    assert close_quote_sanity(q, NOW, svc.settings()) == []


# ---------------------------------------------------------------------------
# curve_mid
# ---------------------------------------------------------------------------


def _contract(sym: str, bid: float, ask: float) -> OptionContract:
    return OptionContract(
        symbol=sym, underlying="SPY", expiration=EXP, strike=int(sym[-8:]) / 1000,
        option_type="call" if sym[-9] == "C" else "put", bid=bid, ask=ask, mid=(bid + ask) / 2,
    )  # fmt: skip


def test_curve_mid_recovers_a_smooth_curve_and_ignores_the_leg_itself() -> None:
    chain = [
        _contract(f"SPY261030C00{k:03d}000", p - 0.01, p + 0.01)
        for k, p in ((k, 0.02 * (780 - k) ** 2 / 10 + 1) for k in range(760, 776))
    ]
    fair = 0.02 * (780 - 768) ** 2 / 10 + 1
    bumped = [c if c.symbol != C768 else _contract(C768, fair + 0.5, fair + 0.52) for c in chain]
    assert curve_mid(bumped, C768) == pytest.approx(fair, abs=1e-6)


def test_curve_mid_needs_neighbours_on_both_sides() -> None:
    edge = [_contract(f"SPY261030C00{k:03d}000", 5.0 - k / 1000, 5.02 - k / 1000)
            for k in range(769, 776)]  # fmt: skip
    assert curve_mid(edge, C768) is None  # nothing below 768
    assert curve_mid(edge[:3], "SPY261030C00772000") is None  # fewer than 4 neighbours
    puts = [c.model_copy(update={"symbol": c.symbol.replace("C0", "P0")}) for c in edge]
    assert curve_mid(puts, "SPY261030C00772000") is None  # other type never counts


# ---------------------------------------------------------------------------
# The captured live case (root cause evidence)
# ---------------------------------------------------------------------------


class FixtureMarket:
    """One captured read of the SPY Oct-30 call chain as a MarketDataProvider."""

    def __init__(self, read: dict[str, Any]) -> None:
        self.read = read

    def option_chain(self, underlying: str, a: dt.date, b: dt.date) -> list[OptionContract]:
        out = []
        for sym, q in self.read["quotes"].items():
            c = _contract(sym, q["bid"], q["ask"])
            out.append(c.model_copy(update={
                "bid_size": q["bs"], "ask_size": q["as"],
                "quote_timestamp": dt.datetime.fromisoformat(q["ts"]),
            }))  # fmt: skip
        return out

    def underlying_quote(self, symbol: str) -> UnderlyingQuote:
        s = self.read["spot"]
        return UnderlyingQuote(
            symbol=symbol, bid=s, ask=s, mid=s,
            timestamp=dt.datetime.fromisoformat(self.read["spot_ts"]),
        )  # fmt: skip


def _read(name: str) -> dict[str, Any]:
    return json.loads(FIXTURE.read_text())["reads"][name]


def _priced(name: str) -> tuple[Any, dt.datetime]:
    r = _read(name)
    fetched = dt.datetime.fromisoformat(r["fetched_at"])
    return price_structure(
        FixtureMarket(r), CLOSE, as_of=fetched.date(), r=0.04, require_iv=False
    ), fetched


def test_bad_read_is_fresh_synced_and_tight_but_off_the_curve() -> None:
    priced, fetched = _priced("bad")
    quotes = priced.leg_quotes()
    assert all((fetched - q.quote_ts).total_seconds() <= 2 for q in quotes if q.quote_ts)
    assert all(q.spread_pct is not None and q.spread_pct < 0.02 for q in quotes)
    problems = close_quote_sanity(quotes, fetched, cfg())
    assert codes(problems) == ["off_curve"], problems
    # the one read's combo is ~$0.25 richer than its neighbours say
    assert priced.structure.net_debit_credit == D("-0.805")


def test_good_read_passes() -> None:
    priced, fetched = _priced("good")
    assert close_quote_sanity(priced.leg_quotes(), fetched, cfg()) == []


def test_bad_read_band_starts_outside_the_market() -> None:
    """The failure: a band built from the bad read never reaches the tradeable price."""
    s = cfg()
    bad, _ = _priced("bad")
    good, _ = _priced("good")
    limit = limit_price(bad.structure.net_debit_credit, s.limit_tick)
    band = price_band(bad.structure.legs, limit, market_snapshot(bad.contracts, {}), s)
    lo, hi = combo_nbbo(good.structure.legs, market_snapshot(good.contracts, {})) or (None, None)
    assert lo is not None and hi is not None
    # every attempt asks for more credit (a lower number) than the best bid in the clean read
    assert band.hi < lo, (band, lo, hi)


# ---------------------------------------------------------------------------
# Property: a band from sane quotes never starts outside the combo NBBO
# ---------------------------------------------------------------------------

_cents = st.integers(min_value=5, max_value=5000)


@st.composite
def leg_books(draw: Any) -> list[tuple[Leg, Quote]]:
    n = draw(st.integers(min_value=1, max_value=4))
    out = []
    for i in range(n):
        bid = draw(_cents)
        spread = draw(st.integers(min_value=0, max_value=max(1, bid // 10)))
        side = draw(st.sampled_from([LegIntent.LONG, LegIntent.SHORT]))
        ratio = draw(st.integers(min_value=1, max_value=3))
        b, a = D(bid) / 100, D(bid + spread) / 100
        sym = f"SPY261030C00{700 + i:03d}000"
        out.append((Leg(occ_symbol=sym, side=side, ratio=ratio, premium=(b + a) / 2),
                    Quote(bid=b, ask=a, as_of=NOW)))  # fmt: skip
    return out


@given(book=leg_books(), steps=st.integers(min_value=0, max_value=6),
       reach=st.floats(min_value=0.0, max_value=1.0))  # fmt: skip
@hsettings(max_examples=300, deadline=None)
def test_band_from_sane_quotes_stays_inside_the_combo_nbbo(
    book: list[tuple[Leg, Quote]], steps: int, reach: float
) -> None:
    s = cfg(execution_improvement_steps=steps, execution_band_reach=reach)
    legs = [leg for leg, _ in book]
    snap = MarketSnapshot(quotes={leg.occ_symbol: q for leg, q in book}, next_earnings={})
    net = sum(
        (leg.ratio * (q.bid + q.ask) / 2 * (1 if leg.side == LegIntent.LONG else -1))
        for leg, q in book
    )
    limit = limit_price(D(net), s.limit_tick)
    band = price_band(legs, limit, snap, s)
    nbbo = combo_nbbo(legs, snap)
    assert nbbo is not None
    lo, hi = nbbo
    assert lo <= band.lo <= band.hi <= hi, (band, nbbo)
    for p in band.ladder(D(str(s.limit_tick))):
        assert lo <= p <= hi
