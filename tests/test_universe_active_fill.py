"""E14.9 (D67): active-list round-robin fill Discovery ⇄ Trending with spill.

After core + momentum, the ``open_slots`` left under the cap go D1, T1, D2, T2, …
(Discovery first and takes the odd slot); a tier that runs out spills its slots to
the other. ``universe.active_fill: precedence`` reproduces the D58 cut exactly.
"""

from __future__ import annotations

import datetime as dt

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from arc.control.registry import Risk, Target, ValueType, lookup, parse_value
from arc.universe.config import UniverseConfig, load_universe_config
from arc.universe.tiers import (
    DROP_OVER_ACTIVE_CAP,
    DROP_OVER_TIER_SIZE,
    ActiveUniverse,
    DroppedMember,
    Tier,
    TierMember,
    resolve_active,
)

DAY = dt.date(2026, 10, 9)
CORE = [f"C{i}" for i in range(1, 21)]  # 20 core names
MOM = [f"M{i}" for i in range(1, 14)]  # 13 momentum names -> 17 open slots at cap 50


def _tier(tier: Tier, names: list[str]) -> list[TierMember]:
    return [
        TierMember(ticker=t, tier=tier, rank=i, source="t", as_of=DAY)
        for i, t in enumerate(names, 1)
    ]


def _resolve(
    disc: list[str],
    trend: list[str],
    *,
    open_slots: int,
    fill: str = "round_robin",
    core: list[str] = CORE,
    mom: list[str] = MOM,
) -> ActiveUniverse:
    return resolve_active(
        core=_tier(Tier.CORE, core),
        momentum=_tier(Tier.MOMENTUM, mom),
        discoveries=_tier(Tier.DISCOVERY, disc),
        trending=_tier(Tier.TRENDING, trend),
        active_max=len(core) + len(mom) + open_slots,
        tier_sizes={Tier.MOMENTUM: 20, Tier.DISCOVERY: 25, Tier.TRENDING: 25},
        as_of=DAY,
        fill=fill,  # type: ignore[arg-type]
    )


def _d(n: int) -> list[str]:
    return [f"D{i}" for i in range(1, n + 1)]


def _t(n: int) -> list[str]:
    return [f"T{i}" for i in range(1, n + 1)]


def _split(a: ActiveUniverse) -> tuple[int, int]:
    return a.counts["discovery"], a.counts["trending"]


# ---------------------------------------------------------------------------
# Owner examples (card acceptance)
# ---------------------------------------------------------------------------


