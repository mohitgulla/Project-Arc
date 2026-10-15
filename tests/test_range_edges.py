"""E23.1 (D98): the range-edge entry filter for sideways regimes.

Pure rule (``range_position``, ``is_mid_range``, monotone in pos, bears mirror bulls),
config + registry, journal labels, the ``rng`` tech part, and the Research step: off =
byte-identical to main (pinned sha), on = a mid-range idea is dropped and journaled.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
import yaml
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from arc.control.registry import REGISTRY, lookup, read_raw, write_raw
from arc.features.technicals import (
    RangeEdgeRule,
    TechnicalFeatures,
    is_mid_range,
    range_position,
)
from arc.journal.reasons import REASON_LABELS, ReasonCode
from arc.personas.builders import regime_line, tech_segment
from arc.routines.config import RangeEdgesSettings, RoutinesConfig, load_routines
from arc.routines.headline import BLOCKER_LABELS

FIXTURES = Path(__file__).parent / "fixtures"
HL = RangeEdgeRule(levels="high_low")
VA = RangeEdgeRule()  # value_area (default)


def _tech(
    close: float = 100.0,
    low20: float | None = 90.0,
    high20: float | None = 110.0,
    atr: float | None = 2.0,
    val: float | None = None,
    vah: float | None = None,
    vp_pos: float | None = None,
    rp: float | None | str = "auto",
) -> TechnicalFeatures:
    pos = range_position(close, low20, high20) if rp == "auto" else rp
    return TechnicalFeatures(
        as_of=dt.date(2026, 10, 9),
        bar_date=dt.date(2026, 10, 9),
        close=close,
        low20=low20,
        high20=high20,
        atr14=atr,
        vp_val=val,
        vp_vah=vah,
        vp_pos=vp_pos,
        range_pos_20d=pos,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# range_position
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("close", "low", "high", "expected"),
    [
        (90.0, 90.0, 110.0, 0.0),  # at the low
        (110.0, 90.0, 110.0, 1.0),  # at the high
        (100.0, 90.0, 110.0, 0.5),
        (95.0, 90.0, 110.0, 0.25),
        (80.0, 90.0, 110.0, 0.0),  # clipped below
        (120.0, 90.0, 110.0, 1.0),  # clipped above
        (100.0, 100.0, 100.0, None),  # equal high/low
        (100.0, 110.0, 90.0, None),  # inverted
        (None, 90.0, 110.0, None),
        (100.0, None, 110.0, None),
        (100.0, 90.0, None, None),
        (float("nan"), 90.0, 110.0, None),
        (100.0, 90.0, float("inf"), None),
    ],
)
def test_range_position(
    close: float | None, low: float | None, high: float | None, expected: float | None
) -> None:
    got = range_position(close, low, high)
    assert got == (None if expected is None else pytest.approx(expected))


def test_compute_technicals_sets_range_pos_20d() -> None:
    from arc.features.technicals import compute_technicals
    from tests.test_features_technicals import AS_OF, _nvda

    f = compute_technicals(_nvda(), AS_OF)
    assert f is not None and f.low20 is not None and f.high20 is not None
    assert f.range_pos_20d == pytest.approx((f.close - f.low20) / (f.high20 - f.low20))
    with pytest.raises(ValidationError):
        _tech(rp=1.5)  # the stored field is clipped to [0, 1]


# ---------------------------------------------------------------------------
# is_mid_range: each branch
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("stance", "close", "expected"),
    [
        # high_low, range 90..110 (10 ATR wide): bull keep <= 0.35, bear keep >= 0.65
        ("bullish", 90.0, "ok"),  # at the low
        ("bullish", 97.0, "ok"),  # 0.35, at the threshold
        ("bullish", 97.2, "mid_range"),  # 0.36
        ("bullish", 100.0, "mid_range"),
        ("bullish", 109.0, "mid_range"),  # wrong edge: dropped too (intended)
        ("bearish", 110.0, "ok"),
        ("bearish", 103.0, "ok"),  # 0.65, at the threshold
        ("bearish", 102.8, "mid_range"),
        ("bearish", 91.0, "mid_range"),  # wrong edge
        ("Bullish ", 92.0, "ok"),  # stance normalised
    ],
)
def test_high_low_truth_table(stance: str, close: float, expected: str) -> None:
    v = is_mid_range(_tech(close), "sideways", stance, HL)
    assert v.status == expected
    assert v.mid_range is (expected == "mid_range")
    assert v.levels == "high_low" and v.width_atr == pytest.approx(10.0)


@pytest.mark.parametrize("regime", ["bull", "bear", None, "", "?"])
def test_non_sideways_regimes_are_untouched(regime: str | None) -> None:
    v = is_mid_range(_tech(100.0), regime, "bullish", HL)
    assert v.status == "not_applicable" and not v.mid_range


@pytest.mark.parametrize("stance", ["neutral", "", "long vol"])
def test_neutral_is_untouched(stance: str) -> None:
    assert is_mid_range(_tech(100.0), "sideways", stance, HL).status == "not_applicable"


def test_regimes_list_is_configurable() -> None:
    rule = RangeEdgeRule(levels="high_low", regimes=("sideways", "bull"))
    assert is_mid_range(_tech(100.0), "bull", "bullish", rule).mid_range
    assert is_mid_range(_tech(100.0), "Sideways", "bullish", rule).mid_range


def test_missing_data_never_drops() -> None:
    v = is_mid_range(None, "sideways", "bullish", HL)
    assert v.status == "missing" and v.missing == ["technicals"]
    assert v.text() == "technicals missing: technicals"
    v = is_mid_range(_tech(low20=None, high20=None), "sideways", "bullish", HL)
    assert v.status == "missing" and v.missing == ["range_pos_20d"]
    v = is_mid_range(_tech(atr=None), "sideways", "bearish", HL)
    assert v.status == "missing" and v.missing == ["atr14"]
    v = is_mid_range(_tech(atr=0.0), "sideways", "bearish", HL)
    assert v.status == "missing" and v.missing == ["atr14"]


def test_pre_e231_entry_without_range_pos_is_computed_from_low_high() -> None:
    v = is_mid_range(_tech(100.0, rp=None), "sideways", "bullish", HL)
    assert v.status == "mid_range" and v.pos == pytest.approx(0.5)


def test_too_narrow_range_keeps_the_idea() -> None:
    # 99..101 = 1 ATR wide < 1.5: the range is noise, rule skipped
    v = is_mid_range(_tech(100.0, 99.0, 101.0), "sideways", "bullish", HL)
    assert v.status == "too_narrow" and not v.mid_range
    assert v.width_atr == pytest.approx(1.0)
    assert v.text() == "20-day range 99.00–101.00 is 1.00 ATR wide (< 1.5): rule skipped"
    # exactly at the minimum applies the rule
    assert is_mid_range(_tech(100.0, 98.5, 101.5), "sideways", "bullish", HL).mid_range


def test_value_area_uses_vp_pos_unclipped_outside_counts_as_the_edge() -> None:
    # VA 96..104 (4 ATR): close 100 -> 0.5 mid
    v = is_mid_range(_tech(100.0, val=96.0, vah=104.0, vp_pos=0.5), "sideways", "bullish", VA)
    assert v.status == "mid_range" and v.levels == "value_area" and not v.vp_missing
    assert v.text() == "mid-range: 0.50 of the value area 96.00–104.00 (keep ≤ 0.35)"
    # below VAL (pos < 0) is the low edge for a bull; above VAH is the top edge for a bear
    v = is_mid_range(_tech(94.0, val=96.0, vah=104.0, vp_pos=-0.25), "sideways", "bullish", VA)
    assert v.status == "ok" and v.pos == -0.25
    v = is_mid_range(_tech(106.0, val=96.0, vah=104.0, vp_pos=1.25), "sideways", "bearish", VA)
    assert v.status == "ok" and v.text().startswith("at the edge: 1.25 of the value area")
    # a bear below VAL is the wrong edge: dropped
    assert is_mid_range(
        _tech(94.0, val=96.0, vah=104.0, vp_pos=-0.25), "sideways", "bearish", VA
    ).mid_range
    # vp_pos missing but VA present: computed from close
    v = is_mid_range(_tech(97.0, val=96.0, vah=104.0), "sideways", "bullish", VA)
    assert v.pos == pytest.approx(0.125) and v.status == "ok"
    # the width in use is VAH - VAL: 2 ATR VA... 1 ATR VA is too narrow
    v = is_mid_range(_tech(100.0, val=99.0, vah=101.0, vp_pos=0.5), "sideways", "bullish", VA)
    assert v.status == "too_narrow" and v.width_atr == pytest.approx(1.0)


def test_value_area_missing_falls_back_to_high_low() -> None:
    v = is_mid_range(_tech(100.0), "sideways", "bullish", VA)
    assert v.status == "mid_range" and v.levels == "high_low" and v.vp_missing
    v = is_mid_range(_tech(92.0, val=100.0, vah=100.0), "sideways", "bullish", VA)  # empty VA
    assert v.status == "ok" and v.levels == "high_low" and v.vp_missing


def test_verdict_text_and_roundtrip() -> None:
    v = is_mid_range(_tech(100.0), "sideways", "bearish", HL)
    assert v.text() == "mid-range: 0.50 of the 20-day range 90.00–110.00 (keep ≥ 0.65)"
    back = json.loads(json.dumps(v.model_dump(mode="json")))
    assert back["status"] == "mid_range" and back["rule"]["bear_min_pos"] == 0.65
    assert is_mid_range(_tech(), "bull", "bullish", HL).text().startswith("rule not applicable")


@given(
    pos=st.floats(-0.5, 1.5),
    other=st.floats(-0.5, 1.5),
    levels=st.sampled_from(["value_area", "high_low"]),
)
def test_monotone_in_pos_and_bears_mirror_bulls(pos: float, other: float, levels: str) -> None:
    rule = RangeEdgeRule(levels=levels)  # type: ignore[arg-type]

    def tech(p: float) -> TechnicalFeatures:
        if levels == "value_area":
            return _tech(96.0 + 8.0 * p, val=96.0, vah=104.0, vp_pos=p)
        q = min(1.0, max(0.0, p))
        return _tech(90.0 + 20.0 * q)

    lo, hi = sorted((pos, other))
    # bulls: a lower position is never more mid-range than a higher one
    if is_mid_range(tech(lo), "sideways", "bullish", rule).mid_range:
        assert is_mid_range(tech(hi), "sideways", "bullish", rule).mid_range
    # bears: a higher position is never more mid-range than a lower one
    if is_mid_range(tech(hi), "sideways", "bearish", rule).mid_range:
        assert is_mid_range(tech(lo), "sideways", "bearish", rule).mid_range
    # mirror: bull at p == bear at 1 - p
    mirror = RangeEdgeRule(levels=levels, bull_max_pos=0.35, bear_min_pos=0.65)  # type: ignore[arg-type]
    b = is_mid_range(tech(pos), "sideways", "bullish", mirror)
    m = is_mid_range(tech(1.0 - pos), "sideways", "bearish", mirror)
    if levels == "value_area":
        assert b.status == m.status


# ---------------------------------------------------------------------------
# Config, registry, labels, tech segment
# ---------------------------------------------------------------------------


def test_shipped_config_is_on_with_the_d98_defaults() -> None:
    cfg = load_routines().range_edges
    assert cfg.enabled is True  # D86/D98: ships on, off = rollback
    assert (cfg.regimes, cfg.levels) == (("sideways",), "value_area")
    assert (cfg.bull_max_pos, cfg.bear_min_pos, cfg.min_range_atr) == (0.35, 0.65, 1.5)
    assert cfg.rule() == RangeEdgeRule()
    assert RangeEdgesSettings().enabled is False  # absent block = off


@pytest.mark.parametrize(
    "data",
    [
        {"range_edges": {"enabled": "maybe"}},
        {"range_edges": {"bull_max_pos": 0.6}},
        {"range_edges": {"bear_min_pos": 0.4}},
        {"range_edges": {"levels": "poc"}},
        {"range_edges": {"regimes": ["choppy"]}},
        {"range_edges": {"bogus": 1}},
    ],
)
def test_bad_config_is_refused(data: dict[str, Any]) -> None:
    from arc.routines.strategy import StrategyConfig

    with pytest.raises((ValidationError, ValueError)):
        StrategyConfig.model_validate(data)
    with pytest.raises((ValidationError, ValueError)):
        RoutinesConfig.model_validate(data)


def test_routines_yaml_refuses_the_block(tmp_path: Path) -> None:
    p = tmp_path / "routines.yaml"
    p.write_text("timezone: America/New_York\nrange_edges:\n  enabled: on\n")
    with pytest.raises(ValueError, match=r"moved to config/strategy\.yaml"):
        load_routines(p)


def test_registry_keys_read_and_write_the_yaml() -> None:
    raw = yaml.safe_load(open("config/strategy.yaml").read())  # noqa: SIM115, PTH123
    assert lookup("range_edges").key == "personas.range_edges"  # alias
    assert read_raw(REGISTRY["personas.range_edges"], raw) == "on"
    assert write_raw(REGISTRY["personas.range_edges"], "off", raw) == [
        (("range_edges", "enabled"), False)
    ]
    assert read_raw(REGISTRY["range_edges.levels"], raw) == "value_area"
    assert read_raw(REGISTRY["range_edges.bull_max_pos"], raw) == 0.35
    assert read_raw(REGISTRY["range_edges.bear_min_pos"], raw) == 0.65
    assert read_raw(REGISTRY["range_edges.min_range_atr"], raw) == 1.5
    keys = {k for k in REGISTRY if "range_edges" in k}
    assert keys == {
        "personas.range_edges",
        "range_edges.levels",
        "range_edges.bull_max_pos",
        "range_edges.bear_min_pos",
        "range_edges.min_range_atr",
    }
    assert (REGISTRY["range_edges.bull_max_pos"].min, REGISTRY["range_edges.bull_max_pos"].max) == (
        0.1,
        0.5,
    )
    assert (REGISTRY["range_edges.bear_min_pos"].min, REGISTRY["range_edges.bear_min_pos"].max) == (
        0.5,
        0.9,
    )
    t = REGISTRY["range_edges.min_range_atr"]
    assert (t.min, t.max) == (0.5, 5.0)


def test_reason_codes_have_plain_labels() -> None:
    assert ReasonCode.MID_RANGE_ENTRY.value == "mid_range_entry"
    assert REASON_LABELS[ReasonCode.MID_RANGE_ENTRY] == (
        "Skipped: mid-range entry in a sideways market"
    )
    assert ReasonCode.RANGE_TOO_NARROW.value == "range_too_narrow"
    assert ReasonCode.VP_MISSING.value == "vp_missing"
    assert set(REASON_LABELS) == set(ReasonCode)
    assert BLOCKER_LABELS["mid_range"] == "mid-range entry"


def test_gate_never_imports_the_filter() -> None:
    text = open("pyproject.toml").read()  # noqa: SIM115, PTH123
    assert '"arc.features.technicals",  # E16.2' in text  # forbidden for arc.gate


def test_tech_segment_rng_part() -> None:
    tech = _tech(95.0).model_dump(mode="json")
    plain = tech_segment(tech)
    assert "rng" not in plain  # off: byte-identical
    assert tech_segment(tech, rng=True) == plain.replace("off 20d hi", "off 20d hi · rng 0.25")
    assert "rng" not in tech_segment({**tech, "range_pos_20d": None}, rng=True)
    payload = {"regime": {}, "vol": {}, "technicals": tech}
    assert regime_line("X", payload, technicals=True) == regime_line(
        "X", payload, technicals=True, rng=False
    )
    assert "rng 0.25" in regime_line("X", payload, technicals=True, rng=True)
    assert "rng" not in regime_line("X", payload, technicals=False, rng=True)


# ---------------------------------------------------------------------------
# The Research step (offline fixtures). The recorded tapes are too short for a
# regime label, so each test sets ``regime.current`` on the written entries.
# high_low positions: NVDA 0.73, PLTR 0.89, XOM 0.31 (all ranges >= 5 ATR).
# ---------------------------------------------------------------------------

# Research prompt / journal of this fixture run on main 6989a93 (anti-chase on, under
# tests/conftest.py env pins), captured before E23.1. Re-taken for D118 (sleeves 10-35 /
# 36-60): the only prompt change is the entry-window line, and the journal gains three
# NVDA `menu_not_chosen` rows (its 30-35 DTE expiries now also get a short-sleeve menu).
MAIN_RESEARCH_SHA = "5f5eb361cda42c8c7fb333cedb1ebd5ceae31111d3362841b347687cd931ccb7"
MAIN_JOURNAL_SHA = "97102ae4892b69ffaffbb0804b15a720b1556f0682ac1a485fa3d25100f2fe08"


@pytest.fixture
def sideways(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Label every written regime entry ``sideways`` (optionally merge level fields)."""
    import arc.pipeline.steps as steps

    reg = json.loads((FIXTURES / "regime" / "stored_regime_v2_spy.json").read_text())["regime"]
    extra: dict[str, Any] = {"label": "sideways", "tech": {}}
    real = steps._with_levels

    def fake(ctx: Any, ticker: str, payload: Any, today: dt.date) -> dict[str, Any]:
        out = dict(real(ctx, ticker, payload, today) or payload)
        if extra["label"] is not None:
            out["regime"] = {**reg, "current": extra["label"]}
        if out.get("technicals") and ticker in extra["tech"]:
            out["technicals"] = {**out["technicals"], **extra["tech"][ticker]}
        return out

    monkeypatch.setattr(steps, "_with_levels", fake)
    return extra


