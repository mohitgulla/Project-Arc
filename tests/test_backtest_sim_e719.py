"""E7.19 (D125): the live-shaped simulator trades both entry-DTE sleeves.

* menus per sleeve window, every expiration (capped per sleeve), tagged with the
  sleeve (``ArcSettings.sleeve_for_dte``); a single-window profile is one ``default``
  sleeve and its book is byte-identical to pre-E7.19 main (golden);
* per-sleeve slot caps through the pure ``check_sleeve_positions`` (``sleeve_full``);
* each position's exit block resolved at entry (``exit_block_for_dte``);
* the intraday-rule daily-range proxy: hand-computed fill, stop-first, no look-ahead;
* the YAML sleeve windows and exit blocks drive all of it (no hard-coded sleeve).
"""

from __future__ import annotations

import datetime as dt
import io
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd
import pytest
import yaml

from arc.backtest.engine import MULT, open_trade
from arc.backtest.sim import (
    DEFAULT_SLEEVE,
    GATE_RULES_APPLIED,
    PROXY_EXITS,
    SKIP_REASONS,
    SimCandidate,
    SimSettings,
    SimTicker,
    proxy_marks,
    range_proxy_rule,
    session_ranges,
    sim_outcome,
    sim_ticker_from_chains,
    sleeve_windows,
)
from arc.backtest.sim_report import sleeve_table
from arc.backtest.strategies import ExpiryMode, capped_expirations
from arc.config import ArcSettings
from arc.exits.policy import load_exit_config, resolve_rules
from arc.pricing.bs import price_vectorized
from arc.scanner.rank import Ranker
from arc.structures import analyze
from tests import sim_margin_golden
from tests.golden_compare import assert_json_close
from tests.test_backtest_sim import (
    COST,
    DAYS,
    DECIDE,
    EXITS,
    _cfg,
    _days,
    _run,
    _settings,
    view,  # noqa: F401 - module fixture
)

if TYPE_CHECKING:
    from arc.backtest.data import HistoryView

REPO = Path(__file__).resolve().parent.parent


def _cash(hv: HistoryView, t: str = "AAA", *, settings: ArcSettings | None = None, **kw: object):  # noqa: ANN202
    s = settings or _settings("cash_debit")
    h = hv.load(t)
    sim = kw.pop("sim", None) or SimSettings(regime_model="v1")
    return sim_ticker_from_chains(
        t,
        h.chains,
        h.closes,
        decision_days=DECIDE,
        days=_days(100.0),
        cfg=_cfg(),
        exits=kw.pop("exits", EXITS),  # type: ignore[arg-type]
        cost=COST,
        settings={"cash_debit": s},
        windows={"cash_debit": s.entry_dte_window},
        rankers={"cash_debit": [Ranker.MANAGED_NET_EV]},
        stances=None,
        sim=sim,  # type: ignore[arg-type]
        ohlc=h.ohlc,
    )


@pytest.fixture(scope="module")
def cash(view: HistoryView) -> SimTicker:  # noqa: F811
    return _cash(view)


def _dte(sc: SimCandidate) -> int:
    return (sc.candidate.picks[0].expiration - sc.candidate.day).days


# ---------------------------------------------------------------------------
# Margin golden: the single-window profile is unchanged
# ---------------------------------------------------------------------------


def test_margin_nearest_book_matches_pre_e719_main(tmp_path: Path) -> None:
    text = sim_margin_golden.trades_csv(tmp_path)
    new = pd.read_csv(io.StringIO(text))
    gold_text = sim_margin_golden.GOLDEN.read_text()
    gold = pd.read_csv(io.StringIO(gold_text))
    assert len(gold) > 0
    # E7.19 only appends columns; main's columns keep their order
    assert list(new.columns[: len(gold.columns)]) == list(gold.columns)
    assert list(new.columns[len(gold.columns) :]) == ["sleeve", "dte_entry"]
    assert (new["sleeve"] == DEFAULT_SLEEVE).all()
    assert_json_close(
        sim_margin_golden.records(new[list(gold.columns)].to_csv(index=False)),
        sim_margin_golden.records(gold_text),
    )


