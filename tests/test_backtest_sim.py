"""E7.11 (D84): the live-shaped portfolio simulator (arc.backtest.sim)."""

from __future__ import annotations

import datetime as dt
import json
import random
from pathlib import Path

import pandas as pd
import pytest

from arc.backtest.costs import load_cost_model
from arc.backtest.data import DataSettings, HistoryView
from arc.backtest.ranking import (
    picked_outcomes,
    run_portfolio,
    simulate_outcome,
)
from arc.backtest.sim import (
    GATE_RULES_APPLIED,
    NOT_REPLAYED,
    SKIP_REASONS,
    DayInfo,
    SimSettings,
    SimTicker,
    active_list,
    capacity_cap,
    momentum_scores,
    run_sim,
    sim_outcome,
    sim_ticker_from_chains,
    spread_ok,
)
from arc.config import ArcSettings
from arc.exits.policy import load_exit_config
from arc.scanner.rank import Ranker, RankFilters
from tests.backtest_data_fixtures import (
    R,
    sessions,
    spot_path,
    synth_rows,
    write_history,
    write_underlying,
    write_universe,
)

REPO = Path(__file__).resolve().parent.parent
COST = load_cost_model(REPO / "config" / "costs.yaml")
EXITS = load_exit_config()
N = 70
DAYS = sessions(dt.date(2025, 3, 3), N)
DECIDE = DAYS[:12]
WINDOWS = {"margin": (20, 45), "cash_debit": (20, 45)}
NO_FILTERS = RankFilters(enabled=False)  # flat-vol synthetic chains carry no edge


def _cfg():  # noqa: ANN202 - RankingFile
    from arc.backtest.ranking import load_ranking_file

    cfg = load_ranking_file()
    # D131 (E28.1): the sim tests were worked out on the pre-D131 credit menu (nearest 2%
    # wing, no floor); the shipped widest-fit wing is tested in test_e281_credit_wings.py.
    legacy = {"width_pct": 0.02, "wing_fit": "nearest", "wing_cap": None, "min_credit_width": 0.0}
    menus = {
        p: [m.model_copy(update=legacy) if m.wing_fit == "widest" else m for m in ms]
        for p, ms in cfg.backtest.menus.items()
    }
    bt = cfg.backtest.model_copy(update={"n_paths": 200, "stance": "none", "menus": menus})
    return cfg.model_copy(
        update={
            "backtest": bt,
            "ranking": cfg.ranking.model_copy(update={"filters": NO_FILTERS}),
        }
    )


def _settings(profile: str = "margin", **kw: object) -> ArcSettings:
    return ArcSettings().with_profile(profile).model_copy(update=kw)


@pytest.fixture(scope="module")
def view(tmp_path_factory: pytest.TempPathFactory) -> HistoryView:
    root = tmp_path_factory.mktemp("sim")
    spots = spot_path(N)
    for t in ("AAA", "ZZZ"):  # identical prices -> identical candidate scores
        write_history(root, t, synth_rows(t, DAYS, spots, oi=400.0))
        write_underlying(root, t, DAYS, spots)
    q = dt.date(2025, 1, 1)
    write_universe(
        root,
        ["AAA", "ZZZ"],
        [(q, "AAA", True), (q, "ZZZ", True), (dt.date(2025, 4, 1), "AAA", True)]
        + [(dt.date(2025, 4, 1), "ZZZ", True)],
    )
    return HistoryView(
        root,
        settings=DataSettings(pit=False),
        universe="c125",
        start=DAYS[0],
        end=DAYS[-1],
        cost=COST,
        flat_rate=R,
        min_iv_obs=20,
    )


def _days(liq: float, **kw: object) -> dict[dt.date, DayInfo]:
    base: dict[str, object] = {
        "eligible": True,
        "screen_pass": True,
        "seed": False,
        "liquidity": liq,
        "earnings_known": True,
        "next_earnings": None,
    }
    base |= kw
    return {d: DayInfo(**base) for d in DAYS}  # type: ignore[arg-type]


def _ticker(
    view: HistoryView,
    t: str,
    *,
    sim: SimSettings | None = None,
    liq: float = 100.0,
    profiles: tuple[str, ...] = ("margin",),
    rankers: tuple[Ranker, ...] = (Ranker.CREDIT_WIDTH,),
    **day_kw: object,
) -> SimTicker:
    h = view.load(t)
    return sim_ticker_from_chains(
        t,
        h.chains,
        h.closes,
        decision_days=DECIDE,
        days=_days(liq, **day_kw),
        cfg=_cfg(),
        exits=EXITS,
        cost=COST,
        settings={p: _settings(p) for p in profiles},
        windows={p: WINDOWS[p] for p in profiles},
        rankers={p: list(rankers) for p in profiles},
        stances=None,
        sim=sim or SimSettings(regime_model="v1"),
    )


