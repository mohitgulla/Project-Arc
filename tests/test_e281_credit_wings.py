"""E28.1 (D131): credit wings at a share of spot, fitted to the per-underlying cap, and
the credit/width floor. Offline: synthetic contracts and the recorded SPY chain."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from arc.backtest.costs import load_cost_model
from arc.config import ArcSettings
from arc.data.base import OptionContract, OptionGreeks
from arc.data.recorded import SPY_CHAIN_FIXTURE, RecordedMarketData
from arc.scanner import ScanParams, ScanStrategy, scan, select_wing
from arc.scanner.scan import wing_max_loss
from arc.structures import format_occ, parse_occ

if TYPE_CHECKING:
    from pathlib import Path

EXP = dt.date(2026, 11, 6)
CM = load_cost_model()


@pytest.fixture(autouse=True)
def _shipped_credit_rules(monkeypatch: pytest.MonkeyPatch) -> None:
    """The suite pins the pre-D131 wing (tests/pre_d85_env.py); this file tests the shipped one."""
    monkeypatch.delenv("ARC_SCANNER_WING_MODE", raising=False)
    monkeypatch.delenv("ARC_SCANNER_MIN_CREDIT_WIDTH", raising=False)


def _c(strike: float, mid: float, kind: str = "put", *, half: float = 0.02) -> OptionContract:
    sym = format_occ("XYZ", EXP, kind, strike)
    return OptionContract(
        symbol=sym,
        underlying="XYZ",
        expiration=EXP,
        strike=strike,
        option_type=kind,
        bid=round(mid - half, 4),
        ask=round(mid + half, 4),
        mid=mid,
        open_interest=500,
        volume=50,
        implied_volatility=0.3,
        greeks=OptionGreeks(delta=-0.2 if kind == "put" else 0.2),
    )


# short put 100 @ 2.00; wings 99/97/95/94 (puts cheaper further OTM)
SHORT = _c(100, 2.00)
SIDE = [SHORT, _c(99, 1.70), _c(97, 1.20), _c(95, 0.80), _c(94, 0.60), _c(101, 2.5)]


class TestSelectWing:
    def test_dollars_mode_is_the_legacy_nearest(self) -> None:
        # nearest $5 wing within [2.5, 10]: 95
        assert select_wing(SHORT, SIDE, 5.0).strike == 95
        assert select_wing(SHORT, SIDE, 3.0).strike == 97

    def test_widest_gap_within_max_gap(self) -> None:
        # max gap 5.5: 94 is 6 away (out), 95 is the widest in range
        w = select_wing(SHORT, SIDE, 5.0, max_gap=5.5, cost=CM)
        assert w is not None and w.strike == 95
        # max gap exactly 6 includes 94
        assert select_wing(SHORT, SIDE, 5.0, max_gap=6.0, cost=CM).strike == 94

    def test_widest_gap_that_fits_the_cap(self) -> None:
        # 95: (5 - 1.20) x 100 = 380 + costs; 97: (3 - 0.80) x 100 = 220 + costs
        loss95 = wing_max_loss(SHORT, SIDE[3], CM)
        loss97 = wing_max_loss(SHORT, SIDE[2], CM)
        assert loss95 is not None and loss97 is not None
        assert 380 < loss95 < 400 and 220 < loss97 < 240
        w = select_wing(SHORT, SIDE, 5.0, max_gap=5.5, cap_dollars=loss95 - 0.01, cost=CM)
        assert w is not None and w.strike == 97
        w = select_wing(SHORT, SIDE, 5.0, max_gap=5.5, cap_dollars=loss95, cost=CM)
        assert w is not None and w.strike == 95  # the cap is inclusive

    def test_no_gap_fits_returns_none(self) -> None:
        assert select_wing(SHORT, SIDE, 5.0, max_gap=5.5, cap_dollars=50.0, cost=CM) is None
        # nothing further OTM within the gap at all
        assert select_wing(SHORT, [SHORT, _c(101, 2.5)], 5.0, max_gap=5.0, cost=CM) is None

    def test_illiquid_widest_strike_falls_through(self) -> None:
        # 95 filtered out as illiquid (absent from the liquid side): the next-widest fits
        side = [c for c in SIDE if c.strike != 95]
        assert select_wing(SHORT, side, 5.0, max_gap=5.5, cost=CM).strike == 97

    def test_non_credit_pair_is_skipped(self) -> None:
        # a wing priced above the short (crossed marks) is never a credit
        side = [SHORT, _c(95, 2.10), _c(97, 1.20)]
        assert select_wing(SHORT, side, 5.0, max_gap=5.5, cost=CM).strike == 97

    def test_calls_look_above(self) -> None:
        short = _c(100, 2.0, "call")
        side = [short, _c(103, 1.1, "call"), _c(105, 0.7, "call"), _c(97, 3.5, "call")]
        assert select_wing(short, side, 5.0, max_gap=5.0, cost=CM).strike == 105

    def test_default_cost_model_is_loaded(self) -> None:
        assert select_wing(SHORT, SIDE, 5.0, max_gap=5.5).strike == 95


@settings(max_examples=150, deadline=None)
@given(
    spot=st.floats(20.0, 800.0),
    pct=st.floats(0.01, 0.10),
    cap=st.one_of(st.none(), st.floats(50.0, 5000.0)),
    steps=st.lists(st.sampled_from([0.5, 1.0, 2.5, 5.0, 10.0]), min_size=1, max_size=12),
    decay=st.floats(0.5, 0.95),
)
def test_property_width_and_cap(
    spot: float, pct: float, cap: float | None, steps: list[float], decay: float
) -> None:
    """Whenever a wing is returned: gap <= pct x spot and 1-lot max loss <= cap."""
    k = round(spot * 0.95, 1)
    short = _c(k, 3.0)
    side = [short]
    strike, mid = k, 3.0
    for s in steps:
        strike, mid = round(strike - s, 2), round(max(mid * decay, 0.05), 2)
        if strike <= 0:
            break
        side.append(_c(strike, mid, half=0.01))
    w = select_wing(short, side, 5.0, max_gap=pct * spot, cap_dollars=cap, cost=CM)
    if w is None:
        return
    assert 0 < short.strike - w.strike <= pct * spot + 1e-9
    loss = wing_max_loss(short, w, CM)
    assert loss is not None
    if cap is not None:
        assert loss <= cap


# ---------------------------------------------------------------------------
# scan() on the recorded SPY chain
# ---------------------------------------------------------------------------


def _scan(**kw: Any) -> Any:
    prov = RecordedMarketData.from_files(SPY_CHAIN_FIXTURE)
    params = ScanParams(dte_min=30, dte_max=45, **kw)
    return scan(prov, "SPY", params, as_of=prov.recording("SPY").as_of)


def _widths(c: Any) -> list[float]:
    by: dict[str, list[float]] = {}
    for leg in c.structure.legs:
        o = parse_occ(leg.occ_symbol)
        by.setdefault(o.kind, []).append(float(o.strike))
    return [max(v) - min(v) for v in by.values()]


class TestScan:
    def test_legacy_params_dump_unchanged(self) -> None:
        dump = ScanParams().model_dump(mode="json")
        for k in ("wing_mode", "wing_pct_spot", "cap_dollars", "min_credit_width"):
            assert k not in dump
        res = _scan()
        assert res.dropped is None
        assert "dropped" not in res.model_dump(mode="json")

    def test_pct_spot_widths_within_pct(self) -> None:
        res = _scan(wing_mode="pct_spot", wing_pct_spot=0.05)
        assert res.candidates
        assert res.dropped == {"wing_cap_unfit": 0, "credit_width_floor": 0}
        for c in res.candidates:
            assert all(0 < w <= 0.05 * res.spot + 1e-9 for w in _widths(c))
        # the widest gaps are wider than the old $5 wing on a ~$771 underlying
        assert max(c.width for c in res.candidates) > 10

    def test_cap_bounds_every_side_and_the_condor(self) -> None:
        cap = 1500.0
        res = _scan(
            wing_mode="pct_spot",
            wing_pct_spot=0.05,
            cap_dollars=cap,
            strategies=[ScanStrategy.BULL_PUT, ScanStrategy.BEAR_CALL, ScanStrategy.IRON_CONDOR],
        )
        assert res.candidates
        kinds = {c.strategy for c in res.candidates}
        assert ScanStrategy.IRON_CONDOR in kinds
        for c in res.candidates:
            # a vertical's max loss incl. costs fits; a condor's = max of its two sides
            assert float(c.structure.max_loss) <= cap
        # condor sides are picked independently: some condor has unequal wings
        condors = [c for c in res.candidates if c.strategy is ScanStrategy.IRON_CONDOR]
        assert all(len(_widths(c)) == 2 for c in condors)

    def test_cap_too_small_counts_unfit(self) -> None:
        res = _scan(wing_mode="pct_spot", cap_dollars=1.0)
        credit = [c for c in res.candidates if c.credit_width is not None]
        assert credit == []
        assert res.dropped is not None and res.dropped["wing_cap_unfit"] > 0

    def test_floor_drops_low_credit_width_only(self) -> None:
        strategies = [ScanStrategy.BULL_PUT, ScanStrategy.LONG_CALL]
        base = _scan(strategies=strategies)
        cws = [c.credit_width for c in base.candidates if c.credit_width is not None]
        assert cws
        floor = sorted(cws)[len(cws) // 2]
        res = _scan(strategies=strategies, min_credit_width=floor)
        kept = [c.credit_width for c in res.candidates if c.credit_width is not None]
        assert all(cw >= floor for cw in kept)
        assert res.dropped is not None
        assert res.dropped["credit_width_floor"] == len(cws) - len(kept) > 0
        assert res.dropped["wing_cap_unfit"] == 0  # dollars wing: never counted
        # debit kinds are untouched by the floor
        debit = [c for c in base.candidates if c.credit_width is None]
        assert [c.structure for c in res.candidates if c.credit_width is None] == [
            c.structure for c in debit
        ]

    def test_dollars_mode_from_settings_is_byte_identical(self) -> None:
        """Off (rollback) = the pre-D131 scan: same dump as bare legacy params."""
        s = ArcSettings(  # type: ignore[call-arg]
            _env_file=None,
            account_profile="margin",
            scanner_wing_mode="dollars",
            scanner_min_credit_width=0.0,
        )
        prov = RecordedMarketData.from_files(SPY_CHAIN_FIXTURE)
        as_of = prov.recording("SPY").as_of
        p_off = ScanParams.from_settings(s)
        legacy = p_off.model_copy(
            update={"wing_mode": "dollars", "min_credit_width": 0.0, "cap_dollars": None}
        )
        got = scan(prov, "SPY", p_off, as_of=as_of).model_dump(mode="json")
        want = scan(prov, "SPY", legacy, as_of=as_of).model_dump(mode="json")
        assert got == want
        assert "wing_mode" not in got["params"] and "dropped" not in got
        # and the shipped default turns both rules on
        on = ScanParams.from_settings(ArcSettings(_env_file=None))  # type: ignore[call-arg]
        assert (on.wing_mode, on.wing_pct_spot, on.min_credit_width) == ("pct_spot", 0.05, 0.10)


class TestCli:
    def test_chains_flags(self, capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
        from arc.cli import main

        rc = main(
            [
                "chains", "SPY", "--dte", "30-45", "--fixture", "spy", "--profile", "margin",
                "--db", str(tmp_path / "arc.db"), "--top", "3", "--strategy", "bull_put",
                "--wing-mode", "pct_spot", "--wing-pct", "0.05", "--min-credit-width", "0.1",
                "--equity", "23500",
            ]
        )  # fmt: skip
        out = capsys.readouterr().out
        assert rc == 0
        from arc.config import get_settings

        cap = get_settings().max_alloc_pct * 23500  # empty book: the base tier x equity
        assert f"credit rules: wing ≤ 5% of spot ($38.57), 1-lot max loss ≤ cap ${cap:,.0f}" in out
        assert "dropped: wing_cap_unfit" in out and "credit_width_floor" in out

    def test_chains_dollars_has_no_rules_line(
        self, capsys: pytest.CaptureFixture[str], tmp_path: Path
    ) -> None:
        from arc.cli import main

        args = ["chains", "SPY", "--fixture", "spy", "--db", str(tmp_path / "arc.db")]
        args += ["--wing-mode", "dollars", "--min-credit-width", "0", "--cap-dollars", "999"]
        assert main(args) == 0
        assert "credit rules" not in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Backtest parity (StrategySpec wing_fit / wing_cap / min_credit_width)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def chain_100() -> Any:
    import pandas as pd

    from arc.backtest.costs import CostModel
    from tests.test_backtest import R, prepare_chain, synth_rows

    day = dt.date(2024, 3, 1)
    rows = pd.DataFrame([r.model_dump() for r in synth_rows(day, 100.0)])
    rows["right"] = rows["right"].astype(str)
    return prepare_chain(rows, 100.0, cost=CostModel(), r=R)


class TestBacktestParity:
    def test_defaults_keep_labels_and_dumps(self) -> None:
        from arc.backtest.strategies import StrategyKind, StrategySpec

        s = StrategySpec(kind=StrategyKind.BULL_PUT, delta=0.2)
        assert s.label == "bull_put_d20_w2_dte30-45"
        assert set(s.model_dump()) >= {"width_pct"}
        for k in ("wing_fit", "wing_cap", "min_credit_width"):
            assert k not in s.model_dump()
        w = s.model_copy(update={"wing_fit": "widest", "wing_cap": 300.0, "min_credit_width": 0.1})
        assert w.label == "bull_put_d20_w2max300_cw0.1_dte30-45"
        nocap = s.model_copy(update={"wing_fit": "widest"})
        assert nocap.label == "bull_put_d20_w2max_dte30-45"
        debit = StrategySpec(kind=StrategyKind.BULL_CALL, wing_fit="widest", min_credit_width=0.1)
        assert debit.label == "bull_call_d20_w2_dte30-45"

    @pytest.mark.parametrize("kind", ["bull_put", "bear_call", "iron_condor"])
    def test_widest_fits_pct_and_cap(self, chain_100: Any, kind: str) -> None:
        from arc.backtest.strategies import (
            StrategyKind,
            StrategySpec,
            pick_expirations,
            select_legs,
        )

        spec = StrategySpec(kind=StrategyKind(kind), delta=0.20, width_pct=0.05, wing_fit="widest")
        (exp,) = pick_expirations(chain_100, spec)
        legs = select_legs(chain_100, spec, 100.0, exp)
        assert legs is not None
        widths = []
        for sh in (lg for lg in legs if lg.side < 0):
            lo = next(lg for lg in legs if lg.side > 0 and lg.right == sh.right)
            widths.append(abs(lo.strike - sh.strike))
            assert 0 < widths[-1] <= 5.0 + 1e-9
            loss = (widths[-1] - (sh.mid - lo.mid)) * 100
            capped = spec.model_copy(update={"wing_cap": loss - 1e-6})
            got = select_legs(chain_100, capped, 100.0, exp)
            if got is not None:  # a narrower wing on this side fits
                sh2 = next(lg for lg in got if lg.side < 0 and lg.right == sh.right)
                lo2 = next(lg for lg in got if lg.side > 0 and lg.right == sh.right)
                assert (abs(lo2.strike - sh2.strike) - (sh2.mid - lo2.mid)) * 100 < loss
        assert max(widths) == 5.0  # the widest gap on a $1 grid = 5% of 100
        tiny = spec.model_copy(update={"wing_cap": 0.01})
        assert select_legs(chain_100, tiny, 100.0, exp) is None

    def test_menu_floor_drops_low_credit_width(self, chain_100: Any) -> None:
        import pandas as pd

        from arc.backtest.ranking import build_menu
        from arc.backtest.strategies import StrategyKind, StrategySpec
        from arc.exits.policy import load_exit_config

        day = dt.date(2024, 3, 1)
        closes = pd.Series(
            [100.0 * (1 + 0.001 * i) for i in range(80)],
            index=[day - dt.timedelta(days=79 - i) for i in range(80)],
        )
        exits = load_exit_config()
        base = StrategySpec(kind=StrategyKind.BULL_PUT, delta=0.20, width_pct=0.05)
        kw: dict[str, Any] = {
            "day": day,
            "underlying": "TST",
            "cost": CM,
            "exits": exits,
            "mc": exits.model.model_copy(update={"n_paths": 200}),
            "r": 0.04,
        }
        (c,) = build_menu(chain_100, closes, specs=[base], **kw)
        cw = c.inputs.credit_width
        assert cw is not None
        above = base.model_copy(update={"min_credit_width": round(cw + 0.01, 4)})
        assert build_menu(chain_100, closes, specs=[above], **kw) == []
        below = base.model_copy(update={"min_credit_width": round(max(cw - 0.01, 0.001), 4)})
        assert len(build_menu(chain_100, closes, specs=[below], **kw)) == 1

    def test_shipped_margin_menu_follows_the_live_default(self) -> None:
        from arc.backtest.ranking import load_ranking_file

        (credit, *_rest) = load_ranking_file().backtest.menus["margin"]
        s = ArcSettings(_env_file=None)  # type: ignore[call-arg]
        assert credit.width_pct == s.scanner_wing_pct_spot
        assert credit.wing_fit == "widest"
        assert credit.min_credit_width == s.scanner_min_credit_width
        # wing_cap = the shipped base tier x the sim's equity (an empty book)
        from arc.backtest.ranking import load_ranking_file as _l

        base = ArcSettings.model_fields["max_alloc_pct"].default
        assert credit.wing_cap == pytest.approx(base * _l().backtest.sim.equity)


# ---------------------------------------------------------------------------
# Pipeline wiring (_quant_menus passes the per-underlying cap)
# ---------------------------------------------------------------------------


def test_pipeline_menu_fits_the_cap(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from tests.test_menu_measure import _quant_chains, _run, legacy_margin_file

    pf = legacy_margin_file(tmp_path)
    _, _, prompts = _run(
        "margin", monkeypatch, profiles_file=pf, menu_measure="control", credit_rules=True
    )
    chains = _quant_chains(prompts["quant"])
    # fixture account: equity $100,000, empty book -> the base tier x equity
    pct = ArcSettings(_env_file=None).max_alloc_pct  # type: ignore[call-arg]
    cap = float(Decimal(str(pct)) * Decimal(100_000))
    rows = [r for ch in chains.values() for r in ch["menu"] if r["credit_width"] is not None]
    assert rows
    for r in rows:
        assert r["max_loss"] <= cap
        assert r["credit_width"] >= 0.10


def test_wing_cap_dollars_off_and_broker_error(monkeypatch: pytest.MonkeyPatch) -> None:
    import types

    import arc.pipeline.steps as steps

    off = types.SimpleNamespace(
        settings=ArcSettings(_env_file=None, scanner_wing_mode="dollars")  # type: ignore[call-arg]
    )
    assert steps._wing_cap_dollars(off, object()) is None  # type: ignore[arg-type]

    def boom(*_a: object) -> None:
        raise RuntimeError("broker down")

    monkeypatch.setattr(steps, "_account_inputs", boom)
    on = types.SimpleNamespace(settings=ArcSettings(_env_file=None))  # type: ignore[call-arg]
    assert steps._wing_cap_dollars(on, object()) is None  # type: ignore[arg-type]