def _single_window(s: ArcSettings) -> ArcSettings:
    """*s* with its entry-DTE sleeves removed (a one-window profile)."""
    return s.model_copy(
        update={"account_profile_spec": s.profile.model_copy(update={"sleeves": None})}
    )


def test_single_window_profile_is_one_default_sleeve() -> None:
    # margin gained the cash_debit sleeves in D128, so drop them for a one-window profile
    m = _single_window(_settings("margin"))
    assert [x.name for x in m.entry_sleeves] == [DEFAULT_SLEEVE]
    (w,) = sleeve_windows(m, m.entry_dte_window)
    assert (w.name, (w.dte_min, w.dte_max), w.exit_block) == (
        DEFAULT_SLEEVE,
        m.entry_dte_window,
        None,
    )
    (w,) = sleeve_windows(m, (20, 45))
    assert (w.name, w.dte_min, w.dte_max, w.exit_block) == (DEFAULT_SLEEVE, 20, 45, None)
    # an explicit override keeps a sleeve profile on one window too (the golden's path)
    c = _settings("cash_debit")
    (o,) = sleeve_windows(c, (20, 45), override=True)
    assert o.name == DEFAULT_SLEEVE
    assert [x.name for x in sleeve_windows(_settings("margin"), (10, 60))] == ["short", "swing"]


# ---------------------------------------------------------------------------
# Expiry cap
# ---------------------------------------------------------------------------


def test_capped_expirations_low_middle_high() -> None:
    base = dt.date(2025, 1, 1)
    dte = {base + dt.timedelta(days=d): d for d in (10, 14, 17, 21, 24, 28, 31, 35)}
    got = capped_expirations(dte, 10, 35, 3)
    assert [dte[e] for e in got] == [10, 21, 35]  # targets 10, 22.5 (tie -> 21), 35
    assert [dte[e] for e in capped_expirations(dte, 10, 35, 1)] == [21]
    # two targets on one expiry keep it once
    two = {base + dt.timedelta(days=d): d for d in (12, 30)}
    assert [two[e] for e in capped_expirations(two, 10, 35, 3)] == [12, 30]


def test_spec_dump_drops_unset_e719_fields() -> None:
    from arc.backtest.strategies import StrategyKind, StrategySpec

    s = StrategySpec(kind=StrategyKind.LONG_CALL, delta=0.5, dte_min=10, dte_max=35)
    assert "max_expiries" not in s.model_dump() and "exit_block" not in s.model_dump()
    c = s.model_copy(update={"max_expiries": 3, "exit_block": "short"})
    assert c.model_dump()["max_expiries"] == 3 and c.model_dump()["exit_block"] == "short"


def test_ranking_yaml_menus_trade_every_expiry_capped_at_three() -> None:
    from arc.backtest.ranking import load_ranking_file

    for prof, menus in load_ranking_file().backtest.menus.items():
        for m in menus:
            assert m.expiry_mode is ExpiryMode.ALL, prof
            assert m.max_expiries_per_sleeve == 3, prof


# ---------------------------------------------------------------------------
# Sleeve tagging and per-sleeve exit blocks
# ---------------------------------------------------------------------------


def test_cash_debit_menu_covers_both_sleeves_tagged_by_dte(cash: SimTicker) -> None:
    s = _settings("cash_debit")
    seen: set[str] = set()
    for cs in cash.menus["cash_debit"].values():
        for sc in cs:
            sl = s.sleeve_for_dte(_dte(sc))
            assert sl is not None and sc.sleeve == sl.name
            seen.add(sc.sleeve)
    assert seen == {x.name for x in s.entry_sleeves} == {"short", "swing"}
    # per name and sleeve, at most per_name_depth paths per ranker per day
    for cs in cash.menus["cash_debit"].values():
        for name in seen:
            assert sum(1 for sc in cs if sc.sleeve == name) <= SimSettings().per_name_depth


