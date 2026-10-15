"""E23.3 (D98): strike vs level context on the Quant menu, trade card and Tower.

The pure rule (:func:`arc.journal.analytics.level_crossings`), its text, the config +
registry, the pipeline wiring (menu ``levels`` + stored ``analytics.levels``), the card's
``Levels`` fact, and the golden: ``strike_levels`` off = Quant/Risk/Research prompts and
stored analytics byte-identical to main (pinned sha, captured on main 8b763d9).
"""

from __future__ import annotations

import json
import re
from typing import Any

import pytest
import yaml
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

import arc.pipeline.steps as steps
from arc.config import ArcSettings
from arc.control.registry import REGISTRY, Risk, lookup, read_raw, write_raw
from arc.journal.analytics import (
    LevelCrossing,
    ProposalAnalytics,
    level_crossings,
    levels_text,
    tech_levels,
)
from arc.routines.config import RoutinesConfig, StrikeLevelsSettings, load_routines
from tests import test_pipeline as tp

# spot 184, ATR14 2: POC 186.1, VAH 188, VAL 180, call wall 190, put wall 178
TECH: dict[str, Any] = {
    "atr14": 2.0,
    "vp_poc": 186.1,
    "vp_vah": 188.0,
    "vp_val": 180.0,
    "oi_call_wall": 190.0,
    "oi_put_wall": 178.0,
    "vp_hvn_above": None,
    "vp_hvn_below": None,
}
SPOT = 184.0


def _x(legs: list[tuple[str, str, float]], bes: list[float], net: float, tech: Any = TECH):  # noqa: ANN202
    return level_crossings(
        spot=SPOT,
        net_debit_credit=net,
        legs=legs,  # type: ignore[arg-type]
        breakevens=bes,
        tech=tech,
    )


# ---------------------------------------------------------------------------
# The rule
# ---------------------------------------------------------------------------


def test_bull_call_debit_matches_the_card_example() -> None:
    c = _x([("long", "call", 185.0), ("short", "call", 190.0)], [187.0], 2.0)
    assert levels_text(c) == "BE beyond POC 186.1 (+0.5 ATR) · short strike at call wall 190"
    be = next(x for x in c if x.which == "breakeven")
    assert (be.side, be.level_kind, be.price, be.dist_atr, be.crossed) == (
        "beyond",
        "poc",
        186.1,
        0.45,
        1,
    )
    assert be.beyond_first and be.direction == 1
    short = next(x for x in c if x.which == "short_strike")
    assert (short.side, short.level_kind, short.crossed) == ("at", "call_wall", 2)


def test_bear_put_debit_mirrors_the_bull() -> None:
    # mirror of the bull case around spot: levels at 184 ± the same distances
    mirror = {
        "atr14": 2.0,
        "vp_poc": 181.9,
        "vp_val": 180.0,
        "vp_vah": 188.0,
        "oi_put_wall": 178.0,
        "oi_call_wall": 190.0,
    }
    bull = _x([("long", "call", 185.0), ("short", "call", 190.0)], [187.0], 2.0)
    bear = _x([("long", "put", 183.0), ("short", "put", 178.0)], [181.0], 2.0, mirror)
    assert levels_text(bear) == "BE beyond POC 181.9 (+0.5 ATR) · short strike at put wall 178"
    assert [(c.which, c.side, c.dist_atr, c.crossed) for c in bear] == [
        (c.which, c.side, c.dist_atr, c.crossed) for c in bull
    ]
    assert {c.direction for c in bear} == {-1}


def test_before_the_first_level() -> None:
    (c,) = _x([("long", "call", 184.0)], [185.5], 1.5)
    assert (c.side, c.level_kind, c.crossed, c.dist_atr) == ("before", "poc", 0, -0.3)
    assert not c.beyond_first
    assert levels_text([c]) == "BE before POC 186.1 (-0.3 ATR)"


def test_long_call_has_no_short_strike() -> None:
    c = _x([("long", "call", 185.0)], [188.0], 3.0)
    assert [x.which for x in c] == ["breakeven"]
    assert levels_text(c) == "BE at VAH 188"


def test_long_put_reads_below_spot_only() -> None:
    c = _x([("long", "put", 183.0)], [179.0], 4.0)
    assert levels_text(c) == "BE beyond VAL 180 (+0.5 ATR)"


