"""E7.18: loss & drawdown anatomy scripts (feature PIT contract, time-ordered CV, maths)."""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import loss_anatomy_analysis as la  # noqa: E402
from scripts import loss_anatomy_features as lf  # noqa: E402
from scripts.loss_anatomy import trade_key  # noqa: E402

D0 = dt.date(2021, 1, 4)


def _sessions(n: int, start: dt.date = dt.date(2020, 1, 2)) -> list[dt.date]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += dt.timedelta(days=1)
    return out


def _px(days: list[dt.date], seed: int, drift: float = 0.0003) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    c = 100 * np.exp(np.cumsum(rng.normal(drift, 0.012, len(days))))
    o = c * (1 + rng.normal(0, 0.004, len(days)))
    return pd.DataFrame({"open": o, "high": c * 1.01, "low": c * 0.99, "close": c}, index=days)


def _opt(days: list[dt.date], seed: int) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    df = pd.DataFrame(index=days)
    for c in lf.OPTION_COLS:
        df[c] = rng.uniform(0.05, 0.6, len(days))
    df["d9_pass"] = rng.random(len(days)) > 0.3
    return df


def _trades(days: list[dt.date]) -> pd.DataFrame:
    rows = []
    for i, (u, k, d) in enumerate(
        [
            ("AAA", "bull_put", days[300]),
            ("BBB", "bear_call", days[300]),
            ("AAA", "iron_condor", days[320]),
            ("SPY", "long_call", days[340]),
            ("BBB", "bull_put", days[345]),
        ]
    ):
        exp = d + dt.timedelta(days=40)
        rows.append(
            {
                "underlying": u,
                "kind": k,
                "spec": f"{k}_d{20 + i}_w2_dte30-45",
                "entry_date": d,
                "exit_date": d + dt.timedelta(days=14),
                "exit_day": d + dt.timedelta(days=14),
                "expiration": exp,
                "contracts": 2,
                "pnl": [-300.0, 120.0, 80.0, -50.0, 40.0][i],
                "pnl_mid": [-250.0, 140.0, 100.0, -30.0, 60.0][i],
                "pnl_unit": [-150.0, 60.0, 40.0, -25.0, 20.0][i],
                "max_loss_unit": 200.0,
                "entry_net": -0.9,
                "entry_net_mid": -1.0,
                "fees": 2.0,
                "exit_reason": "stop" if i == 0 else "take_profit",
                "credit_width": 2.0 if k != "long_call" else np.nan,
                "debit_width": np.nan if k != "long_call" else 1.0,
                "etf": u == "SPY",
                "risk": 400.0,
                "direction": {"bull_put": "bullish", "bear_call": "bearish"}.get(k, "neutral"),
                "legs": f"+{u}{i}",
                "class": "ETFs" if u == "SPY" else "Stocks",
                "year": d.year,
                "win": [False, True, True, False, True][i],
            }
        )
    return pd.DataFrame(rows)


@pytest.fixture
def world() -> dict[str, object]:
    days = _sessions(420)
    prices = {
        t: _px(days, s)
        for t, s in (("AAA", 1), ("BBB", 2), ("SPY", 3), ("HYG", 4), ("TLT", 5), ("XLK", 6))
    }
    opt = {t: _opt(days, s) for t, s in (("AAA", 7), ("BBB", 8), ("SPY", 9))}
    earnings = pd.DataFrame(
        {
            "ticker": ["AAA", "AAA", "BBB"],
            "event_date": [days[250], days[310], days[200]],
            "implied_move": [0.05, 0.06, 0.04],
            "realised_move": [0.02, 0.09, 0.03],
        }
    )
    macro = {"fomc": [days[301], days[380]], "cpi": [days[330]], "nfp": [days[346]]}
    equity = pd.Series(25_000.0 + np.arange(len(days)), index=days)
    return {
        "days": days,
        "prices": prices,
        "opt": opt,
        "earnings": earnings,
        "macro": macro,
        "equity": equity,
        "trades": _trades(days),
    }


