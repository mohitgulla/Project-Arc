"""E16.2 (D76/D78): daily technicals on the regime entry + Research's ``tech …`` segment.

The reference values are recomputed here with plain loops (Wilder RSI/ATR, SMAs,
squeeze), independent of :mod:`arc.features.technicals`, on a committed fixture of
260 real NVDA daily bars (Alpaca SIP, 2025-09-29 .. 2026-10-09) plus SPY closes.
"""

from __future__ import annotations

import csv
import datetime as dt
import json
import math
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from arc.config import ArcSettings
from arc.context import ContextStore
from arc.context.kinds import KINDS, RegimePayload, validate_payload
from arc.data.base import HistoryBar
from arc.features._series import ohlc_from_bars, truncate_frame
from arc.features.snapshot import build_snapshot, build_snapshot_from_bars
from arc.features.technicals import (
    MIN_TECH_BARS,
    TechnicalFeatures,
    compute_technicals,
    squeeze_series,
    wilder_atr,
    wilder_rsi,
)
from arc.personas.builders import (
    ResearchInput,
    build_research_prompt,
    regime_line,
    research_input_from_context,
    tech_segment,
)
from arc.routines.config import RoutinesConfig, load_routines
from arc.routines.handlers import JobContext
from arc.store.db import connect
from arc.store.migrate import migrate
from arc.utils.calendar import ET

FIXTURES = Path(__file__).parent / "fixtures"
TECH = FIXTURES / "technicals"
AS_OF = dt.date(2026, 10, 9)


def _nvda() -> pd.DataFrame:
    with (TECH / "nvda_daily_ohlc.csv").open() as fh:
        rows = list(csv.DictReader(fh))
    return pd.DataFrame(
        {k: [float(r[k]) for r in rows] for k in ("open", "high", "low", "close")},
        index=[dt.date.fromisoformat(r["date"]) for r in rows],
    )


def _spy() -> pd.Series:
    with (TECH / "spy_daily_closes.csv").open() as fh:
        rows = list(csv.DictReader(fh))
    return pd.Series(
        [float(r["close"]) for r in rows],
        index=[dt.date.fromisoformat(r["date"]) for r in rows],
        dtype=float,
    )


def _frame(
    closes: list[float], *, spread: float = 0.01, start: dt.date | None = None
) -> pd.DataFrame:
    """OHLC from closes: open = previous close, high/low = close +/- spread * close."""
    day = start or dt.date(2025, 1, 2)
    days: list[dt.date] = []
    while len(days) < len(closes):
        if day.weekday() < 5:
            days.append(day)
        day += dt.timedelta(days=1)
    c = np.asarray(closes, dtype=float)
    o = np.concatenate([[c[0]], c[:-1]])
    hi = np.maximum(o, c) * (1 + spread)
    lo = np.minimum(o, c) * (1 - spread)
    return pd.DataFrame({"open": o, "high": hi, "low": lo, "close": c}, index=days)


# ---------------------------------------------------------------------------
# Plain-loop reference implementations (independent of the module under test)
# ---------------------------------------------------------------------------


def _ref_rsi(closes: list[float], n: int = 14) -> float:
    gains, losses = [], []
    for i in range(1, len(closes)):
        ch = closes[i] - closes[i - 1]
        gains.append(ch if ch > 0 else 0.0)
        losses.append(-ch if ch < 0 else 0.0)
    ag = sum(gains[:n]) / n
    al = sum(losses[:n]) / n
    for i in range(n, len(gains)):
        ag = (ag * (n - 1) + gains[i]) / n
        al = (al * (n - 1) + losses[i]) / n
    if al == 0:
        return 50.0 if ag == 0 else 100.0
    return 100 - 100 / (1 + ag / al)


def _ref_tr(h: list[float], lo: list[float], c: list[float]) -> list[float]:
    return [
        max(h[i] - lo[i], abs(h[i] - c[i - 1]), abs(lo[i] - c[i - 1])) for i in range(1, len(c))
    ]


