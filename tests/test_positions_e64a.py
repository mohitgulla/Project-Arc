"""E6.4a (Analyst A-1): the remaining-EV floor no longer closes fresh positions.

1. Parity: at unchanged marks the position manager's remaining EV equals the
   propose step's managed Net EV + entry costs + the cost to close now (same paths).
2. Floor timing: ``positions.remaining_ev_floor_eod_only`` (default on) evaluates the
   floor on end-of-day marks only; the three 2026-10-01 fill-day reviews no longer
   close, a genuinely decayed position at an EOD mark still does.
3. Live Net EV floor: ``ranking.filters.min_managed_net_ev`` gates the propose step.
4. Observability: floor-exit facts for ``arc journal explain``/tower; same-day bucket.
"""

from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal as D
from typing import TYPE_CHECKING

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from arc.backtest.costs import load_cost_model
from arc.exits.model import model_exits
from arc.exits.policy import load_exit_config
from arc.exits.position import OpenPosition, PositionMarks, evaluate_position
from arc.journal.floor_exit import floor_exit_facts, floor_exit_line
from arc.models import Leg, LegIntent, Structure, StructureKind
from arc.positions.evaluate import SignalKind, review_position
from arc.pricing.bs import BSMInputs, OptionKind, price
from arc.scanner.rank import RankFilters, live_net_ev_check, load_ranking_config
from arc.structures import debit_vertical

if TYPE_CHECKING:
    from arc.exits import ExitConfig

R = 0.04
EXITS = load_exit_config()
COST = load_cost_model()


def _vertical(
    long: tuple[str, str], short: tuple[str, str], net: str, width: int, dte: int
) -> Structure:
    """A live 2026-10-01 debit vertical (legs/mids as stored on the proposal)."""
    max_loss = D(net) * 100
    return Structure(
        legs=[
            Leg(occ_symbol=long[0], side=LegIntent.LONG, premium=D(long[1])),
            Leg(occ_symbol=short[0], side=LegIntent.SHORT, premium=D(short[1])),
        ],
        kind=StructureKind.VERTICAL_DEBIT,
        net_debit_credit=D(net),
        max_gain=D(width) * 100 - max_loss,
        max_loss=max_loss,
        breakevens=[],
        dte=dte,
        buying_power=max_loss,
    )


# The three 2026-10-01 fills (legs/mids at the proposal, spot/ATM IV/RV forecast
# from the proposal's exit_model context; fill net from open_structures).
IWM = dict(
    st=_vertical(("IWM261120P00280000", "9.32"), ("IWM261120P00268000", "4.725"), "4.595", 12, 50),
    spot=276.54, iv=0.20551, rv=0.11778, entry=4.6,
    marks=[  # 10-01 reviews that fired the floor: (at, long mid, short mid)
        ("2026-10-01T14:22:00Z", 9.32, 4.60), ("2026-10-01T14:49:00Z", 9.10, 4.585),
    ],
)  # fmt: skip
SPY = dict(
    st=_vertical(("SPY261106P00765000", "14.73"), ("SPY261106P00740000", "7.08"), "7.65", 25, 36),
    spot=760.63, iv=0.149157, rv=0.108644, entry=7.62,
    marks=[("2026-10-01T14:49:00Z", 14.80, 7.045)],
)  # fmt: skip
NFLX = dict(
    st=_vertical(("NFLX261120P00067500", "3.665"), ("NFLX261120P00062500", "1.715"), "1.95", 5, 50),
    spot=68.165, iv=0.418147, rv=0.356606, entry=2.04,
    marks=[("2026-10-01T15:18:00Z", 3.66, 1.68)],
)  # fmt: skip
LIVE = {"IWM": IWM, "SPY": SPY, "NFLX": NFLX}


def _marks(
    case: dict, mids: dict[str, float], *, eod: bool, as_of=dt.date(2026, 10, 1)
) -> PositionMarks:  # type: ignore[no-untyped-def, type-arg]
    return PositionMarks(
        as_of=as_of, leg_mids=mids, spot=case["spot"], iv=case["iv"], r=R,
        realized_vol=case["rv"], end_of_day=eod,
    )  # fmt: skip


