"""E2.6 (D116): liquidity-aware strike and expiry choice inside the unchanged delta bands.

Pure score / key helpers (:mod:`arc.scanner.liquidity`), strike selection with the score,
the thinner-leg rule for verticals, the expiry volume bonus, config + registry, the menu
``liquidity`` field, and the off identity: with ``scanner.liquidity_rank`` off the
scanner's candidates on the recorded SPY chain match the golden captured on main
c4fb48a before E2.6 (``tests/fixtures/scanner_liquidity_off_golden.json``).
"""

from __future__ import annotations

import datetime as dt
import json
import math
from pathlib import Path
from typing import Any

import pytest
import yaml
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from arc.control.registry import REGISTRY, Risk, lookup, read_raw, write_raw
from arc.data.base import OptionContract, OptionGreeks
from arc.data.recorded import SPY_CHAIN_FIXTURE, RecordedMarketData
from arc.models import Liquidity
from arc.routines.config import LiquidityRankSettings, RoutinesConfig, load_routines
from arc.scanner import (
    CREDIT_STRATEGIES,
    DEBIT_STRATEGIES,
    ScanParams,
    ScanStrategy,
    scan,
    select_debit_short,
    select_shorts,
)
from arc.scanner.liquidity import (
    LiquidityRank,
    LiquidityWeights,
    expiry_bonus,
    expiry_volume_shares,
    liquidity_rank_from,
    liquidity_scores,
    min_leg_score,
    strike_key,
)
from arc.scanner.scan import _rank, _verticals
from arc.structures import MarketInputs, format_occ
from tests.golden_compare import assert_json_close

GOLDEN = Path(__file__).parent / "fixtures" / "scanner_liquidity_off_golden.json"
EXP = dt.date(2026, 11, 20)
W = LiquidityWeights()


def _c(
    strike: float,
    d: float,
    *,
    kind: str = "call",
    oi: int = 500,
    vol: int = 50,
    bid: float = 1.00,
    ask: float = 1.05,
    exp: dt.date = EXP,
) -> OptionContract:
    return OptionContract(
        symbol=format_occ("XYZ", exp, kind, strike),
        underlying="XYZ",
        expiration=exp,
        strike=strike,
        option_type=kind,
        bid=bid,
        ask=ask,
        mid=(bid + ask) / 2,
        open_interest=oi,
        volume=vol,
        implied_volatility=0.30,
        greeks=OptionGreeks(delta=d if kind == "call" else -abs(d)),
    )


def _scores(cs: list[OptionContract]) -> dict[str, float]:
    return liquidity_scores(cs, spread_max_pct=0.10, weights=W)


# ---------------------------------------------------------------------------
# Score
# ---------------------------------------------------------------------------


def test_score_terms_and_normalisation() -> None:
    thin = _c(100, 0.3, oi=100, vol=10, bid=1.00, ask=1.10)  # spread 9.5% of mid
    deep = _c(105, 0.3, oi=5000, vol=3000, bid=1.00, ask=1.01)  # spread ~1%
    s = _scores([thin, deep])
    # deep: OI and volume are the chain max (1, 1); tight = 1 - 0.995%/10% = 0.9005
    tight_deep = 1 - (0.01 / 1.005) / 0.10
    assert s[deep.symbol] == pytest.approx(0.35 + 0.35 + 0.30 * tight_deep)
    # thin: chain min on both (0, 0); tight = 1 - (0.10/1.05)/0.10
    assert s[thin.symbol] == pytest.approx(0.30 * (1 - (0.10 / 1.05) / 0.10))
    # a flat chain scores the OI / volume terms 1 for everyone
    flat = _scores([_c(100, 0.3), _c(105, 0.3)])
    assert len(set(flat.values())) == 1


def test_weights_are_normalised_by_their_sum() -> None:
    cs = [_c(100, 0.3, oi=100, vol=10), _c(105, 0.3, oi=900, vol=90)]
    a = liquidity_scores(cs, spread_max_pct=0.1, weights=LiquidityWeights(oi=1, volume=0, spread=0))
    b = liquidity_scores(
        cs, spread_max_pct=0.1, weights=LiquidityWeights(oi=0.5, volume=0, spread=0)
    )
    assert a == b
    assert a[cs[0].symbol] == 0.0 and a[cs[1].symbol] == 1.0
    with pytest.raises(ValidationError):
        LiquidityWeights(oi=0, volume=0, spread=0)