def _build(w: dict[str, object]) -> pd.DataFrame:
    prices, opt = w["prices"], w["opt"]
    market = lf.market_state(prices, opt, ["AAA", "BBB", "SPY"])  # type: ignore[arg-type]
    return lf.build_features(
        w["trades"],  # type: ignore[arg-type]
        w["equity"],  # type: ignore[arg-type]
        prices=prices,  # type: ignore[arg-type]
        opt=opt,  # type: ignore[arg-type]
        market=market,
        earnings=w["earnings"],  # type: ignore[arg-type]
        macro=w["macro"],  # type: ignore[arg-type]
        groups={"AAA": "technology", "BBB": "technology", "SPY": "etf:broad_equity"},
    )


ENTRY_TIME_PREFIXES = ("px_", "opt_", "mkt_")
ENTRY_TIME_COLS = (
    "days_since_earn",
    "days_to_earn_proj",
    "earn_proj_in_window",
    "fomc_in_window",
    "cpi_in_window",
    "nfp_in_window",
    "fomc_within_2s",
    "cpi_within_2s",
    "nfp_within_2s",
    "open_positions",
    "open_same_dir_share",
    "same_name_open",
    "same_group_open",
    "same_cluster_open",
    "drawdown_at_entry",
)


def _mutate_after(df: pd.DataFrame, d: dt.date) -> pd.DataFrame:
    """Every row dated after *d* scrambled (numbers scaled/shifted, booleans flipped)."""
    out = df.copy()
    late = np.array([x > d for x in df.index])
    for c in out.columns:
        if out[c].dtype == bool:
            out.loc[late, c] = ~out.loc[late, c]
        else:
            out.loc[late, c] = out.loc[late, c] * -3.7 + 11.0
    return out


def _entry_cols(f: pd.DataFrame) -> list[str]:
    return [c for c in f.columns if c.startswith(ENTRY_TIME_PREFIXES) or c in ENTRY_TIME_COLS]


def test_feature_row_is_point_in_time(world: dict[str, object]) -> None:
    """Mutating every price/option/market/earnings row after a trade's entry session
    leaves that trade's entry-time feature row unchanged (no look-ahead)."""
    base = _build(world)
    cols = _entry_cols(base)
    assert len(cols) > 40
    trades: pd.DataFrame = world["trades"]  # type: ignore[assignment]
    for i, r in trades.iterrows():
        d = r["entry_date"]
        w2 = dict(world)
        w2["prices"] = {k: _mutate_after(v, d) for k, v in world["prices"].items()}  # type: ignore[union-attr]
        w2["opt"] = {k: _mutate_after(v, d) for k, v in world["opt"].items()}  # type: ignore[union-attr]
        ev: pd.DataFrame = world["earnings"].copy()  # type: ignore[union-attr]
        ev = pd.concat(
            [
                ev,
                pd.DataFrame(
                    {
                        "ticker": [r["underlying"]],
                        "event_date": [d + dt.timedelta(days=3)],
                        "implied_move": [0.5],
                        "realised_move": [0.9],
                    }
                ),
            ],
            ignore_index=True,
        )
        w2["earnings"] = ev
        w2["equity"] = world["equity"].where(  # type: ignore[union-attr]
            world["equity"].index.map(lambda x, d=d: x < d),
            -1.0,  # type: ignore[union-attr]
        )
        got = _build(w2)
        pd.testing.assert_series_equal(
            base.loc[i, cols].astype(object), got.loc[i, cols].astype(object), check_names=False
        )


def test_post_columns_do_change_when_future_changes(world: dict[str, object]) -> None:
    """Control: the ``post_*`` hold descriptors are the ones allowed to see the future."""
    base = _build(world)
    w2 = dict(world)
    t: pd.DataFrame = world["trades"]  # type: ignore[assignment]
    ev = pd.concat(
        [
            world["earnings"],
            pd.DataFrame(
                {
                    "ticker": ["BBB"],
                    "event_date": [t.loc[1, "entry_date"] + dt.timedelta(days=2)],  # type: ignore[list-item]
                    "implied_move": [0.1],
                    "realised_move": [0.2],
                }
            ),
        ],
        ignore_index=True,
    )
    w2["earnings"] = ev
    got = _build(w2)
    assert not bool(base.loc[1, "post_earn_in_hold"])
    assert bool(got.loc[1, "post_earn_in_hold"])


