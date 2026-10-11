"""E7.5b (PLAN D79): one menu measure for debit + credit, ranked before the cut, and the
stance-tilted ranking key (flag off; control byte-identical to main)."""

from __future__ import annotations

import datetime as dt
import json
import math
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from arc.config import ArcSettings
from arc.data.recorded import SPY_CHAIN_FIXTURE, RecordedMarketData
from arc.exits import load_exit_config, model_exits
from arc.scanner import ScanParams, scan
from arc.scanner.menu import MENU_MEASURE_RANKER, candidate_model, rank_menu
from arc.scanner.rank import (
    DIRECTIONAL_KINDS,
    Ranker,
    RankFilters,
    RankInputs,
    live_net_ev_check,
    rank,
    stance_sign,
    tilted_drift,
)
from tests.golden_compare import assert_json_close

if TYPE_CHECKING:
    from arc.exits import ExitConfig
    from arc.scanner import ScanCandidate, ScanResult

GOLDEN = Path(__file__).parent / "fixtures" / "menu_control_golden.json"
CHAINS_MARK = "### Option chains with Greeks\n"
R = 0.04


def _c(key: str, **kw: object) -> RankInputs:
    base: dict[str, object] = {
        "key": key,
        "credit": False,
        "vertical": True,
        "ev_proxy": 0.0,
        "managed_net_ev": 1.0,
        "managed_pop": 0.5,
    }
    base.update(kw)
    return RankInputs.model_validate(base)


def _rorc(ev: float, max_loss: float, days: float) -> float:
    return ev / (max_loss * days)


# ---------------------------------------------------------------------------
# One key for every kind (pure)
# ---------------------------------------------------------------------------


def test_debit_and_credit_sort_on_one_key_regardless_of_kind() -> None:
    # A debit with the better rorc_day outranks a credit with the better credit/width;
    # the scanner (credit_width) puts every credit ahead of every debit.
    debit = _c(
        "debit",
        credit=False,
        ev_ratio=0.01,
        managed_net_ev=30.0,
        rorc_day=_rorc(30.0, 300.0, 10.0),  # 0.0100
    )
    credit = _c(
        "credit",
        credit=True,
        credit_width=0.40,
        managed_net_ev=40.0,
        rorc_day=_rorc(40.0, 300.0, 20.0),  # 0.0067
    )
    menu = [credit, debit]
    assert [c.key for c in rank(menu, Ranker.CREDIT_WIDTH)] == ["credit", "debit"]
    assert [c.key for c in rank(menu, Ranker.RORC_DAY)] == ["debit", "credit"]
    # managed Net EV in dollars: the credit's $40 beats the debit's $30
    assert [c.key for c in rank(menu, Ranker.MANAGED_NET_EV)] == ["credit", "debit"]
    # same numbers, kinds swapped: the order follows the numbers, not the kind
    swapped = [
        credit.model_copy(update={"credit": False, "credit_width": None}),
        debit.model_copy(update={"credit": True, "credit_width": 0.1}),
    ]
    assert [c.key for c in rank(swapped, Ranker.RORC_DAY)] == ["debit", "credit"]


@settings(max_examples=150, deadline=None)
@given(
    rows=st.lists(
        st.tuples(
            st.booleans(),
            st.floats(-200, 200, allow_nan=False),
            st.floats(50, 2000),
            st.floats(1, 40),
        ),
        min_size=2,
        max_size=12,
    )
)
def test_unified_order_is_by_key_only(rows: list[tuple[bool, float, float, float]]) -> None:
    menu = [
        _c(
            f"k{i:02d}",
            credit=cr,
            credit_width=0.3 if cr else None,
            managed_net_ev=ev,
            rorc_day=_rorc(ev, ml, d),
        )
        for i, (cr, ev, ml, d) in enumerate(rows)
    ]
    for ranker, field in ((Ranker.RORC_DAY, "rorc_day"), (Ranker.MANAGED_NET_EV, "managed_net_ev")):
        got = [getattr(c, field) for c in rank(menu, ranker)]
        assert got == sorted(got, reverse=True)
        assert len(got) == len(menu)  # no kind is dropped or grouped


