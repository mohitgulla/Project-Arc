"""E16.5 (PLAN D76): breakeven in ATR terms per structure + the optional realism filter."""

from __future__ import annotations

import datetime as dt
import json
import math
from types import SimpleNamespace
from typing import Any

import pandas as pd
import pytest
from hypothesis import given
from hypothesis import strategies as st

from arc.journal.analytics import be_atr_multiple, debit_direction, directional_breakeven
from arc.journal.reasons import REASON_LABELS, ReasonCode
from arc.pipeline.analytics import regime_atr14
from arc.scanner.be_atr import (
    ScannerFilters,
    filter_menu,
    load_scanner_filters,
    structure_be_atr,
)
from arc.structures import credit_vertical, debit_vertical, iron_condor, long_call, long_put

AS_OF = dt.date(2026, 10, 9)
EXP = AS_OF + dt.timedelta(days=25)


# ---------------------------------------------------------------------------
# Math
# ---------------------------------------------------------------------------


def test_hand_computed_units() -> None:
    # spot 100, BE 105, ATR 2, 25 DTE -> 5 / (2 x 5) = 0.5
    assert be_atr_multiple(105.0, 100.0, 2.0, 25) == pytest.approx(0.5)
    assert be_atr_multiple(95.0, 100.0, 2.0, 25) == pytest.approx(0.5)  # distance, unsigned


@pytest.mark.parametrize(
    ("atr", "dte", "spot"), [(None, 25, 100.0), (0.0, 25, 100.0), (-1.0, 25, 100.0),
                             (float("nan"), 25, 100.0), (2.0, 0, 100.0), (2.0, 25, 0.0)]
)  # fmt: skip
def test_degenerate_inputs_are_none(atr: float | None, dte: int, spot: float) -> None:
    assert be_atr_multiple(105.0, spot, atr, dte) is None


@given(
    be=st.floats(1, 1000),
    spot=st.floats(1, 1000),
    atr=st.floats(0.01, 50),
    dte=st.integers(1, 400),
    k=st.floats(0.1, 10),
)
def test_scales_inversely_with_atr(be: float, spot: float, atr: float, dte: int, k: float) -> None:
    a = be_atr_multiple(be, spot, atr, dte)
    b = be_atr_multiple(be, spot, atr * k, dte)
    assert a is not None and b is not None and a >= 0
    assert a == pytest.approx(b * k, rel=1e-9, abs=1e-12)
    assert a == pytest.approx(abs(be - spot) / (atr * math.sqrt(dte)), rel=1e-12)


def test_direction_choice() -> None:
    assert debit_direction(1.8, ["call"]) == 1
    assert debit_direction(2.0, ["put"]) == -1
    assert debit_direction(-0.5, ["put"]) == 0  # credit
    assert debit_direction(1.0, ["call", "put"]) == 0  # straddle-like: no direction
    assert debit_direction(1.0, []) == 0
    assert directional_breakeven([95.0, 105.0], 1) == 105.0
    assert directional_breakeven([95.0, 105.0], -1) == 95.0
    assert directional_breakeven([95.0, 105.0], 0) is None
    assert directional_breakeven([], 1) is None


# ---------------------------------------------------------------------------
# Structures (direction per kind)
# ---------------------------------------------------------------------------


def test_long_call_uses_its_breakeven() -> None:
    s = long_call("XYZ", EXP, 100, "5.00", as_of=AS_OF)  # BE 105
    assert structure_be_atr(s, 100.0, 2.0) == pytest.approx(0.5)


def test_long_put_uses_its_breakeven() -> None:
    s = long_put("XYZ", EXP, 100, "5.00", as_of=AS_OF)  # BE 95
    assert structure_be_atr(s, 100.0, 2.0) == pytest.approx(0.5)


def test_bull_call_and_bear_put_debit_verticals() -> None:
    bull = debit_vertical(
        "c", "XYZ", EXP, long_strike=100, long_premium="3.00",
        short_strike=105, short_premium="1.20", as_of=AS_OF,
    )  # fmt: skip  # BE 101.80
    bear = debit_vertical(
        "p", "XYZ", EXP, long_strike=105, long_premium="3.50",
        short_strike=100, short_premium="1.50", as_of=AS_OF,
    )  # fmt: skip  # BE 103
    assert structure_be_atr(bull, 100.0, 2.0) == pytest.approx(1.8 / 10)
    assert structure_be_atr(bear, 100.0, 2.0) == pytest.approx(3.0 / 10)