def _run(
    stances: dict[str, str],
    *,
    enabled: bool = True,
    profile: str = "cash_debit",
    levels: str = "high_low",
    routines: RoutinesConfig | None = None,
) -> tuple[Any, Any]:
    from arc.config import ArcSettings
    from arc.ingest.llm import FixtureScalpLLM
    from arc.pipeline import PipelineEnv
    from tests.test_e59_research_portfolio import _run as run

    env = PipelineEnv.fixtures()
    d = json.loads((Path("arc/pipeline/fixtures") / "research.json").read_text())
    base = d["shortlist"][1]
    d["shortlist"] = [
        {**base, "ticker": t, "stance": s, "rank": n + 1}
        for n, (t, s) in enumerate(stances.items())
    ]
    d["excluded"] = []
    env.llms["research"] = FixtureScalpLLM([json.dumps(d)])
    if routines is None:
        cfg = RangeEdgesSettings(enabled=enabled, levels=levels)  # type: ignore[arg-type]
        routines = load_routines().model_copy(update={"range_edges": cfg})
    settings = ArcSettings(_env_file=None, account_profile=profile)  # type: ignore[call-arg]
    return run(settings, routines, env)


def _decisions(conn: Any, code: str) -> list[Any]:
    return conn.execute(
        "SELECT subject, choice, reason_text, payload FROM decisions WHERE reason_code=?"
        " ORDER BY rowid",
        (code,),
    ).fetchall()