class TestRoundRobin:
    def test_eight_open_slots_split_four_four(self) -> None:
        a = _resolve(_d(10), _t(10), open_slots=8)
        assert _split(a) == (4, 4)
        assert a.tier_tickers(Tier.DISCOVERY) == _d(4)
        assert a.tier_tickers(Tier.TRENDING) == _t(4)
        assert a.fill == "round_robin" and a.open_slots == 8
        assert a.slots == {"discovery": 4, "trending": 4}

    def test_nine_open_slots_discovery_takes_the_odd_slot(self) -> None:
        a = _resolve(_d(10), _t(10), open_slots=9)
        assert _split(a) == (5, 4)

    def test_discovery_runs_out_trending_spills_in(self) -> None:
        a = _resolve(_d(2), _t(10), open_slots=9)
        assert _split(a) == (2, 7)
        assert a.tier_tickers(Tier.TRENDING) == _t(7)

    def test_trending_runs_out_discovery_spills_in(self) -> None:
        a = _resolve(_d(10), _t(1), open_slots=9)
        assert _split(a) == (8, 1)

    def test_both_short_every_name_fits(self) -> None:
        a = _resolve(_d(3), _t(4), open_slots=17)
        assert _split(a) == (3, 4) and a.dropped == []
        assert a.open_slots == 17 and len(a.members) == 20 + 13 + 7

    def test_live_2026_10_09_shape(self) -> None:
        """Card: core 20, momentum 13, discovery 3, trending 20 -> 3 + 14 (spill)."""
        a = _resolve(_d(3), _t(20), open_slots=17)
        assert _split(a) == (3, 14)
        assert [d.ticker for d in a.dropped] == [f"T{i}" for i in range(15, 21)]

    def test_twelve_discoveries_split_nine_eight(self) -> None:
        """Card: a day with 12 discoveries -> 9/8 instead of D58's 12/5."""
        a = _resolve(_d(12), _t(20), open_slots=17)
        assert _split(a) == (9, 8)
        b = _resolve(_d(12), _t(20), open_slots=17, fill="precedence")
        assert _split(b) == (12, 5)

    def test_name_in_both_counts_once_as_discovery(self) -> None:
        a = _resolve(["X", "D2", "D3"], ["T1", "X", "T3", "T4"], open_slots=4)
        x = next(m for m in a.members if m.ticker == "X")
        assert x.tier is Tier.DISCOVERY and x.also_in == [Tier.TRENDING]
        assert a.tickers.count("X") == 1
        # trending queue is T1, T3, T4 (X held by discovery): X, T1, D2, T3
        assert a.tier_tickers(Tier.DISCOVERY) == ["X", "D2"]
        assert a.tier_tickers(Tier.TRENDING) == ["T1", "T3"]

    def test_name_held_by_core_or_momentum_is_not_in_the_queues(self) -> None:
        a = _resolve(["C1", "D2"], ["M1", "T2"], open_slots=2)
        assert a.tier_tickers(Tier.DISCOVERY) == ["D2"]
        assert a.tier_tickers(Tier.TRENDING) == ["T2"]
        c1 = next(m for m in a.members if m.ticker == "C1")
        assert c1.tier is Tier.CORE and c1.also_in == [Tier.DISCOVERY]

    def test_dropped_tail_carries_reason_and_rank(self) -> None:
        a = _resolve(_d(5), _t(5), open_slots=4)
        over = [d for d in a.dropped if d.reason == DROP_OVER_ACTIVE_CAP]
        assert over == [
            DroppedMember(ticker="D3", tier=Tier.DISCOVERY, reason=DROP_OVER_ACTIVE_CAP, rank=3),
            DroppedMember(ticker="D4", tier=Tier.DISCOVERY, reason=DROP_OVER_ACTIVE_CAP, rank=4),
            DroppedMember(ticker="D5", tier=Tier.DISCOVERY, reason=DROP_OVER_ACTIVE_CAP, rank=5),
            DroppedMember(ticker="T3", tier=Tier.TRENDING, reason=DROP_OVER_ACTIVE_CAP, rank=3),
            DroppedMember(ticker="T4", tier=Tier.TRENDING, reason=DROP_OVER_ACTIVE_CAP, rank=4),
            DroppedMember(ticker="T5", tier=Tier.TRENDING, reason=DROP_OVER_ACTIVE_CAP, rank=5),
        ]

    def test_members_grouped_by_tier_then_rank(self) -> None:
        a = _resolve(_d(6), _t(6), open_slots=7)
        tiers = [m.tier for m in a.members]
        order = [Tier.CORE, Tier.MOMENTUM, Tier.DISCOVERY, Tier.TRENDING]
        assert tiers == sorted(tiers, key=order.index)
        for t in order:
            ranks = [m.rank for m in a.members if m.tier is t]
            assert ranks == sorted(ranks)

    def test_tier_size_cut_before_the_fill(self) -> None:
        a = _resolve(_d(30), [], open_slots=40)
        assert a.counts["discovery"] == 25
        cut = [d for d in a.dropped if d.reason == DROP_OVER_TIER_SIZE]
        assert [d.ticker for d in cut] == [f"D{i}" for i in range(26, 31)]

    def test_cap_below_core_plus_momentum_still_holds(self) -> None:
        """Core and momentum always fit at 50; a lower cap still cuts in tier order."""
        a = resolve_active(
            core=_tier(Tier.CORE, CORE),
            momentum=_tier(Tier.MOMENTUM, MOM),
            discoveries=_tier(Tier.DISCOVERY, _d(3)),
            trending=_tier(Tier.TRENDING, _t(3)),
            active_max=25,
            as_of=DAY,
        )
        assert len(a.members) == 25 and a.open_slots == 0
        assert a.counts == {"core": 20, "momentum": 5, "discovery": 0, "trending": 0}
        assert a.slots == {"discovery": 0, "trending": 0}

    def test_default_fill_is_round_robin(self) -> None:
        a = resolve_active(
            core=_tier(Tier.CORE, ["NVDA"]),
            discoveries=_tier(Tier.DISCOVERY, ["A", "B"]),
            trending=_tier(Tier.TRENDING, ["C", "D"]),
            active_max=3,
            as_of=DAY,
        )
        assert a.fill == "round_robin" and a.tickers == ["NVDA", "A", "C"]


