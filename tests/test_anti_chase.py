"""E16.3 (D76/D78): the anti-chase entry filter.

Rule truth table (pure), session VWAP helpers, config + registry, journal labels,
and the Research step: flag off = byte-identical, flag on = a stretched idea is
dropped and journaled with the numbers that tripped it.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from arc.control.registry import REGISTRY, lookup, read_raw, write_raw
from arc.features.technicals import (
    AntiChaseRule,
    TechnicalFeatures,
    is_stretched,
    session_vwap,
    vwap_stretch_atr,
)
from arc.journal.reasons import REASON_LABELS, ReasonCode
from arc.routines.config import AntiChaseSettings, RoutinesConfig, load_routines
from arc.utils.calendar import ET

FIXTURES = Path("arc/pipeline/fixtures")
D78 = AntiChaseRule()  # combine all, 2.5 ATR, RSI 75 / 25
D76 = AntiChaseRule(combine="any", max_stretch_atr=2.0)


def _tech(
    stretch: float | None = 0.0,
    rsi: float | None = 50.0,
    dh: float | None = 1.0,
    dl: float | None = 1.0,
    atr: float | None = 2.0,
) -> TechnicalFeatures:
    return TechnicalFeatures(
        as_of=dt.date(2026, 10, 9),
        bar_date=dt.date(2026, 10, 9),
        close=100.0,
        stretch_atr=stretch,
        rsi14=rsi,
        dist_high20_atr=dh,
        dist_low20_atr=dl,
        atr14=atr,
    )


# ---------------------------------------------------------------------------
# Rule truth table
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("stance", "stretch", "rsi", "expected"),
    [
        # D78 (combine all): stretch >= 2.5 AND RSI >= 75 (bears: <= -2.5 AND <= 25)
        ("bullish", 2.5, 75.0, "stretched"),  # both at the threshold
        ("bullish", 3.4, 81.0, "stretched"),
        ("bullish", 2.49, 90.0, "ok"),  # stretch just under
        ("bullish", 4.0, 74.9, "ok"),  # RSI just under
        ("bullish", -3.0, 20.0, "ok"),  # a bear setup never trips a bull
        ("bearish", -2.5, 25.0, "stretched"),
        ("bearish", -3.1, 18.0, "stretched"),
        ("bearish", -2.4, 10.0, "ok"),
        ("bearish", -3.0, 25.1, "ok"),
        ("bearish", 3.0, 80.0, "ok"),  # a bull setup never trips a bear
        ("neutral", 4.0, 90.0, "not_directional"),
        ("Bullish ", 3.0, 80.0, "stretched"),  # stance is normalised
        ("", 3.0, 80.0, "not_directional"),
    ],
)
def test_d78_truth_table(stance: str, stretch: float, rsi: float, expected: str) -> None:
    v = is_stretched(_tech(stretch, rsi), stance, D78)
    assert v.status == expected
    assert v.stretched is (expected == "stretched")
    assert bool(v.triggers) is (expected == "stretched")


@pytest.mark.parametrize(
    ("stance", "stretch", "rsi", "dh", "dl", "expected"),
    [
        # D76 card rule (combine any, 2.0): stretch OR (RSI AND near the 20d extreme)
        ("bullish", 2.0, 50.0, 3.0, 3.0, "stretched"),  # stretch alone
        ("bullish", 1.0, 76.0, 0.25, 3.0, "stretched"),  # RSI + at the 20d high
        ("bullish", 1.0, 76.0, 0.26, 3.0, "ok"),  # RSI but not near the high
        ("bullish", 1.0, 74.0, 0.0, 3.0, "ok"),  # near the high but RSI below
        ("bearish", -2.0, 50.0, 3.0, 3.0, "stretched"),
        ("bearish", -1.0, 24.0, 3.0, 0.1, "stretched"),  # RSI + at the 20d low
        ("bearish", -1.0, 24.0, 0.1, 3.0, "ok"),  # near the HIGH does not count for a bear
        ("bearish", -1.9, 26.0, 3.0, 0.0, "ok"),
    ],
)
def test_d76_any_truth_table(
    stance: str, stretch: float, rsi: float, dh: float, dl: float, expected: str
) -> None:
    assert is_stretched(_tech(stretch, rsi, dh, dl), stance, D76).status == expected


def test_missing_data_keeps_the_idea_unless_another_condition_decides() -> None:
    # No technicals at all: missing (the caller keeps the idea and journals it).
    v = is_stretched(None, "bullish", D78)
    assert v.status == "missing" and v.missing == ["technicals"] and not v.stretched
    # all: one leg False decides "ok" even when the other is unknown
    assert is_stretched(_tech(1.0, None), "bullish", D78).status == "ok"
    # all: one leg True + other unknown cannot decide
    v = is_stretched(_tech(3.0, None), "bullish", D78)
    assert v.status == "missing" and v.missing == ["rsi14"]
    assert is_stretched(_tech(None, 80.0), "bullish", D78).missing == ["stretch_atr"]
    # any: stretch True decides even with RSI unknown
    assert is_stretched(_tech(2.5, None, None), "bullish", D76).status == "stretched"
    # any: stretch False + RSI near-high unknown -> missing
    v = is_stretched(_tech(1.0, 80.0, None), "bullish", D76)
    assert v.status == "missing" and "dist_high20_atr" in v.missing
    v = is_stretched(_tech(-1.0, 20.0, 1.0, None), "bearish", D76)
    assert v.status == "missing" and "dist_low20_atr" in v.missing


def test_verdict_carries_the_numbers_and_a_plain_text() -> None:
    v = is_stretched(_tech(2.83, 79.4, 0.1), "bullish", D78)
    assert v.stretch_atr == 2.83 and v.rsi14 == 79.4 and v.dist_extreme20_atr == 0.1
    assert v.text() == "+2.8 ATR over SMA20 (limit 2.5); RSI 79 (limit 75)"
    dumped = v.model_dump(mode="json")
    assert dumped["rule"]["max_stretch_atr"] == 2.5 and dumped["status"] == "stretched"
    b = is_stretched(_tech(-2.6, 22.0), "bearish", D78)
    assert b.text() == "-2.6 ATR under SMA20 (limit 2.5); RSI 22 (limit 25)"
    assert is_stretched(_tech(), "neutral", D78).text() == "not a directional idea"
    assert is_stretched(None, "bullish", D78).text() == "technicals missing: technicals"
    assert is_stretched(_tech(), "bullish", D78).text() == "not stretched"
    a = is_stretched(_tech(1.0, 76.0, 0.2), "bullish", D76)
    assert "0.20 ATR from the 20-day high" in a.text()


def test_vwap_part_is_or_ed_and_its_absence_never_drops() -> None:
    rule = D78.model_copy(update={"vwap": True})
    calm = _tech(0.5, 55.0)
    v = is_stretched(calm, "bullish", rule, vwap_stretch=0.75)
    assert v.stretched and v.triggers == ["+0.75 ATR vs VWAP (limit 0.75)"]
    assert is_stretched(calm, "bullish", rule, vwap_stretch=0.74).status == "ok"
    assert is_stretched(calm, "bearish", rule, vwap_stretch=-0.8).stretched
    assert is_stretched(calm, "bearish", rule, vwap_stretch=0.8).status == "ok"
    # fetch failed: VWAP part skipped (reported), the daily rule still decides
    v = is_stretched(calm, "bullish", rule, vwap_stretch=None)
    assert v.status == "ok" and v.missing == ["vwap"]
    assert is_stretched(_tech(3.0, 80.0), "bullish", rule, vwap_stretch=None).stretched
    # the switch off ignores a VWAP number entirely
    assert is_stretched(calm, "bullish", D78, vwap_stretch=5.0).status == "ok"


@given(
    stretch=st.one_of(st.none(), st.floats(-8, 8)),
    rsi=st.one_of(st.none(), st.floats(0, 100)),
    dh=st.one_of(st.none(), st.floats(0, 5)),
    dl=st.one_of(st.none(), st.floats(0, 5)),
    combine=st.sampled_from(["all", "any"]),
)
def test_mirror_symmetry_and_monotonicity(
    stretch: float | None, rsi: float | None, dh: float | None, dl: float | None, combine: str
) -> None:
    rule = AntiChaseRule(combine=combine)  # type: ignore[arg-type]
    bull = is_stretched(_tech(stretch, rsi, dh, dl), "bullish", rule)
    mirror = _tech(
        None if stretch is None else -stretch, None if rsi is None else 100.0 - rsi, dl, dh
    )
    bear = is_stretched(mirror, "bearish", rule)
    assert bull.status == bear.status  # bears mirror bulls exactly
    # a stricter (lower) stretch bar never un-stretches an idea
    looser = rule.model_copy(update={"max_stretch_atr": rule.max_stretch_atr * 0.6})
    if bull.stretched:
        assert is_stretched(_tech(stretch, rsi, dh, dl), "bullish", looser).stretched


# ---------------------------------------------------------------------------
# Session VWAP
# ---------------------------------------------------------------------------


class _Bar:
    def __init__(self, t: dt.datetime, h: float, lo: float, c: float, v: float) -> None:
        self.timestamp, self.high, self.low, self.close, self.volume = t, h, lo, c, v


def _at(h: int, m: int) -> dt.datetime:
    return dt.datetime(2026, 10, 9, h, m, tzinfo=ET)


def test_session_vwap_weights_typical_price_by_volume_inside_the_session() -> None:
    bars = [
        _Bar(_at(9, 25), 999, 999, 999, 1e6),  # pre-market: excluded
        _Bar(_at(9, 30), 101, 99, 100, 100),  # typical 100
        _Bar(_at(9, 35), 104, 100, 102, 300),  # typical 102
        _Bar(_at(9, 40), 0, 0, 0, 0),  # no volume
        _Bar(_at(10, 0), 999, 999, 999, 1e6),  # at/after `end`: excluded
        _Bar(dt.datetime(2026, 10, 8, 10, 0, tzinfo=ET), 1, 1, 1, 1e6),  # another day
    ]
    v = session_vwap(bars, start=dt.time(9, 30), end=_at(10, 0))
    assert v == pytest.approx((100 * 100 + 102 * 300) / 400)
    assert session_vwap(bars[:1], start=dt.time(9, 30), end=_at(10, 0)) is None
    # a UTC-stamped bar is read on the ET clock
    utc = _Bar(_at(9, 30).astimezone(dt.UTC), 11, 9, 10, 5)
    assert session_vwap([utc], start=dt.time(9, 30), end=_at(10, 0)) == pytest.approx(10.0)


def test_vwap_stretch_atr() -> None:
    assert vwap_stretch_atr(103.0, 101.0, 2.0) == pytest.approx(1.0)
    assert vwap_stretch_atr(99.0, 101.0, 2.0) == pytest.approx(-1.0)
    for args in [(None, 1.0, 1.0), (1.0, None, 1.0), (1.0, 1.0, None), (1.0, 1.0, 0.0)]:
        assert vwap_stretch_atr(*args) is None


# ---------------------------------------------------------------------------
# Config, registry, labels
# ---------------------------------------------------------------------------


def test_shipped_config_is_the_d78_rule_on_with_vwap_off() -> None:
    cfg = load_routines().anti_chase
    assert cfg.enabled is True  # D78: ships on without an experiment
    assert (cfg.combine, cfg.max_stretch_atr, cfg.rsi_overbought, cfg.rsi_oversold) == (
        "all",
        2.5,
        75.0,
        25.0,
    )
    assert cfg.vwap is False and cfg.max_vwap_stretch_atr == 0.75
    assert cfg.max_dist_high20_atr == 0.25
    assert cfg.rule() == AntiChaseRule()  # the pure rule's defaults are the shipped ones
    assert AntiChaseSettings().enabled is False  # absent switch = off (rollback)


@pytest.mark.parametrize(("raw", "on"), [("on", True), ("off", False), (True, True)])
def test_switch_parses(raw: Any, on: bool) -> None:
    cfg = RoutinesConfig.model_validate({"personas": {"anti_chase": raw}})
    assert cfg.anti_chase.enabled is on
    assert "anti_chase" not in cfg.personas  # lifted out of the job map


@pytest.mark.parametrize(
    "data",
    [
        {"personas": {"anti_chase": "maybe"}},
        {"personas": {"anti_chase": "on"}, "anti_chase": {"enabled": True}},  # one switch
        {"anti_chase": {"max_stretch_atr": 0}},
        {"anti_chase": {"rsi_overbought": 40}},
        {"anti_chase": {"rsi_oversold": 60}},
        {"anti_chase": {"combine": "either"}},
        {"anti_chase": {"vwap": "sometimes"}},
        {"anti_chase": {"bogus": 1}},
    ],
)
def test_bad_config_is_refused(data: dict[str, Any]) -> None:
    with pytest.raises((ValidationError, ValueError)):
        RoutinesConfig.model_validate(data)


def test_registry_keys_read_and_write_the_yaml() -> None:
    raw = yaml.safe_load(open("config/routines.yaml").read())  # noqa: SIM115, PTH123
    assert lookup("personas.anti_chase").key == "personas.anti_chase"
    assert lookup("anti_chase").key == "personas.anti_chase"  # alias
    assert read_raw(REGISTRY["personas.anti_chase"], raw) == "on"
    assert write_raw(REGISTRY["personas.anti_chase"], "off", raw) == [
        (("personas", "anti_chase"), "off")
    ]
    assert read_raw(REGISTRY["anti_chase.max_stretch_atr"], raw) == 2.5
    assert read_raw(REGISTRY["anti_chase.vwap"], raw) == "off"
    assert write_raw(REGISTRY["anti_chase.rsi_overbought"], 80.0, raw) == [
        (("anti_chase", "rsi_overbought"), 80.0)
    ]
    keys = {k for k in REGISTRY if "anti_chase" in k}
    assert keys == {
        "personas.anti_chase",
        "anti_chase.combine",
        "anti_chase.max_stretch_atr",
        "anti_chase.rsi_overbought",
        "anti_chase.rsi_oversold",
        "anti_chase.max_dist_high20_atr",
        "anti_chase.vwap",
        "anti_chase.max_vwap_stretch_atr",
    }
    # every numeric knob is bounded
    for k in keys:
        t = REGISTRY[k]
        assert t.choices or (t.min is not None and t.max is not None), k


def test_reason_codes_have_plain_labels() -> None:
    assert REASON_LABELS[ReasonCode.STRETCHED_ENTRY] == "Skipped: move already stretched"
    assert ReasonCode.TECHNICALS_MISSING.value == "technicals_missing"
    assert ReasonCode.VWAP_MISSING.value == "vwap_missing"
    assert set(REASON_LABELS) == set(ReasonCode)


def test_gate_never_imports_the_filter() -> None:
    text = open("pyproject.toml").read()  # noqa: SIM115, PTH123
    assert '"arc.features.technicals",  # E16.2' in text  # forbidden for arc.gate


def test_json_roundtrip_of_a_verdict() -> None:
    v = is_stretched(_tech(3.0, 80.0), "bullish", D78)
    back = json.loads(json.dumps(v.model_dump(mode="json")))
    assert back["triggers"] and back["direction"] == "bullish"


# ---------------------------------------------------------------------------
# The Research step (offline fixtures: PLTR's recorded tape is +2.92 ATR, RSI 63.5;
# NVDA +0.96 ATR, RSI 55; XOM -0.95 ATR, RSI 49)
# ---------------------------------------------------------------------------


def _pipeline_run(
    flag: bool,
    stance_by_ticker: dict[str, str],
    *,
    profile: str = "cash_debit",
    rule: dict[str, Any] | None = None,
) -> tuple[Any, Any]:
    from arc.config import ArcSettings
    from arc.ingest.llm import FixtureScalpLLM
    from arc.pipeline import PipelineEnv
    from tests.test_e59_research_portfolio import _run

    env = PipelineEnv.fixtures()
    d = json.loads((FIXTURES / "research.json").read_text())
    base = d["shortlist"][1]
    d["shortlist"] = [
        {**base, "ticker": t, "stance": s, "rank": n + 1}
        for n, (t, s) in enumerate(stance_by_ticker.items())
    ]
    d["excluded"] = []
    env.llms["research"] = FixtureScalpLLM([json.dumps(d)])
    ac = AntiChaseSettings(enabled=flag, **(rule or {}))
    routines = load_routines().model_copy(update={"anti_chase": ac})
    settings = ArcSettings(_env_file=None, account_profile=profile)  # type: ignore[call-arg]
    return _run(settings, routines, env)


def _decisions(conn: Any, code: str) -> list[Any]:
    return conn.execute(
        "SELECT subject, choice, reason_text, payload FROM decisions WHERE reason_code=?"
        " ORDER BY rowid",
        (code,),
    ).fetchall()


def _tickers(conn: Any) -> list[str]:
    from tests.test_e59_research_portfolio import _shortlist

    return [i["ticker"] for i in _shortlist(conn)["shortlist"]]


IDEAS = {"PLTR": "bullish", "NVDA": "bullish", "XOM": "bearish"}
LOW_RSI = {"rsi_overbought": 60.0}  # trips PLTR (+2.9 ATR, RSI 63.5) on the D78 shape


def test_d78_default_keeps_every_fixture_idea() -> None:
    # PLTR is +2.9 ATR over SMA20 but RSI 63.5 < 75: the D78 AND rule keeps it.
    conn, _ = _pipeline_run(True, IDEAS)
    assert _tickers(conn) == ["PLTR", "NVDA", "XOM"]
    assert _decisions(conn, "stretched_entry") == []
    assert _decisions(conn, "technicals_missing") == []


def test_stretched_idea_is_dropped_journaled_and_survivors_reranked() -> None:
    from tests.test_e59_research_portfolio import _outcome, _shortlist

    conn, report = _pipeline_run(True, IDEAS, rule=LOW_RSI)
    assert _tickers(conn) == ["NVDA", "XOM"]
    assert [i["rank"] for i in _shortlist(conn)["shortlist"]] == [1, 2]
    (row,) = _decisions(conn, "stretched_entry")
    assert row["subject"] == "PLTR" and row["choice"] == "rejected"
    assert row["reason_text"] == "bullish: +2.9 ATR over SMA20 (limit 2.5); RSI 64 (limit 60)"
    p = json.loads(row["payload"])["anti_chase"]
    assert p["status"] == "stretched" and p["stretch_atr"] == pytest.approx(2.918, abs=1e-3)
    assert p["rule"]["rsi_overbought"] == 60.0
    assert _outcome(report, "research").metrics.get("stretched_entry") == 1
    # never double-journaled as not_ranked
    nr = conn.execute("SELECT subject FROM decisions WHERE reason_code='not_ranked'").fetchall()
    assert "PLTR" not in {r[0] for r in nr}


def test_combine_any_is_the_d76_card_rule() -> None:
    conn, _ = _pipeline_run(True, IDEAS, rule={"combine": "any"})
    assert _tickers(conn) == ["NVDA", "XOM"]  # +2.9 ATR >= 2.5 alone trips `any`


def test_flag_off_is_the_pre_e163_pipeline() -> None:
    off, _ = _pipeline_run(False, IDEAS, rule=LOW_RSI)
    on_default, _ = _pipeline_run(True, IDEAS)
    assert _tickers(off) == ["PLTR", "NVDA", "XOM"]
    for code in ("stretched_entry", "technicals_missing", "vwap_missing"):
        assert _decisions(off, code) == []

    def _shape(conn: Any) -> list[tuple[Any, ...]]:
        return [
            tuple(r)
            for r in conn.execute(
                "SELECT stage, subject, choice, reason_code, reason_text FROM decisions"
                " ORDER BY rowid"
            ).fetchall()
        ]

    # with nothing stretched, "on" journals exactly what "off" does
    assert _shape(off) == _shape(on_default)


def test_neutral_and_credit_capable_ideas_are_never_filtered() -> None:
    # margin profile: bullish maps to credit spreads too -> not long-premium only
    conn, _ = _pipeline_run(True, IDEAS, profile="margin", rule=LOW_RSI)
    assert "PLTR" in _tickers(conn)
    assert _decisions(conn, "stretched_entry") == []
    conn, _ = _pipeline_run(True, {"PLTR": "neutral"}, profile="margin", rule=LOW_RSI)
    assert _decisions(conn, "stretched_entry") == []


# ---------------------------------------------------------------------------
# _anti_chase_filter directly (missing technicals, VWAP fetch paths)
# ---------------------------------------------------------------------------


class _Entry:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload


class _Snap:
    def __init__(self, by_ticker: dict[str, dict[str, Any] | None]) -> None:
        self._m = by_ticker

    def latest(self, kind: str, ticker: str) -> Any:
        assert kind == "regime"
        p = self._m.get(ticker)
        return None if p is None else _Entry(p)


class _Ctx:
    def __init__(self, cfg: AntiChaseSettings, now: dt.datetime) -> None:
        from arc.config import ArcSettings

        self.routines = load_routines().model_copy(update={"anti_chase": cfg})
        self.settings = ArcSettings(_env_file=None, account_profile="cash_debit")  # type: ignore[call-arg]
        self.now = now
        self.inputs: list[str] = []

    def record_input(self, name: str, *_: Any, **__: Any) -> None:
        self.inputs.append(name)


class _Market:
    def __init__(self, bars: list[Any] | Exception) -> None:
        self._bars = bars
        self.calls: list[tuple[str, str]] = []

    def history_bars(self, t: str, s: dt.date, e: dt.date, tf: str = "1Day") -> list[Any]:
        self.calls.append((t, tf))
        if isinstance(self._bars, Exception):
            raise self._bars
        return self._bars


class _Env:
    def __init__(self, market: _Market) -> None:
        self.market = market
        self.offline = True


def _item(ticker: str, stance: str, rank: int) -> Any:
    from arc.personas.schemas import ResearchRankedItem

    base = json.loads((FIXTURES / "research.json").read_text())["shortlist"][1]
    return ResearchRankedItem.model_validate(
        {**base, "ticker": ticker, "stance": stance, "rank": rank}
    )


def _tech_payload(**kw: Any) -> dict[str, Any]:
    return {"technicals": _tech(**kw).model_dump(mode="json")}


NOW = dt.datetime(2026, 10, 9, 11, 0, tzinfo=ET)


def test_missing_technicals_keep_the_idea_and_are_reported() -> None:
    from arc.pipeline.steps import _anti_chase_filter

    ctx = _Ctx(AntiChaseSettings(enabled=True), NOW)
    snap = _Snap(
        {"AAA": None, "BBB": {"technicals": None}, "CCC": _tech_payload(stretch=3, rsi=80)}
    )
    items = [_item("AAA", "bullish", 1), _item("BBB", "bearish", 2), _item("CCC", "bullish", 3)]
    out = _anti_chase_filter(ctx, _Env(_Market([])), snap, items)  # type: ignore[arg-type]
    assert [i.ticker for i in out.kept] == ["AAA", "BBB"]
    assert [i.rank for i in out.kept] == [1, 2]
    assert [i.ticker for i in out.tech_missing] == ["AAA", "BBB"]
    assert out.dropped == {"stretched_entry": 1}


def test_vwap_part_uses_intraday_bars_and_a_failure_only_skips_it() -> None:
    from arc.pipeline.steps import _anti_chase_filter

    cfg = AntiChaseSettings(enabled=True, vwap=True)
    calm = _tech_payload(stretch=0.5, rsi=55, atr=2.0)
    bars = [
        _Bar(_at(9, 30), 101, 99, 100, 1000),  # VWAP 100
        _Bar(_at(10, 55), 102, 101, 102, 1),  # last close 102 -> +1.0 ATR vs VWAP
        _Bar(_at(11, 5), 200, 200, 200, 1),  # after now: ignored
    ]
    ctx = _Ctx(cfg, NOW)
    mk = _Market(bars)
    out = _anti_chase_filter(ctx, _Env(mk), _Snap({"AAA": calm}), [_item("AAA", "bullish", 1)])  # type: ignore[arg-type]
    assert out.kept == [] and out.dropped == {"stretched_entry": 1}
    assert mk.calls == [("AAA", "5Min")] and ctx.inputs == ["bars5m:AAA"]
    assert out.verdicts["AAA"].vwap_stretch_atr == pytest.approx(
        (102 - 100.0 * 1000 / 1001 - 101.5 / 1001 * 1) / 2.0, abs=0.01
    )

    ctx = _Ctx(cfg, NOW)
    out = _anti_chase_filter(  # type: ignore[arg-type]
        ctx,
        _Env(_Market(RuntimeError("feed down"))),
        _Snap({"AAA": calm}),
        [_item("AAA", "bullish", 1)],
    )
    assert [i.ticker for i in out.kept] == ["AAA"]
    ((item, why),) = out.vwap_missing
    assert item.ticker == "AAA" and "feed down" in why
    # no session bars yet (before the open) -> skipped, idea kept
    out = _anti_chase_filter(  # type: ignore[arg-type]
        _Ctx(cfg, NOW), _Env(_Market(bars[:0])), _Snap({"AAA": calm}), [_item("AAA", "bullish", 1)]
    )
    assert out.vwap_missing[0][1] == "no session bars yet"
    # vwap off: no intraday request at all
    mk = _Market(bars)
    _anti_chase_filter(  # type: ignore[arg-type]
        _Ctx(AntiChaseSettings(enabled=True), NOW),
        _Env(mk),
        _Snap({"AAA": calm}),
        [_item("AAA", "bullish", 1)],
    )
    assert mk.calls == []