def test_short_sleeve_positions_use_the_short_exit_block(cash: SimTicker) -> None:
    short = EXITS.sleeves["short"].kinds
    reasons = {"short": set(), "swing": set()}
    for cs in cash.menus["cash_debit"].values():
        for sc in cs:
            if sc.result is None:
                continue
            tr = sc.result.outcome.trade
            reasons[sc.sleeve].add(tr.exit_reason)
            if sc.sleeve == "short" and tr.exit_reason == "dte_exit":
                kind = analyze(list(_legs_of(sc)), as_of=sc.candidate.day).kind
                assert kind is not None
                close_at = short[kind].close_at_dte
                assert (tr.expiration - tr.exit_date).days <= close_at  # type: ignore[operator]
    # the short block's intraday stop shows up only on the short sleeve
    assert PROXY_EXITS["stop"] in reasons["short"]
    assert not reasons["swing"] & set(PROXY_EXITS.values())


def _legs_of(sc: SimCandidate):  # noqa: ANN202
    from arc.backtest.ranking import _legs

    return _legs(sc.candidate.picks, sc.candidate.underlying)


def test_exit_block_is_resolved_at_entry_through_the_live_call(
    view: HistoryView,  # noqa: F811
    cash: SimTicker,
) -> None:
    sc = next(
        sc
        for cs in cash.menus["cash_debit"].values()
        for sc in cs
        if sc.sleeve == "short" and sc.result is not None
    )
    h = view.load("AAA")
    kw = {
        "chains": h.chains,
        "closes": h.closes,
        "cost": COST,
        "exits": EXITS,
        "settings": _settings("cash_debit"),
        "spread_rule": True,
    }
    on = sim_outcome(sc.candidate, **kw)  # type: ignore[arg-type]
    off = sim_outcome(sc.candidate, sleeves=False, **kw)  # type: ignore[arg-type]
    assert on is not None and off is not None
    # 0.40 x debit TP / close_at_dte 3 (short) vs 0.60 / 7 (kind policy): they differ
    assert on.outcome.trade.model_dump() != off.outcome.trade.model_dump()


# ---------------------------------------------------------------------------
# Per-sleeve slot caps
# ---------------------------------------------------------------------------


def test_sleeve_slot_caps_through_the_pure_gate_rule(cash: SimTicker) -> None:
    assert any("check_sleeve_positions" in r for r in GATE_RULES_APPLIED)
    assert {"sleeve_full", "gate_dte_window"} <= set(SKIP_REASONS)
    base = _settings("cash_debit")
    big = SimSettings(momentum_size=0, capacity="off", equity=250_000)
    free = _run({"AAA": cash}, profile="cash_debit", settings=base, sim=big,
                ranker=Ranker.MANAGED_NET_EV)  # fmt: skip
    capped_profile = base.profile.model_copy(
        update={
            "sleeves": [
                v.model_copy(update={"max_positions": 1}) for v in base.profile.sleeves or []
            ]
        }
    )
    one = base.model_copy(update={"account_profile_spec": capped_profile})
    run = _run({"AAA": cash}, profile="cash_debit", settings=one, sim=big,
               ranker=Ranker.MANAGED_NET_EV)  # fmt: skip
    assert run.skip_counts["sleeve_full"] > 0
    full = run.skips[run.skips.reason == "sleeve_full"]
    assert set(full["detail"]) <= {"short", "swing"}
    t = run.trades
    assert len(t) > 0 and len(t) < len(free.trades)
    # never two open positions in one sleeve
    for name, g in t.groupby("sleeve"):
        spans = sorted(zip(g["entry_date"], g["exit_date"].fillna(g["expiration"]), strict=True))
        for (_, e1), (s2, _) in zip(spans, spans[1:], strict=False):
            assert s2 >= e1, name
    assert set(free.trades["sleeve"]) == {"short", "swing"}


def test_sleeve_table_rows_per_sleeve(cash: SimTicker) -> None:
    run = _run({"AAA": cash}, profile="cash_debit", settings=_settings("cash_debit"),
               sim=SimSettings(momentum_size=0, capacity="off", equity=250_000),
               ranker=Ranker.MANAGED_NET_EV)  # fmt: skip
    tab = sleeve_table(run.trades)
    assert list(tab["sleeve"]) == ["short", "swing", "all"]
    allrow = tab[tab.sleeve == "all"].iloc[0]
    assert allrow["n"] == len(run.trades)
    assert allrow["net"] == pytest.approx(round(run.trades["pnl"].sum(), 2))
    assert tab["n"].iloc[:2].sum() == allrow["n"]
    short = tab[tab.sleeve == "short"].iloc[0]
    assert 10 <= short["mean_dte_entry"] <= 35
    # Σ pnl = Δequity once every trade has settled (data end)
    assert run.equity.iloc[-1] - 250_000 == pytest.approx(run.trades["pnl"].sum(), abs=0.01)
    assert sleeve_table(run.trades.iloc[0:0]).empty


