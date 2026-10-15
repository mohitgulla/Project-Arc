"""E23.0 (D98): options OI walls + the levels merge into technicals, pure."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

import pytest

from arc.features.levels import LevelsPayload, VolumeProfileLevels, merge_levels
from arc.features.oi_walls import OiWalls, oi_walls
from arc.features.technicals import TechnicalFeatures

E1, E2, E3 = dt.date(2026, 11, 20), dt.date(2026, 12, 18), dt.date(2027, 1, 15)


@dataclass(frozen=True)
class C:
    strike: float
    option_type: str
    open_interest: int | None
    expiration: dt.date = E1


def test_call_wall_above_put_wall_below_and_max_strike() -> None:
    chain = [
        C(190, "call", 5000),
        C(195, "call", 3000),
        C(175, "call", 9000),  # below spot: not a call wall, but counts toward the total
        C(175, "put", 4000),
        C(170, "put", 6000),
        C(185, "put", 100),  # above spot: never a put wall
    ]
    w = oi_walls(chain, 180.0, [E1])
    assert w is not None
    assert (w.call_wall, w.call_wall_oi) == (190, 5000)
    assert (w.put_wall, w.put_wall_oi) == (170, 6000)
    assert (w.max_oi_strike, w.max_oi) == (175, 13000)
    assert w.contracts_with_oi == 6 and w.expiries == [E1]


def test_oi_is_summed_across_the_given_expiries_only() -> None:
    chain = [
        C(190, "call", 3000, E1),
        C(190, "call", 3000, E2),
        C(200, "call", 5000, E1),
        C(210, "call", 99_000, E3),  # outside the expiries
    ]
    w = oi_walls(chain, 180.0, [E1, E2])
    assert w is not None and w.call_wall == 190 and w.call_wall_oi == 6000
    assert w.expiries == [E1, E2]


def test_ties_go_to_the_strike_nearest_spot_then_the_lower() -> None:
    chain = [C(200, "call", 1000), C(190, "call", 1000), C(170, "put", 700), C(160, "put", 700)]
    w = oi_walls(chain, 180.0, [E1])
    assert w is not None and w.call_wall == 190 and w.put_wall == 170
    eq = oi_walls([C(170, "put", 500), C(190, "call", 500)], 180.0, [E1])
    assert eq is not None and eq.max_oi_strike == 170  # equidistant: the lower strike


def test_no_oi_gives_no_walls() -> None:
    w = oi_walls([C(190, "call", None), C(170, "put", 0)], 180.0, [E1])
    assert w is not None
    assert (w.call_wall, w.put_wall, w.max_oi_strike, w.contracts_with_oi) == (None, None, None, 0)
    assert oi_walls([], 180.0, [E1]) == OiWalls(spot=180.0)


def test_strikes_on_one_side_only() -> None:
    calls = oi_walls([C(190, "call", 900), C(200, "call", 800)], 180.0, [E1])
    assert calls is not None and calls.call_wall == 190 and calls.put_wall is None
    puts = oi_walls([C(170, "put", 900), C(160, "P", 950)], 180.0, [E1])
    assert puts is not None and puts.put_wall == 160 and puts.call_wall is None


def test_min_oi_floor_and_bad_spot() -> None:
    chain = [C(190, "call", 400), C(170, "put", 600), C(175, "x", 9999)]
    w = oi_walls(chain, 180.0, [E1], min_oi=500)
    assert w is not None and w.call_wall is None and w.put_wall == 170
    assert w.contracts_with_oi == 2  # the unknown type is ignored
    assert oi_walls(chain, 0.0, [E1]) is None
    assert oi_walls(chain, float("nan"), [E1]) is None


# ---------------------------------------------------------------------------
# merge_levels
# ---------------------------------------------------------------------------

DAY = dt.date(2026, 10, 12)


def _tech(close: float = 180.0, atr: float | None = 4.0) -> TechnicalFeatures:
    return TechnicalFeatures(as_of=DAY, bar_date=DAY, close=close, atr14=atr)


def _levels(**kw: object) -> LevelsPayload:
    vp = VolumeProfileLevels(
        bin_width=0.4, bins=50, total_volume=1e6, poc=182.4, val=178.0, vah=186.0,
        va_share=0.71, hvns=[172.0, 182.4, 190.0], lvns=[176.0], sessions=20,
        first_session=dt.date(2026, 9, 14), last_session=dt.date(2026, 10, 9),
        bar="15Min", feed="sip", bars=520, atr14=4.0,
    )  # fmt: skip
    walls = OiWalls(spot=180.0, call_wall=190.0, put_wall=175.0, max_oi_strike=180.0)
    return LevelsPayload(
        ticker="X",
        as_of=DAY,
        volume_profile=kw.get("vp", vp),  # type: ignore[arg-type]
        oi_walls=kw.get("walls", walls),  # type: ignore[arg-type]
    )


def test_merge_sets_levels_and_atr_distances() -> None:
    t = merge_levels(_tech(), _levels())
    assert (t.vp_poc, t.vp_val, t.vp_vah) == (182.4, 178.0, 186.0)
    assert t.vp_pos == pytest.approx(0.25)  # (180 - 178) / 8
    assert t.dist_poc_atr == pytest.approx(-0.6)
    assert t.dist_vah_atr == pytest.approx(-1.5) and t.dist_val_atr == pytest.approx(0.5)
    assert (t.vp_hvn_above, t.vp_hvn_below) == (182.4, 172.0)
    assert (t.oi_call_wall, t.oi_put_wall, t.oi_max_strike) == (190.0, 175.0, 180.0)
    assert t.dist_call_wall_atr == pytest.approx(2.5)
    assert t.dist_put_wall_atr == pytest.approx(1.25)


def test_vp_pos_is_unclipped() -> None:
    assert merge_levels(_tech(close=190.0), _levels()).vp_pos == pytest.approx(1.5)
    assert merge_levels(_tech(close=170.0), _levels()).vp_pos == pytest.approx(-1.0)


def test_merge_without_atr_keeps_prices_drops_distances() -> None:
    t = merge_levels(_tech(atr=None), _levels())
    assert t.vp_poc == 182.4 and t.oi_call_wall == 190.0
    assert t.dist_poc_atr is None and t.dist_call_wall_atr is None
    assert merge_levels(_tech(atr=0.0), _levels()).dist_val_atr is None


def test_merge_switches_and_missing_parts() -> None:
    base = _tech()
    assert merge_levels(base, None) is base
    assert merge_levels(base, _levels(), volume_profile=False, oi_walls=False) is base
    only_vp = merge_levels(base, _levels(), oi_walls=False)
    assert only_vp.vp_poc == 182.4 and only_vp.oi_call_wall is None
    only_oi = merge_levels(base, _levels(), volume_profile=False)
    assert only_oi.vp_poc is None and only_oi.oi_put_wall == 175.0
    assert merge_levels(base, _levels(vp=None, walls=None)) is base


def test_zero_width_value_area_has_no_pos() -> None:
    lv = _levels()
    assert lv.volume_profile is not None
    flat = lv.volume_profile.model_copy(update={"val": 180.0, "vah": 180.0, "poc": 180.0})
    t = merge_levels(_tech(), lv.model_copy(update={"volume_profile": flat}))
    assert t.vp_pos is None and t.vp_poc == 180.0