def _ref_atr(h: list[float], lo: list[float], c: list[float], n: int = 14) -> float:
    tr = _ref_tr(h, lo, c)
    atr = sum(tr[:n]) / n
    for x in tr[n:]:
        atr = (atr * (n - 1) + x) / n
    return atr


def _ref_squeeze_on(h: list[float], lo: list[float], c: list[float], i: int) -> bool:
    w = c[i - 19 : i + 1]
    m = sum(w) / 20
    sd = math.sqrt(sum((x - m) ** 2 for x in w) / 20)
    tr = _ref_tr(h, lo, c)  # tr[k] belongs to bar k + 1
    atr20 = sum(tr[i - 20 : i]) / 20
    return 2 * sd < 1.5 * atr20


# ---------------------------------------------------------------------------
# Reference fixture
# ---------------------------------------------------------------------------


class TestReferenceFixture:
    def test_matches_plain_loop_reference(self) -> None:
        df, spy = _nvda(), _spy()
        h, lo, c, o = (df[k].tolist() for k in ("high", "low", "close", "open"))
        f = compute_technicals(df, AS_OF, iv30=0.40, benchmark=spy)
        assert f is not None and f.missing == []
        assert f.bar_date == AS_OF and f.close == c[-1] == 229.28
        assert f.sma20 == pytest.approx(sum(c[-20:]) / 20)
        assert f.sma50 == pytest.approx(sum(c[-50:]) / 50)
        assert f.sma200 == pytest.approx(sum(c[-200:]) / 200)
        assert f.pct_vs_sma50 == pytest.approx(c[-1] / (sum(c[-50:]) / 50) - 1)
        assert f.sma50_gt_sma200 is (sum(c[-50:]) / 50 > sum(c[-200:]) / 200)
        assert f.sma50_slope_20d == pytest.approx(sum(c[-50:]) / sum(c[-70:-20]) - 1)
        assert f.rsi14 == pytest.approx(_ref_rsi(c), abs=1e-9)
        atr = _ref_atr(h, lo, c)
        assert f.atr14 == pytest.approx(atr, rel=1e-12)
        assert f.atr14_pct == pytest.approx(atr / c[-1])
        assert f.stretch_atr == pytest.approx((c[-1] - sum(c[-20:]) / 20) / atr)
        assert f.high20 == max(h[-20:]) and f.low20 == min(lo[-20:])
        assert f.dist_high20_atr == pytest.approx((max(h[-20:]) - c[-1]) / atr)
        assert f.dist_low20_atr == pytest.approx((c[-1] - min(lo[-20:])) / atr)
        assert f.pct_from_52w_high == pytest.approx(c[-1] / max(h[-252:]) - 1)
        assert f.pct_from_52w_low == pytest.approx(c[-1] / min(lo[-252:]) - 1)
        assert f.ret5d == pytest.approx(c[-1] / c[-6] - 1)
        implied = c[-1] * 0.40 * math.sqrt(20 / 252)
        assert f.implied_move_20d == pytest.approx(implied)
        assert f.realised_atr_move_20d == pytest.approx(atr * math.sqrt(20))
        assert f.implied_vs_atr == pytest.approx(implied / (atr * math.sqrt(20)))
        s = spy.tolist()
        assert f.rs_spy_20d == pytest.approx((c[-1] / c[-21] - 1) - (s[-1] / s[-21] - 1))
        assert f.rs_spy_60d == pytest.approx((c[-1] / c[-61] - 1) - (s[-1] / s[-61] - 1))
        w = c[-20:]
        m = sum(w) / 20
        sd = math.sqrt(sum((x - m) ** 2 for x in w) / 20)
        assert f.bb_width20 == pytest.approx(4 * sd / m)
        days = 0
        for i in range(len(c) - 1, 19, -1):
            if not _ref_squeeze_on(h, lo, c, i):
                break
            days += 1
        assert f.squeeze_on is (days > 0) and f.squeeze_days == days
        assert (f.prev_high, f.prev_low, f.prev_close) == (h[-2], lo[-2], c[-2])
        assert f.gap_pct == pytest.approx(o[-1] / c[-2] - 1)
        assert f.close_vs_prev_range == "below"  # 229.28 < 2026-10-08 low 229.85

    def test_reference_values_pinned(self) -> None:
        """The same numbers the PR cross-checks against public sources (2026-10-09)."""
        f = compute_technicals(_nvda(), AS_OF)
        assert f is not None
        assert f.rsi14 == pytest.approx(52.96, abs=0.01)
        assert f.sma50 == pytest.approx(222.08, abs=0.01)
        assert f.atr14 == pytest.approx(5.687, abs=0.001)

    def test_squeeze_series_matches_reference_every_bar(self) -> None:
        df = _nvda()
        h, lo, c = (df[k].tolist() for k in ("high", "low", "close"))
        got = squeeze_series(*(df[k].to_numpy() for k in ("high", "low", "close")))
        assert not got[:20].any()
        assert [bool(x) for x in got[20:]] == [
            _ref_squeeze_on(h, lo, c, i) for i in range(20, len(c))
        ]
        assert got.any() and not got.all()  # real data has both states