def test_event_and_portfolio_columns(world: dict[str, object]) -> None:
    f = _build(world)
    days: list[dt.date] = world["days"]  # type: ignore[assignment]
    # FOMC at days[301] is one session after the days[300] entries
    assert bool(f.loc[0, "fomc_within_2s"]) and bool(f.loc[0, "fomc_in_window"])
    assert not bool(f.loc[2, "fomc_within_2s"])
    # AAA earnings at days[250] → 50 sessions before the days[300] entry
    assert f.loc[0, "days_since_earn"] == (days[300] - days[250]).days
    assert pd.isna(f.loc[3, "days_since_earn"])  # ETF
    # trade 2 (AAA, days[320]) sees trades 0 and 1 open (exit +14 cal days ≈ 10 sessions? no)
    assert f.loc[0, "open_positions"] == 0
    assert f.loc[0, "new_risk_today_pct"] == pytest.approx(800.0 / 25_299.0)
    assert f.loc[4, "same_group_open"] >= 0
    assert set(f["mkt_spy_trend"]) <= {"up", "down", "mixed", "unknown"}


def test_trade_key_matches_analysis_key(world: dict[str, object]) -> None:
    t: pd.DataFrame = world["trades"]  # type: ignore[assignment]
    r = t.iloc[0]
    assert trade_key(r["underlying"], r["entry_date"], r["legs"]) == f"AAA|{r['entry_date']}|+AAA0"


@pytest.mark.parametrize(
    ("name", "label"),
    [
        ("ProShares UltraPro QQQ", "leveraged"),
        ("Direxion Daily Semiconductor Bull 3X Shares", "leveraged"),
        ("SPDR Gold Trust", "commodity"),
        ("iShares 20+ Year Treasury Bond ETF", "bond"),
        ("iShares MSCI Emerging Markets ETF", "international"),
        ("Energy Select Sector SPDR Fund", "sector"),
        ("ProShares VIX Short-Term Futures ETF", "vol"),
        ("SPDR S&P 500 ETF Trust", "broad_equity"),
    ],
)
def test_etf_class(name: str, label: str) -> None:
    assert lf.etf_class("X", name) == label


def test_sic_sector() -> None:
    assert lf.sic_sector(3571) == "technology"
    assert lf.sic_sector(2834) == "health"
    assert lf.sic_sector(6021) == "financials_realestate"
    assert lf.sic_sector(None) == "unknown"


def test_time_folds_are_time_ordered() -> None:
    """Train strictly on earlier years whose trades also exited before the test year."""
    rng = np.random.default_rng(0)
    entries = [dt.date(2020, 1, 1) + dt.timedelta(days=int(x)) for x in rng.integers(0, 1460, 2000)]
    exits = pd.Series([d + dt.timedelta(days=30) for d in entries])
    years = pd.Series([d.year for d in entries])
    folds = la.time_folds(years, exits)
    assert [y for _, _, y in folds] == [2021, 2022, 2023]
    for tr, te, y in folds:
        assert max(years.iloc[tr]) < y
        assert max(exits.iloc[tr]) < dt.date(y, 1, 1)
        assert set(years.iloc[te]) == {y}
        assert list(tr) == sorted(tr)


def test_episodes() -> None:
    days = _sessions(10)
    e = pd.Series([100, 110, 95, 90, 99, 111, 112, 100, 98, 99.0], index=days)
    ep = la.episodes(e, min_depth=0.10)
    assert len(ep) == 2
    first = ep.sort_values("peak").iloc[0]
    assert first["peak"] == days[1] and first["trough"] == days[3] and first["recovered"] == days[5]
    assert first["depth_pct"] == pytest.approx(18.2, abs=0.05)
    assert ep.sort_values("peak").iloc[1]["recovered"] is None


def test_slides_split_one_long_episode() -> None:
    days = _sessions(9)
    # never regains 100: one episode, but two separable legs (100→80, 92→70)
    e = pd.Series([100, 90, 80, 85, 92, 85, 70, 75, 76.0], index=days)
    assert len(la.episodes(e)) == 1
    s = la.slides(e)
    assert [(r.peak, r.trough) for r in s.itertuples()] == [(days[0], days[2]), (days[4], days[6])]
    assert s["loss_usd"].tolist() == [20.0, 22.0]
    assert s["recovered"].isna().all()


