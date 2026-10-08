"""E14.5 (D60): Reddit mention velocity in retail_buzz, the trending ranker and the Scout.

- parse: ApeWisdom ``mentions_24h_ago`` / ``upvotes`` stored (and absent -> None);
  a v1 retail_buzz row still loads;
- ``mention_velocity``: smoothing, ``min_mentions``, a missing 24 h count never counts
  as zero; hypothesis properties;
- ranker parity: ``scoring: rank_gain`` reproduces the pre-E14.5 tier byte for byte;
  the ``velocity`` arm promotes fast risers;
- the flags: ``universe.trending.scoring`` and ``personas.scout_buzz_velocity`` in the
  registry (choices, default control), the XP-10 draft, the arm owns the trending job;
- the Scout lines with the flag on / off, the golden prompt, the handler wiring;
- the Tower ``/api/ops/universe`` trending member carries its velocity.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest import mock

import pytest
import yaml
from hypothesis import given
from hypothesis import strategies as st

from arc.context.kinds import RetailBuzzPayload
from arc.ingest.retail_buzz import parse_apewisdom
from arc.personas.scout import BuzzVelocity, retail_buzz_lines, retail_buzz_view
from arc.universe.trending import (
    TrendingOptions,
    VelocityOptions,
    apewisdom_scores,
    buzz_velocities,
    fastest_risers,
    format_velocity,
    mention_velocity,
    velocity_text,
)
from tests import scout_prompt_golden as golden
from tests import trending_velocity_fixture as fx

if TYPE_CHECKING:
    import sqlite3

REPO = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures"
V = VelocityOptions()


# -- parse ------------------------------------------------------------------------------


def test_parse_apewisdom_keeps_the_24h_counts() -> None:
    page = {
        "results": [
            {"rank": 3, "ticker": "APLD", "name": "Applied Blockchain", "mentions": 182,
             "upvotes": 493, "rank_24h_ago": 20, "mentions_24h_ago": 27},
            {"rank": 4, "ticker": "NVDA", "name": "NVIDIA", "mentions": "89",
             "rank_24h_ago": None},
        ]
    }  # fmt: skip
    a, n = parse_apewisdom([page])
    assert (a.mentions, a.mentions_24h_ago, a.upvotes, a.rank_24h_ago) == (182, 27, 493, 20)
    # an older payload / a row without the counts -> None, never 0
    assert n.mentions == 89.0 and n.mentions_24h_ago is None and n.upvotes is None


def test_v1_retail_buzz_row_still_loads() -> None:
    payload = fx.buzz(with_counts=False).model_dump(mode="json")
    row = payload["inputs"]["reddit"]["rows"][0]
    row.pop("mentions_24h_ago", None)
    row.pop("upvotes", None)
    buzz = RetailBuzzPayload.model_validate(payload)
    assert buzz.inputs["reddit"].rows[0].mentions_24h_ago is None


def test_retail_buzz_schema_is_v2() -> None:
    from arc.context.kinds import KINDS

    assert KINDS["retail_buzz"].schema_version == 2
    schema = json.loads((REPO / "schemas/context/retail_buzz.v2.json").read_text())
    assert "mentions_24h_ago" in json.dumps(schema)
    assert not (REPO / "schemas/context/retail_buzz.v1.json").exists()


# -- mention_velocity --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("m", "m24", "want"),
    [
        (157, 30, round(162 / 35, 6)),  # APLD 2026-10-07: 4.6x
        (12, 3, 2.125),  # smoothing: a 3 -> 12 jump reads 2.1x, not 4x
        (10, 0, 3.0),  # zero yesterday is finite (k), not infinite
        (383, 247, round(388 / 252, 6)),
        (82, 123, round(87 / 128, 6)),  # falling -> below 1
        (9, 1, None),  # under min_mentions 10
        (50, None, None),  # missing 24 h count: never treated as 0
        (None, 5, None),
    ],
)
def test_mention_velocity_cases(m: float | None, m24: float | None, want: float | None) -> None:
    assert mention_velocity(m, m24, V) == want


def test_min_mentions_and_smoothing_are_knobs() -> None:
    assert mention_velocity(9, 1, VelocityOptions(min_mentions=0)) == round(14 / 6, 6)
    assert mention_velocity(12, 3, VelocityOptions(smoothing=1)) == round(13 / 4, 6)
    with pytest.raises(ValueError, match="greater than 0"):
        VelocityOptions(smoothing=0)


@given(
    m=st.floats(min_value=10, max_value=1e6, allow_nan=False),
    m24=st.floats(min_value=0, max_value=1e6, allow_nan=False),
    k=st.floats(min_value=0.1, max_value=100, allow_nan=False),
)
def test_velocity_properties(m: float, m24: float, k: float) -> None:
    opts = VelocityOptions(smoothing=k)
    v = mention_velocity(m, m24, opts)
    assert v is not None and v > 0
    # bounded: never more than (m + k) / k, so a 0 -> m jump is finite
    assert v <= (m + k) / k + 1e-6
    # monotone: more mentions today never lowers it, more yesterday never raises it
    assert (mention_velocity(m + 1, m24, opts) or 0) >= v - 1e-6
    assert (mention_velocity(m, m24 + 1, opts) or 0) <= v + 1e-6
    # direction: > 1 exactly when mentions grew
    if m > m24:
        assert v >= 1 - 1e-6
    elif m < m24:
        assert v <= 1 + 1e-6


def test_format_velocity() -> None:
    assert format_velocity(4.628571) == "4.6×"
    assert format_velocity(0.67) == "0.7×"
    assert format_velocity(42.5) == "42×"
    assert velocity_text((4.628571, 157.0, 30.0)) == "4.6× (157 vs 30)"
    assert velocity_text((None, 9.0, 1.0)) is None and velocity_text(None) is None


# -- the ranker ----------------------------------------------------------------------------


def test_rank_gain_is_byte_identical_to_the_pre_e14_5_tier() -> None:
    """Parity: the default scoring on the fixture reproduces the tier + table stored
    before E14.5 (tests/fixtures/trending/parity_rank_gain.json), with or without the
    new 24 h counts on the rows."""
    want = (FIXTURES / "trending" / "parity_rank_gain.json").read_text()
    assert TrendingOptions().scoring == "rank_gain"
    assert fx.render(with_counts=False) == want
    assert fx.render(with_counts=True) == want
    assert fx.render(opts=TrendingOptions(scoring="rank_gain")) == want


def _tickers(out: dict[str, Any]) -> list[str]:
    return [m["ticker"] for m in out["payload"]["members"]]


def test_velocity_arm_promotes_fast_risers() -> None:
    old = _tickers(fx.ranked())
    new = _tickers(fx.ranked(TrendingOptions(scoring="velocity")))
    assert len(old) == len(new) == 12
    # SPY / AAPL stay excluded and leveraged SOXL stays out in both arms
    assert not {"SPY", "AAPL", "SOXL"} & (set(old) | set(new))
    assert old == ["APLD", "RGTI", "MU", "GME", "OPEN", "NVDA", "SMR", "BULL", "TSLA",
                   "RKLB", "AMD", "PLTR"]  # fmt: skip
    assert new == ["APLD", "RGTI", "GME", "MU", "OPEN", "SMR", "NVDA", "BULL", "RKLB",
                   "PLTR", "SOFI", "TSLA"]  # fmt: skip
    # falling names (NVDA 82 vs 123, TSLA 80 vs 95) rank lower; risers (SMR, RKLB) higher
    assert new.index("NVDA") > old.index("NVDA") and new.index("TSLA") > old.index("TSLA")
    assert new.index("SMR") < old.index("SMR") and new.index("RKLB") < old.index("RKLB")
    # both-inputs-first is unchanged: the first 7 are the same set
    assert set(new[:7]) == set(old[:7])
    # deterministic
    assert fx.ranked(TrendingOptions(scoring="velocity")) == fx.ranked(
        TrendingOptions(scoring="velocity")
    )


def test_velocity_half_is_zero_without_a_24h_count() -> None:
    reddit = fx.buzz().inputs["reddit"]
    res = apewisdom_scores("reddit", reddit, scoring="velocity", velocity=V)
    gain = apewisdom_scores("reddit", reddit)
    # IONQ (no 24 h count) and OKLO (none either) keep only their mentions half
    assert res.raw["IONQ"] < gain.raw["IONQ"]
    assert set(res.raw) == set(gain.raw)  # nobody dropped from the input


def test_buzz_velocities_and_fastest_risers() -> None:
    buzz = fx.buzz()
    vel = buzz_velocities(buzz, V)
    assert vel["APLD"] == (round(162 / 35, 6), 157, 30)
    assert vel["IONQ"][0] is None and vel["LCID"][0] is None  # no count / under 10
    risers = fastest_risers(buzz, V, top=5, stop_words=["OPEN"])
    syms = [r[0] for r in risers]
    assert syms[0] == "BULL"
    assert "SOXL" not in syms  # leveraged fund
    assert "OPEN" not in syms  # a word ticker (stop_words)
    assert all(r[1] > 1 for r in risers)  # only rising names
    assert [r[1] for r in risers] == sorted((r[1] for r in risers), reverse=True)
    assert len(risers) == 5
    assert fastest_risers(None, V) == []
    assert buzz_velocities(None, V) == {}


# -- registry / config / experiment --------------------------------------------------------


def test_flags_in_the_registry_default_control() -> None:
    from arc.control.registry import lookup, read_raw
    from arc.routines.config import load_routines

    raw = yaml.safe_load((REPO / "config/routines.yaml").read_text())
    s = lookup("universe.trending.scoring")
    assert s.choices == ("rank_gain", "velocity") and read_raw(s, raw) == "rank_gain"
    f = lookup("personas.scout_buzz_velocity")
    assert f.choices == ("off", "on") and read_raw(f, raw) == "off"
    r = load_routines()
    assert r.scout_buzz_velocity.enabled is False
    job = r.job("universe.trending")
    assert job is not None
    opts = TrendingOptions.from_options(job[1].options)
    assert opts.scoring == "rank_gain" and opts.velocity == VelocityOptions()
    on = load_routines(
        overrides={
            ("personas", "scout_buzz_velocity"): "on",
            ("sources", "universe.trending", "scoring"): "velocity",
        }
    )
    job = on.job("universe.trending")
    assert job is not None
    assert on.scout_buzz_velocity.enabled is True
    assert TrendingOptions.from_options(job[1].options).scoring == "velocity"


def test_xp10_draft_owns_the_trending_job_and_the_scout() -> None:
    from arc.experiments.config import RunnerConfig
    from arc.experiments.models import ExperimentSpec
    from arc.experiments.runner import arm_owned_personas, arm_plan, persona_jobs
    from arc.routines.config import load_routines

    spec = ExperimentSpec.model_validate(
        yaml.safe_load((REPO / "config/experiments/live/xp10_trending_velocity.yaml").read_text())
    )
    assert spec.id == "XP-10"
    ov = spec.arms.treatment.overlay
    assert ov == {"routines": {"sources": {"universe.trending": {"scoring": "velocity"}}}}
    assert arm_owned_personas(ov) == {"trending", "scout"}
    assert arm_owned_personas({"routines": {"personas": {"scout_buzz_velocity": "on"}}}) == {
        "scout"
    }
    routines = load_routines(overrides={("sources", "universe.trending", "scoring"): "velocity"})
    plan = arm_plan(routines, ov, RunnerConfig())
    assert plan.fork_step == "research"
    assert persona_jobs(plan.arm_personas) == ["scout", "universe.trending"]
    assert {"universe.trending", "scout"} <= set(plan.own_producers)


# -- the Scout -----------------------------------------------------------------------------


def test_view_flag_off_is_unchanged() -> None:
    view = retail_buzz_view(golden.buzz().model_dump(), trending=golden.TRENDING)
    assert view.risers is None
    assert all(n.velocity is None for n in view.names)
    assert not any("velocity" in line or "risers" in line for line in retail_buzz_lines(view))


def test_view_flag_on_adds_velocity_and_risers() -> None:
    vel = BuzzVelocity(options=V, stop_words=("YOLO",))
    view = retail_buzz_view(golden.buzz().model_dump(), trending=golden.TRENDING, velocity=vel)
    by = {n.ticker: n for n in view.names}
    assert (
        by["RKLB"]
        .line()
        .startswith("- RKLB · reddit #2/640 mentions · velocity 4.2× (640 vs 150) · stocktwits #1")
    )
    # SOFI has no 24 h count: no velocity shown, not a riser
    assert by["SOFI"].velocity is None and "velocity" not in by["SOFI"].line()
    assert by["OKLO"].line().startswith("- OKLO · reddit — · stocktwits #5")
    assert view.risers == ["ACHR 5.0× (120 vs 20)", "RKLB 4.2× (640 vs 150)",
                           "GME 1.1× (1,234 vs 1,100)"]  # fmt: skip
    # order is the control ranking in both cases
    off = retail_buzz_view(golden.buzz().model_dump(), trending=golden.TRENDING)
    assert [n.ticker for n in view.names] == [n.ticker for n in off.names]
    assert retail_buzz_lines(view)[-1].startswith("Fastest risers (Reddit mention velocity")


def test_prompt_golden_with_velocity() -> None:
    assert (
        golden.prompt(with_buzz=True, velocity=True)
        == (FIXTURES / "scout" / "prompt_buzz_velocity.txt").read_text()
    )


@pytest.fixture
def _stop_patches() -> Any:
    yield
    mock.patch.stopall()


@pytest.mark.usefixtures("_stop_patches")
def test_handler_passes_velocity_only_with_the_flag(tmp_path: Path) -> None:
    from arc.routines.config import ScoutBuzzVelocitySettings
    from arc.routines.handlers import scout_persona
    from tests.test_scout_persona import FakeLLM, _ctx, _guard, _routines, _seed, _settings
    from tests.test_scout_retail_buzz import _write_buzz

    def run(flag: str) -> str:
        from arc.store.db import connect
        from arc.store.migrate import migrate

        db: sqlite3.Connection = connect(tmp_path / f"{flag}.db")
        migrate(db)
        _seed(db)
        _write_buzz(db)
        db.commit()
        routines = _routines().model_copy(
            update={"scout_buzz_velocity": ScoutBuzzVelocitySettings(enabled=flag == "on")}
        )
        settings = _settings()
        llm = FakeLLM()
        scout_persona(_ctx(db, routines, settings), llm=llm, guard=_guard(db, settings))
        db.close()
        return llm.prompts[0]

    off, on = run("off"), run("on")
    assert "velocity" not in off and "Fastest risers" not in off
    assert "- RKLB · reddit #2/640 mentions · velocity 4.2× (640 vs 150)" in on
    assert "Fastest risers (Reddit mention velocity = (mentions + 5)" in on


# -- the Tower --------------------------------------------------------------------------------


def test_tower_trending_member_carries_velocity(tmp_path: Path) -> None:
    from arc.config import ArcSettings
    from arc.context.store import ContextStore
    from arc.context.ttl import Ttl
    from arc.store.db import connect
    from arc.store.migrate import migrate
    from arc.tower.data import connect_ro
    from arc.tower.data_universe import load_universe
    from arc.universe.tiers import Tier, TierMember, UniverseTierPayload, resolve_active

    now = fx.NOW + dt.timedelta(minutes=30)
    day = now.date()
    p = tmp_path / "arc.db"
    c = connect(p)
    migrate(c)
    store = ContextStore(c)
    ttl = Ttl(duration=dt.timedelta(hours=20))

    def m(t: str, tier: Tier, rank: int) -> TierMember:
        return TierMember(ticker=t, tier=tier, rank=rank, source="x", reason="r", as_of=day)

    trending = [m("APLD", Tier.TRENDING, 1), m("IONQ", Tier.TRENDING, 2)]
    at = fx.NOW
    store.write(kind="retail_buzz", subject="all", payload=fx.buzz(), produced_by="retail_buzz",
                ttl=ttl, valid_from=at - dt.timedelta(minutes=10), now=at)  # fmt: skip
    store.write(
        kind="universe_tier", subject="trending", produced_by="universe.trending", ttl=ttl,
        payload=UniverseTierPayload(tier=Tier.TRENDING, fetched_at=at, source="x",
                                    members=trending),
        valid_from=at, now=at,
    )  # fmt: skip
    active = resolve_active(
        core=[m("NVDA", Tier.CORE, 1)], trending=trending, active_max=10, as_of=day
    )
    store.write(kind="active_universe", subject="active", payload=active, produced_by="t",
                ttl=ttl, valid_from=at, now=at)  # fmt: skip
    c.commit()
    c.close()
    ro = connect_ro(p)
    try:
        r = load_universe(ro, ArcSettings(), now=now)
    finally:
        ro.close()
    by = {a.ticker: a for a in r.active}
    assert by["APLD"].velocity == round(162 / 35, 6)
    assert by["APLD"].velocity_detail == "reddit #3 · 4.6× (157 vs 30)"
    assert by["IONQ"].velocity is None and by["IONQ"].velocity_detail is None  # no 24 h count
    assert by["NVDA"].velocity is None  # only trending members carry it


def test_cli_scoring_is_dry_run_only(capsys: pytest.CaptureFixture[str]) -> None:
    import argparse

    from arc.universe.cli import add_universe_parser, run_universe

    ap = argparse.ArgumentParser()
    add_universe_parser(ap.add_subparsers(dest="cmd"))
    args = ap.parse_args(["universe", "trending", "--scoring", "velocity", "--json"])
    assert args.scoring == "velocity"
    assert run_universe(args) == 2
    assert "--scoring are only valid with --dry-run" in capsys.readouterr().out
