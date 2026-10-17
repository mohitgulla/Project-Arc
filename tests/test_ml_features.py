"""E27.1 feature store: no look-ahead, registry/schema, oi_missing, formula parity."""

from __future__ import annotations

import datetime as dt
from typing import Any

import numpy as np
import pandas as pd
import pytest

from arc.features.vol import iv_percentile, iv_rank
from arc.ml.features import registry as reg
from arc.ml.features.compute import (
    EARN_SRC_CALENDAR,
    EARN_SRC_NONE,
    EARN_SRC_PROJECTED,
    OPTION_INPUT_COLS,
    FeatureInputs,
    calendar_frame,
    compute_panel,
    earnings_frame,
    iv_rank_pct,
    portfolio_row,
    sessions_since_change,
    third_friday,
    ticker_option_frame,
    trailing_pct,
)
from arc.utils.calendar import sessions_between

TICKERS = ("AAA", "BBB", "CCC")
ALL = (*TICKERS, "SPY")


def synth_world(seed: int = 7, n: int = 330, end: dt.date = dt.date(2026, 9, 30)) -> FeatureInputs:
    """A deterministic synthetic world: OHLC + option rows + events + calendar."""
    rng = np.random.default_rng(seed)
    sess = sessions_between(end - dt.timedelta(days=int(n * 1.6)), end)[-n:]
    ohlc: dict[str, pd.DataFrame] = {}
    opt: dict[str, pd.DataFrame] = {}
    for k, t in enumerate(ALL):
        r = rng.normal(0.0004, 0.015 + 0.004 * k, n)
        close = 100.0 * (1 + 0.3 * k) * np.exp(np.cumsum(r))
        op = close * np.exp(rng.normal(0, 0.004, n))
        hi = np.maximum(op, close) * np.exp(np.abs(rng.normal(0, 0.006, n)))
        lo = np.minimum(op, close) * np.exp(-np.abs(rng.normal(0, 0.006, n)))
        ohlc[t] = pd.DataFrame({"open": op, "high": hi, "low": lo, "close": close}, index=sess)
        iv = 0.25 + 0.05 * k + 0.03 * np.sin(np.arange(n) / 17.0) + rng.normal(0, 0.005, n)
        f = pd.DataFrame(index=sess, columns=list(OPTION_INPUT_COLS), dtype=float)
        f["iv30"] = iv
        f["iv60"] = iv - 0.01
        f["front_iv"] = iv + 0.02
        f["term_slope"] = f["iv60"] - f["iv30"]
        f["front_over_iv30"] = f["front_iv"] / f["iv30"]
        f["put25_iv"] = iv + 0.03
        f["call25_iv"] = iv - 0.01
        f["skew_25d"] = f["put25_iv"] - f["call25_iv"]
        f["atm_spread_pct"] = rng.uniform(0.02, 0.1, n)
        f["two_sided_pct"] = rng.uniform(0.6, 0.95, n)
        f["oi_total"] = rng.uniform(1e4, 1e5, n)
        f["oi_near_atm"] = f["oi_total"] * 0.4
        f["opt_volume"] = rng.uniform(1e3, 5e4, n)
        f["put_call_oi"] = rng.uniform(0.5, 1.5, n)
        f["put_call_vol"] = rng.uniform(0.5, 1.5, n)
        f.iloc[::37, 0] = np.nan  # gaps in the IV series
        opt[t] = f
    events = pd.DataFrame(
        {
            "ticker": ["AAA", "AAA", "AAA", "BBB", "BBB", "CCC"],
            "event_date": [sess[40], sess[103], sess[166], sess[60], sess[300], sess[-2]],
            "realised_over_implied": [0.8, 1.3, 0.6, 1.1, 0.9, 1.4],
        }
    )
    upcoming = pd.DataFrame(
        {
            "ticker": ["BBB", "CCC", "AAA"],
            "reaction_date": [
                sess[320],
                sess[-1] + dt.timedelta(days=30),
                sess[-1] + dt.timedelta(days=9),
            ],
            "known_at": [sess[290], sess[310], sess[-2]],
        }
    )
    members = {
        sess[0]: frozenset({"AAA", "BBB"}),
        sess[150]: frozenset({"AAA", "BBB", "CCC"}),
        sess[290]: frozenset({"BBB", "CCC"}),
    }
    macro = {
        "fomc": [sess[30], sess[200], sess[315]],
        "cpi": [sess[i] for i in range(10, n, 21)],
        "nfp": [sess[i] for i in range(3, n, 21)],
    }
    return FeatureInputs(
        ohlc=ohlc,
        opt=opt,
        events=events,
        upcoming=upcoming,
        macro=macro,
        members=members,
        etfs=frozenset({"SPY"}),
        sessions=sessions_between(sess[0], end + dt.timedelta(days=200)),
    )


