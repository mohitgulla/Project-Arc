"""E7.17 (D84): loss-attribution fixes in the live-shaped simulator.

* the exit book marks every two-sided quote (a wide leg no longer freezes a position),
* a whole-number forward split inside the hold follows the re-struck contract,
* the attribution counterfactual knobs (estimated fill spread, size scale, per-day
  new-risk cap, excluded kinds) change only what they name.
"""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING

import pandas as pd
import pytest

from arc.backtest.sim import (
    SKIP_REASONS,
    SimSettings,
    quote_marks,
    sim_outcome,
    split_adjusted,
    split_ratios,
)
from tests.test_backtest_sim import (
    COST,
    DAYS,
    EXITS,
    _a_candidate,
    _run,
    _settings,
    _ticker,
    view,  # noqa: F401 - module fixture
)

if TYPE_CHECKING:
    from arc.backtest.data import HistoryView

# ---------------------------------------------------------------------------
# quote_marks: the exit book
# ---------------------------------------------------------------------------


def _raw(rows: list[tuple[str, float | None, float | None]], day: dt.date) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "date": [day] * len(rows),
            "symbol": [r[0] for r in rows],
            "expiration": [day + dt.timedelta(days=30)] * len(rows),
            "bid": [r[1] for r in rows],
            "ask": [r[2] for r in rows],
        }
    )


def test_quote_marks_keep_every_two_sided_quote_however_wide() -> None:
    d = DAYS[0]
    raw = _raw(
        [
            ("WIDE", 0.10, 2.00),  # 180 % wide: the entry chain drops it, exits must see it
            ("ZEROBID", 0.0, 0.05),  # a valid mark for a long leg
            ("LOCKED", 1.0, 1.0),
            ("CROSSED", 1.2, 1.0),
            ("NOASK", 0.5, 0.0),
            ("NAN", None, 1.0),
        ],
        d,
    )
    got = quote_marks(raw, pd.Series([100.0], index=[d]), dte_max=75)[d].set_index("symbol")
    assert set(got.index) == {"WIDE", "ZEROBID", "LOCKED"}
    assert got.loc["WIDE", "mid"] == pytest.approx(1.05)
    assert got.loc["WIDE", "spread"] == pytest.approx(1.90)
    assert got.loc["ZEROBID", "mid"] == pytest.approx(0.025)
    # no close that day -> no marks (like the chains); outside the DTE window -> dropped
    assert quote_marks(raw, pd.Series([100.0], index=[DAYS[1]]), dte_max=75) == {}
    assert quote_marks(raw, pd.Series([100.0], index=[d]), dte_max=20) == {}


def _book(chains: dict[dt.date, pd.DataFrame]) -> dict[dt.date, pd.DataFrame]:
    return {d: ch[["symbol", "mid", "spread"]].reset_index(drop=True) for d, ch in chains.items()}


def test_a_leg_dropped_from_the_entry_chain_no_longer_rides_to_expiry(
    view: HistoryView,  # noqa: F811
) -> None:
    """The E7.11 bug: marks came from the entry-filtered chain, so a leg wider than
    max_quoted_spread_pct made the position unmarkable and every exit waited."""
    c = _a_candidate(view)
    h = view.load("AAA")
    kw = {"closes": h.closes, "cost": COST, "exits": EXITS, "settings": _settings()}
    base = sim_outcome(c, chains=h.chains, spread_rule=False, **kw)
    assert base is not None
    if base.outcome.trade.exit_date is None:
        pytest.skip("fixture candidate held to expiry")
    sym = c.picks[0].symbol
    dropped = {d: (ch if d <= c.day else ch[ch["symbol"] != sym]) for d, ch in h.chains.items()}
    old = sim_outcome(c, chains=dropped, spread_rule=False, **kw)
    assert old is not None and old.outcome.trade.exit_date is None  # rode to expiry
    assert old.outcome.marks == {}
    new = sim_outcome(c, chains=dropped, marks=_book(h.chains), spread_rule=False, **kw)
    assert new is not None
    assert new.outcome.model_dump() == base.outcome.model_dump()


# ---------------------------------------------------------------------------
# Splits
# ---------------------------------------------------------------------------


def test_split_ratios_snap_whole_numbers_and_report_the_rest() -> None:
    idx = [dt.date(2021, 7, d) for d in (16, 19, 20, 21, 22)]
    adj = pd.Series([180.0, 181.0, 182.0, 183.0, 184.0], index=idx)
    raw = adj.copy()
    raw.iloc[:2] *= 4.0  # 4:1 effective 07-20
    assert split_ratios(raw, adj) == {idx[2]: 4.0}
    rev = adj.copy()
    rev.iloc[:3] /= 10.0  # 1:10 reverse effective 07-21
    assert split_ratios(rev, adj) == {idx[3]: pytest.approx(0.1)}
    odd = adj.copy()
    odd.iloc[:1] *= 1.5  # 3:2
    assert split_ratios(odd, adj) == {idx[1]: pytest.approx(1.5)}
    spin = adj.copy()
    spin.iloc[:4] *= 1.2  # a spin-off style adjustment
    assert split_ratios(spin, adj) == {idx[4]: pytest.approx(1.2)}
    assert split_ratios(adj, adj) == {}
    assert split_ratios(raw.iloc[:1], adj.iloc[:1]) == {}
    back = split_adjusted(raw, split_ratios(raw, adj))
    pd.testing.assert_series_equal(back, adj, check_names=False)
    assert split_adjusted(adj, {}) is adj