# ---------------------------------------------------------------------------
# Walk-forward safety and bounds (hypothesis)
# ---------------------------------------------------------------------------

_closes = st.lists(
    st.floats(min_value=1.0, max_value=1000.0, allow_nan=False, allow_infinity=False),
    min_size=MIN_TECH_BARS + 1,
    max_size=260,
)


@settings(max_examples=60, deadline=None)
@given(
    closes=_closes,
    cut=st.integers(min_value=MIN_TECH_BARS, max_value=259),
    junk=st.lists(st.floats(min_value=0.5, max_value=5000.0), min_size=1, max_size=30),
)
def test_bars_after_as_of_change_nothing(closes: list[float], cut: int, junk: list[float]) -> None:
    cut = min(cut, len(closes) - 1)
    df = _frame(closes)
    as_of = df.index[cut - 1]
    base = compute_technicals(df, as_of, iv30=0.3, benchmark=df["close"] * 1.01)
    # mutate every bar after as_of and append more
    tail = _frame([*closes[:cut], *junk, *junk], spread=0.07)
    mutated = pd.concat([df.iloc[:cut], tail.iloc[cut:]])
    bench = pd.concat([df["close"].iloc[:cut] * 1.01, tail["close"].iloc[cut:] * 3])
    again = compute_technicals(mutated, as_of, iv30=0.3, benchmark=bench)
    assert base is not None and again is not None
    assert base.model_dump() == again.model_dump()


@settings(max_examples=80, deadline=None)
@given(closes=_closes, spread=st.floats(min_value=0.0, max_value=0.1))
def test_bounds(closes: list[float], spread: float) -> None:
    df = _frame(closes, spread=spread)
    f = compute_technicals(df, df.index[-1])
    assert f is not None
    assert f.rsi14 is not None and 0.0 <= f.rsi14 <= 100.0
    assert f.atr14 is not None and f.atr14 >= 0.0
    if len(closes) > 20:  # noqa: PLR2004 - the squeeze needs 21 bars
        assert f.squeeze_days is not None and f.squeeze_days >= 0
        assert (f.squeeze_days > 0) is bool(f.squeeze_on)
    else:
        assert f.squeeze_on is None and f.squeeze_days is None
    if f.high20 is not None:
        assert f.low20 is not None and f.low20 <= f.close * (1 + spread) and f.high20 >= f.low20


def test_flat_series_rsi_50_atr_0_no_divide_by_zero() -> None:
    df = _frame([50.0] * 60, spread=0.0)
    f = compute_technicals(df, df.index[-1], iv30=0.2)
    assert f is not None
    assert f.rsi14 == 50.0 and f.atr14 == 0.0
    assert f.stretch_atr is None and f.dist_high20_atr is None and f.dist_low20_atr is None
    assert f.implied_vs_atr is None and f.realised_atr_move_20d == 0.0
    assert f.squeeze_on is False and f.squeeze_days == 0  # 0 < 0 is false: no band at all
    assert f.bb_width20 == 0.0 and f.gap_pct == 0.0 and f.close_vs_prev_range == "inside"
    assert any("ATR14 is 0" in m for m in f.missing)