def _scale_after(df: pd.DataFrame, d: dt.date) -> pd.DataFrame:
    out = df.copy()
    late = np.array([x > d for x in out.index])
    out.loc[late] = out.loc[late] * 3.0
    return out


def mutate_after(inp: FeatureInputs, d: dt.date) -> FeatureInputs:
    """Every input fact dated after *d* x3 (prices, option rows, events, PIT membership,
    calendar rows known later). Scheduled macro dates and the session calendar are known in
    advance and stay."""
    ohlc = {t: _scale_after(df, d) for t, df in inp.ohlc.items()}
    opt = {t: _scale_after(df, d) for t, df in inp.opt.items()}
    ev = inp.events.copy()
    late = ev["event_date"] > d
    ev.loc[late, "realised_over_implied"] = ev.loc[late, "realised_over_implied"] * 3.0
    ev.loc[late, "event_date"] = [
        x - dt.timedelta(days=1) if x - dt.timedelta(days=1) > d else x
        for x in ev.loc[late, "event_date"]
    ]
    up = inp.upcoming.copy()
    unk = up["known_at"] > d
    up.loc[unk, "reaction_date"] = [d + dt.timedelta(days=3)] * int(unk.sum())
    members = {q: (frozenset({"CCC"}) if q > d else s) for q, s in inp.members.items()}
    return inp.model_copy(
        update={"ohlc": ohlc, "opt": opt, "events": ev, "upcoming": up, "members": members}
    )


@pytest.fixture(scope="module")
def world() -> FeatureInputs:
    return synth_world()


@pytest.fixture(scope="module")
def panel(world: FeatureInputs) -> pd.DataFrame:
    days = sorted(world.ohlc["SPY"].index)[-60:]
    return compute_panel(world, days, ALL)


GROUPS = [g for g in reg.PANEL_GROUPS]


@pytest.mark.parametrize("group", GROUPS)
@pytest.mark.parametrize("k", [-45, -20, -3])
def test_no_lookahead_per_group(
    world: FeatureInputs, panel: pd.DataFrame, group: str, k: int
) -> None:
    """Mutating every input row after d leaves every feature of the group at d unchanged."""
    days = sorted(world.ohlc["SPY"].index)
    d = days[k]
    cols = [f.name for f in reg.FEATURES if f.group == group]
    mutated = compute_panel(mutate_after(world, d), days[-60:], ALL)
    a = panel[panel["date"] == d].set_index("ticker")[cols]
    b = mutated[mutated["date"] == d].set_index("ticker")[cols]
    assert len(a) == len(ALL)
    pd.testing.assert_frame_equal(a, b, check_exact=False, rtol=0, atol=0)
    # the mutation is real: the last row changes (calendar inputs are all known ahead)
    later = days[-1]
    assert (
        not panel[panel["date"] == later]
        .set_index("ticker")[cols]
        .equals(mutated[mutated["date"] == later].set_index("ticker")[cols])
        or group == "calendar"
    )


def test_panel_shape_and_coverage(panel: pd.DataFrame) -> None:
    assert list(panel.columns[:4]) == ["date", "ticker", "pit_member", "is_etf"]
    assert list(panel.columns[4:]) == reg.panel_feature_names()
    assert len(panel) == 60 * len(ALL)
    last = panel[panel["date"] == panel["date"].max()]
    # 330 sessions cover every window: no NaN left in the price / market groups
    price = [f.name for f in reg.FEATURES if f.group == "ticker_price"]
    assert not last[price].isna().any().any()
    assert last.set_index("ticker").loc["SPY", "is_etf"]
    assert np.isnan(last.set_index("ticker").loc["SPY", "sess_to_earn"])


def test_pit_member_follows_quarters(world: FeatureInputs, panel: pd.DataFrame) -> None:
    last = panel[panel["date"] == panel["date"].max()].set_index("ticker")["pit_member"]
    assert last.to_dict() == {"AAA": False, "BBB": True, "CCC": True, "SPY": False}