def test_tilted_ranker_falls_back_to_untilted_without_a_tilted_model() -> None:
    menu = [_c("a", rorc_day=0.01), _c("b", rorc_day=0.02), _c("c", rorc_day=0.03)]
    for untilted, tilted in (
        (Ranker.RORC_DAY, Ranker.RORC_DAY_TILTED),
        (Ranker.MANAGED_NET_EV, Ranker.MANAGED_NET_EV_TILTED),
    ):
        assert [c.key for c in rank(menu, tilted)] == [c.key for c in rank(menu, untilted)]
    menu[0] = menu[0].model_copy(update={"rorc_day_tilted": 0.09})
    assert [c.key for c in rank(menu, Ranker.RORC_DAY_TILTED)] == ["a", "c", "b"]


# ---------------------------------------------------------------------------
# Tilted drift (pure)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("stance", "kind", "want"),
    [
        ("bullish", "vertical_debit", 1),
        ("bearish", "vertical_debit", -1),
        ("bullish", "vertical_credit", 1),
        ("bullish", "long_call", 1),
        ("bearish", "long_put", -1),
        ("neutral", "vertical_debit", 0),
        ("bullish", "iron_condor", 0),
        ("bearish", "other", 0),
        (None, "long_call", 0),
        ("BULLISH", "long_call", 1),
        ("bullish", None, 0),
    ],
)
def test_stance_sign(stance: str | None, kind: str | None, want: int) -> None:
    assert stance_sign(stance, kind) == want


def test_directional_kinds() -> None:
    assert "iron_condor" not in DIRECTIONAL_KINDS
    assert {"vertical_debit", "vertical_credit", "long_call", "long_put"} == DIRECTIONAL_KINDS


@given(
    sign=st.sampled_from([-1, 0, 1]),
    tilt=st.floats(0.0, 0.5),
    sigma=st.floats(0.05, 1.5),
    days=st.floats(1.0, 60.0),
)
def test_tilted_drift_formula(sign: int, tilt: float, sigma: float, days: float) -> None:
    hold = days / 365.0
    mu = tilted_drift(r=R, sign=sign, tilt=tilt, sigma=sigma, hold_years=hold)
    if sign == 0 or tilt == 0.0:
        assert mu == R
    else:
        assert mu == pytest.approx(R + sign * tilt * sigma / math.sqrt(hold))
        # the expected log move over the hold is tilt x a 1-sigma hold move
        assert (mu - R) * hold == pytest.approx(sign * tilt * sigma * math.sqrt(hold))


def test_tilted_drift_degenerate_inputs_return_r() -> None:
    assert tilted_drift(r=R, sign=1, tilt=0.25, sigma=0.0, hold_years=0.1) == R
    assert tilted_drift(r=R, sign=1, tilt=0.25, sigma=0.3, hold_years=0.0) == R


# ---------------------------------------------------------------------------
# Tilt on the managed model: direction raises / lowers the key, never the card
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def debit_pool() -> tuple[ScanResult, ExitConfig]:
    s = ArcSettings(_env_file=None, account_profile="cash_debit")  # type: ignore[call-arg]
    prov = RecordedMarketData.from_files(SPY_CHAIN_FIXTURE)
    res = scan(prov, "SPY", ScanParams.from_settings(s), as_of=prov.recording("SPY").as_of)
    ex = load_exit_config()
    small = ex.model.model_copy(update={"n_paths": 4000})
    return res, ex.model_copy(update={"model": small})


def _first(res: ScanResult, strategy: str) -> ScanCandidate:
    return next(c for c in res.candidates if c.strategy.value == strategy)