def test_credit_structures_have_no_directional_value() -> None:
    cv = credit_vertical(
        "p", "XYZ", EXP, short_strike=95, short_premium="1.50",
        long_strike=90, long_premium="0.50", as_of=AS_OF,
    )  # fmt: skip
    ic = iron_condor(
        "XYZ", EXP, long_put_strike=85, long_put_premium="0.30", short_put_strike=90,
        short_put_premium="0.90", short_call_strike=110, short_call_premium="0.90",
        long_call_strike=115, long_call_premium="0.30", as_of=AS_OF,
    )  # fmt: skip
    assert structure_be_atr(cv, 100.0, 2.0) is None
    assert structure_be_atr(ic, 100.0, 2.0) is None


def test_missing_atr_is_none() -> None:
    s = long_call("XYZ", EXP, 100, "5.00", as_of=AS_OF)
    assert structure_be_atr(s, 100.0, None) is None


# ---------------------------------------------------------------------------
# Filter on / off
# ---------------------------------------------------------------------------


def _cand(structure: Any, name: str) -> SimpleNamespace:
    return SimpleNamespace(
        structure=structure, strategy=SimpleNamespace(value=name), expiration=EXP, name=name
    )


def _menu() -> list[SimpleNamespace]:
    near = long_call("XYZ", EXP, 100, "2.00", as_of=AS_OF)  # BE 102 -> 0.2
    far = long_call("XYZ", EXP, 110, "2.00", as_of=AS_OF)  # BE 112 -> 1.2
    cred = credit_vertical(
        "c", "XYZ", EXP, short_strike=130, short_premium="0.50",
        long_strike=135, long_premium="0.10", as_of=AS_OF,
    )  # fmt: skip
    return [_cand(far, "far"), _cand(cred, "credit"), _cand(near, "near")]


def test_filter_off_keeps_everything_in_order() -> None:
    m = _menu()
    res = filter_menu(m, spot=100.0, atr14=2.0, max_be_atr=None)
    assert res.kept == m and not res.dropped


def test_filter_on_drops_unrealistic_debits_only() -> None:
    m = _menu()
    res = filter_menu(m, spot=100.0, atr14=2.0, max_be_atr=1.0)
    assert [c.name for c in res.kept] == ["credit", "near"]
    (row,) = res.dropped_rows()
    assert row["be_atr"] == pytest.approx(1.2) and row["breakevens"] == [112.0]
    assert row["strategy"] == "far"


def test_filter_without_atr_drops_nothing() -> None:
    m = _menu()
    res = filter_menu(m, spot=100.0, atr14=None, max_be_atr=0.5)
    assert res.kept == m and not res.dropped


def test_regime_atr14() -> None:
    assert regime_atr14({"technicals": {"atr14": 4.2}}) == 4.2
    assert regime_atr14({"technicals": {"atr14": None}}) is None
    assert regime_atr14({"technicals": {"atr14": 0}}) is None
    assert regime_atr14({"technicals": None}) is None
    assert regime_atr14(None) is None


# ---------------------------------------------------------------------------
# Config, registry, reason codes
# ---------------------------------------------------------------------------


def test_ranking_yaml_ships_off() -> None:
    from arc.backtest.ranking import load_ranking_file

    assert load_scanner_filters().max_be_atr is None
    cfg = load_ranking_file()
    assert cfg.scanner.max_be_atr is None and cfg.backtest.max_be_atr is None


@pytest.mark.parametrize("bad", [0.4, 5.1])
def test_bounds(bad: float) -> None:
    with pytest.raises(ValueError):
        ScannerFilters(max_be_atr=bad)
    assert load_scanner_filters(overrides={("scanner", "max_be_atr"): 1.5}).max_be_atr == 1.5


def test_registry_entry() -> None:
    from arc.control.registry import Risk, Target, ValueType, lookup

    t = lookup("scanner.max_be_atr")
    assert t.target is Target.RANKING and t.path == ("scanner", "max_be_atr")
    assert t.type is ValueType.FLOAT_OR_NONE and (t.min, t.max) == (0.5, 5.0)
    assert t.risk is Risk.UP


def test_reason_labels_exhaustive() -> None:
    label = REASON_LABELS[ReasonCode.BE_UNREALISTIC]
    assert label == "Dropped: breakeven needs an unrealistic move"
    assert set(REASON_LABELS) == set(ReasonCode)


# ---------------------------------------------------------------------------
# Pipeline (fixture run): off = byte-identical prompt; on = drops + journal
# ---------------------------------------------------------------------------