def _tickers(conn: Any) -> list[str]:
    from tests.test_e59_research_portfolio import _shortlist

    return [i["ticker"] for i in _shortlist(conn)["shortlist"]]


def _journal_sha(conn: Any) -> str:
    rows = [
        tuple(r)
        for r in conn.execute(
            "SELECT stage, subject, choice, reason_code, reason_text FROM decisions ORDER BY rowid"
        )
    ]
    return hashlib.sha256(json.dumps(rows).encode()).hexdigest()


def _prompt_sha(conn: Any) -> str:
    return conn.execute(
        "SELECT prompt_sha256 FROM persona_calls WHERE persona='research'"
    ).fetchone()[0]


IDEAS = {"PLTR": "bullish", "NVDA": "bullish", "XOM": "bearish"}


def _anti_chase_on(enabled: bool) -> RoutinesConfig:
    """The routines the main-side golden was taken with (tests/test_anti_chase.py)."""
    from arc.routines.config import AntiChaseSettings

    return load_routines().model_copy(
        update={
            "anti_chase": AntiChaseSettings(enabled=True),
            "range_edges": RangeEdgesSettings(enabled=enabled, levels="high_low"),
        }
    )


def test_flag_off_is_byte_identical_to_main() -> None:
    conn, _ = _run(IDEAS, routines=_anti_chase_on(False))
    assert _prompt_sha(conn) == MAIN_RESEARCH_SHA
    assert _journal_sha(conn) == MAIN_JOURNAL_SHA
    assert _tickers(conn) == ["PLTR", "NVDA", "XOM"]