def _ranked_key(
    c: ScanCandidate, res: ScanResult, ex: ExitConfig, stance: str, tilt: float
) -> tuple[float | None, float | None, float]:
    base = candidate_model(c, res.spot, ex, R, None)
    assert base is not None
    _, keys = rank_menu(
        [c],
        {id(c): base},
        measure="rorc_day_tilted",
        top=1,
        stance=stance,
        tilt=tilt,
        spot=res.spot,
        exits=ex,
        r=R,
        realized_vol=None,
    )
    k = keys[id(c)]
    return k.value, base.rorc_day, base.managed.net_ev


@pytest.mark.parametrize("strategy", ["bull_call_debit", "long_call"])
def test_bullish_tilt_raises_a_bull_call_key(debit_pool: Any, strategy: str) -> None:
    res, ex = debit_pool
    c = _first(res, strategy)
    tilted, untilted, _ = _ranked_key(c, res, ex, "bullish", 0.25)
    assert tilted is not None and untilted is not None
    assert tilted > untilted
    # zero tilt == untilted (no second model run)
    zero, untilted2, _ = _ranked_key(c, res, ex, "bullish", 0.0)
    assert zero == untilted == untilted2


@pytest.mark.parametrize("strategy", ["bear_put_debit", "long_put"])
def test_bearish_stance_raises_a_bear_put_key_and_bullish_lowers_it(
    debit_pool: Any, strategy: str
) -> None:
    res, ex = debit_pool
    c = _first(res, strategy)
    bear, untilted, _ = _ranked_key(c, res, ex, "bearish", 0.25)
    assert bear is not None and untilted is not None and bear > untilted
    # A positive (up) drift on a bear put lowers its key: drive it directly
    base = candidate_model(c, res.spot, ex, R, None)
    assert base is not None
    up = tilted_drift(
        r=R,
        sign=1,
        tilt=0.25,
        sigma=base.path_vol,
        hold_years=base.managed.expected_days_held / 365,
    )
    m_up = candidate_model(c, res.spot, ex, R, None, drift=up)
    assert m_up is not None and m_up.rorc_day is not None and base.rorc_day is not None
    assert m_up.rorc_day < base.rorc_day


def test_neutral_stance_is_untilted(debit_pool: Any) -> None:
    res, ex = debit_pool
    c = _first(res, "bull_call_debit")
    tilted, untilted, _ = _ranked_key(c, res, ex, "neutral", 0.5)
    assert tilted == untilted


def test_condor_is_never_tilted() -> None:
    s = ArcSettings(_env_file=None, account_profile="margin")  # type: ignore[call-arg]
    prov = RecordedMarketData.from_files(SPY_CHAIN_FIXTURE)
    params = ScanParams.from_settings(s, strategies=["iron_condor"])
    res = scan(prov, "SPY", params, as_of=prov.recording("SPY").as_of)
    ex = load_exit_config()
    ex = ex.model_copy(update={"model": ex.model.model_copy(update={"n_paths": 2000})})
    c = res.candidates[0]
    tilted, untilted, _ = _ranked_key(c, res, ex, "bullish", 0.5)
    assert tilted == untilted


def test_drift_none_is_the_untilted_model(debit_pool: Any) -> None:
    res, ex = debit_pool
    c = _first(res, "bull_call_debit")
    kw = {"spot": res.spot, "iv": c.atm_iv, "r": R, "cfg": ex.model, "spreads": c.leg_spreads}
    pol = ex.policy_for(c.structure.kind)
    a = model_exits(c.structure, pol, **kw)  # type: ignore[arg-type]
    b = model_exits(c.structure, pol, drift=None, **kw)  # type: ignore[arg-type]
    d = model_exits(c.structure, pol, drift=R, **kw)  # type: ignore[arg-type]
    assert a.model_dump() == b.model_dump() == d.model_dump()


# ---------------------------------------------------------------------------
# rank_menu: full pool, one ordering, unmodelled last, cut after ranking
# ---------------------------------------------------------------------------