def test_rsi_extremes_and_short_inputs() -> None:
    up = np.arange(1.0, 30.0)
    assert wilder_rsi(up) == 100.0
    assert wilder_rsi(up[::-1]) == 0.0
    assert wilder_rsi(up[:14]) is None
    assert wilder_atr(up[:14], up[:14], up[:14]) is None


def test_short_history_fields_are_none_never_fabricated() -> None:
    df = _frame([100 + i for i in range(30)])
    f = compute_technicals(df, df.index[-1])
    assert f is not None
    assert f.sma20 is not None and f.sma50 is None and f.sma200 is None
    assert f.sma50_gt_sma200 is None and f.sma50_slope_20d is None
    assert f.pct_from_52w_high is None and f.rs_spy_20d is None and f.rs_spy_60d is None
    assert f.implied_move_20d is None and f.implied_vs_atr is None
    joined = " | ".join(f.missing)
    for key in ("sma50", "sma200", "52w range", "no iv30", "no benchmark"):
        assert key in joined
    assert compute_technicals(df.iloc[: MIN_TECH_BARS - 1], df.index[-1]) is None
    assert compute_technicals(df, df.index[0] - dt.timedelta(days=1)) is None


# ---------------------------------------------------------------------------
# Squeeze, relative strength, prior-day levels
# ---------------------------------------------------------------------------


def test_squeeze_turns_on_in_compression_and_fires_on_expansion() -> None:
    # 40 trending bars (2% a day: the Bollinger band is wide), then 25 bars where the
    # close barely moves but the true range stays 4% wide (band inside Keltner), then
    # a 25% breakout bar that throws the band back outside.
    wide = [100 * 1.02**i for i in range(40)]
    base = wide[-1]
    tight = [base + (0.05 if i % 2 else -0.05) for i in range(25)]
    closes = [*wide, *tight, base * 1.25]
    c = np.asarray(closes)
    h = c * 1.02
    lo = c * 0.98
    days = _frame(closes).index
    df = pd.DataFrame({"open": c, "high": h, "low": lo, "close": c}, index=days)
    on = squeeze_series(h, lo, c)
    assert not on[:41].any()  # trending: off
    assert on[64]  # compression: on
    during = compute_technicals(df, days[64])
    assert during is not None and during.squeeze_on is True
    assert during.squeeze_days is not None and during.squeeze_days >= 5
    assert during.squeeze_days == int(on[:65][::-1].argmin())  # consecutive run length
    fired = compute_technicals(df, days[65])
    assert fired is not None and fired.squeeze_on is False and fired.squeeze_days == 0
    assert fired.close_vs_prev_range == "above"


def test_relative_strength_sign_vs_spy() -> None:
    spy = _frame([100 * 1.001**i for i in range(80)])["close"]
    strong = _frame([100 * 1.004**i for i in range(80)])
    weak = _frame([100 * 0.998**i for i in range(80)])
    fs = compute_technicals(strong, strong.index[-1], benchmark=spy)
    fw = compute_technicals(weak, weak.index[-1], benchmark=spy)
    assert fs is not None and fw is not None
    assert fs.rs_spy_20d is not None and fs.rs_spy_20d > 0 and fs.rs_spy_60d > 0  # type: ignore[operator]
    assert fw.rs_spy_20d is not None and fw.rs_spy_20d < 0 and fw.rs_spy_60d < 0  # type: ignore[operator]
    assert fs.rs_spy_20d == pytest.approx((1.004**20 - 1) - (1.001**20 - 1))
    # vs itself: zero; a benchmark with no bar on as_of: None (never a stale match)
    same = compute_technicals(strong, strong.index[-1], benchmark=strong["close"])
    assert same is not None and same.rs_spy_20d == pytest.approx(0.0)
    lagged = compute_technicals(strong, strong.index[-1], benchmark=spy.iloc[:-1])
    assert lagged is not None and lagged.rs_spy_20d is None