def test_flag_on_without_a_sideways_regime_only_adds_rng() -> None:
    conn, _ = _run(IDEAS, routines=_anti_chase_on(True))
    assert _prompt_sha(conn) != MAIN_RESEARCH_SHA  # the rng part + legend
    assert _journal_sha(conn) == MAIN_JOURNAL_SHA  # nothing dropped or noted
    row = conn.execute("SELECT prompt_text FROM persona_calls WHERE persona='research'").fetchone()
    assert "rng 0.73" in row[0] and "rng = close's position" in row[0]


def test_mid_range_ideas_are_dropped_journaled_and_reranked(sideways: dict[str, Any]) -> None:
    from tests.test_e59_research_portfolio import _outcome, _shortlist

    stances = {"PLTR": "bearish", "NVDA": "bullish", "XOM": "bullish"}
    conn, report = _run(stances)
    assert _tickers(conn) == ["PLTR", "XOM"]  # PLTR 0.89 bear, XOM 0.31 bull: at the edge
    assert [i["rank"] for i in _shortlist(conn)["shortlist"]] == [1, 2]
    (row,) = _decisions(conn, "mid_range_entry")
    assert row["subject"] == "NVDA" and row["choice"] == "rejected"
    assert row["reason_text"] == (
        "bullish in sideways: mid-range: 0.73 of the 20-day range 210.96–230.36 (keep ≤ 0.35)"
    )
    p = json.loads(row["payload"])
    assert p["idea"]["ticker"] == "NVDA" and p["range_edges"]["status"] == "mid_range"
    assert p["range_edges"]["levels"] == "high_low" and p["range_edges"]["width_atr"] > 1.5
    assert _outcome(report, "research").metrics.get("mid_range_entry") == 1
    nr = conn.execute("SELECT subject FROM decisions WHERE reason_code='not_ranked'").fetchall()
    assert "NVDA" not in {r[0] for r in nr}