def test_rank_menu_full_pool_cuts_after_ranking(debit_pool: Any) -> None:
    res, ex = debit_pool
    pool = list(res.candidates)
    assert len(pool) > 5
    models = {id(c): m for c in pool if (m := candidate_model(c, res.spot, ex, R, None))}
    kept, keys = rank_menu(
        pool,
        models,
        measure="rorc_day_full",
        top=5,
        stance="bullish",
        tilt=0.0,
        spot=res.spot,
        exits=ex,
        r=R,
        realized_vol=None,
    )
    assert len(kept) == 5
    vals = [keys[id(c)].value for c in kept]
    best5 = sorted((models[id(c)].rorc_day for c in pool), reverse=True)[:5]  # type: ignore[type-var]
    assert vals == best5
    assert all(keys[id(c)].measure == "rorc_day_full" for c in kept)
    assert all(keys[id(c)].tilt is None for c in kept)


def test_rank_menu_unmodelled_sort_last_in_scanner_order(debit_pool: Any) -> None:
    res, ex = debit_pool
    pool = list(res.candidates)[:6]
    models = {id(c): m for c in pool if (m := candidate_model(c, res.spot, ex, R, None))}
    unmodelled = [pool[0], pool[3]]
    for c in unmodelled:
        models.pop(id(c))
    kept, keys = rank_menu(
        pool,
        models,
        measure="managed_net_ev_full",
        top=10,
        stance="neutral",
        tilt=0.0,
        spot=res.spot,
        exits=ex,
        r=R,
        realized_vol=None,
    )
    assert kept[-2:] == unmodelled
    assert keys[id(pool[0])].value is None


def test_every_measure_maps_to_a_ranker() -> None:
    from arc.exits.policy import MenuMeasure

    measures = set(MenuMeasure.__args__) - {"control"}  # type: ignore[attr-defined]
    assert measures == set(MENU_MEASURE_RANKER)


# ---------------------------------------------------------------------------
# Pipeline: control golden, *_full menus, the tilt never reaches the card / floor
# ---------------------------------------------------------------------------


def _quant_chains(prompt: str) -> dict[str, Any]:
    body = prompt.split(CHAINS_MARK, 1)[1].split("\n\n### ", 1)[0]
    return json.loads(body)


def _run(profile: str, monkeypatch: pytest.MonkeyPatch, **pipeline: object) -> Any:
    from arc.routines.config import load_routines
    from tests import test_pipeline as tp

    if pipeline:
        import arc.pipeline.steps as steps

        cfg = load_exit_config()
        on = cfg.model_copy(update={"pipeline": cfg.pipeline.model_copy(update=pipeline)})
        monkeypatch.setattr(steps, "exit_config", lambda _settings=None: on)
    monkeypatch.delenv("ARC_GATE_SECRET", raising=False)
    s = ArcSettings(_env_file=None, account_profile=profile)  # type: ignore[call-arg]
    return tp._recording_fixture_run(s, load_routines())