def _run(
    tickers: dict[str, SimTicker],
    *,
    sim: SimSettings | None = None,
    settings: ArcSettings | None = None,
    ranker: Ranker = Ranker.CREDIT_WIDTH,
    profile: str = "margin",
    **kw: object,
):  # noqa: ANN202 - SimRun
    return run_sim(
        tickers,
        ranker=ranker,
        profile=profile,
        settings=settings or _settings(profile),
        sim=sim or SimSettings(momentum_size=0),
        filters=NO_FILTERS,
        vrp_threshold=0.0,
        sessions=DAYS,
        end=DECIDE[-1],
        **kw,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# Pure pieces
# ---------------------------------------------------------------------------


def test_spread_rule_matches_the_live_threshold() -> None:
    s = _settings()  # spread_max_pct 0.10, spread_max_abs 0.10
    assert spread_ok(2.0, 0.20, s)  # 10 % of mid
    assert not spread_ok(2.0, 0.21, s)
    assert spread_ok(0.5, 0.10, s)  # the absolute floor
    assert not spread_ok(0.5, 0.11, s)
    assert not spread_ok(float("nan"), 0.01, s)


def test_capacity_cap_takes_the_tightest_leg() -> None:
    sim = SimSettings()
    assert capacity_cap([400.0, 1000.0], [50.0, 50.0], sim) == 12  # 25 % of 50 volume
    assert capacity_cap([100.0], [1000.0], sim) == 5  # 5 % of 100 OI
    assert capacity_cap([None], [None], sim) is None  # alpaca: no OI, no volume row
    assert capacity_cap([None], [40.0], sim) == 10  # volume only


def test_momentum_is_risk_adjusted_12_1_on_past_closes_only() -> None:
    idx = sessions(dt.date(2024, 1, 2), 60)
    up = pd.Series([100 * 1.004**i for i in range(60)], index=idx)
    flat = pd.Series([100.0 + (i % 2) for i in range(60)], index=idx)
    day = idx[50]
    s = momentum_scores({"UP": up, "FLAT": flat}, day, lookback=40, skip=5)
    assert s["UP"] > s["FLAT"]
    future = up.copy()
    future[future.index > day] = 1.0  # a crash after d changes nothing
    assert momentum_scores({"UP": future}, day, lookback=40, skip=5)["UP"] == s["UP"]
    assert momentum_scores({"UP": up}, idx[20], lookback=40, skip=5) == {}  # too short


def test_active_list_core_then_momentum_then_liquidity() -> None:
    sim = SimSettings(active_cap=4, momentum_size=1)
    got = active_list(
        dt.date(2025, 3, 3),
        core=["SPY"],
        momentum=["MOM1", "MOM2"],
        rest=[("LOW", 1.0), ("HIGH", 9.0), ("MID", 5.0)],
        sim=sim,
    )
    assert got == ["SPY", "MOM1", "HIGH", "MID"]


# ---------------------------------------------------------------------------
# Fill model on real (synthetic) chains
# ---------------------------------------------------------------------------


def _a_candidate(view: HistoryView):  # noqa: ANN202
    t = _ticker(view, "AAA")
    menu = t.menus["margin"][DECIDE[0]]
    assert menu, "fixture should build candidates"
    return menu[0].candidate


def test_sim_outcome_without_spread_rule_equals_the_e75_outcome(view: HistoryView) -> None:
    c = _a_candidate(view)
    h = view.load("AAA")
    ours = sim_outcome(
        c,
        chains=h.chains,
        closes=h.closes,
        cost=COST,
        exits=EXITS,
        settings=_settings(),
        spread_rule=False,
    )
    ref = simulate_outcome(
        c, chains=h.chains, days=sorted(h.chains), closes=h.closes, cost=COST, exits=EXITS
    )
    assert ours is not None and ref is not None
    assert ours.outcome.model_dump() == ref.model_dump()
    assert ours.entry_ok and ours.exit_delays == 0
    assert all(o == 400.0 for o in ours.leg_oi) and all(v == 50.0 for v in ours.leg_volume)


def test_no_fill_on_a_wide_entry_spread_and_a_delayed_exit(view: HistoryView) -> None:
    c = _a_candidate(view)
    h = view.load("AAA")
    kw = {"closes": h.closes, "cost": COST, "exits": EXITS, "settings": _settings()}
    base = sim_outcome(c, chains=h.chains, spread_rule=True, **kw)
    assert base is not None and base.entry_ok
    # entry: one leg's spread past the live rule -> no fill
    sym = c.picks[0].symbol
    wide_entry = c.model_copy(
        update={"picks": [c.picks[0].model_copy(update={"spread": 5.0}), *c.picks[1:]]}
    )
    w = sim_outcome(wide_entry, chains=h.chains, spread_rule=True, **kw)
    assert w is not None and not w.entry_ok
    # exit: the session the exit fired is too wide -> it waits for the next one
    fired = base.outcome.exit_day
    if base.outcome.trade.exit_date is None:
        pytest.skip("fixture candidate held to expiry")
    chains = dict(h.chains)
    ch = chains[fired].copy()
    ch.loc[ch["symbol"] == sym, "spread"] = 50.0
    chains[fired] = ch
    late = sim_outcome(c, chains=chains, spread_rule=True, **kw)
    assert late is not None and late.exit_delays >= 1
    assert late.outcome.exit_day > fired
    off = sim_outcome(c, chains=chains, spread_rule=False, **kw)
    assert off is not None and off.outcome.exit_day == fired


# ---------------------------------------------------------------------------
# The walk
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def pair(view: HistoryView) -> dict[str, SimTicker]:
    return {"AAA": _ticker(view, "AAA", liq=10.0), "ZZZ": _ticker(view, "ZZZ", liq=90.0)}


def _trade_key(df: pd.DataFrame) -> list[tuple[object, ...]]:
    cols = ["underlying", "entry_date", "exit_date", "contracts", "kind", "legs"]
    return sorted(map(tuple, df[cols].itertuples(index=False))) if len(df) else []


def test_cross_name_ranking_is_order_independent(pair: dict[str, SimTicker]) -> None:
    a = _run(pair)
    names = list(pair)
    rng = random.Random(7)
    for _ in range(3):
        rng.shuffle(names)
        b = _run({t: pair[t] for t in names})
        assert _trade_key(b.trades) == _trade_key(a.trades)
        assert b.equity.equals(a.equity)
        assert b.skip_counts == a.skip_counts
    assert len(a.trades) > 0
    assert set(a.skip_counts) == set(SKIP_REASONS)


def test_one_slot_goes_to_the_better_name_not_the_first_alphabetically(
    pair: dict[str, SimTicker],
) -> None:
    # identical candidates: liquidity breaks the tie, so ZZZ (90) beats AAA (10)
    one = _run(pair, settings=_settings(max_open_positions=1))
    first = one.trades.sort_values("entry_date").iloc[0]
    assert first["underlying"] == "ZZZ" and first["entry_date"] == DECIDE[0]
    assert one.skip_counts["gate_max_open_positions"] > 0
    # the E7.5 walk visits tickers alphabetically: AAA takes the slot
    legacy = _run(pair, sim=SimSettings(selection="per_ticker"), settings=one_slot_legacy())
    assert legacy.trades.sort_values("entry_date").iloc[0]["underlying"] == "AAA"


def one_slot_legacy() -> ArcSettings:
    return _settings(max_open_positions=1)


def test_sizing_on_the_25k_account(pair: dict[str, SimTicker]) -> None:
    run = _run(pair, sim=SimSettings(momentum_size=0, capacity="off"))
    t = run.trades
    assert len(t) > 0
    cap = 0.10 * 25_000  # max_alloc_pct x D80 equity (equity moves only by P&L)
    assert (t["max_loss_unit"] * t["contracts"] <= cap * 1.2).all()
    assert run.equity.iloc[0] == pytest.approx(25_000, abs=200)
    # the per-underlying budget holds across positions on the same name
    big = _run(pair, sim=SimSettings(momentum_size=0, capacity="off", equity=250_000))
    assert big.trades["contracts"].max() > t["contracts"].max()


def test_capacity_clips_and_skips(pair: dict[str, SimTicker]) -> None:
    off = _run(pair, sim=SimSettings(momentum_size=0, capacity="off", equity=250_000))
    assert off.trades["contracts"].max() > 12
    clip = _run(pair, sim=SimSettings(momentum_size=0, capacity="clip", equity=250_000))
    assert clip.trades["contracts"].max() <= 12  # 25 % of 50 contracts of volume
    assert clip.trades["capacity_clipped"].any()
    assert clip.skip_counts["capacity"] > 0
    assert (clip.skips[clip.skips.reason == "capacity"]["detail"].str.startswith("clipped")).all()
    skip = _run(pair, sim=SimSettings(momentum_size=0, capacity="skip", equity=250_000))
    assert len(skip.trades) < len(off.trades)


def test_earnings_blackout_blocks_a_credit_open(view: HistoryView) -> None:
    exp_soon = DECIDE[0] + dt.timedelta(days=10)
    blocked = {"AAA": _ticker(view, "AAA", next_earnings=exp_soon)}
    run = _run(blocked)
    t = run.trades
    held = (t["entry_date"] <= exp_soon) & (t["expiration"] >= exp_soon)
    through = t[(t["entry_net"] < 0) & held]
    assert len(through) == 0  # no short premium held through the projected date
    assert run.skip_counts["blackout"] > 0
    # unknown earnings date fails closed, like the live rule
    unknown = _run({"AAA": _ticker(view, "AAA", earnings_known=False)})
    assert unknown.skip_counts["blackout"] > 0
    # blackout off -> the same credit opens
    free = _run(blocked, sim=SimSettings(momentum_size=0, earnings_blackout=False))
    f = free.trades
    assert ((f["entry_net"] < 0) & (f["entry_date"] <= exp_soon)).any()


def test_screen_and_eligibility_feed_the_funnel(view: HistoryView) -> None:
    t = {
        "AAA": _ticker(view, "AAA", screen_pass=False),
        "ZZZ": _ticker(view, "ZZZ", screen_pass=False, seed=True),
    }
    run = _run(t)
    assert set(run.trades["underlying"]) == {"ZZZ"}  # seed names skip the screen
    assert run.skip_counts["screen"] == len(DECIDE)
    d = run.decisions.set_index("day")
    assert (d.loc[DECIDE, "eligible"] == 2).all() and (d.loc[DECIDE, "screened"] == 1).all()
    assert (d.loc[DECIDE, "active"] == 1).all()
    assert d.loc[DAYS[-1], "candidates"] == 0  # past --to: no decisions
    open_screen = _run(t, sim=SimSettings(momentum_size=0, apply_screen=False))
    assert set(open_screen.trades["underlying"]) == {"AAA", "ZZZ"}


def test_per_ticker_mode_reproduces_the_e75_harness(view: HistoryView) -> None:
    """active_cap large, screen and blackout off, selection per_ticker -> run_portfolio."""
    sim = SimSettings(
        selection="per_ticker",
        active_cap=500,
        apply_screen=False,
        earnings_blackout=False,
        regime_model="v1",
    )
    profile, ranker = "margin", Ranker.CREDIT_WIDTH
    settings = _settings(profile)
    cfg = _cfg()
    built = {t: _ticker(view, t, sim=sim) for t in ("AAA", "ZZZ")}
    ours = run_sim(
        built,
        ranker=ranker,
        profile=profile,
        settings=settings,
        sim=sim,
        filters=NO_FILTERS,
        vrp_threshold=0.0,
        sessions=DAYS,
        end=DAYS[-1],
        starting_equity=100_000.0,
    )
    menus = {}
    outcomes = {}
    for t, st in built.items():
        h = view.load(t)
        m = {d: [sc.candidate for sc in v] for d, v in st.menus[profile].items()}
        # E7.5 menus hold every candidate; ours hold each ranker's top: same top-1
        menus[t] = m
        outcomes |= picked_outcomes(
            m,
            rankers=[ranker],
            filters=NO_FILTERS,
            vrp_threshold=0.0,
            chains=h.chains,
            closes=h.closes,
            cost=COST.model_copy(update={"slippage_frac": sim.slippage_x}),
            exits=EXITS,
        )
    ref = run_portfolio(
        menus,
        outcomes,
        ranker=ranker,
        profile=profile,
        settings=settings,
        starting_equity=100_000.0,
        filters=NO_FILTERS,
        vrp_threshold=cfg.ranking.vrp_threshold,
        sessions=DAYS,
        slippage=sim.slippage_x,
        trend={t: b.trend for t, b in built.items()},
        vol={t: b.vol for t, b in built.items()},
    )
    assert len(ref.trades) > 0
    cols = ["underlying", "entry_date", "exit_date", "contracts", "pnl", "kind", "legs"]
    a = ours.trades[cols].sort_values(["entry_date", "underlying"]).reset_index(drop=True)
    b = ref.trades[cols].sort_values(["entry_date", "underlying"]).reset_index(drop=True)
    pd.testing.assert_frame_equal(a, b)
    pd.testing.assert_series_equal(ours.equity, ref.equity)


def test_cli_sim_writes_outputs_and_workers_match_serial(
    view: HistoryView, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import json

    from arc.cli import main

    cfg_path = tmp_path / "ranking.yaml"
    import yaml

    raw = yaml.safe_load((REPO / "config" / "ranking.yaml").read_text())
    raw["backtest"]["n_paths"] = 200
    raw["backtest"]["stance"] = "none"
    raw["backtest"]["data"]["pit"] = False
    raw["ranking"]["filters"]["enabled"] = False
    cfg_path.write_text(yaml.safe_dump(raw))

    def run(out: Path, workers: int) -> dict:
        rc = main(
            [
                "backtest",
                "sim",
                "--universe",
                "c125",
                "--from",
                DECIDE[0].isoformat(),
                "--to",
                DECIDE[3].isoformat(),
                "--data-end",
                DAYS[-1].isoformat(),
                "--profile",
                "cash_debit",
                "--data-dir",
                str(view.data_dir),
                "--config",
                str(cfg_path),
                "--out",
                str(out),
                "--workers",
                str(workers),
                "--no-cache",
            ]
        )
        assert rc == 0
        return json.loads((out / "manifest.json").read_text())

    m = run(tmp_path / "a", 1)
    assert m["tickers"] == 2 and m["equity"] == 25_000 and m["partitions"] == {"thetadata": 2}
    # E7.19 (D128): the incumbent is the live menu measure's ranker (managed_net_ev_full)
    assert m["rankers"] == {"cash_debit": ["managed_net_ev"]}
    assert set(m["runtime_s"]) == {"build", "walk", "total"} and m["peak_rss_mb"] > 0
    run_dir = tmp_path / "a" / "cash_debit" / "managed_net_ev"
    for f in ("trades.csv", "skips.csv", "equity.csv", "decisions.csv"):
        assert (run_dir / f).is_file()
    dec = pd.read_csv(run_dir / "decisions.csv")
    assert {"eligible", "screened", "active", "candidates", "opened"} <= set(dec.columns)
    report = (tmp_path / "a" / "report.md").read_text()
    assert "Discovery and Trending" in report and "check_per_underlying" in report
    assert "Discovery and Trending" in capsys.readouterr().out
    run(tmp_path / "b", 2)
    for f in ("trades.csv", "skips.csv", "equity.csv", "decisions.csv"):
        a = (run_dir / f).read_text()
        assert (tmp_path / "b" / "cash_debit" / "managed_net_ev" / f).read_text() == a


def test_cli_sim_refuses_an_incomplete_checkpoint(
    view: HistoryView, capsys: pytest.CaptureFixture[str]
) -> None:
    """E7.9: once checkpoints.json exists, a run on a checkpoint it lacks is refused."""
    from arc.backtest.pull import checkpoints_path
    from arc.cli import main

    cp = checkpoints_path(view.data_dir)
    cp.write_text(json.dumps({"c500": {"complete_at": None, "sessions": 1, "gb": 0.0}}))
    try:
        rc = main(
            [
                "backtest",
                "sim",
                "--universe",
                "c125",
                "--from",
                DECIDE[0].isoformat(),
                "--to",
                DECIDE[1].isoformat(),
                "--profile",
                "cash_debit",
                "--data-dir",
                str(view.data_dir),
                "--out",
                str(view.data_dir / "refused"),
                "--no-cache",
            ]
        )
    finally:
        cp.unlink()
    assert rc == 3
    assert "checkpoint c125 is not complete" in capsys.readouterr().err
    assert not (view.data_dir / "refused" / "report.md").exists()


def test_report_constants_name_the_limits() -> None:
    assert "Discovery and Trending" in NOT_REPLAYED
    assert "check_earnings_blackout" in GATE_RULES_APPLIED
    assert "check_per_underlying" in GATE_RULES_APPLIED


def test_ranking_yaml_sim_section_is_not_runtime_tunable() -> None:
    from arc.backtest.ranking import load_ranking_file
    from arc.control.registry import NOT_EXPOSED_RANKING_SECTIONS

    s = load_ranking_file().backtest.sim
    assert s == SimSettings()  # yaml restates the model defaults
    assert s.equity == 25_000 and s.selection == "cross_name"
    assert "backtest.sim" in NOT_EXPOSED_RANKING_SECTIONS
