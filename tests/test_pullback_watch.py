"""E23.2 (D98): the pullback watch for anti-chase (``stretched_entry``) drops.

Pure rule (``rearm_verdict``: every branch, bears mirror bulls, VWAP on/off/missing,
expiry in trading sessions across a weekend + holiday, invalidation, regime flip;
hypothesis: never ready while stretched past the re-arm bar), config + registry + kinds,
and the Research step: off = byte-identical to main (pinned shas), on = a stretched drop
goes on watch, re-arms two sessions later at its volume-profile level, becomes a
candidate row, is ranked again, and reaches Quant.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from arc.context.kinds import CandidatePayload, PullbackWatchPayload, kind_spec
from arc.control.registry import REGISTRY, lookup, read_raw, write_raw
from arc.features.technicals import TechnicalFeatures
from arc.journal.reasons import REASON_LABELS, ReasonCode
from arc.pipeline import FIXTURE_NOW
from arc.pipeline.pullback import PullbackRule, WatchView, rearm_verdict
from arc.routines.config import (
    AntiChaseSettings,
    PullbackWatchSettings,
    RangeEdgesSettings,
    RoutinesConfig,
    load_routines,
)

FIXTURES = Path("arc/pipeline/fixtures")
TODAY = dt.date(2026, 10, 13)  # a Tuesday
SMA = PullbackRule(target="sma20")
VAL = PullbackRule()  # target value (default)


def _watch(stance: str = "bullish", **kw: Any) -> WatchView:
    base: dict[str, Any] = {
        "ticker": "NVDA",
        "stance": stance,
        "expires_session": dt.date(2026, 10, 15),
        "price_at_drop": 110.0 if stance == "bullish" else 90.0,
        "sma20": 100.0,
        "atr14": 2.0,
    }
    return WatchView(**{**base, **kw})


def _tech(**kw: Any) -> TechnicalFeatures:
    return TechnicalFeatures(
        as_of=TODAY, bar_date=TODAY, close=kw.pop("close", 100.0),
        sma20=kw.pop("sma20", 100.0), atr14=kw.pop("atr14", 2.0), **kw,
    )  # fmt: skip


def _v(
    spot: float | None,
    stance: str = "bullish",
    *,
    rule: PullbackRule = SMA,
    tech: Any = "default",
    vwap: float | None = None,
    today: dt.date = TODAY,
    regime: str | None = None,
    **watch: Any,
) -> Any:
    t = _tech() if tech == "default" else tech
    return rearm_verdict(_watch(stance, **watch), spot, t, vwap, rule, today=today, regime=regime)


# ---------------------------------------------------------------------------
# rearm_verdict: each branch
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("spot", "stance", "status"),
    [
        (101.0, "bullish", "ready"),  # +0.5 ATR <= 1.0
        (102.0, "bullish", "ready"),  # exactly at the bar
        (102.2, "bullish", "waiting"),  # +1.1 ATR
        (98.5, "bullish", "ready"),  # under SMA20, above the invalidation line
        (97.9, "bullish", "invalidated"),  # -1.05 ATR < -1.0
        (99.0, "bearish", "ready"),  # mirrored
        (98.0, "bearish", "ready"),
        (97.8, "bearish", "waiting"),
        (101.5, "bearish", "ready"),
        (102.1, "bearish", "invalidated"),
    ],
)
def test_sma20_target_truth_table(spot: float, stance: str, status: str) -> None:
    v = _v(spot, stance)
    assert v.status == status, v.reason
    assert v.target == ("sma20" if status != "invalidated" else None)


def test_expiry_counts_trading_sessions_across_a_weekend_and_a_holiday() -> None:
    from arc.utils.calendar import add_sessions

    # Wed 2026-11-25 drop, 3 sessions: Thu 26 is Thanksgiving, Fri 27 + Mon 30 + Tue 1
    drop = dt.date(2026, 11, 25)
    last = add_sessions(drop, 3)
    assert last == dt.date(2026, 12, 1)
    w: dict[str, Any] = {"expires_session": last}
    assert _v(101.0, today=dt.date(2026, 11, 30), **w).status == "ready"
    assert _v(101.0, today=last, **w).status == "ready"  # still watched on the last one
    v = _v(101.0, today=dt.date(2026, 12, 2), **w)
    assert v.status == "expired" and v.reason == "watched through 2026-12-01"
    # expiry wins over everything else
    assert _v(None, tech=None, today=dt.date(2026, 12, 2), **w).status == "expired"


@pytest.mark.parametrize(
    ("stance", "label", "status"),
    [
        ("bullish", "bear", "invalidated"),
        ("bullish", "Bear ", "invalidated"),
        ("bullish", "sideways", "ready"),
        ("bullish", "bull", "ready"),
        ("bearish", "bull", "invalidated"),
        ("bearish", "bear", "ready"),
        ("bullish", None, "ready"),
    ],
)
def test_regime_flip_invalidates(stance: str, label: str | None, status: str) -> None:
    spot = 101.0 if stance == "bullish" else 99.0
    v = _v(spot, stance, regime=label)
    assert v.status == status
    if status == "invalidated":
        assert v.reason.startswith("regime flipped to ")


def test_missing_inputs_wait_and_todays_technicals_win() -> None:
    v = _v(None)
    assert v.status == "waiting" and v.missing == ["spot"]
    v = _v(101.0, tech=None, sma20=None, atr14=None)
    assert v.status == "waiting" and v.missing == ["sma20", "atr14"]
    v = _v(101.0, tech=None, atr14=0.0)
    assert v.status == "waiting" and v.missing == ["atr14"]
    # stored drop numbers are the fallback when today has no technicals
    assert _v(101.0, tech=None).status == "ready"
    # today's SMA20 / ATR14 beat the stored ones: SMA20 moved up to 104 -> -1.5 ATR
    assert _v(101.0, tech=_tech(sma20=104.0)).status == "invalidated"


@pytest.mark.parametrize("stance", ["bullish", "bearish"])
def test_vwap_on_off_and_missing(stance: str) -> None:
    sign = 1.0 if stance == "bullish" else -1.0
    spot = 100.0 + sign * 1.0
    on = PullbackRule(target="sma20", vwap=True)
    assert _v(spot, stance, rule=SMA, vwap=None).status == "ready"  # off: ignored
    v = _v(spot, stance, rule=on, vwap=None)
    assert v.status == "waiting" and v.vwap_missing and v.reason == "no session VWAP"
    assert _v(spot, stance, rule=on, vwap=sign * 0.2).ready
    v = _v(spot, stance, rule=on, vwap=sign * 0.3)
    assert v.status == "waiting" and "over VWAP" in v.reason
    assert _v(spot, stance, rule=on, vwap=-sign * 2.0).ready  # under VWAP: fine
    assert v.vwap_stretch_atr == pytest.approx(sign * 0.3)


def test_value_target_picks_the_nearest_level_on_the_stance_side() -> None:
    # bullish: drop at 110; POC 104, VAH 106, HVN below 101; VAL is a bearish level
    tech = _tech(vp_poc=104.0, vp_vah=106.0, vp_val=98.0, vp_hvn_below=101.0, vp_hvn_above=112.0)
    v = _v(106.6, rule=VAL, tech=tech)  # 0.3 ATR above VAH
    assert v.ready and v.target == "value" and v.level is not None
    assert (v.level.name, v.level.price) == ("VAH", 106.0)
    assert v.level.dist_atr == pytest.approx(0.3)
    assert v.now_text().startswith("now 0.3 ATR above VAH 106.00")
    v = _v(108.0, rule=VAL, tech=tech)  # 1.0 ATR above VAH
    assert v.status == "waiting" and "from VAH" in v.reason
    v = _v(105.5, rule=VAL, tech=tech)  # under VAH: nearest on-side is POC 104 (0.75)
    assert v.status == "waiting" and v.level is not None and v.level.name == "POC"
    v = _v(104.6, rule=VAL, tech=tech)
    assert v.ready and v.level is not None and v.level.name == "POC"
    v = _v(100.5, rule=VAL, tech=tech)  # below every level, above invalidation: ready
    assert v.ready and v.level is not None and v.level.name == "HVN"
    assert v.now_text().startswith("now 0.2 ATR below HVN 101.00")


def test_value_target_mirrors_for_bears_and_skips_far_side_levels() -> None:
    tech = _tech(vp_poc=96.0, vp_val=94.0, vp_vah=102.0, vp_hvn_above=99.0, vp_hvn_below=88.0)
    v = _v(93.5, "bearish", rule=VAL, tech=tech)  # 0.25 ATR below VAL
    assert v.ready and v.level is not None and v.level.name == "VAL"
    assert v.now_text().startswith("now 0.2 ATR below VAL 94.00")
    v = _v(99.5, "bearish", rule=VAL, tech=tech)
    assert v.ready and v.level is not None and v.level.name == "HVN"
    # a level above the bullish drop price (110) is not a pullback target
    far = _tech(vp_poc=111.0, vp_vah=115.0)
    v = _v(101.0, rule=VAL, tech=far)
    assert v.vp_missing and v.target == "sma20" and v.ready


def test_missing_levels_fall_back_to_sma20() -> None:
    v = _v(101.0, rule=VAL)
    assert v.ready and v.vp_missing and v.target == "sma20" and v.level is None
    v = _v(102.5, rule=VAL)
    assert v.status == "waiting" and v.vp_missing
    v = _v(101.0, rule=VAL, tech=_tech(vp_poc=0.0))  # non-positive levels are ignored
    assert v.vp_missing
    assert not _v(101.0, rule=SMA, tech=_tech(vp_poc=100.0)).vp_missing  # sma20 asked


def test_verdict_text_and_json_roundtrip() -> None:
    v = _v(101.0, rule=PullbackRule(target="sma20", vwap=True), vwap=0.1)
    assert v.text() == (
        "ready: now +0.5 ATR vs SMA20, +0.1 ATR vs VWAP (0.50 ATR stretch vs SMA20 (rearm 1))"
    )
    assert type(v).model_validate_json(v.model_dump_json()) == v
    assert _v(None).now_text() == "now n/a"


@given(
    spot=st.floats(50.0, 150.0, allow_nan=False),
    sma=st.floats(80.0, 120.0, allow_nan=False),
    atr=st.floats(0.5, 5.0, allow_nan=False),
    rearm=st.floats(0.0, 2.5),
    bull=st.booleans(),
)
def test_never_ready_while_stretched_past_the_bar(
    spot: float, sma: float, atr: float, rearm: float, bull: bool
) -> None:
    stance = "bullish" if bull else "bearish"
    rule = PullbackRule(target="sma20", rearm_stretch_atr=rearm)
    v = rearm_verdict(_watch(stance, sma20=sma, atr14=atr), spot, None, None, rule, today=TODAY)
    signed = (spot - sma) / atr * (1.0 if bull else -1.0)
    if signed > rearm + 1e-9:
        assert not v.ready
    # mirror: a bear at 2*sma - spot gets the same status as the bull at spot
    if bull:
        m = rearm_verdict(
            _watch("bearish", sma20=sma, atr14=atr), 2 * sma - spot, None, None, rule,
            today=TODAY,
        )  # fmt: skip
        assert m.status == v.status


@given(
    spot=st.floats(90.0, 110.0, allow_nan=False),
    poc=st.floats(90.0, 110.0, allow_nan=False),
    rearm=st.floats(0.0, 2.0),
)
def test_value_target_never_ready_far_above_its_level(
    spot: float, poc: float, rearm: float
) -> None:
    rule = PullbackRule(rearm_level_atr=rearm, invalidate_below_sma20_atr=-5.0)
    v = rearm_verdict(_watch(price_at_drop=200.0), spot, _tech(vp_poc=poc), None, rule, today=TODAY)
    if (spot - poc) / 2.0 > rearm + 1e-9:
        assert not v.ready


# ---------------------------------------------------------------------------
# config, registry, kinds, reasons
# ---------------------------------------------------------------------------


def test_shipped_config_is_on_with_the_d98_defaults() -> None:
    r = load_routines()
    assert r.pullback_watch == PullbackWatchSettings(enabled=True)
    assert r.pullback_watch.target == "value" and r.pullback_watch.ttl_sessions == 3
    assert RoutinesConfig().pullback_watch.enabled is False  # model default (no strategy.yaml)
    pol = r.context_policy("pullback_watch", "research")
    assert pol.ttl.sessions is not None and pol.ttl.sessions > 1 + 5  # outlives any watch
    research = r.personas["research"]
    assert "pullback_watch" in (research.reads or [])
    assert {"pullback_watch", "candidate"} <= set(research.writes or [])


@pytest.mark.parametrize(
    "data",
    [
        {"ttl_sessions": 0},
        {"ttl_sessions": 6},
        {"rearm_stretch_atr": 3.0},
        {"rearm_level_atr": -0.1},
        {"invalidate_below_sma20_atr": 0.5},
        {"target": "vwap"},
        {"max_ready_in_prompt": 11},
        {"bogus": 1},
    ],
)
def test_bad_config_is_refused(data: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        PullbackWatchSettings.model_validate(data)


def test_rule_carries_the_settings() -> None:
    cfg = PullbackWatchSettings(enabled=True, target="sma20", rearm_stretch_atr=0.5)
    r = cfg.rule(vwap=True)
    assert r.target == "sma20" and r.rearm_stretch_atr == 0.5 and r.vwap


def test_registry_keys_read_and_write_the_yaml() -> None:
    raw = yaml.safe_load(Path("config/strategy.yaml").read_text())
    assert lookup("pullback_watch").key == "personas.pullback_watch"  # alias
    assert read_raw(REGISTRY["personas.pullback_watch"], raw) == "on"
    assert write_raw(REGISTRY["personas.pullback_watch"], "off", raw) == [
        (("pullback_watch", "enabled"), False)
    ]
    assert read_raw(REGISTRY["pullback_watch.target"], raw) == "value"
    assert read_raw(REGISTRY["pullback_watch.rearm_level_atr"], raw) == 0.5
    keys = {k for k in REGISTRY if "pullback_watch" in k}
    assert keys == {
        "personas.pullback_watch",
        "pullback_watch.target",
        "pullback_watch.ttl_sessions",
        "pullback_watch.rearm_stretch_atr",
        "pullback_watch.rearm_level_atr",
        "pullback_watch.rearm_vwap_atr",
        "pullback_watch.invalidate_below_sma20_atr",
        "pullback_watch.max_ready_in_prompt",
    }
    for k in keys:
        t = REGISTRY[k]
        assert t.choices or (t.min is not None and t.max is not None), k
        if k != "personas.pullback_watch":  # every value matches the settings bounds
            PullbackWatchSettings.model_validate({k.split(".")[1]: read_raw(t, raw)})


def test_kinds_and_reason_labels() -> None:
    assert kind_spec("pullback_watch").schema_version == 1
    assert kind_spec("candidate").schema_version == 4
    old = {"ticker": "NVDA", "stance": "bullish", "catalyst_type": "technical",
           "confidence": 0.6, "created_at": "2026-10-09T10:00:00-04:00"}  # fmt: skip
    assert CandidatePayload.model_validate(old).origin == "feed"  # v3 rows still validate
    w = PullbackWatchPayload(
        ticker="NVDA", stance="bullish", dropped_at="x", drop_session="2026-10-09",
        expires_session="2026-10-14", confidence=0.6,
    )  # fmt: skip
    assert w.status == "watching"
    with pytest.raises(ValidationError):
        PullbackWatchPayload.model_validate({**w.model_dump(), "extra": 1})
    with pytest.raises(ValidationError):
        PullbackWatchPayload.model_validate({**w.model_dump(), "thesis": "x" * 401})
    for code in (
        ReasonCode.PULLBACK_WATCH_ADDED,
        ReasonCode.PULLBACK_REARMED,
        ReasonCode.PULLBACK_EXPIRED,
        ReasonCode.PULLBACK_INVALIDATED,
        ReasonCode.PULLBACK_RETIRED,
    ):
        assert REASON_LABELS[code]


def test_gate_never_imports_the_rule() -> None:
    for p in Path("arc/gate").rglob("*.py"):
        assert "pullback" not in p.read_text(), p


# ---------------------------------------------------------------------------
# the Research step (offline fixtures)
# ---------------------------------------------------------------------------

IDEAS = {"PLTR": "bullish", "NVDA": "bullish", "XOM": "bearish"}
MON = dt.datetime(2026, 9, 28, 10, 30, tzinfo=FIXTURE_NOW.tzinfo)  # two sessions later
# main (pre-E23.2) with the same two runs: anti-chase on (RSI 60), range edges off
MAIN_PROMPT_SHAS = ["14af3b71bfa04014429ab853ef248390c32521c57eef512fa53c1602d8b1720a"]
MAIN_JOURNAL_SHA = "19ac3330a53b28c10f1b4185abce331afdb74410cfa969b103fb4976437d4ea6"


def _routines(pullback: PullbackWatchSettings | None = None, **anti: Any) -> RoutinesConfig:
    return load_routines().model_copy(
        update={
            "anti_chase": AntiChaseSettings(enabled=True, rsi_overbought=60.0, **anti),
            "range_edges": RangeEdgesSettings(enabled=False),
            "pullback_watch": pullback or PullbackWatchSettings(enabled=True),
        }
    )


# Quant's pick from PLTR's menu (the scanner's first long call on the fixture chain);
# the pipeline matches it to the menu by legs and replaces the numbers
PLTR_LONG_CALL = {
    "ticker": "PLTR", "structure_type": "long_call",
    "legs": [{"occ_symbol": "PLTR261030C00190000", "side": "long", "ratio": 1,
              "strike": 190.0, "expiry": "2026-10-30", "option_type": "call"}],
    "net_debit_credit": 9.4, "max_gain": None, "max_loss": 940.0, "breakevens": [199.4],
    "greeks": {"delta": 53.0, "gamma": 0.0, "vega": 0.0, "theta": 0.0}, "dte": 32,
    "pop": 0.34, "ev_per_contract": 118.0, "cost_bps": 1.0, "confidence": 0.65,
    "rationale": "Fixture: long call from the PLTR menu after the pullback.",
}  # fmt: skip
PLTR_RISK = {
    "ticker": "PLTR", "structure_type": "long_call", "risk_rating": "moderate",
    "concentration_warning": False, "greek_budget_impact": "Fixture: long delta.",
    "calendar_concerns": "Fixture: none.", "sizing_suggestion": 1,
    "max_loss_pct_equity": 0.04, "narrative": "Fixture: long call, max loss = the debit.",
}  # fmt: skip


def _env(ideas: dict[str, str], quant: list[dict[str, Any]] | None = None) -> Any:
    from arc.ingest.llm import FixtureScalpLLM
    from arc.pipeline import PipelineEnv

    env = PipelineEnv.fixtures()
    if quant is not None:
        reply = {"structures": quant, "analysis_notes": "Fixture Quant reply (E23.2)."}
        env.llms["quant"] = FixtureScalpLLM([json.dumps(reply)])
        risk = {"assessments": [PLTR_RISK], "portfolio_summary": "Fixture: flat book.",
                "advisory_notes": "Fixture Risk reply (E23.2)."}  # fmt: skip
        env.llms["risk"] = FixtureScalpLLM([json.dumps(risk)] * 3)
    d = json.loads((FIXTURES / "research.json").read_text())
    base = d["shortlist"][1]
    d["shortlist"] = [
        {**base, "ticker": t, "stance": s, "rank": n + 1} for n, (t, s) in enumerate(ideas.items())
    ]
    d["excluded"] = []
    env.llms["research"] = FixtureScalpLLM([json.dumps(d)])
    return env


def _run(
    ideas: dict[str, str],
    routines: RoutinesConfig,
    *,
    conn: Any = None,
    now: dt.datetime = FIXTURE_NOW,
    profile: str = "cash_debit",
    quant: list[dict[str, Any]] | None = None,
) -> tuple[Any, Any]:
    from arc.config import ArcSettings
    from tests.test_e59_research_portfolio import _run as run

    settings = ArcSettings(_env_file=None, account_profile=profile)  # type: ignore[call-arg]
    return run(settings, routines, _env(ideas, quant), conn=conn, now=now)


def _codes(conn: Any, code: str) -> list[Any]:
    return conn.execute(
        "SELECT subject, choice, reason_text, payload FROM decisions WHERE reason_code=?"
        " ORDER BY rowid",
        (code,),
    ).fetchall()


def _watches(conn: Any) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT payload FROM context_entries WHERE kind='pullback_watch' ORDER BY rowid"
    )
    return [json.loads(r[0]) for r in rows]


def _last_prompt(conn: Any) -> str:
    return conn.execute(
        "SELECT prompt_text FROM persona_calls WHERE persona='research' ORDER BY rowid DESC"
    ).fetchone()[0]


def _shortlisted(conn: Any) -> list[str]:
    from tests.test_e59_research_portfolio import _shortlist

    return [i["ticker"] for i in _shortlist(conn)["shortlist"]]


@pytest.fixture
def pulled_back(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """PLTR's spot + today's technicals after a pullback (Mon): spot 179.5, POC 178."""
    import arc.data.base as base
    import arc.pipeline.steps as steps

    reg = json.loads(Path("tests/fixtures/regime/stored_regime_v2_spy.json").read_text())["regime"]
    state: dict[str, Any] = {
        "spot": 179.5,
        "tech": {"close": 179.5, "rsi14": 52.0, "vp_poc": 178.0, "vp_vah": 184.0},
        "label": None,
        "active": lambda ctx: ctx.now.date() > FIXTURE_NOW.date(),  # after the drop day
    }
    real_lv = steps._with_levels

    def fake_lv(ctx: Any, ticker: str, payload: Any, today: dt.date) -> dict[str, Any]:
        out = dict(real_lv(ctx, ticker, payload, today) or payload)
        if ticker == "PLTR" and state["active"](ctx) and out.get("technicals"):
            out["technicals"] = {**out["technicals"], **state["tech"]}
            if state["label"]:
                out["regime"] = {**(out.get("regime") or reg), "current": state["label"]}
        return out

    real_ref = base.reference_price

    def fake_ref(provider: Any, symbol: str, *, today: dt.date, **kw: Any) -> float | None:
        if symbol == "PLTR" and today > FIXTURE_NOW.date():
            return state["spot"]
        return real_ref(provider, symbol, today=today, **kw)

    monkeypatch.setattr(steps, "_with_levels", fake_lv)
    monkeypatch.setattr(base, "reference_price", fake_ref)
    return state


def _journal_sha(conn: Any) -> str:
    rows = [
        tuple(r)
        for r in conn.execute(
            "SELECT stage, subject, choice, reason_code, reason_text FROM decisions ORDER BY rowid"
        )
    ]
    return hashlib.sha256(json.dumps(rows).encode()).hexdigest()


def test_flag_off_is_byte_identical_to_main() -> None:
    r = _routines(PullbackWatchSettings(enabled=False))
    conn, _ = _run(IDEAS, r)
    conn, _ = _run(IDEAS, r, conn=conn, now=MON)
    prompts = conn.execute(
        "SELECT prompt_sha256 FROM persona_calls WHERE persona='research' ORDER BY rowid"
    ).fetchall()
    assert [p[0] for p in prompts] == MAIN_PROMPT_SHAS
    assert _journal_sha(conn) == MAIN_JOURNAL_SHA
    assert _watches(conn) == []
    assert "Pullback watch" not in _last_prompt(conn)


def test_stretched_drop_goes_on_watch_with_its_numbers() -> None:
    from tests.test_e59_research_portfolio import _outcome

    conn, report = _run(IDEAS, _routines())
    assert _shortlisted(conn) == ["NVDA", "XOM"]
    (row,) = _codes(conn, "pullback_watch_added")
    assert row["subject"] == "PLTR" and row["choice"] == "noted"
    assert row["reason_text"] == "bullish: watched through 2026-09-30 (3 sessions, target value)"
    (w,) = _watches(conn)
    assert w["status"] == "watching" and w["stance"] == "bullish"
    assert (w["drop_session"], w["expires_session"]) == ("2026-09-25", "2026-09-30")
    assert w["price_at_drop"] == pytest.approx(189.67)
    assert w["stretch_atr_at_drop"] == pytest.approx(2.918, abs=1e-3)
    assert w["rsi14_at_drop"] == pytest.approx(63.5, abs=0.1)
    assert w["research_call_id"] and w["thesis"].startswith("Fixture:")
    m = _outcome(report, "research").metrics
    assert m["pullback_added"] == 1 and m["pullback_ready"] == 0
    # the first run had nothing on watch yet: no section in its prompt
    assert "Pullback watch" not in _last_prompt(conn)


def test_a_live_watch_is_never_re_extended() -> None:
    conn, _ = _run(IDEAS, _routines())
    later = FIXTURE_NOW + dt.timedelta(minutes=40)
    conn, _ = _run(IDEAS, _routines(), conn=conn, now=later)
    assert len(_codes(conn, "pullback_watch_added")) == 1
    assert len(_watches(conn)) == 1
    # waiting (still stretched): one count line, no per-name line
    p = _last_prompt(conn)
    assert "Pullback watch: 1 waiting" in p and "Pullback watch (ready)" not in p


def test_pullback_rearms_becomes_a_candidate_and_reaches_quant(
    pulled_back: dict[str, Any],
) -> None:
    from tests.test_e59_research_portfolio import _outcome

    conn, _ = _run(IDEAS, _routines())
    # Mon: the feed has no PLTR idea; Research's fixture ranks PLTR from the ready line
    conn, report = _run(
        {"PLTR": "bullish"}, _routines(), conn=conn, now=MON, quant=[PLTR_LONG_CALL]
    )
    (row,) = _codes(conn, "pullback_rearmed")
    assert row["subject"] == "PLTR" and row["choice"] == "selected"
    assert row["reason_text"].startswith("bullish: ready: now 0.4 ATR above POC 178.00")
    p = json.loads(row["payload"])["pullback"]
    assert p["level"]["name"] == "POC" and p["target"] == "value"
    prompt = _last_prompt(conn)
    assert (
        "Pullback watch (ready): PLTR bullish · dropped Fri at +2.9 ATR / RSI 64 · "
        "now 0.4 ATR above POC 178.00, +0.3 ATR vs SMA20 · thesis: Fixture:"
    ) in prompt
    # a candidate row for today (proposals.candidate_id resolves) + a v4 entry
    cand = conn.execute(
        "SELECT id, sources FROM candidates WHERE ticker='PLTR' AND day='2026-09-28'"
    ).fetchone()
    assert cand is not None and json.loads(cand["sources"]) == ["pullback_watch:2026-09-25"]
    entry = conn.execute(
        "SELECT payload FROM context_entries WHERE kind='candidate' AND subject='PLTR'"
        " ORDER BY rowid DESC LIMIT 1"
    ).fetchone()
    assert json.loads(entry[0])["origin"] == "pullback_watch"
    # Research ranked it and anti-chase passed it (RSI 52: not stretched)
    assert _shortlisted(conn) == ["PLTR"]
    assert len(_codes(conn, "stretched_entry")) == 1  # Friday's only
    assert _outcome(report, "research").metrics["pullback_candidates"] == 1
    quant = conn.execute(
        "SELECT prompt_text FROM persona_calls WHERE persona='quant' ORDER BY rowid DESC"
    ).fetchone()[0]
    assert '"PLTR"' in quant and "long_call" in quant  # its menu reached Quant
    # the proposal links to the re-surfaced candidate row
    links = conn.execute(
        "SELECT candidate_id FROM proposals WHERE ticker='PLTR' ORDER BY rowid DESC"
    ).fetchall()
    assert links and links[0][0] == cand["id"]
    # the watch stays live (rearmed once) until it expires / is retired
    assert _watches(conn)[-1]["rearmed_at"] == MON.isoformat()


def test_a_ready_watch_already_a_candidate_gets_no_second_row(
    pulled_back: dict[str, Any],
) -> None:
    # same session: PLTR pulls back 40 minutes after the drop while still a feed candidate
    pulled_back["active"] = lambda ctx: ctx.now > FIXTURE_NOW
    conn, _ = _run(IDEAS, _routines())
    later = FIXTURE_NOW + dt.timedelta(minutes=40)
    import arc.data.base as base

    orig = base.reference_price
    base.reference_price = lambda p, s, *, today, **kw: (  # type: ignore[assignment]
        179.5 if s == "PLTR" else orig(p, s, today=today, **kw)
    )
    try:
        conn, _ = _run(IDEAS, _routines(), conn=conn, now=later)
    finally:
        base.reference_price = orig  # type: ignore[assignment]
    assert len(_codes(conn, "pullback_rearmed")) == 1
    rows = conn.execute("SELECT sources FROM candidates WHERE ticker='PLTR'").fetchall()
    assert len(rows) == 1 and "pullback_watch:" not in rows[0][0]
    assert "Pullback watch (ready): PLTR" in _last_prompt(conn)


def test_rearmed_is_journaled_once(pulled_back: dict[str, Any]) -> None:
    conn, _ = _run(IDEAS, _routines())
    conn, _ = _run({"PLTR": "bullish"}, _routines(), conn=conn, now=MON)
    conn, _ = _run({"PLTR": "bullish"}, _routines(), conn=conn, now=MON + dt.timedelta(minutes=40))
    assert len(_codes(conn, "pullback_rearmed")) == 1


def test_waiting_names_are_one_count_line(pulled_back: dict[str, Any]) -> None:
    pulled_back["spot"] = 186.0  # 2.1 ATR above POC 178 / 1.9 above VAH 184
    conn, _ = _run(IDEAS, _routines())
    conn, _ = _run({"PLTR": "bullish"}, _routines(), conn=conn, now=MON)
    # Mon: no feed ideas, only a waiting watch -> nothing to rank, no LLM call
    assert _codes(conn, "pullback_rearmed") == []
    assert (
        conn.execute("SELECT 1 FROM candidates WHERE ticker='PLTR' AND day='2026-09-28'").fetchone()
        is None
    )
    assert len(_codes(conn, "no_candidates")) == 1
    assert (
        conn.execute("SELECT count(*) FROM persona_calls WHERE persona='research'").fetchone()[0]
        == 1
    )
    assert _watches(conn)[-1]["status"] == "watching"  # still on watch


def test_invalidation_and_regime_flip_close_the_watch(pulled_back: dict[str, Any]) -> None:
    pulled_back["spot"] = 172.0  # (172 - 178.4) / 3.86 = -1.66 ATR < -1.0
    conn, _ = _run(IDEAS, _routines())
    conn, _ = _run({"PLTR": "bullish"}, _routines(), conn=conn, now=MON)
    (row,) = _codes(conn, "pullback_invalidated")
    assert row["subject"] == "PLTR" and "ATR below SMA20 (limit 1)" in row["reason_text"]
    assert _watches(conn)[-1]["status"] == "invalidated"
    # closed: never re-checked again
    conn, _ = _run({"PLTR": "bullish"}, _routines(), conn=conn, now=MON + dt.timedelta(hours=1))
    assert len(_codes(conn, "pullback_invalidated")) == 1

    pulled_back["spot"] = 179.5
    pulled_back["label"] = "bear"
    conn2, _ = _run(IDEAS, _routines())
    conn2, _ = _run({"PLTR": "bullish"}, _routines(), conn=conn2, now=MON)
    (row,) = _codes(conn2, "pullback_invalidated")
    assert "regime flipped to bear" in row["reason_text"]


def test_expiry_after_the_last_session(pulled_back: dict[str, Any]) -> None:
    pulled_back["spot"] = 186.0  # never ready
    conn, _ = _run(IDEAS, _routines())
    thu = dt.datetime(2026, 10, 1, 10, 30, tzinfo=FIXTURE_NOW.tzinfo)  # expires Wed 9-30
    conn, _ = _run({"PLTR": "bullish"}, _routines(), conn=conn, now=thu)
    (row,) = _codes(conn, "pullback_expired")
    assert row["reason_text"] == "bullish: expired: now n/a (watched through 2026-09-30)"
    assert _watches(conn)[-1]["status"] == "expired"


def test_open_position_or_pending_proposal_retires_the_watch(
    pulled_back: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    import arc.pipeline.steps as steps

    conn, _ = _run(IDEAS, _routines())
    monkeypatch.setattr(steps, "_pending_tickers", lambda c: {"PLTR"})
    conn, _ = _run({"PLTR": "bullish"}, _routines(), conn=conn, now=MON)
    (row,) = _codes(conn, "pullback_retired")
    assert row["reason_text"] == "bullish: open position or pending proposal on the ticker"
    assert _watches(conn)[-1]["status"] == "retired"
    assert _codes(conn, "pullback_rearmed") == []


def test_pending_tickers_reads_pending_approval_requests() -> None:
    import sqlite3

    import arc.pipeline.steps as steps

    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE approval_requests (ticker TEXT, status TEXT)")
    c.executemany(
        "INSERT INTO approval_requests VALUES (?, ?)",
        [("PLTR", "pending"), ("NVDA", "approved"), ("PLTR", "pending")],
    )
    assert steps._pending_tickers(c) == {"PLTR"}


def test_vwap_on_and_its_failure_waits(pulled_back: dict[str, Any]) -> None:
    # offline fixtures have no intraday bars: the VWAP read fails -> waiting, noted
    conn, _ = _run(IDEAS, _routines())
    conn, _ = _run({"PLTR": "bullish"}, _routines(vwap=True), conn=conn, now=MON)
    assert _codes(conn, "pullback_rearmed") == []
    notes = [r for r in _codes(conn, "vwap_missing") if r["subject"] == "PLTR"]
    assert len(notes) == 1 and notes[0]["reason_text"].startswith("pullback watch: ")
    assert _watches(conn)[-1]["status"] == "watching"


def test_margin_profile_and_neutral_ideas_are_untouched() -> None:
    conn, _ = _run({"PLTR": "neutral", "NVDA": "bullish"}, _routines(), profile="margin")
    assert _watches(conn) == []  # credit-capable: anti-chase passes, nothing to watch
    assert _shortlisted(conn) == ["PLTR", "NVDA"]