def _split_after(
    h_chains: dict[dt.date, pd.DataFrame], closes: pd.Series, day: dt.date, r: float
) -> tuple[dict[dt.date, pd.DataFrame], pd.Series]:
    """The same market after an r:1 split effective *day*: strikes/prices / r."""
    from arc.structures import format_occ

    chains: dict[dt.date, pd.DataFrame] = {}
    for d, ch in h_chains.items():
        if d < day:
            chains[d] = ch
            continue
        ch = ch.copy()
        ch["strike"] = ch["strike"] / r
        ch["mid"] = ch["mid"] / r
        ch["spread"] = ch["spread"] / r
        ch["symbol"] = [
            format_occ("AAA", e, rt, round(k, 3))
            for e, rt, k in zip(ch["expiration"], ch["right"], ch["strike"], strict=True)
        ]
        chains[d] = ch
    c = closes.astype(float).copy()
    c[pd.Index(c.index) >= day] /= r
    return chains, c


def test_a_forward_split_inside_the_hold_follows_the_restruck_contract(
    view: HistoryView,  # noqa: F811
) -> None:
    c = _a_candidate(view)
    h = view.load("AAA")
    kw = {"cost": COST, "exits": EXITS, "settings": _settings(), "spread_rule": False}
    base = sim_outcome(c, chains=h.chains, closes=h.closes, **kw)
    assert base is not None
    sd = next(d for d in sorted(h.chains) if d > c.day)  # split right after entry
    chains, closes = _split_after(h.chains, h.closes, sd, 4.0)
    # E7.11: the old symbols vanish -> unmarkable, settles 4x below its strikes
    blind = sim_outcome(c, chains=chains, closes=closes, **kw)
    assert blind is not None and blind.outcome.trade.exit_date is None
    assert blind.outcome.trade.pnl != pytest.approx(base.outcome.trade.pnl, abs=1.0)
    adj = sim_outcome(c, chains=chains, closes=closes, splits={sd: 4.0}, **kw)
    assert adj is not None
    a, b = adj.outcome, base.outcome
    assert a.exit_day == b.exit_day and a.trade.exit_reason == b.trade.exit_reason
    assert a.trade.pnl == pytest.approx(b.trade.pnl, abs=0.02)
    assert a.hold_to_expiry_pnl == pytest.approx(b.hold_to_expiry_pnl, abs=0.02)
    assert a.marks.keys() == b.marks.keys()
    for d, v in b.marks.items():
        assert a.marks[d] == pytest.approx(v, abs=0.02)
    # a non-standard adjustment inside the hold cannot be settled -> no outcome
    odd = sim_outcome(c, chains=chains, closes=closes, splits={sd: 1.5}, **kw)
    assert odd is None
    # a split after expiry changes nothing
    late = {DAYS[-1] + dt.timedelta(days=1): 4.0}
    same = sim_outcome(c, chains=h.chains, closes=h.closes, splits=late, **kw)
    assert same is not None and same.outcome.model_dump() == b.model_dump()


# ---------------------------------------------------------------------------
# Counterfactual knobs
# ---------------------------------------------------------------------------


def test_estimated_fill_spread_reprices_fills_only(view: HistoryView) -> None:  # noqa: F811
    c = _a_candidate(view)
    h = view.load("AAA")
    kw = {"closes": h.closes, "cost": COST, "exits": EXITS, "settings": _settings()}
    q = sim_outcome(c, chains=h.chains, spread_rule=True, **kw)
    est = COST.model_copy(update={"spread_pct": 0.5, "spread_min": 0.5})
    e = sim_outcome(c, chains=h.chains, spread_rule=True, fill_cost=est, **kw)
    assert q is not None and e is not None
    assert e.entry_ok == q.entry_ok  # the spread rule still reads the quotes
    legs_q, legs_e = q.outcome.trade.legs, e.outcome.trade.legs
    for lq, le, p in zip(legs_q, legs_e, c.picks, strict=True):
        assert le.mid == lq.mid
        assert le.spread == pytest.approx(est.spread(p.mid))
    assert e.outcome.trade.pnl < q.outcome.trade.pnl  # wider fills cost more


def test_walk_knobs_scale_size_cap_day_risk_and_drop_kinds(view: HistoryView) -> None:  # noqa: F811
    t = {"AAA": _ticker(view, "AAA"), "ZZZ": _ticker(view, "ZZZ")}
    base = _run(t)
    assert len(base.trades) > 0
    half = _run(t, sim=SimSettings(momentum_size=0, size_scale=0.5))
    m = base.trades.merge(half.trades, on=["underlying", "entry_date", "legs"], suffixes=("", "_h"))
    assert len(m) > 0
    assert (m["contracts_h"] == (m["contracts"] * 0.5).astype(int)).all()
    tiny = _run(t, sim=SimSettings(momentum_size=0, max_new_risk_pct_day=1e-6))
    assert len(tiny.trades) == 0 and tiny.skip_counts["new_risk_cap"] > 0
    assert "new_risk_cap" in SKIP_REASONS
    kinds = set(base.trades["kind"])
    gone = next(iter(kinds))
    ex = _run(t, sim=SimSettings(momentum_size=0, exclude_kinds=(gone,)))
    assert gone not in set(ex.trades["kind"])
    # defaults change nothing
    same = _run(t, sim=SimSettings(momentum_size=0, size_scale=1.0, max_new_risk_pct_day=None))
    pd.testing.assert_frame_equal(same.trades, base.trades)