@given(
    st.lists(
        st.tuples(
            st.integers(0, 100_000),
            st.integers(0, 100_000),
            st.floats(0.05, 50.0),
            st.floats(0.0, 0.2),
        ),
        min_size=1,
        max_size=20,
    )
)
def test_score_is_in_unit_interval(rows: list[tuple[int, int, float, float]]) -> None:
    cs = [
        _c(100 + i, 0.3, oi=oi, vol=v, bid=b, ask=b * (1 + sp))
        for i, (oi, v, b, sp) in enumerate(rows)
    ]
    for v in _scores(cs).values():
        assert 0.0 <= v <= 1.0


# ---------------------------------------------------------------------------
# Strike choice inside the band
# ---------------------------------------------------------------------------


def test_slightly_off_target_but_much_more_liquid_strike_wins() -> None:
    # band 0.20-0.35 (half-width 0.075), target 0.30
    on_target = _c(960, 0.30, oi=104, vol=16, bid=33.55, ask=38.05)  # the MU fallback leg
    near = _c(1050, 0.27, oi=8000, vol=2946, bid=20.00, ask=20.40)
    cs = [on_target, near]
    old = select_shorts(cs, target=0.30, delta_min=0.20, delta_max=0.35, n=1)
    assert old == [on_target]  # delta distance alone
    new = select_shorts(
        cs, target=0.30, delta_min=0.20, delta_max=0.35, n=1, scores=_scores(cs), lam=0.5
    )
    # key: on_target 0/0.075 - 0.5*~0 = ~0; near 0.03/0.075 - 0.5*~0.99 = 0.40 - 0.50 < 0
    assert new == [near]


def test_lambda_zero_is_the_delta_fit_order() -> None:
    cs = [_c(100 + i, 0.20 + 0.01 * i, oi=100 * (i + 1), vol=10 * (5 - i)) for i in range(5)]
    old = select_shorts(cs, target=0.22, delta_min=0.16, delta_max=0.30, n=5)
    new = select_shorts(
        cs, target=0.22, delta_min=0.16, delta_max=0.30, n=5, scores=_scores(cs), lam=0.0
    )
    assert new == old


@given(
    st.lists(
        st.tuples(st.floats(0.05, 0.95), st.integers(0, 50_000), st.integers(0, 50_000)),
        min_size=1,
        max_size=25,
    ),
    st.floats(0.0, 2.0),
)
def test_a_contract_outside_the_band_never_wins(
    rows: list[tuple[float, int, int]], lam: float
) -> None:
    cs = [_c(100 + i, d, oi=oi, vol=v) for i, (d, oi, v) in enumerate(rows)]
    got = select_shorts(
        cs, target=0.25, delta_min=0.16, delta_max=0.30, n=25, scores=_scores(cs), lam=lam
    )
    assert all(0.16 <= abs(c.greeks.delta) <= 0.30 for c in got)  # type: ignore[union-attr]
    assert len(got) == sum(1 for d, _, _ in rows if 0.16 <= d <= 0.30)


def test_strike_key() -> None:
    assert strike_key(0.03, 0.075, 0.8, 0.5) == pytest.approx(0.4 - 0.4)
    assert strike_key(0.01, 0.0, 0.0, 0.5) == 0.01  # zero scale: raw fit
    assert min_leg_score(["a", "b"], {"a": 0.9, "b": 0.2}) == 0.2


# ---------------------------------------------------------------------------
# Verticals: the thinner leg decides
# ---------------------------------------------------------------------------