def test_iv_rank_pct_matches_live_rule() -> None:
    rng = np.random.default_rng(3)
    idx = sessions_between(dt.date(2025, 1, 2), dt.date(2026, 6, 30))
    s = pd.Series(0.2 + rng.normal(0, 0.03, len(idx)), index=idx)
    s.iloc[::11] = np.nan
    rank, pct = iv_rank_pct(s)
    obs = s.dropna()
    for d in [idx[-1], idx[-50], idx[200]]:
        if pd.isna(s[d]):
            continue
        assert rank[d] == pytest.approx(iv_rank(obs, d), abs=1e-12)
        assert pct[d] == pytest.approx(iv_percentile(obs, d), abs=1e-12)
    assert rank.iloc[:100].isna().all()  # < 120 observations


def test_trailing_pct_and_flip_age() -> None:
    s = pd.Series([1.0, 2.0, 3.0, np.nan, 0.5])
    p = trailing_pct(s, window=4, min_obs=2)
    assert np.isnan(p[0]) and p[1] == 1.0 and p[2] == 1.0 and np.isnan(p[3]) and p[4] == 0.0
    lab = pd.Series([np.nan, 1.0, 1.0, 0.0, 0.0, 0.0, 1.0])
    age = sessions_since_change(lab, cap=2)
    assert np.isnan(age[1]) and np.isnan(age[2])  # run reaches the unknown start
    assert age[3] == 0.0 and age[4] == 1.0 and age[5] == 2.0 and age[6] == 0.0


def test_oi_missing_flag_not_stale() -> None:
    """Free-tier days (no OI) get oi_missing=1 and NaN OI, never yesterday's value."""
    idx = sessions_between(dt.date(2026, 10, 5), dt.date(2026, 10, 14))
    opt = pd.DataFrame(index=idx, columns=list(OPTION_INPUT_COLS), dtype=float)
    opt["oi_total"] = [5e4, 6e4, 6.5e4, 7e4, np.nan, 0.0, np.nan, np.nan][: len(idx)]
    opt["oi_near_atm"] = opt["oi_total"] * 0.5
    opt["put_call_oi"] = 0.9
    price = pd.DataFrame({"rv20": 0.2, "_har_rv": 0.2}, index=idx)
    out = ticker_option_frame(opt, price)
    known = opt["oi_total"] > 0
    assert (out.loc[known, "oi_missing"] == 0).all()
    assert (out.loc[~known, "oi_missing"] == 1).all()
    for c in ("oi_total", "oi_near_atm", "put_call_oi"):
        assert out.loc[~known, c].isna().all()
        assert out.loc[known, c].notna().all()
    spec = reg.registry()
    assert spec["oi_total"].null_policy == "missing_indicator"
    assert spec["oi_missing"].dtype == "bool"


def test_earn_src_semantics_shared() -> None:
    """History (projection, src 1) and live (Finnhub, src 2) count to the same reaction
    session; the calendar row counts only once known; ETFs have none."""
    sess = sessions_between(dt.date(2026, 1, 2), dt.date(2026, 12, 31))
    last = sess[20]
    ev = pd.DataFrame({"ticker": ["X"], "event_date": [last], "realised_over_implied": [1.2]})
    proj = sess[sess.index(next(s for s in sess if s >= last + dt.timedelta(days=91)))]
    days = [sess[60], sess[70]]
    empty = pd.DataFrame(columns=["ticker", "reaction_date", "known_at"])
    h = earnings_frame(days, ev, empty, sess, etf=False)
    up = pd.DataFrame({"ticker": ["X"], "reaction_date": [proj], "known_at": [sess[65]]})
    lv = earnings_frame(days, ev, up, sess, etf=False)
    assert h["earn_src"].tolist() == [EARN_SRC_PROJECTED] * 2
    assert lv["earn_src"].tolist() == [EARN_SRC_PROJECTED, EARN_SRC_CALENDAR]  # known at 65
    assert h["sess_to_earn"].tolist() == lv["sess_to_earn"].tolist()
    assert h.loc[sess[60], "sess_to_earn"] == sess.index(proj) - 60
    assert (h["sess_since_earn"] == [40, 50]).all()
    assert (h["last_earn_move_ratio"] == 1.2).all()
    etf = earnings_frame(days, ev, up, sess, etf=True)
    assert etf["earn_src"].tolist() == [EARN_SRC_NONE] * 2 and etf["sess_to_earn"].isna().all()