# ---------------------------------------------------------------------------
# The YAML drives the sleeves
# ---------------------------------------------------------------------------


def test_sleeve_window_and_exit_block_follow_the_yaml(view: HistoryView, tmp_path: Path) -> None:  # noqa: F811
    prof = yaml.safe_load((REPO / "config" / "account_profiles.yaml").read_text())
    prof["profiles"]["cash_debit"]["sleeves"] = {
        "near": {"dte_min": 10, "dte_max": 20, "max_positions": 5, "exit_policy": "near"},
        "far": {"dte_min": 21, "dte_max": 45, "max_positions": 5},
    }
    pf = tmp_path / "account_profiles.yaml"
    pf.write_text(yaml.safe_dump(prof))
    ex = yaml.safe_load((REPO / "config" / "exits.yaml").read_text())
    ex["sleeves"] = {"near": ex["sleeves"]["short"]}
    for k in ex["sleeves"]["near"]["kinds"].values():
        k["close_at_dte"] = 9
    xf = tmp_path / "exits.yaml"
    xf.write_text(yaml.safe_dump(ex))
    s = ArcSettings(account_profiles_file=pf).with_profile("cash_debit")
    exits = load_exit_config(xf)
    assert [(w.name, w.dte_min, w.dte_max, w.exit_block) for w in sleeve_windows(s, (10, 45))] == [
        ("near", 10, 20, "near"),
        ("far", 21, 45, None),
    ]
    st = _cash(view, settings=s, exits=exits)
    names: set[str] = set()
    for cs in st.menus["cash_debit"].values():
        for sc in cs:
            dte = _dte(sc)
            names.add(sc.sleeve)
            assert sc.sleeve == ("near" if dte <= 20 else "far")
            r = sc.result
            if sc.sleeve == "near" and r is not None and r.outcome.trade.exit_reason == "dte_exit":
                tr = r.outcome.trade
                assert (tr.expiration - tr.exit_date).days <= 9  # type: ignore[operator]
    assert names == {"near", "far"}


# ---------------------------------------------------------------------------
# Intraday-rule daily-range proxy
# ---------------------------------------------------------------------------

DAY = dt.date(2025, 3, 4)
EXP = dt.date(2025, 3, 28)


def _long_call_rules():  # noqa: ANN202 - (rules, legs)
    from arc.backtest.strategies import LegPick

    pick = LegPick(
        symbol="AAA250328C00105000",
        expiration=EXP,
        strike=105.0,
        right="call",
        side=1,
        mid=2.00,
        spread=0.04,
        iv=0.25,
        delta=0.4,
    )
    from arc.backtest.ranking import _legs

    legs = _legs([pick], "AAA")
    structure = analyze(legs, as_of=dt.date(2025, 3, 3))
    rules = resolve_rules(structure, EXITS.policy_for(structure.kind, "short"))
    return rules, pick


def test_proxy_reprices_at_the_extreme_at_the_eod_iv() -> None:
    _, pick = _long_call_rules()
    t = (EXP - DAY).days / 365.0
    spot, iv, r = 100.0, 0.30, 0.04
    eod_mid = float(price_vectorized(np.array(["c"]), np.array([spot]), np.array([105.0]),
                                     np.array([t]), r, np.array([iv]))[0])  # fmt: skip
    marks = {pick.symbol: (eod_mid, 0.05)}
    out = proxy_marks([pick], marks, spot=spot, extreme=97.0, expiration=EXP, day=DAY, r=r)
    assert out is not None
    want = float(price_vectorized(np.array(["c"]), np.array([97.0]), np.array([105.0]),
                                  np.array([t]), r, np.array([iv]))[0])  # fmt: skip
    assert out[pick.symbol][0] == pytest.approx(want, abs=1e-6)
    assert out[pick.symbol][1] == 0.05  # the EOD quoted spread is kept