def test_value_area_default_and_vp_missing_fallback(sideways: dict[str, Any]) -> None:
    # NVDA gets a value area that puts it at the bottom edge: kept on value_area even
    # though its 20d position (0.73) is mid-range. XOM has no VA: fallback, noted.
    sideways["tech"] = {"NVDA": {"vp_val": 224.0, "vp_vah": 240.0, "vp_pos": 0.07}}
    conn, _ = _run({"NVDA": "bullish", "XOM": "bullish"}, levels="value_area")
    assert _tickers(conn) == ["NVDA", "XOM"]
    assert _decisions(conn, "mid_range_entry") == []
    assert [r["subject"] for r in _decisions(conn, "vp_missing")] == ["XOM"]
    # high_low on the same data drops NVDA
    conn, _ = _run({"NVDA": "bullish", "XOM": "bullish"}, levels="high_low")
    assert _tickers(conn) == ["XOM"]
    assert _decisions(conn, "vp_missing") == []


def test_trending_regime_neutral_and_credit_capable_pass(sideways: dict[str, Any]) -> None:
    sideways["label"] = "bull"
    conn, _ = _run(IDEAS)
    assert _tickers(conn) == ["PLTR", "NVDA", "XOM"]
    sideways["label"] = "sideways"
    # margin: bullish/bearish map to credit spreads too -> not long premium only
    conn, _ = _run(IDEAS, profile="margin")
    assert _tickers(conn) == ["PLTR", "NVDA", "XOM"]
    assert _decisions(conn, "mid_range_entry") == []
    conn, _ = _run({"NVDA": "neutral"}, profile="margin")
    assert _decisions(conn, "mid_range_entry") == []