def test_sector_rs_only_with_an_etf() -> None:
    etf = _frame([100 * 1.002**i for i in range(40)])["close"]
    df = _frame([100 * 1.003**i for i in range(40)])
    f = compute_technicals(df, df.index[-1], sector=etf, sector_etf="XLK")
    assert f is not None and f.sector_etf == "XLK"
    assert f.rs_sector_20d == pytest.approx((1.003**20 - 1) - (1.002**20 - 1))
    g = compute_technicals(df, df.index[-1], sector=etf)  # no ETF name -> unmapped
    assert g is not None and g.rs_sector_20d is None and g.sector_etf is None


@pytest.mark.parametrize(
    ("close", "label"), [(105.0, "above"), (95.0, "below"), (101.0, "inside"), (104.0, "inside")]
)
def test_prior_day_range_label_and_gap(close: float, label: str) -> None:
    df = _frame([100.0] * 20)
    days = list(df.index)
    df.loc[days[-2], ["open", "high", "low", "close"]] = [100.0, 104.0, 96.0, 100.0]
    df.loc[days[-1], ["open", "high", "low", "close"]] = [
        102.0,
        max(close, 102.0),
        min(close, 102.0),
        close,
    ]
    f = compute_technicals(df, days[-1])
    assert f is not None
    assert (f.prev_high, f.prev_low, f.prev_close) == (104.0, 96.0, 100.0)
    assert f.gap_pct == pytest.approx(0.02)
    assert f.close_vs_prev_range == label


def test_truncate_and_bar_helpers() -> None:
    ts = dt.datetime(2026, 10, 9, 4, tzinfo=dt.UTC)
    bars = [
        HistoryBar(timestamp=ts, open=1, high=2, low=0.5, close=1.5, volume=1),
        HistoryBar(timestamp=ts - dt.timedelta(days=1), open=1, high=1, low=1, close=1, volume=1),
        HistoryBar(timestamp=ts - dt.timedelta(days=2), open=0, high=1, low=1, close=1, volume=1),
    ]
    frame = ohlc_from_bars(bars)
    assert list(frame.index) == [dt.date(2026, 10, 8), dt.date(2026, 10, 9)]  # 0 open dropped
    assert truncate_frame(frame, dt.date(2026, 10, 8)).index.tolist() == [dt.date(2026, 10, 8)]
    assert ohlc_from_bars([]).empty and truncate_frame(ohlc_from_bars([]), AS_OF).empty


# ---------------------------------------------------------------------------
# Snapshot + context kind v4
# ---------------------------------------------------------------------------


def _bars_from(df: pd.DataFrame) -> list[HistoryBar]:
    return [
        HistoryBar(
            timestamp=dt.datetime.combine(d, dt.time(4), tzinfo=dt.UTC),
            open=r.open,
            high=r.high,
            low=r.low,
            close=r.close,
            volume=1.0,
        )
        for d, r in df.iterrows()
    ]