def _bs(s: float, k: float = 105.0, iv: float = 0.30, r: float = 0.04) -> float:
    t = (EXP - DAY).days / 365.0
    one = np.ones(1)
    return float(price_vectorized(np.array(["c"]), one * s, one * k, one * t, one * r, one * iv)[0])


def test_proxy_stop_fill_hand_computed() -> None:
    """A long call, short-sleeve stop 0.50 x debit, intraday: the day's low fires it.

    EOD: spot 100, the call marks at its 30-vol BSM value, 20 % under the entry
    debit (above the stop). The low is the first 0.25 step whose 30-vol value is
    below half the debit; the proxy re-solves 30 vol from the EOD mid and prices
    the leg there, and the close pays the cost model's spread on that price.
    """
    from arc.backtest.engine import close_early
    from arc.backtest.ranking import _legs
    from arc.backtest.strategies import LegPick, StrategyKind, StrategySpec
    from arc.exits.policy import ExitReason

    eod_mid = _bs(100.0)
    entry = eod_mid / 0.8
    pick = LegPick(symbol="AAA250328C00105000", right="call", strike=105.0, expiration=EXP,
                   side=1, mid=entry, spread=0.04, delta=0.4, iv=0.3)  # fmt: skip
    structure = analyze(_legs([pick], "AAA"), as_of=dt.date(2025, 3, 3))
    rules = resolve_rules(structure, EXITS.policy_for(structure.kind, "short"))
    assert rules.entry_net == pytest.approx(entry, abs=1e-6)
    assert rules.stop_pnl == pytest.approx(-0.5 * entry, abs=1e-6)
    assert not rules.policy.stop_eod_only
    lo = 100.0
    while _bs(lo) - entry > -0.5 * entry:
        lo -= 0.25
    eod = {pick.symbol: (eod_mid, 0.04)}
    hit = range_proxy_rule(rules, [pick], eod, spot=100.0, rng=(101.0, lo), expiration=EXP,
                           day=DAY, r=0.04, peak=None)  # fmt: skip
    assert hit is not None
    rule, pm, x = hit
    assert (rule, x) == ("stop", lo)
    want = _bs(lo)  # 30 vol (the EOD IV) at the low
    assert pm[pick.symbol] == (pytest.approx(want, abs=1e-6), 0.04)
    spec = StrategySpec(kind=StrategyKind.LONG_CALL, delta=0.4, dte_min=10, dte_max=35)
    o = open_trade([pick], spec=spec, underlying="AAA", entry_date=dt.date(2025, 3, 3),
                   spot=100.0, cost=COST)  # fmt: skip
    assert o is not None
    tr = close_early(o, day=DAY, marks=pm, spot=x, reason=ExitReason.STOP, cost=COST)
    sold = COST.fill(want, 0.04, -1)
    assert sold < want  # selling the long pays the spread
    assert tr.pnl == pytest.approx((sold - COST.fill(entry, 0.04, 1)) * MULT - tr.fees, abs=1e-6)
    # the EOD rules alone would have held
    assert eod_mid - entry > rules.stop_pnl  # type: ignore[operator]


def test_proxy_takes_the_stop_before_the_lock_when_both_touch() -> None:
    rules, pick = _long_call_rules()
    entry = rules.entry_net
    assert rules.policy.profit_lock is not None and not rules.policy.stop_eod_only
    t = (EXP - DAY).days / 365.0
    iv, r, k = 0.30, 0.04, 105.0

    def bs(s: float) -> float:
        return float(price_vectorized(np.array(["c"]), np.array([s]), np.array([k]),
                                      np.array([t]), r, np.array([iv]))[0])  # fmt: skip

    spot = 100.0
    marks = {pick.symbol: (bs(spot), 0.04)}
    arm = rules.lock_arm_pnl
    assert arm is not None
    # peak armed, the low both breaches the stop and is below the lock floor
    lo = spot
    while bs(lo) - entry > rules.stop_pnl:  # type: ignore[operator]
        lo -= 0.5
    hit = range_proxy_rule(rules, [pick], marks, spot=spot, rng=(spot, lo), expiration=EXP,
                           day=DAY, r=r, peak=arm + 0.01)  # fmt: skip
    assert hit is not None and hit[0] == "stop"