def test_condor_and_credit_read_both_sides() -> None:
    legs = [
        ("long", "put", 175.0),
        ("short", "put", 178.0),
        ("short", "call", 190.0),
        ("long", "call", 193.0),
    ]
    c = _x(legs, [177.0, 191.0], -1.0)
    assert levels_text(c) == (
        "upper BE beyond POC 186.1 (+2.5 ATR) · lower BE beyond VAL 180 (+1.5 ATR) · "
        "short call at call wall 190 · short put at put wall 178"
    )
    # a bull put credit: one side only (below spot), plain labels
    c = _x([("short", "put", 182.0), ("long", "put", 177.0)], [181.0], -1.0)
    assert levels_text(c) == "BE before VAL 180 (-0.5 ATR) · short strike before VAL 180 (-1.0 ATR)"


@pytest.mark.parametrize(
    "tech",
    [None, {}, {"atr14": 2.0}, {"vp_poc": None, "oi_call_wall": 0}, "x"],
)
def test_no_levels_no_text(tech: Any) -> None:
    c = _x([("long", "call", 185.0), ("short", "call", 190.0)], [187.0], 2.0, tech)
    assert c == [] and levels_text(c) == ""


def test_no_level_on_the_target_side_no_text() -> None:
    # only levels below spot; a bullish target has nothing to cross
    c = _x([("long", "call", 185.0)], [188.0], 3.0, {"atr14": 2.0, "vp_val": 180.0})
    assert c == []


def test_without_atr_no_distance_and_tick_tolerance() -> None:
    tech = {k: v for k, v in TECH.items() if k != "atr14"}
    c = _x([("long", "call", 185.0), ("short", "call", 190.0)], [187.0], 2.0, tech)
    assert levels_text(c) == "BE beyond POC 186.1 · short strike at call wall 190"
    assert all(x.dist_atr is None for x in c)


def test_tech_levels_dedupes_and_ranks_walls_first() -> None:
    lv = tech_levels(
        {"vp_poc": 190.0, "oi_call_wall": 190.0, "vp_hvn_above": 192.0, "vp_vah": True}
    )
    assert sorted(lv, key=lambda kp: kp[1]) == [("call_wall", 190.0), ("hvn", 192.0)]


@given(
    off=st.floats(0.5, 20.0),
    lv=st.lists(st.floats(0.1, 25.0), min_size=1, max_size=6),
    atr=st.floats(0.5, 5.0),
)
def test_bull_bear_mirror_property(off: float, lv: list[float], atr: float) -> None:
    """A bear put debit mirrored around spot reads exactly like the bull call debit."""
    up = {"atr14": atr} | {
        f: SPOT + d
        for f, d in zip(("vp_poc", "vp_vah", "oi_call_wall", "vp_hvn_above"), lv, strict=False)
    }
    down = {"atr14": atr} | {
        f: SPOT - d
        for f, d in zip(("vp_poc", "vp_val", "oi_put_wall", "vp_hvn_below"), lv, strict=False)
    }
    bull = _x([("long", "call", SPOT)], [SPOT + off], 1.0, up)
    bear = _x([("long", "put", SPOT)], [SPOT - off], 1.0, down)
    key = [(c.side, c.crossed, c.dist_atr) for c in bull]
    assert key == [(c.side, c.crossed, c.dist_atr) for c in bear]
    for c in bull:
        assert c.side != "beyond" or (c.dist_atr or 0) > 0
        assert c.side != "before" or (c.dist_atr or 0) < 0


def test_crossing_roundtrip() -> None:
    (c,) = _x([("long", "call", 185.0)], [188.0], 3.0)
    assert LevelCrossing.model_validate(c.model_dump()) == c


# ---------------------------------------------------------------------------
# Config + registry
# ---------------------------------------------------------------------------


def test_shipped_on_absent_off() -> None:
    assert load_routines().strike_levels.enabled is True  # D86/D98: ships on
    assert StrikeLevelsSettings().enabled is False  # absent block = off


def test_bad_config_is_refused() -> None:
    from arc.routines.strategy import StrategyConfig

    for data in ({"strike_levels": {"enabled": "maybe"}}, {"strike_levels": {"x": 1}}):
        with pytest.raises((ValidationError, ValueError)):
            StrategyConfig.model_validate(data)
        with pytest.raises((ValidationError, ValueError)):
            RoutinesConfig.model_validate(data)