def test_debit_short_prefers_the_higher_min_leg_score() -> None:
    long = _c(920, 0.55, oi=3000, vol=375)
    thin = _c(960, 0.30, oi=104, vol=16, bid=33.55, ask=38.05)  # target |Δ|
    deep = _c(1040, 0.28, oi=6000, vol=2264, bid=20.0, ask=20.4)
    cs = [long, thin, deep]
    kw: dict[str, Any] = {"target": 0.30, "delta_min": 0.20, "delta_max": 0.35, "width": None}
    assert select_debit_short(long, cs, n=1, **kw) == [thin]
    sc = _scores(cs)
    assert select_debit_short(long, cs, n=1, scores=sc, lam=0.5, **kw) == [deep]
    # the min-leg rule: a liquid short cannot lift a thin long's vertical above the
    # long's own score, so with a thin long both shorts tie on liquidity and fit decides
    thin_long = _c(920, 0.55, oi=100, vol=10, bid=50.0, ask=55.0)
    cs2 = [thin_long, thin, deep]
    sc2 = _scores(cs2)
    assert min(sc2[thin_long.symbol], sc2[deep.symbol]) == pytest.approx(
        min(sc2[thin_long.symbol], sc2[thin.symbol]), abs=0.05
    )
    assert select_debit_short(thin_long, cs2, n=1, scores=sc2, lam=0.5, **kw) == [thin]


def test_debit_short_width_fit_is_normalised_by_width() -> None:
    long = _c(100, 0.55, oi=3000, vol=300)
    exact = _c(105, 0.30, oi=100, vol=10, bid=1.0, ask=1.1)  # dist 5 = width
    off = _c(106, 0.28, oi=3000, vol=300, bid=1.0, ask=1.01)  # dist 6: fit 1/5 = 0.2
    cs = [long, exact, off]
    kw: dict[str, Any] = {"target": 0.30, "delta_min": 0.20, "delta_max": 0.35, "width": 5.0}
    assert select_debit_short(long, cs, n=1, **kw) == [exact]
    assert select_debit_short(long, cs, n=1, scores=_scores(cs), lam=0.5, **kw) == [off]


def test_credit_vertical_short_scored_by_its_thinner_wing() -> None:
    # two shorts at equal |Δ| distance from 0.20; A's wing is thin, B's wing is liquid
    a = _c(100, 0.19, kind="put", oi=5000, vol=900)
    a_wing = _c(95, 0.12, kind="put", oi=100, vol=10, bid=0.50, ask=0.55)
    b = _c(90, 0.21, kind="put", oi=5000, vol=900)
    b_wing = _c(85, 0.08, kind="put", oi=5000, vol=900, bid=0.50, ask=0.505)
    side = [a, a_wing, b, b_wing]
    mkt = MarketInputs(spot=110.0, r=0.04, ivs={c.symbol: 0.30 for c in side})
    on = ScanParams(liquidity_rank=LiquidityRank(), shorts_per_side=1)
    off = ScanParams(shorts_per_side=1)
    as_of = EXP - dt.timedelta(days=35)
    ((s_off, _, _),) = _verticals("XYZ", EXP, side, off, as_of, mkt)
    assert s_off is a  # tie on distance -> lower |Δ| (0.19) first
    ((s_on, w_on, _),) = _verticals("XYZ", EXP, side, on, as_of, mkt, _scores(side))
    assert (s_on, w_on) == (b, b_wing)


# ---------------------------------------------------------------------------
# Expiry bonus
# ---------------------------------------------------------------------------


def test_expiry_volume_shares_and_bonus() -> None:
    e1, e2, e3 = dt.date(2026, 11, 13), dt.date(2026, 11, 20), dt.date(2026, 12, 18)
    cs = [
        _c(100, 0.3, vol=100, exp=e1),
        _c(110, 0.3, vol=300, exp=e2),
        _c(200, 0.3, vol=99_999, exp=e2),  # > 15% from spot 100: not counted
        _c(100, 0.3, vol=50, exp=e3),
    ]
    sleeves = {e1: 0, e2: 0, e3: 1}
    vol, share = expiry_volume_shares(cs, 100.0, sleeves)
    assert vol == {e1: 100, e2: 300, e3: 50}
    assert share == {e1: 0.25, e2: 0.75, e3: 1.0}
    assert expiry_bonus(share, sleeves, 0.25) == {e1: 0.0625, e2: 0.1875, e3: 0.0}  # e3 alone
    _, eq = expiry_volume_shares([], 100.0, {e1: 0, e2: 0})
    assert eq == {e1: 0.5, e2: 0.5}