CHAINS_MARK = "### Option chains with Greeks\n"


def _run(monkeypatch: pytest.MonkeyPatch, max_be: float | None, profile: str = "margin") -> Any:
    import arc.pipeline.steps as steps
    from arc.config import ArcSettings
    from arc.routines.config import load_routines
    from tests import test_pipeline as tp

    if max_be is not None:
        on = ScannerFilters(max_be_atr=max_be)
        monkeypatch.setattr(steps, "scanner_filters", lambda _settings=None: on)
    monkeypatch.delenv("ARC_GATE_SECRET", raising=False)
    s = ArcSettings(_env_file=None, account_profile=profile)  # type: ignore[call-arg]
    return tp._recording_fixture_run(s, load_routines())


def _chains(prompt: str) -> dict[str, Any]:
    return json.loads(prompt.split(CHAINS_MARK, 1)[1].split("\n\n### ", 1)[0])


@pytest.mark.parametrize("profile", ["margin", "cash_debit"])
def test_flag_off_menu_has_no_be_atr(profile: str, monkeypatch: pytest.MonkeyPatch) -> None:
    _, _, prompts = _run(monkeypatch, None, profile)
    for chain in _chains(prompts["quant"]).values():
        for row in chain["menu"]:
            assert "be_atr" not in row


def test_flag_on_adds_be_atr_and_journals_drops(monkeypatch: pytest.MonkeyPatch) -> None:
    _, _, loose = _run(monkeypatch, 5.0, "cash_debit")
    chains = _chains(loose["quant"])
    vals = [r.get("be_atr") for c in chains.values() for r in c["menu"]]
    assert any(v is not None for v in vals)
    # a tight limit drops the debits above it (journaled), never anything at/below
    lim = 0.6
    conn, _, tight = _run(monkeypatch, lim, "cash_debit")
    for c in _chains(tight["quant"]).values():
        for r in c["menu"]:
            assert r.get("be_atr") is None or r["be_atr"] <= lim + 0.005
    rows = conn.execute(
        "select subject, payload from decisions where reason_code='be_unrealistic'"
    ).fetchall()
    above = [v for v in vals if v is not None and v > lim]
    assert above and rows  # fixture NVDA long calls sit at ~0.55-0.79 ATR√t
    for r in rows:
        p = json.loads(r["payload"])
        assert p["max_be_atr"] == lim and all(d["be_atr"] > lim for d in p["dropped"])


# ---------------------------------------------------------------------------
# Backtest filter
# ---------------------------------------------------------------------------


def test_backtest_atr14_days_rescales_split_adjusted_ohlc() -> None:
    from arc.backtest.entry_filter import atr14_days

    days = pd.bdate_range("2026-01-01", periods=30).date
    ohlc = pd.DataFrame(
        {"open": 50.0, "high": 51.0, "low": 49.0, "close": 50.0, "volume": 1e6},
        index=pd.Index(days, name="date"),
    )
    closes = pd.Series(100.0, index=list(days))  # raw = 2x adjusted (a 2:1 split later)
    out = atr14_days(ohlc, closes, [days[-1], days[2]])
    assert days[2] not in out  # too few bars
    assert out[days[-1]] == pytest.approx(4.0)  # 2.0 adjusted ATR x 2


def test_backtest_apply_be_filter() -> None:
    from arc.backtest.entry_filter import apply_be_filter

    d = AS_OF
    closes = pd.Series([100.0], index=[d])
    far = SimpleNamespace(kind="long_call", name="far")
    near = SimpleNamespace(kind="long_call", name="near")
    cred = SimpleNamespace(kind="credit_vertical", name="credit")
    vals = {"far": 1.2, "near": 0.2}

    import arc.backtest.entry_filter as ef

    orig = ef.candidate_be_atr
    try:
        ef.candidate_be_atr = lambda c, _s, _a: vals.get(c.name)  # type: ignore[assignment]
        menus = {d: [far, cred, near]}
        out, n = apply_be_filter(menus, closes, {d: 2.0}, 1.0)  # type: ignore[arg-type]
        assert [c.name for c in out[d]] == ["credit", "near"] and n == 1
        out, n = apply_be_filter(menus, closes, {}, 1.0)  # type: ignore[arg-type]
        assert out[d] == menus[d] and n == 0  # no ATR: nothing dropped
    finally:
        ef.candidate_be_atr = orig  # type: ignore[assignment]