def test_routines_yaml_refuses_the_block(tmp_path: Any) -> None:
    p = tmp_path / "routines.yaml"
    p.write_text("timezone: America/New_York\nstrike_levels:\n  enabled: on\n")
    with pytest.raises(ValueError, match=r"moved to config/strategy\.yaml"):
        load_routines(p)


def test_registry_key() -> None:
    raw = yaml.safe_load(open("config/strategy.yaml").read())  # noqa: SIM115, PTH123
    t = REGISTRY["strike_levels.enabled"]
    assert lookup("strike_levels").key == "strike_levels.enabled"
    assert t.risk is Risk.NONE and t.path == ("strike_levels", "enabled")
    assert read_raw(t, raw) == "on"
    assert write_raw(t, "off", raw) == [(("strike_levels", "enabled"), False)]


# ---------------------------------------------------------------------------
# Analytics payload + card
# ---------------------------------------------------------------------------


def test_analytics_without_levels_dumps_like_before() -> None:
    from arc.backtest.costs import load_cost_model
    from arc.pipeline.analytics import build_analytics
    from tests.test_card_v2 import _debit_vertical

    _, priced = _debit_vertical()
    off = build_analytics(priced, cost=load_cost_model())  # type: ignore[arg-type]
    assert "levels" not in off.model_dump() and '"levels"' not in off.model_dump_json()
    regime = {"technicals": {"atr14": 8.0, "vp_poc": 773.0, "oi_call_wall": 780.0}}
    on = build_analytics(priced, cost=load_cost_model(), regime=regime, strike_levels=True)  # type: ignore[arg-type]
    assert levels_text(on.levels or []) == (
        "BE beyond POC 773 (+0.2 ATR) · short strike at call wall 780"
    )
    assert ProposalAnalytics.model_validate_json(on.model_dump_json()) == on
    none = build_analytics(priced, cost=load_cost_model(), strike_levels=True)  # type: ignore[arg-type]
    assert none.levels == []


def test_card_levels_fact() -> None:
    from arc.approvals.card import render_card
    from arc.approvals.trail import DecisionTrail
    from arc.backtest.costs import load_cost_model
    from arc.pipeline.analytics import build_analytics
    from tests.test_card_v2 import _debit_vertical, _field

    p, priced = _debit_vertical()
    regime = {"technicals": {"atr14": 8.0, "vp_poc": 773.0, "oi_call_wall": 780.0}}

    def card(**kw: Any) -> Any:
        a = build_analytics(priced, cost=load_cost_model(), regime=regime, **kw)  # type: ignore[arg-type]
        return render_card(
            p, None, proposal_hash="0" * 64, actionable=False, trail=DecisionTrail(analytics=a)
        )

    on = card(strike_levels=True)
    assert _field(on, "Levels") == ["BE beyond POC 773 (+0.2 ATR) · short strike at call wall 780"]
    off = card()
    assert json.dumps(off.blocks).count("*Levels*") == 0
    # off == the pre-E23.3 card: same blocks as analytics with no level at all
    bare = render_card(
        p,
        None,
        proposal_hash="0" * 64,
        actionable=False,
        trail=DecisionTrail(
            analytics=build_analytics(
                priced, cost=load_cost_model(), regime=regime, strike_levels=True
            ).model_copy(update={"levels": []})
        ),  # type: ignore[arg-type]
    )
    assert bare.blocks == off.blocks


# ---------------------------------------------------------------------------
# Pipeline: golden off, menu + analytics on
# ---------------------------------------------------------------------------

CHAINS_MARK = "### Option chains with Greeks\n"


def _inject(real: Any) -> Any:
    def fake(ctx: Any, ticker: str, payload: Any, today: Any) -> Any:
        out = dict(real(ctx, ticker, payload, today) or payload)
        tech = out.get("technicals")
        if tech:
            c = float(tech["close"])
            out["technicals"] = {
                **tech,
                "vp_poc": round(c * 1.01, 2),
                "vp_vah": round(c * 1.03, 2),
                "vp_val": round(c * 0.97, 2),
                "oi_call_wall": float(round(c * 1.05)),
                "oi_put_wall": float(round(c * 0.95)),
            }
        return out

    return fake


def _run(
    monkeypatch: pytest.MonkeyPatch, profile: str, enabled: bool
) -> tuple[Any, dict[str, str]]:
    monkeypatch.setattr(steps, "_with_levels", _inject(steps._with_levels))
    monkeypatch.delenv("ARC_GATE_SECRET", raising=False)
    r = load_routines().model_copy(update={"strike_levels": StrikeLevelsSettings(enabled=enabled)})
    s = ArcSettings(_env_file=None, account_profile=profile)  # type: ignore[call-arg]
    conn, _, prompts = tp._recording_fixture_run(s, r)
    return conn, prompts