def _fill_mids(s: Structure) -> dict[str, float]:
    return {leg.occ_symbol: float(leg.premium or 0) for leg in s.legs}


# ---------------------------------------------------------------------------
# 1. Parity
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", list(LIVE))
def test_parity_remaining_ev_equals_entry_managed_ev_plus_round_trip_costs(name: str) -> None:
    """Same structure, same marks: the two models agree up to the costs actually paid.

    remaining_net_ev = managed.net_ev + entry_costs - close_now_net   (exact, same paths)
    Before E6.4a the remaining EV compared the paths with the flat-IV model value, so
    the model-vs-market gap (+$64/unit on IWM) entered the entry EV but not the
    remaining EV, and a +EV open read as a -0.11 per $BP position minutes later.
    """
    c = LIVE[name]
    s = c["st"]
    pol = EXITS.policy_for(s.kind)
    m = model_exits(s, pol, spot=c["spot"], iv=c["iv"], r=R, cfg=EXITS.model, cost=COST,
                    realized_vol=c["rv"])  # fmt: skip
    state = evaluate_position(
        OpenPosition(structure=s, entry_net=float(s.net_debit_credit)),
        _marks(c, _fill_mids(s), eod=False), pol, cost=COST, cfg=EXITS.model,
    )  # fmt: skip
    assert state.remaining_net_ev is not None and state.close_now_net is not None
    assert state.remaining_net_ev == pytest.approx(
        m.managed.net_ev + m.entry_costs - state.close_now_net, abs=0.05
    )
    # entry costs paid + the cost of closing now are the whole gap, both >= 0
    assert m.entry_costs >= 0 and -state.close_now_net >= 0


def test_parity_iwm_fresh_position_no_longer_reads_far_below_the_floor() -> None:
    """IWM read -0.111 per $BP 6 min after a +EV open; at fill marks it is now >= the
    entry EV per $BP (costs already paid are sunk, the close cost is avoided)."""
    s = IWM["st"]
    m = model_exits(s, EXITS.policy_for(s.kind), spot=IWM["spot"], iv=IWM["iv"], r=R,
                    cfg=EXITS.model, cost=COST, realized_vol=IWM["rv"])  # fmt: skip
    rv = review_position(
        structure_id="os-iwm", ticker="IWM",
        position=OpenPosition(structure=s, entry_net=IWM["entry"], contracts=2),
        marks=_marks(IWM, _fill_mids(s), eod=False), exits=EXITS, cost=COST,
    )  # fmt: skip
    bp = float(s.buying_power)
    assert rv.remaining_ev_per_bp is not None
    assert rv.remaining_ev_per_bp >= m.managed.net_ev / bp - 1e-6
    assert rv.remaining_ev_per_bp > -0.01  # the floor


@settings(max_examples=25, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    width=st.integers(2, 20),
    depth=st.floats(0.0, 0.08),
    iv=st.floats(0.12, 0.6),
    rv_ratio=st.floats(0.5, 1.3),
    dte=st.integers(10, 60),
)
def test_property_fresh_position_remaining_ev_at_least_entry_ev_minus_close_cost(
    width: int, depth: float, iv: float, rv_ratio: float, dte: int
) -> None:
    """remaining_ev_per_bp >= managed_net_ev_per_bp - close_cost_per_bp - MC tol, for a
    freshly filled debit vertical at unchanged marks (Analyst A-1 property)."""
    spot = 100.0
    exp = dt.date(2026, 10, 1) + dt.timedelta(days=dte)
    k_long = round(spot * (1 + depth))
    k_short = k_long - width

    def put(k: float) -> float:
        return price(BSMInputs(S=spot, K=k, t=dte / 365, r=R, sigma=iv, flag=OptionKind.PUT))

    s = debit_vertical("p", "XYZ", exp, long_strike=k_long, long_premium=round(put(k_long), 2),
                       short_strike=k_short, short_premium=round(put(k_short), 2),
                       as_of=dt.date(2026, 10, 1))  # fmt: skip
    if s.buying_power is None or float(s.buying_power) <= 0:
        return
    pol = EXITS.policy_for(s.kind)
    m = model_exits(s, pol, spot=spot, iv=iv, r=R, cfg=EXITS.model, cost=COST,
                    realized_vol=iv * rv_ratio)  # fmt: skip
    case = {"spot": spot, "iv": iv, "rv": iv * rv_ratio}
    state = evaluate_position(
        OpenPosition(structure=s, entry_net=float(s.net_debit_credit)),
        _marks(case, _fill_mids(s), eod=False), pol, cost=COST, cfg=EXITS.model,
    )  # fmt: skip
    if state.remaining_net_ev is None or state.close_now_net is None:
        return
    bp = float(s.buying_power)
    close_cost = -state.close_now_net  # value 0 at mid -> proceeds net = -costs
    tol = 0.002
    assert state.remaining_net_ev / bp >= m.managed.net_ev / bp - close_cost / bp - tol