@pytest.mark.parametrize("profile", ["margin", "cash_debit"])
def test_control_menu_is_identical_to_main(profile: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Golden captured on main 71caa36 (pre-E7.5b): same menus, order and numbers.

    The full Quant prompt was also checked byte-identical to main for both profiles
    when the golden was captured (PR body); the chains JSON is the part E7.5b touches.
    """
    want = json.loads(GOLDEN.read_text())[profile]
    _, _, prompts = _run(profile, monkeypatch)
    got = _quant_chains(prompts["quant"])
    assert_json_close(got, want)
    for chain in got.values():
        for row in chain["menu"]:
            assert "rank_key" not in row and "rank_measure" not in row


def test_control_ignores_tilt_and_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    _, _, base = _run("cash_debit", monkeypatch)
    _, _, knobs = _run(
        "cash_debit", monkeypatch, menu_measure="control", direction_tilt=0.5, menu_pool_max=40
    )
    assert base["quant"] == knobs["quant"]  # Quant prompt bytes


@pytest.mark.parametrize("measure", ["rorc_day_full", "managed_net_ev_full", "rorc_day_tilted"])
def test_full_measure_ranks_the_pool_before_the_cut(
    measure: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, prompts = _run("margin", monkeypatch, menu_measure=measure, direction_tilt=0.25)
    chains = _quant_chains(prompts["quant"])
    field = {"rorc_day_full": "rorc_day", "managed_net_ev_full": "managed_net_ev"}.get(measure)
    for t, chain in chains.items():
        menu = chain["menu"]
        assert 1 <= len(menu) <= 5, t  # still cut to pipeline_scan_top
        keys = [row["rank_key"] for row in menu if row["rank_key"] is not None]
        assert keys == sorted(keys, reverse=True), t
        for row in menu:
            assert row["rank_measure"] == measure
            if field is not None and row["exits"] is not None:
                assert row["rank_key"] == row["exits"][field]
            assert ("rank_tilt" in row) == (measure == "rorc_day_tilted")


def test_tilt_never_reaches_card_or_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    """The stored exits numbers, the Net EV floor input and the PoP are the untilted
    model's, whatever the tilt; only rank_key moves."""
    _, _, full = _run("cash_debit", monkeypatch, menu_measure="rorc_day_full")
    conn, _, tilted = _run(
        "cash_debit", monkeypatch, menu_measure="rorc_day_tilted", direction_tilt=0.5
    )
    by_legs = lambda m: {tuple(x["occ_symbol"] for x in r["legs"]): r for r in m}  # noqa: E731
    f = by_legs(_quant_chains(full["quant"])["NVDA"]["menu"])
    tl = by_legs(_quant_chains(tilted["quant"])["NVDA"]["menu"])
    common = set(f) & set(tl)
    assert common
    moved = 0
    for k in common:
        assert tl[k]["exits"] == f[k]["exits"]  # managed_net_ev, managed_pop, rorc_day ...
        assert tl[k]["pop"] == f[k]["pop"]
        moved += tl[k]["rank_key"] != tl[k]["exits"]["rorc_day"]
    assert moved  # NVDA is bullish in the fixture: the tilted key differs from rorc_day
    row = conn.execute(
        "select payload from context_entries where kind='structures' order by created_at desc"
    ).fetchone()
    for s in json.loads(row["payload"])["structures"]:
        k = tuple(x["occ_symbol"] for x in s["legs"])
        if k in f:
            assert s["exits"]["managed_net_ev"] == f[k]["exits"]["managed_net_ev"]
            ok, _ = live_net_ev_check(s["exits"]["managed_net_ev"], RankFilters())
            ok_f, _ = live_net_ev_check(f[k]["exits"]["managed_net_ev"], RankFilters())
            assert ok == ok_f


# ---------------------------------------------------------------------------
# Config, registry, CLI, draft XP
# ---------------------------------------------------------------------------


def test_exits_yaml_ships_control() -> None:
    p = load_exit_config().pipeline
    assert (p.menu_measure, p.direction_tilt, p.menu_pool_max) == ("control", 0.0, 20)
    assert p.rank_menu_by == "scanner"


def test_registry_bounds() -> None:
    from arc.control.registry import REGISTRY

    t = REGISTRY.get("menu_direction_tilt")
    assert (t.min, t.max, t.path) == (0.0, 0.5, ("pipeline", "direction_tilt"))
    p = REGISTRY.get("menu_pool_max")
    assert (p.min, p.max) == (5, 40)
    m = REGISTRY.get("menu_measure")
    assert m.choices is not None and m.choices[0] == "control"


@pytest.mark.parametrize(
    ("key", "bad"), [("direction_tilt", 0.6), ("direction_tilt", -0.1), ("menu_pool_max", 41)]
)
def test_out_of_bounds_config_rejected(key: str, bad: float) -> None:
    from pydantic import ValidationError

    from arc.exits.policy import PipelineExitConfig

    with pytest.raises(ValidationError):
        PipelineExitConfig.model_validate({key: bad})


def test_chains_cli_unified_and_tilted(capsys: pytest.CaptureFixture[str]) -> None:
    from arc.cli import main

    rc = main(
        [
            "chains",
            "SPY",
            "--fixture",
            "spy",
            "--profile",
            "cash_debit",
            "--db",
            "/nonexistent/arc.db",
            "--top",
            "5",
            "--rank-by",
            "rorc_day_tilted",
            "--tilt",
            "0.25",
            "--stance",
            "bullish",
        ]
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "ranked by rorc_day_tilted tilt 0.25 stance bullish" in out
    rows = [ln for ln in out.splitlines() if " key " in ln]
    assert 1 <= len(rows) <= 5
    assert all("put" not in ln.split()[1] for ln in rows)  # bullish: calls only
    vals = [float(ln.rsplit(" key ", 1)[1]) for ln in rows]
    assert vals == sorted(vals, reverse=True)


def test_chains_cli_json_has_rank_key(capsys: pytest.CaptureFixture[str]) -> None:
    from arc.cli import main

    rc = main(
        [
            "chains",
            "SPY",
            "--fixture",
            "spy",
            "--profile",
            "margin",
            "--db",
            "/nonexistent/arc.db",
            "--top",
            "3",
            "--rank-by",
            "rorc_day_full",
            "--json",
        ]
    )
    assert rc == 0
    (res,) = json.loads(capsys.readouterr().out)
    assert [c["rank"] for c in res["candidates"]] == [1, 2, 3]
    keys = [c["rank_key"] for c in res["candidates"]]
    assert keys == sorted(keys, reverse=True)
    assert {c["rank_measure"] for c in res["candidates"]} == {"rorc_day_full"}


def test_menu_measure_overlays_validate_as_arms() -> None:
    # D86: the retired menu-measure draft is idea-seed lead S-5; its three arm overlays
    # must still load as exits.yaml overlays in a multi-arm spec.
    from arc.experiments.overlay import validate_arms
    from tests import experiment_fixtures as fx

    pipe = {
        "t1": {"menu_measure": "rorc_day_full"},
        "t2": {"menu_measure": "rorc_day_tilted", "direction_tilt": 0.25},
        "t3": {"menu_measure": "managed_net_ev_full"},
    }
    arms = {n: {"overlay": {"exits": {"pipeline": p}}} for n, p in pipe.items()}
    spec = fx.spec("XP-20", spec_version=2, arms={"control": {}, "treatments": arms})
    validate_arms(spec)
    assert spec.arms.names == ["t1", "t2", "t3"]
    assert spec.arms.arm("t2").overlay["exits"]["pipeline"]["direction_tilt"] == 0.25


def test_backtest_tilt_and_live_shaped_menu_options() -> None:
    from arc.backtest.ranking import BacktestSettings, MenuSpec, load_ranking_file
    from arc.backtest.strategies import ExpiryMode

    f = load_ranking_file()
    assert f.backtest.direction_tilt == 0.0  # E7.5 primary run unchanged
    assert all(m.expiry_mode is ExpiryMode.NEAREST for ms in f.backtest.menus.values() for m in ms)
    m = MenuSpec.model_validate({"kinds": ["long_call"], "deltas": [0.55], "expiry_mode": "all"})
    assert {s.expiry_mode for s in m.specs(30, 60)} == {ExpiryMode.ALL}
    with pytest.raises(ValueError, match="less than or equal"):
        BacktestSettings.model_validate({"menus": {}, "direction_tilt": 0.9})


def test_stance_hit_rate() -> None:
    import pandas as pd

    from arc.backtest.ranking import stance_hit_rate, trend_stances

    days = [dt.date(2025, 1, 1) + dt.timedelta(days=i) for i in range(6)]
    closes = pd.Series([100, 101, 102, 101, 100, 99], index=days, dtype=float)
    trend = pd.Series(["bull", "bull", "bear", "sideways", "bear", "bull"], index=days)
    hit, n = stance_hit_rate(closes, trend, horizon=1, start=days[0], end=days[-1])
    # bull d0 (+) hit, bull d1 (+) hit, bear d2 (-) hit, bear d4 (-) hit; d5 has no fwd
    assert (hit, n) == (1.0, 4)
    assert trend_stances(trend)[days[3]] == "neutral"
    assert trend_stances(trend)[days[0]] == "bullish"