class TestSnapshot:
    def test_from_bars_carries_technicals_with_the_snapshot_iv(self) -> None:
        df = _nvda()
        snap = build_snapshot_from_bars(
            "nvda", _bars_from(df), AS_OF, current_iv=0.41, benchmark=_spy()
        )
        assert snap.technicals is not None and snap.vol.iv == 0.41
        assert snap.technicals.implied_move_20d == pytest.approx(
            229.28 * 0.41 * math.sqrt(20 / 252)
        )
        assert snap.technicals.rs_spy_20d is not None
        assert not any(w.startswith("technicals") for w in snap.warnings)

    def test_short_bars_warn_and_closes_only_has_none(self) -> None:
        df = _frame([100.0 + i for i in range(10)])
        snap = build_snapshot_from_bars("X", _bars_from(df), df.index[-1])
        assert snap.technicals is None
        assert f"technicals: need {MIN_TECH_BARS} OHLC bars" in snap.warnings
        assert build_snapshot("X", df["close"], df.index[-1]).technicals is None

    def test_regime_kind_v4_round_trip_and_old_rows_load(self) -> None:
        assert KINDS["regime"].schema_version == 4
        snap = build_snapshot_from_bars("NVDA", _bars_from(_nvda()), AS_OF, benchmark=_spy())
        stored = json.loads(
            json.dumps(RegimePayload.model_validate(snap.model_dump()).model_dump(mode="json"))
        )
        back = validate_payload("regime", stored)
        assert isinstance(back, RegimePayload) and back.technicals == snap.technicals
        assert back.model_dump(mode="json") == stored
        # a v2 row (pre-E17.1) and a v3 row (no technicals) still validate
        v2 = json.loads((FIXTURES / "regime" / "stored_regime_v2_spy.json").read_text())
        assert validate_payload("regime", v2).technicals is None  # type: ignore[attr-defined]
        v3 = build_snapshot("SPY", _spy(), AS_OF).model_dump(mode="json")
        v3.pop("technicals")
        assert validate_payload("regime", v3).technicals is None  # type: ignore[attr-defined]

    def test_extra_fields_rejected(self) -> None:
        f = compute_technicals(_nvda(), AS_OF)
        assert f is not None
        with pytest.raises(ValueError, match="Extra inputs"):
            TechnicalFeatures.model_validate({**f.model_dump(), "macd": 1.0})


# ---------------------------------------------------------------------------
# Research rendering
# ---------------------------------------------------------------------------


def _payload() -> dict[str, Any]:
    snap = build_snapshot_from_bars(
        "NVDA", _bars_from(_nvda()), AS_OF, current_iv=0.41, benchmark=_spy()
    )
    return snap.model_dump(mode="json")


class TestResearchSegment:
    def test_segment_format_and_length(self) -> None:
        seg = tech_segment(_payload()["technicals"])
        assert seg == (
            "tech rsi 53 · +0.4 ATR vs 20d · 50>200 · 5.8% off 20d hi · RS20 +3.2% vs SPY"
            " · < y-lo · impl/ATR 1.0"
        )
        assert len(seg) <= 130

    def test_segment_example_parts_and_drops(self) -> None:
        tech = {
            "rsi14": 71.2, "stretch_atr": 2.31, "sma50_gt_sma200": True, "close": 98.9,
            "high20": 100.0, "rs_spy_20d": 0.041, "squeeze_on": True, "squeeze_days": 6,
            "close_vs_prev_range": "above", "implied_vs_atr": 1.31,
        }  # fmt: skip
        assert tech_segment(tech) == (
            "tech rsi 71 · +2.3 ATR vs 20d · 50>200 · 1.1% off 20d hi · RS20 +4.1% vs SPY"
            " · squeeze 6d · > y-hi · impl/ATR 1.3"
        )
        assert tech_segment({**tech, "close": 100.0}).count("at 20d hi") == 1
        assert tech_segment({"rsi14": None, "sma50_gt_sma200": False}) == "tech 50<200"
        assert tech_segment({"squeeze_on": False, "squeeze_days": 0}) == ""
        assert tech_segment({"stretch_atr": -0.036, "rs_spy_20d": -0.0001}) == (
            "tech +0.0 ATR vs 20d · RS20 +0.0% vs SPY"  # never "-0.0"
        )
        assert tech_segment(None) == "" and tech_segment({}) == ""

    def test_regime_line_off_is_byte_identical_on_appends(self) -> None:
        p = _payload()
        without = {k: v for k, v in p.items() if k != "technicals"}
        assert regime_line("NVDA", p) == regime_line("NVDA", without)
        on = regime_line("NVDA", p, technicals=True)
        assert on == regime_line("NVDA", p) + " · " + tech_segment(p["technicals"])
        assert regime_line("NVDA", without, technicals=True) == regime_line("NVDA", without)