# ---------------------------------------------------------------------------
# 2. Floor timing (EOD only)
# ---------------------------------------------------------------------------


def _replay(case: dict, at: str, lmid: float, smid: float, *, eod: bool, exits: ExitConfig = EXITS):  # type: ignore[no-untyped-def, type-arg]
    s = case["st"]
    mids = {s.legs[0].occ_symbol: lmid, s.legs[1].occ_symbol: smid}
    return review_position(
        structure_id="os-x", ticker="X",
        position=OpenPosition(structure=s, entry_net=case["entry"]),
        marks=_marks(case, mids, eod=eod), exits=exits, cost=COST,
        minutes_since_fill=30.0,
    )  # fmt: skip


@pytest.mark.parametrize(
    ("name", "at", "lmid", "smid"),
    [(n, at, lm, sm) for n, c in LIVE.items() for (at, lm, sm) in c["marks"]],
)
def test_replay_10_01_intraday_reviews_no_floor_exit(
    name: str, at: str, lmid: float, smid: float
) -> None:
    """The 2026-10-01 fill-day intraday reviews that closed 3/3 positions: none fires now."""
    rv = _replay(LIVE[name], at, lmid, smid, eod=False)
    assert all(s.kind is not SignalKind.REMAINING_EV_FLOOR for s in rv.signals), rv.signals
    assert rv.ev_floor_window == "eod" and rv.ev_floor_live is False
    assert rv.end_of_day is False and rv.ev_floor == pytest.approx(-0.01)


def test_floor_eligible_first_on_the_eod_mark() -> None:
    """The first evaluation where the floor may fire is the EOD-marks window."""
    rv = _replay(IWM, "2026-10-01T19:50:00Z", 9.32, 4.60, eod=True)
    assert rv.ev_floor_live is True and rv.end_of_day is True


def test_decayed_position_at_eod_mark_still_closes() -> None:
    """A genuinely decayed debit spread (remaining EV/$BP well below -0.01) on an EOD
    mark, days held >= 1, still fires the floor."""
    s = IWM["st"]
    # next day, IWM +3.4% against the put spread and the spread still marks rich vs the
    # model: holding is worth less than closing (remaining EV/$BP about -0.09)
    case = dict(IWM, spot=286.0, iv=0.20)
    mids = {s.legs[0].occ_symbol: 4.00, s.legs[1].occ_symbol: 1.20}
    rv = review_position(
        structure_id="os-iwm", ticker="IWM",
        position=OpenPosition(structure=s, entry_net=4.6),
        marks=_marks(case, mids, eod=True, as_of=dt.date(2026, 10, 2)),
        exits=EXITS, cost=COST, minutes_since_fill=1530.0,
    )  # fmt: skip
    assert rv.remaining_ev_per_bp is not None and rv.remaining_ev_per_bp < -0.01
    kinds = [x.kind for x in rv.signals]
    assert SignalKind.REMAINING_EV_FLOOR in kinds, (rv.remaining_ev_per_bp, rv.signals)
    floor_sig = next(x for x in rv.signals if x.kind is SignalKind.REMAINING_EV_FLOOR)
    assert "end-of-day marks" in floor_sig.detail


