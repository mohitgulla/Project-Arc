"""E28.6 (D131): the D131 rules as backtest overlay knobs, the sim-only VRP cap and the
sim-only VIX regime variant.

Every knob defaults to the shipped behaviour (``None`` / ``"and"``) and changes the
fixture walk's book when set.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import TYPE_CHECKING

import pandas as pd
import pytest
from pydantic import ValidationError

from arc.backtest.market_index import (
    read_index_closes,
    vix_high_days,
    vix_percentiles,
    write_index_closes,
)
from arc.backtest.ranking import BacktestSettings, CreditWings, MenuSpec, load_ranking_file
from arc.backtest.sim import DayInfo, hv20_series
from arc.backtest.sim_config import SimGateOverrides, SimSettings
from arc.backtest.strategies import StrategySpec
from arc.config import ArcSettings
from arc.gate import MarketTrend
from arc.utils.yamlpatch import deep_merge

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# Config: defaults are the shipped behaviour, one overlay line per arm
# ---------------------------------------------------------------------------


def test_defaults_are_shipped_behaviour() -> None:
    cfg = load_ranking_file()
    sim = cfg.backtest.sim
    assert sim.gate == SimGateOverrides()
    assert sim.gate.settings_update() == {}
    assert sim.vrp_max is None
    assert cfg.backtest.credit_wings == CreditWings()
    s = ArcSettings().with_profile("margin")
    assert sim.gate.apply(s) is s
    for p, ms in cfg.backtest.menus.items():  # credit_wings off = the menus as written
        assert cfg.backtest.specs_for(p, 20, 45) == [x for m in ms for x in m.specs(20, 45)]


def test_one_overlay_line_per_arm() -> None:
    base = load_ranking_file().model_dump(mode="json")
    from arc.backtest.ranking import RankingFile

    arm = RankingFile.model_validate(
        deep_merge(base, {"backtest": {"sim": {"gate": {"max_open_risk_pct": 0.30}}}})
    )
    assert arm.backtest.sim.gate.max_open_risk_pct == pytest.approx(0.30)
    assert arm.backtest.sim.gate.settings_update() == {"max_open_risk_pct": 0.30}
    s = arm.backtest.sim.gate.apply(ArcSettings().with_profile("margin"))
    assert s.max_open_risk_pct == pytest.approx(0.30)
    vrp = RankingFile.model_validate(deep_merge(base, {"backtest": {"sim": {"vrp_max": 0.05}}}))
    assert vrp.backtest.sim.vrp_max == pytest.approx(0.05)


@pytest.mark.parametrize(
    "bad",
    [
        {"max_open_risk_pct": 0.05},
        {"cluster_cap_corr_threshold": 0.99},
        {"cluster_cap_max_names": 0},
        {"regime_gate_ma": 150},
        {"regime_gate_buffer_pct": 0.2},
        {"regime_vix_pct_above": 1.0},
        {"regime_vix_combine": "xor"},
        {"unknown": 1},
    ],
)
def test_gate_override_bounds_match_settings(bad: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        SimGateOverrides.model_validate(bad)


def test_gate_overrides_cover_every_arcsettings_knob() -> None:
    fields = set(SimGateOverrides.model_fields) - {"regime_vix_pct_above", "regime_vix_combine"}
    assert fields <= set(ArcSettings.model_fields)
    s = ArcSettings().with_profile("margin")
    full = SimGateOverrides(
        regime_gate_enabled=False,
        regime_gate_ma=50,
        regime_gate_buffer_pct=0.01,
        regime_gate_blocked_kinds="bull_put",
        max_open_risk_pct=0.5,
        cluster_cap_enabled=False,
        cluster_cap_corr_threshold=0.8,
        cluster_cap_max_names=3,
        regime_vix_pct_above=0.8,
    )
    got = full.apply(s)
    for k, v in full.settings_update().items():
        assert getattr(got, k) == v
    ArcSettings.model_validate(got.model_dump())  # the overridden settings still validate


#: Every arm overlay named in docs/RESEARCH/d131-margin-rules.md (one line each).
BRIEF_ARMS: list[dict[str, object]] = [
    *({"backtest": {"credit_wings": {"min_credit_width": x}}} for x in (0.15, 0.20, 0.25)),
    *({"backtest": {"sim": {"vrp_max": x}}} for x in (0.05, 0.10, 0.15, 0.20)),
    {"backtest": {"sim": {"gate": {"regime_gate_ma": 50, "regime_vix_pct_above": 0.8}}}},
    *({"backtest": {"sim": {"gate": {"max_open_risk_pct": x}}}} for x in (0.30, 0.50)),
]


@pytest.mark.parametrize("arm", BRIEF_ARMS)
def test_brief_arms_validate_as_study_overlays(arm: dict[str, object]) -> None:
    """The brief's arms pass the study runner's overlay validator on the shipped file."""
    from pathlib import Path

    import yaml

    from arc.backtest.ranking import RankingFile
    from arc.experiments.overlay import validate_target

    root = Path(__file__).resolve().parent.parent
    base = yaml.safe_load((root / "config" / "ranking.yaml").read_text())
    merged = deep_merge(base, arm)
    validate_target("ranking", merged)
    got = RankingFile.model_validate(merged)
    assert got != RankingFile.model_validate(base)


def test_vrp_max_bounds() -> None:
    with pytest.raises(ValidationError):
        SimSettings(vrp_max=-0.01)
    with pytest.raises(ValidationError):
        SimSettings(vrp_max=1.5)


# ---------------------------------------------------------------------------
# Credit wings (E28.1 rule as an arm knob)
# ---------------------------------------------------------------------------


def _menu(**kw: object) -> MenuSpec:
    base: dict[str, object] = {
        "kinds": ["bull_put", "bear_call"],
        "deltas": [0.3],
        "width_pct": 0.05,
        "wing_fit": "widest",
        "wing_cap": 3750.0,
        "min_credit_width": 0.10,
    }
    return MenuSpec.model_validate(base | kw)


def test_credit_wings_apply() -> None:
    m = _menu()
    assert CreditWings().apply(m) is m
    debit = _menu(kinds=["long_call"])
    assert CreditWings(min_credit_width=0.25).apply(debit) is debit  # no credit kind
    floor = CreditWings(min_credit_width=0.20).apply(m)
    assert floor.min_credit_width == pytest.approx(0.20) and floor.wing_cap == 3750.0
    dollars = CreditWings(mode="dollars").apply(m)
    assert dollars.wing_dollars == 5.0
    assert CreditWings(mode="dollars", dollars=2.5).apply(m).wing_dollars == 2.5
    near = CreditWings(mode="pct_nearest", pct=0.02).apply(m)
    assert (near.wing_fit, near.wing_cap, near.width_pct) == ("nearest", None, 0.02)
    back = CreditWings(mode="pct_spot", cap=2000.0).apply(dollars)
    assert (back.wing_fit, back.wing_dollars, back.wing_cap) == ("widest", None, 2000.0)


def test_credit_wings_reach_specs_and_labels() -> None:
    bt = BacktestSettings.model_validate(
        load_ranking_file().backtest.model_dump(mode="json")
        | {"credit_wings": {"min_credit_width": 0.25}}
    )
    specs = bt.specs_for("margin", 20, 45)
    credit = [s for s in specs if s.kind in ("bull_put", "bear_call", "iron_condor")]
    assert credit and all(s.min_credit_width == pytest.approx(0.25) for s in credit)
    assert all("_cw0.25" in s.label for s in credit)


def test_wing_dollars_dump_and_label() -> None:
    s = StrategySpec(kind="bull_put", dte_min=20, dte_max=45, delta=0.3, width_pct=0.05)
    assert "wing_dollars" not in s.model_dump()  # menu-cache keys unchanged by default
    d = s.model_copy(update={"wing_dollars": 5.0})
    assert d.model_dump()["wing_dollars"] == 5.0
    assert "_w$5" in d.label and d.label != s.label


@pytest.mark.parametrize("kind", ["bull_put", "bear_call", "iron_condor"])
def test_wing_dollars_picks_the_dollar_wing(kind: str) -> None:
    from arc.backtest.costs import CostModel
    from arc.backtest.strategies import StrategyKind, pick_expirations, select_legs
    from tests.test_backtest import R, prepare_chain, synth_rows

    rows = pd.DataFrame([r.model_dump() for r in synth_rows(dt.date(2024, 3, 1), 100.0)])
    rows["right"] = rows["right"].astype(str)
    chain = prepare_chain(rows, 100.0, cost=CostModel(), r=R)
    spec = StrategySpec(kind=StrategyKind(kind), delta=0.20, width_pct=0.05, wing_fit="widest")
    (exp,) = pick_expirations(chain, spec)

    def widths(legs: list) -> set[float]:  # type: ignore[type-arg]
        out = set()
        for sh in (lg for lg in legs if lg.side < 0):
            lo = next(lg for lg in legs if lg.side > 0 and lg.right == sh.right)
            out.add(abs(lo.strike - sh.strike))
        return out

    pct = select_legs(chain, spec, 100.0, exp)
    dol = select_legs(chain, spec.model_copy(update={"wing_dollars": 2.0}), 100.0, exp)
    assert pct is not None and dol is not None
    assert widths(pct) == {5.0}
    assert widths(dol) == {2.0}


# ---------------------------------------------------------------------------
# VRP inputs
# ---------------------------------------------------------------------------


def test_dayinfo_vrp() -> None:
    base = {"eligible": True, "screen_pass": True, "seed": False, "liquidity": 1.0}
    assert DayInfo(**base).vrp is None  # type: ignore[arg-type]
    assert DayInfo(**base, iv30=0.30).vrp is None  # type: ignore[arg-type]
    assert DayInfo(**base, iv30=0.30, hv20=0.22).vrp == pytest.approx(0.08)  # type: ignore[arg-type]


def test_hv20_point_in_time() -> None:
    days = [dt.date(2025, 1, 1) + dt.timedelta(days=i) for i in range(30)]
    closes = pd.Series([100.0 * (1.01 if i % 2 else 0.99) ** i for i in range(30)], index=days)
    hv = hv20_series(closes, None)
    assert min(hv) == days[20]  # needs 21 closes
    later = closes.copy()
    later.iloc[-1] = 1_000.0  # a later close never moves an earlier session's value
    hv2 = hv20_series(later, None)
    assert all(hv2[d] == pytest.approx(hv[d]) for d in days[20:-1])
    assert hv20_series(closes.iloc[:10], None) == {}
    ohlc = pd.DataFrame({"close": closes * 2.0})  # split-adjusted file wins; scale-free
    assert hv20_series(closes, ohlc)[days[25]] == pytest.approx(hv[days[25]])


# ---------------------------------------------------------------------------
# VIX percentile (the market_health vix_pct_1y rule, point in time)
# ---------------------------------------------------------------------------


def test_vix_index_roundtrip_and_percentiles(tmp_path: Path) -> None:
    days = [dt.date(2024, 1, 1) + dt.timedelta(days=i) for i in range(200)]
    closes = {d: 10.0 + i for i, d in enumerate(days)}  # rising: always the max
    closes[days[-1]] = 1.0  # last: the min
    write_index_closes(tmp_path, "vix", closes | {dt.date(2023, 1, 1): 0.0})
    s = read_index_closes(tmp_path, "VIX")
    assert s is not None and len(s) == 200  # non-positive close dropped
    assert read_index_closes(tmp_path, "SPX") is None
    pct = vix_percentiles(s, [days[50], days[150], days[-1], dt.date(2030, 1, 1)])
    assert pct[days[50]] is None  # < 120 prior obs
    assert pct[days[150]] == pytest.approx(1.0)
    assert pct[days[-1]] == pytest.approx(0.0)
    assert pct[dt.date(2030, 1, 1)] is None
    high = vix_high_days(s, [days[50], days[150], days[-1]], 0.8)
    assert high == {days[50]: True, days[150]: True, days[-1]: False}  # unknown = high


# ---------------------------------------------------------------------------
# The walk: each knob changes the fixture book
# ---------------------------------------------------------------------------


def _fixture(tmp_path: Path, **day_kw: object):  # noqa: ANN202
    from tests import test_backtest_sim as tb
    from tests.test_e282_regime_gate import _Factory

    view = tb.view.__wrapped__(_Factory(tmp_path))  # type: ignore[attr-defined]
    return tb, {t: tb._ticker(view, t, **day_kw) for t in ("AAA", "ZZZ")}


def _credit(run) -> int:  # noqa: ANN001
    t = run.trades
    return int(t["kind"].isin(["vertical_credit", "iron_condor"]).sum()) if len(t) else 0


def test_vrp_cap_drops_credit_entries(tmp_path: Path) -> None:
    tb, rich = _fixture(tmp_path, iv30=0.40, hv20=0.20)  # vrp 0.20
    base = tb._run(rich)
    assert _credit(base) > 0
    capped = tb._run(rich, sim=SimSettings(momentum_size=0, vrp_max=0.10))
    assert capped.skip_counts["vrp_cap"] > 0
    assert _credit(capped) == 0
    loose = tb._run(rich, sim=SimSettings(momentum_size=0, vrp_max=0.25))
    assert loose.skip_counts["vrp_cap"] == 0 and len(loose.trades) == len(base.trades)
    # unknown input: never skipped
    (tmp_path / "u").mkdir()
    _, unknown = _fixture(tmp_path / "u", iv30=0.40)
    run = tb._run(unknown, sim=SimSettings(momentum_size=0, vrp_max=0.0))
    assert run.skip_counts["vrp_cap"] == 0 and _credit(run) == _credit(base)


def test_gate_override_open_risk_changes_the_book(tmp_path: Path) -> None:
    """At 10 % the cap's headroom sizes the second open down and blocks the rest."""
    tb, tickers = _fixture(tmp_path)
    s = tb._settings("margin", max_alloc_pct=0.10, max_alloc_pct_reduced=0.10)
    base = tb._run(tickers, settings=s)
    tight = tb._run(
        tickers,
        settings=s,
        sim=SimSettings(momentum_size=0, gate=SimGateOverrides(max_open_risk_pct=0.10)),
    )
    assert len(base.trades) > len(tight.trades) > 0
    assert int(tight.trades["contracts"].sum()) < int(base.trades["contracts"].sum())
    same = tb._run(
        tickers,
        settings=s,
        sim=SimSettings(
            momentum_size=0, gate=SimGateOverrides(max_open_risk_pct=s.max_open_risk_pct)
        ),
    )
    pd.testing.assert_frame_equal(same.trades, base.trades)