def test_too_narrow_range_is_kept_and_noted(sideways: dict[str, Any]) -> None:
    sideways["tech"] = {"NVDA": {"atr14": 15.0}}  # 19.4 / 15 = 1.3 ATR < 1.5
    conn, _ = _run({"NVDA": "bullish"})
    assert _tickers(conn) == ["NVDA"]
    (row,) = _decisions(conn, "range_too_narrow")
    assert row["subject"] == "NVDA" and "1.29 ATR wide" in row["reason_text"]


def test_anti_chase_drop_is_not_double_journaled(sideways: dict[str, Any]) -> None:
    from arc.routines.config import AntiChaseSettings

    # PLTR +2.9 ATR, RSI 63.5 trips anti-chase at RSI 60; it would be mid-range too.
    base = load_routines()
    routines = base.model_copy(
        update={
            "anti_chase": AntiChaseSettings(enabled=True, rsi_overbought=60.0),
            "range_edges": RangeEdgesSettings(enabled=True, levels="high_low"),
        }
    )
    conn, _ = _run({"PLTR": "bullish", "NVDA": "bullish"}, routines=routines)
    assert _tickers(conn) == []
    assert [r["subject"] for r in _decisions(conn, "stretched_entry")] == ["PLTR"]
    assert [r["subject"] for r in _decisions(conn, "mid_range_entry")] == ["NVDA"]