def test_floor_intraday_mode_restores_old_window() -> None:
    """``remaining_ev_floor_eod_only: false`` evaluates the floor on every review."""
    s = IWM["st"]
    exits = EXITS.model_copy(
        update={
            "positions": EXITS.positions.model_copy(update={"remaining_ev_floor_eod_only": False})
        }
    )
    case = dict(IWM, spot=286.0, iv=0.20)
    mids = {s.legs[0].occ_symbol: 4.00, s.legs[1].occ_symbol: 1.20}
    rv = review_position(
        structure_id="os-iwm", ticker="IWM",
        position=OpenPosition(structure=s, entry_net=4.6),
        marks=_marks(case, mids, eod=False), exits=exits, cost=COST,
    )  # fmt: skip
    assert rv.ev_floor_window == "intraday" and rv.ev_floor_live is True
    assert any(x.kind is SignalKind.REMAINING_EV_FLOOR for x in rv.signals)


def test_review_records_entry_ev_and_minutes_since_fill() -> None:
    s = IWM["st"]
    rv = review_position(
        structure_id="os-iwm", ticker="IWM",
        position=OpenPosition(structure=s, entry_net=4.6),
        marks=_marks(IWM, _fill_mids(s), eod=False), exits=EXITS, cost=COST,
        entry_managed_net_ev=69.07, minutes_since_fill=6.0,
    )  # fmt: skip
    assert rv.entry_managed_net_ev == 69.07
    assert rv.entry_managed_net_ev_per_bp == pytest.approx(69.07 / 459.5, abs=1e-6)
    assert rv.minutes_since_fill == 6.0


def test_exits_yaml_default_is_eod_only() -> None:
    assert EXITS.positions.remaining_ev_floor_eod_only is True


# ---------------------------------------------------------------------------
# 3. Live Net EV floor
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("ticker", "net_ev", "ok"),
    # The E2.4 managed Net EV frozen in each 2026-10-01 proposal's market context at
    # the fill-time re-price ($/unit). NB the card quoted quant_json.ev (+69.07 /
    # +218.42 / -0.94), which is the scanner's ev_proxy, not the managed Net EV.
    [("NFLX", -17.70, False), ("IWM", -2.49, False), ("SPY", 113.81, True), ("ZERO", 0.0, False)],
)
def test_live_net_ev_floor_10_01_proposals(ticker: str, net_ev: float, ok: bool) -> None:
    """NFLX 15:07Z is rejected; SPY passes; the floor is strict > (IWM was -EV too)."""
    f = load_ranking_config().filters
    passed, why = live_net_ev_check(net_ev, f)
    assert passed is ok, (ticker, why)
    # the card's ev_proxy number for NFLX would also be rejected
    assert live_net_ev_check(-0.94, f)[0] is False


def test_live_net_ev_floor_fails_closed_without_a_model_and_can_be_switched_off() -> None:
    f = load_ranking_config().filters
    assert f.live is True and f.min_managed_net_ev == 0.0
    assert live_net_ev_check(None, f)[0] is False
    assert live_net_ev_check(-5.0, RankFilters(live=False))[0] is True
    assert live_net_ev_check(-5.0, RankFilters(enabled=False))[0] is True


def test_ranking_overrides_reach_the_live_floor() -> None:
    cfg = load_ranking_config(overrides={("ranking", "filters", "min_managed_net_ev"): -2.0})
    assert live_net_ev_check(-0.94, cfg.filters)[0] is True


# ---------------------------------------------------------------------------
# 4. Observability
# ---------------------------------------------------------------------------


def _floor_db():  # type: ignore[no-untyped-def]
    from arc.store.db import connect
    from arc.store.migrate import migrate

    c = connect(":memory:")
    migrate(c)
    return c


def test_floor_exit_facts_none_without_a_floor_decision() -> None:
    c = _floor_db()
    assert floor_exit_facts(c, "nohash") is None