def test_eod_only_rules_never_take_the_proxy() -> None:
    from arc.backtest.ranking import _legs
    from arc.backtest.strategies import LegPick

    pick = LegPick(symbol="AAA250328C00105000", expiration=EXP, strike=105.0, right="call",
                   side=1, mid=2.0, spread=0.04, iv=0.25, delta=0.4)  # fmt: skip
    legs = _legs([pick], "AAA")
    structure = analyze(legs, as_of=dt.date(2025, 3, 3))
    swing = resolve_rules(structure, EXITS.policy_for(structure.kind))  # stop EOD-only
    marks = {pick.symbol: (2.0, 0.04)}
    # the swing kind policy keeps an intraday lock: unarmed (peak None) -> nothing fires
    assert range_proxy_rule(swing, [pick], marks, spot=100.0, rng=(100.0, 50.0),
                            expiration=EXP, day=DAY, r=0.04, peak=None) is None  # fmt: skip


def test_session_ranges_are_raw_strike_units_and_contain_the_close() -> None:
    idx = [dt.date(2025, 3, 3), dt.date(2025, 3, 4)]
    closes = pd.Series([200.0, 100.0], index=idx)  # raw: a 2:1 split between the days
    ohlc = pd.DataFrame({"high": [101.0, 102.0], "low": [99.0, 98.0], "close": [100.0, 100.0]},
                        index=idx)  # fmt: skip
    rg = session_ranges(closes, ohlc)
    assert rg[idx[0]] == (pytest.approx(202.0), pytest.approx(198.0))
    assert rg[idx[1]] == (pytest.approx(102.0), pytest.approx(98.0))
    assert session_ranges(closes, None) == {}


def test_proxy_reads_only_the_exit_day_range(view: HistoryView) -> None:  # noqa: F811
    """Mutation: crash every session's high/low after day d; d's outcomes are unchanged."""
    h = view.load("AAA")
    base = _cash(view)
    cut = DAYS[20]
    mutated = h.ohlc.copy()
    later = [d for d in mutated.index if d > cut]
    mutated.loc[later, "low"] = mutated.loc[later, "close"] * 0.5
    mutated.loc[later, "high"] = mutated.loc[later, "close"] * 1.5
    s = _settings("cash_debit")
    st = sim_ticker_from_chains(
        "AAA",
        h.chains,
        h.closes,
        decision_days=DECIDE,
        days=_days(100.0),
        cfg=_cfg(),
        exits=EXITS,
        cost=COST,
        settings={"cash_debit": s},
        windows={"cash_debit": s.entry_dte_window},
        rankers={"cash_debit": [Ranker.MANAGED_NET_EV]},
        stances=None,
        sim=SimSettings(regime_model="v1"),
        ohlc=mutated,
    )
    changed = same = 0
    for d, cs in base.menus["cash_debit"].items():
        for a, b in zip(cs, st.menus["cash_debit"][d], strict=True):
            assert a.candidate.inputs.key == b.candidate.inputs.key
            if a.result is None:
                continue
            ex = a.result.outcome.trade.exit_date
            if ex is not None and ex <= cut:
                assert a.result.outcome.trade.model_dump() == b.result.outcome.trade.model_dump()  # type: ignore[union-attr]
                same += 1
            elif a.result.outcome.trade.model_dump() != b.result.outcome.trade.model_dump():  # type: ignore[union-attr]
                changed += 1
    assert same > 0 and changed > 0  # sanity: the mutation did bite after the cut


def test_intraday_proxy_none_books_no_proxy_exits(view: HistoryView) -> None:  # noqa: F811
    st = _cash(view, sim=SimSettings(regime_model="v1", intraday_proxy="none"))
    for cs in st.menus["cash_debit"].values():
        for sc in cs:
            if sc.result is not None:
                assert sc.result.outcome.trade.exit_reason not in PROXY_EXITS.values()