# ---------------------------------------------------------------------------
# precedence = the D58 cut, byte for byte
# ---------------------------------------------------------------------------


def _d58_reference(
    core: list[str], mom: list[str], disc: list[str], trend: list[str], active_max: int
) -> tuple[list[str], list[tuple[str, str, str, int | None]]]:
    """The pre-D67 cut (resolve_active on main before E14.9): dedupe in tier order,
    keep the first *active_max* names, drop the rest as over_active_cap."""
    seen: list[str] = []
    tier_of: dict[str, tuple[str, int]] = {}
    feeds = (("core", core), ("momentum", mom), ("discovery", disc), ("trending", trend))
    for tier, names in feeds:
        for rank, n in enumerate(names, 1):
            if n not in tier_of:
                tier_of[n] = (tier, rank)
                seen.append(n)
    kept = seen[:active_max]
    dropped: list[tuple[str, str, str, int | None]] = [
        (n, tier_of[n][0], DROP_OVER_ACTIVE_CAP, tier_of[n][1]) for n in seen[active_max:]
    ]
    return kept, dropped


class TestPrecedence:
    @pytest.mark.parametrize(("nd", "nt", "slots"), [(3, 20, 17), (12, 20, 17), (25, 25, 9)])
    def test_matches_the_d58_cut(self, nd: int, nt: int, slots: int) -> None:
        a = _resolve(_d(nd), _t(nt), open_slots=slots, fill="precedence")
        kept, dropped = _d58_reference(CORE, MOM, _d(nd), _t(nt), 33 + slots)
        assert a.tickers == kept
        assert [(d.ticker, d.tier.value, d.reason, d.rank) for d in a.dropped] == dropped

    def test_same_payload_as_d58_apart_from_the_new_fields(self) -> None:
        a = _resolve(_d(12), _t(20), open_slots=17, fill="precedence")
        dump = a.model_dump(mode="json", exclude={"fill", "open_slots", "slots"})
        b = ActiveUniverse.model_validate(dump)
        assert b.fill is None and b.slots == {}  # a pre-D67 row loads with the defaults
        assert b.tickers == a.tickers and b.dropped == a.dropped and b.counts == a.counts


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------


@settings(max_examples=200, deadline=None)
@given(
    nd=st.integers(min_value=0, max_value=25),
    nt=st.integers(min_value=0, max_value=25),
    slots=st.integers(min_value=0, max_value=50),
    overlap=st.integers(min_value=0, max_value=5),
)
def test_round_robin_properties(nd: int, nt: int, slots: int, overlap: int) -> None:
    shared = [f"X{i}" for i in range(overlap)]
    disc = shared + _d(nd)
    trend = _t(nt) + shared
    disc, trend = disc[:25], trend[:25]
    a = _resolve(disc, trend, open_slots=slots)
    assert a == _resolve(disc, trend, open_slots=slots)  # deterministic
    q_d = len(disc)
    q_t = len([t for t in trend if t not in set(disc)])
    d, t = _split(a)
    assert d + t == min(slots, q_d + q_t)
    if q_d >= slots and q_t >= slots:  # both queues long enough: alternate
        assert d - t in (0, 1)
    # spill: a tier only goes below its fair share when it ran out
    assert d == min(q_d, max((slots + 1) // 2, slots - q_t))
    assert len(a.tickers) == len(set(a.tickers))
    assert a.slots == {"discovery": d, "trending": t} and a.open_slots == slots


# ---------------------------------------------------------------------------
# Config + registry
# ---------------------------------------------------------------------------


class TestConfig:
    def test_yaml_default_is_round_robin(self) -> None:
        assert load_universe_config().active_fill == "round_robin"
        assert UniverseConfig().active_fill == "round_robin"

    def test_override_switches_it_off(self) -> None:
        cfg = load_universe_config(overrides={("active_fill",): "precedence"})
        assert cfg.active_fill == "precedence"

    def test_bad_value_refused(self) -> None:
        with pytest.raises(ValueError, match="active_fill"):
            load_universe_config(overrides={("active_fill",): "alternate"})

    def test_registry_choice_key(self) -> None:
        t = lookup("universe.active_fill")
        assert t.type is ValueType.CHOICE and t.target is Target.UNIVERSE
        assert t.path == ("active_fill",) and t.risk is Risk.ANY
        assert t.choices == ("round_robin", "precedence")
        assert t.bounds == "round_robin | precedence"
        assert parse_value(t, "precedence") == "precedence"
        assert lookup("active_fill") is t