def test_expiry_bonus_reorders_but_never_removes() -> None:
    spy = RecordedMarketData.from_files(SPY_CHAIN_FIXTURE)
    as_of = spy.recording("SPY").as_of
    strategies = [*CREDIT_STRATEGIES, *DEBIT_STRATEGIES]

    def run(mu: float) -> list[Any]:
        p = ScanParams(strategies=strategies, liquidity_rank=LiquidityRank(expiry_mu=mu))
        return scan(spy, "SPY", p, as_of=as_of).candidates

    def legs(c: Any) -> tuple[str, ...]:
        return tuple(leg.occ_symbol for leg in c.structure.legs)

    flat, bonus = run(0.0), run(1.0)
    assert sorted(map(legs, flat)) == sorted(map(legs, bonus))  # same set: nothing dropped
    assert [legs(c) for c in flat] != [legs(c) for c in bonus]  # reordered
    # 2026-10-30 holds ~99% of the near-spot volume: it moves up within each group
    thin = dt.date(2026, 11, 6)
    for got in (bonus,):
        for group in ({True}, {False}):
            g = [c for c in got if (c.credit_width is not None) in group]
            exps = [c.expiration for c in g]
            if thin in exps and dt.date(2026, 10, 30) in exps:
                assert exps.index(thin) > 0


def test_rank_bonus_only_shifts_the_primary_key() -> None:
    spy = RecordedMarketData.from_files(SPY_CHAIN_FIXTURE)
    res = scan(spy, "SPY", ScanParams(), as_of=spy.recording("SPY").as_of)
    a, b = res.candidates[0], res.candidates[1]
    assert _rank([a, b], ScanParams().rank_by)[0].structure == a.structure
    boosted = b.model_copy(update={"expiry_bonus": 1.0})
    assert _rank([a, boosted], ScanParams().rank_by)[0].structure == b.structure


# ---------------------------------------------------------------------------
# Off identity (golden captured on main c4fb48a, pre-E2.6) and the on dump
# ---------------------------------------------------------------------------


def _golden_runs(lr: LiquidityRank | None) -> dict[str, list[dict[str, Any]]]:
    spy = RecordedMarketData.from_files(SPY_CHAIN_FIXTURE)
    as_of = spy.recording("SPY").as_of
    out: dict[str, list[dict[str, Any]]] = {}
    for name, kw in {
        "credit": {"strategies": list(CREDIT_STRATEGIES)},
        "debit": {"strategies": list(DEBIT_STRATEGIES)},
        "debit_w5": {"strategies": list(DEBIT_STRATEGIES), "debit_width": 5.0},
        "wide_dte": {
            "strategies": [*CREDIT_STRATEGIES, *DEBIT_STRATEGIES],
            "dte_min": 20,
            "dte_max": 60,
        },
    }.items():
        res = scan(spy, "SPY", ScanParams(liquidity_rank=lr, **kw), as_of=as_of)  # type: ignore[arg-type]
        out[name] = [
            json.loads(json.dumps(c.model_dump(mode="json"), sort_keys=True))
            for c in res.candidates
        ]
    return out


def test_off_is_identical_to_main() -> None:
    want = json.loads(GOLDEN.read_text())
    got = _golden_runs(None)
    assert_json_close(got, {k: want[k] for k in got})
    for rows in got.values():
        for row in rows:
            assert "expiry_bonus" not in row and "score" not in row["structure"]["liquidity"]


def test_on_changes_selection_and_carries_the_score() -> None:
    off, on = _golden_runs(None), _golden_runs(LiquidityRank())
    assert off != on
    for rows in on.values():
        for row in rows:
            assert 0.0 <= row["structure"]["liquidity"]["score"] <= 1.0
            assert "expiry_bonus" in row


def test_scan_params_and_result_dumps_omit_off_fields() -> None:
    assert "liquidity_rank" not in ScanParams().model_dump()
    assert ScanParams(liquidity_rank=LiquidityRank()).model_dump()["liquidity_rank"]["lam"] == 0.5
    assert Liquidity(spread_pct=0.1, open_interest=5, volume=2).model_dump() == {
        "spread_pct": 0.1,
        "open_interest": 5,
        "volume": 2,
    }
    spy = RecordedMarketData.from_files(SPY_CHAIN_FIXTURE)
    as_of = spy.recording("SPY").as_of
    assert "expiry_volume" not in scan(spy, "SPY", ScanParams(), as_of=as_of).model_dump()
    on = scan(spy, "SPY", ScanParams(liquidity_rank=LiquidityRank()), as_of=as_of)
    assert on.expiry_volume == {"2026-10-30": 166889, "2026-11-06": 1330}