def _research_ctx(conn: Any, payloads: dict[str, dict[str, Any]]) -> Any:
    now = dt.datetime(2026, 10, 9, 10, 0, tzinfo=ET)
    store = ContextStore(conn)
    for t, p in payloads.items():
        store.write(
            kind="regime",
            subject=t,
            payload=RegimePayload.model_validate(p),
            produced_by="test",
            run_id="r",
            now=now,
        )
    return store.snapshot(now)


def test_research_prompt_off_byte_identical_on_has_segment() -> None:
    conn = connect(":memory:")
    migrate(conn)
    p = _payload()
    old = {k: v for k, v in p.items() if k != "technicals"}
    kw: dict[str, Any] = {
        "portfolio_summary": "Equity $25,000.",
        "scan_date": "2026-10-09",
        "compact": True,
    }
    snap_new = _research_ctx(conn, {"NVDA": p})
    conn2 = connect(":memory:")
    migrate(conn2)
    snap_old = _research_ctx(conn2, {"NVDA": old})
    off = build_research_prompt(research_input_from_context(snap_new, **kw))
    before = build_research_prompt(research_input_from_context(snap_old, **kw))
    assert off == before  # flag off: the stored technicals change nothing
    assert "tech " not in off
    on_inp = research_input_from_context(snap_new, **kw, technicals=True)
    assert isinstance(on_inp, ResearchInput) and on_inp.technicals
    on = build_research_prompt(on_inp)
    assert "tech = daily chart facts computed by code" in on
    assert tech_segment(p["technicals"]) in on
    assert on.replace(tech_segment(p["technicals"]), "") != off  # legend + segment only


# ---------------------------------------------------------------------------
# Config switch + registry
# ---------------------------------------------------------------------------


def test_shipped_switch_is_on_and_off_is_the_rollback() -> None:
    from arc.control.registry import lookup

    r = load_routines()
    assert r.research_technicals.enabled is True  # D78 ships on
    off = load_routines(overrides={("personas", "research_technicals"): "off"})
    assert off.research_technicals.enabled is False
    t = lookup("personas.research_technicals")
    assert lookup("research_technicals") is t and t.choices == ("off", "on")


def test_sector_etf_map_lives_in_sectors_yaml(tmp_path: Path) -> None:
    """E20.2 (D85): config/sectors.yaml `sector_etf:` is the one source."""
    from arc.pipeline.portfolio_context import load_sector_etfs

    etfs = load_sector_etfs()
    assert etfs["technology"] == "XLK" and len(etfs) == 10
    assert "technicals" not in RoutinesConfig.model_fields
    bad = tmp_path / "s.yaml"
    bad.write_text("sectors: {technology: [AAPL]}\nsector_etf: {tech: XLK}\n")
    with pytest.raises(ValueError, match="sector_etf"):
        load_sector_etfs(bad)
    bad.write_text("sectors: {technology: [AAPL]}\nsector_etf: {technology: not a ticker}\n")
    with pytest.raises(ValueError, match="sector_etf"):
        load_sector_etfs(bad)


# ---------------------------------------------------------------------------
# The regime step: bars fetched once, SPY + sector ETF reused, technicals stored
# ---------------------------------------------------------------------------


class _Market:
    def __init__(self, fail: frozenset[str] = frozenset()) -> None:
        self.calls: list[str] = []
        self.fail = fail
        self.frames = {
            "NVDA": _nvda(),
            "AAPL": _nvda() * 1.3,
            "SPY": pd.DataFrame({k: _spy() for k in ("open", "high", "low", "close")}),
            "XLK": _nvda() * 0.9,
        }

    def history_bars(self, symbol, start, end, timeframe="1Day"):  # noqa: ANN001, ANN201
        self.calls.append(symbol)
        if symbol in self.fail:
            raise RuntimeError("bars down")
        return _bars_from(self.frames[symbol])

    def option_chain(self, underlying, start, end):  # noqa: ANN001, ANN201
        raise RuntimeError("no chains in this test")

    def underlying_quote(self, symbol):  # noqa: ANN001, ANN201
        raise RuntimeError("no quotes in this test")