def test_trade_values_rebuild_equity() -> None:
    days = _sessions(6)
    t = pd.DataFrame(
        {
            "trade": ["a", "b"],
            "contracts": [2, 1],
            "exit_day": [days[3], days[5]],
            "pnl": [-50.0, 30.0],
        }
    )
    paths = pd.DataFrame(
        {
            "trade": ["a", "a", "b"],
            "day": [days[1], days[2], days[2]],
            "mark_unit": [-5.0, -20.0, 10.0],
        }
    )
    v = la.trade_values(t, paths, days)
    assert v["0"].tolist() == [0.0, -10.0, -40.0, -50.0, -50.0, -50.0]
    assert v["1"].tolist() == [0.0, 0.0, 10.0, 10.0, 10.0, 30.0]
    eq = la.rebuild_equity(v)
    assert eq.iloc[-1] == pytest.approx(25_000 - 20.0)
    assert la.rebuild_equity(v, [False, True]).iloc[-1] == pytest.approx(25_030.0)


def test_path_stats_gap_through() -> None:
    t = pd.DataFrame(
        {
            "trade": ["a", "b"],
            "max_loss_unit": [100.0, 100.0],
            "R": [-0.9, 0.5],
            "kind": ["bull_put", "bull_put"],
            "exit_reason": ["stop", "take_profit"],
            "pnl": [-90.0, 50.0],
            "contracts": [1, 1],
            "days_held": [5, 7],
        }
    )
    paths = pd.DataFrame(
        {
            "trade": ["a", "a", "b", "b"],
            "day": [1, 2, 1, 2],
            "mark_unit": [35.0, -10.0, -55.0, 20.0],
        }
    )
    ps = la.path_stats(t, paths)
    assert ps.loc[0, "ever_p03R"] and ps.loc[0, "last_mark_R"] == pytest.approx(-0.1)
    assert ps.loc[1, "first_m05R_session"] == 1
    assert ps.loc[0, "mae_R"] == pytest.approx(-0.9)
    tabs = la.path_tables(t.assign(year=2021, win=t["R"] > 0), ps)
    row = tabs["path_by_kind"].iloc[0]
    assert row["stops_gap_through"] == 1 and row["stops_were_p03R_first"] == 1.0


def test_quintile_spread_detects_signal() -> None:
    rng = np.random.default_rng(3)
    n = 600
    x = rng.normal(size=n)
    days = _sessions(n)
    t = pd.DataFrame(
        {
            "f": x,
            "R": 0.4 * x + rng.normal(0, 0.3, n),
            "entry_date": days,
            "pnl": x * 10,
            "risk": 100.0,
        }
    )
    t["loser_05R"] = t["R"] <= -0.5
    r = la.quintile_spread(t, "f", n_boot=200)
    assert r is not None and r["ci_excludes_0"] and r["q5_minus_q1_R"] > 0.5


def test_guard_table_baseline_and_block() -> None:
    days = _sessions(4)
    t = pd.DataFrame(
        {"pnl": [-100.0, 50.0], "risk": [200.0, 200.0], "R": [-0.5, 0.25], "x": [1, 0]},
        index=[0, 1],
    )
    v = pd.DataFrame({"0": [0, -50, -100, -100.0], "1": [0, 20, 50, 50.0]}, index=days)
    g = la.guard_table(t, v, {"no_x": lambda d: d["x"] == 1})
    assert g.loc[0, "net"] == -50 and g.loc[1, "net"] == 50 and g.loc[1, "blocked"] == 1
    assert g.loc[1, "blocked_mean_R"] == -0.5 and g.loc[1, "sum_R"] == 0.2


def test_classifier_time_ordered_runs() -> None:
    pytest.importorskip("sklearn")
    rng = np.random.default_rng(5)
    n = 900
    entries = [dt.date(2020, 1, 2) + dt.timedelta(days=int(i * 1400 / n)) for i in range(n)]
    t = pd.DataFrame(
        {c: rng.normal(size=n) for c in la.NUMERIC[:6]}
        | {
            "kind": rng.choice(["bull_put", "bear_call"], n),
            "year": [d.year for d in entries],
            "exit_day": [d + dt.timedelta(days=20) for d in entries],
        }
    )
    t["loser_05R"] = (t[la.NUMERIC[0]] + rng.normal(0, 0.5, n)) > 0.8
    out = la.classifier(t, top=3)
    pooled = out["auc"].iloc[-1]
    assert pooled["test_year"] == "pooled" and pooled["auc"] > 0.7
    assert out["importance"].iloc[0]["feature"] == la.NUMERIC[0]