def test_calendar_frame() -> None:
    sess = sessions_between(dt.date(2026, 9, 1), dt.date(2026, 12, 31))
    assert third_friday(2026, 10) == dt.date(2026, 10, 16)
    days = [
        dt.date(2026, 10, 12),
        dt.date(2026, 10, 16),
        dt.date(2026, 10, 29),
        dt.date(2026, 10, 30),
    ]
    c = calendar_frame(days, {"fomc": [dt.date(2026, 10, 28)]}, sess)
    assert c["sess_to_opex"].tolist() == [4, 0, 16, 15]  # next OPEX 2026-11-20
    assert c["opex_week"].tolist() == [1.0, 1.0, 0.0, 0.0]
    assert c["month_end_3s"].tolist() == [0.0, 0.0, 1.0, 1.0]
    assert c["dow"].tolist() == [0.0, 4.0, 3.0, 4.0]
    assert c.loc[dt.date(2026, 10, 12), "sess_to_fomc"] == 12
    assert np.isnan(c.loc[dt.date(2026, 10, 29), "sess_to_fomc"])


def test_portfolio_row() -> None:
    idx = sessions_between(dt.date(2026, 6, 1), dt.date(2026, 9, 30))
    rng = np.random.default_rng(1)
    base = pd.Series(rng.normal(0, 0.01, len(idx)), index=idx)
    rets = {
        "A": base,
        "B": base + rng.normal(0, 0.001, len(idx)),
        "C": pd.Series(rng.normal(0, 0.01, len(idx)), index=idx),
    }
    book = pd.DataFrame(
        {
            "underlying": ["A", "B", "C"],
            "direction": ["bull", "bull", "bear"],
            "risk": [500.0, 300.0, 200.0],
        }
    )
    row = portfolio_row(
        ticker="A", direction="bull", book=book, equity=25_000, equity_peak=27_500,
        groups={"A": "tech", "B": "tech", "C": "energy"}, returns=rets, day=idx[-1],
    )  # fmt: skip
    assert row == {
        "open_positions": 3.0,
        "open_risk_pct": pytest.approx(0.04),
        "open_same_dir_share": pytest.approx(2 / 3),
        "same_name_open": 1.0,
        "same_cluster_open": 1.0,
        "same_sector_open": 2.0,
        "drawdown_from_peak": pytest.approx(25_000 / 27_500 - 1),
    }
    assert set(row) == {f.name for f in reg.FEATURES if f.group == "portfolio"}


# --- registry / schema --------------------------------------------------------------


def test_registry_unique_and_groups() -> None:
    r = reg.registry()
    assert len(r) == len(reg.FEATURES)
    assert {f.group for f in reg.FEATURES} == {
        "ticker_price", "ticker_options", "market", "calendar", "event", "portfolio", "candidate"
    }  # fmt: skip
    with pytest.raises(ValueError, match="extra"):
        reg.FeatureSpec.model_validate({**reg.FEATURES[0].model_dump(), "bogus": 1})


def test_default_model_features_exclude_research_only() -> None:
    names = reg.default_model_features()
    off = {f.name for f in reg.FEATURES if not f.available_live}
    assert off and not off & set(names)
    assert {"iv60", "oi_total", "breadth_ma50"} <= off
    assert "iv30" in names and "ret_1d" in names


def test_schema_version_changes_on_any_edit() -> None:
    base = reg.schema_version()
    assert base == reg.FEATURE_SCHEMA_VERSION and len(base) == 12
    specs = list(reg.FEATURES)
    specs[0] = specs[0].model_copy(update={"window": specs[0].window + 1})
    assert reg.schema_version(tuple(specs)) != base
    specs = list(reg.FEATURES)
    specs[-1] = specs[-1].model_copy(update={"description": specs[-1].description + "."})
    assert reg.schema_version(tuple(specs)) != base
    src = (reg.Path(reg.__file__).parent / "compute.py").read_bytes()
    assert reg.schema_version(compute_source=src) == base
    assert reg.schema_version(compute_source=src + b"\n# edit") != base
    reg.require_schema(base)
    with pytest.raises(reg.SchemaMismatchError):
        reg.require_schema("000000000000")


def test_cli_list(capsys: Any) -> None:
    from arc.cli import main

    assert main(["ml", "features", "list"]) == 0
    out = capsys.readouterr().out
    assert reg.schema_version() in out
    assert all(f.name in out for f in reg.FEATURES)