def _step(conn: Any, market: _Market, tickers: list[str]) -> tuple[list[str], Any]:
    from arc.pipeline.env import PipelineEnv
    from arc.pipeline.steps import _regime_entries

    now = dt.datetime(2026, 10, 9, 10, 0, tzinfo=ET)
    routines = RoutinesConfig.model_validate(
        {
            "sources": {
                "iv.record": {
                    "schedule": ["15:50"],
                    "writes": ["regime"],
                    "category": "options_slow",
                    "tickers": ["AAPL"],
                }
            },
        }
    )
    kind, spec = routines.step("iv.record")
    ctx = JobContext(
        job="iv.record",
        kind=kind,
        spec=spec,
        run_id="run-1",
        chain_run_id=None,
        scheduled_for=now,
        now=now,
        conn=conn,
        snapshot=ContextStore(conn).snapshot(now),
        routines=routines,
        settings_factory=lambda: ArcSettings(regime_model="v1"),  # type: ignore[call-arg]
    )
    env = PipelineEnv(
        market=market,  # type: ignore[arg-type]
        account=lambda: None,  # type: ignore[arg-type,return-value]
        positions=list,
        llms={},
    )
    written = _regime_entries(ctx, env, tickers)
    return written, ContextStore(conn).snapshot(now)


def test_regime_step_stores_technicals_and_fetches_refs_once() -> None:
    conn = connect(":memory:")
    migrate(conn)
    m = _Market()
    written, snap = _step(conn, m, ["AAPL", "NVDA", "SPY"])
    assert written == ["AAPL", "NVDA", "SPY"]
    assert sorted(m.calls) == ["AAPL", "NVDA", "SPY", "XLK"]  # SPY + XLK once each
    nv = snap.latest("regime", "NVDA").payload["technicals"]
    assert nv["sector_etf"] == "XLK" and nv["rs_sector_20d"] is not None
    assert nv["rs_spy_20d"] == pytest.approx(
        compute_technicals(_nvda(), AS_OF, benchmark=_spy()).rs_spy_20d  # type: ignore[union-attr]
    )
    spy = snap.latest("regime", "SPY").payload["technicals"]
    assert spy["rs_spy_20d"] is None and spy["sector_etf"] is None  # no RS vs itself


def test_regime_step_reference_failure_only_drops_rs() -> None:
    conn = connect(":memory:")
    migrate(conn)
    written, snap = _step(conn, _Market(fail=frozenset({"SPY", "XLK"})), ["NVDA"])
    assert written == ["NVDA"]
    t = snap.latest("regime", "NVDA").payload["technicals"]
    assert t["rs_spy_20d"] is None and t["rs_sector_20d"] is None and t["rsi14"] is not None


# ---------------------------------------------------------------------------
# Audit trail: technicals frozen with the proposal's MarketContext
# ---------------------------------------------------------------------------


def test_market_context_freezes_technicals_for_the_trail() -> None:
    from arc.approvals.trail import _features
    from arc.journal.models import MarketContext

    p = _payload()
    mc = MarketContext(
        proposal_hash="h",
        subject="NVDA",
        at=dt.datetime(2026, 10, 9, 10, tzinfo=ET),
        technicals=p["technicals"],
    )
    back = MarketContext.model_validate_json(mc.model_dump_json())
    assert back.technicals is not None and back.technicals.rsi14 == p["technicals"]["rsi14"]
    feats = _features(back)
    assert feats is not None and feats["technicals"]["rsi14"] == p["technicals"]["rsi14"]
    old = MarketContext(proposal_hash="h", subject="NVDA", at=mc.at)  # pre-E16.2 row
    assert _features(old)["technicals"] is None  # type: ignore[index]