def test_floor_exit_line_renders_every_fact() -> None:
    from arc.journal.floor_exit import FloorExitFacts

    f = FloorExitFacts(
        exit_proposal_hash="x", remaining_ev_per_bp=-0.111, floor=-0.01,
        entry_managed_net_ev=69.07, entry_managed_net_ev_per_bp=0.1503,
        minutes_since_fill=6.0, days_held=0, window="intraday",
    )  # fmt: skip
    line = floor_exit_line(f)
    for frag in ("-0.1110", "-0.0100", "+0.1503", "+69.07", "6 min since fill",
                 "days held 0", "intraday marks"):  # fmt: skip
        assert frag in line, (frag, line)
    old = floor_exit_line(f.model_copy(update={"window": None}))
    assert "pre-E6.4a" in old


def test_review_payload_round_trips_new_fields() -> None:
    s = IWM["st"]
    rv = _replay(IWM, "x", 9.32, 4.60, eod=False)
    data = json.loads(rv.model_dump_json())
    assert {"end_of_day", "ev_floor", "ev_floor_window", "ev_floor_live",
            "entry_managed_net_ev", "minutes_since_fill"} <= set(data)  # fmt: skip
    assert s.kind is StructureKind.VERTICAL_DEBIT


def test_floor_exit_facts_from_the_store_and_explain() -> None:
    """A floor exit written the way positions.evaluate writes it surfaces every fact."""
    from arc.journal.reasons import JournalPersona, ReasonCode, Stage
    from arc.journal.store import JournalStore
    from arc.journal.views import explain, explain_lines

    c = _floor_db()
    c.execute("PRAGMA foreign_keys = OFF")  # facts reader only; no candidate/proposal rows
    s = IWM["st"]
    c.execute(
        """INSERT INTO open_structures (id, ticker, open_proposal_hash, candidate_id,
               structure_json, contracts, entry_net, opened_at, status, exit_proposal_hash,
               exit_reason)
           VALUES ('os-iwm', 'IWM', 'h-open', 'cand-1', ?, 2, 4.6,
                   '2026-10-01T14:16:00+00:00', 'closed', 'h-close', 'remaining_ev_floor')""",
        (s.model_dump_json(),),
    )
    c.execute(
        "INSERT INTO market_contexts (id, proposal_hash, payload, created_at) VALUES (?,?,?,?)",
        (
            "mc-1",
            "h-open",
            json.dumps({"analytics": {"exit_model": {"managed": {"net_ev": 69.07}}}}),
            "2026-10-01T14:14:00+00:00",
        ),
    )
    rv = _replay(IWM, "x", 9.32, 4.60, eod=True)
    c.execute(
        """INSERT INTO proposals (id, candidate_id, proposal_hash, structure_json, thesis,
               quant_json, sizing_json, expires_at, created_at, ticker, kind)
           VALUES ('p-close', 'cand-1', 'h-close', ?, 'exit', '{}', '{}',
                   '2026-10-01T20:00:00+00:00', '2026-10-01T19:52:00+00:00', 'IWM', 'close')""",
        (s.model_dump_json(),),
    )
    JournalStore(c).record(
        persona=JournalPersona.INVESTOR, stage=Stage.EXIT, subject="IWM", choice="selected",
        reason_code=ReasonCode.EXIT_EV_FLOOR, proposal_hash="h-close",
        payload={"structure_id": "os-iwm",
                 "review": rv.model_dump(mode="json", exclude={"structure"})},
        at=dt.datetime(2026, 10, 1, 19, 52, tzinfo=dt.UTC),
    )  # fmt: skip
    c.commit()
    for h in ("h-close", "h-open"):
        f = floor_exit_facts(c, h)
        assert f is not None and f.structure_id == "os-iwm" and f.open_proposal_hash == "h-open"
        assert f.window == "end_of_day" and f.floor_mode == "eod" and f.floor == -0.01
        assert f.entry_managed_net_ev == 69.07
        assert f.entry_managed_net_ev_per_bp == pytest.approx(69.07 / 459.5, abs=1e-6)
        assert f.minutes_since_fill == 30.0  # recorded on the review
        assert f.days_held == 0 and f.remaining_ev_per_bp == rv.remaining_ev_per_bp
    rep = explain(c, "h-close")
    text = "\n".join(explain_lines(rep))
    assert "remaining-EV floor exit" in text and "end-of-day marks" in text