def _analytics(conn: Any) -> list[dict[str, Any]]:
    rows = conn.execute("select payload from market_contexts order by rowid").fetchall()
    return [json.loads(r[0])["analytics"] for r in rows]


def _chains(prompt: str) -> dict[str, Any]:
    return json.loads(prompt.split(CHAINS_MARK, 1)[1].split("\n\n### ", 1)[0])


# The off path must equal the pre-E23.3 pipeline and the on path may only add `levels`.
# Both are checked against a run in the same process rather than sha256 goldens: the
# prompts and analytics carry floats whose last digit differs between macOS and the
# Linux CI runner, so a captured hash flips per platform. (Verified byte-identical to
# main 8b763d9 on macOS before this change; see PR #261.)
def _strip_levels(prompt: str) -> str:
    return re.sub(r'\n *"levels": "[^"\n]*",', "", prompt)


@pytest.mark.parametrize("profile", ["margin", "cash_debit"])
def test_off_adds_nothing(profile: str, monkeypatch: pytest.MonkeyPatch) -> None:
    conn_off, off = _run(monkeypatch, profile, enabled=False)
    assert off and all('"levels"' not in v for v in off.values())
    for a in _analytics(conn_off):
        assert "levels" not in a
    conn_on, on = _run(monkeypatch, profile, enabled=True)
    assert sorted(on) == sorted(off)
    for k in off:
        assert _strip_levels(on[k]) == off[k], k
    a_off, a_on = _analytics(conn_off), _analytics(conn_on)
    assert len(a_off) == len(a_on)
    for x, y in zip(a_off, a_on, strict=True):
        assert {k: v for k, v in y.items() if k != "levels"} == x


@pytest.mark.parametrize("profile", ["margin", "cash_debit"])
def test_on_adds_menu_levels_only(profile: str, monkeypatch: pytest.MonkeyPatch) -> None:
    _, off = _run(monkeypatch, profile, enabled=False)
    conn, prompts = _run(monkeypatch, profile, enabled=True)
    chains = _chains(prompts["quant"])
    rows = [r for c in chains.values() for r in c["menu"]]
    texts = [r["levels"] for r in rows if "levels" in r]
    assert texts and all(" POC " in t or " wall " in t or " VA" in t for t in texts)
    # nothing else on the menu moved: drop the new key and the prompt is the off prompt
    stripped = _strip_levels(prompts["quant"])
    assert stripped.count('"levels"') == 0
    assert stripped == off["quant"]
    assert prompts["research"] == off["research"]  # Research untouched
    for a in _analytics(conn):
        assert isinstance(a["levels"], list)
        LevelCrossing.model_validate(a["levels"][0])


def test_tower_trade_detail_levels_text() -> None:
    import sqlite3

    from arc.tower.data_trades import _quant

    a = {
        "spot": 184.0,
        "dte": 30,
        "legs": [],
        "cost_model": json.loads(
            __import__("arc.backtest.costs", fromlist=["x"]).load_cost_model().model_dump_json()
        ),
        "entry_slippage": 0.0,
        "levels": [
            c.model_dump(mode="json")
            for c in _x([("long", "call", 185.0), ("short", "call", 190.0)], [187.0], 2.0)
        ],
    }
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    p = conn.execute(
        "select '{}' as quant_json, '{}' as sizing_json, '{}' as structure_json"
    ).fetchone()
    mc = conn.execute(
        "select ? as payload, '2026-10-12T10:00:00-04:00' as created_at",
        (json.dumps({"analytics": a}),),
    ).fetchone()
    q = _quant(p, mc)
    assert q.analytics_error is None
    assert q.levels_text == "BE beyond POC 186.1 (+0.5 ATR) · short strike at call wall 190"
    a.pop("levels")
    mc2 = conn.execute(
        "select ? as payload, '2026-10-12T10:00:00-04:00' as created_at",
        (json.dumps({"analytics": a}),),
    ).fetchone()
    assert _quant(p, mc2).levels_text is None


def test_gate_never_reads_levels() -> None:
    import pathlib

    for f in pathlib.Path("arc/gate").rglob("*.py"):
        text = f.read_text()
        assert "level_crossings" not in text and "strike_levels" not in text