# ---------------------------------------------------------------------------
# Config + registry
# ---------------------------------------------------------------------------


def test_shipped_config_is_on_with_card_defaults() -> None:
    lr = load_routines().scanner.liquidity_rank
    assert lr.enabled is True
    assert (lr.weights.oi, lr.weights.volume, lr.weights.spread) == (0.35, 0.35, 0.30)
    assert (lr.lam, lr.expiry_mu) == (0.5, 0.25)
    assert liquidity_rank_from(lr) == LiquidityRank()


def test_off_parses_and_maps_to_none() -> None:
    off = LiquidityRankSettings.model_validate({"enabled": "off"})
    assert off.enabled is False and liquidity_rank_from(off) is None
    assert liquidity_rank_from(None) is None
    assert RoutinesConfig().scanner.liquidity_rank.enabled is False  # model default: off
    with pytest.raises(ValidationError):
        LiquidityRankSettings.model_validate({"enabled": True, "lam": 3})
    with pytest.raises(ValidationError):
        LiquidityRankSettings.model_validate({"enabled": True, "bogus": 1})


def test_registry_keys() -> None:
    raw = yaml.safe_load(Path("config/strategy.yaml").read_text())
    t = REGISTRY["scanner.liquidity_rank.enabled"]
    assert lookup("liquidity_rank").key == t.key
    assert t.risk is Risk.NONE and read_raw(t, raw) == "on"
    assert write_raw(t, "off", raw) == [(("scanner", "liquidity_rank", "enabled"), False)]
    for key, want in (
        ("scanner.liquidity_rank.lambda", 0.5),
        ("scanner.liquidity_rank.expiry_mu", 0.25),
        ("scanner.liquidity_rank.weights.oi", 0.35),
        ("scanner.liquidity_rank.weights.volume", 0.35),
        ("scanner.liquidity_rank.weights.spread", 0.30),
    ):
        assert read_raw(REGISTRY[key], raw) == want, key


def test_registry_override_reaches_the_effective_config(tmp_path: Path) -> None:
    from arc.routines.strategy import split_overrides

    routines, strategy = split_overrides({("scanner", "liquidity_rank", "enabled"): False})
    assert routines == {} and strategy == {("scanner", "liquidity_rank", "enabled"): False}
    cfg = load_routines(overrides={("scanner", "liquidity_rank", "lambda"): 1.5})
    assert math.isclose(cfg.scanner.liquidity_rank.lam, 1.5)


# ---------------------------------------------------------------------------
# Pipeline: the Quant menu's liquidity field
# ---------------------------------------------------------------------------


def test_quant_menu_carries_liquidity_only_when_on(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.test_menu_measure import _quant_chains, _run

    _, _, off = _run("margin", monkeypatch)
    _, _, on = _run("margin", monkeypatch, liquidity_rank=True)
    for chain in _quant_chains(off["quant"]).values():
        assert all("liquidity" not in row for row in chain["menu"])
    rows = [row for chain in _quant_chains(on["quant"]).values() for row in chain["menu"]]
    assert rows
    for row in rows:
        liq = row["liquidity"]
        assert set(liq) == {"score", "min_leg_oi", "min_leg_volume", "spread_pct"}
        assert 0.0 <= liq["score"] <= 1.0 and liq["min_leg_oi"] >= 100


def test_cli_chains_shows_the_score(capsys: pytest.CaptureFixture[str]) -> None:
    from arc.cli import main

    assert main(["chains", "SPY", "--fixture", "spy", "--liquidity-rank", "on", "--top", "3"]) == 0
    out = capsys.readouterr().out
    assert "liq 0." in out
    assert main(["chains", "SPY", "--fixture", "spy", "--liquidity-rank", "off", "--top", "3"]) == 0
    assert "liq " not in capsys.readouterr().out


def test_strategy_lane_unaffected_bands() -> None:
    """The delta bands and debit width are untouched by E2.6 (same ScanParams defaults)."""
    p = ScanParams(liquidity_rank=LiquidityRank())
    q = ScanParams()
    for f in ("delta_min", "delta_max", "long_delta_min", "long_delta_max", "debit_width"):
        assert getattr(p, f) == getattr(q, f)
    assert ScanStrategy.BULL_PUT in p.strategies