def test_missing_technicals_keep_the_idea_noted_once(sideways: dict[str, Any]) -> None:
    import arc.pipeline.steps as steps
    from tests.test_anti_chase import _Snap

    ctx = type("Ctx", (), {})()
    from arc.config import ArcSettings

    ctx.routines = load_routines().model_copy(
        update={"range_edges": RangeEdgesSettings(enabled=True, levels="high_low")}
    )
    ctx.settings = ArcSettings(_env_file=None, account_profile="cash_debit")  # type: ignore[call-arg]
    from tests.test_anti_chase import _item

    snap = _Snap(
        {
            "AAA": {"regime": {"current": "sideways"}, "technicals": None},
            "BBB": None,
            "CCC": {"regime": {"current": "sideways"}, "technicals": _tech(100.0).model_dump()},
        }
    )
    items = [_item("AAA", "bullish", 1), _item("BBB", "bullish", 2), _item("CCC", "bullish", 3)]
    out = steps._range_edge_filter(ctx, snap, items)  # type: ignore[arg-type]
    assert [i.ticker for i in out.kept] == ["AAA", "BBB"]  # BBB: no regime entry at all
    assert [i.rank for i in out.kept] == [1, 2]
    assert [i.ticker for i in out.tech_missing] == ["AAA"]
    assert out.dropped == {"mid_range_entry": 1}

    class _J:
        def __init__(self) -> None:
            self.rows: list[tuple[str, Any]] = []

        def add(self, _p: Any, _s: Any, subject: str, _c: Any, code: Any, **_: Any) -> None:
            self.rows.append((subject, code))

    j = _J()
    steps._journal_range_edges(j, out, None, {"AAA"})  # type: ignore[arg-type]
    assert j.rows == [("CCC", ReasonCode.MID_RANGE_ENTRY)]  # AAA already noted by anti-chase
    j = _J()
    steps._journal_range_edges(j, out, None, set())  # type: ignore[arg-type]
    assert ("AAA", ReasonCode.TECHNICALS_MISSING) in j.rows


def test_slack_off_reaches_the_step(sideways: dict[str, Any]) -> None:
    """`arc config set personas.range_edges off` (ControlService) turns the step off."""
    from arc.config import ArcSettings
    from arc.control.effective import effective_routines
    from arc.control.service import ControlService
    from arc.store.db import connect
    from arc.store.migrate import migrate
    from arc.utils.calendar import ET

    store = connect(":memory:")
    migrate(store)
    store.row_factory = sqlite3.Row
    owner = "U0OWNER"
    base = ArcSettings(_env_file=None, approver_slack_user_ids=[owner])  # type: ignore[call-arg]
    svc = ControlService(
        store,
        base=base,
        now=lambda: dt.datetime(2026, 10, 12, 9, 0, tzinfo=ET),
        is_halted=lambda: False,
    )
    on = effective_routines(store)
    assert on.range_edges.enabled is True
    r = svc.set("personas.range_edges", "off", actor=owner, source="slack")
    if r.pending is not None:
        r = svc.confirm(r.pending.code, actor=owner, source="slack")
    assert r.outcome == "applied"
    off = effective_routines(store)
    assert off.range_edges.enabled is False

    stances = {"NVDA": "bullish", "XOM": "bullish"}
    conn, _ = _run(stances, routines=on.model_copy(update={"range_edges": on.range_edges}))
    assert _decisions(conn, "mid_range_entry") or _decisions(conn, "vp_missing")
    conn, _ = _run(stances, routines=off)
    assert _tickers(conn) == ["NVDA", "XOM"]
    for code in ("mid_range_entry", "vp_missing", "range_too_narrow"):
        assert _decisions(conn, code) == []