def test_gate_override_cluster_cap_changes_the_book(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import arc.backtest.sim as sim
    from arc.gate.cluster import corr_key

    def _all(_self: object, _day: object, ticker: str, others: object) -> object:
        return {corr_key(ticker, o): 1.0 for o in others if o.upper() != ticker.upper()}  # type: ignore[attr-defined]

    monkeypatch.setattr(sim._PitCorr, "correlations", _all)
    tb, tickers = _fixture(tmp_path)
    s = tb._settings("margin", cluster_cap_enabled=False)
    off = tb._run(tickers, settings=s)
    on = tb._run(
        tickers,
        settings=s,
        sim=SimSettings(
            momentum_size=0,
            gate=SimGateOverrides(cluster_cap_enabled=True, cluster_cap_max_names=1),
        ),
    )
    assert on.skip_counts["gate_cluster_cap"] > 0
    assert off.skip_counts["gate_cluster_cap"] == 0


def _trends(close: int) -> dict[dt.date, MarketTrend]:
    from tests import test_backtest_sim as tb

    return {
        d: MarketTrend(ticker="SPY", close=Decimal(close), ma=200, sma=Decimal(2), as_of=d)
        for d in tb.DAYS
    }


def test_gate_override_regime_switch(tmp_path: Path) -> None:
    tb, tickers = _fixture(tmp_path)
    s = tb._settings("margin", regime_gate_enabled=False)
    off = tb._run(tickers, settings=s, trends=_trends(1))
    assert off.skip_counts["gate_short_premium_regime"] == 0
    on = tb._run(
        tickers,
        settings=s,
        trends=_trends(1),
        sim=SimSettings(momentum_size=0, gate=SimGateOverrides(regime_gate_enabled=True)),
    )
    assert on.skip_counts["gate_short_premium_regime"] > 0
    assert _credit(on) < _credit(off)


@pytest.mark.parametrize(
    ("combine", "spy_below", "vix_high", "blocked"),
    [
        ("and", True, True, True),
        ("and", True, False, False),
        ("and", False, True, False),
        ("or", True, False, True),
        ("or", False, True, True),
        ("or", False, False, False),
    ],
)
def test_regime_vix_variant(
    tmp_path: Path, combine: str, spy_below: bool, vix_high: bool, blocked: bool
) -> None:
    tb, tickers = _fixture(tmp_path)
    s = tb._settings("margin", regime_gate_enabled=True)
    gate = SimGateOverrides(regime_vix_pct_above=0.8, regime_vix_combine=combine)  # type: ignore[arg-type]
    run = tb._run(
        tickers,
        settings=s,
        trends=_trends(1 if spy_below else 3),
        vix_high=dict.fromkeys(tb.DAYS, vix_high),
        sim=SimSettings(momentum_size=0, gate=gate),
    )
    assert (run.skip_counts["gate_short_premium_regime"] > 0) is blocked


def test_regime_vix_variant_off_when_rule_disabled(tmp_path: Path) -> None:
    tb, tickers = _fixture(tmp_path)
    run = tb._run(
        tickers,
        settings=tb._settings("margin", regime_gate_enabled=False),
        trends=_trends(3),
        vix_high=dict.fromkeys(tb.DAYS, True),
        sim=SimSettings(
            momentum_size=0,
            gate=SimGateOverrides(regime_vix_pct_above=0.8, regime_vix_combine="or"),
        ),
    )
    assert run.skip_counts["gate_short_premium_regime"] == 0


def test_regime_vix_variant_needs_the_series(tmp_path: Path) -> None:
    tb, tickers = _fixture(tmp_path)
    with pytest.raises(ValueError, match="VIX"):
        tb._run(
            tickers,
            sim=SimSettings(momentum_size=0, gate=SimGateOverrides(regime_vix_pct_above=0.8)),
        )


def test_build_refuses_vix_variant_without_file(tmp_path: Path) -> None:
    from arc.backtest.sim_report import _vix_high

    assert _vix_high(tmp_path, None, []) is None
    with pytest.raises(FileNotFoundError, match="fetch_vix_history"):
        _vix_high(tmp_path, 0.8, [dt.date(2025, 1, 2)])
    days = [dt.date(2024, 1, 1) + dt.timedelta(days=i) for i in range(130)]
    write_index_closes(tmp_path, "VIX", {d: 10.0 + i for i, d in enumerate(days)})
    got = _vix_high(tmp_path, 0.8, days[-2:])
    assert got == {days[-2]: True, days[-1]: True}
